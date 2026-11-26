"""Start and stop the local research evidence website as a detached process."""

import argparse
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen

from . import releases

MODULE = "research_library.web_server"


def managed_process(pid, *, port, registry=None, current_state=None, project_root=None,
                    markdown_repository=None, confirm_markdown_rights=None, allow_current_metadata=None):
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return False
    arguments = [item.decode(errors="replace") for item in command if item]
    def option(name):
        return (arguments[arguments.index(name) + 1]
                if name in arguments and arguments.index(name) + 1 < len(arguments) else None)
    if (registry is None) == (current_state is None):
        raise ValueError("Select exactly one registry or current-state store")
    name = "--current-state" if current_state is not None else "--registry"
    other = "--registry" if current_state is not None else "--current-state"
    selected = current_state if current_state is not None else registry
    if not (option("-m") == MODULE and option("--port") == str(port)
            and option(other) is None and option(name) is not None
            and Path(option(name)).absolute() == selected):
        return False
    if project_root is not None and (option("--project-root") is None
                                     or Path(option("--project-root")).absolute() != project_root):
        return False
    if confirm_markdown_rights is not None:
        configured = option("--experimental-markdown-repository")
        if ((Path(configured).absolute() if configured else None) != markdown_repository
                or ("--confirm-markdown-rights" in arguments) != confirm_markdown_rights):
            return False
    if allow_current_metadata is not None and (
            ("--allow-current-metadata" in arguments) != allow_current_metadata):
        return False
    return True


def process_running(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return False
    state = stat[stat.rfind(")") + 2:].split()[0]
    return state != "Z"


def read_pid(path):
    try:
        value = path.read_text(encoding="ascii").strip()
        pid = int(value)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise ValueError(f"Invalid website PID file: {path}") from error
    if pid < 1:
        raise ValueError(f"Invalid website PID file: {path}")
    return pid


def tail(path):
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 8192))
            return stream.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def reject_symlink(path):
    if path.is_symlink():
        raise ValueError(f"Web process state cannot be a symlink: {path}")


def write_pid(path, pid):
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".web-pid-")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(f"{pid}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def start(args):
    state_dir = args.state_dir.absolute()
    state_dir.mkdir(parents=True, exist_ok=True)
    pid_file, log_file = state_dir / "web.pid", state_dir / "web.log"
    reject_symlink(state_dir)
    reject_symlink(pid_file)
    reject_symlink(log_file)
    lock_file = state_dir / "web.lock"
    reject_symlink(lock_file)
    registry = args.registry.absolute() if args.registry else None
    current_state = args.current_state.absolute() if args.current_state else None
    start_config = {
        "port": args.port, "registry": registry, "current_state": current_state,
        "project_root": args.project_root.absolute(),
        "markdown_repository": (args.experimental_markdown_repository.absolute()
                                if args.experimental_markdown_repository else None),
        "confirm_markdown_rights": args.confirm_markdown_rights,
        "allow_current_metadata": args.allow_current_metadata,
    }
    with lock_file.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old_pid = read_pid(pid_file)
        if old_pid is not None and process_running(old_pid) and managed_process(old_pid, **start_config):
            print(f"Web app is already running at http://127.0.0.1:{args.port} (PID {old_pid}).")
            return 0
        if old_pid is not None:
            if process_running(old_pid):
                raise ValueError(f"PID {old_pid} is not the configured website; refusing to replace its state file")
            pid_file.unlink(missing_ok=True)

        child_args = [
            sys.executable, "-m", MODULE,
            "--current-state" if current_state else "--registry", str(current_state or registry),
            "--project-root", str(args.project_root.absolute()),
            "--pdf-data-root", str(args.pdf_data_root.absolute()),
            "--port", str(args.port), "--mcp-url", args.mcp_url,
        ]
        for profile in args.allow_profile:
            child_args.extend(("--allow-profile", profile))
        for collection in args.allow_collection:
            child_args.extend(("--allow-collection", collection))
        for collection in args.default_collection:
            child_args.extend(("--default-collection", collection))
        if args.experimental_markdown_repository:
            child_args.extend(("--experimental-markdown-repository",
                               str(args.experimental_markdown_repository.absolute())))
        if args.confirm_markdown_rights:
            child_args.append("--confirm-markdown-rights")
        if args.allow_current_metadata:
            child_args.append("--allow-current-metadata")
        with log_file.open("ab") as log:
            os.chmod(log_file, 0o600)
            process = subprocess.Popen(
                child_args, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True)
        write_pid(pid_file, process.pid)
        url = f"http://127.0.0.1:{args.port}/healthz"
        for _ in range(50):
            if process.poll() is not None:
                pid_file.unlink(missing_ok=True)
                raise RuntimeError(f"Website exited during startup. Log output:\n{tail(log_file)}")
            if managed_process(process.pid, **start_config):
                try:
                    with urlopen(url, timeout=0.25) as response:
                        if response.status == 200:
                            print(f"Web app started at http://127.0.0.1:{args.port} (PID {process.pid}).")
                            print(f"Log: {log_file}")
                            return 0
                except (OSError, URLError):
                    pass
            time.sleep(0.2)
        if process.poll() is None and managed_process(process.pid, **start_config):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"Website did not stop after failed readiness check. Log: {log_file}")
        pid_file.unlink(missing_ok=True)
        raise RuntimeError(f"Website did not become ready at {url}. Log output:\n{tail(log_file)}")


def stop(args):
    state_dir = args.state_dir.absolute()
    pid_file = state_dir / "web.pid"
    reject_symlink(state_dir)
    reject_symlink(pid_file)
    if not pid_file.is_file():
        print("Web app is not running.")
        return 0
    registry = args.registry.absolute() if args.registry else None
    current_state = args.current_state.absolute() if args.current_state else None
    lock_file = state_dir / "web.lock"
    reject_symlink(lock_file)
    with lock_file.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pid = read_pid(pid_file)
        if pid is None:
            print("Web app is not running.")
            return 0
        if not process_running(pid):
            pid_file.unlink(missing_ok=True)
            print("Web app was already stopped.")
            return 0
        if not managed_process(pid, port=args.port, registry=registry, current_state=current_state):
            raise ValueError(f"PID {pid} is not the configured website; refusing to stop it")
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and process_running(pid):
            time.sleep(0.1)
        if process_running(pid):
            raise RuntimeError(f"Website process {pid} did not stop after SIGTERM; PID file was preserved")
        pid_file.unlink(missing_ok=True)
        print(f"Web app stopped (PID {pid}).")
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("start", "stop"):
        command = commands.add_parser(action)
        roots = command.add_mutually_exclusive_group(required=True)
        roots.add_argument("--registry", type=Path)
        roots.add_argument("--current-state", type=Path)
        command.add_argument("--project-root", type=Path, required=True)
        command.add_argument("--pdf-data-root", type=Path, default=Path("releases/pdf-data"))
        command.add_argument("--state-dir", type=Path, default=Path("releases"))
        command.add_argument("--port", type=int, default=8081)
        command.add_argument("--mcp-url", default="http://127.0.0.1:8000/mcp")
        command.add_argument("--allow-profile", action="append", required=True,
                             choices=sorted(releases.PROFILES))
        command.add_argument("--allow-collection", action="append", required=True)
        command.add_argument("--default-collection", action="append", required=True)
        command.add_argument("--experimental-markdown-repository", type=Path)
        command.add_argument("--confirm-markdown-rights", action="store_true")
        command.add_argument("--allow-current-metadata", action="store_true")
    args = parser.parse_args()
    try:
        return start(args) if args.action == "start" else stop(args)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Web lifecycle error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
