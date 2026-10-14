from __future__ import annotations

import hmac
import os
from http.server import BaseHTTPRequestHandler

import main


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        main.init_db()
        secret = os.environ.get("CRON_SECRET", "")
        auth = self.headers.get("Authorization", "")
        if not secret or not hmac.compare_digest(auth, f"Bearer {secret}"):
            self.send_response(401)
            self.end_headers()
            return
        try:
            from datetime import datetime, timedelta, timezone
            stale_before = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            with main.sqlite3.connect(main.DB_PATH, timeout=10) as db:
                db.execute("UPDATE lead_reminders SET status='pending' WHERE status='sending' AND created_at < ?", (stale_before,))
                db.execute("UPDATE leads SET telegram_status='pending' WHERE telegram_status='sending' AND created_at < ?", (stale_before,))
            main.deliver_pending_leads()
            main.deliver_due_reminders()
            status = 200
        except Exception:
            status = 500
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

