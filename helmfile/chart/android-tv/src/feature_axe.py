#!/usr/bin/env python3
import signal
import threading
import time
from dataclasses import dataclass

import cv2
import numpy as np
import reactivex as rx
from reactivex import operators as ops
from reactivex.disposable import Disposable
from reactivex.scheduler import NewThreadScheduler

from common_action import (
    DEFAULT_ACTION_STEPS as TOOL_REPAIR_ACTION_STEPS,
    RED_LINE_ACTION_STEPS as STAMINA_RECOVERY_ACTION_STEPS,
    run_action_steps,
)
from common_adb import (
    adb_tap_frame_point_with_random_drift,
    adb_shell_ok,
    resolve_tap_target_size,
    wait_with_random_jitter,
)
from common_feature_runtime import AsyncBusyWorker, AsyncSequencedWorker
from common_feature_settings import (
    get_common_tool_state_settings,
    get_common_runtime_settings,
    make_common_startup_log,
    make_common_stream_manager,
    make_common_waiting_payload,
    make_common_web_server,
)
from common_stream import FrameStreamManager
from common_cv import hex_to_rgb, is_color_close, sample_rgb_from_frame, scale_point_from_reference


shutdown_event = threading.Event()
COMMON_SETTINGS = get_common_runtime_settings()
COMMON_TOOL_STATE_SETTINGS = get_common_tool_state_settings()

STAMINA_LINE_REF_WIDTH = 640
STAMINA_LINE_REF_HEIGHT = 360
STAMINA_LINE_REF_Y = 38
STAMINA_LINE_REF_X1 = 110
STAMINA_LINE_REF_X2 = 170
TOOL_STATE_SAMPLE_A_REF_X = 409
TOOL_STATE_SAMPLE_A_REF_Y = 220
TOOL_STATE_SAMPLE_B_REF_X = 377
TOOL_STATE_SAMPLE_B_REF_Y = 243
DEFAULT_AUTOMATION_ENABLED = True
DEFAULT_AUTOMATION_LOOP_INTERVAL_SEC = 1.0
DEFAULT_TAP_TARGET_WIDTH = 0
DEFAULT_TAP_TARGET_HEIGHT = 0
DEFAULT_STAMINA_LINE_MIN_RATIO = 0.65
DEFAULT_STAMINA_LINE_H1_MIN = 0
DEFAULT_STAMINA_LINE_H1_MAX = 10
DEFAULT_STAMINA_LINE_H2_MIN = 170
DEFAULT_STAMINA_LINE_H2_MAX = 179
DEFAULT_STAMINA_LINE_MIN_S = 120
DEFAULT_STAMINA_LINE_MIN_V = 120
DEFAULT_BURST_TAP_COUNT = 20
DEFAULT_BURST_TAP_INTERVAL_SEC = 0.25


@dataclass
class RuntimeConfig:
    automation_enabled: bool
    automation_loop_interval_sec: float
    tool_state_color_tolerance_a: int
    tool_state_color_tolerance_b: int
    tap_target_width: int
    tap_target_height: int
    stamina_line_min_ratio: float
    stamina_line_h1_min: int
    stamina_line_h1_max: int
    stamina_line_h2_min: int
    stamina_line_h2_max: int
    stamina_line_min_s: int
    stamina_line_min_v: int
    burst_tap_count: int
    burst_tap_interval_sec: float


class OverlayState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._payload: dict = {"shapes": [], "texts": []}

    def update(self, age_sec: float) -> None:
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
            self._payload = {"shapes": [], "texts": texts}

    def make_web_payload(self) -> dict:
        with self._lock:
            return dict(self._payload)


@dataclass(frozen=True)
class AxeDecision:
    should_recover_stamina: bool


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
    automation_enabled = DEFAULT_AUTOMATION_ENABLED
    automation_loop_interval_sec = DEFAULT_AUTOMATION_LOOP_INTERVAL_SEC
    tool_state_color_tolerance_a = COMMON_TOOL_STATE_SETTINGS.color_tolerance_a
    tool_state_color_tolerance_b = COMMON_TOOL_STATE_SETTINGS.color_tolerance_b
    tap_target_width = DEFAULT_TAP_TARGET_WIDTH
    tap_target_height = DEFAULT_TAP_TARGET_HEIGHT
    stamina_line_min_ratio = DEFAULT_STAMINA_LINE_MIN_RATIO
    stamina_line_h1_min = DEFAULT_STAMINA_LINE_H1_MIN
    stamina_line_h1_max = DEFAULT_STAMINA_LINE_H1_MAX
    stamina_line_h2_min = DEFAULT_STAMINA_LINE_H2_MIN
    stamina_line_h2_max = DEFAULT_STAMINA_LINE_H2_MAX
    stamina_line_min_s = DEFAULT_STAMINA_LINE_MIN_S
    stamina_line_min_v = DEFAULT_STAMINA_LINE_MIN_V
    burst_tap_count = DEFAULT_BURST_TAP_COUNT
    burst_tap_interval_sec = DEFAULT_BURST_TAP_INTERVAL_SEC

    if automation_loop_interval_sec < 0:
        raise ValueError("AXE_AUTOMATION_LOOP_INTERVAL_SEC must be >= 0")
    if tool_state_color_tolerance_a < 0:
        raise ValueError("AXE_COLOR_TOLERANCE_A must be >= 0")
    if tool_state_color_tolerance_b < 0:
        raise ValueError("AXE_COLOR_TOLERANCE_B must be >= 0")
    if tap_target_width < 0 or tap_target_height < 0:
        raise ValueError("AXE_TAP_TARGET_WIDTH/HEIGHT must be >= 0")
    if (tap_target_width == 0) != (tap_target_height == 0):
        raise ValueError("AXE_TAP_TARGET_WIDTH and AXE_TAP_TARGET_HEIGHT must both be 0 or both be > 0")
    if stamina_line_min_ratio < 0 or stamina_line_min_ratio > 1:
        raise ValueError("AXE_RED_LINE_MIN_RATIO must be within 0..1")
    if stamina_line_h1_min < 0 or stamina_line_h1_min > 179:
        raise ValueError("AXE_RED_LINE_H1_MIN must be within 0..179")
    if stamina_line_h1_max < 0 or stamina_line_h1_max > 179:
        raise ValueError("AXE_RED_LINE_H1_MAX must be within 0..179")
    if stamina_line_h2_min < 0 or stamina_line_h2_min > 179:
        raise ValueError("AXE_RED_LINE_H2_MIN must be within 0..179")
    if stamina_line_h2_max < 0 or stamina_line_h2_max > 179:
        raise ValueError("AXE_RED_LINE_H2_MAX must be within 0..179")
    if stamina_line_h1_min > stamina_line_h1_max:
        raise ValueError("AXE_RED_LINE_H1_MIN must be <= AXE_RED_LINE_H1_MAX")
    if stamina_line_h2_min > stamina_line_h2_max:
        raise ValueError("AXE_RED_LINE_H2_MIN must be <= AXE_RED_LINE_H2_MAX")
    if stamina_line_min_s < 0 or stamina_line_min_s > 255:
        raise ValueError("AXE_RED_LINE_MIN_S must be within 0..255")
    if stamina_line_min_v < 0 or stamina_line_min_v > 255:
        raise ValueError("AXE_RED_LINE_MIN_V must be within 0..255")
    if burst_tap_count <= 0:
        raise ValueError("AXE_BURST_TAP_COUNT must be > 0")
    if burst_tap_interval_sec < 0:
        raise ValueError("AXE_BURST_TAP_INTERVAL_SEC must be >= 0")

    return RuntimeConfig(
        automation_enabled=automation_enabled,
        automation_loop_interval_sec=automation_loop_interval_sec,
        tool_state_color_tolerance_a=tool_state_color_tolerance_a,
        tool_state_color_tolerance_b=tool_state_color_tolerance_b,
        tap_target_width=tap_target_width,
        tap_target_height=tap_target_height,
        stamina_line_min_ratio=stamina_line_min_ratio,
        stamina_line_h1_min=stamina_line_h1_min,
        stamina_line_h1_max=stamina_line_h1_max,
        stamina_line_h2_min=stamina_line_h2_min,
        stamina_line_h2_max=stamina_line_h2_max,
        stamina_line_min_s=stamina_line_min_s,
        stamina_line_min_v=stamina_line_min_v,
        burst_tap_count=burst_tap_count,
        burst_tap_interval_sec=burst_tap_interval_sec,
    )


class JpegPayloadProvider:
    def __init__(self, manager: FrameStreamManager, config: RuntimeConfig, overlay_state: OverlayState) -> None:
        self._manager = manager
        self._config = config
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
        self._overlay_state.update(age_sec)

        with self._lock:
            if captured_at == self._last_ts and self._last_payload is not None:
                return self._last_payload

            ok, encoded = cv2.imencode(".jpg", frame, self._jpeg_opts)
            if not ok:
                return None

            self._last_ts = captured_at
            self._last_payload = encoded.tobytes()
            return self._last_payload


class AxeAutomationRunner:
    def __init__(
        self,
        manager: FrameStreamManager,
        config: RuntimeConfig,
        shutdown_event: threading.Event,
    ) -> None:
        self._manager = manager
        self._config = config
        self._shutdown_event = shutdown_event
        self._loop_lock = threading.Lock()
        self._loop_scheduler = NewThreadScheduler()
        self._loop_subscription: Disposable | None = None
        self._tap_target_w, self._tap_target_h = resolve_tap_target_size(
            COMMON_SETTINGS.capture_width,
            COMMON_SETTINGS.capture_height,
            override_width=self._config.tap_target_width,
            override_height=self._config.tap_target_height,
            log_prefix="axe_automation tap_map",
        )
        self._latest_frame_ts = -1.0

        self._cv_worker = AsyncBusyWorker(self._run_cv_detection, name="axe-cv")
        self._cv_lock = threading.Lock()
        self._cv_status = "idle"
        self._cv_result_seq = 0
        self._cv_last_decision: AxeDecision | None = None
        self._last_dispatched_cv_seq = -1

        self._action_worker = AsyncSequencedWorker(self._run_action_cycle, cooldown_sec=0.0, name="axe-action")
        self._action_lock = threading.Lock()
        self._action_status = "idle"
        self._action_run_count = 0
        self._action_skip_count = 0

    def start(self) -> None:
        if not self._config.automation_enabled:
            print("axe_automation status=disabled")
            return
        with self._loop_lock:
            if self._loop_subscription is not None:
                return
            tick_sec = self._config.automation_loop_interval_sec if self._config.automation_loop_interval_sec > 0 else 0.01
            self._loop_subscription = (
                rx.interval(tick_sec, scheduler=self._loop_scheduler)
                .pipe(ops.take_while(lambda _i: not self._shutdown_event.is_set()))
                .subscribe(
                    on_next=lambda _i: self._on_loop_tick(),
                    on_error=self._on_loop_error,
                    on_completed=self._on_loop_completed,
                )
            )
        print(
            "axe_automation status=enabled "
            f"interval={self._config.automation_loop_interval_sec}s "
            f"tol_a={self._config.tool_state_color_tolerance_a} tol_b={self._config.tool_state_color_tolerance_b}"
        )

    def join(self, timeout: float | None = None) -> None:
        deadline = None if timeout is None else (time.monotonic() + max(0.0, timeout))
        while True:
            with self._loop_lock:
                running = self._loop_subscription is not None
            if not running:
                return
            if deadline is not None and time.monotonic() >= deadline:
                return
            self._shutdown_event.wait(timeout=0.05)

    def _clear_loop_subscription(self) -> None:
        with self._loop_lock:
            self._loop_subscription = None

    def _on_loop_error(self, exc: Exception) -> None:
        print(f"axe_automation loop_error type={type(exc).__name__} msg={exc}")
        self._clear_loop_subscription()

    def _on_loop_completed(self) -> None:
        self._clear_loop_subscription()

    def _on_loop_tick(self) -> None:
        if self._shutdown_event.is_set():
            return
        if self._is_action_busy() or self._is_cv_busy():
            return

        decision, cv_result_seq = self._snapshot_cv_result()
        if decision is not None and cv_result_seq > self._last_dispatched_cv_seq:
            if self._submit_action_if_allowed(decision, cv_result_seq):
                self._last_dispatched_cv_seq = cv_result_seq
            return

        snapshot = self._manager.snapshot()
        if snapshot is None:
            return
        captured_at, frame = snapshot
        if captured_at != self._latest_frame_ts:
            self._submit_cv_if_idle(captured_at, frame.copy())

    def _is_action_busy(self) -> bool:
        busy, _skip_busy_count, _skip_cooldown_count = self._action_worker.snapshot()
        return busy

    def _is_cv_busy(self) -> bool:
        busy, _drop_count = self._cv_worker.snapshot()
        return busy

    def _wait(self, seconds: float) -> bool:
        return wait_with_random_jitter(seconds, shutdown_event=self._shutdown_event)

    def _wait_for_updated_frame(self, previous_ts: float, timeout_sec: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_sec)
        while not self._shutdown_event.is_set() and time.monotonic() < deadline:
            snapshot = self._manager.snapshot()
            if snapshot is not None:
                captured_at, _frame = snapshot
                if captured_at > previous_ts:
                    return True
            if self._shutdown_event.wait(timeout=0.05):
                return False
        return False

    def _open_tool_pack_before_tool_state_check(self) -> bool:
        previous_ts = -1.0
        snapshot = self._manager.snapshot()
        if snapshot is not None:
            previous_ts, _frame = snapshot

        self._tap(590, 328)
        if self._shutdown_event.wait(timeout=1.0):
            return False

        refreshed = self._wait_for_updated_frame(previous_ts, timeout_sec=1.5)
        print(f"axe_automation tool_pack_open refreshed={int(refreshed)}")
        return True

    def _close_tool_pack_after_tool_state_check(self) -> bool:
        if self._shutdown_event.is_set():
            return False
        closed = self._tap(590, 328)
        print(f"axe_automation tool_pack_close ok={int(closed)}")
        return closed

    def _equip_tool_from_tool_pack(self) -> bool:
        if self._shutdown_event.is_set():
            return False
        equipped = self._tap(377, 243)
        print(f"axe_automation tool_equip ok={int(equipped)}")
        return equipped

    @staticmethod
    def _noop_publish_active_step(_order: int) -> None:
        return

    def _tap(self, frame_x: int, frame_y: int) -> bool:
        ok, tap_x, tap_y, used_x, used_y = adb_tap_frame_point_with_random_drift(
            frame_x,
            frame_y,
            COMMON_SETTINGS.capture_width,
            COMMON_SETTINGS.capture_height,
            self._tap_target_w,
            self._tap_target_h,
            max_drift_px=2,
        )
        print(
            f"axe_automation tap frame=({frame_x},{frame_y}) mapped=({tap_x},{tap_y}) tap=({used_x},{used_y}) "
            f"drift=({used_x - tap_x},{used_y - tap_y}) ok={int(ok)}"
        )
        return ok

    def _input_text(self, text: str) -> bool:
        ok = adb_shell_ok(["input", "text", text])
        print(f"axe_automation input_text={text} ok={int(ok)}")
        return ok

    def _is_stamina_line_in_frame(
        self,
        frame: np.ndarray,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        min_ratio: float = 0.85,
    ) -> bool:
        h, w = frame.shape[:2]
        if y1 != y2:
            return False
        if y1 < 0 or y1 >= h:
            return False

        left = max(0, min(x1, x2))
        right = min(w - 1, max(x1, x2))
        if left > right:
            return False

        y_start = max(0, y1 - 2)
        y_end = min(h - 1, y1 + 2)
        best_ratio = 0.0

        for y in range(y_start, y_end + 1):
            line_bgr = frame[y : y + 1, left : right + 1]
            if line_bgr.size == 0:
                continue

            line_hsv = cv2.cvtColor(line_bgr, cv2.COLOR_BGR2HSV)
            lower1 = np.array(
                [self._config.stamina_line_h1_min, self._config.stamina_line_min_s, self._config.stamina_line_min_v],
                dtype=np.uint8,
            )
            upper1 = np.array([self._config.stamina_line_h1_max, 255, 255], dtype=np.uint8)
            lower2 = np.array(
                [self._config.stamina_line_h2_min, self._config.stamina_line_min_s, self._config.stamina_line_min_v],
                dtype=np.uint8,
            )
            upper2 = np.array([self._config.stamina_line_h2_max, 255, 255], dtype=np.uint8)

            mask1 = cv2.inRange(line_hsv, lower1, upper1)
            mask2 = cv2.inRange(line_hsv, lower2, upper2)
            mask = cv2.bitwise_or(mask1, mask2)
            stamina_ratio = float(np.count_nonzero(mask)) / float(mask.size)
            if stamina_ratio >= best_ratio:
                best_ratio = stamina_ratio

        has_stamina_warning = best_ratio >= min_ratio
        return has_stamina_warning

    def _has_top_stamina_warning_line_in_frame(self, frame: np.ndarray, min_ratio: float) -> bool:
        h, w = frame.shape[:2]
        if h <= 0 or w <= 0:
            return False

        scale_x = w / float(STAMINA_LINE_REF_WIDTH)
        scale_y = h / float(STAMINA_LINE_REF_HEIGHT)
        target_y = int(round(STAMINA_LINE_REF_Y * scale_y))
        expected_x1 = int(round(STAMINA_LINE_REF_X1 * scale_x))
        expected_x2 = int(round(STAMINA_LINE_REF_X2 * scale_x))

        # Keep a narrow horizontal tolerance to absorb minor UI offsets without drifting to other regions.
        margin_px = max(2, int(round(6 * scale_x)))
        scan_x1 = max(0, expected_x1 - margin_px)
        scan_x2 = min(w - 1, expected_x2 + margin_px)

        return self._is_stamina_line_in_frame(frame, scan_x1, target_y, scan_x2, target_y, min_ratio=min_ratio)

    def _should_recover_stamina_from_frame(self, frame: np.ndarray) -> bool:
        sample_count = 5
        sample_interval_sec = 0.12
        stamina_warning_hits = 0

        for idx in range(sample_count):
            if self._has_top_stamina_warning_line_in_frame(frame, min_ratio=self._config.stamina_line_min_ratio):
                stamina_warning_hits += 1
            if idx < sample_count - 1 and self._shutdown_event.wait(timeout=sample_interval_sec):
                return True

        return stamina_warning_hits > 0

    def _evaluate_tool_state_from_latest_frame(self) -> tuple[bool, bool] | None:
        stable = self._wait_for_stable_tool_state_frame(
            stable_count=COMMON_TOOL_STATE_SETTINGS.stable_frame_count,
            color_tolerance=COMMON_TOOL_STATE_SETTINGS.stable_color_tolerance,
            timeout_sec=COMMON_TOOL_STATE_SETTINGS.stable_timeout_sec,
            poll_interval_sec=COMMON_TOOL_STATE_SETTINGS.stable_poll_interval_sec,
        )
        if stable is None:
            return None

        frame, ax, ay, bx, by, color_a, color_b, stable_hits = stable

        target_a = hex_to_rgb("FFFDFF")
        target_b = hex_to_rgb("A19997")
        has_equipped_tool_signal = is_color_close(color_a, target_a, self._config.tool_state_color_tolerance_a)
        has_tool_worn_signal = is_color_close(color_b, target_b, self._config.tool_state_color_tolerance_b)
        print(
            "axe_automation tool_state "
            f"A@({ax},{ay})={color_a} equipped_signal={int(has_equipped_tool_signal)} "
            f"B@({bx},{by})={color_b} worn_signal={int(has_tool_worn_signal)} "
            f"stable_frames={stable_hits}"
        )
        return has_equipped_tool_signal, has_tool_worn_signal

    def _resolve_tool_state_sample_points(self, frame: np.ndarray) -> tuple[int, int, int, int] | None:
        h, w = frame.shape[:2]
        if h <= 0 or w <= 0:
            return None
        ax, ay = scale_point_from_reference(
            w,
            h,
            TOOL_STATE_SAMPLE_A_REF_X,
            TOOL_STATE_SAMPLE_A_REF_Y,
            ref_w=STAMINA_LINE_REF_WIDTH,
            ref_h=STAMINA_LINE_REF_HEIGHT,
        )
        bx, by = scale_point_from_reference(
            w,
            h,
            TOOL_STATE_SAMPLE_B_REF_X,
            TOOL_STATE_SAMPLE_B_REF_Y,
            ref_w=STAMINA_LINE_REF_WIDTH,
            ref_h=STAMINA_LINE_REF_HEIGHT,
        )
        return ax, ay, bx, by

    def _wait_for_stable_tool_state_frame(
        self,
        *,
        stable_count: int,
        color_tolerance: int,
        timeout_sec: float,
        poll_interval_sec: float,
    ) -> tuple[
        np.ndarray,
        int,
        int,
        int,
        int,
        tuple[int, int, int],
        tuple[int, int, int],
        int,
    ] | None:
        required = max(1, int(stable_count))
        deadline = time.monotonic() + max(0.0, timeout_sec)

        last_ts = -1.0
        last_color_a: tuple[int, int, int] | None = None
        last_color_b: tuple[int, int, int] | None = None
        hits = 0
        latest_payload: tuple[
            np.ndarray, int, int, int, int, tuple[int, int, int], tuple[int, int, int]
        ] | None = None

        while not self._shutdown_event.is_set():
            snapshot = self._manager.snapshot()
            if snapshot is not None:
                captured_at, frame = snapshot
                if captured_at != last_ts:
                    last_ts = captured_at
                    points = self._resolve_tool_state_sample_points(frame)
                    if points is not None:
                        ax, ay, bx, by = points
                        color_a = sample_rgb_from_frame(frame, ax, ay)
                        color_b = sample_rgb_from_frame(frame, bx, by)
                        if color_a is not None and color_b is not None:
                            if (
                                last_color_a is not None
                                and last_color_b is not None
                                and is_color_close(color_a, last_color_a, color_tolerance)
                                and is_color_close(color_b, last_color_b, color_tolerance)
                            ):
                                hits += 1
                            else:
                                hits = 1
                            last_color_a = color_a
                            last_color_b = color_b
                            latest_payload = (frame, ax, ay, bx, by, color_a, color_b)
                            if hits >= required:
                                return (*latest_payload, hits)

            if time.monotonic() >= deadline:
                break
            if self._shutdown_event.wait(timeout=max(0.0, poll_interval_sec)):
                return None

        if latest_payload is None:
            return None
        print(
            "axe_automation tool_state_unstable "
            f"stable_hits={hits} required={required} timeout={timeout_sec:.2f}s"
        )
        return (*latest_payload, hits)

    def _run_cv_detection(self, captured_at: float, frame: np.ndarray) -> None:
        started = time.monotonic()
        status = "ok"
        decision: AxeDecision | None = None

        h, w = frame.shape[:2]
        if h <= 0 or w <= 0:
            status = "invalid_frame"
        else:
            try:
                should_recover_stamina = self._should_recover_stamina_from_frame(frame)
                decision = AxeDecision(should_recover_stamina=should_recover_stamina)
                print("axe_automation cv " f"stamina_recover={int(should_recover_stamina)}")
            except Exception as exc:
                status = f"error:{type(exc).__name__}"
                print(f"axe_automation cv_error type={type(exc).__name__} msg={exc}")

        latency_ms = (time.monotonic() - started) * 1000.0
        with self._cv_lock:
            self._cv_status = status
            self._cv_result_seq += 1
            self._cv_last_decision = decision
        print(f"axe_automation cv_done status={status} ms={latency_ms:.1f}")

    def _submit_cv_if_idle(self, captured_at: float, frame: np.ndarray) -> None:
        if not self._cv_worker.submit(captured_at, frame):
            return

        with self._cv_lock:
            self._cv_status = "running"

        self._latest_frame_ts = captured_at

    def _snapshot_cv_result(self) -> tuple[AxeDecision | None, int]:
        with self._cv_lock:
            return self._cv_last_decision, self._cv_result_seq

    def _run_action_cycle(self, decision: AxeDecision) -> None:
        status = "ok"
        try:
            if decision.should_recover_stamina:
                print("axe_automation flow stamina_recover=1")
                interrupted = run_action_steps(
                    STAMINA_RECOVERY_ACTION_STEPS,
                    publish_active_step=self._noop_publish_active_step,
                    tap=self._tap,
                    input_text=self._input_text,
                    wait=self._wait,
                    start_tap_order=1,
                )
                if interrupted:
                    status = "interrupted"
                    return
            else:
                print("axe_automation flow stamina_recover=0")

            if not self._open_tool_pack_before_tool_state_check():
                status = "interrupted"
                return

            tool_state = self._evaluate_tool_state_from_latest_frame()
            if tool_state is None:
                self._close_tool_pack_after_tool_state_check()
                needs_repair = True
                print("axe_automation flow repair=1 reason=tool_state_unavailable")
            else:
                has_equipped_tool_signal, has_tool_worn_signal = tool_state

                if not has_equipped_tool_signal:
                    print("axe_automation flow equip_tool=1 close_mode=auto")
                    equipped = self._equip_tool_from_tool_pack()
                    if not equipped:
                        needs_repair = True
                        print("axe_automation flow repair=1 reason=equip_failed")
                    else:
                        # Equip tap auto-closes tool pack; sample once more to read post-equip wear state.
                        snapshot = self._manager.snapshot()
                        previous_ts = snapshot[0] if snapshot is not None else -1.0
                        if not self._shutdown_event.wait(timeout=1.0):
                            self._wait_for_updated_frame(previous_ts, timeout_sec=1.5)

                        post_equip_tool_state = self._evaluate_tool_state_from_latest_frame()
                        if post_equip_tool_state is None:
                            needs_repair = True
                            print("axe_automation flow repair=1 reason=post_equip_tool_state_unavailable")
                        else:
                            _post_equipped_signal, post_worn_signal = post_equip_tool_state
                            needs_repair = bool(post_worn_signal)
                            print(f"axe_automation flow post_equip_worn_signal={int(post_worn_signal)}")
                else:
                    print("axe_automation flow equip_tool=0 close_mode=manual")
                    self._close_tool_pack_after_tool_state_check()
                    needs_repair = bool(has_tool_worn_signal)

                print(f"axe_automation flow repair={int(needs_repair)}")

            if needs_repair:
                interrupted = run_action_steps(
                    TOOL_REPAIR_ACTION_STEPS,
                    publish_active_step=self._noop_publish_active_step,
                    tap=self._tap,
                    input_text=self._input_text,
                    wait=self._wait,
                    start_tap_order=1,
                )
                if interrupted:
                    status = "interrupted"
                    return

            print("axe_automation flow use_tool=1")
            burst_tap = (550, 273)
            for idx in range(self._config.burst_tap_count):
                self._tap(*burst_tap)
                if idx < self._config.burst_tap_count - 1 and self._shutdown_event.wait(
                    timeout=self._config.burst_tap_interval_sec
                ):
                    status = "interrupted"
                    return

            if self._wait(120.0):
                status = "interrupted"
                return
        except Exception as exc:
            status = f"error:{type(exc).__name__}"
            print(f"axe_automation action_error type={type(exc).__name__} msg={exc}")
        finally:
            with self._action_lock:
                self._action_status = status
                self._action_run_count += 1

    def _submit_action_if_allowed(self, decision: AxeDecision, cv_result_seq: int) -> bool:
        allowed, _reason, _cooldown_left_ms = self._action_worker.submit(cv_result_seq, decision)
        if not allowed:
            self._action_skip_count += 1
            return False

        with self._action_lock:
            self._action_status = "running"
        return True


def main() -> None:
    install_signal_handlers()
    config = load_config()

    stream_manager = make_common_stream_manager(shutdown_event)
    stream_manager.start()

    waiting_payload = make_common_waiting_payload()
    overlay_state = OverlayState()
    payload_provider = JpegPayloadProvider(stream_manager, config, overlay_state)
    automation_runner = AxeAutomationRunner(
        stream_manager,
        config,
        shutdown_event,
    )
    automation_runner.start()

    web_server = make_common_web_server(
        payload_provider,
        waiting_payload,
        shutdown_event,
        overlay_provider=overlay_state.make_web_payload,
    )

    print(make_common_startup_log())

    try:
        web_server.serve_forever()
    except KeyboardInterrupt:
        request_shutdown("keyboard_interrupt")
    finally:
        web_server.shutdown()
        request_shutdown("server_stop")
        automation_runner.join(timeout=2.0)
        stream_manager.join(timeout=2.0)


if __name__ == "__main__":
    main()
