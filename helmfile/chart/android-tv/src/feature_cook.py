#!/usr/bin/env python3
from pathlib import Path
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

from common_action import triangle_point_to_tap_point
from common_adb import adb_tap_frame_point_with_random_drift, resolve_tap_target_size
from common_feature_runtime import AsyncSequencedWorker
from common_feature_settings import (
    get_common_runtime_settings,
    make_common_startup_log,
    make_common_stream_manager,
    make_common_waiting_payload,
    make_common_web_server,
)
from common_stream import FrameStreamManager
from common_cv import detect_yellow_inverted_triangle_tuples


shutdown_event = threading.Event()
COMMON_SETTINGS = get_common_runtime_settings()
SAMPLE_TEMPLATE_PATH = Path("/sample_click/cook.png")
SAMPLE_MATCH_THRESHOLD = 0.60
TAP_WAIT_SEC = 0.5
DETECTION_MIN_INTERVAL_SEC = 0.12
DETECTION_ERROR_BACKOFF_SEC = 0.25
TRIANGLE_TAP_WAIT_SEC = 1.0
LINE_TAP_WAIT_SEC = 2.0

# 640x360 reference line for direct color-match tapping.
LINE_TAP_X1_REF = 450
LINE_TAP_Y_REF = 337
LINE_TAP_X2_REF = 500
LINE_TAP_TARGET_BGR = np.array([197, 205, 58], dtype=np.float32)  # #3ACDC5 in BGR
LINE_TAP_COLOR_DIST_MAX = 32.0
LINE_TAP_MATCH_RATIO_MIN = 0.95

# ROI rectangles in 640x360 reference coordinates: (x1, y1, x2, y2).
STREAM_OVERLAY_RECTS_REF: tuple[tuple[int, int, int, int], ...] = (
    (69, 120, 109, 160),
    (79, 150, 99, 170),
)


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


def scale_line_tap_segment(frame_w: int, frame_h: int) -> tuple[int, int, int]:
    x1 = max(0, min(frame_w - 1, int(round(LINE_TAP_X1_REF * frame_w / 640.0))))
    x2 = max(0, min(frame_w - 1, int(round(LINE_TAP_X2_REF * frame_w / 640.0))))
    y = max(0, min(frame_h - 1, int(round(LINE_TAP_Y_REF * frame_h / 360.0))))
    if x2 < x1:
        x1, x2 = x2, x1
    return x1, y, x2


@dataclass(frozen=True)
class Circle:
    x: int
    y: int
    r: int
    w: int
    h: int
    score: float


_sample_template_gray: np.ndarray | None = None
_sample_template_size: tuple[int, int] | None = None


def _load_sample_template() -> tuple[np.ndarray, tuple[int, int]]:
    global _sample_template_gray, _sample_template_size
    if _sample_template_gray is not None and _sample_template_size is not None:
        return _sample_template_gray, _sample_template_size

    tpl = cv2.imread(str(SAMPLE_TEMPLATE_PATH), cv2.IMREAD_GRAYSCALE)
    if tpl is None:
        raise RuntimeError(f"sample template not found/readable: {SAMPLE_TEMPLATE_PATH}")

    h, w = tpl.shape[:2]
    if h < 2 or w < 2:
        raise RuntimeError(f"sample template too small: {w}x{h}")

    _sample_template_gray = tpl
    _sample_template_size = (w, h)
    print(
        f"cook_template path={SAMPLE_TEMPLATE_PATH} size={w}x{h} threshold={SAMPLE_MATCH_THRESHOLD:.2f}"
    )
    return _sample_template_gray, _sample_template_size


class SharedDetectionState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._captured_at = -1.0
        self._circles: list[Circle] = []
        self._triangles: list[tuple[int, int, int]] = []

    def update(self, captured_at: float, circles: list[Circle], triangles: list[tuple[int, int, int]]) -> None:
        with self._lock:
            self._captured_at = captured_at
            self._circles = list(circles)
            self._triangles = list(triangles)

    def snapshot(self) -> tuple[float, list[Circle], list[tuple[int, int, int]]]:
        with self._lock:
            return self._captured_at, list(self._circles), list(self._triangles)


class OverlayState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._payload: dict = {"shapes": [], "texts": []}

    def update(self, payload: dict) -> None:
        with self._lock:
            self._payload = payload

    def make_web_payload(self) -> dict:
        with self._lock:
            return {
                "shapes": list(self._payload.get("shapes", [])),
                "texts": list(self._payload.get("texts", [])),
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


def _clip_roi(frame_shape: tuple[int, int, int], roi_rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    frame_h, frame_w = frame_shape[:2]
    x1, y1, x2, y2 = roi_rect
    x1 = max(0, min(frame_w - 1, x1))
    y1 = max(0, min(frame_h - 1, y1))
    x2 = max(x1, min(frame_w - 1, x2))
    y2 = max(y1, min(frame_h - 1, y2))
    return x1, y1, x2, y2


def _detect_template_in_roi(roi_bgr: np.ndarray) -> Circle | None:
    roi_h, roi_w = roi_bgr.shape[:2]
    tpl, (tpl_w, tpl_h) = _load_sample_template()
    if roi_h < tpl_h or roi_w < tpl_w:
        return None

    gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
    res = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
    _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(res)
    if max_val < SAMPLE_MATCH_THRESHOLD:
        return None

    top_left_x, top_left_y = max_loc
    cx = top_left_x + tpl_w // 2
    cy = top_left_y + tpl_h // 2
    radius = max(tpl_w, tpl_h) // 2
    return Circle(x=cx, y=cy, r=radius, w=tpl_w, h=tpl_h, score=float(max_val))


def detect_circles_in_roi(
    frame_bgr: np.ndarray,
    roi_rect: tuple[int, int, int, int],
) -> list[Circle]:
    x1, y1, x2, y2 = _clip_roi(frame_bgr.shape, roi_rect)
    roi = frame_bgr[y1 : y2 + 1, x1 : x2 + 1]
    if roi.size == 0:
        return []

    local = _detect_template_in_roi(roi)
    if local is None:
        return []

    return [
        Circle(
            x=x1 + local.x,
            y=y1 + local.y,
            r=local.r,
            w=local.w,
            h=local.h,
            score=local.score,
        )
    ]


def detect_triangles_in_roi(
    frame_bgr: np.ndarray,
    roi_rect: tuple[int, int, int, int],
) -> list[tuple[int, int, int]]:
    x1, y1, x2, y2 = _clip_roi(frame_bgr.shape, roi_rect)
    roi = frame_bgr[y1 : y2 + 1, x1 : x2 + 1]
    if roi.size == 0:
        return []

    local = detect_yellow_inverted_triangle_tuples(roi)
    return [(x1 + tx, y1 + ty, base_len) for tx, ty, base_len in local]


def detect_line_tap_point(frame_bgr: np.ndarray) -> tuple[int, int] | None:
    frame_h, frame_w = frame_bgr.shape[:2]
    x1, y, x2 = scale_line_tap_segment(frame_w, frame_h)

    line_pixels = frame_bgr[y, x1 : x2 + 1].astype(np.float32)
    if line_pixels.size == 0:
        return None

    color_dists = np.linalg.norm(line_pixels - LINE_TAP_TARGET_BGR, axis=1)
    match_ratio = float(np.mean(color_dists <= LINE_TAP_COLOR_DIST_MAX))
    if match_ratio < LINE_TAP_MATCH_RATIO_MIN:
        return None

    return (x1 + x2) // 2, y


class JpegPayloadProvider:
    def __init__(
        self,
        manager: FrameStreamManager,
        detection_state: SharedDetectionState,
        overlay_state: OverlayState,
    ) -> None:
        self._manager = manager
        self._detection_state = detection_state
        self._overlay_state = overlay_state
        self._jpeg_opts = [int(cv2.IMWRITE_JPEG_QUALITY), COMMON_SETTINGS.jpeg_quality]

        self._lock = threading.Lock()
        self._last_ts = -1.0
        self._last_payload: bytes | None = None
        self._display_circles: list[Circle] = []
        self._display_triangles: list[tuple[int, int, int]] = []

    def __call__(self) -> bytes | None:
        snapshot = self._manager.snapshot()
        if snapshot is None:
            return None

        captured_at, frame = snapshot
        age_sec = time.monotonic() - captured_at

        with self._lock:
            if captured_at == self._last_ts and self._last_payload is not None:
                return self._last_payload

            circle_roi, triangle_roi = scale_overlay_rectangles(frame.shape[1], frame.shape[0])
            _, state_circles, state_triangles = self._detection_state.snapshot()
            self._display_circles = state_circles
            self._display_triangles = state_triangles
            line_x1, line_y, line_x2 = scale_line_tap_segment(frame.shape[1], frame.shape[0])

            shapes: list[dict] = [
                {
                    "type": "rect",
                    "x1": circle_roi[0],
                    "y1": circle_roi[1],
                    "x2": circle_roi[2],
                    "y2": circle_roi[3],
                    "color": "rgb(0,0,255)",
                    "width": 1,
                },
                {
                    "type": "rect",
                    "x1": triangle_roi[0],
                    "y1": triangle_roi[1],
                    "x2": triangle_roi[2],
                    "y2": triangle_roi[3],
                    "color": "rgb(255,165,0)",
                    "width": 1,
                },
                {
                    "type": "line",
                    "x1": line_x1,
                    "y1": line_y,
                    "x2": line_x2,
                    "y2": line_y,
                    "color": "rgb(0,180,255)",
                    "width": 1,
                },
            ]
            texts: list[dict] = []

            for idx, c in enumerate(self._display_circles, start=1):
                half_w = max(1, c.w // 2)
                half_h = max(1, c.h // 2)
                x1 = max(0, c.x - half_w)
                y1 = max(0, c.y - half_h)
                x2 = min(frame.shape[1] - 1, c.x + half_w)
                y2 = min(frame.shape[0] - 1, c.y + half_h)
                shapes.append(
                    {
                        "type": "rect",
                        "x1": x1,
                        "y1": y1,
                        "x2": x2,
                        "y2": y2,
                        "color": "rgb(0,255,0)",
                        "width": 1,
                    }
                )
                shapes.append({"type": "line", "x1": c.x - 4, "y1": c.y, "x2": c.x + 4, "y2": c.y, "color": "rgb(0,200,255)", "width": 1})
                shapes.append({"type": "line", "x1": c.x, "y1": c.y - 4, "x2": c.x, "y2": c.y + 4, "color": "rgb(0,200,255)", "width": 1})
                texts.append(
                    {
                        "text": f"#{idx} template s={c.score:.2f}",
                        "x": c.x + 4,
                        "y": c.y - 6,
                        "color": "rgb(0,255,255)",
                        "font": "bold 12px monospace",
                    }
                )

            for idx, (tx, ty, base_len) in enumerate(self._display_triangles, start=1):
                half = max(7, int(round(base_len * 0.9)))
                x1 = max(0, tx - half)
                y1 = max(0, ty - half)
                x2 = min(frame.shape[1] - 1, tx + half)
                y2 = min(frame.shape[0] - 1, ty + half)
                shapes.append(
                    {
                        "type": "rect",
                        "x1": x1,
                        "y1": y1,
                        "x2": x2,
                        "y2": y2,
                        "color": "rgb(255,0,255)",
                        "width": 1,
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

            self._overlay_state.update({"shapes": shapes, "texts": texts})

            ok, encoded = cv2.imencode(".jpg", frame, self._jpeg_opts)
            if not ok:
                return None

            self._last_ts = captured_at
            self._last_payload = encoded.tobytes()
            return self._last_payload


class CircleTapRunner:
    def __init__(
        self,
        manager: FrameStreamManager,
        shutdown: threading.Event,
        detection_state: SharedDetectionState,
    ) -> None:
        self._manager = manager
        self._shutdown_event = shutdown
        self._detection_state = detection_state
        self._loop_lock = threading.Lock()
        self._loop_scheduler = NewThreadScheduler()
        self._loop_subscription: Disposable | None = None
        self._last_ts = -1.0
        self._last_detect_at = 0.0
        self._tap_target_w = COMMON_SETTINGS.capture_width
        self._tap_target_h = COMMON_SETTINGS.capture_height
        self._detect_seq = 0
        self._tap_worker = AsyncSequencedWorker(self._run_tap_action, cooldown_sec=0.0, name="cook-tap")

    def start(self) -> None:
        with self._loop_lock:
            if self._loop_subscription is not None:
                return
        self._tap_target_w, self._tap_target_h = resolve_tap_target_size(
            COMMON_SETTINGS.capture_width,
            COMMON_SETTINGS.capture_height,
            log_prefix="cook_tap_map",
        )
        with self._loop_lock:
            self._loop_subscription = (
                rx.interval(0.02, scheduler=self._loop_scheduler)
                .pipe(ops.take_while(lambda _i: not self._shutdown_event.is_set()))
                .subscribe(
                    on_next=lambda _i: self._on_loop_tick(),
                    on_error=self._on_loop_error,
                    on_completed=self._on_loop_completed,
                )
            )
        print(f"cook_tap status=adb-tap wait_sec={TAP_WAIT_SEC}")

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
        print(f"cook_tap loop_error type={type(exc).__name__} msg={exc}")
        self._clear_loop_subscription()

    def _on_loop_completed(self) -> None:
        self._clear_loop_subscription()

    def _run_tap_action(
        self,
        source: str,
        target_x: int,
        target_y: int,
        frame_w: int,
        frame_h: int,
        wait_sec: float,
    ) -> None:
        ok, tap_x, tap_y, used_x, used_y = adb_tap_frame_point_with_random_drift(
            target_x,
            target_y,
            frame_w,
            frame_h,
            self._tap_target_w,
            self._tap_target_h,
            max_drift_px=0,
        )
        print(
            f"cook_tap type={source} frame=({target_x},{target_y}) mapped=({tap_x},{tap_y}) "
            f"tap=({used_x},{used_y}) drift=({used_x - tap_x},{used_y - tap_y}) ok={int(ok)} "
            f"frame_size={frame_w}x{frame_h} target={self._tap_target_w}x{self._tap_target_h}"
        )
        self._shutdown_event.wait(timeout=wait_sec)

    def _submit_tap_if_allowed(
        self,
        source: str,
        target_x: int,
        target_y: int,
        frame_w: int,
        frame_h: int,
        wait_sec: float,
        detect_seq: int,
    ) -> None:
        allowed, _reason, _cooldown_left_ms = self._tap_worker.submit(
            detect_seq,
            source,
            target_x,
            target_y,
            frame_w,
            frame_h,
            wait_sec,
        )
        if not allowed:
            return

    def _on_loop_tick(self) -> None:
        if self._shutdown_event.is_set():
            return

        snapshot = self._manager.snapshot()
        if snapshot is None:
            return

        captured_at, frame = snapshot
        if captured_at == self._last_ts:
            return
        self._last_ts = captured_at

        now = time.monotonic()
        if (now - self._last_detect_at) < DETECTION_MIN_INTERVAL_SEC:
            return
        self._last_detect_at = now
        self._detect_seq += 1
        detect_seq = self._detect_seq

        try:
            circle_roi, triangle_roi = scale_overlay_rectangles(frame.shape[1], frame.shape[0])
            line_tap = detect_line_tap_point(frame)
            triangles = detect_triangles_in_roi(frame, triangle_roi)
            circles = detect_circles_in_roi(frame, circle_roi)
        except BaseException as exc:
            print(f"cook_detect error={exc}")
            self._detection_state.update(captured_at, [], [])
            self._shutdown_event.wait(timeout=DETECTION_ERROR_BACKOFF_SEC)
            return

        if line_tap is not None:
            self._detection_state.update(captured_at, [], [])
            target_x, target_y = line_tap
            source = "line"
            wait_sec = LINE_TAP_WAIT_SEC
        elif triangles:
            self._detection_state.update(captured_at, [], triangles)
            target_x, target_y = triangle_point_to_tap_point(triangles[0][0], triangles[0][1])
            source = "triangle"
            wait_sec = TRIANGLE_TAP_WAIT_SEC
        else:
            self._detection_state.update(captured_at, circles, [])
            if not circles:
                return
            target_x, target_y = circles[0].x, circles[0].y
            source = "circle"
            wait_sec = TAP_WAIT_SEC

        frame_h, frame_w = frame.shape[:2]
        self._submit_tap_if_allowed(
            source,
            target_x,
            target_y,
            frame_w,
            frame_h,
            wait_sec,
            detect_seq,
        )


def main() -> None:
    install_signal_handlers()
    _load_sample_template()

    stream_manager = make_common_stream_manager(shutdown_event)
    stream_manager.start()
    detection_state = SharedDetectionState()
    overlay_state = OverlayState()
    tap_runner = CircleTapRunner(stream_manager, shutdown_event, detection_state)
    tap_runner.start()

    waiting_payload = make_common_waiting_payload()
    payload_provider = JpegPayloadProvider(stream_manager, detection_state, overlay_state)

    web_server = make_common_web_server(
        payload_provider,
        waiting_payload,
        shutdown_event,
        overlay_provider=overlay_state.make_web_payload,
    )

    print(make_common_startup_log("mode=roi-overlay"))

    try:
        web_server.serve_forever()
    except KeyboardInterrupt:
        request_shutdown("keyboard_interrupt")
    finally:
        web_server.shutdown()
        request_shutdown("server_stop")
        tap_runner.join(timeout=2.0)
        stream_manager.join(timeout=2.0)


if __name__ == "__main__":
    main()
