# ask-llm

A small command-line client for OpenAI-compatible chat/completions APIs.
Stdlib-only Python (no pip dependencies), provider-agnostic via env config.

## What it does

- Send a text prompt to a configured model and get the reply on stdout
- Send multiple files as a "corpus" and ask questions about them (`--paths`)
- Generate code in the style of a reference file (`--context`)
- Write the reply to a file (`--target`)
- Refuse to read known secret-bearing paths (`.env`, SSH private keys,
  keychains, `.pem`, ...) — prompt-injection guard for agent use

## Installation

```bash
git clone <repo> ask-llm
cd ask-llm
./setup.sh
```

`setup.sh`:
- Symlinks `bin/ask-llm` into `~/bin/` (or `~/.local/bin/` if `~/bin` isn't on PATH)
- Copies `.env.example` to `~/.config/ask-llm.env` if it doesn't exist

Then fill in `~/.config/ask-llm.env` with your values:

```bash
ASK_LLM_URL=http://localhost:4000/v1/chat/completions
ASK_LLM_MODEL=your-model-id
ASK_LLM_AUTH=dummy
```

Environment variables of the same name override the config file.

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
```

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

## Stats

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
