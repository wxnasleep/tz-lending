from __future__ import annotations

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler

import main


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
        supplied = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if len(expected) < 16 or not hmac.compare_digest(supplied, expected):
            return self._respond(403, {"ok": False})
        main.init_db()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1_000_000:
                return self._respond(400, {"ok": False})
            update = json.loads(self.rfile.read(length))
            if not isinstance(update, dict):
                return self._respond(400, {"ok": False})
        except (ValueError, json.JSONDecodeError):
            return self._respond(400, {"ok": False})
        try:
            main.process_telegram_update(update)
        except Exception:
            return self._respond(500, {"ok": False})
        return self._respond(200, {"ok": True})

    def _respond(self, status, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

