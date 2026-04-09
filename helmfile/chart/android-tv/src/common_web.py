#!/usr/bin/env python3
import base64
import hashlib
import json
import os
import threading
import urllib.parse
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

import reactivex as rx
from reactivex import operators as ops
from reactivex.scheduler import NewThreadScheduler

from common_adb import get_recent_adb_events


class MjpegWebServer:
    def __init__(
        self,
        host: str,
        port: int,
        stream_fps: float,
        payload_provider: Callable[[], bytes | None],
        waiting_payload: bytes,
        shutdown_event: threading.Event,
        overlay_provider: Callable[[], dict | None] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._stream_fps = stream_fps
        self._payload_provider = payload_provider
        self._waiting_payload = waiting_payload
        self._shutdown_event = shutdown_event
        self._overlay_provider = overlay_provider

        self._server: ThreadingHTTPServer | None = None

    def serve_forever(self) -> None:
        handler = self._build_handler()
        self._server = ThreadingHTTPServer((self._host, self._port), handler)
        self._server.daemon_threads = True
        self._server.serve_forever(poll_interval=0.5)

    def shutdown(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        frame_interval = 1.0 / self._stream_fps
        payload_provider = self._payload_provider
        waiting_payload = self._waiting_payload
        shutdown_event = self._shutdown_event
        overlay_provider = self._overlay_provider
        overlay_enabled = overlay_provider is not None
        page_title = os.getenv("POD_NAME", "ADB Live Stream")
        page_template_path = Path(__file__).with_name("common_web.html")
        page_template_text = page_template_path.read_text(encoding="utf-8")
        print(f"web page_template source=file path={page_template_path}")
        ws_magic = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

        def encode_ws_text_frame(payload: bytes) -> bytes:
            length = len(payload)
            if length <= 125:
                header = bytes([0x81, length])
            elif length <= 65535:
                header = bytes([0x81, 126]) + length.to_bytes(2, "big")
            else:
                header = bytes([0x81, 127]) + length.to_bytes(8, "big")
            return header + payload

        def with_adb_events(payload: dict | None) -> dict:
            base = dict(payload or {})
            base["adb_events"] = get_recent_adb_events()
            return base

        class Handler(BaseHTTPRequestHandler):
            server_version = "ADB-MJPEG/1.2"

            def log_message(self, fmt: str, *args: object) -> None:
                print(f"web client={self.client_address[0]} msg={fmt % args}")

            def _send_html(self) -> None:
                page = (
                    page_template_text.replace("__PAGE_TITLE__", escape(page_title))
                    .replace("__OVERLAY_ENABLED__", "true" if overlay_enabled else "false")
                    .encode("utf-8")
                )
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)

            def _send_overlay_json(self) -> None:
                if overlay_provider is None:
                    self.send_response(HTTPStatus.NOT_FOUND)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b'{"enabled":false}\n')
                    return

                payload = with_adb_events(overlay_provider())
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _is_ws_upgrade(self) -> bool:
                connection = self.headers.get("Connection", "")
                upgrade = self.headers.get("Upgrade", "")
                key = self.headers.get("Sec-WebSocket-Key", "")
                return (
                    "upgrade" in connection.lower()
                    and upgrade.lower() == "websocket"
                    and bool(key)
                )

            def _serve_overlay_ws(self) -> None:
                if overlay_provider is None:
                    self.send_response(HTTPStatus.NOT_FOUND)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b"overlay disabled\n")
                    return

                if not self._is_ws_upgrade():
                    self.send_response(HTTPStatus.BAD_REQUEST)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b"expected websocket upgrade\n")
                    return

                key = self.headers.get("Sec-WebSocket-Key", "")
                accept_raw = hashlib.sha1((key + ws_magic).encode("ascii")).digest()
                accept = base64.b64encode(accept_raw).decode("ascii")

                self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()

                last_payload = b""
                ws_interval = max(0.05, frame_interval)
                ws_stop_event = threading.Event()

                def _pump_ws() -> None:
                    nonlocal last_payload
                    payload_obj = with_adb_events(overlay_provider())
                    payload = json.dumps(payload_obj, separators=(",", ":")).encode("utf-8")
                    if payload == last_payload:
                        return
                    frame = encode_ws_text_frame(payload)
                    try:
                        self.wfile.write(frame)
                        self.wfile.flush()
                        last_payload = payload
                    except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                        ws_stop_event.set()

                scheduler = NewThreadScheduler()
                subscription = (
                    rx.interval(ws_interval, scheduler=scheduler)
                    .pipe(ops.take_while(lambda _i: not shutdown_event.is_set() and not ws_stop_event.is_set()))
                    .subscribe(
                        on_next=lambda _i: _pump_ws(),
                        on_error=lambda _exc: ws_stop_event.set(),
                        on_completed=lambda: ws_stop_event.set(),
                    )
                )
                try:
                    while not shutdown_event.is_set() and not ws_stop_event.is_set():
                        ws_stop_event.wait(timeout=0.05)
                finally:
                    subscription.dispose()

            def _send_mjpeg(self) -> None:
                boundary = b"frame"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0, private, no-transform")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "keep-alive")
                self.end_headers()

                # Bound socket writes so stalled downstream clients do not hang forever.
                # self.connection.settimeout(max(5.0, frame_interval * 4.0))

                mjpeg_stop_event = threading.Event()

                def _pump_mjpeg() -> None:
                    payload = payload_provider()
                    if payload is None:
                        payload = waiting_payload
                    if not payload:
                        return
                    try:
                        self.wfile.write(b"--" + boundary + b"\r\n")
                        self.wfile.write(b"Content-Type: image/jpeg\r\n")
                        self.wfile.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii"))
                        self.wfile.write(payload)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                        mjpeg_stop_event.set()

                scheduler = NewThreadScheduler()
                subscription = (
                    rx.interval(frame_interval, scheduler=scheduler)
                    .pipe(ops.take_while(lambda _i: not shutdown_event.is_set() and not mjpeg_stop_event.is_set()))
                    .subscribe(
                        on_next=lambda _i: _pump_mjpeg(),
                        on_error=lambda _exc: mjpeg_stop_event.set(),
                        on_completed=lambda: mjpeg_stop_event.set(),
                    )
                )
                try:
                    while not shutdown_event.is_set() and not mjpeg_stop_event.is_set():
                        mjpeg_stop_event.wait(timeout=0.05)
                finally:
                    subscription.dispose()

            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                route = parsed.path

                if route in {"/", "/index.html"}:
                    self._send_html()
                    return
                if route.startswith("/stream.mjpg"):
                    self._send_mjpeg()
                    return
                if route.startswith("/overlay.json"):
                    self._send_overlay_json()
                    return
                if route.startswith("/overlay.ws"):
                    self._serve_overlay_ws()
                    return
                if route == "/healthz":
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"ok")
                    return

                self.send_response(HTTPStatus.NOT_FOUND)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"not found\n")

        return Handler
