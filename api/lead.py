from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler

import main


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/lead":
            return self._respond(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Маршрут не найден."})
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return self._respond(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"ok": False, "error": "Неверный формат заявки."})
        main.init_db()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16_000:
                return self._respond(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Проверьте заполнение формы."})
            payload = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            return self._respond(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Не удалось прочитать заявку. Попробуйте ещё раз."})
        if not isinstance(payload, dict):
            return self._respond(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Проверьте заполнение формы."})
        forwarded = self.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        remote_ip = forwarded or self.headers.get("x-real-ip", "unknown")
        status, data, headers = main.process_lead(payload, remote_ip)
        return self._respond(status, data, headers)

    def _respond(self, status, data, extra_headers=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

