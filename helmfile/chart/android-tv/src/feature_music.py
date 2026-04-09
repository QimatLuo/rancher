#!/usr/bin/env python3
import signal
import threading
import time

from dataclasses import dataclass

import cv2
import reactivex as rx
from reactivex import operators as ops
from reactivex.disposable import Disposable
from reactivex.scheduler import NewThreadScheduler

from common_adb import adb_swipe_frame_point_with_random_drift, resolve_tap_target_size, wait_with_random_jitter
from common_feature_settings import (
    get_common_runtime_settings,
    make_common_startup_log,
    make_common_stream_manager,
    make_common_waiting_payload,
    make_common_web_server,
)
from common_stream import FrameStreamManager


shutdown_event = threading.Event()
COMMON_SETTINGS = get_common_runtime_settings()

BASE_DO_X = 178
BASE_DO_Y = 224
COL_STEP_X = 70
ROW_STEP_Y = 50
GRID_ROWS = 3
GRID_COLS = 5

DEFAULT_AUTO_PLAY_ONCE = True
DEFAULT_START_DELAY_SEC = 1.0
DEFAULT_MAX_DRIFT_PX = 1
DEFAULT_NOTE_UNIT_SEC = 0.30
ARPEGGIO_PREFIX = "arp:"


@dataclass
class RuntimeConfig:
    auto_play_once: bool
    start_delay_sec: float
    max_drift_px: int
    note_unit_sec: float


class OverlayState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._payload: dict = {"shapes": [], "texts": []}

    def update(self, frame_h: int, age_sec: float) -> None:
        texts: list[dict] = []
        if age_sec >= COMMON_SETTINGS.stale_frame_sec:
            texts.append(
                {
                    "text": f"RECONNECTING {age_sec:.1f}s",
                    "x": 16,
                    "y": 32,
                    "color": "rgb(0,255,255)",
                    "font": "bold 20px monospace",
                }
            )
        with self._lock:
            self._payload = {"shapes": [], "texts": texts, "frame_h": frame_h}

    def make_web_payload(self) -> dict:
        with self._lock:
            return dict(self._payload)


def request_shutdown(reason: str) -> None:
    if shutdown_event.is_set():
        return
    print(f"shutdown reason={reason}")
    shutdown_event.set()


def install_signal_handlers() -> None:
    def _handler(signum: int, _frame: object | None) -> None:
        name = str(signum)
        try:
            name = signal.Signals(signum).name
        except ValueError:
            pass
        request_shutdown(f"signal={name}")

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def load_config() -> RuntimeConfig:
    auto_play_once = DEFAULT_AUTO_PLAY_ONCE
    start_delay_sec = DEFAULT_START_DELAY_SEC
    max_drift_px = DEFAULT_MAX_DRIFT_PX
    note_unit_sec = DEFAULT_NOTE_UNIT_SEC

    if start_delay_sec < 0:
        raise ValueError("MUSIC_START_DELAY_SEC must be >= 0")
    if max_drift_px < 0:
        raise ValueError("MUSIC_TAP_MAX_DRIFT_PX must be >= 0")
    if note_unit_sec <= 0:
        raise ValueError("MUSIC_NOTE_UNIT_SEC must be > 0")

    return RuntimeConfig(
        auto_play_once=auto_play_once,
        start_delay_sec=start_delay_sec,
        max_drift_px=max_drift_px,
        note_unit_sec=note_unit_sec,
    )


def build_note_points() -> dict[str, tuple[int, int]]:
    points: dict[str, tuple[int, int]] = {}

    names = [
        ["do", "re", "mi", "fa", "so"],
        ["la", "si", "do2", "re2", "mi2"],
        ["fa2", "so2", "la2", "si2", "do3"],
    ]

    for row in range(GRID_ROWS):
        for col in range(GRID_COLS):
            x = BASE_DO_X + col * COL_STEP_X
            y = BASE_DO_Y + row * ROW_STEP_Y
            points[names[row][col]] = (x, y)

    return points


# Beethoven Ode to Joy main theme (extended, diatonic full-phrase arrangement).
# Each tuple is (note_or_arpeggio, hold_beats, rest_beats).
# Arpeggio format: "arp:note1,note2,note3".
MUSIC_SEQUENCE: tuple[tuple[str, float, float], ...] = (
    # A section
    ("mi", 1.0, 0.0), ("mi", 1.0, 0.0), ("fa", 1.0, 0.0), ("so", 1.0, 0.0),
    ("so", 1.0, 0.0), ("fa", 1.0, 0.0), ("mi", 1.0, 0.0), ("re", 1.0, 0.0),
    ("do", 1.0, 0.0), ("do", 1.0, 0.0), ("re", 1.0, 0.0), ("mi", 1.0, 0.0),
    ("mi", 1.5, 0.0), ("re", 0.5, 0.0), ("re", 2.0, 0.5),

    # A' section
    ("mi", 1.0, 0.0), ("mi", 1.0, 0.0), ("fa", 1.0, 0.0), ("so", 1.0, 0.0),
    ("so", 1.0, 0.0), ("fa", 1.0, 0.0), ("mi", 1.0, 0.0), ("re", 1.0, 0.0),
    ("do", 1.0, 0.0), ("do", 1.0, 0.0), ("re", 1.0, 0.0), ("mi", 1.0, 0.0),
    ("re", 1.5, 0.0), ("do", 0.5, 0.0), ("do", 2.0, 0.5),

    # B section
    ("re", 1.0, 0.0), ("re", 1.0, 0.0), ("mi", 1.0, 0.0), ("do", 1.0, 0.0),
    ("re", 1.0, 0.0), ("mi", 0.5, 0.0), ("fa", 0.5, 0.0), ("mi", 1.0, 0.0),
    ("do", 1.0, 0.0), ("re", 0.5, 0.0), ("mi", 0.5, 0.0), ("fa", 1.0, 0.0),
    ("mi", 1.0, 0.0), ("re", 1.0, 0.0), ("do", 1.0, 0.0), ("re", 1.0, 0.0),
    ("so", 2.0, 0.5),

    # Return A with octave lift ending
    ("mi", 1.0, 0.0), ("mi", 1.0, 0.0), ("fa", 1.0, 0.0), ("so", 1.0, 0.0),
    ("so", 1.0, 0.0), ("fa", 1.0, 0.0), ("mi", 1.0, 0.0), ("re", 1.0, 0.0),
    ("do", 1.0, 0.0), ("do", 1.0, 0.0), ("re", 1.0, 0.0), ("mi", 1.0, 0.0),
    ("re", 1.5, 0.0), ("do", 0.5, 0.0), ("do", 1.0, 0.0),
    ("arp:do,mi,so", 1.0, 0.0), ("arp:re,fa,la", 1.0, 0.0), ("arp:mi,so,do2", 2.0, 0.0),
    ("do2", 2.0, 0.00),
)


class JpegPayloadProvider:
    def __init__(self, manager: FrameStreamManager, overlay_state: OverlayState) -> None:
        self._manager = manager
        self._overlay_state = overlay_state
        self._jpeg_opts = [int(cv2.IMWRITE_JPEG_QUALITY), COMMON_SETTINGS.jpeg_quality]

        self._lock = threading.Lock()
        self._last_ts = -1.0
        self._last_payload: bytes | None = None

    def __call__(self) -> bytes | None:
        snapshot = self._manager.snapshot()
        if snapshot is None:
            return None

        captured_at, frame = snapshot
        age_sec = time.monotonic() - captured_at
        self._overlay_state.update(frame.shape[0], age_sec)

        with self._lock:
            if captured_at == self._last_ts and self._last_payload is not None:
                return self._last_payload

            ok, encoded = cv2.imencode(".jpg", frame, self._jpeg_opts)
            if not ok:
                return None

            self._last_ts = captured_at
            self._last_payload = encoded.tobytes()
            return self._last_payload


class MusicActionRunner:
    def __init__(self, config: RuntimeConfig, shutdown: threading.Event) -> None:
        self._config = config
        self._shutdown_event = shutdown
        self._run_lock = threading.Lock()
        self._run_scheduler = NewThreadScheduler()
        self._run_subscription: Disposable | None = None
        self._played_once = False
        self._note_points = build_note_points()
        self._tap_target_w, self._tap_target_h = resolve_tap_target_size(
            COMMON_SETTINGS.capture_width,
            COMMON_SETTINGS.capture_height,
            log_prefix="music tap_map",
        )

    def start(self) -> None:
        with self._run_lock:
            if self._run_subscription is not None:
                return
            self._run_subscription = (
                rx.just(0)
                .pipe(ops.subscribe_on(self._run_scheduler))
                .subscribe(
                    on_next=lambda _i: self._run_loop(),
                    on_error=self._on_run_error,
                    on_completed=self._on_run_completed,
                )
            )

    def join(self, timeout: float | None = None) -> None:
        deadline = None if timeout is None else (time.monotonic() + max(0.0, timeout))
        while True:
            with self._run_lock:
                running = self._run_subscription is not None
            if not running:
                return
            if deadline is not None and time.monotonic() >= deadline:
                return
            self._shutdown_event.wait(timeout=0.05)

    def _clear_run_subscription(self) -> None:
        with self._run_lock:
            self._run_subscription = None

    def _on_run_error(self, exc: Exception) -> None:
        print(f"music runner_error type={type(exc).__name__} msg={exc}")
        self._clear_run_subscription()

    def _on_run_completed(self) -> None:
        self._clear_run_subscription()

    def _press_note(self, note: str, hold_sec: float) -> bool:
        frame_x, frame_y = self._note_points[note]
        hold_ms = max(0, int(round(hold_sec * 1000.0)))
        ok, tap_x, tap_y, used_x, used_y = adb_swipe_frame_point_with_random_drift(
            frame_x,
            frame_y,
            COMMON_SETTINGS.capture_width,
            COMMON_SETTINGS.capture_height,
            self._tap_target_w,
            self._tap_target_h,
            duration_ms=hold_ms,
            max_drift_px=self._config.max_drift_px,
        )
        print(
            f"music note={note} frame=({frame_x},{frame_y}) mapped=({tap_x},{tap_y}) "
            f"press_ms={hold_ms} tap=({used_x},{used_y}) drift=({used_x - tap_x},{used_y - tap_y}) ok={int(ok)}"
        )
        return ok

    def _play_arpeggio(self, note_names: list[str], hold_sec: float) -> None:
        if not note_names:
            return

        step_sec = hold_sec / float(len(note_names)) if hold_sec > 0 else 0.0
        for note in note_names:
            if self._shutdown_event.is_set():
                return
            self._press_note(note, step_sec)

    def _parse_arpeggio(self, token: str) -> list[str]:
        if not token.startswith(ARPEGGIO_PREFIX):
            return []

        payload = token[len(ARPEGGIO_PREFIX):]
        parts = [part.strip() for part in payload.split(",")]
        notes = [part for part in parts if part in self._note_points]
        return notes

    def _play_score_once(self) -> None:
        if self._shutdown_event.is_set() or self._played_once:
            return

        if self._shutdown_event.wait(timeout=self._config.start_delay_sec):
            return

        for note_token, hold_beats, rest_beats in MUSIC_SEQUENCE:
            if self._shutdown_event.is_set():
                return

            hold_sec = max(0.0, hold_beats) * self._config.note_unit_sec
            rest_sec = max(0.0, rest_beats) * self._config.note_unit_sec
            arpeggio_notes = self._parse_arpeggio(note_token)
            if arpeggio_notes:
                print(f"music arpeggio={','.join(arpeggio_notes)} hold={hold_sec:.3f}s")
                self._play_arpeggio(arpeggio_notes, hold_sec)
            elif note_token in self._note_points:
                self._press_note(note_token, hold_sec)
            else:
                print(f"music skip_unknown_note token={note_token}")
            if wait_with_random_jitter(
                rest_sec,
                shutdown_event=self._shutdown_event,
                max_extra_sec=min(0.04, rest_sec * 0.2),
            ):
                return

        self._played_once = True
        print("music status=played_once")

    def _run_loop(self) -> None:
        if not self._config.auto_play_once:
            print("music status=disabled")
            return

        self._play_score_once()


def main() -> None:
    install_signal_handlers()
    config = load_config()

    stream_manager = make_common_stream_manager(shutdown_event)
    stream_manager.start()

    waiting_payload = make_common_waiting_payload()
    overlay_state = OverlayState()
    payload_provider = JpegPayloadProvider(stream_manager, overlay_state)
    web_server = make_common_web_server(
        payload_provider,
        waiting_payload,
        shutdown_event,
        overlay_provider=overlay_state.make_web_payload,
    )

    music_runner = MusicActionRunner(config, shutdown_event)
    music_runner.start()

    print(
        make_common_startup_log(
            "mode=music-once song=ode-to-joy-full "
            f"note_unit={config.note_unit_sec:.3f}s"
        )
    )

    try:
        web_server.serve_forever()
    except KeyboardInterrupt:
        request_shutdown("keyboard_interrupt")
    finally:
        web_server.shutdown()
        request_shutdown("server_stop")
        music_runner.join(timeout=2.0)
        stream_manager.join(timeout=2.0)


if __name__ == "__main__":
    main()
