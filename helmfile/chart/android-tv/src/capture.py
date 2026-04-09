#!/usr/bin/env python3
import os
import re
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import cv2
import reactivex as rx
from reactivex import operators as ops
from reactivex.scheduler import NewThreadScheduler

from common_stream import FrameStreamManager, StreamConfig


shutdown_event = threading.Event()
DEFAULT_MATCH_SIZE = "640x360"
DEFAULT_CAPTURE_FPS = 15.0
DEFAULT_RECONNECT_DELAY_SEC = 1.0
DEFAULT_SESSION_ROTATE_SEC = 170.0
DEFAULT_STANDBY_WARMUP_SEC = 8.0
DEFAULT_FIRST_FRAME_TIMEOUT_SEC = 8.0
DEFAULT_OUTPUT_DIR = "/tmp"


@dataclass
class RuntimeConfig:
	capture_width: int
	capture_height: int
	capture_size_text: str
	capture_fps: float
	reconnect_delay_sec: float
	session_rotate_sec: float
	standby_warmup_sec: float
	first_frame_timeout_sec: float
	output_dir: str


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


def parse_capture_size(size_text: str) -> tuple[int, int]:
	match = re.fullmatch(r"\s*(\d+)x(\d+)\s*", size_text)
	if not match:
		raise ValueError(f"MATCH_SIZE must be WIDTHxHEIGHT, got: {size_text}")

	width = int(match.group(1))
	height = int(match.group(2))
	if width <= 0 or height <= 0:
		raise ValueError(f"MATCH_SIZE must be positive, got: {size_text}")
	return width, height


def load_config() -> RuntimeConfig:
	capture_size_text = DEFAULT_MATCH_SIZE
	capture_width, capture_height = parse_capture_size(capture_size_text)

	capture_fps = DEFAULT_CAPTURE_FPS
	reconnect_delay_sec = DEFAULT_RECONNECT_DELAY_SEC
	session_rotate_sec = DEFAULT_SESSION_ROTATE_SEC
	standby_warmup_sec = DEFAULT_STANDBY_WARMUP_SEC
	first_frame_timeout_sec = DEFAULT_FIRST_FRAME_TIMEOUT_SEC
	output_dir = DEFAULT_OUTPUT_DIR

	if capture_fps <= 0:
		raise ValueError("MATCH_STREAM_FPS must be > 0")
	if reconnect_delay_sec < 0:
		raise ValueError("HARVEST_STREAM_RETRY_DELAY must be >= 0")
	if session_rotate_sec <= 0:
		raise ValueError("ADB_SESSION_ROTATE_SEC must be > 0")
	if standby_warmup_sec < 0:
		raise ValueError("ADB_STANDBY_WARMUP_SEC must be >= 0")
	if first_frame_timeout_sec <= 0:
		raise ValueError("ADB_FIRST_FRAME_TIMEOUT_SEC must be > 0")

	return RuntimeConfig(
		capture_width=capture_width,
		capture_height=capture_height,
		capture_size_text=f"{capture_width}x{capture_height}",
		capture_fps=capture_fps,
		reconnect_delay_sec=reconnect_delay_sec,
		session_rotate_sec=session_rotate_sec,
		standby_warmup_sec=standby_warmup_sec,
		first_frame_timeout_sec=first_frame_timeout_sec,
		output_dir=output_dir,
	)


def wait_first_frame(manager: FrameStreamManager, timeout_sec: float) -> tuple[float, object]:
	result_lock = threading.Lock()
	result: tuple[float, object] | None = None
	done_event = threading.Event()

	def _poll_snapshot() -> None:
		nonlocal result
		if shutdown_event.is_set():
			done_event.set()
			return
		snapshot = manager.snapshot()
		if snapshot is not None:
			with result_lock:
				result = snapshot
			done_event.set()

	scheduler = NewThreadScheduler()
	subscription = (
		rx.interval(0.01, scheduler=scheduler)
		.pipe(ops.take_while(lambda _i: not done_event.is_set()))
		.subscribe(
			on_next=lambda _i: _poll_snapshot(),
			on_error=lambda _exc: done_event.set(),
			on_completed=lambda: done_event.set(),
		)
	)
	try:
		if done_event.wait(timeout=max(0.1, timeout_sec)):
			if shutdown_event.is_set():
				raise RuntimeError("shutdown requested before first frame")
			with result_lock:
				if result is not None:
					return result
		raise TimeoutError(f"first frame timeout after {timeout_sec:.1f}s")
	finally:
		done_event.set()
		subscription.dispose()


def build_output_path(output_dir: str) -> str:
	os.makedirs(output_dir, exist_ok=True)
	ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
	return os.path.join(output_dir, f"frame_{ts}.jpg")


def main() -> None:
	install_signal_handlers()
	config = load_config()

	stream_config = StreamConfig(
		capture_width=config.capture_width,
		capture_height=config.capture_height,
		capture_size_text=config.capture_size_text,
		capture_fps=config.capture_fps,
		reconnect_delay_sec=config.reconnect_delay_sec,
		session_rotate_sec=config.session_rotate_sec,
		standby_warmup_sec=config.standby_warmup_sec,
		first_frame_timeout_sec=config.first_frame_timeout_sec,
	)

	stream_manager = FrameStreamManager(stream_config, shutdown_event)
	stream_manager.start()

	try:
		captured_at, frame = wait_first_frame(stream_manager, config.first_frame_timeout_sec)
		output_path = build_output_path(config.output_dir)

		ok = cv2.imwrite(output_path, frame)
		if not ok:
			raise RuntimeError(f"failed to write image: {output_path}")

		print(
			f"saved first frame path={output_path} "
			f"captured_monotonic={captured_at:.3f} size={config.capture_size_text}"
		)
	finally:
		request_shutdown("capture_done")
		stream_manager.join(timeout=2.0)


if __name__ == "__main__":
	main()
