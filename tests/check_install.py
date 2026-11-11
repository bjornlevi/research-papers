"""Build real distribution artifacts and verify a source-free installed environment."""

from contextlib import ExitStack
from email.parser import Parser
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests/portable"))
from fixtures import corpus
from test_releases import fixture_bundle
from test_mcp import protocol_cases
from test_http import running_server
from research_library import releases


def run(command, *, cwd, expected=0):
    result = subprocess.run([str(part) for part in command], cwd=cwd, capture_output=True, text=True)
    if result.returncode != expected:
        raise RuntimeError(f"Command failed: {command}\n{result.stdout}\n{result.stderr}")
    return result


def inspect_members(names):
    denied = {".pdf", ".docx", ".xlsx", ".png", ".sqlite", ".jsonl", ".csv"}
    for name in names:
        path = Path(name)
        private_directories = {"papers", "evidence", "releases", "highlights", ".venv"}
        if path.suffix.lower() in denied or private_directories.intersection(path.parts) or ".env" in path.parts:
            raise ValueError(f"Restricted or runtime file in distribution: {name}")
        if path.suffix == ".json" and not name.endswith("schemas/contract-v2.json"):
            raise ValueError(f"Unexpected data JSON in distribution: {name}")


def main():
    with ExitStack() as stack:
        temporary = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="research-library-install-")))
        synthetic = stack.enter_context(corpus())
        release_bundle, _ = fixture_bundle(temporary)
        release_input = temporary / "bundle.json"
        release_input.write_text(json.dumps(release_bundle))
        artifacts = temporary / "artifacts"
        run([sys.executable, "-m", "build", "--outdir", artifacts], cwd=ROOT)
        wheel, = artifacts.glob("*.whl")
        source, = artifacts.glob("*.tar.gz")
        with zipfile.ZipFile(wheel) as archive:
            inspect_members(archive.namelist())
            assert "research_library/schemas/contract-v2.json" in archive.namelist()
            assert not any(name.startswith("istopmm_evidence/") for name in archive.namelist())
            metadata_file, = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            metadata = Parser().parsestr(archive.read(metadata_file).decode())
            assert metadata["Name"] == "research-library"
            for dependency in metadata.get_all("Requires-Dist", []):
                if dependency.startswith("PyMuPDF"):
                    assert 'extra == "build"' in dependency, dependency
        with tarfile.open(source) as archive:
            inspect_members(archive.getnames())
        environment = temporary / "venv"
        venv.EnvBuilder(with_pip=False).create(environment)
        python = environment / "bin/python"
        run([sys.executable, "-m", "pip", "--python", python, "install", wheel], cwd=temporary)
        run([python, "-c",
             "import importlib.util,importlib.metadata; from research_library import __version__,contracts; "
             "assert importlib.util.find_spec('pymupdf') is None; "
             "assert importlib.util.find_spec('istopmm_evidence') is None; "
             "assert importlib.metadata.version('research-library') == __version__; "
             "assert len(contracts.TOOLS)==12"], cwd=temporary)
        run([environment / "bin/research-library", "--help"], cwd=temporary)
        run([environment / "bin/research-build", "--help"], cwd=temporary)
        run([environment / "bin/research-release", "--help"], cwd=temporary)
        run([environment / "bin/research-mcp", "--help"], cwd=temporary)
        run([environment / "bin/research-evaluate", "--help"], cwd=temporary)
        run([environment / "bin/research-check-cards", "--help"], cwd=temporary)
        run([environment / "bin/research-http", "--help"], cwd=temporary)
        run([environment / "bin/research-http-token", "--help"], cwd=temporary)
        run([environment / "bin/research-collections", "--help"], cwd=temporary)
        run([environment / "bin/research-literature", "--help"], cwd=temporary)
        run([environment / "bin/research-web", "--help"], cwd=temporary)
        run([environment / "bin/research-mcp-check", "--help"], cwd=temporary)
        run([python, "-m", "research_library.quality_cli", "--help"], cwd=temporary)
        policy = run([environment / "bin/research-literature", "policy"], cwd=temporary)
        assert json.loads(policy.stdout)["preprints"] == "excluded"
        missing = run([environment / "bin/research-build", "--data-root", synthetic,
                       "literature-ingest", "--help"], cwd=temporary, expected=2)
        assert "[build]" in missing.stderr
        for arguments in (["search", "screening"], ["claims", "--measure", "subdistribution_hazard_ratio"],
                          ["claim", "fictional-claim-1"], ["results"]):
            response = run([environment / "bin/research-library", "--data-root", synthetic, *arguments],
                           cwd=temporary)
            assert json.loads(response.stdout)
        released = temporary / "release"
        run([environment / "bin/research-release", "export", release_input, released,
             "--profile", "synthetic"], cwd=temporary)
        run([environment / "bin/research-release", "validate", released,
             "--allow-profile", "synthetic"], cwd=temporary)
        quality_report = run([python, "-m", "research_library.quality_cli", "--release", released,
                              "--allow-profile", "synthetic", "report"], cwd=temporary)
        assert not json.loads(quality_report.stdout)["ready"]
        run([python, "-m", "research_library.quality_cli", "--release", released,
             "--allow-profile", "synthetic", "check"], cwd=temporary, expected=1)
        missing_inventory = run([python, "-m", "research_library.quality_cli", "--release", released,
                                 "--allow-profile", "synthetic", "inventory", "--papers", synthetic],
                                cwd=temporary, expected=1)
        assert "[build]" in missing_inventory.stderr
        run([python, "-c",
             "import sys,httpx; from research_library.web_server import create_app; "
             "app=create_app(release=sys.argv[1], profiles={'synthetic'}); "
             "client=httpx.Client(transport=httpx.WSGITransport(app=app),base_url='http://127.0.0.1:8080'); "
             "response=client.get('/search?query=screening'); "
             "assert response.status_code==200 and 'Read source context' in response.text; "
             "assert client.get('/reader.js').status_code==200; "
             "response=client.get('/connect'); "
             "assert response.status_code==200 and 'research-mcp-check' in response.text; client.close()", released], cwd=temporary)
        expected = temporary / "expected.json"
        expected.write_text(json.dumps(protocol_cases(
            releases.validate(released, allowed_profiles={"synthetic"}))))
        client_environment = temporary / "client-venv"
        venv.EnvBuilder(with_pip=False).create(client_environment)
        client_python = client_environment / "bin/python"
        run([sys.executable, "-m", "pip", "--python", client_python, "install", "mcp>=1.30,<1.31"],
            cwd=temporary)
        harness = temporary / "mcp_client.py"
        shutil.copy2(ROOT / "tests/mcp_client.py", harness)
        http_harness = temporary / "http_client.py"
        shutil.copy2(ROOT / "tests/http_client.py", http_harness)
        run([client_python, "-c", "import importlib.util; assert importlib.util.find_spec('research_library') is None"],
            cwd=temporary)
        before = {p: p.read_bytes() for p in released.rglob("*") if p.is_file()}
        for p in released.rglob("*"):
            p.chmod(0o555 if p.is_dir() else 0o444)
        released.chmod(0o555)
        try:
            run([client_python, harness, "--server", environment / "bin/research-mcp",
                 "--release", released, "--expected", expected, "--cwd", temporary], cwd=temporary)
            http_root = temporary / "http"
            http_root.mkdir()
            with running_server(http_root, released, python=python) as (url, token, registry, identifier, log):
                run([client_python, http_harness, "--url", url, "--token-file", http_root / "token",
                     "--expected", expected], cwd=temporary)
            assert all(p.read_bytes() == content for p, content in before.items())
        finally:
            released.chmod(0o755)
            for p in released.rglob("*"):
                p.chmod(0o755 if p.is_dir() else 0o644)
        for tool, arguments in (
            ("corpus_status", {}), ("search_evidence", {"query": "screening"}),
            ("get_claim", {"claim_id": "fictional-claim"}),
            ("get_source_context", {"unit_id": "fictional-word-footnote"}),
            ("get_table", {"table_id": "fictional-table", "limit": 1}),
            ("get_source_issue", {"issue_id": "fictional-issue"}),
            ("get_related_evidence", {"evidence_id": "fictional-claim"}),
        ):
            response = run([environment / "bin/research-library", "--release", released,
                            "--allow-profile", "synthetic", "tool", tool, json.dumps(arguments)],
                           cwd=temporary)
            assert json.loads(response.stdout)["ok"]
        # Copy tests/examples only; no application sources or source-derived evidence.
        clean = temporary / "unrelated-project"
        shutil.copytree(ROOT / "tests", clean / "tests",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(ROOT / "examples", clean / "examples",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        run([sys.executable, "-m", "pip", "--python", python, "install", f"{wheel}[build]"], cwd=clean)
        run([environment / "bin/research-project", "--help"], cwd=clean)
        for tool in ("literature-ingest", "citation-neighborhood", "literature-update"):
            run([environment / "bin/research-build", "--data-root", clean, tool, "--help"], cwd=clean)
        run([python, "tests/run.py", "portable"], cwd=clean)
        run([python, "tests/check_evaluation.py"], cwd=clean)
        run([python, "tests/check_http.py"], cwd=clean)
        run([python, "tests/check_collections.py"], cwd=clean)
        run([python, "tests/check_catalog.py"], cwd=clean)
        missing = run([python, "tests/run.py", "collection"], cwd=clean, expected=2)
        assert "authorized local corpus" in missing.stderr
        run([environment / "bin/research-build", "--data-root", synthetic,
             "evidence", "claims"], cwd=clean)
        print(f"PASS: {wheel.name} and {source.name}; no source assets in artifacts.")
        print("PASS: serving-only install without PyMuPDF, all retrieval commands outside checkout.")
        print("PASS: serving-only release export/validation and versioned tools outside checkout.")
        print("PASS: independent SDK-only client environment exercises installed MCP server against read-only release.")
        print("PASS: independently installed HTTP server and SDK-only client outside checkout, plus HTTP load/rollback.")
        print("PASS: portable suite in source-free project; missing private collection fails explicitly.")
        print("Build dependencies:", importlib.metadata.version("build"),
              "Python:", sys.version.split()[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
