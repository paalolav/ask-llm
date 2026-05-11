#!/usr/bin/env bash
# setup-keychain.sh — Store LiteLLM virtual key in macOS Keychain
#
# Usage:
#   ./setup-keychain.sh                         # uses defaults
#   ./setup-keychain.sh my-service my-account   # custom service/account
#
# The stored key is read automatically by ask-llm when
# ASK_LLM_AUTH and ASK_LLM_API_KEY are both unset.
# Override lookup with env:
#   ASK_LLM_AUTH_KEYCHAIN_SERVICE (default: ask-llm)
#   ASK_LLM_AUTH_KEYCHAIN_ACCOUNT (default: litellm-vkey)
set -euo pipefail

SERVICE="${1:-ask-llm}"
ACCOUNT="${2:-litellm-vkey}"

echo "Saving LiteLLM virtual key to macOS Keychain"
echo "  Service : $SERVICE"
echo "  Account : $ACCOUNT"
echo ""
echo "macOS will prompt for system password or biometric to allow access."
echo ""
read -rs -p "Enter key (input hidden): " KEY
echo ""

if [[ -z "$KEY" ]]; then
    echo "Error: key cannot be empty" >&2
    exit 1
fi

security add-generic-password -U -s "$SERVICE" -a "$ACCOUNT" -w "$KEY"
echo "Saved successfully."
echo ""
echo "Verify: security find-generic-password -s $SERVICE -a $ACCOUNT -w"
echo ""
echo "To remove from .env after setup:"
echo "  Remove or comment out ASK_LLM_AUTH / ASK_LLM_API_KEY from ~/.config/ask-llm.env"
echo "  ask-llm will automatically fall back to the Keychain entry."
