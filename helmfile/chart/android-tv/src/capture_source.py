import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Protocol

import cv2
import numpy as np
import reactivex as rx

from adb_source import AdbMjpegSource, AdbSourceConfig


class FrameSource(Protocol):
    def frames(self) -> rx.Observable:
        ...


@dataclass
class ScreencapSourceConfig:
    stream_fps: float
    jpeg_quality: int


class AdbScreencapPollingSource:
    _PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

    def __init__(self, config: ScreencapSourceConfig, stop_event: threading.Event) -> None:
        self._config = config
        self._stop_event = stop_event

    def _build_adb_cmd(self) -> list[str]:
        return ["adb", "exec-out", "screencap", "-p"]

    def _capture_png(self) -> bytes:
        return subprocess.check_output(
            self._build_adb_cmd(),
            stderr=subprocess.DEVNULL,
        )

    def _decode_png(self, png: bytes):
        arr = np.frombuffer(png, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)

    def _png_to_jpeg(self, png: bytes) -> bytes | None:
        image = self._decode_png(png)

        # Fallback for rare devices that still mangle line endings on screencap output.
        if image is None and not png.startswith(self._PNG_SIGNATURE):
            image = self._decode_png(png.replace(b"\r\n", b"\n"))

        if image is None:
            return None

        ok, encoded = cv2.imencode(
            ".jpg",
            image,
            [int(cv2.IMWRITE_JPEG_QUALITY), self._config.jpeg_quality],
        )
        if not ok:
            return None

        return encoded.tobytes()

    def frames(self) -> rx.Observable:
        def _subscribe(observer, _scheduler):
            subscription_stop = threading.Event()

            def _run() -> None:
                frame_interval = 1.0 / max(self._config.stream_fps, 0.1)

                try:
                    while not self._stop_event.is_set() and not subscription_stop.is_set():
                        started_at = time.monotonic()

                        try:
                            png = self._capture_png()
                            frame = self._png_to_jpeg(png)
                            if frame is not None:
                                observer.on_next(frame)
                            else:
                                print("[source] failed to decode/encode screencap frame")
                        except subprocess.CalledProcessError:
                            print("[source] adb screencap command failed")
                        except Exception as exc:  # pragma: no cover
                            print(f"[source] unexpected error: {exc}")

                        elapsed = time.monotonic() - started_at
                        wait_sec = frame_interval - elapsed
                        if wait_sec > 0:
                            time.sleep(wait_sec)

                    observer.on_completed()
                except Exception as exc:  # pragma: no cover
                    observer.on_error(exc)

            worker = threading.Thread(target=_run, daemon=True)
            worker.start()

            def _dispose() -> None:
                subscription_stop.set()

            return _dispose

        return rx.create(_subscribe)


def build_source(
    stop_event: threading.Event,
    stream_fps: float,
) -> tuple[FrameSource, str]:
    mode = os.getenv("ADB_CAPTURE_MODE", "screencap").strip().lower()

    if mode == "screenrecord":
        source = AdbMjpegSource(
            AdbSourceConfig(
                stream_fps=stream_fps,
                adb_chunk_bytes=int(os.getenv("ADB_CHUNK_BYTES", "32768")),
                ffmpeg_jpeg_q=os.getenv("FFMPEG_JPEG_Q", "5"),
            ),
            stop_event,
        )
        return source, mode

    if mode == "screencap":
        source = AdbScreencapPollingSource(
            ScreencapSourceConfig(
                stream_fps=stream_fps,
                jpeg_quality=max(10, min(100, int(os.getenv("JPEG_QUALITY", "75")))),
            ),
            stop_event,
        )
        return source, mode

    raise ValueError(
        f"Unsupported ADB_CAPTURE_MODE '{mode}'. Use 'screenrecord' or 'screencap'."
    )
