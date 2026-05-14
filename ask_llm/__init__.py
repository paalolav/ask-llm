"""Send a prompt to a LiteLLM-backed model. Optional file paths for context.

Config is loaded from ~/.config/ask-llm.env (KEY=VALUE), overridable by env:
  ASK_LLM_URL                    OpenAI-compatible /v1/chat/completions endpoint
  ASK_LLM_MODEL                  Model alias (e.g. qwen3.6-35b)
  ASK_LLM_API_KEY                Virtual key for LiteLLM spend tracking (preferred)
  ASK_LLM_AUTH                   Bearer token fallback (LiteLLM accepts "dummy" by default)
  ASK_LLM_TIMEOUT                Seconds, default 180
  ASK_LLM_AUTH_KEYCHAIN_SERVICE  macOS Keychain service name (default: ask-llm)
  ASK_LLM_AUTH_KEYCHAIN_ACCOUNT  macOS Keychain account name (default: litellm-vkey)

Usage:
  ask-llm "prompt"                              # text Q&A
  ask-llm -q "question" --paths f1.py f2.py     # multi-file Q&A. Files are
                                                # wrapped in <file path='X'>
                                                # ...</file> for stable
                                                # KV-cache prefix
  ask-llm -q "spec" --context style.py          # generate matching style
  ask-llm -q "spec" --paths f.py --target out.py  # write reply to file
  ask-llm --list                                # show model catalog and presets
  ask-llm --task code "write binary search"     # use code preset (model+temp+system)
  ask-llm "prompt" --stream                     # SSE streaming (avoids truncation)
  ask-llm "prompt" --retries 3                  # retry 5xx/timeout up to 3 times
"""
import argparse, json, os, socket, subprocess, sys, time, urllib.request, urllib.error
from pathlib import Path

CONFIG_FILE = Path("~/.config/ask-llm.env").expanduser()
MODELS_FILE = Path(__file__).resolve().parent / "models.yaml"
CACHE_FILE = Path("~/.cache/ask-llm/last.json").expanduser()
MAX_CACHED_MESSAGES = 20  # ~10 user/assistant pairs; keeps -c chains bounded


def load_last_messages():
    """Load previous conversation from CACHE_FILE. Returns None if absent/broken."""
    if not CACHE_FILE.is_file():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text())
        if isinstance(data, dict) and isinstance(data.get("messages"), list):
            return data
    except Exception:
        return None
    return None


def _truncate_messages(messages, cap=MAX_CACHED_MESSAGES):
    """Cap conversation length, preserving any leading system messages."""
    if len(messages) <= cap:
        return messages
    system_prefix = []
    for m in messages:
        if m.get("role") == "system":
            system_prefix.append(m)
        else:
            break
    keep = cap - len(system_prefix)
    if keep <= 0:
        return system_prefix[:cap]
    return system_prefix + messages[len(system_prefix):][-keep:]


def save_last_messages(messages, assistant_content, model):
    """Persist messages + assistant reply for next ask-llm -c invocation.

    NOTE: cache stores prompts and replies in plaintext (chmod 600). Delete
    ~/.cache/ask-llm/last.json if your prompt contained secrets.
    """
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        full = list(messages) + [
            {"role": "assistant", "content": assistant_content}]
        payload = {
            "model": model,
            "messages": _truncate_messages(full),
        }
        CACHE_FILE.write_text(json.dumps(payload))
        try:
            os.chmod(CACHE_FILE, 0o600)
        except Exception:
            pass
    except Exception:
        # Cache failures must not break the main call.
        pass


def load_config():
    if CONFIG_FILE.is_file():
        for line in CONFIG_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def get_from_keychain(service="ask-llm", account="litellm-vkey"):
    """Retrieve a secret from macOS Keychain. Returns None if unavailable."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def load_models():
    """Load model catalog from models.yaml. Returns empty dict if unavailable."""
    if not MODELS_FILE.is_file():
        return {"models": {}, "tasks": {}}
    try:
        import yaml
        return yaml.safe_load(MODELS_FILE.read_text()) or {"models": {}, "tasks": {}}
    except ImportError:
        # pyyaml not installed — graceful fallback
        return {"models": {}, "tasks": {}}


def print_model_list(catalog):
    models = catalog.get("models", {})
    tasks = catalog.get("tasks", {})

    local = {n: m for n, m in models.items() if m.get("type") == "local"}
    cloud = {n: m for n, m in models.items() if m.get("type") == "cloud"}

    if local:
        print("Local models (free, served via exo/MLX):")
        for name, m in local.items():
            r = m.get("recommended", {})
            ctx = m.get("context_k", "?")
            ctx_str = f"{ctx}K" if isinstance(ctx, int) else str(ctx)
            print(f"  {name:<22} T={r.get('temperature', '?'):<5} ctx={ctx_str:<8} {m.get('speed', '')}")
            for s in m.get("strengths", []):
                print(f"      - {s}")
            print()

    if cloud:
        print("Cloud models (paid, via LiteLLM → Google):")
        for name, m in cloud.items():
            r = m.get("recommended", {})
            ctx = m.get("context_k", "?")
            ctx_str = f"{ctx}K" if isinstance(ctx, int) else str(ctx)
            print(f"  {name:<22} T={r.get('temperature', '?'):<5} ctx={ctx_str:<8} {m.get('speed', '')}")
            for s in m.get("strengths", []):
                print(f"      - {s}")
            print()

    if tasks:
        print("Task presets (--task <name> \"prompt\"):")
        for name, t in tasks.items():
            print(f"  --task {name:<12} -> {t.get('model', '?'):<22} T={t.get('temperature', '?'):<5} max={t.get('max_tokens', '?')}")


DENY_SUBSTR = (".ssh/", ".aws/", ".gnupg/", "Library/Keychains/",
               "/.env", "/credentials", "/.netrc", "/.pgpass",
               "/master.passwd", "/shadow")
DENY_NAMES = {".env", ".netrc", ".pgpass", "credentials", "master.passwd", "shadow"}
DENY_KEY_PREFIX = ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")
DENY_SUFFIX = {".pem", ".key"}


def safe_read(path_str):
    p = Path(path_str).expanduser().resolve()
    s, name = str(p), p.name
    if any(d in s for d in DENY_SUBSTR):
        sys.exit(f"refused: {p} matches secret-path pattern")
    if name in DENY_NAMES:
        sys.exit(f"refused: {p}")
    if any(name.startswith(k) for k in DENY_KEY_PREFIX) and not name.endswith(".pub"):
        sys.exit(f"refused: {p} looks like an SSH private key")
    if p.suffix in DENY_SUFFIX:
        sys.exit(f"refused: {p}")
    if not p.is_file():
        sys.exit(f"not a file: {p}")
    return p, p.read_text(errors="replace")


def _do_request(req, timeout, retries):
    """Execute HTTP request with exponential-backoff retry on 5xx/network errors."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code < 500:  # 4xx: no retry (auth error, bad request)
                raise
            last_err = e
        except (urllib.error.URLError, socket.timeout) as e:
            last_err = e
        if attempt < retries:
            wait = 2 ** attempt
            print(f"[ask-llm: retry {attempt + 1}/{retries} after {wait}s due to {last_err}]",
                  file=sys.stderr)
            time.sleep(wait)
    raise last_err


def _do_streaming_request(req, timeout):
    """SSE streaming request. Returns (full_text, usage_dict)."""
    full_text = ""
    usage = {}
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            delta = chunk.get("choices", [{}])[0].get("delta", {})
            content = delta.get("content", "")
            if content:
                full_text += content
                sys.stdout.write(content)
                sys.stdout.flush()
            if "usage" in chunk:
                usage = chunk["usage"]
    print()  # final newline after streamed content
    return full_text, usage


def run_stats(argv):
    import argparse as ap
    from datetime import datetime, timedelta

    p = ap.ArgumentParser(prog="ask-llm stats", description="LiteLLM usage stats")
    p.add_argument("--days", type=int, default=7, help="Days to look back (default 7)")
    p.add_argument("--by-model", action="store_true", help="Group by model")
    p.add_argument("--raw", action="store_true", help="Raw JSON output")
    p.add_argument("--all", action="store_true", help="All consumers (needs LITELLM_MASTER_KEY env)")
    args = p.parse_args(argv)

    base_url = os.environ.get("ASK_LLM_URL", "")
    # /spend/logs requires admin (master key) — virtual keys get 401
    api_key = os.environ.get("LITELLM_MASTER_KEY", "").strip()

    if not api_key:
        print("ask-llm stats requires LITELLM_MASTER_KEY in ~/.config/ask-llm.env")
        print("(The /spend/logs endpoint requires admin access.)")
        sys.exit(1)

    if not base_url:
        print("ASK_LLM_URL not set")
        sys.exit(1)

    litellm_base = base_url.replace("/v1/chat/completions", "").rstrip("/")
    cutoff = (datetime.now() - timedelta(days=args.days)).isoformat()

    url = f"{litellm_base}/spend/logs"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {api_key}",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            logs = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"ask-llm stats requires a LiteLLM endpoint "
                  f"(got 404 from {url}).")
            print("Your ASK_LLM_URL does not appear to point at LiteLLM. "
                  "Stats are a LiteLLM-specific feature.")
            sys.exit(1)
        print(f"Failed to fetch spend logs: HTTP {e.code}")
        sys.exit(1)
    except (json.JSONDecodeError, ValueError):
        print(f"ask-llm stats requires a LiteLLM endpoint "
              f"({url} returned non-JSON).")
        print("Your ASK_LLM_URL does not appear to point at LiteLLM. "
              "Stats are a LiteLLM-specific feature.")
        sys.exit(1)
    except Exception as e:
        print(f"Failed to fetch spend logs: {e}")
        print("Check that LiteLLM has a database connected.")
        sys.exit(1)

    if not isinstance(logs, list):
        logs = []
    logs = [e for e in logs if e.get("startTime", "") >= cutoff]
    if not args.all:
        logs = [e for e in logs if e.get("user") == "ask-llm"]

    total_req = len(logs)
    total_in = sum(e.get("prompt_tokens", 0) for e in logs)
    total_out = sum(e.get("completion_tokens", 0) for e in logs)

    by_model = {}
    for e in logs:
        m = e.get("model", "unknown")
        if m not in by_model:
            by_model[m] = {"requests": 0, "tokens_in": 0, "tokens_out": 0}
        by_model[m]["requests"] += 1
        by_model[m]["tokens_in"] += e.get("prompt_tokens", 0)
        by_model[m]["tokens_out"] += e.get("completion_tokens", 0)

    if args.raw:
        print(json.dumps({
            "days": args.days, "total_requests": total_req,
            "total_tokens_in": total_in, "total_tokens_out": total_out,
            "by_model": by_model,
        }, indent=2))
        return

    label = "all consumers" if args.all else "ask-llm"
    print(f"LiteLLM spend ({label}, last {args.days} days)")
    print("-" * 40)
    print(f"{'Requests:':<14}{total_req:>10,}")
    print(f"{'Tokens in:':<14}{total_in:>10,}")
    print(f"{'Tokens out:':<14}{total_out:>10,}")

    if by_model:
        print(f"\nBy model:")
        for m, d in sorted(by_model.items(), key=lambda x: -x[1]["requests"]):
            print(f"  {m:<20}{d['requests']:>5} req  {d['tokens_in']:>8,} in  {d['tokens_out']:>8,} out")

    if args.all:
        by_user = {}
        for e in logs:
            u = e.get("user", "unknown")
            if u not in by_user:
                by_user[u] = 0
            by_user[u] += 1
        if by_user:
            print(f"\nBy consumer:")
            for u, c in sorted(by_user.items(), key=lambda x: -x[1]):
                print(f"  {u:<20}{c:>5} req")


def main():
    load_config()

    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        return run_stats(sys.argv[2:])

    # --list: show model catalog (no prompt required)
    if "--list" in sys.argv:
        catalog = load_models()
        print_model_list(catalog)
        return

    # A2: Keychain fallback — only if both auth env vars are unset
    if not os.environ.get("ASK_LLM_API_KEY") and not os.environ.get("ASK_LLM_AUTH"):
        kc_service = os.environ.get("ASK_LLM_AUTH_KEYCHAIN_SERVICE", "ask-llm")
        kc_account = os.environ.get("ASK_LLM_AUTH_KEYCHAIN_ACCOUNT", "litellm-vkey")
        kc_value = get_from_keychain(kc_service, kc_account)
        if kc_value:
            os.environ["ASK_LLM_AUTH"] = kc_value

    url = os.environ.get("ASK_LLM_URL")
    api_key = os.environ.get("ASK_LLM_API_KEY", "").strip()
    auth = api_key if api_key else os.environ.get("ASK_LLM_AUTH", "dummy")
    default_model = os.environ.get("ASK_LLM_MODEL")
    timeout = int(os.environ.get("ASK_LLM_TIMEOUT", "180"))
    if not url or not default_model:
        sys.exit("ASK_LLM_URL and ASK_LLM_MODEL must be set "
                 "(in env or ~/.config/ask-llm.env). See .env.example.")

    ap = argparse.ArgumentParser(description="LiteLLM CLI client")
    ap.add_argument("prompt", nargs="*", help="Question/spec (or use -q)")
    ap.add_argument("-q", "--question",
                    help="Question/spec — use this when --paths is set, "
                         "since --paths is greedy")
    ap.add_argument("--paths", nargs="+", help="Files to include as corpus")
    ap.add_argument("--context",
                    help="Single style-reference file for generation")
    ap.add_argument("--target", help="Write response to file (default: stdout)")
    ap.add_argument("--model", default=default_model)
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--temperature", type=float, default=0.3)
    ap.add_argument("--retries", type=int, default=2,
                    help="Retry attempts on 5xx/timeout (default 2, use 0 to disable)")
    ap.add_argument("--stream", action="store_true",
                    help="SSE streaming mode (avoids Gemini-Flash truncation)")
    ap.add_argument("--task",
                    choices=["code", "reasoning", "chat", "roleplay",
                             "summary", "analysis", "quick"],
                    help="Use preset (model + temp + max_tokens + system-prompt)")
    ap.add_argument("-c", "--continue", dest="continue_flag",
                    action="store_true",
                    help="Continue previous conversation from cache "
                         "(~/.cache/ask-llm/last.json)")
    args = ap.parse_args()

    if args.continue_flag and (args.paths or args.context or args.task):
        ap.error("-c cannot be combined with --paths, --context, or --task")

    # A6: Apply task preset before building messages
    catalog = load_models()
    if args.task:
        preset = catalog.get("tasks", {}).get(args.task, {})
        if preset:
            # Only override model if user did not explicitly pass --model
            if args.model == default_model:
                args.model = preset.get("model", args.model)
            # Only override temperature if still at argparse default
            if args.temperature == 0.3:
                args.temperature = preset.get("temperature", args.temperature)
            # Only override max_tokens if still at argparse default
            if args.max_tokens == 4096:
                args.max_tokens = preset.get("max_tokens", args.max_tokens)

    prompt = args.question or " ".join(args.prompt)
    if not prompt:
        ap.error("question required (positional or -q)")

    if args.continue_flag:
        prev = load_last_messages()
        if not prev:
            print("ask-llm -c: no previous conversation found "
                  "(~/.cache/ask-llm/last.json). Run a normal call first.",
                  file=sys.stderr)
            sys.exit(1)
        messages = list(prev["messages"]) + [
            {"role": "user", "content": prompt}]
        if args.model == default_model:
            args.model = prev.get("model", args.model)
    elif args.paths:
        chunks = []
        for ps in args.paths:
            p, content = safe_read(ps)
            chunks.append(f"<file path='{p}'>\n{content}\n</file>")
            print(f"[ask-llm: read {p} ({len(content)}c)]", file=sys.stderr)
        messages = [
            {"role": "system", "content":
             "Precise code/document analyst. Read the files and answer concisely. "
             "Quote file paths and line numbers when relevant. Bullets, not prose."},
            {"role": "user", "content": f"<corpus>\n{chr(10).join(chunks)}\n</corpus>"},
            {"role": "user", "content": prompt},
        ]
    elif args.context:
        p, content = safe_read(args.context)
        print(f"[ask-llm: context {p} ({len(content)}c)]", file=sys.stderr)
        messages = [
            {"role": "system", "content":
             "Generate clean, idiomatic code matching the reference style. "
             "Output ONLY file contents — no explanations, no markdown fences."},
            {"role": "user", "content":
             f"<reference>\n{content}\n</reference>\nWrite: {prompt}"},
        ]
    else:
        messages = [{"role": "user", "content": prompt}]

    # Prepend preset system message (after file-based system messages are set above)
    if args.task:
        preset = catalog.get("tasks", {}).get(args.task, {})
        preset_system = preset.get("system") if preset else None
        if preset_system:
            # Inject as first message if no system message present, else prepend
            if messages and messages[0].get("role") == "system":
                messages[0]["content"] = preset_system + "\n\n" + messages[0]["content"]
            else:
                messages = [{"role": "system", "content": preset_system}] + messages

    body_dict = {
        "model": args.model, "messages": messages,
        "temperature": args.temperature, "max_tokens": args.max_tokens,
        "user": "ask-llm",
    }
    if args.stream:
        body_dict["stream"] = True

    body = json.dumps(body_dict).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {auth}"})

    if args.stream:
        answer, u = _do_streaming_request(req, timeout)
    else:
        r = _do_request(req, timeout, args.retries)
        answer = r["choices"][0]["message"]["content"]
        u = r.get("usage", {})

    if args.context and answer.startswith("```"):
        answer = answer.split("\n", 1)[1].rsplit("```", 1)[0]

    if not args.stream:
        if args.target:
            Path(args.target).expanduser().write_text(answer)
            print(f"wrote {args.target} ({len(answer)}c)", file=sys.stderr)
        else:
            print(answer)
    else:
        if args.target:
            Path(args.target).expanduser().write_text(answer)
            print(f"wrote {args.target} ({len(answer)}c)", file=sys.stderr)

    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    print(f"[{args.model}: {u.get('prompt_tokens', '?')} in ({cached} cached) / "
          f"{u.get('completion_tokens', '?')} out]", file=sys.stderr)

    save_last_messages(messages, answer, args.model)

    log_dir = Path("~/.local/share/ask-llm").expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    with open(log_dir / "usage.log", "a") as lf:
        lf.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}\t{args.model}\t"
                 f"{u.get('prompt_tokens', 0)}\t{u.get('completion_tokens', 0)}\n")


if __name__ == "__main__":
    main()
