#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
mkdir -p "$BIN_DIR" "$HOME/.config"

# Prefer ~/bin if it's already on PATH (common Homebrew/Mac convention)
if [[ ":$PATH:" == *":$HOME/bin:"* ]]; then
    BIN_DIR="$HOME/bin"
fi

ln -sf "$SCRIPT_DIR/bin/ask-llm" "$BIN_DIR/ask-llm"
chmod +x "$SCRIPT_DIR/bin/ask-llm"
echo "Linked: $BIN_DIR/ask-llm -> $SCRIPT_DIR/bin/ask-llm"

CONFIG="$HOME/.config/ask-llm.env"
if [ ! -f "$CONFIG" ]; then
    cp "$SCRIPT_DIR/.env.example" "$CONFIG"
    echo "Created: $CONFIG (fill in ASK_LLM_URL and ASK_LLM_MODEL)"
else
    echo "Kept existing: $CONFIG"
fi

echo
echo "Done. Run 'ask-llm --help' to verify."
