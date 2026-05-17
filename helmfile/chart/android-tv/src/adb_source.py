#!/usr/bin/env python3
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable

import reactivex as rx


@dataclass
class AdbSourceConfig:
    stream_fps: float
    adb_chunk_bytes: int
    ffmpeg_jpeg_q: str
    adb_record_size: str = os.getenv("ADB_RECORD_SIZE", "640x360")


class AdbMjpegSource:
    def __init__(self, config: AdbSourceConfig, stop_event: threading.Event) -> None:
        self._config = config
        self._stop_event = stop_event

    def _build_adb_cmd(self) -> list[str]:
        return [
            "adb",
            "exec-out",
            "screenrecord",
            "--size",
            self._config.adb_record_size,
            "--output-format=h264",
            "-",
        ]

    def _build_ffmpeg_cmd(self) -> list[str]:
        return [
            "ffmpeg",
            "-loglevel",
            "error",
            "-probesize",
            "32",
            "-analyzeduration",
            "0",
            "-f",
            "h264",
            "-i",
            "pipe:0",
            "-an",
            "-vf",
            f"fps={self._config.stream_fps}",
            "-f",
            "mjpeg",
            "-q:v",
            self._config.ffmpeg_jpeg_q,
            "pipe:1",
        ]

    def _offer_chunk(self, write_q: queue.Queue[bytes], chunk: bytes) -> None:
        try:
            write_q.put_nowait(chunk)
            return
        except queue.Full:
            pass

        # Keep source ingest non-blocking by dropping stale queue data.
        while not self._stop_event.is_set():
            try:
                write_q.get_nowait()
            except queue.Empty:
                break
            try:
                write_q.put_nowait(chunk)
                return
            except queue.Full:
                continue

    def _feed_ffmpeg_stdin(
        self,
        write_q: queue.Queue[bytes],
        ffmpeg_proc: subprocess.Popen[bytes],
        session_stop: threading.Event,
    ) -> None:
        stdin = ffmpeg_proc.stdin
        if stdin is None:
            return

        while not self._stop_event.is_set() and not session_stop.is_set():
            try:
                chunk = write_q.get(timeout=0.2)
            except queue.Empty:
                continue

            try:
                stdin.write(chunk)
                stdin.flush()
            except (BrokenPipeError, OSError):
                session_stop.set()
                break

        try:
            stdin.close()
        except OSError:
            pass

    def _parse_mjpeg_frames(
        self,
        ffmpeg_proc: subprocess.Popen[bytes],
        session_stop: threading.Event,
        emit: Callable[[bytes], None],
    ) -> None:
        stdout = ffmpeg_proc.stdout
        if stdout is None:
            return

        buffer = bytearray()
        while not self._stop_event.is_set() and not session_stop.is_set():
            chunk = stdout.read1(65536)
            if not chunk:
                session_stop.set()
                break

            buffer.extend(chunk)

            while True:
                soi = buffer.find(b"\xff\xd8")
                if soi < 0:
                    if len(buffer) > 1024 * 1024:
                        del buffer[:-2]
                    break

                eoi = buffer.find(b"\xff\xd9", soi + 2)
                if eoi < 0:
                    if soi > 0:
                        del buffer[:soi]
                    break

                frame = bytes(buffer[soi : eoi + 2])
                emit(frame)
                del buffer[: eoi + 2]

    def _run_single_session(self, emit: Callable[[bytes], None], session_stop: threading.Event) -> None:
        adb_cmd = self._build_adb_cmd()
        ffmpeg_cmd = self._build_ffmpeg_cmd()

        print(f"[source] start adb: {' '.join(adb_cmd)}")
        adb_proc = subprocess.Popen(adb_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        print(f"[source] start ffmpeg: {' '.join(ffmpeg_cmd)}")
        ffmpeg_proc = subprocess.Popen(
            ffmpeg_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        write_q: queue.Queue[bytes] = queue.Queue(maxsize=32)

        feeder = threading.Thread(
            target=self._feed_ffmpeg_stdin,
            args=(write_q, ffmpeg_proc, session_stop),
            daemon=True,
        )
        parser = threading.Thread(
            target=self._parse_mjpeg_frames,
            args=(ffmpeg_proc, session_stop, emit),
            daemon=True,
        )
        feeder.start()
        parser.start()

        try:
            while not self._stop_event.is_set() and not session_stop.is_set():
                stream = adb_proc.stdout
                if stream is None:
                    break

                chunk = stream.read1(self._config.adb_chunk_bytes)
                if not chunk:
                    session_stop.set()
                    break

                self._offer_chunk(write_q, chunk)

                if adb_proc.poll() is not None or ffmpeg_proc.poll() is not None:
                    session_stop.set()
                    break
        finally:
            session_stop.set()
            feeder.join(timeout=1)
            parser.join(timeout=1)

            for proc in (adb_proc, ffmpeg_proc):
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        proc.kill()

    def frames(self) -> rx.Observable:
        def _subscribe(observer, _scheduler):
            subscription_stop = threading.Event()

            def _emit(frame: bytes) -> None:
                if self._stop_event.is_set() or subscription_stop.is_set():
                    return
                observer.on_next(frame)

            def _run() -> None:
                try:
                    while not self._stop_event.is_set() and not subscription_stop.is_set():
                        session_stop = threading.Event()
                        self._run_single_session(_emit, session_stop)
                        if not self._stop_event.is_set() and not subscription_stop.is_set():
                            time.sleep(1)
                    observer.on_completed()
                except Exception as exc:  # pragma: no cover
                    observer.on_error(exc)

            thread = threading.Thread(target=_run, daemon=True)
            thread.start()

            def _dispose() -> None:
                subscription_stop.set()

            return _dispose

        return rx.create(_subscribe)
