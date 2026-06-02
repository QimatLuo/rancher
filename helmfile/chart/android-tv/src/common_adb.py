#!/usr/bin/env python3
import random
import re
import subprocess
import threading
import time
from collections import deque
from typing import Any

import reactivex as rx


ADB_TIMEOUT_SEC = 10.0
ADB_EVENT_TTL_SEC = 3.5
ADB_EVENT_MAX = 80

_adb_event_lock = threading.Lock()
_adb_events: deque[dict[str, Any]] = deque(maxlen=ADB_EVENT_MAX)
_adb_shell_lock = threading.Lock()


def _record_adb_event(event: dict[str, Any]) -> None:
    row = dict(event)
    row["at"] = time.monotonic()
    with _adb_event_lock:
        _adb_events.append(row)


def get_recent_adb_events(ttl_sec: float = ADB_EVENT_TTL_SEC, max_events: int = 24) -> list[dict[str, Any]]:
    now = time.monotonic()
    keep_ttl = max(0.1, float(ttl_sec))
    keep_count = max(1, int(max_events))

    with _adb_event_lock:
        while _adb_events and (now - float(_adb_events[0].get("at", now))) > keep_ttl:
            _adb_events.popleft()
        rows = list(_adb_events)[-keep_count:]

    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        at = float(item.pop("at", now))
        item["age_sec"] = max(0.0, now - at)
        out.append(item)
    return out


def adb_shell_ok(args: list[str]) -> bool:
    cmd = ["adb", "shell", *args]
    ok = False
    try:
        with _adb_shell_lock:
            result = subprocess.run(
                cmd,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=ADB_TIMEOUT_SEC,
            )
        ok = result.returncode == 0
        if not ok:
            stderr_text = (result.stderr or "").strip().replace("\n", " | ")
            print(f"adb_shell_ok failed rc={result.returncode} cmd={' '.join(cmd)} stderr={stderr_text}")
    except subprocess.TimeoutExpired:
        print(f"adb_shell_ok timeout={ADB_TIMEOUT_SEC}s cmd={' '.join(cmd)}")
        ok = False
    except BaseException as exc:
        print(f"adb_shell_ok exception={exc} cmd={' '.join(cmd)}")
        ok = False

    if len(args) >= 3 and args[0] == "input" and args[1] == "keyevent":
        _record_adb_event(
            {
                "kind": "keyevent",
                "code": str(args[2]),
                "ok": bool(ok),
            }
        )
    elif len(args) >= 3 and args[0] == "input" and args[1] == "text":
        _record_adb_event(
            {
                "kind": "text",
                "text": " ".join(str(x) for x in args[2:])[:80],
                "ok": bool(ok),
            }
        )

    return ok


def probe_device_tap_size() -> tuple[int, int] | None:
    try:
        result = subprocess.run(
            ["adb", "shell", "wm", "size"],
            check=False,
            capture_output=True,
            text=True,
            timeout=ADB_TIMEOUT_SEC,
        )
    except BaseException:
        return None

    if result.returncode != 0:
        return None

    output = (result.stdout or "") + "\n" + (result.stderr or "")
    match = re.search(r"Override size:\s*(\d+)x(\d+)", output)
    if not match:
        match = re.search(r"Physical size:\s*(\d+)x(\d+)", output)
    if not match:
        return None

    width = int(match.group(1))
    height = int(match.group(2))
    if width <= 0 or height <= 0:
        return None
    if height > width:
        width, height = height, width
    return width, height


def map_frame_point_to_target(
    x: int,
    y: int,
    source_width: int,
    source_height: int,
    target_width: int,
    target_height: int,
) -> tuple[int, int]:
    src_w = max(1, source_width)
    src_h = max(1, source_height)
    tgt_w = max(1, target_width)
    tgt_h = max(1, target_height)

    if src_w > 1 and src_h > 1:
        tap_x = int(round(x * (tgt_w - 1) / (src_w - 1)))
        tap_y = int(round(y * (tgt_h - 1) / (src_h - 1)))
    else:
        tap_x = int(round(x))
        tap_y = int(round(y))

    tap_x = max(0, min(tgt_w - 1, tap_x))
    tap_y = max(0, min(tgt_h - 1, tap_y))
    return tap_x, tap_y


def resolve_tap_target_size(
    source_width: int,
    source_height: int,
    *,
    override_width: int = 0,
    override_height: int = 0,
    log_prefix: str = "tap_map",
) -> tuple[int, int]:
    src_w = max(1, source_width)
    src_h = max(1, source_height)

    if override_width > 0 and override_height > 0:
        print(
            f"{log_prefix} source=env "
            f"target={override_width}x{override_height} "
            f"capture={src_w}x{src_h}"
        )
        return override_width, override_height

    probed = probe_device_tap_size()
    if probed is not None:
        tw, th = probed
        print(f"{log_prefix} source=wm_size target={tw}x{th} capture={src_w}x{src_h}")
        return tw, th

    print(f"{log_prefix} source=fallback_identity target={src_w}x{src_h}")
    return src_w, src_h


def adb_tap_frame_point_with_random_drift(
    frame_x: int,
    frame_y: int,
    source_width: int,
    source_height: int,
    target_width: int,
    target_height: int,
    *,
    max_drift_px: int = 10,
) -> tuple[bool, int, int, int, int]:
    tap_x, tap_y = map_frame_point_to_target(
        frame_x,
        frame_y,
        source_width,
        source_height,
        target_width,
        target_height,
    )

    clamped_target_w = max(1, target_width)
    clamped_target_h = max(1, target_height)
    ok, used_x, used_y = adb_tap_with_random_drift(
        tap_x,
        tap_y,
        max_drift_px=max_drift_px,
        min_x=0,
        min_y=0,
        max_x=clamped_target_w - 1,
        max_y=clamped_target_h - 1,
    )
    _record_adb_event(
        {
            "kind": "tap",
            "frame_x": int(frame_x),
            "frame_y": int(frame_y),
            "mapped_x": int(tap_x),
            "mapped_y": int(tap_y),
            "used_x": int(used_x),
            "used_y": int(used_y),
            "ok": bool(ok),
        }
    )
    return ok, tap_x, tap_y, used_x, used_y


def adb_tap_with_random_drift(
    x: int,
    y: int,
    max_drift_px: int = 2,
    min_x: int = 0,
    min_y: int = 0,
    max_x: int | None = None,
    max_y: int | None = None,
) -> tuple[bool, int, int]:
    drift = max(0, max_drift_px)
    tap_x = x + random.randint(-drift, drift)
    tap_y = y + random.randint(-drift, drift)

    if max_x is not None:
        tap_x = min(max_x, tap_x)
    if max_y is not None:
        tap_y = min(max_y, tap_y)
    tap_x = max(min_x, tap_x)
    tap_y = max(min_y, tap_y)

    ok = adb_shell_ok(["input", "tap", str(tap_x), str(tap_y)])
    return ok, tap_x, tap_y


def adb_swipe_with_random_drift(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    *,
    duration_ms: int = 0,
    max_drift_px: int = 2,
    min_x: int = 0,
    min_y: int = 0,
    max_x: int | None = None,
    max_y: int | None = None,
) -> tuple[bool, int, int, int, int]:
    drift = max(0, max_drift_px)

    if start_x == end_x and start_y == end_y:
        drift_x = random.randint(-drift, drift)
        drift_y = random.randint(-drift, drift)
        used_start_x = start_x + drift_x
        used_start_y = start_y + drift_y
        used_end_x = end_x + drift_x
        used_end_y = end_y + drift_y
    else:
        used_start_x = start_x + random.randint(-drift, drift)
        used_start_y = start_y + random.randint(-drift, drift)
        used_end_x = end_x + random.randint(-drift, drift)
        used_end_y = end_y + random.randint(-drift, drift)

    if max_x is not None:
        used_start_x = min(max_x, used_start_x)
        used_end_x = min(max_x, used_end_x)
    if max_y is not None:
        used_start_y = min(max_y, used_start_y)
        used_end_y = min(max_y, used_end_y)

    used_start_x = max(min_x, used_start_x)
    used_start_y = max(min_y, used_start_y)
    used_end_x = max(min_x, used_end_x)
    used_end_y = max(min_y, used_end_y)

    swipe_duration_ms = max(0, int(round(duration_ms)))
    ok = adb_shell_ok(
        [
            "input",
            "swipe",
            str(used_start_x),
            str(used_start_y),
            str(used_end_x),
            str(used_end_y),
            str(swipe_duration_ms),
        ]
    )
    return ok, used_start_x, used_start_y, used_end_x, used_end_y


def adb_swipe_frame_point_with_random_drift(
    frame_x: int,
    frame_y: int,
    source_width: int,
    source_height: int,
    target_width: int,
    target_height: int,
    *,
    duration_ms: int = 0,
    max_drift_px: int = 2,
) -> tuple[bool, int, int, int, int]:
    tap_x, tap_y = map_frame_point_to_target(
        frame_x,
        frame_y,
        source_width,
        source_height,
        target_width,
        target_height,
    )

    clamped_target_w = max(1, target_width)
    clamped_target_h = max(1, target_height)
    ok, used_start_x, used_start_y, used_end_x, used_end_y = adb_swipe_with_random_drift(
        tap_x,
        tap_y,
        tap_x,
        tap_y,
        duration_ms=duration_ms,
        max_drift_px=max_drift_px,
        min_x=0,
        min_y=0,
        max_x=clamped_target_w - 1,
        max_y=clamped_target_h - 1,
    )
    _record_adb_event(
        {
            "kind": "swipe",
            "frame_x": int(frame_x),
            "frame_y": int(frame_y),
            "mapped_x": int(tap_x),
            "mapped_y": int(tap_y),
            "used_x": int(used_start_x),
            "used_y": int(used_start_y),
            "used_end_x": int(used_end_x),
            "used_end_y": int(used_end_y),
            "duration_ms": int(max(0, int(round(duration_ms)))),
            "ok": bool(ok),
        }
    )
    return ok, tap_x, tap_y, used_start_x, used_start_y


def wait_with_random_jitter(
    base_sec: float,
    shutdown_event: threading.Event | None = None,
    max_extra_sec: float = 1.0,
) -> bool:
    total_sec = max(0.0, base_sec) + random.uniform(0.0, max(0.0, max_extra_sec))
    done_event = threading.Event()
    subscription = rx.timer(total_sec).subscribe(
        on_next=lambda _i: done_event.set(),
        on_error=lambda _exc: done_event.set(),
        on_completed=lambda: done_event.set(),
    )
    try:
        if shutdown_event is None:
            done_event.wait(timeout=max(0.0, total_sec) + 0.05)
            return False
        while not done_event.is_set():
            if shutdown_event.wait(timeout=0.05):
                return True
        return False
    finally:
        subscription.dispose()
