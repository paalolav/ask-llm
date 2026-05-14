# ask-llm

A small command-line client for OpenAI-compatible chat/completions APIs.
Provider-agnostic via env config (LiteLLM, Ollama, llama.cpp, mlx-lm, exo,
vLLM, OpenAI/Anthropic/DeepSeek). Single dependency: PyYAML for the model
catalog.

## What it does

- Send a text prompt to a configured model and get the reply on stdout
- Send multiple files as a "corpus" and ask questions about them (`--paths`)
- Generate code in the style of a reference file (`--context`)
- Write the reply to a file (`--target`)
- Refuse to read known secret-bearing paths (`.env`, SSH private keys,
  keychains, `.pem`, ...) — prompt-injection guard for agent use

## Installation

Pick one:

```bash
# pipx (recommended for daily use; isolates dependencies)
pipx install git+https://github.com/paalolav/ask-llm

# or: clone + symlink (good for local hacking)
git clone https://github.com/paalolav/ask-llm
cd ask-llm
./setup.sh
```

`setup.sh` symlinks `bin/ask-llm` into `~/bin/` (or `~/.local/bin/`). Both
install methods provide the same `ask-llm` command.

Then create `~/.config/ask-llm.env`:

```bash
ASK_LLM_URL=http://localhost:4000/v1/chat/completions
ASK_LLM_MODEL=your-model-id
ASK_LLM_AUTH=dummy
```

Examples for non-LiteLLM endpoints:

```bash
# Ollama
ASK_LLM_URL=http://localhost:11434/v1/chat/completions
ASK_LLM_MODEL=llama3.1:8b
ASK_LLM_AUTH=dummy

# OpenAI
ASK_LLM_URL=https://api.openai.com/v1/chat/completions
ASK_LLM_MODEL=gpt-4o-mini
ASK_LLM_AUTH=sk-...
```

Environment variables of the same name override the config file. macOS users
can store `ASK_LLM_AUTH` in Keychain via `setup-keychain.sh` to avoid
plaintext on disk.

## Usage

```bash
# Plain prompt
ask-llm "Explain the difference between rebase and merge"

# Question about files. Files are wrapped in <file path='X'>...</file>
# so the model can quote paths/lines and the corpus prefix is stable
# for KV-cache reuse across calls.
ask-llm -q "Find any SQL injection risks" --paths app/db.py app/api.py

# Generate a new file in the style of an existing one
ask-llm -q "pytest test for the auth flow" \
        --context tests/test_main.py \
        --target tests/test_auth.py

# Override the model on the fly
ask-llm --model gpt-4o-mini "..."

# Continue the previous conversation (cached in ~/.cache/ask-llm/last.json)
ask-llm "what causes a deadlock?"
ask-llm -c "give me an example in Python"
ask-llm -c "now show how to detect it"
```

`-c` reuses the prior model unless you pass `--model`. It cannot be combined
with `--paths`, `--context`, or `--task` (start a fresh conversation for those).

The cache (`~/.cache/ask-llm/last.json`, mode 0600) stores prompts and replies
in plaintext. Conversation length is capped at 20 messages (≈10 turns) so
long `-c` chains don't exceed context windows. Run `rm ~/.cache/ask-llm/last.json`
to clear if a prompt contained secrets you don't want lingering on disk.

`-q`/`--question` is required when using `--paths`, because argparse is
greedy and would otherwise consume the prompt into the paths list.

## Security

`--paths` and `--context` go through `safe_read()`, which refuses:

- Paths containing `.ssh/`, `.aws/`, `.gnupg/`, `Library/Keychains/`
- Files named `.env`, `.netrc`, `.pgpass`, `credentials`, `master.passwd`, `shadow`
- Files starting with `id_rsa`, `id_ed25519`, `id_ecdsa`, `id_dsa` (without `.pub`)
- File extensions `.pem`, `.key`

This is a prompt-injection guard for when the tool is invoked by an agent
that can choose paths.

## Testing

```bash
python3 -m unittest discover tests -v
```

Tests mock the network and do not require a running LLM endpoint.

## LiteLLM Spend Tracking

If your LiteLLM proxy has a PostgreSQL database connected, ask-llm can track
per-request usage via virtual keys.

### Setup

1. Configure LiteLLM with `database_url` and `master_key` in your proxy config
2. Generate a virtual key:
   ```bash
   curl -s http://<litellm-host>:4000/key/generate \
     -H "Authorization: Bearer <master-key>" \
     -H "Content-Type: application/json" \
     -d '{"key_alias": "ask-llm", "user_id": "ask-llm"}'
   ```
3. Add the returned key to your config:
   ```
   ASK_LLM_API_KEY=sk-<returned-key>
   ```

Without `ASK_LLM_API_KEY`, ask-llm falls back to `ASK_LLM_AUTH` (default: `dummy`).
Inference works either way — only spend tracking requires a virtual key.

## Stats (LiteLLM only)

```bash
ask-llm stats              # Usage summary, last 7 days
ask-llm stats --days 30    # Last 30 days
ask-llm stats --by-model   # Grouped by model
ask-llm stats --raw        # Raw JSON
```

Cross-consumer stats (requires admin access):
```bash
LITELLM_MASTER_KEY=sk-... ask-llm stats --all
```

Stats reads `/spend/logs` on the configured endpoint. Against Ollama,
llama.cpp, OpenAI, etc., it prints a friendly notice and exits — they don't
have that endpoint.

## Claude Code Integration

### Block Haiku subagents

`examples/hooks/block-haiku-subagent.sh` blocks Claude Code from spawning
Haiku subagents, encouraging use of ask-llm with a local model instead.

Install in Claude Code `settings.json`:
```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Agent",
        "hooks": [{"type": "command", "command": "bash /path/to/block-haiku-subagent.sh", "timeout": 5}]
      }
    ]
  }
}
```

## License

MIT. See [LICENSE](LICENSE).
