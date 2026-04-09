#!/usr/bin/env bash

set -u

if ! command -v adb >/dev/null 2>&1; then
  echo "[connect.sh] failed: adb command not found in container PATH." >&2
  exit 1
fi

PORT="${ADB_SERVER_PORT:-}"

if [[ -z "$PORT" ]]; then
  echo "[connect.sh] failed: ADB_SERVER_PORT is not set." >&2
  exit 1
fi

if [[ ! "$PORT" =~ ^[0-9]+$ ]]; then
  echo "[connect.sh] failed: ADB_SERVER_PORT must be numeric, got '$PORT'." >&2
  exit 1
fi

PREFERRED_IP="${ADB_SERVER_IP:-}"

targets=()

if [[ -n "$PREFERRED_IP" ]]; then
  targets+=("${PREFERRED_IP}:${PORT}")
fi

for host in $(seq 100 200); do
  targets+=("192.168.1.${host}:${PORT}")
done

for target in "${targets[@]}"; do
  echo "[connect.sh] trying: $target"
  output="$(timeout 1s adb connect "$target" 2>&1)"
  status=$?

  if [[ $status -eq 0 ]] && grep -qiE "connected to|already connected to" <<< "$output"; then
    echo "[connect.sh] connected: $target"
    exit 0
  fi
done

echo "[connect.sh] failed: cannot connect to 192.168.1.100-200 on port ${PORT}." >&2
exit 1
