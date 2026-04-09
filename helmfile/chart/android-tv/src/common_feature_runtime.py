#!/usr/bin/env python3
import threading
import time
from typing import Any, Callable

import reactivex as rx
from reactivex import operators as ops
from reactivex.scheduler import ThreadPoolScheduler


class BusyDropGate:
    """Thread-safe gate for at-most-one concurrent worker with drop counting."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False
        self._drop_count = 0

    def try_begin(self) -> bool:
        with self._lock:
            if self._busy:
                self._drop_count += 1
                return False
            self._busy = True
            return True

    def finish(self) -> None:
        with self._lock:
            self._busy = False

    def snapshot(self) -> tuple[bool, int]:
        with self._lock:
            return self._busy, self._drop_count


class SequencedActionGate:
    """Thread-safe gate for non-blocking action dispatch.

    Rules:
    - At most one action worker runs at a time.
    - A result sequence can be dispatched only once.
    - Optional cooldown blocks dispatch until cooldown expires.
    """

    def __init__(self, cooldown_sec: float = 0.0) -> None:
        self._lock = threading.Lock()
        self._cooldown_sec = max(0.0, cooldown_sec)

        self._busy = False
        self._last_dispatched_seq = -1
        self._next_allowed_at = 0.0

        self._skip_busy_count = 0
        self._skip_cooldown_count = 0

    def try_begin(self, seq: int) -> tuple[bool, str, float]:
        """Try to start an action.

        Returns (allowed, reason, cooldown_left_ms).
        reason is one of: ok, busy, stale, cooldown.
        """
        with self._lock:
            now = time.monotonic()
            if self._busy:
                self._skip_busy_count += 1
                return False, "busy", 0.0

            if seq <= self._last_dispatched_seq:
                return False, "stale", 0.0

            if now < self._next_allowed_at:
                self._skip_cooldown_count += 1
                return False, "cooldown", (self._next_allowed_at - now) * 1000.0

            self._busy = True
            self._last_dispatched_seq = seq
            return True, "ok", 0.0

    def finish(self) -> None:
        with self._lock:
            self._busy = False
            self._next_allowed_at = time.monotonic() + self._cooldown_sec

    def snapshot(self) -> tuple[bool, int, int]:
        with self._lock:
            return self._busy, self._skip_busy_count, self._skip_cooldown_count

    def skip_total(self) -> int:
        with self._lock:
            return self._skip_busy_count + self._skip_cooldown_count


class AsyncBusyWorker:
    """Run a worker function asynchronously with busy/drop gating."""

    def __init__(self, run_fn: Callable[..., None], name: str) -> None:
        self._run_fn = run_fn
        self._name = name
        self._gate = BusyDropGate()
        self._scheduler = ThreadPoolScheduler(max_workers=1)

    def submit(self, *args: Any) -> bool:
        if not self._gate.try_begin():
            return False
        rx.just(args).pipe(ops.subscribe_on(self._scheduler)).subscribe(
            on_next=lambda item: self._run(*item),
            on_error=lambda _exc: self._gate.finish(),
        )
        return True

    def _run(self, *args: Any) -> None:
        try:
            self._run_fn(*args)
        finally:
            self._gate.finish()

    def snapshot(self) -> tuple[bool, int]:
        return self._gate.snapshot()


class AsyncSequencedWorker:
    """Run a worker function asynchronously with sequenced dispatch gating."""

    def __init__(self, run_fn: Callable[..., None], cooldown_sec: float, name: str) -> None:
        self._run_fn = run_fn
        self._name = name
        self._gate = SequencedActionGate(cooldown_sec=cooldown_sec)
        self._scheduler = ThreadPoolScheduler(max_workers=1)

    def submit(self, seq: int, *args: Any) -> tuple[bool, str, float]:
        allowed, reason, cooldown_left_ms = self._gate.try_begin(seq)
        if not allowed:
            return False, reason, cooldown_left_ms
        rx.just(args).pipe(ops.subscribe_on(self._scheduler)).subscribe(
            on_next=lambda item: self._run(*item),
            on_error=lambda _exc: self._gate.finish(),
        )
        return True, "ok", 0.0

    def _run(self, *args: Any) -> None:
        try:
            self._run_fn(*args)
        finally:
            self._gate.finish()

    def snapshot(self) -> tuple[bool, int, int]:
        return self._gate.snapshot()
