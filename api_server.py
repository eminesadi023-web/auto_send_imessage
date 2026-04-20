from __future__ import annotations

import json
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from config import AppConfig
from imessage_sender import send_imessages


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _build_runtime_options(app_config: AppConfig) -> dict[str, Any]:
    return {
        "delivery_check_timeout_seconds": app_config.imessage_delivery_check_timeout_seconds,
        "delivery_check_interval_seconds": app_config.imessage_delivery_check_interval_seconds,
    }


def _parse_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    content_length = int(handler.headers.get("Content-Length", 0))
    if content_length == 0:
        return {}
    body = handler.rfile.read(content_length)
    return json.loads(body.decode("utf-8"))


def _send_json(handler: BaseHTTPRequestHandler, status: HTTPStatus, payload: dict[str, Any]) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.end_headers()
    handler.wfile.write(_json_bytes(payload))


def create_app_handler(app_config: AppConfig) -> type[BaseHTTPRequestHandler]:
    runtime_options = _build_runtime_options(app_config)

    class AppHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path == "/send":
                self._handle_send(is_batch=False)
            elif self.path == "/send/batch":
                self._handle_send(is_batch=True)
            else:
                _send_json(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def _handle_send(self, *, is_batch: bool) -> None:
            try:
                payload = _parse_json_body(self)
                message = str(payload.get("message", "") or "").strip() or app_config.imessage_text
                image = payload.get("image")
                if image is None:
                    image = app_config.imessage_image_path
                else:
                    image = str(image).strip() or None
                batch_date = str(payload.get("batch_date", "") or "").strip() or None
                if is_batch:
                    if image:
                        raise ValueError("批量发送不支持图片")
                    recipients = payload.get("recipients")
                    if not isinstance(recipients, list):
                        raise ValueError("`recipients` 必须是数组")
                    prepared = [str(item).strip() for item in recipients if str(item).strip()]
                    if not prepared:
                        raise ValueError("至少需要一个有效收件人")
                    results = send_imessages(
                        prepared,
                        message=message,
                        attachment=None,
                        batch_date=batch_date,
                        **runtime_options,
                    )
                else:
                    recipient = str(payload.get("recipient", "") or "").strip()
                    if not recipient:
                        raise ValueError("`recipient` 不能为空")
                    # For single send, use direct send without risk control
                    from imessage_sender import _send_imessage_direct
                    _send_imessage_direct(recipient, message, image)
                    results = [{"phone": recipient, "status": "sent", "detail": "Message sent", "error": None}]
                _send_json(self, HTTPStatus.OK, {"results": results})
            except Exception as exc:
                _send_json(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def log_message(self, format, *args):
            # 禁用默认日志输出
            pass

    return AppHandler


def run_api_server(app_config: AppConfig) -> None:
    handler_class = create_app_handler(app_config)
    server = ThreadingHTTPServer((app_config.api_host, app_config.api_port), handler_class)
    print(f"API server running on http://{app_config.api_host}:{app_config.api_port}")
    server.serve_forever()