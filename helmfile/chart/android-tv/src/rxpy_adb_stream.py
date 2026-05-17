#!/usr/bin/env python3
import os
import signal
import threading

from adb_source import AdbMjpegSource, AdbSourceConfig
from web_stream import LatestFrameStore, WebMjpegServer, WebStreamConfig


def _install_signal_handlers(stop_event: threading.Event) -> None:
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())


def main() -> None:
    stop_event = threading.Event()
    _install_signal_handlers(stop_event)

    stream_fps = float(os.getenv("STREAM_FPS", "10"))
    source_cfg = AdbSourceConfig(
        stream_fps=stream_fps,
        adb_chunk_bytes=int(os.getenv("ADB_CHUNK_BYTES", "32768")),
        ffmpeg_jpeg_q=os.getenv("FFMPEG_JPEG_Q", "5"),
    )
    web_cfg = WebStreamConfig(
        host=os.getenv("WEB_HOST", "0.0.0.0"),
        port=int(os.getenv("WEB_PORT", "8000")),
    )

    frame_store = LatestFrameStore()
    source = AdbMjpegSource(source_cfg, stop_event)
    web = WebMjpegServer(web_cfg, frame_store, stop_event)

    subscription = source.frames().subscribe(
        on_next=frame_store.set,
        on_error=lambda exc: (print(f"[main] source error: {exc}"), stop_event.set()),
        on_completed=lambda: stop_event.set(),
    )

    try:
        web.serve_forever()
    finally:
        stop_event.set()
        subscription.dispose()


if __name__ == "__main__":
    main()
