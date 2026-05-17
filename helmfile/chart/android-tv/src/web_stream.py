#!/usr/bin/env python3
import os
import threading
import time
from dataclasses import dataclass
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

import reactivex as rx
from reactivex import operators as ops
from reactivex.scheduler import ThreadPoolScheduler
from reactivex.subject import Subject


@dataclass
class WebStreamConfig:
    host: str = os.getenv("WEB_HOST", "0.0.0.0")
    port: int = int(os.getenv("WEB_PORT", "8000"))


class LatestFrameStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._frame: Optional[bytes] = None
        self._ts = 0.0
        self._seq = 0

    def set(self, frame: bytes) -> None:
        with self._cond:
            self._frame = frame
            self._ts = time.time()
            self._seq += 1
            self._cond.notify_all()

    def get(self) -> tuple[Optional[bytes], float]:
        with self._lock:
            return self._frame, self._ts

    def wait_next(self, last_seq: int, timeout: float = 0.5) -> tuple[bytes | None, float, int] | None:
        with self._cond:
            if self._seq <= last_seq:
                self._cond.wait(timeout=timeout)
            if self._seq <= last_seq:
                return None
            return self._frame, self._ts, self._seq


class WebMjpegServer:
    def __init__(
        self,
        config: WebStreamConfig,
        frame_store: LatestFrameStore,
        stop_event: threading.Event,
    ) -> None:
        self._config = config
        self._frame_store = frame_store
        self._stop_event = stop_event

    def _build_handler(self):
        frame_store = self._frame_store
        stop_event = self._stop_event
        page_title = os.getenv("POD_NAME", "ADB Screen Stream")
        template_path = Path(__file__).with_name("web_stream.html")
        template_text = template_path.read_text(encoding="utf-8")

        class Handler(BaseHTTPRequestHandler):
            server_version = "RX-ADB/1.0"

            def log_message(self, fmt: str, *args: object) -> None:
                print(f"[web] {self.client_address[0]} {fmt % args}")

            def _send_html(self) -> None:
                html = template_text.replace("__PAGE_TITLE__", escape(page_title)).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(html)

            def _send_health(self) -> None:
                frame, ts = frame_store.get()
                fresh = frame is not None and (time.time() - ts) < 10
                body = ("ok" if fresh else "starting").encode("utf-8")
                self.send_response(HTTPStatus.OK if fresh else HTTPStatus.SERVICE_UNAVAILABLE)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_mjpeg(self) -> None:
                boundary = b"frame"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()

                self.connection.settimeout(5.0)
                done = threading.Event()

                def write_frame(frame: bytes | None) -> None:
                    if not frame:
                        return
                    try:
                        self.wfile.write(b"--" + boundary + b"\r\n")
                        self.wfile.write(b"Content-Type: image/jpeg\r\n")
                        self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii"))
                        self.wfile.write(frame)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                        done.set()

                write_scheduler = ThreadPoolScheduler(max_workers=1)
                frame_subject: Subject[bytes] = Subject()
                subscription = (
                    frame_subject
                    .pipe(
                        ops.map(
                            lambda frame: rx.from_callable(
                                lambda f=frame: write_frame(f)
                            ).pipe(ops.subscribe_on(write_scheduler))
                        )
                    )
                    # exhaustMap behavior: while one write is in progress, drop new frames.
                    .pipe(ops.exclusive())
                    .subscribe(
                        on_next=lambda _: None,
                        on_error=lambda _: done.set(),
                        on_completed=lambda: done.set(),
                    )
                )
                try:
                    last_seq = 0
                    while not stop_event.is_set() and not done.is_set():
                        update = frame_store.wait_next(last_seq, timeout=0.5)
                        if update is None:
                            continue
                        frame, _ts, last_seq = update
                        if frame is None:
                            continue
                        frame_subject.on_next(frame)
                finally:
                    frame_subject.on_completed()
                    subscription.dispose()

            def do_GET(self) -> None:
                if self.path in {"/", "/index.html"}:
                    self._send_html()
                    return
                if self.path.startswith("/stream.mjpg"):
                    self._send_mjpeg()
                    return
                if self.path == "/healthz":
                    self._send_health()
                    return

                self.send_response(HTTPStatus.NOT_FOUND)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"not found\n")

        return Handler

    def serve_forever(self) -> None:
        server = ThreadingHTTPServer((self._config.host, self._config.port), self._build_handler())
        server.daemon_threads = True

        print(f"[web] listening on {self._config.host}:{self._config.port}")
        try:
            while not self._stop_event.is_set():
                server.handle_request()
        finally:
            server.server_close()
