"""Retain reproducible fictional software acceptance; no medical reviewer required."""

import argparse
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent / "portable"))
from test_evaluation import specifications
from test_releases import fixture_bundle
from test_research_cards import cards

from research_library import evaluation, releases, research_cards


def check(output):
    output.mkdir(parents=True, exist_ok=False)
    bundle, _ = fixture_bundle(output)
    release = output / "release"
    releases.export(bundle, release, profile="synthetic")
    snapshot = releases.validate(release, allowed_profiles={"synthetic"})
    draft = evaluation.draft(bundle, specifications(), "synthetic-software-1")
    evaluation.write_new(output / "draft.json", draft)
    frozen = evaluation.freeze(
        draft, snapshot, reviewer="Automated synthetic software acceptance (not a domain reviewer)",
        approved_at=datetime.now(timezone.utc).isoformat(), review_kind="software",
    )
    benchmark = output / "frozen.json"
    evaluation.write_new(benchmark, frozen)
    before = {p: p.read_bytes() for p in release.rglob("*") if p.is_file()}
    for p in release.rglob("*"):
        p.chmod(0o555 if p.is_dir() else 0o444)
    release.chmod(0o555)
    try:
        for split in ("development", "held_out"):
            subprocess.run([
                sys.executable, "-m", "research_library.evaluation", "run",
                "--benchmark", str(benchmark), "--release", str(release),
                "--allow-profile", "synthetic", "--split", split, "--output", str(output / split),
            ], check=True, cwd=output, timeout=120)
        document = cards(snapshot)
        evaluation.write_new(output / "cards.json", document)
        evaluation.write_new(output / "card-audit.json", research_cards.validate(document, snapshot))
        assert all(p.read_bytes() == payload for p, payload in before.items())
    finally:
        release.chmod(0o755)
        for p in release.rglob("*"):
            p.chmod(0o755 if p.is_dir() else 0o644)
    print("PASS: both frozen software splits over MCP, citation/card audit, retained reports, unchanged release")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="New private output directory; otherwise use temporary storage")
    args = parser.parse_args()
    if args.output:
        check(args.output.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="istopmm-evaluation-") as temporary:
            check(Path(temporary) / "acceptance")


if __name__ == "__main__":
    main()
