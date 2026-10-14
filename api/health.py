from http.server import BaseHTTPRequestHandler
import json
import os

import main


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        main.init_db()
        payload = {
            "ok": True,
            "backend": "vercel-python",
            "database_configured": bool(os.environ.get("DATABASE_URL")),
            "telegram_configured": bool(main.BOT_TOKEN),
            "telegram_recipient_connected": bool(main.setting("telegram_chat_id")),
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

