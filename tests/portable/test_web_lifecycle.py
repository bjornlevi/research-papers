from argparse import Namespace
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from research_library import web_lifecycle


class WebLifecycleTests(unittest.TestCase):
    def test_start_checks_health_and_stop_cleans_up(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            args = Namespace(
                state_dir=root / "state", project_root=root / "project",
                registry=root / "project/registry", current_state=None, pdf_data_root=root / "pdfs",
                port=port, mcp_url="http://127.0.0.1:8000/mcp",
                allow_profile=["synthetic"], allow_collection=["core"],
                default_collection=["core"], experimental_markdown_repository=None,
                confirm_markdown_rights=False, allow_current_metadata=False)
            children = []
            spawn = web_lifecycle.subprocess.Popen

            def tracked_spawn(*args, **kwargs):
                child = spawn(*args, **kwargs)
                children.append(child)
                return child

            try:
                with patch.object(web_lifecycle.subprocess, "Popen", side_effect=tracked_spawn), \
                        patch.object(web_lifecycle, "urlopen", wraps=urlopen) as probe:
                    self.assertEqual(web_lifecycle.start(args), 0)
                self.assertTrue(probe.called)
                self.assertTrue(all(call.args[0] == f"http://127.0.0.1:{port}/healthz"
                                    for call in probe.call_args_list))
                pid = web_lifecycle.read_pid(args.state_dir / "web.pid")
                self.assertTrue(web_lifecycle.process_running(pid))
                with urlopen(f"http://127.0.0.1:{port}/settings", timeout=5) as response:
                    self.assertEqual(response.status, 200)
                self.assertEqual(web_lifecycle.start(args), 0)
                self.assertEqual(web_lifecycle.read_pid(args.state_dir / "web.pid"), pid)
            finally:
                try:
                    self.assertEqual(web_lifecycle.stop(args), 0)
                finally:
                    for child in children:
                        child.wait(timeout=5)
            self.assertFalse(web_lifecycle.process_running(pid))
            self.assertFalse((args.state_dir / "web.pid").exists())
