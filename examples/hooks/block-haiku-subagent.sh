#!/bin/bash
# Block Haiku subagent spawning in Claude Code — use ask-llm instead.
#
# Install: add to hooks.PreToolUse in Claude Code settings.json:
#   {"matcher": "Agent", "hooks": [{"type": "command", "command": "bash /path/to/block-haiku-subagent.sh", "timeout": 5}]}
input=$(cat)
model=$(echo "$input" | jq -r '.tool_input.model // empty')
if [[ "$model" == *haiku* ]]; then
  echo "BLOCKED: Haiku not allowed. Use ask-llm for lightweight tasks." >&2
  exit 2
fi
exit 0
