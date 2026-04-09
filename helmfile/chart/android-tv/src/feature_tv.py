#!/usr/bin/env python3
import signal
import threading
import time
from dataclasses import dataclass

import cv2
import numpy as np

from common_adb import adb_shell_ok
from common_feature_runtime import AsyncBusyWorker, AsyncSequencedWorker
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

# ROI rectangle in frame coordinates: (x1, y1) ~ (x2, y2)
STREAM_ROI_RECT: tuple[int, int, int, int] = (500, 303, 622, 341)
DEFAULT_RR_ACTION_COOLDOWN_SEC = 2.0


@dataclass
class RuntimeConfig:
    rr_action_cooldown_sec: float


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
    rr_action_cooldown_sec = DEFAULT_RR_ACTION_COOLDOWN_SEC
    if rr_action_cooldown_sec < 0:
        raise ValueError("HARVEST_RR_ACTION_COOLDOWN_SEC must be >= 0")

    return RuntimeConfig(
        rr_action_cooldown_sec=rr_action_cooldown_sec,
    )


class JpegPayloadProvider:
    def __init__(self, manager: FrameStreamManager, config: RuntimeConfig) -> None:
        self._manager = manager
        self._config = config
        self._jpeg_opts = [int(cv2.IMWRITE_JPEG_QUALITY), COMMON_SETTINGS.jpeg_quality]

        self._render_lock = threading.Lock()
        self._last_render_version: tuple[float, int, bool, int] | None = None
        self._last_payload: bytes | None = None
        self._overlay_payload: dict = {"shapes": [], "texts": []}

        self._latest_frame_ts = -1.0

        self._cv_worker = AsyncBusyWorker(self._run_cv_detection, name="tv-cv")
        self._cv_lock = threading.Lock()
        self._cv_status = "idle"
        self._cv_last_latency_ms = 0.0
        self._cv_last_detections: list[tuple[int, int, int, int]] = []
        self._cv_result_seq = 0

        self._adb_worker = AsyncSequencedWorker(
            self._run_adb_action,
            cooldown_sec=config.rr_action_cooldown_sec,
            name="tv-adb",
        )
        self._adb_lock = threading.Lock()
        self._adb_status = "idle"
        self._adb_last_ok = True
        self._adb_action_seq = 0
        self._adb_sent_count = 0
        self._adb_fail_count = 0

    def _detect_rounded_rectangles_in_roi(self, frame: np.ndarray) -> list[tuple[int, int, int, int]]:
        frame_h, frame_w = frame.shape[:2]
        x1, y1, x2, y2 = STREAM_ROI_RECT

        x1 = max(0, min(frame_w - 1, x1))
        y1 = max(0, min(frame_h - 1, y1))
        x2 = max(x1, min(frame_w - 1, x2))
        y2 = max(y1, min(frame_h - 1, y2))

        roi = frame[y1 : y2 + 1, x1 : x2 + 1]
        if roi.size == 0:
            return []

        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blur, 30, 100)
        edges = cv2.dilate(edges, np.ones((3, 3), dtype=np.uint8), iterations=1)

        contours, _hier = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        detections: list[tuple[int, int, int, int]] = []
        roi_area = float((x2 - x1 + 1) * (y2 - y1 + 1))
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < max(60.0, roi_area * 0.004):
                continue

            peri = cv2.arcLength(contour, True)
            if peri <= 0:
                continue

            approx = cv2.approxPolyDP(contour, 0.02 * peri, True)
            # Rounded rectangles usually have more points than a sharp 4-corner box.
            if len(approx) < 6 or len(approx) > 24:
                continue

            rx, ry, rw, rh = cv2.boundingRect(contour)
            if rw < 40 or rh < 18 or rh > 35:
                continue

            rect_area = float(rw * rh)
            extent = area / rect_area if rect_area > 0 else 0.0
            if extent < 0.45 or extent > 0.98:
                continue

            aspect = rw / float(rh)
            if aspect < 0.5 or aspect > 3.2:
                continue

            detections.append((x1 + rx, y1 + ry, rw, rh))

        # Keep larger candidates first and remove highly-overlapped duplicates.
        detections.sort(key=lambda b: b[2] * b[3], reverse=True)
        filtered: list[tuple[int, int, int, int]] = []
        for bx, by, bw, bh in detections:
            keep = True
            for fx, fy, fw, fh in filtered:
                ix1 = max(bx, fx)
                iy1 = max(by, fy)
                ix2 = min(bx + bw, fx + fw)
                iy2 = min(by + bh, fy + fh)
                iw = max(0, ix2 - ix1)
                ih = max(0, iy2 - iy1)
                inter = iw * ih
                if inter == 0:
                    continue
                union = (bw * bh) + (fw * fh) - inter
                iou = inter / float(union) if union > 0 else 0.0
                if iou > 0.5:
                    keep = False
                    break
            if keep:
                filtered.append((bx, by, bw, bh))

        return filtered

    def make_web_payload(self) -> dict:
        with self._render_lock:
            return {
                "shapes": list(self._overlay_payload.get("shapes", [])),
                "texts": list(self._overlay_payload.get("texts", [])),
            }

    def _run_cv_detection(self, frame: np.ndarray) -> None:
        started = time.monotonic()
        try:
            detections = self._detect_rounded_rectangles_in_roi(frame)
            status = "ok"
        except Exception as exc:
            print(f"tv_rr_detect error={type(exc).__name__} msg={exc}")
            detections = []
            status = "error"

        latency_ms = (time.monotonic() - started) * 1000.0
        with self._cv_lock:
            self._cv_last_detections = detections
            self._cv_last_latency_ms = latency_ms
            self._cv_status = status
            self._cv_result_seq += 1

    def _submit_cv_if_idle(self, captured_at: float, frame: np.ndarray) -> None:
        if not self._cv_worker.submit(frame):
            return

        with self._cv_lock:
            self._cv_status = "running"

        self._latest_frame_ts = captured_at

    def _snapshot_cv_state(self) -> tuple[list[tuple[int, int, int, int]], bool, str, float, int, int]:
        cv_busy, cv_drop_count = self._cv_worker.snapshot()
        with self._cv_lock:
            return (
                list(self._cv_last_detections),
                cv_busy,
                self._cv_status,
                self._cv_last_latency_ms,
                cv_drop_count,
                self._cv_result_seq,
            )

    def _trigger_dpad_center(self) -> bool:
        # KEYCODE_DPAD_CENTER selects the focused item on Android TV.
        ok = adb_shell_ok(
            ["input", "keyevent", "KEYCODE_DPAD_CENTER"],
        )
        print(f"rr action=dpad_center ok={ok}")

        # Requirement: wait 2 seconds after sending the action.
        if shutdown_event.wait(timeout=self._config.rr_action_cooldown_sec):
            return ok
        return ok

    def _run_adb_action(self, cv_result_seq: int) -> None:
        try:
            ok = self._trigger_dpad_center()
        except Exception as exc:
            print(f"rr action error={type(exc).__name__} msg={exc}")
            ok = False

        with self._adb_lock:
            self._adb_last_ok = ok
            self._adb_status = "ok" if ok else "error"
            self._adb_action_seq += 1
            if ok:
                self._adb_sent_count += 1
            else:
                self._adb_fail_count += 1

    def _submit_adb_if_allowed(self, rr_detected: bool, cv_result_seq: int) -> None:
        if not rr_detected:
            return

        allowed, _reason, _cooldown_left_ms = self._adb_worker.submit(cv_result_seq, cv_result_seq)
        if not allowed:
            return

        with self._adb_lock:
            self._adb_status = "running"

    def _snapshot_adb_state(self) -> tuple[bool, str, bool, int, int, int]:
        adb_busy, _skip_busy, _skip_cooldown = self._adb_worker.snapshot()
        with self._adb_lock:
            return (
                adb_busy,
                self._adb_status,
                self._adb_last_ok,
                self._adb_action_seq,
                self._adb_sent_count,
                self._adb_fail_count,
            )

    def __call__(self) -> bytes | None:
        snapshot = self._manager.snapshot()
        if snapshot is None:
            return None

        captured_at, frame = snapshot
        age_sec = time.monotonic() - captured_at

        if captured_at != self._latest_frame_ts:
            # Skip-frame model: if CV is still running, do not queue this frame.
            self._submit_cv_if_idle(captured_at, frame.copy())

        detections, cv_busy, cv_status, cv_latency_ms, cv_drop_count, cv_result_seq = self._snapshot_cv_state()

        rr_detected = len(detections) > 0
        self._submit_adb_if_allowed(rr_detected, cv_result_seq)
        adb_busy, adb_status, _adb_last_ok, adb_action_seq, adb_sent_count, adb_fail_count = self._snapshot_adb_state()

        shapes: list[dict] = []
        texts: list[dict] = []
        x1, y1, x2, y2 = STREAM_ROI_RECT
        x1 = max(0, min(frame.shape[1] - 1, x1))
        y1 = max(0, min(frame.shape[0] - 1, y1))
        x2 = max(x1, min(frame.shape[1] - 1, x2))
        y2 = max(y1, min(frame.shape[0] - 1, y2))
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

        for idx, (dx, dy, dw, dh) in enumerate(detections, start=1):
            shapes.append(
                {
                    "type": "rect",
                    "x1": dx,
                    "y1": dy,
                    "x2": dx + dw,
                    "y2": dy + dh,
                    "color": "rgb(0,220,0)",
                    "width": 2,
                }
            )
            texts.append(
                {
                    "text": f"RR{idx}",
                    "x": dx,
                    "y": max(16, dy - 6),
                    "color": "rgb(0,255,0)",
                    "font": "bold 12px monospace",
                }
            )

        texts.append(
            {
                "text": f"CV {('running' if cv_busy else cv_status)} rr={len(detections)} drop={cv_drop_count} {cv_latency_ms:.1f}ms",
                "x": 16,
                "y": frame.shape[0] - 32,
                "color": "rgb(0,255,255)" if cv_busy else "rgb(120,255,120)",
                "font": "bold 12px monospace",
            }
        )
        texts.append(
            {
                "text": f"ADB {('running' if adb_busy else adb_status)} sent={adb_sent_count} fail={adb_fail_count}",
                "x": 16,
                "y": frame.shape[0] - 14,
                "color": "rgb(0,255,255)" if adb_busy else "rgb(120,255,120)",
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

        with self._render_lock:
            self._overlay_payload = {"shapes": shapes, "texts": texts}

        render_version = (captured_at, cv_result_seq, adb_busy, adb_action_seq)
        with self._render_lock:
            if render_version == self._last_render_version and self._last_payload is not None:
                return self._last_payload

            ok, encoded = cv2.imencode(".jpg", frame, self._jpeg_opts)
            if not ok:
                return None
            self._last_render_version = render_version
            self._last_payload = encoded.tobytes()
            return self._last_payload


def main() -> None:
    install_signal_handlers()
    config = load_config()

    stream_manager = make_common_stream_manager(shutdown_event)
    stream_manager.start()

    waiting_payload = make_common_waiting_payload()
    payload_provider = JpegPayloadProvider(stream_manager, config)

    web_server = make_common_web_server(
        payload_provider,
        waiting_payload,
        shutdown_event,
        overlay_provider=payload_provider.make_web_payload,
    )

    print(make_common_startup_log())

    try:
        web_server.serve_forever()
    except KeyboardInterrupt:
        request_shutdown("keyboard_interrupt")
    finally:
        web_server.shutdown()
        request_shutdown("server_stop")
        stream_manager.join(timeout=2.0)


if __name__ == "__main__":
    main()
