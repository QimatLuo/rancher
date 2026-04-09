#!/usr/bin/env python3
import subprocess
import threading
import time
from dataclasses import dataclass

import numpy as np
import reactivex as rx
from reactivex import operators as ops
from reactivex.disposable import Disposable
from reactivex.scheduler import NewThreadScheduler


@dataclass
class StreamConfig:
    capture_width: int
    capture_height: int
    capture_size_text: str
    capture_fps: float
    reconnect_delay_sec: float
    session_rotate_sec: float
    standby_warmup_sec: float
    first_frame_timeout_sec: float


class FrameBuffer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._timestamp = 0.0
        self._frame: np.ndarray | None = None

    def update(self, frame: np.ndarray, captured_at: float) -> None:
        with self._lock:
            self._timestamp = captured_at
            self._frame = frame

    def snapshot(self) -> tuple[float, np.ndarray] | None:
        with self._lock:
            if self._frame is None:
                return None
            return self._timestamp, self._frame.copy()


def read_exact(stream, byte_count: int) -> bytes:
    data = bytearray()
    while len(data) < byte_count:
        block = stream.read(byte_count - len(data))
        if not block:
            break
        data.extend(block)
    return bytes(data)


class CaptureSession:
    def __init__(self, config: StreamConfig, name: str, shutdown_event: threading.Event) -> None:
        self._config = config
        self._name = name
        self._shutdown_event = shutdown_event

        self._frame_lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._frame_ts = 0.0

        self._stop_event = threading.Event()
        self._first_frame_event = threading.Event()
        self._done_event = threading.Event()

        self._run_lock = threading.Lock()
        self._run_scheduler = NewThreadScheduler()
        self._run_subscription: Disposable | None = None
        self._adb_proc: subprocess.Popen | None = None
        self._ffmpeg_proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()

        self.start_monotonic = 0.0
        self.reason = ""

    @property
    def name(self) -> str:
        return self._name

    def start(self) -> None:
        with self._run_lock:
            if self._run_subscription is not None:
                raise RuntimeError(f"session {self._name} already started")
        self.start_monotonic = time.monotonic()
        with self._run_lock:
            self._run_subscription = (
                rx.just(0)
                .pipe(ops.subscribe_on(self._run_scheduler))
                .subscribe(
                    on_next=lambda _i: self._run(),
                    on_error=self._on_run_error,
                    on_completed=self._on_run_completed,
                )
            )

    def stop(self) -> None:
        self._stop_event.set()
        with self._proc_lock:
            for proc in (self._ffmpeg_proc, self._adb_proc):
                if proc is not None and proc.poll() is None:
                    proc.terminate()

    def join(self, timeout: float | None = None) -> None:
        self._done_event.wait(timeout=timeout)

    def _clear_run_subscription(self) -> None:
        with self._run_lock:
            self._run_subscription = None

    def _on_run_error(self, exc: Exception) -> None:
        self.reason = f"error:{type(exc).__name__}"
        self._clear_run_subscription()
        self._done_event.set()

    def _on_run_completed(self) -> None:
        self._clear_run_subscription()

    def has_first_frame(self) -> bool:
        return self._first_frame_event.is_set()

    def wait_first_frame(self, timeout: float) -> bool:
        return self._first_frame_event.wait(timeout=timeout)

    def is_done(self) -> bool:
        return self._done_event.is_set()

    def latest_frame(self) -> tuple[float, np.ndarray] | None:
        with self._frame_lock:
            if self._frame is None:
                return None
            return self._frame_ts, self._frame.copy()

    def _set_procs(self, adb_proc: subprocess.Popen | None, ffmpeg_proc: subprocess.Popen | None) -> None:
        with self._proc_lock:
            self._adb_proc = adb_proc
            self._ffmpeg_proc = ffmpeg_proc

    def _cleanup_procs(self) -> None:
        with self._proc_lock:
            procs = [self._ffmpeg_proc, self._adb_proc]
            self._ffmpeg_proc = None
            self._adb_proc = None

        for proc in procs:
            if proc is not None and proc.poll() is None:
                proc.terminate()
        for proc in procs:
            if proc is None:
                continue
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _run(self) -> None:
        frame_bytes = self._config.capture_width * self._config.capture_height * 3
        adb_cmd = [
            "adb",
            "exec-out",
            "screenrecord",
            "--output-format",
            "h264",
            "--size",
            self._config.capture_size_text,
            "-",
        ]
        ffmpeg_cmd = [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "h264",
            "-i",
            "pipe:0",
            "-vf",
            f"fps={self._config.capture_fps}",
            "-pix_fmt",
            "bgr24",
            "-f",
            "rawvideo",
            "pipe:1",
        ]

        try:
            adb_proc = subprocess.Popen(adb_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            if adb_proc.stdout is None:
                adb_proc.kill()
                raise RuntimeError("failed to create adb stdout pipe")

            ffmpeg_proc = subprocess.Popen(
                ffmpeg_cmd,
                stdin=adb_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            adb_proc.stdout.close()

            if ffmpeg_proc.stdout is None:
                ffmpeg_proc.kill()
                adb_proc.kill()
                raise RuntimeError("failed to create ffmpeg stdout pipe")

            self._set_procs(adb_proc, ffmpeg_proc)

            while not self._shutdown_event.is_set() and not self._stop_event.is_set():
                raw = read_exact(ffmpeg_proc.stdout, frame_bytes)
                if len(raw) != frame_bytes:
                    if self._stop_event.is_set() or self._shutdown_event.is_set():
                        self.reason = "stopped"
                    else:
                        self.reason = "video stream ended"
                    return

                frame = np.frombuffer(raw, dtype=np.uint8).reshape(
                    (self._config.capture_height, self._config.capture_width, 3)
                )
                ts = time.monotonic()
                with self._frame_lock:
                    self._frame = frame
                    self._frame_ts = ts
                self._first_frame_event.set()

            self.reason = "stopped"
        except BaseException as exc:
            self.reason = str(exc)
        finally:
            self._cleanup_procs()
            self._done_event.set()


class FrameStreamManager:
    def __init__(self, config: StreamConfig, shutdown_event: threading.Event) -> None:
        self._config = config
        self._shutdown_event = shutdown_event
        self._frame_buffer = FrameBuffer()
        self._run_lock = threading.Lock()
        self._run_scheduler = NewThreadScheduler()
        self._run_subscription: Disposable | None = None
        self._done_event = threading.Event()

    def start(self) -> None:
        with self._run_lock:
            if self._run_subscription is not None:
                return
            self._done_event.clear()
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
        self._done_event.wait(timeout=timeout)

    def _clear_run_subscription(self) -> None:
        with self._run_lock:
            self._run_subscription = None

    def _on_run_error(self, exc: Exception) -> None:
        print(f"capture loop_error type={type(exc).__name__} msg={exc}")
        self._clear_run_subscription()
        self._done_event.set()

    def _on_run_completed(self) -> None:
        self._clear_run_subscription()
        self._done_event.set()

    def snapshot(self) -> tuple[float, np.ndarray] | None:
        return self._frame_buffer.snapshot()

    def _start_session(self, name: str) -> CaptureSession:
        session = CaptureSession(self._config, name=name, shutdown_event=self._shutdown_event)
        session.start()
        print(f"capture session_start name={name}")
        return session

    def _switch_active(self, active: CaptureSession, standby: CaptureSession) -> CaptureSession:
        latest = standby.latest_frame()
        if latest is not None:
            ts, frame = latest
            self._frame_buffer.update(frame, ts)
        print(f"capture session_switch from={active.name} to={standby.name}")
        active.stop()
        active.join(timeout=1.5)
        return standby

    def _run_manager(self) -> None:
        counter = 1
        active = self._start_session(name=f"s{counter}")
        if not active.wait_first_frame(timeout=self._config.first_frame_timeout_sec):
            print(
                "capture first_frame_timeout "
                f"session={active.name} timeout={self._config.first_frame_timeout_sec}s continuing=1"
            )

        standby: CaptureSession | None = None
        last_forwarded_ts = 0.0

        while not self._shutdown_event.is_set():
            now = time.monotonic()

            active_latest = active.latest_frame()
            if active_latest is not None:
                ts, frame = active_latest
                if ts > last_forwarded_ts:
                    self._frame_buffer.update(frame, ts)
                    last_forwarded_ts = ts

            prewarm_at = self._config.session_rotate_sec - self._config.standby_warmup_sec
            if standby is None and (now - active.start_monotonic) >= max(0.0, prewarm_at):
                counter += 1
                standby = self._start_session(name=f"s{counter}")

            if standby is not None:
                if standby.has_first_frame():
                    active = self._switch_active(active, standby)
                    standby = None
                    last_forwarded_ts = 0.0
                    continue

                if standby.is_done() and not standby.has_first_frame():
                    print(f"capture standby_failed name={standby.name} reason={standby.reason}")
                    standby = None

            if active.is_done():
                reason = active.reason or "unknown"
                print(f"capture reconnect reason={reason} active={active.name}")

                if standby is not None and standby.has_first_frame():
                    active = self._switch_active(active, standby)
                    standby = None
                    last_forwarded_ts = 0.0
                    continue

                if standby is not None:
                    standby.stop()
                    standby.join(timeout=1.0)
                    standby = None

                if self._config.reconnect_delay_sec > 0:
                    self._shutdown_event.wait(timeout=self._config.reconnect_delay_sec)
                    if self._shutdown_event.is_set():
                        break

                counter += 1
                active = self._start_session(name=f"s{counter}")
                last_forwarded_ts = 0.0

                if not active.wait_first_frame(timeout=self._config.first_frame_timeout_sec):
                    print(
                        "capture first_frame_timeout "
                        f"session={active.name} timeout={self._config.first_frame_timeout_sec}s continuing=1"
                    )

            self._shutdown_event.wait(timeout=0.01)

        if standby is not None:
            standby.stop()
            standby.join(timeout=1.0)
        active.stop()
        active.join(timeout=1.0)

    def _run_loop(self) -> None:
        while not self._shutdown_event.is_set():
            try:
                self._run_manager()
                return
            except BaseException as exc:
                if self._shutdown_event.is_set():
                    return
                print(f"capture manager_restart reason={exc}")
                if self._config.reconnect_delay_sec > 0:
                    self._shutdown_event.wait(timeout=self._config.reconnect_delay_sec)
