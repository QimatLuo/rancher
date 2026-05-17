#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEY_DIR="$ROOT_DIR/.sops-local/age"
KEY_FILE="$KEY_DIR/keys.txt"
GPG_HOME="$ROOT_DIR/.sops-local/gnupg"
GPG_KEY_NAME="helmfile-local-sops"
CONFIG_FILE="$ROOT_DIR/.sops.local.yaml"

mkdir -p "$KEY_DIR"

write_age_config() {
  local pubkey="$1"
  cat > "$CONFIG_FILE" <<EOF
creation_rules:
  - path_regex: chart/cluster-issuer/secrets\\.sops\\.yaml$
    age: $pubkey
EOF
}

write_gpg_config() {
  local fpr="$1"
  cat > "$CONFIG_FILE" <<EOF
creation_rules:
  - path_regex: chart/cluster-issuer/secrets\\.sops\\.yaml$
    pgp: $fpr
EOF
}

if command -v age-keygen >/dev/null 2>&1; then
  if [[ ! -f "$KEY_FILE" ]]; then
    age-keygen -o "$KEY_FILE"
    chmod 600 "$KEY_FILE"
  fi
  PUBKEY="$(grep '^# public key:' "$KEY_FILE" | head -n 1 | sed 's/^# public key: //')"

  if [[ -z "$PUBKEY" ]]; then
    echo "Failed to read public key from $KEY_FILE" >&2
    exit 1
  fi

  write_age_config "$PUBKEY"

  echo "Project-local SOPS key is ready (age)."
  echo "Key file: $KEY_FILE"
  echo "Config file: $CONFIG_FILE"
  echo "Use with: ./scripts/with-local-sops.sh <command> [args...]"
  exit 0
fi

if command -v gpg >/dev/null 2>&1; then
  mkdir -p "$GPG_HOME"
  chmod 700 "$GPG_HOME"

  FPR="$(gpg --homedir "$GPG_HOME" --batch --with-colons --list-secret-keys 2>/dev/null | awk -F: '/^fpr:/ {print $10; exit}')"

  if [[ -z "$FPR" ]]; then
    gpg --homedir "$GPG_HOME" --batch --pinentry-mode loopback --passphrase '' \
      --quick-generate-key "$GPG_KEY_NAME" default default 1y >/dev/null
    FPR="$(gpg --homedir "$GPG_HOME" --batch --with-colons --list-secret-keys 2>/dev/null | awk -F: '/^fpr:/ {print $10; exit}')"
  fi

  if [[ -z "$FPR" ]]; then
    echo "Failed to create/read project-local GPG key in $GPG_HOME" >&2
    exit 1
  fi

  write_gpg_config "$FPR"

  echo "Project-local SOPS key is ready (gpg)."
  echo "GPG home: $GPG_HOME"
  echo "GPG fingerprint: $FPR"
  echo "Config file: $CONFIG_FILE"
  echo "Use with: ./scripts/with-local-sops.sh <command> [args...]"
  exit 0
fi

echo "Cannot initialize project-local SOPS key: no supported backend found." >&2
echo "Install either 'age-keygen' or 'gpg'." >&2
exit 1
