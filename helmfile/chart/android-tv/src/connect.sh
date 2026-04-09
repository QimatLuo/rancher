#!/usr/bin/env bash

success=1

for ip in $(ip neigh | awk '{print $1}' | grep '^192')
do
  if timeout 1 bash -c "echo > /dev/tcp/$ip/5555" 2>/dev/null; then
    if adb connect "$ip:5555" >/dev/null 2>&1; then
      success=0
      break
    fi
  fi
done

exit "$success"
