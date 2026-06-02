#!/usr/bin/env python3
import math
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
    use_item,
)
from common_adb import (
    adb_shell_ok,
    adb_swipe_frame_point_with_random_drift,
    adb_tap_frame_point_with_random_drift,
    resolve_tap_target_size,
    wait_with_random_jitter,
)
from common_cv import hex_to_rgb, is_color_close, sample_rgb_from_frame, scale_point_from_reference
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


shutdown_event = threading.Event()
COMMON_SETTINGS = get_common_runtime_settings()
COMMON_TOOL_STATE_SETTINGS = get_common_tool_state_settings()

DEFAULT_AUTOMATION_ENABLED = True
DEFAULT_AUTOMATION_LOOP_INTERVAL_SEC = 0.25
DEFAULT_TAP_TARGET_WIDTH = 0
DEFAULT_TAP_TARGET_HEIGHT = 0
DEFAULT_HOLD_SEC = 7.0
DEFAULT_COLOR_TOLERANCE = 25
DEFAULT_BAIT_REARM_COOLDOWN_SEC = 12.0
DEFAULT_BAIT_HIT_CONFIRM_SEC = 3.0
DEFAULT_CV_MIN_INTERVAL_SEC = 0.12
DEFAULT_NO_SLASH_RECOVER_AFTER_SEC = 10.0
DEFAULT_NO_SLASH_RECOVER_WAIT_SEC = 2.0
DEFAULT_STAMINA_LINE_MIN_RATIO = 0.65
DEFAULT_STAMINA_LINE_H1_MIN = 0
DEFAULT_STAMINA_LINE_H1_MAX = 10
DEFAULT_STAMINA_LINE_H2_MIN = 170
DEFAULT_STAMINA_LINE_H2_MAX = 179
DEFAULT_STAMINA_LINE_MIN_S = 120
DEFAULT_STAMINA_LINE_MIN_V = 120

STAMINA_LINE_REF_WIDTH = 640
STAMINA_LINE_REF_HEIGHT = 360
STAMINA_LINE_REF_Y = 38
STAMINA_LINE_REF_X1 = 110
STAMINA_LINE_REF_X2 = 170
TOOL_STATE_SAMPLE_A_REF_X = 509
TOOL_STATE_SAMPLE_A_REF_Y = 220
TOOL_STATE_SAMPLE_B_REF_X = 477
TOOL_STATE_SAMPLE_B_REF_Y = 243

MIN_LENGTH = 100.0
MIN_ANGLE_DEG = 0.8
MAX_ANGLE_DEG = 35.0

ROI_X1 = 120
ROI_Y1 = 60
ROI_X2 = 350
ROI_Y2 = 318
HOLD_FRAME_X = 550
HOLD_FRAME_Y = 273
BAIT_SAMPLE_X = 51
BAIT_SAMPLE_Y = 336
BAIT_TARGET_HEX = "B1B3BB"
FALLBACK_LABEL_X = BAIT_SAMPLE_X
FALLBACK_LABEL_Y = BAIT_SAMPLE_Y - 28


@dataclass
class RuntimeConfig:
    automation_enabled: bool
    automation_loop_interval_sec: float
    tap_target_width: int
    tap_target_height: int
    hold_sec: float
    color_tolerance: int
    bait_rearm_cooldown_sec: float
    bait_hit_confirm_sec: float
    cv_min_interval_sec: float
    no_slash_recover_after_sec: float
    no_slash_recover_wait_sec: float
    tool_state_color_tolerance_a: int
    tool_state_color_tolerance_b: int
    tool_state_stable_frame_count: int
    tool_state_stable_color_tolerance: int
    tool_state_stable_timeout_sec: float
    tool_state_stable_poll_interval_sec: float


@dataclass(frozen=True)
class FishDecision:
    should_hold: bool
    use_bait: bool
    recover_no_slash: bool = False
    recover_stamina: bool = False


@dataclass(frozen=True)
class SlashOverlayResult:
    captured_at: float
    processed_at: float
    status: str
    candidates: tuple[tuple[int, int, int, int, float], ...]


class SlashOverlayState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._result: SlashOverlayResult | None = None
        self._action_status: str | None = None
        self._sample_points: list[dict] = []
        self._countdowns: list[dict] = []

    def update(
        self,
        captured_at: float,
        status: str,
        candidates: list[tuple[int, int, int, int, float, float, float, float, float, float, float, float, float]],
    ) -> None:
        top = tuple((int(c[0]), int(c[1]), int(c[2]), int(c[3]), float(c[4])) for c in candidates[:3])
        result = SlashOverlayResult(
            captured_at=captured_at,
            processed_at=time.monotonic(),
            status=status,
            candidates=top,
        )
        with self._lock:
            self._result = result

    def snapshot(self) -> SlashOverlayResult | None:
        with self._lock:
            return self._result

    def set_action_status(self, status: str | None) -> None:
        with self._lock:
            self._action_status = status

    def action_status(self) -> str | None:
        with self._lock:
            return self._action_status

    def set_sample_points(self, sample_points: list[dict]) -> None:
        normalized: list[dict] = []
        for item in sample_points:
            if not isinstance(item, dict):
                continue
            normalized.append(
                {
                    "x": int(item.get("x", 0)),
                    "y": int(item.get("y", 0)),
                    "label": str(item.get("label", "")),
                    "color": str(item.get("color", "rgb(0,255,255)")),
                }
            )
        with self._lock:
            self._sample_points = normalized

    def set_countdowns(self, countdowns: list[dict]) -> None:
        now = time.monotonic()
        normalized: list[dict] = []
        for item in countdowns:
            if not isinstance(item, dict):
                continue
            until_at = item.get("until_at")
            if until_at is None:
                remaining_sec = max(0.0, float(item.get("remaining_sec", 0.0)))
                until_at = now + remaining_sec
            normalized.append(
                {
                    "x": int(item.get("x", 0)),
                    "y": int(item.get("y", 0)),
                    "label": str(item.get("label", "")),
                    "color": str(item.get("color", "rgb(255,255,255)")),
                    "until_at": float(until_at),
                }
            )
        with self._lock:
            self._countdowns = normalized

    def _snapshot_overlay_debug(self) -> tuple[list[dict], list[dict]]:
        now = time.monotonic()
        with self._lock:
            sample_points = [dict(item) for item in self._sample_points]
            countdowns: list[dict] = []
            for item in self._countdowns:
                remaining_sec = max(0.0, float(item.get("until_at", now) - now))
                countdowns.append(
                    {
                        "x": int(item.get("x", 0)),
                        "y": int(item.get("y", 0)),
                        "label": str(item.get("label", "")),
                        "color": str(item.get("color", "rgb(255,255,255)")),
                        "remaining_sec": remaining_sec,
                    }
                )
        return sample_points, countdowns

    def clear_candidates(self, status: str) -> None:
        now = time.monotonic()
        with self._lock:
            captured_at = self._result.captured_at if self._result is not None else now
            self._result = SlashOverlayResult(
                captured_at=captured_at,
                processed_at=now,
                status=status,
                candidates=(),
            )

    def make_web_payload(self) -> dict:
        snapshot = self.snapshot()
        sample_points, countdowns = self._snapshot_overlay_debug()
        return {
            "roi": [ROI_X1, ROI_Y1, ROI_X2, ROI_Y2],
            "action_status": self.action_status(),
            "status": snapshot.status if snapshot is not None else "idle",
            "captured_at": snapshot.captured_at if snapshot is not None else None,
            "processed_at": snapshot.processed_at if snapshot is not None else None,
            "candidates": [list(c) for c in (snapshot.candidates if snapshot is not None else ())],
            "sample_points": sample_points,
            "countdowns": countdowns,
        }


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
    tap_target_width = DEFAULT_TAP_TARGET_WIDTH
    tap_target_height = DEFAULT_TAP_TARGET_HEIGHT
    hold_sec = DEFAULT_HOLD_SEC
    color_tolerance = DEFAULT_COLOR_TOLERANCE
    bait_rearm_cooldown_sec = DEFAULT_BAIT_REARM_COOLDOWN_SEC
    bait_hit_confirm_sec = DEFAULT_BAIT_HIT_CONFIRM_SEC
    cv_min_interval_sec = DEFAULT_CV_MIN_INTERVAL_SEC
    no_slash_recover_after_sec = DEFAULT_NO_SLASH_RECOVER_AFTER_SEC
    no_slash_recover_wait_sec = DEFAULT_NO_SLASH_RECOVER_WAIT_SEC
    tool_state_color_tolerance_a = COMMON_TOOL_STATE_SETTINGS.color_tolerance_a
    tool_state_color_tolerance_b = COMMON_TOOL_STATE_SETTINGS.color_tolerance_b
    tool_state_stable_frame_count = COMMON_TOOL_STATE_SETTINGS.stable_frame_count
    tool_state_stable_color_tolerance = COMMON_TOOL_STATE_SETTINGS.stable_color_tolerance
    tool_state_stable_timeout_sec = COMMON_TOOL_STATE_SETTINGS.stable_timeout_sec
    tool_state_stable_poll_interval_sec = COMMON_TOOL_STATE_SETTINGS.stable_poll_interval_sec

    if automation_loop_interval_sec < 0:
        raise ValueError("FISH_AUTOMATION_LOOP_INTERVAL_SEC must be >= 0")
    if tap_target_width < 0 or tap_target_height < 0:
        raise ValueError("FISH_TAP_TARGET_WIDTH/HEIGHT must be >= 0")
    if (tap_target_width == 0) != (tap_target_height == 0):
        raise ValueError("FISH_TAP_TARGET_WIDTH and FISH_TAP_TARGET_HEIGHT must both be 0 or both be > 0")
    if hold_sec <= 0:
        raise ValueError("FISH_HOLD_SEC must be > 0")
    if color_tolerance < 0:
        raise ValueError("FISH_COLOR_TOLERANCE must be >= 0")
    if bait_rearm_cooldown_sec < 0:
        raise ValueError("FISH_BAIT_REARM_COOLDOWN_SEC must be >= 0")
    if bait_hit_confirm_sec <= 0:
        raise ValueError("FISH_BAIT_HIT_CONFIRM_SEC must be > 0")
    if cv_min_interval_sec < 0:
        raise ValueError("FISH_CV_MIN_INTERVAL_SEC must be >= 0")
    if no_slash_recover_after_sec < 0:
        raise ValueError("FISH_NO_SLASH_RECOVER_AFTER_SEC must be >= 0")
    if no_slash_recover_wait_sec < 0:
        raise ValueError("FISH_NO_SLASH_RECOVER_WAIT_SEC must be >= 0")
    if tool_state_color_tolerance_a < 0:
        raise ValueError("FISH_TOOL_STATE_COLOR_TOLERANCE_A must be >= 0")
    if tool_state_color_tolerance_b < 0:
        raise ValueError("FISH_TOOL_STATE_COLOR_TOLERANCE_B must be >= 0")
    if tool_state_stable_frame_count <= 0:
        raise ValueError("FISH_TOOL_STATE_STABLE_FRAME_COUNT must be > 0")
    if tool_state_stable_color_tolerance < 0:
        raise ValueError("FISH_TOOL_STATE_STABLE_COLOR_TOLERANCE must be >= 0")
    if tool_state_stable_timeout_sec <= 0:
        raise ValueError("FISH_TOOL_STATE_STABLE_TIMEOUT_SEC must be > 0")
    if tool_state_stable_poll_interval_sec <= 0:
        raise ValueError("FISH_TOOL_STATE_STABLE_POLL_INTERVAL_SEC must be > 0")

    return RuntimeConfig(
        automation_enabled=automation_enabled,
        automation_loop_interval_sec=automation_loop_interval_sec,
        tap_target_width=tap_target_width,
        tap_target_height=tap_target_height,
        hold_sec=hold_sec,
        color_tolerance=color_tolerance,
        bait_rearm_cooldown_sec=bait_rearm_cooldown_sec,
        bait_hit_confirm_sec=bait_hit_confirm_sec,
        cv_min_interval_sec=cv_min_interval_sec,
        no_slash_recover_after_sec=no_slash_recover_after_sec,
        no_slash_recover_wait_sec=no_slash_recover_wait_sec,
        tool_state_color_tolerance_a=tool_state_color_tolerance_a,
        tool_state_color_tolerance_b=tool_state_color_tolerance_b,
        tool_state_stable_frame_count=tool_state_stable_frame_count,
        tool_state_stable_color_tolerance=tool_state_stable_color_tolerance,
        tool_state_stable_timeout_sec=tool_state_stable_timeout_sec,
        tool_state_stable_poll_interval_sec=tool_state_stable_poll_interval_sec,
    )


def line_metrics(x1: int, y1: int, x2: int, y2: int) -> tuple[float, float]:
    dx = float(x2 - x1)
    dy = float(y2 - y1)
    length = math.hypot(dx, dy)
    angle = abs(math.degrees(math.atan2(dy, dx)))
    if angle > 90.0:
        angle = 180.0 - angle
    return length, angle


def sample_line_pixels(
    img: np.ndarray,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    num_samples: int = 120,
    line_half_thickness: int = 1,
) -> np.ndarray:
    h, w = img.shape[:2]
    xs = np.linspace(x1, x2, num_samples)
    ys = np.linspace(y1, y2, num_samples)

    out = []
    for xf, yf in zip(xs, ys):
        x = int(round(xf))
        y = int(round(yf))
        if 0 <= x < w and 0 <= y < h:
            for oy in range(-line_half_thickness, line_half_thickness + 1):
                yy = y + oy
                if 0 <= yy < h:
                    out.append(img[yy, x])

    if not out:
        return np.empty((0, 3), dtype=np.float32)
    return np.array(out, dtype=np.float32)


def detect_white_slash_candidates(
    img: np.ndarray,
) -> list[tuple[int, int, int, int, float, float, float, float, float, float, float, float, float]]:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    edges = cv2.Canny(gray, 40, 120)
    lines = cv2.HoughLinesP(
        edges,
        rho=1,
        theta=np.pi / 180,
        threshold=60,
        minLineLength=int(MIN_LENGTH),
        maxLineGap=22,
    )

    if lines is None:
        return []

    candidates: list[tuple[int, int, int, int, float, float, float, float, float, float, float, float, float]] = []
    for line in lines:
        x1, y1, x2, y2 = [int(v) for v in line[0]]
        length, angle = line_metrics(x1, y1, x2, y2)
        if length <= MIN_LENGTH:
            continue
        if not (MIN_ANGLE_DEG < angle < MAX_ANGLE_DEG):
            continue

        samples = sample_line_pixels(img, x1, y1, x2, y2)
        if samples.size == 0:
            continue

        mean_bgr = samples.mean(axis=0)
        white_spread = float(np.max(mean_bgr) - np.min(mean_bgr))
        white_mean = float(np.mean(mean_bgr))

        hsv_samples = cv2.cvtColor(samples.astype(np.uint8).reshape(1, -1, 3), cv2.COLOR_BGR2HSV).reshape(-1, 3)
        sat = hsv_samples[:, 1].astype(np.float32)
        val = hsv_samples[:, 2].astype(np.float32)
        # Relax slash color gates so slightly dim/less-neutral white strokes can pass.
        white_ratio = float(np.mean((sat <= 110) & (val >= 135)))
        bright_ratio = float(np.mean(val >= 165))
        sat_mean = float(np.mean(sat))
        val_mean = float(np.mean(val))

        line_score = (
            white_ratio * 2.8
            + bright_ratio * 1.4
            + val_mean / 255.0 * 0.5
            - sat_mean / 255.0 * 0.4
            + length / 400.0
        )

        candidates.append(
            (
                x1,
                y1,
                x2,
                y2,
                length,
                angle,
                white_mean,
                white_spread,
                white_ratio,
                bright_ratio,
                sat_mean,
                val_mean,
                line_score,
            )
        )

    filtered = [
        c
        for c in candidates
        if c[8] >= 0.18
        and c[9] >= 0.20
        and c[11] >= 135.0
        and c[5] <= 35.0
        and ((c[1] + c[3]) * 0.5) >= 40.0
    ]
    filtered.sort(key=lambda c: (c[12], c[8], c[9], c[4]), reverse=True)
    return filtered


def clamp_roi_bounds(width: int, height: int) -> tuple[int, int, int, int] | None:
    x1 = max(0, min(ROI_X1, width - 1))
    y1 = max(0, min(ROI_Y1, height - 1))
    x2 = max(0, min(ROI_X2, width - 1))
    y2 = max(0, min(ROI_Y2, height - 1))
    if x2 < x1 or y2 < y1:
        return None
    return x1, y1, x2, y2


class JpegPayloadProvider:
    def __init__(self, manager: FrameStreamManager) -> None:
        self._manager = manager
        self._jpeg_opts = [int(cv2.IMWRITE_JPEG_QUALITY), COMMON_SETTINGS.jpeg_quality]

        self._lock = threading.Lock()
        self._last_ts = -1.0
        self._last_payload: bytes | None = None

    def __call__(self) -> bytes | None:
        snapshot = self._manager.snapshot()
        if snapshot is None:
            return None

        captured_at, frame = snapshot

        with self._lock:
            if captured_at == self._last_ts and self._last_payload is not None:
                return self._last_payload

            ok, encoded = cv2.imencode(".jpg", frame, self._jpeg_opts)
            if not ok:
                return None

            self._last_ts = captured_at
            self._last_payload = encoded.tobytes()
            return self._last_payload


class FishAutomationRunner:
    def __init__(
        self,
        manager: FrameStreamManager,
        config: RuntimeConfig,
        shutdown_event: threading.Event,
        overlay_state: SlashOverlayState,
    ) -> None:
        self._manager = manager
        self._config = config
        self._shutdown_event = shutdown_event
        self._overlay_state = overlay_state
        self._loop_lock = threading.Lock()
        self._loop_scheduler = NewThreadScheduler()
        self._loop_subscription: Disposable | None = None
        self._tap_target_w, self._tap_target_h = resolve_tap_target_size(
            COMMON_SETTINGS.capture_width,
            COMMON_SETTINGS.capture_height,
            override_width=self._config.tap_target_width,
            override_height=self._config.tap_target_height,
            log_prefix="fish_automation tap_map",
        )
        self._latest_frame_ts = -1.0

        self._cv_worker = AsyncBusyWorker(self._run_cv_detection, name="fish-cv")
        self._cv_lock = threading.Lock()
        self._cv_status = "idle"
        self._cv_result_seq = 0
        self._cv_last_decision: FishDecision | None = None
        self._last_cv_submit_at = 0.0
        self._bait_last_triggered_at = -1e9
        self._bait_last_logged_match: bool | None = None
        self._bait_hit_started_at: float | None = None
        self._bait_pending_log_bucket: int | None = None
        now = time.monotonic()
        self._last_slash_detected_at = now
        self._last_no_slash_recover_at = -1e9
        self._has_seen_slash = False
        self._no_slash_excluded_sec = 0.0

        # During 3-second hold action, CV submissions are intentionally skipped.
        self._action_worker = AsyncSequencedWorker(self._run_action_cycle, cooldown_sec=0.0, name="fish-action")

    def start(self) -> None:
        if not self._config.automation_enabled:
            print("fish_automation status=disabled")
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
            "fish_automation status=enabled "
            f"interval={self._config.automation_loop_interval_sec}s hold={self._config.hold_sec}s "
            f"tol_a={self._config.tool_state_color_tolerance_a} "
            f"tol_b={self._config.tool_state_color_tolerance_b}"
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
        print(f"fish_automation loop_error type={type(exc).__name__} msg={exc}")
        self._clear_loop_subscription()

    def _on_loop_completed(self) -> None:
        self._clear_loop_subscription()

    def _on_loop_tick(self) -> None:
        if self._shutdown_event.is_set():
            return
        if self._is_action_busy():
            return

        snapshot = self._manager.snapshot()
        if snapshot is not None:
            captured_at, frame = snapshot
            if captured_at != self._latest_frame_ts:
                now = time.monotonic()
                if (now - self._last_cv_submit_at) >= self._config.cv_min_interval_sec:
                    self._submit_cv_if_idle(captured_at, frame)
                    self._last_cv_submit_at = now

        decision, cv_result_seq = self._snapshot_cv_result()
        if decision is not None:
            self._submit_action_if_allowed(decision, cv_result_seq)

    def _is_action_busy(self) -> bool:
        busy, _skip_busy_count, _skip_cooldown_count = self._action_worker.snapshot()
        return busy

    def _hold_press(self, frame_x: int, frame_y: int, hold_sec: float) -> bool:
        hold_ms = max(1, int(round(hold_sec * 1000.0)))
        ok, tap_x, tap_y, used_x, used_y = adb_swipe_frame_point_with_random_drift(
            frame_x,
            frame_y,
            COMMON_SETTINGS.capture_width,
            COMMON_SETTINGS.capture_height,
            self._tap_target_w,
            self._tap_target_h,
            duration_ms=hold_ms,
            max_drift_px=1,
        )
        print(
            f"fish_automation hold frame=({frame_x},{frame_y}) mapped=({tap_x},{tap_y}) "
            f"tap=({used_x},{used_y}) press_ms={hold_ms} ok={int(ok)}"
        )
        return ok

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
            f"fish_automation tap frame=({frame_x},{frame_y}) mapped=({tap_x},{tap_y}) "
            f"tap=({used_x},{used_y}) drift=({used_x - tap_x},{used_y - tap_y}) ok={int(ok)}"
        )
        return ok

    def _input_text(self, text: str) -> bool:
        ok = adb_shell_ok(["input", "text", text])
        print(f"fish_automation input_text={text} ok={int(ok)}")
        return ok

    def _wait(self, seconds: float) -> bool:
        return wait_with_random_jitter(seconds, shutdown_event=self._shutdown_event, max_extra_sec=0.2)

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
        print(f"fish_automation tool_pack_open refreshed={int(refreshed)}")
        return True

    def _close_tool_pack_after_tool_state_check(self) -> bool:
        if self._shutdown_event.is_set():
            return False
        closed = self._tap(590, 328)
        print(f"fish_automation tool_pack_close ok={int(closed)}")
        return closed

    def _equip_tool_from_tool_pack(self) -> bool:
        if self._shutdown_event.is_set():
            return False
        equipped = self._tap(477, 243)
        print(f"fish_automation tool_equip ok={int(equipped)}")
        return equipped

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
            "fish_automation tool_state_unstable "
            f"stable_hits={hits} required={required} timeout={timeout_sec:.2f}s"
        )
        return (*latest_payload, hits)

    def _evaluate_tool_state_from_latest_frame(self) -> tuple[bool, bool] | None:
        stable = self._wait_for_stable_tool_state_frame(
            stable_count=self._config.tool_state_stable_frame_count,
            color_tolerance=self._config.tool_state_stable_color_tolerance,
            timeout_sec=self._config.tool_state_stable_timeout_sec,
            poll_interval_sec=self._config.tool_state_stable_poll_interval_sec,
        )
        if stable is None:
            return None
        _frame, ax, ay, bx, by, color_a, color_b, stable_hits = stable

        target_a = hex_to_rgb("FFFDFF")
        target_b = hex_to_rgb("C7B8B1")
        has_equipped_tool_signal = is_color_close(color_a, target_a, self._config.tool_state_color_tolerance_a)
        has_tool_worn_signal = is_color_close(color_b, target_b, self._config.tool_state_color_tolerance_b)
        print(
            "fish_automation tool_state "
            f"A@({ax},{ay})={color_a} equipped_signal={int(has_equipped_tool_signal)} "
            f"B@({bx},{by})={color_b} worn_signal={int(has_tool_worn_signal)} "
            f"stable_frames={stable_hits}"
        )
        return has_equipped_tool_signal, has_tool_worn_signal

    def _is_bait_color_hit(self, frame: np.ndarray) -> bool:
        sampled = sample_rgb_from_frame(frame, BAIT_SAMPLE_X, BAIT_SAMPLE_Y)
        target = hex_to_rgb(BAIT_TARGET_HEX)
        if sampled is None:
            print(f"fish_automation bait_color sample_unavailable at=({BAIT_SAMPLE_X},{BAIT_SAMPLE_Y})")
            self._overlay_state.set_sample_points(
                [
                    {
                        "x": BAIT_SAMPLE_X,
                        "y": BAIT_SAMPLE_Y,
                        "label": "bait sample: N/A",
                        "color": "rgb(255,80,80)",
                    }
                ]
            )
            return False
        matched = is_color_close(sampled, target, self._config.color_tolerance)
        self._overlay_state.set_sample_points(
            [
                {
                    "x": BAIT_SAMPLE_X,
                    "y": BAIT_SAMPLE_Y,
                    "label": (
                        f"bait sample={sampled} target={target} "
                        f"tol={self._config.color_tolerance} match={int(matched)}"
                    ),
                    "color": "rgb(0,255,160)" if matched else "rgb(255,120,80)",
                }
            ]
        )
        if self._bait_last_logged_match is None or self._bait_last_logged_match != matched:
            print(
                "fish_automation bait_color "
                f"at=({BAIT_SAMPLE_X},{BAIT_SAMPLE_Y}) sampled={sampled} target={target} "
                f"tol={self._config.color_tolerance} match={int(matched)}"
            )
            self._bait_last_logged_match = matched
        return matched

    def _update_bait_confirm_state(self, raw_hit: bool) -> tuple[bool, float]:
        if not raw_hit:
            self._bait_hit_started_at = None
            self._bait_pending_log_bucket = None
            return False, 0.0

        now = time.monotonic()
        if self._bait_hit_started_at is None:
            self._bait_hit_started_at = now

        hit_for_sec = max(0.0, now - self._bait_hit_started_at)
        confirmed = hit_for_sec >= self._config.bait_hit_confirm_sec
        return confirmed, hit_for_sec

    def _is_stamina_line_in_frame(
        self,
        frame: np.ndarray,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        min_ratio: float,
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
                [DEFAULT_STAMINA_LINE_H1_MIN, DEFAULT_STAMINA_LINE_MIN_S, DEFAULT_STAMINA_LINE_MIN_V],
                dtype=np.uint8,
            )
            upper1 = np.array([DEFAULT_STAMINA_LINE_H1_MAX, 255, 255], dtype=np.uint8)
            lower2 = np.array(
                [DEFAULT_STAMINA_LINE_H2_MIN, DEFAULT_STAMINA_LINE_MIN_S, DEFAULT_STAMINA_LINE_MIN_V],
                dtype=np.uint8,
            )
            upper2 = np.array([DEFAULT_STAMINA_LINE_H2_MAX, 255, 255], dtype=np.uint8)

            mask1 = cv2.inRange(line_hsv, lower1, upper1)
            mask2 = cv2.inRange(line_hsv, lower2, upper2)
            mask = cv2.bitwise_or(mask1, mask2)
            stamina_ratio = float(np.count_nonzero(mask)) / float(mask.size)
            if stamina_ratio >= best_ratio:
                best_ratio = stamina_ratio

        return best_ratio >= min_ratio

    def _has_top_stamina_warning_line_in_frame(self, frame: np.ndarray, min_ratio: float) -> bool:
        h, w = frame.shape[:2]
        if h <= 0 or w <= 0:
            return False

        scale_x = w / float(STAMINA_LINE_REF_WIDTH)
        scale_y = h / float(STAMINA_LINE_REF_HEIGHT)
        target_y = int(round(STAMINA_LINE_REF_Y * scale_y))
        expected_x1 = int(round(STAMINA_LINE_REF_X1 * scale_x))
        expected_x2 = int(round(STAMINA_LINE_REF_X2 * scale_x))

        margin_px = max(2, int(round(6 * scale_x)))
        scan_x1 = max(0, expected_x1 - margin_px)
        scan_x2 = min(w - 1, expected_x2 + margin_px)

        return self._is_stamina_line_in_frame(frame, scan_x1, target_y, scan_x2, target_y, min_ratio=min_ratio)

    def _should_recover_stamina_from_frame(self, frame: np.ndarray) -> bool:
        sample_count = 5
        sample_interval_sec = 0.12
        stamina_warning_hits = 0

        for idx in range(sample_count):
            if self._has_top_stamina_warning_line_in_frame(frame, min_ratio=DEFAULT_STAMINA_LINE_MIN_RATIO):
                stamina_warning_hits += 1
            if idx < sample_count - 1 and self._shutdown_event.wait(timeout=sample_interval_sec):
                return True

        return stamina_warning_hits > 0

    def _run_cv_detection(self, captured_at: float, frame: np.ndarray) -> None:
        started = time.monotonic()
        status = "ok"
        decision: FishDecision | None = None
        candidates: list[tuple[int, int, int, int, float, float, float, float, float, float, float, float, float]] = []

        h, w = frame.shape[:2]
        if h <= 0 or w <= 0:
            status = "invalid_frame"
        else:
            try:
                should_recover_stamina = self._should_recover_stamina_from_frame(frame)
                if should_recover_stamina:
                    decision = FishDecision(should_hold=False, use_bait=False, recover_stamina=True)
                    self._overlay_state.clear_candidates("slash_paused_stamina")
                    print("fish_automation cv stamina_recover=1 bait=skipped slash_check=skipped")
                else:
                    raw_bait_hit = self._is_bait_color_hit(frame)
                    bait_confirmed, hit_for_sec = self._update_bait_confirm_state(raw_bait_hit)
                    now = time.monotonic()
                    since_last_trigger_sec = now - self._bait_last_triggered_at
                    cooldown_left = max(0.0, self._config.bait_rearm_cooldown_sec - since_last_trigger_sec)
                    can_trigger_bait = bait_confirmed and cooldown_left <= 0.0

                    if can_trigger_bait:
                        decision = FishDecision(should_hold=False, use_bait=True)
                        self._bait_last_triggered_at = now
                        self._overlay_state.clear_candidates("slash_paused_bait")
                        self._overlay_state.set_countdowns(
                            [
                                {
                                    "x": BAIT_SAMPLE_X,
                                    "y": BAIT_SAMPLE_Y,
                                    "label": "bait trigger",
                                    "remaining_sec": 0.0,
                                    "color": "rgb(0,255,160)",
                                }
                            ]
                        )
                        print(
                            "fish_automation cv "
                            f"stamina_recover=0 bait=1 hit_for={hit_for_sec:.1f}s "
                            f"confirm={self._config.bait_hit_confirm_sec:.1f}s slash_check=skipped"
                        )
                    else:
                        confirm_remaining = 0.0
                        if raw_bait_hit:
                            confirm_remaining = max(0.0, self._config.bait_hit_confirm_sec - hit_for_sec)
                        self._overlay_state.set_countdowns(
                            [
                                {
                                    "x": BAIT_SAMPLE_X,
                                    "y": BAIT_SAMPLE_Y,
                                    "label": "bait confirm",
                                    "remaining_sec": confirm_remaining,
                                    "color": "rgb(255,180,0)",
                                }
                            ]
                        )
                        if bait_confirmed:
                            print(
                                "fish_automation cv "
                                f"stamina_recover=0 bait=1 hit_for={hit_for_sec:.1f}s "
                                f"confirm={self._config.bait_hit_confirm_sec:.1f}s "
                                f"trigger=blocked cooldown_left={cooldown_left:.1f}s slash_check=enabled"
                            )
                        elif raw_bait_hit:
                            bucket = int(hit_for_sec)
                            if self._bait_pending_log_bucket != bucket:
                                remain = max(0.0, self._config.bait_hit_confirm_sec - hit_for_sec)
                                print(
                                    "fish_automation cv "
                                    f"stamina_recover=0 bait=0 confirm=pending hit_for={hit_for_sec:.1f}s "
                                    f"remain={remain:.1f}s"
                                )
                                self._bait_pending_log_bucket = bucket

                        roi = clamp_roi_bounds(w, h)
                        if roi is None:
                            status = "invalid_roi"
                        else:
                            rx1, ry1, rx2, ry2 = roi
                            roi_frame = frame[ry1 : ry2 + 1, rx1 : rx2 + 1]
                            local_candidates = detect_white_slash_candidates(roi_frame)
                            candidates = [
                                (
                                    c[0] + rx1,
                                    c[1] + ry1,
                                    c[2] + rx1,
                                    c[3] + ry1,
                                    c[4],
                                    c[5],
                                    c[6],
                                    c[7],
                                    c[8],
                                    c[9],
                                    c[10],
                                    c[11],
                                    c[12],
                                )
                                for c in local_candidates
                            ]
                        should_hold = len(candidates) > 0
                        decision = FishDecision(should_hold=should_hold, use_bait=False)
                        if candidates:
                            top = candidates[0]
                            print(
                                "fish_automation cv "
                                f"stamina_recover=0 bait=0 slash=1 roi=({ROI_X1},{ROI_Y1})-({ROI_X2},{ROI_Y2}) "
                                f"len={top[4]:.1f} angle={top[5]:.1f} "
                                f"wr={top[8]:.2f} br={top[9]:.2f} score={top[12]:.2f}"
                            )
                        else:
                            print(
                                "fish_automation cv "
                                f"stamina_recover=0 bait=0 slash=0 roi=({ROI_X1},{ROI_Y1})-({ROI_X2},{ROI_Y2})"
                            )

            except Exception as exc:
                status = f"error:{type(exc).__name__}"
                print(f"fish_automation cv_error type={type(exc).__name__} msg={exc}")
                candidates = []

        self._overlay_state.update(captured_at, status, candidates if h > 0 and w > 0 else [])

        latency_ms = (time.monotonic() - started) * 1000.0
        with self._cv_lock:
            self._cv_status = status
            self._cv_result_seq += 1
            self._cv_last_decision = decision
        print(f"fish_automation cv_done status={status} ms={latency_ms:.1f}")

    def _submit_cv_if_idle(self, captured_at: float, frame: np.ndarray) -> None:
        if not self._cv_worker.submit(captured_at, frame):
            return

        with self._cv_lock:
            self._cv_status = "running"

        self._latest_frame_ts = captured_at

    def _snapshot_cv_result(self) -> tuple[FishDecision | None, int]:
        with self._cv_lock:
            return self._cv_last_decision, self._cv_result_seq

    def _run_action_cycle(self, decision: FishDecision) -> None:
        if not decision.use_bait and not decision.should_hold and not decision.recover_no_slash and not decision.recover_stamina:
            return

        status = "ok"
        action_started_at = time.monotonic()
        try:
            if decision.recover_stamina:
                self._overlay_state.clear_candidates("slash_paused_stamina")
                self._overlay_state.set_action_status("CV PAUSED: STAMINA RECOVERY")
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
            elif decision.use_bait:
                self._overlay_state.set_action_status("CV PAUSED: BAIT PRECHECK TOOL")
                if not self._open_tool_pack_before_tool_state_check():
                    status = "interrupted"
                else:
                    tool_state = self._evaluate_tool_state_from_latest_frame()
                    if tool_state is None:
                        self._close_tool_pack_after_tool_state_check()
                        needs_repair = True
                        print("fish_automation flow bait_precheck repair=1 reason=tool_state_unavailable")
                    else:
                        has_equipped_tool_signal, has_tool_worn_signal = tool_state

                        if not has_equipped_tool_signal:
                            print("fish_automation flow bait_precheck equip_tool=1 close_mode=auto")
                            equipped = self._equip_tool_from_tool_pack()
                            if not equipped:
                                needs_repair = True
                                print("fish_automation flow bait_precheck repair=1 reason=equip_failed")
                            else:
                                snapshot = self._manager.snapshot()
                                previous_ts = snapshot[0] if snapshot is not None else -1.0
                                if not self._shutdown_event.wait(timeout=1.0):
                                    self._wait_for_updated_frame(previous_ts, timeout_sec=1.5)

                                post_equip_tool_state = self._evaluate_tool_state_from_latest_frame()
                                if post_equip_tool_state is None:
                                    needs_repair = True
                                    print(
                                        "fish_automation flow bait_precheck "
                                        "repair=1 reason=post_equip_tool_state_unavailable"
                                    )
                                else:
                                    _post_equipped_signal, post_worn_signal = post_equip_tool_state
                                    needs_repair = bool(post_worn_signal)
                                    print(
                                        "fish_automation flow bait_precheck "
                                        f"post_equip_worn_signal={int(post_worn_signal)}"
                                    )
                        else:
                            print("fish_automation flow bait_precheck equip_tool=0 close_mode=manual")
                            self._close_tool_pack_after_tool_state_check()
                            needs_repair = bool(has_tool_worn_signal)

                        print(f"fish_automation flow bait_precheck repair={int(needs_repair)}")

                    if status == "ok" and needs_repair:
                        self._overlay_state.set_action_status("CV PAUSED: BAIT PRECHECK REPAIR")
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

                if status != "ok":
                    return

                self._overlay_state.clear_candidates("slash_paused_bait")
                self._overlay_state.set_action_status("CV PAUSED: BAIT FLOW")
                interrupted = run_action_steps(
                    use_item("bait", final_wait_sec=1.0),
                    publish_active_step=self._noop_publish_active_step,
                    tap=self._tap,
                    input_text=self._input_text,
                    wait=self._wait,
                    start_tap_order=1,
                )
                if interrupted:
                    status = "interrupted"
                else:
                    self._overlay_state.set_action_status("CV PAUSED: WAIT 1.0s")
                    self._overlay_state.set_countdowns(
                        [
                            {
                                "x": BAIT_SAMPLE_X,
                                "y": BAIT_SAMPLE_Y,
                                "label": "bait wait",
                                "until_at": time.monotonic() + 1.0,
                                "color": "rgb(255,220,0)",
                            }
                        ]
                    )
                if status == "ok" and self._wait(1.0):
                    status = "interrupted"
                if status == "ok":
                    self._overlay_state.set_action_status("CV PAUSED: TAP (550,273)")
                    self._tap(HOLD_FRAME_X, HOLD_FRAME_Y)
                    self._overlay_state.set_action_status("CV PAUSED: WAIT 4.0s")
                    self._overlay_state.set_countdowns(
                        [
                            {
                                "x": HOLD_FRAME_X,
                                "y": HOLD_FRAME_Y,
                                "label": "post-bait wait",
                                "until_at": time.monotonic() + 4.0,
                                "color": "rgb(255,220,0)",
                            }
                        ]
                    )
                    if self._wait(4.0):
                        status = "interrupted"
            elif decision.recover_no_slash:
                self._overlay_state.clear_candidates("slash_paused_fallback")
                self._overlay_state.set_action_status("CV PAUSED: NO SLASH >10s HOLD (550,273) 0.5s")
                self._hold_press(HOLD_FRAME_X, HOLD_FRAME_Y, 0.5)
                if self._config.no_slash_recover_wait_sec > 0:
                    self._overlay_state.set_action_status(
                        f"CV PAUSED: WAIT {self._config.no_slash_recover_wait_sec:.1f}s"
                    )
                    self._overlay_state.set_countdowns(
                        [
                            {
                                "x": FALLBACK_LABEL_X,
                                "y": FALLBACK_LABEL_Y,
                                "label": "fallback wait",
                                "until_at": time.monotonic() + self._config.no_slash_recover_wait_sec,
                                "color": "rgb(255,120,80)",
                            }
                        ]
                    )
                    if self._wait(self._config.no_slash_recover_wait_sec):
                        status = "interrupted"
            else:
                self._overlay_state.set_action_status(
                    f"CV PAUSED: HOLD ({HOLD_FRAME_X},{HOLD_FRAME_Y}) {self._config.hold_sec:.1f}s"
                )
                ok = self._hold_press(HOLD_FRAME_X, HOLD_FRAME_Y, self._config.hold_sec)
                if not ok:
                    status = "hold_failed"
        except Exception as exc:
            status = f"error:{type(exc).__name__}"
            print(f"fish_automation action_error type={type(exc).__name__} msg={exc}")
        finally:
            paused_elapsed = max(0.0, time.monotonic() - action_started_at)
            self._no_slash_excluded_sec += paused_elapsed
            mode = (
                "stamina"
                if decision.recover_stamina
                else "bait"
                if decision.use_bait
                else "recover"
                if decision.recover_no_slash
                else "hold"
            )
            print(
                "fish_automation no_slash_timer_exclude "
                f"reason={mode} elapsed={paused_elapsed:.1f}s total_excluded={self._no_slash_excluded_sec:.1f}s"
            )
            if decision.use_bait:
                # Restart no-slash fallback timer only after bait flow (including final 4s wait) completes.
                self._last_slash_detected_at = time.monotonic()
                self._last_no_slash_recover_at = -1e9
                self._no_slash_excluded_sec = 0.0
                print(
                    "fish_automation no_slash_timer_reset "
                    "reason=bait_flow_completed baseline=now"
                )
            self._overlay_state.set_action_status(None)
            self._overlay_state.set_countdowns([])
            print(f"fish_automation action_done mode={mode} status={status}")

    def _submit_action_if_allowed(self, decision: FishDecision, cv_result_seq: int) -> None:
        now = time.monotonic()

        if decision.should_hold:
            self._has_seen_slash = True
            self._last_slash_detected_at = now
            self._last_no_slash_recover_at = -1e9
            self._no_slash_excluded_sec = 0.0
            self._overlay_state.set_countdowns([])

        use_decision = decision
        if not decision.use_bait and not decision.should_hold and not decision.recover_stamina:
            no_slash_for = max(0.0, now - self._last_slash_detected_at - self._no_slash_excluded_sec)
            recover_ready = no_slash_for >= self._config.no_slash_recover_after_sec
            recover_cooldown_ok = (
                now - self._last_no_slash_recover_at
            ) >= self._config.no_slash_recover_after_sec
            recover_remaining = max(0.0, self._config.no_slash_recover_after_sec - no_slash_for)
            self._overlay_state.set_countdowns(
                [
                    {
                        "x": FALLBACK_LABEL_X,
                        "y": FALLBACK_LABEL_Y,
                        "label": "fallback(no slash)",
                        "remaining_sec": recover_remaining,
                        "color": "rgb(255,180,0)",
                    }
                ]
            )
            if not (recover_ready and recover_cooldown_ok):
                return
            use_decision = FishDecision(should_hold=False, use_bait=False, recover_no_slash=True)

        allowed, _reason, _cooldown_left_ms = self._action_worker.submit(cv_result_seq, use_decision)
        if not allowed:
            return

        if use_decision.recover_no_slash:
            self._overlay_state.clear_candidates("slash_paused_fallback")
            self._last_no_slash_recover_at = now
            no_slash_for = max(0.0, now - self._last_slash_detected_at - self._no_slash_excluded_sec)
            self._overlay_state.set_countdowns(
                [
                    {
                        "x": FALLBACK_LABEL_X,
                        "y": FALLBACK_LABEL_Y,
                        "label": "fallback start",
                        "remaining_sec": 0.0,
                        "color": "rgb(255,80,80)",
                    }
                ]
            )
            print(
                "fish_automation no_slash_recover "
                f"no_slash_for={no_slash_for:.1f}s "
                f"hold=({HOLD_FRAME_X},{HOLD_FRAME_Y}) hold_sec=0.5 "
                f"wait={self._config.no_slash_recover_wait_sec:.1f}s"
            )


def main() -> None:
    install_signal_handlers()
    config = load_config()

    stream_manager = make_common_stream_manager(shutdown_event)
    stream_manager.start()

    waiting_payload = make_common_waiting_payload()
    overlay_state = SlashOverlayState()
    payload_provider = JpegPayloadProvider(stream_manager)
    automation_runner = FishAutomationRunner(
        stream_manager,
        config,
        shutdown_event,
        overlay_state,
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
