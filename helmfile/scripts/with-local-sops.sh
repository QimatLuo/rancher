#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEY_FILE="$ROOT_DIR/.sops-local/age/keys.txt"
GPG_HOME="$ROOT_DIR/.sops-local/gnupg"
CONFIG_FILE="$ROOT_DIR/.sops.local.yaml"

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "Missing local SOPS config: $CONFIG_FILE" >&2
  echo "Run: ./scripts/init-local-sops.sh" >&2
  exit 1
fi

if [[ $# -eq 0 ]]; then
  echo "Usage: ./scripts/with-local-sops.sh <command> [args ...]" >&2
  exit 1
fi

export SOPS_CONFIG="$CONFIG_FILE"

if [[ -f "$KEY_FILE" ]]; then
  export SOPS_AGE_KEY_FILE="$KEY_FILE"
fi

if [[ -d "$GPG_HOME" ]]; then
  export GNUPGHOME="$GPG_HOME"
fi

exec "$@"
