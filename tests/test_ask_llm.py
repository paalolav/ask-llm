"""Tests for ask-llm. Run: python3 -m unittest discover tests -v

Imports the ask_llm package from the repo root. Network calls are mocked —
no LiteLLM required.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import ask_llm  # noqa: E402


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


class AuthHeaderTests(unittest.TestCase):
    """Verify ASK_LLM_API_KEY takes precedence over ASK_LLM_AUTH."""

    def _run_with_env(self, env_overrides):
        """Run main() with mocked env + network, return the Authorization header sent."""
        env = {
            "ASK_LLM_URL": "http://fake:4000/v1/chat/completions",
            "ASK_LLM_MODEL": "test-model",
            "ASK_LLM_AUTH": "dummy",
            **env_overrides,
        }

        captured = {}
        def fake_urlopen(req, timeout):
            captured["auth"] = req.get_header("Authorization")
            return _fake_response("ok")

        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch("sys.argv", ["ask-llm", "test"]), \
             mock.patch.object(ask_llm.urllib.request, "urlopen", fake_urlopen), \
             mock.patch("sys.stdout", io.StringIO()), \
             mock.patch("sys.stderr", io.StringIO()), \
             mock.patch.object(ask_llm, "CONFIG_FILE", Path("/nonexistent")):
            ask_llm.main()
        return captured.get("auth")

    def test_api_key_takes_precedence(self):
        auth = self._run_with_env({"ASK_LLM_API_KEY": "sk-test-key-123"})
        self.assertEqual(auth, "Bearer sk-test-key-123")

    def test_falls_back_to_auth(self):
        auth = self._run_with_env({})
        self.assertEqual(auth, "Bearer dummy")

    def test_api_key_empty_falls_back(self):
        auth = self._run_with_env({"ASK_LLM_API_KEY": ""})
        self.assertEqual(auth, "Bearer dummy")


class StatsTests(unittest.TestCase):
    """Tests for ask-llm stats subcommand."""

    @classmethod
    def setUpClass(cls):
        from datetime import datetime, timedelta
        # Use today/yesterday so the 7-day default filter does not age out.
        d0 = datetime.utcnow().isoformat() + "Z"
        d1 = (datetime.utcnow() - timedelta(days=1)).isoformat() + "Z"
        cls.SAMPLE_SPEND_LOGS = json.dumps([
            {"startTime": d0, "model": "qwen3.6-35b",
             "prompt_tokens": 500, "completion_tokens": 200,
             "total_tokens": 700, "user": "ask-llm"},
            {"startTime": d0, "model": "gemma-4",
             "prompt_tokens": 300, "completion_tokens": 100,
             "total_tokens": 400, "user": "ask-llm"},
            {"startTime": d1, "model": "qwen3.6-35b",
             "prompt_tokens": 600, "completion_tokens": 250,
             "total_tokens": 850, "user": "ask-llm"},
        ]).encode()

    def _run_stats(self, extra_args=None, env_overrides=None, response_data=None):
        env = {
            "ASK_LLM_URL": "http://fake:4000/v1/chat/completions",
            "ASK_LLM_MODEL": "test-model",
            "LITELLM_MASTER_KEY": "sk-master-test",
            **(env_overrides or {}),
        }
        argv = ["ask-llm", "stats"] + (extra_args or [])
        resp_data = response_data or self.SAMPLE_SPEND_LOGS

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return resp_data

        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch("sys.argv", argv), \
             mock.patch.object(ask_llm.urllib.request, "urlopen", return_value=FakeResp()), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
             mock.patch("sys.stderr", new_callable=io.StringIO), \
             mock.patch.object(ask_llm, "CONFIG_FILE", Path("/nonexistent")):
            try:
                ask_llm.main()
            except SystemExit:
                pass
            return out.getvalue()

    def test_stats_default(self):
        output = self._run_stats()
        self.assertIn("Requests:", output)
        self.assertIn("3", output)

    def test_stats_by_model(self):
        output = self._run_stats(["--by-model"])
        self.assertIn("qwen3.6-35b", output)
        self.assertIn("gemma-4", output)

    def test_stats_raw(self):
        output = self._run_stats(["--raw"])
        data = json.loads(output)
        self.assertEqual(data["total_requests"], 3)
        self.assertIn("qwen3.6-35b", data["by_model"])

    def test_stats_no_master_key(self):
        output = self._run_stats(env_overrides={"LITELLM_MASTER_KEY": ""})
        self.assertIn("LITELLM_MASTER_KEY", output)

    def test_stats_non_litellm_endpoint_degrades(self):
        """If ASK_LLM_URL points at a non-LiteLLM endpoint, /spend/logs returns
        404 or HTML — should print a friendly message, not a traceback."""
        import urllib.error
        env = {
            "ASK_LLM_URL": "http://ollama:11434/v1/chat/completions",
            "ASK_LLM_MODEL": "test-model",
            "LITELLM_MASTER_KEY": "sk-master-test",
        }

        def fake_urlopen(req, timeout):
            raise urllib.error.HTTPError(
                req.full_url, 404, "Not Found", {}, io.BytesIO(b"Not Found"))

        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch("sys.argv", ["ask-llm", "stats"]), \
             mock.patch.object(ask_llm.urllib.request, "urlopen", fake_urlopen), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
             mock.patch("sys.stderr", new_callable=io.StringIO), \
             mock.patch.object(ask_llm, "CONFIG_FILE", Path("/nonexistent")):
            try:
                ask_llm.main()
            except SystemExit:
                pass
            output = out.getvalue()
        self.assertIn("LiteLLM", output)
        self.assertNotIn("Traceback", output)


class ContinuationTests(unittest.TestCase):
    """Tests for -c/--continue flag (chat continuation via local cache file)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cache = self.tmp / "last.json"
        self.env = {
            "ASK_LLM_URL": "http://test/v1/chat/completions",
            "ASK_LLM_MODEL": "default-model",
            "ASK_LLM_AUTH": "test-token",
        }

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, argv, response="reply", capture=None):
        if capture is None:
            capture = {}

        def fake_urlopen(req, timeout):
            capture["url"] = req.full_url
            capture["body"] = json.loads(req.data)
            return _fake_response(response)

        with mock.patch.dict(os.environ, self.env, clear=False), \
             mock.patch("sys.argv", ["ask-llm"] + argv), \
             mock.patch.object(ask_llm.urllib.request, "urlopen", fake_urlopen), \
             mock.patch("sys.stdout", io.StringIO()), \
             mock.patch("sys.stderr", io.StringIO()), \
             mock.patch.object(ask_llm, "CONFIG_FILE", self.tmp / "nonexistent.env"), \
             mock.patch.object(ask_llm, "CACHE_FILE", self.cache):
            ask_llm.main()
        return capture

    def test_normal_call_saves_cache(self):
        self._run(["hei"], response="hallo")
        self.assertTrue(self.cache.exists())
        saved = json.loads(self.cache.read_text())
        roles = [m["role"] for m in saved["messages"]]
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)
        self.assertEqual(saved["messages"][-1]["content"], "hallo")
        self.assertEqual(saved["model"], "default-model")

    def test_continue_appends_to_cached_messages(self):
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_text(json.dumps({
            "model": "qwen3.5-4b",
            "messages": [
                {"role": "user", "content": "Q1"},
                {"role": "assistant", "content": "A1"},
            ],
        }))
        cap = self._run(["-c", "Q2"], response="A2")
        msgs = cap["body"]["messages"]
        self.assertEqual(len(msgs), 3)
        self.assertEqual(msgs[0], {"role": "user", "content": "Q1"})
        self.assertEqual(msgs[1], {"role": "assistant", "content": "A1"})
        self.assertEqual(msgs[2], {"role": "user", "content": "Q2"})
        # Model should default to the cached one when --model not given
        self.assertEqual(cap["body"]["model"], "qwen3.5-4b")

    def test_continue_explicit_model_overrides_cached(self):
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_text(json.dumps({
            "model": "qwen3.5-4b",
            "messages": [{"role": "user", "content": "x"},
                         {"role": "assistant", "content": "y"}],
        }))
        cap = self._run(["-c", "--model", "gemma-4", "next"])
        self.assertEqual(cap["body"]["model"], "gemma-4")

    def test_continue_without_cache_errors(self):
        with mock.patch.dict(os.environ, self.env, clear=False), \
             mock.patch("sys.argv", ["ask-llm", "-c", "follow"]), \
             mock.patch("sys.stdout", io.StringIO()) as out, \
             mock.patch("sys.stderr", io.StringIO()) as err, \
             mock.patch.object(ask_llm, "CONFIG_FILE", self.tmp / "nonexistent.env"), \
             mock.patch.object(ask_llm, "CACHE_FILE", self.cache):
            with self.assertRaises(SystemExit):
                ask_llm.main()
            combined = out.getvalue() + err.getvalue()
        self.assertIn("previous", combined.lower())

    def test_continue_appends_assistant_reply_to_cache(self):
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_text(json.dumps({
            "model": "qwen3.5-4b",
            "messages": [{"role": "user", "content": "Q1"},
                         {"role": "assistant", "content": "A1"}],
        }))
        self._run(["-c", "Q2"], response="A2")
        saved = json.loads(self.cache.read_text())
        self.assertEqual(len(saved["messages"]), 4)
        self.assertEqual(saved["messages"][-1],
                         {"role": "assistant", "content": "A2"})

    def test_cache_truncates_long_history(self):
        """Cache caps at MAX_CACHED_MESSAGES so long -c chains don't blow context."""
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        # 30 messages, alternating user/assistant
        msgs = [{"role": "user" if i % 2 == 0 else "assistant",
                 "content": f"m{i}"} for i in range(30)]
        self.cache.write_text(json.dumps({"model": "x", "messages": msgs}))
        self._run(["-c", "next"], response="reply")
        saved = json.loads(self.cache.read_text())
        # 30 (prior) + 1 (new user) + 1 (assistant reply) would be 32 without
        # truncation. We cap at MAX_CACHED_MESSAGES.
        cap = ask_llm.MAX_CACHED_MESSAGES
        self.assertLessEqual(len(saved["messages"]), cap)
        # Last two should be the new exchange.
        self.assertEqual(saved["messages"][-2]["content"], "next")
        self.assertEqual(saved["messages"][-1]["content"], "reply")

    def test_cache_truncation_preserves_system_prefix(self):
        """If conversation starts with a system message (e.g., from --task on
        the original call), keep it across truncation."""
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        msgs = [{"role": "system", "content": "you are a precise assistant"}]
        for i in range(30):
            msgs.append({"role": "user" if i % 2 == 0 else "assistant",
                         "content": f"m{i}"})
        self.cache.write_text(json.dumps({"model": "x", "messages": msgs}))
        self._run(["-c", "next"], response="reply")
        saved = json.loads(self.cache.read_text())
        cap = ask_llm.MAX_CACHED_MESSAGES
        self.assertLessEqual(len(saved["messages"]), cap)
        self.assertEqual(saved["messages"][0]["role"], "system")
        self.assertIn("precise assistant", saved["messages"][0]["content"])

    def test_continue_rejects_paths(self):
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_text(json.dumps({
            "model": "x", "messages": [{"role": "user", "content": "x"}]}))
        f = self.tmp / "a.py"
        f.write_text("x")
        with mock.patch.dict(os.environ, self.env, clear=False), \
             mock.patch("sys.argv", ["ask-llm", "-c", "next", "--paths", str(f)]), \
             mock.patch("sys.stdout", io.StringIO()), \
             mock.patch("sys.stderr", io.StringIO()) as err, \
             mock.patch.object(ask_llm, "CONFIG_FILE", self.tmp / "nonexistent.env"), \
             mock.patch.object(ask_llm, "CACHE_FILE", self.cache):
            with self.assertRaises(SystemExit):
                ask_llm.main()
            self.assertIn("--paths", err.getvalue() + "")


if __name__ == "__main__":
    unittest.main()
