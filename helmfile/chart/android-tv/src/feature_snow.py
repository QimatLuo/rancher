#!/usr/bin/env python3
import os
import random
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import reactivex as rx
from reactivex import operators as ops
from reactivex.disposable import Disposable
from reactivex.scheduler import NewThreadScheduler

from common_action import RED_LINE_ACTION_STEPS as STAMINA_RECOVERY_ACTION_STEPS
from common_action import run_action_steps, triangles_to_tap_points
from common_adb import (
    adb_tap_frame_point_with_random_drift,
    adb_shell_ok,
    resolve_tap_target_size,
    wait_with_random_jitter,
)
from common_feature_runtime import AsyncSequencedWorker
from common_feature_settings import (
    get_common_runtime_settings,
    make_common_startup_log,
    make_common_stream_manager,
    make_common_waiting_payload,
    make_common_web_server,
)
from common_stream import FrameStreamManager
from common_cv import (
    detect_yellow_inverted_triangle_tuples,
    scale_point_from_reference,
)


shutdown_event = threading.Event()
COMMON_SETTINGS = get_common_runtime_settings()

# ROI rectangles defined as (x1, y1, x2, y2) in 640x360 reference coordinates.
STREAM_OVERLAY_RECTS_REF: tuple[tuple[int, int, int, int], ...] = (
    (40, 85, 205, 297),
    (435, 85, 600, 297),
)
DEFAULT_TAP_ENABLED = True
DEFAULT_TAP_TARGET_WIDTH = 0
DEFAULT_TAP_TARGET_HEIGHT = 0
DEFAULT_CIRCLE_POST_WAIT_MIN_SEC = 0.00
DEFAULT_CIRCLE_POST_WAIT_MAX_SEC = 0.10
DEFAULT_CIRCLE_BATCH_POST_WAIT_SEC = 1.0
TRIANGLE_SUPPRESS_AFTER_CIRCLE_SEC = 20.0
STAMINA_LINE_REF_WIDTH = 640
STAMINA_LINE_REF_HEIGHT = 360
STAMINA_LINE_REF_Y = 38
STAMINA_LINE_REF_X1 = 110
STAMINA_LINE_REF_X2 = 170
DEFAULT_STAMINA_LINE_MIN_RATIO = 0.65
DEFAULT_STAMINA_LINE_H1_MIN = 0
DEFAULT_STAMINA_LINE_H1_MAX = 10
DEFAULT_STAMINA_LINE_H2_MIN = 170
DEFAULT_STAMINA_LINE_H2_MAX = 179
DEFAULT_STAMINA_LINE_MIN_S = 120
DEFAULT_STAMINA_LINE_MIN_V = 120
SNOW_TEMPLATE_PATH = Path("/sample_click/snow.jpg")
SNOW_TEMPLATE_MATCH_THRESHOLD = float(os.environ.get("SNOW_TEMPLATE_MATCH_THRESHOLD", "0.70"))
SNOW_TEMPLATE_MAX_DETECTIONS = int(os.environ.get("SNOW_TEMPLATE_MAX_DETECTIONS", "20"))

_snow_template_gray: np.ndarray | None = None
_snow_template_size: tuple[int, int] | None = None


@dataclass(frozen=True)
class Circle:
    x: int
    y: int
    r: int


def _dedupe_circles(circles: list[Circle], min_center_dist: float = 6.0) -> list[Circle]:
    if not circles:
        return []

    circles = sorted(circles, key=lambda c: c.r)
    picked: list[Circle] = []
    for c in circles:
        keep = True
        for p in picked:
            if np.hypot(c.x - p.x, c.y - p.y) < min_center_dist:
                keep = False
                break
        if keep:
            picked.append(c)
    return picked


def _load_snow_template() -> tuple[np.ndarray, tuple[int, int]]:
    global _snow_template_gray, _snow_template_size
    if _snow_template_gray is not None and _snow_template_size is not None:
        return _snow_template_gray, _snow_template_size

    tpl = cv2.imread(str(SNOW_TEMPLATE_PATH), cv2.IMREAD_GRAYSCALE)
    if tpl is None:
        raise RuntimeError(f"snow template not found/readable: {SNOW_TEMPLATE_PATH}")

    h, w = tpl.shape[:2]
    if h < 2 or w < 2:
        raise RuntimeError(f"snow template too small: {w}x{h}")

    _snow_template_gray = tpl
    _snow_template_size = (w, h)
    print(
        f"snow_template path={SNOW_TEMPLATE_PATH} size={w}x{h} threshold={SNOW_TEMPLATE_MATCH_THRESHOLD:.2f}"
    )
    return _snow_template_gray, _snow_template_size


def detect_ring_circles(
    frame_bgr: np.ndarray,
    *,
    min_radius: int = 9,
    max_radius: int = 12,
    target_radius: int = 12,
    radius_tolerance: int = 2,
    refine_search_px: int = 3,
) -> list[Circle]:
    del min_radius, max_radius, target_radius, radius_tolerance, refine_search_px

    tpl, (tpl_w, tpl_h) = _load_snow_template()
    frame_h, frame_w = frame_bgr.shape[:2]
    if frame_h < tpl_h or frame_w < tpl_w:
        return []

    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    score_map = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
    work = score_map.copy()

    radius = max(1, int(round((tpl_w + tpl_h) / 4.0)))
    selected: list[Circle] = []
    for _ in range(SNOW_TEMPLATE_MAX_DETECTIONS):
        _min_v, max_v, _min_loc, max_loc = cv2.minMaxLoc(work)
        if float(max_v) < SNOW_TEMPLATE_MATCH_THRESHOLD:
            break

        x1 = int(max_loc[0])
        y1 = int(max_loc[1])
        selected.append(Circle(x=x1 + tpl_w // 2, y=y1 + tpl_h // 2, r=radius))

        sx1 = max(0, x1 - tpl_w // 2)
        sy1 = max(0, y1 - tpl_h // 2)
        sx2 = min(work.shape[1], x1 + tpl_w // 2 + 1)
        sy2 = min(work.shape[0], y1 + tpl_h // 2 + 1)
        work[sy1:sy2, sx1:sx2] = -1.0

    selected = _dedupe_circles(selected, min_center_dist=max(6.0, radius * 0.6))
    selected.sort(key=lambda c: (c.y, c.x))
    return selected


def detect_ring_circle_tuples(
    frame_bgr: np.ndarray,
    *,
    min_radius: int = 9,
    max_radius: int = 12,
    target_radius: int = 12,
    radius_tolerance: int = 2,
    refine_search_px: int = 3,
) -> list[tuple[int, int, int]]:
    circles = detect_ring_circles(
        frame_bgr,
        min_radius=min_radius,
        max_radius=max_radius,
        target_radius=target_radius,
        radius_tolerance=radius_tolerance,
        refine_search_px=refine_search_px,
    )
    return [(c.x, c.y, c.r) for c in circles]


def scale_overlay_rectangles(frame_w: int, frame_h: int) -> list[tuple[int, int, int, int]]:
    ref_w, ref_h = 640.0, 360.0
    sx = frame_w / ref_w
    sy = frame_h / ref_h

    rects: list[tuple[int, int, int, int]] = []
    for x1_ref, y1_ref, x2_ref, y2_ref in STREAM_OVERLAY_RECTS_REF:
        x1 = max(0, min(frame_w - 1, int(round(x1_ref * sx))))
        y1 = max(0, min(frame_h - 1, int(round(y1_ref * sy))))
        x2 = max(x1, min(frame_w - 1, int(round(x2_ref * sx))))
        y2 = max(y1, min(frame_h - 1, int(round(y2_ref * sy))))
        rects.append((x1, y1, x2, y2))
    return rects


def build_triangle_points(x: int, y: int, base_len: int) -> list[tuple[int, int]]:
    half = max(2, int(round(base_len / 2.0)))
    tip = max(3, int(round(base_len * 0.65)))
    return [
        (x - half, y - max(1, tip // 2)),
        (x + half, y - max(1, tip // 2)),
        (x, y + tip),
    ]


@dataclass
class RuntimeConfig:
    tap_enabled: bool
    tap_target_width: int
    tap_target_height: int
    autonomous_fps: float
    stamina_line_min_ratio: float
    stamina_line_h1_min: int
    stamina_line_h1_max: int
    stamina_line_h2_min: int
    stamina_line_h2_max: int
    stamina_line_min_s: int
    stamina_line_min_v: int


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
    tap_enabled = DEFAULT_TAP_ENABLED
    tap_target_width = DEFAULT_TAP_TARGET_WIDTH
    tap_target_height = DEFAULT_TAP_TARGET_HEIGHT
    autonomous_fps = COMMON_SETTINGS.web_fps
    stamina_line_min_ratio = DEFAULT_STAMINA_LINE_MIN_RATIO
    stamina_line_h1_min = DEFAULT_STAMINA_LINE_H1_MIN
    stamina_line_h1_max = DEFAULT_STAMINA_LINE_H1_MAX
    stamina_line_h2_min = DEFAULT_STAMINA_LINE_H2_MIN
    stamina_line_h2_max = DEFAULT_STAMINA_LINE_H2_MAX
    stamina_line_min_s = DEFAULT_STAMINA_LINE_MIN_S
    stamina_line_min_v = DEFAULT_STAMINA_LINE_MIN_V
    if tap_target_width < 0 or tap_target_height < 0:
        raise ValueError("HARVEST_CV_TAP_TARGET_WIDTH/HEIGHT must be >= 0")
    if (tap_target_width == 0) != (tap_target_height == 0):
        raise ValueError("HARVEST_CV_TAP_TARGET_WIDTH and HARVEST_CV_TAP_TARGET_HEIGHT must both be 0 or both be > 0")
    if autonomous_fps <= 0:
        raise ValueError("HARVEST_AUTONOMOUS_FPS must be > 0")
    if stamina_line_min_ratio < 0 or stamina_line_min_ratio > 1:
        raise ValueError("SNOW_RED_LINE_MIN_RATIO must be within 0..1")
    if stamina_line_h1_min < 0 or stamina_line_h1_min > 179:
        raise ValueError("SNOW_RED_LINE_H1_MIN must be within 0..179")
    if stamina_line_h1_max < 0 or stamina_line_h1_max > 179:
        raise ValueError("SNOW_RED_LINE_H1_MAX must be within 0..179")
    if stamina_line_h2_min < 0 or stamina_line_h2_min > 179:
        raise ValueError("SNOW_RED_LINE_H2_MIN must be within 0..179")
    if stamina_line_h2_max < 0 or stamina_line_h2_max > 179:
        raise ValueError("SNOW_RED_LINE_H2_MAX must be within 0..179")
    if stamina_line_h1_min > stamina_line_h1_max:
        raise ValueError("SNOW_RED_LINE_H1_MIN must be <= SNOW_RED_LINE_H1_MAX")
    if stamina_line_h2_min > stamina_line_h2_max:
        raise ValueError("SNOW_RED_LINE_H2_MIN must be <= SNOW_RED_LINE_H2_MAX")
    if stamina_line_min_s < 0 or stamina_line_min_s > 255:
        raise ValueError("SNOW_RED_LINE_MIN_S must be within 0..255")
    if stamina_line_min_v < 0 or stamina_line_min_v > 255:
        raise ValueError("SNOW_RED_LINE_MIN_V must be within 0..255")

    return RuntimeConfig(
        tap_enabled=tap_enabled,
        tap_target_width=tap_target_width,
        tap_target_height=tap_target_height,
        autonomous_fps=autonomous_fps,
        stamina_line_min_ratio=stamina_line_min_ratio,
        stamina_line_h1_min=stamina_line_h1_min,
        stamina_line_h1_max=stamina_line_h1_max,
        stamina_line_h2_min=stamina_line_h2_min,
        stamina_line_h2_max=stamina_line_h2_max,
        stamina_line_min_s=stamina_line_min_s,
        stamina_line_min_v=stamina_line_min_v,
    )


class JpegPayloadProvider:
    def __init__(self, manager: FrameStreamManager, config: RuntimeConfig) -> None:
        self._manager = manager
        self._config = config
        self._jpeg_opts = [int(cv2.IMWRITE_JPEG_QUALITY), COMMON_SETTINGS.jpeg_quality]

        self._lock = threading.Lock()
        self._last_ts = -1.0
        self._last_payload: bytes | None = None
        self._overlay_payload: dict = {"shapes": [], "texts": []}

        self._cv_lock = threading.Lock()
        self._cv_busy = False
        self._cv_drop_count = 0
        self._cv_last_circles: list[tuple[int, int, int]] = []
        self._cv_last_triangles: list[tuple[int, int, int]] = []
        self._cv_last_latency_ms = 0.0
        self._cv_status = "idle"
        self._cv_needs_stamina_recovery = False
        self._cv_result_seq = 0
        self._cv_scheduler = NewThreadScheduler()
        self._triangle_suppress_until = 0.0

        self._tap_lock = threading.Lock()
        self._tap_status = "idle"
        self._tap_last_latency_ms = 0.0
        self._tap_last_count = 0
        self._tap_worker = AsyncSequencedWorker(self._run_tap_batch, cooldown_sec=1.0, name="snow-tap")

        self._stamina_lock = threading.Lock()
        self._stamina_status = "idle"
        self._stamina_last_latency_ms = 0.0
        self._stamina_last_recover = False
        self._stamina_worker = AsyncSequencedWorker(
            self._run_stamina_recovery,
            cooldown_sec=0.0,
            name="snow-stamina-recovery",
        )

        self._tap_target_w, self._tap_target_h = resolve_tap_target_size(
            COMMON_SETTINGS.capture_width,
            COMMON_SETTINGS.capture_height,
            override_width=self._config.tap_target_width,
            override_height=self._config.tap_target_height,
            log_prefix="tap_map",
        )

    def _run_cv_detection(self, frame: np.ndarray, roi_jobs: list[tuple[int, int, np.ndarray]]) -> None:
        started = time.monotonic()
        circles: list[tuple[int, int, int]] = []
        triangles: list[tuple[int, int, int]] = []
        status = "ok"
        needs_stamina_recovery = False
        try:
            if self._should_recover_stamina_from_frame(frame):
                needs_stamina_recovery = True
                status = "need_stamina_recovery"

            # Circle detection always runs. A circle can trigger a triangle-suppression window.
            if not needs_stamina_recovery:
                for x1, y1, roi in roi_jobs:
                    if roi.size == 0:
                        continue

                    roi_circles = detect_ring_circle_tuples(
                        roi,
                        min_radius=9,
                        max_radius=12,
                        target_radius=12,
                        radius_tolerance=2,
                        refine_search_px=3,
                    )
                    for x, y, r in roi_circles:
                        circles.append((x + x1, y + y1, r))

                now = time.monotonic()
                suppress_active = now < self._triangle_suppress_until
                if circles and not suppress_active:
                    self._triangle_suppress_until = now + TRIANGLE_SUPPRESS_AFTER_CIRCLE_SEC
                    suppress_active = True
                    print(
                        f"triangle_suppress action=start sec={TRIANGLE_SUPPRESS_AFTER_CIRCLE_SEC:.1f} "
                        f"circles={len(circles)}"
                    )

                if suppress_active:
                    remain_ms = max(0.0, (self._triangle_suppress_until - now) * 1000.0)
                    status = f"ok:tri_supp {remain_ms:.0f}ms"
                else:
                    for x1, y1, roi in roi_jobs:
                        if roi.size == 0:
                            continue
                        roi_targets = detect_yellow_inverted_triangle_tuples(roi)
                        for x, y, base_len in roi_targets:
                            triangles.append((x + x1, y + y1, base_len))
        except Exception as exc:
            status = f"error:{type(exc).__name__}"

        latency_ms = (time.monotonic() - started) * 1000.0
        with self._cv_lock:
            self._cv_last_circles = circles
            self._cv_last_triangles = triangles
            self._cv_last_latency_ms = latency_ms
            self._cv_status = status
            self._cv_needs_stamina_recovery = needs_stamina_recovery
            self._cv_result_seq += 1
            self._cv_busy = False

    def _submit_cv_if_idle(self, frame: np.ndarray, roi_jobs: list[tuple[int, int, np.ndarray]]) -> None:
        with self._cv_lock:
            if self._cv_busy:
                self._cv_drop_count += 1
                return
            self._cv_busy = True
            self._cv_status = "running"
        frame_copy = frame.copy()
        jobs_copy = list(roi_jobs)
        rx.just((frame_copy, jobs_copy)).pipe(ops.subscribe_on(self._cv_scheduler)).subscribe(
            on_next=lambda payload: self._run_cv_detection(payload[0], payload[1]),
            on_error=self._on_cv_submit_error,
        )

    def _on_cv_submit_error(self, exc: Exception) -> None:
        with self._cv_lock:
            self._cv_status = f"error:{type(exc).__name__}"
            self._cv_busy = False
        print(f"snow cv_submit_error type={type(exc).__name__} msg={exc}")

    def _snapshot_cv_state(
        self,
    ) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int]], bool, str, int, float, int, bool]:
        with self._cv_lock:
            circles = list(self._cv_last_circles)
            triangles = list(self._cv_last_triangles)
            is_busy = self._cv_busy
            status = self._cv_status
            dropped = self._cv_drop_count
            latency_ms = self._cv_last_latency_ms
            result_seq = self._cv_result_seq
            needs_stamina_recovery = self._cv_needs_stamina_recovery
        return circles, triangles, is_busy, status, dropped, latency_ms, result_seq, needs_stamina_recovery

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
            f"stamina_recovery tap frame=({frame_x},{frame_y}) mapped=({tap_x},{tap_y}) "
            f"tap=({used_x},{used_y}) drift=({used_x - tap_x},{used_y - tap_y}) ok={int(ok)}"
        )
        return ok

    def _input_text(self, text: str) -> bool:
        ok = adb_shell_ok(["input", "text", text])
        print(f"stamina_recovery input_text={text} ok={int(ok)}")
        return ok

    def _wait(self, seconds: float) -> bool:
        return wait_with_random_jitter(seconds, shutdown_event=shutdown_event)

    @staticmethod
    def _noop_publish_active_step(_order: int) -> None:
        return

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

        return best_ratio >= min_ratio

    def _has_top_stamina_warning_line_in_frame(self, frame: np.ndarray) -> bool:
        h, w = frame.shape[:2]
        if h <= 0 or w <= 0:
            return False

        expected_x1, target_y = scale_point_from_reference(
            w,
            h,
            STAMINA_LINE_REF_X1,
            STAMINA_LINE_REF_Y,
            ref_w=STAMINA_LINE_REF_WIDTH,
            ref_h=STAMINA_LINE_REF_HEIGHT,
        )
        expected_x2, _target_y2 = scale_point_from_reference(
            w,
            h,
            STAMINA_LINE_REF_X2,
            STAMINA_LINE_REF_Y,
            ref_w=STAMINA_LINE_REF_WIDTH,
            ref_h=STAMINA_LINE_REF_HEIGHT,
        )

        scale_x = w / float(STAMINA_LINE_REF_WIDTH)
        margin_px = max(2, int(round(6 * scale_x)))
        scan_x1 = max(0, expected_x1 - margin_px)
        scan_x2 = min(w - 1, expected_x2 + margin_px)

        return self._is_stamina_line_in_frame(
            frame,
            scan_x1,
            target_y,
            scan_x2,
            target_y,
            min_ratio=self._config.stamina_line_min_ratio,
        )

    def _should_recover_stamina_from_frame(self, frame: np.ndarray) -> bool:
        sample_count = 5
        sample_interval_sec = 0.12
        stamina_warning_hits = 0

        for idx in range(sample_count):
            if self._has_top_stamina_warning_line_in_frame(frame):
                stamina_warning_hits += 1
            if idx < sample_count - 1 and shutdown_event.wait(timeout=sample_interval_sec):
                return False

        return stamina_warning_hits > 0

    def _run_stamina_recovery(self) -> None:
        started = time.monotonic()
        status = "ok"
        recovered = False
        print("stamina_recovery action=start")
        try:
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
            else:
                recovered = True
        except Exception as exc:
            status = f"error:{type(exc).__name__}"
            print(f"stamina_recovery action_error type={type(exc).__name__} msg={exc}")

        latency_ms = (time.monotonic() - started) * 1000.0
        print(
            f"stamina_recovery action=done recovered={int(recovered)} "
            f"status={status} ms={latency_ms:.1f}"
        )
        with self._stamina_lock:
            self._stamina_status = status
            self._stamina_last_latency_ms = latency_ms
            self._stamina_last_recover = recovered

    def _submit_stamina_recovery_if_needed(self, needs_recovery: bool, cv_result_seq: int) -> bool:
        if not needs_recovery:
            return False

        allowed, reason, remain_ms = self._stamina_worker.submit(cv_result_seq)
        if not allowed:
            if reason == "busy":
                print("stamina_recovery action=skip reason=busy")
            elif reason == "cooldown":
                print(f"stamina_recovery action=skip reason=cooldown_left_ms={remain_ms:.1f}")
            return False

        with self._stamina_lock:
            self._stamina_status = "running"
            self._stamina_last_recover = False
        with self._cv_lock:
            self._cv_needs_stamina_recovery = False
        return True

    def _snapshot_stamina_state(self) -> tuple[bool, str, float, bool]:
        busy, _skip_busy, _skip_cooldown = self._stamina_worker.snapshot()
        with self._stamina_lock:
            return (
                busy,
                self._stamina_status,
                self._stamina_last_latency_ms,
                self._stamina_last_recover,
            )

    def _dedupe_tap_points(self, candidates: list[tuple[int, int]]) -> list[tuple[int, int]]:
        points: list[tuple[int, int]] = []
        for x, y in candidates:
            keep = True
            for px, py in points:
                if abs(px - x) <= 3 and abs(py - y) <= 3:
                    keep = False
                    break
            if keep:
                points.append((x, y))
        return points

    def _alternate_left_right_points(self, points: list[tuple[int, int]]) -> list[tuple[int, int]]:
        if len(points) <= 1:
            return points

        center_x = COMMON_SETTINGS.capture_width / 2.0
        left = sorted([(x, y) for x, y in points if x < center_x], key=lambda p: (p[1], p[0]))
        right = sorted([(x, y) for x, y in points if x >= center_x], key=lambda p: (p[1], p[0]))

        if not left or not right:
            return left + right

        out: list[tuple[int, int]] = []
        li = 0
        ri = 0

        # Start from the side that has more points to reduce consecutive leftovers.
        take_left = len(left) >= len(right)
        while li < len(left) or ri < len(right):
            if take_left and li < len(left):
                out.append(left[li])
                li += 1
            elif (not take_left) and ri < len(right):
                out.append(right[ri])
                ri += 1

            take_left = not take_left

            if li >= len(left):
                while ri < len(right):
                    out.append(right[ri])
                    ri += 1
            elif ri >= len(right):
                while li < len(left):
                    out.append(left[li])
                    li += 1

        return out

    def _build_tap_points(
        self,
        circles: list[tuple[int, int, int]],
        triangles: list[tuple[int, int, int]],
    ) -> list[tuple[int, int, float, str]]:
        circle_points = self._dedupe_tap_points([(x, y) for x, y, _r in circles])
        circle_points = self._alternate_left_right_points(circle_points)
        triangle_points = self._dedupe_tap_points(triangles_to_tap_points(triangles))

        out: list[tuple[int, int, float, str]] = []
        for x, y in circle_points:
            out.append(
                (
                    x,
                    y,
                    random.uniform(DEFAULT_CIRCLE_POST_WAIT_MIN_SEC, DEFAULT_CIRCLE_POST_WAIT_MAX_SEC),
                    "circle",
                )
            )
        for x, y in triangle_points:
            # Triangle-triggered taps must wait 1 second before continuing.
            out.append((x, y, 1.0, "triangle"))
        return out

    def _run_tap_batch(self, tap_points: list[tuple[int, int, float, str]]) -> None:
        started = time.monotonic()
        ok_count = 0
        status = "ok"
        circle_count = 0

        print(f"tap_batch action=start count={len(tap_points)}")

        for idx, (x, y, post_wait_sec, source) in enumerate(tap_points, start=1):
            tap_x = x
            tap_y = y
            used_x = x
            used_y = y
            try:
                tap_started = time.monotonic()
                ok, tap_x, tap_y, used_x, used_y = adb_tap_frame_point_with_random_drift(
                    x,
                    y,
                    COMMON_SETTINGS.capture_width,
                    COMMON_SETTINGS.capture_height,
                    self._tap_target_w,
                    self._tap_target_h,
                    max_drift_px=2,
                )
                tap_ms = (time.monotonic() - tap_started) * 1000.0
                if ok:
                    ok_count += 1
                    if source == "circle":
                        circle_count += 1
                    print(
                        f"tap_item idx={idx} src={source} frame=({x},{y}) mapped=({tap_x},{tap_y}) "
                        f"tap=({used_x},{used_y}) drift=({used_x - tap_x},{used_y - tap_y}) "
                        f"ok=1 ms={tap_ms:.1f}"
                    )
                else:
                    status = "rc=1"
                    print(
                        f"tap_item idx={idx} src={source} frame=({x},{y}) mapped=({tap_x},{tap_y}) "
                        f"tap=({used_x},{used_y}) drift=({used_x - tap_x},{used_y - tap_y}) "
                        f"ok=0 ms={tap_ms:.1f}"
                    )
            except BaseException as exc:
                status = f"error:{type(exc).__name__}"
                print(
                    f"tap_item idx={idx} src={source} frame=({x},{y}) mapped=({tap_x},{tap_y}) "
                    f"tap=({used_x},{used_y}) drift=({used_x - tap_x},{used_y - tap_y}) "
                    f"ok=0 status={status}"
                )

            if post_wait_sec > 0:
                if source == "circle":
                    # Keep circle delay exact; do not add common jitter.
                    shutdown_event.wait(timeout=post_wait_sec)
                else:
                    wait_with_random_jitter(post_wait_sec)

        if circle_count > 0:
            print(f"tap_batch post_circle_wait_sec={DEFAULT_CIRCLE_BATCH_POST_WAIT_SEC:.1f}")
            shutdown_event.wait(timeout=DEFAULT_CIRCLE_BATCH_POST_WAIT_SEC)

        latency_ms = (time.monotonic() - started) * 1000.0
        print(f"tap_batch action=done ok={ok_count}/{len(tap_points)} status={status} ms={latency_ms:.1f}")
        with self._tap_lock:
            self._tap_last_count = ok_count
            self._tap_last_latency_ms = latency_ms
            self._tap_status = status

    def _submit_taps_if_allowed(
        self,
        circles: list[tuple[int, int, int]],
        triangles: list[tuple[int, int, int]],
        cv_result_seq: int,
    ) -> bool:
        if not self._config.tap_enabled:
            return False

        tap_points = self._build_tap_points(circles, triangles)
        if not tap_points:
            return False

        allowed, reason, remain_ms = self._tap_worker.submit(cv_result_seq, tap_points)
        if not allowed:
            busy, skip_busy, skip_cooldown = self._tap_worker.snapshot()
            skip_total = skip_busy + skip_cooldown
            if reason == "busy":
                print(f"tap_batch action=skip reason=busy skip_count={skip_total}")
            elif reason == "cooldown":
                print(
                    f"tap_batch action=skip reason=cooldown_left_ms={remain_ms:.1f} "
                    f"skip_count={skip_total}"
                )
            return False

        with self._tap_lock:
            self._tap_status = "running"
        return True

    def _snapshot_tap_state(self) -> tuple[bool, str, int, float, int]:
        busy, skip_busy, skip_cooldown = self._tap_worker.snapshot()
        with self._tap_lock:
            return (
                busy,
                self._tap_status,
                self._tap_last_count,
                self._tap_last_latency_ms,
                skip_busy + skip_cooldown,
            )

    def __call__(self) -> bytes | None:
        snapshot = self._manager.snapshot()
        if snapshot is None:
            return None

        captured_at, frame = snapshot
        age_sec = time.monotonic() - captured_at

        with self._lock:
            if captured_at == self._last_ts and self._last_payload is not None:
                return self._last_payload

            roi_rects = scale_overlay_rectangles(frame.shape[1], frame.shape[0])
            circles, triangles, cv_busy, cv_status, dropped, latency_ms, cv_result_seq, needs_stamina_recovery = (
                self._snapshot_cv_state()
            )
            tap_busy, tap_status, tap_count, tap_latency_ms, tap_skip = self._snapshot_tap_state()
            stamina_busy, stamina_status, stamina_latency_ms, stamina_recovered = self._snapshot_stamina_state()

            if not stamina_busy and needs_stamina_recovery:
                self._submit_stamina_recovery_if_needed(needs_stamina_recovery, cv_result_seq)
                stamina_busy, stamina_status, stamina_latency_ms, stamina_recovered = self._snapshot_stamina_state()

            if stamina_busy:
                cv_status = "paused_for_stamina_recovery"
                cv_busy = False

            # A detected batch must be fully tapped before submitting the next CV round.
            if (not tap_busy) and (not stamina_busy):
                self._submit_taps_if_allowed(circles, triangles, cv_result_seq)
                tap_busy, tap_status, tap_count, tap_latency_ms, tap_skip = self._snapshot_tap_state()

            if (not tap_busy) and (not stamina_busy):
                roi_jobs: list[tuple[int, int, np.ndarray]] = []
                for x1, y1, x2, y2 in roi_rects:
                    roi = frame[y1 : y2 + 1, x1 : x2 + 1]
                    if roi.size == 0:
                        continue
                    roi_jobs.append((x1, y1, roi.copy()))
                self._submit_cv_if_idle(frame, roi_jobs)

            circles, triangles, cv_busy, cv_status, dropped, latency_ms, cv_result_seq, needs_stamina_recovery = (
                self._snapshot_cv_state()
            )
            if tap_busy:
                cv_status = "paused_for_tap"
                cv_busy = False
            elif stamina_busy:
                cv_status = "paused_for_stamina_recovery"
                cv_busy = False

            shapes: list[dict] = []
            texts: list[dict] = []

            for x1, y1, x2, y2 in roi_rects:
                shapes.append(
                    {
                        "type": "rect",
                        "x1": x1,
                        "y1": y1,
                        "x2": x2,
                        "y2": y2,
                        "color": "rgb(0,255,255)",
                        "width": 2,
                    }
                )

            for idx, (x, y, r) in enumerate(circles, start=1):
                shapes.append({"type": "circle", "x": x, "y": y, "r": r, "color": "rgb(0,255,0)", "width": 2})
                shapes.append({"type": "circle", "x": x, "y": y, "r": 2, "color": "rgb(255,0,0)", "fill": True})
                texts.append(
                    {
                        "text": f"#{idx} r={r}",
                        "x": x + 6,
                        "y": y - 6,
                        "color": "rgb(0,255,255)",
                        "font": "bold 12px monospace",
                    }
                )

            for idx, (x, y, base_len) in enumerate(triangles, start=1):
                tri_pts = build_triangle_points(x, y, base_len)
                shapes.append(
                    {
                        "type": "polyline",
                        "points": [[px, py] for px, py in tri_pts],
                        "closed": True,
                        "color": "rgb(0,255,0)",
                        "width": 2,
                    }
                )
                shapes.append({"type": "circle", "x": x, "y": y, "r": 2, "color": "rgb(255,0,0)", "fill": True})
                texts.append(
                    {
                        "text": f"T{idx} b={base_len}",
                        "x": x + 6,
                        "y": y - 6,
                        "color": "rgb(0,255,255)",
                        "font": "bold 12px monospace",
                    }
                )

            state_text = "running" if cv_busy else cv_status
            texts.append(
                {
                    "text": f"CV {state_text} circles={len(circles)} triangles={len(triangles)} drop={dropped} {latency_ms:.1f}ms",
                    "x": 16,
                    "y": frame.shape[0] - 14,
                    "color": "rgb(0,255,255)" if cv_busy else "rgb(120,255,120)",
                    "font": "bold 12px monospace",
                }
            )
            texts.append(
                {
                    "text": f"TAP {('running' if tap_busy else tap_status)} sent={tap_count} skip={tap_skip} {tap_latency_ms:.1f}ms",
                    "x": 16,
                    "y": frame.shape[0] - 32,
                    "color": "rgb(0,255,255)" if tap_busy else "rgb(120,255,120)",
                    "font": "bold 12px monospace",
                }
            )
            texts.append(
                {
                    "text": (
                        f"STAMINA {('running' if stamina_busy else stamina_status)} "
                        f"recover={int(stamina_recovered)} {stamina_latency_ms:.1f}ms"
                    ),
                    "x": 16,
                    "y": frame.shape[0] - 50,
                    "color": "rgb(0,255,255)" if stamina_busy else "rgb(120,255,120)",
                    "font": "bold 12px monospace",
                }
            )
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

            self._overlay_payload = {"shapes": shapes, "texts": texts}

            ok, encoded = cv2.imencode(".jpg", frame, self._jpeg_opts)
            if not ok:
                return None

            self._last_ts = captured_at
            self._last_payload = encoded.tobytes()
            return self._last_payload

    def make_web_payload(self) -> dict:
        with self._lock:
            return {
                "shapes": list(self._overlay_payload.get("shapes", [])),
                "texts": list(self._overlay_payload.get("texts", [])),
            }


class AutonomousRunner:
    def __init__(
        self,
        payload_provider: JpegPayloadProvider,
        shutdown_event: threading.Event,
        loop_fps: float,
    ) -> None:
        self._payload_provider = payload_provider
        self._shutdown_event = shutdown_event
        self._interval_sec = 1.0 / loop_fps
        self._loop_lock = threading.Lock()
        self._loop_scheduler = NewThreadScheduler()
        self._loop_subscription: Disposable | None = None

    def start(self) -> None:
        with self._loop_lock:
            if self._loop_subscription is not None:
                return
            self._loop_subscription = (
                rx.interval(self._interval_sec if self._interval_sec > 0 else 0.01, scheduler=self._loop_scheduler)
                .pipe(ops.take_while(lambda _i: not self._shutdown_event.is_set()))
                .subscribe(
                    on_next=lambda _i: self._on_loop_tick(),
                    on_error=self._on_loop_error,
                    on_completed=self._on_loop_completed,
                )
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
        print(f"autonomous loop_error type={type(exc).__name__} msg={exc}")
        self._clear_loop_subscription()

    def _on_loop_completed(self) -> None:
        self._clear_loop_subscription()

    def _on_loop_tick(self) -> None:
        if self._shutdown_event.is_set():
            return
        started = time.monotonic()
        try:
            self._payload_provider()
        except Exception as exc:
            print(f"autonomous loop_error type={type(exc).__name__} msg={exc}")

        elapsed = time.monotonic() - started
        wait_sec = max(0.0, self._interval_sec - elapsed)
        wait_with_random_jitter(wait_sec, shutdown_event=self._shutdown_event)


def main() -> None:
    install_signal_handlers()
    config = load_config()

    stream_manager = make_common_stream_manager(shutdown_event)
    stream_manager.start()

    waiting_payload = make_common_waiting_payload()
    payload_provider = JpegPayloadProvider(stream_manager, config)
    autonomous_runner = AutonomousRunner(
        payload_provider=payload_provider,
        shutdown_event=shutdown_event,
        loop_fps=config.autonomous_fps,
    )
    autonomous_runner.start()

    web_server = make_common_web_server(
        payload_provider,
        waiting_payload,
        shutdown_event,
        overlay_provider=payload_provider.make_web_payload,
    )

    print(make_common_startup_log(f"autonomous_fps={config.autonomous_fps}"))

    try:
        web_server.serve_forever()
    except KeyboardInterrupt:
        request_shutdown("keyboard_interrupt")
    finally:
        web_server.shutdown()
        request_shutdown("server_stop")
        autonomous_runner.join(timeout=2.0)
        stream_manager.join(timeout=2.0)


if __name__ == "__main__":
    main()
