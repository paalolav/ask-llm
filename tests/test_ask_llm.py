"""Tests for ask-llm. Run: python3 -m unittest discover tests -v

Loads bin/ask-llm via importlib (the script has no .py extension).
Network calls are mocked — no LiteLLM required.
"""
import importlib.machinery
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "bin" / "ask-llm"

# bin/ask-llm has no .py extension — load explicitly via SourceFileLoader.
_loader = importlib.machinery.SourceFileLoader("ask_llm", str(SCRIPT))
_spec = importlib.util.spec_from_loader("ask_llm", _loader)
ask_llm = importlib.util.module_from_spec(_spec)
_loader.exec_module(ask_llm)


class SafeReadTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, content="hello"):
        p = self.tmp / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    def test_reads_normal_file(self):
        p = self._write("notes.md", "content")
        path, content = ask_llm.safe_read(str(p))
        self.assertEqual(path, p.resolve())
        self.assertEqual(content, "content")

    def test_denies_env_file(self):
        p = self._write(".env", "SECRET=x")
        with self.assertRaises(SystemExit):
            ask_llm.safe_read(str(p))

    def test_denies_ssh_private_key(self):
        p = self._write("id_ed25519", "PRIVATE")
        with self.assertRaises(SystemExit):
            ask_llm.safe_read(str(p))

    def test_allows_ssh_public_key(self):
        p = self._write("id_ed25519.pub", "ssh-ed25519 AAAA")
        _, content = ask_llm.safe_read(str(p))
        self.assertEqual(content, "ssh-ed25519 AAAA")

    def test_denies_pem(self):
        p = self._write("server.pem", "-----BEGIN-----")
        with self.assertRaises(SystemExit):
            ask_llm.safe_read(str(p))

    def test_denies_path_under_ssh_dir(self):
        p = self._write(".ssh/anything", "x")
        with self.assertRaises(SystemExit):
            ask_llm.safe_read(str(p))

    def test_missing_file_exits(self):
        with self.assertRaises(SystemExit):
            ask_llm.safe_read(str(self.tmp / "nope"))


class ConfigLoadingTests(unittest.TestCase):

    def test_load_config_parses_keys(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("# comment\n")
            f.write("ASK_LLM_URL=http://x/y\n")
            f.write('ASK_LLM_MODEL="quoted-model"\n')
            f.write("\n")
            f.write("invalid line without equals\n")
            cfg_path = f.name

        try:
            with mock.patch.object(ask_llm, "CONFIG_FILE", Path(cfg_path)):
                # setdefault means existing env wins — clear first
                for k in ("ASK_LLM_URL", "ASK_LLM_MODEL"):
                    os.environ.pop(k, None)
                ask_llm.load_config()
                self.assertEqual(os.environ.get("ASK_LLM_URL"), "http://x/y")
                self.assertEqual(os.environ.get("ASK_LLM_MODEL"), "quoted-model")
        finally:
            os.unlink(cfg_path)
            for k in ("ASK_LLM_URL", "ASK_LLM_MODEL"):
                os.environ.pop(k, None)

    def test_env_overrides_config(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("ASK_LLM_URL=http://from-file\n")
            cfg_path = f.name
        try:
            os.environ["ASK_LLM_URL"] = "http://from-env"
            with mock.patch.object(ask_llm, "CONFIG_FILE", Path(cfg_path)):
                ask_llm.load_config()
                self.assertEqual(os.environ["ASK_LLM_URL"], "http://from-env")
        finally:
            os.unlink(cfg_path)
            os.environ.pop("ASK_LLM_URL", None)


def _fake_response(content="ok", prompt_tokens=10, completion_tokens=5):
    """Build a fake urllib response object."""
    body = json.dumps({
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }).encode()

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False  # do not suppress exceptions

        def read(self):
            return body

    return FakeResp()


class CLIIntegrationTests(unittest.TestCase):
    """End-to-end argparse + main() with network mocked."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["ASK_LLM_URL"] = "http://test/v1/chat/completions"
        os.environ["ASK_LLM_MODEL"] = "test-model"
        os.environ["ASK_LLM_AUTH"] = "test-token"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k in ("ASK_LLM_URL", "ASK_LLM_MODEL", "ASK_LLM_AUTH"):
            os.environ.pop(k, None)

    def _run(self, argv):
        captured_request = {}

        def fake_urlopen(req, timeout):
            captured_request["url"] = req.full_url
            captured_request["body"] = json.loads(req.data)
            captured_request["headers"] = dict(req.headers)
            return _fake_response("model-reply")

        out = io.StringIO()
        err = io.StringIO()
        with mock.patch("sys.argv", ["ask-llm"] + argv), \
             mock.patch.object(ask_llm.urllib.request, "urlopen", fake_urlopen), \
             mock.patch("sys.stdout", out), \
             mock.patch("sys.stderr", err), \
             mock.patch.object(ask_llm, "CONFIG_FILE", self.tmp / "nonexistent.env"):
            ask_llm.main()

        return out.getvalue(), err.getvalue(), captured_request

    def test_positional_prompt(self):
        out, _, req = self._run(["What is 2+2?"])
        self.assertIn("model-reply", out)
        self.assertEqual(req["body"]["messages"][0]["role"], "user")
        self.assertEqual(req["body"]["messages"][0]["content"], "What is 2+2?")
        self.assertEqual(req["body"]["model"], "test-model")
        self.assertEqual(req["headers"]["Authorization"], "Bearer test-token")

    def test_q_flag_with_paths_wraps_files(self):
        f1 = self.tmp / "a.py"
        f2 = self.tmp / "b.py"
        f1.write_text("def a(): pass\n")
        f2.write_text("def b(): pass\n")

        _, _, req = self._run(
            ["-q", "Summarize", "--paths", str(f1), str(f2)])

        msgs = req["body"]["messages"]
        self.assertEqual(len(msgs), 3)
        self.assertEqual(msgs[0]["role"], "system")
        corpus = msgs[1]["content"]
        self.assertIn(f"<file path='{f1.resolve()}'>", corpus)
        self.assertIn("def a(): pass", corpus)
        self.assertIn(f"<file path='{f2.resolve()}'>", corpus)
        self.assertIn("def b(): pass", corpus)
        self.assertEqual(msgs[2]["content"], "Summarize")

    def test_context_strips_markdown_fences(self):
        ref = self.tmp / "ref.py"
        ref.write_text("def x(): pass")

        def fake_urlopen(req, timeout):
            return _fake_response("```python\nfenced output\n```")

        out_path = self.tmp / "out.py"
        with mock.patch("sys.argv", [
                "ask-llm", "-q", "spec", "--context", str(ref),
                "--target", str(out_path)]), \
             mock.patch.object(ask_llm.urllib.request, "urlopen", fake_urlopen), \
             mock.patch("sys.stdout", io.StringIO()), \
             mock.patch("sys.stderr", io.StringIO()), \
             mock.patch.object(ask_llm, "CONFIG_FILE",
                               self.tmp / "nonexistent.env"):
            ask_llm.main()

        self.assertEqual(out_path.read_text().strip(), "fenced output")

    def test_no_question_errors(self):
        with mock.patch("sys.argv", ["ask-llm"]), \
             mock.patch("sys.stdout", io.StringIO()), \
             mock.patch("sys.stderr", io.StringIO()), \
             mock.patch.object(ask_llm, "CONFIG_FILE",
                               self.tmp / "nonexistent.env"):
            with self.assertRaises(SystemExit):
                ask_llm.main()

    def test_missing_url_errors(self):
        del os.environ["ASK_LLM_URL"]
        with mock.patch("sys.argv", ["ask-llm", "hi"]), \
             mock.patch("sys.stdout", io.StringIO()), \
             mock.patch("sys.stderr", io.StringIO()), \
             mock.patch.object(ask_llm, "CONFIG_FILE",
                               self.tmp / "nonexistent.env"):
            with self.assertRaises(SystemExit):
                ask_llm.main()


if __name__ == "__main__":
    unittest.main()
