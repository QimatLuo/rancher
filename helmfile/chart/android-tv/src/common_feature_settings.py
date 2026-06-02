#!/usr/bin/env python3
import re
from dataclasses import dataclass

import cv2
import numpy as np

from common_stream import FrameStreamManager, StreamConfig
from common_web import MjpegWebServer


DEFAULT_MATCH_SIZE = "640x360"
DEFAULT_CAPTURE_FPS = 15.0
DEFAULT_WEB_HOST = "0.0.0.0"
DEFAULT_WEB_PORT = 8000
DEFAULT_WEB_FPS = 8.0
DEFAULT_JPEG_QUALITY = 80
DEFAULT_RECONNECT_DELAY_SEC = 1.0
DEFAULT_STALE_FRAME_SEC = 1.5
DEFAULT_SESSION_ROTATE_SEC = 170.0
DEFAULT_STANDBY_WARMUP_SEC = 8.0
DEFAULT_FIRST_FRAME_TIMEOUT_SEC = 8.0


@dataclass(frozen=True)
class CommonRuntimeSettings:
    capture_width: int
    capture_height: int
    capture_size_text: str
    capture_fps: float
    web_host: str
    web_port: int
    web_fps: float
    jpeg_quality: int
    reconnect_delay_sec: float
    stale_frame_sec: float
    session_rotate_sec: float
    standby_warmup_sec: float
    first_frame_timeout_sec: float


@dataclass(frozen=True)
class CommonToolStateSettings:
    color_tolerance_a: int
    color_tolerance_b: int
    stable_frame_count: int
    stable_color_tolerance: int
    stable_timeout_sec: float
    stable_poll_interval_sec: float


def parse_capture_size(size_text: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)x(\d+)\s*", size_text)
    if not match:
        raise ValueError(f"MATCH_SIZE must be WIDTHxHEIGHT, got: {size_text}")

    width = int(match.group(1))
    height = int(match.group(2))
    if width <= 0 or height <= 0:
        raise ValueError(f"MATCH_SIZE must be positive, got: {size_text}")
    return width, height


def load_common_runtime_settings() -> CommonRuntimeSettings:
    capture_width, capture_height = parse_capture_size(DEFAULT_MATCH_SIZE)

    if DEFAULT_CAPTURE_FPS <= 0:
        raise ValueError("MATCH_STREAM_FPS must be > 0")
    if DEFAULT_WEB_PORT < 1 or DEFAULT_WEB_PORT > 65535:
        raise ValueError("HARVEST_WEB_PORT must be within 1..65535")
    if DEFAULT_WEB_FPS <= 0:
        raise ValueError("HARVEST_WEB_FPS must be > 0")
    if DEFAULT_JPEG_QUALITY < 1 or DEFAULT_JPEG_QUALITY > 100:
        raise ValueError("HARVEST_WEB_JPEG_QUALITY must be within 1..100")
    if DEFAULT_RECONNECT_DELAY_SEC < 0:
        raise ValueError("HARVEST_STREAM_RETRY_DELAY must be >= 0")
    if DEFAULT_STALE_FRAME_SEC <= 0:
        raise ValueError("HARVEST_WEB_STALE_AFTER_SEC must be > 0")
    if DEFAULT_SESSION_ROTATE_SEC <= 0:
        raise ValueError("ADB_SESSION_ROTATE_SEC must be > 0")
    if DEFAULT_STANDBY_WARMUP_SEC < 0:
        raise ValueError("ADB_STANDBY_WARMUP_SEC must be >= 0")
    if DEFAULT_FIRST_FRAME_TIMEOUT_SEC <= 0:
        raise ValueError("ADB_FIRST_FRAME_TIMEOUT_SEC must be > 0")

    return CommonRuntimeSettings(
        capture_width=capture_width,
        capture_height=capture_height,
        capture_size_text=f"{capture_width}x{capture_height}",
        capture_fps=DEFAULT_CAPTURE_FPS,
        web_host=DEFAULT_WEB_HOST,
        web_port=DEFAULT_WEB_PORT,
        web_fps=DEFAULT_WEB_FPS,
        jpeg_quality=DEFAULT_JPEG_QUALITY,
        reconnect_delay_sec=DEFAULT_RECONNECT_DELAY_SEC,
        stale_frame_sec=DEFAULT_STALE_FRAME_SEC,
        session_rotate_sec=DEFAULT_SESSION_ROTATE_SEC,
        standby_warmup_sec=DEFAULT_STANDBY_WARMUP_SEC,
        first_frame_timeout_sec=DEFAULT_FIRST_FRAME_TIMEOUT_SEC,
    )


_COMMON_RUNTIME_SETTINGS = load_common_runtime_settings()


_COMMON_TOOL_STATE_SETTINGS = CommonToolStateSettings(
    color_tolerance_a=60,
    color_tolerance_b=18,
    stable_frame_count=20,
    stable_color_tolerance=6,
    stable_timeout_sec=10.0,
    stable_poll_interval_sec=0.1,
)


def get_common_runtime_settings() -> CommonRuntimeSettings:
    return _COMMON_RUNTIME_SETTINGS


def get_common_tool_state_settings() -> CommonToolStateSettings:
    return _COMMON_TOOL_STATE_SETTINGS


def make_common_stream_config() -> StreamConfig:
    common = get_common_runtime_settings()
    return StreamConfig(
        capture_width=common.capture_width,
        capture_height=common.capture_height,
        capture_size_text=common.capture_size_text,
        capture_fps=common.capture_fps,
        reconnect_delay_sec=common.reconnect_delay_sec,
        session_rotate_sec=common.session_rotate_sec,
        standby_warmup_sec=common.standby_warmup_sec,
        first_frame_timeout_sec=common.first_frame_timeout_sec,
    )


def make_common_stream_manager(shutdown_event) -> FrameStreamManager:
    return FrameStreamManager(make_common_stream_config(), shutdown_event)


def make_common_waiting_payload() -> bytes:
    common = get_common_runtime_settings()
    waiting = np.full((common.capture_height, common.capture_width, 3), 16, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", waiting, [int(cv2.IMWRITE_JPEG_QUALITY), common.jpeg_quality])
    return encoded.tobytes() if ok else b""


def make_common_web_server(
    payload_provider,
    waiting_payload: bytes,
    shutdown_event,
    overlay_provider=None,
) -> MjpegWebServer:
    common = get_common_runtime_settings()
    return MjpegWebServer(
        host=common.web_host,
        port=common.web_port,
        stream_fps=common.web_fps,
        payload_provider=payload_provider,
        waiting_payload=waiting_payload,
        shutdown_event=shutdown_event,
        overlay_provider=overlay_provider,
    )


def make_common_startup_log(extra: str = "") -> str:
    common = get_common_runtime_settings()
    text = (
        f"web listen={common.web_host}:{common.web_port} stream=/stream.mjpg "
        f"capture={common.capture_size_text} capture_fps={common.capture_fps} "
        f"rotate={common.session_rotate_sec}s warmup={common.standby_warmup_sec}s"
    )
    if extra:
        return f"{text} {extra}"
    return text
