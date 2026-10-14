from __future__ import annotations

import json
import gzip
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from html import escape
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).parent
DB_PATH = ROOT / "leads.sqlite3"
STATIC = ROOT / "static"
ENV_PATH = ROOT / ".env"


def load_env() -> None:
    """Load the small local .env file without adding a runtime dependency."""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


load_env()
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
RECIPIENT_USERNAME = os.environ.get("TELEGRAM_RECIPIENT_USERNAME", "wxnasleep").strip().lstrip("@").lower()
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
INTEREST_LABELS = {
    "subscription": "Подписка · 2 пачки по 250 г / месяц",
    "order": "Разовый заказ",
    "question": "Вопрос о кофе",
}


def normalize_phone(value: str) -> str | None:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 9:
        digits = "375" + digits
    elif len(digits) == 10 and digits.startswith("0"):
        digits = "375" + digits[1:]
    if len(digits) == 12 and digits.startswith("375") and digits[3:5] in {"17", "25", "29", "33", "44"}:
        return "+" + digits
    return None


def allow_request(remote_ip: str) -> tuple[bool, int]:
    """Persistent per-IP request budget. Store only a keyed hash of the address."""
    now = time.time()
    day = int(now // 86_400)
    try:
        address = str(ipaddress.ip_address(remote_ip))
    except ValueError:
        address = "unknown"
    with sqlite3.connect(DB_PATH, timeout=5) as db:
        db.execute("BEGIN IMMEDIATE")
        secret_row = db.execute("SELECT value FROM app_settings WHERE key='rate_limit_secret'").fetchone()
        secret = bytes.fromhex(secret_row[0]) if secret_row else b"kvarta-local-rate-limit"
        ip_hash = hmac.new(secret, address.encode("ascii"), hashlib.sha256).hexdigest()
        row = db.execute("SELECT window_started,window_count,day_started,day_count FROM request_limits WHERE ip_hash=?", (ip_hash,)).fetchone()
        if row is None:
            db.execute("INSERT INTO request_limits VALUES(?,?,?,?,?,?)", (ip_hash, now, 1, day, 1, now))
            allowed, retry = True, 0
        else:
            window_started, window_count, day_started, day_count = row
            if now - window_started >= 600:
                window_started, window_count = now, 0
            if day_started != day:
                day_started, day_count = day, 0
            if window_count >= 6:
                allowed, retry = False, max(1, int(window_started + 600 - now + 0.99))
            elif day_count >= 30:
                allowed, retry = False, max(1, int((day + 1) * 86_400 - now + 0.99))
            else:
                window_count += 1
                day_count += 1
                allowed, retry = True, 0
            db.execute(
                "UPDATE request_limits SET window_started=?,window_count=?,day_started=?,day_count=?,updated_at=? WHERE ip_hash=?",
                (window_started, window_count, day_started, day_count, now, ip_hash),
            )
        if secrets.randbelow(64) == 0:
            db.execute("DELETE FROM request_limits WHERE updated_at < ?", (now - 30 * 86_400,))
        db.commit()
    return allowed, retry


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            interest TEXT NOT NULL,
            comment TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            medium TEXT NOT NULL DEFAULT '',
            campaign TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            term TEXT NOT NULL DEFAULT '',
            landing_page TEXT NOT NULL DEFAULT '',
            referrer TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS leads_phone_created ON leads(phone, created_at)")
        columns = {row[1] for row in db.execute("PRAGMA table_info(leads)")}
        for name, declaration in (
            ("telegram_status", "TEXT NOT NULL DEFAULT 'pending'"),
            ("telegram_message_id", "TEXT NOT NULL DEFAULT ''"),
            ("lead_status", "TEXT NOT NULL DEFAULT 'new'"),
        ):
            if name not in columns:
                db.execute(f"ALTER TABLE leads ADD COLUMN {name} {declaration}")
        db.execute("""CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT ''
        )""")
        db.execute("INSERT OR IGNORE INTO app_settings(key, value) VALUES('rate_limit_secret', ?)", (secrets.token_hex(32),))
        db.execute("""CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL UNIQUE REFERENCES leads(id),
            status TEXT NOT NULL DEFAULT 'in_progress',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS lead_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL REFERENCES leads(id),
            note TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS lead_notes_by_lead ON lead_notes(lead_id, created_at)")
        db.execute("""CREATE TABLE IF NOT EXISTS lead_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL REFERENCES leads(id),
            due_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            notified_at TEXT NOT NULL DEFAULT ''
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS reminders_due ON lead_reminders(status, due_at)")
        db.execute("UPDATE lead_reminders SET status='pending' WHERE status='sending'")
        db.execute("UPDATE leads SET telegram_status='pending' WHERE telegram_status='sending'")
        db.execute("""CREATE TABLE IF NOT EXISTS request_limits (
            ip_hash TEXT PRIMARY KEY,
            window_started REAL NOT NULL,
            window_count INTEGER NOT NULL,
            day_started INTEGER NOT NULL,
            day_count INTEGER NOT NULL,
            updated_at REAL NOT NULL
        )""")


class Handler(BaseHTTPRequestHandler):
    server_version = "Kvarta/1.0"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(6)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/health":
            return self.respond(HTTPStatus.OK, {
                "ok": True,
                "telegram_configured": bool(BOT_TOKEN),
                "telegram_recipient_connected": bool(setting("telegram_chat_id")),
            })
        if path == "/" or path == "/index.html":
            file = STATIC / "index.html"
            content_type = "text/html; charset=utf-8"
        elif path.startswith("/assets/"):
            file = STATIC / path.removeprefix("/assets/")
            content_type = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}.get(file.suffix, "application/octet-stream")
        else:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            data = file.read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        if "gzip" in self.headers.get("Accept-Encoding", "") and (content_type.startswith("text/") or content_type == "image/svg+xml"):
            data = gzip.compress(data, compresslevel=6)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable" if path.startswith("/assets/") else "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/lead":
            return self.respond(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Маршрут не найден."})
        allowed, retry_after = allow_request(self.client_address[0])
        if not allowed:
            return self.respond(HTTPStatus.TOO_MANY_REQUESTS, {
                "ok": False,
                "error": "Слишком много попыток. Подождите немного и попробуйте снова.",
            }, {"Retry-After": str(retry_after)})
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return self.respond(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"ok": False, "error": "Неверный формат заявки."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16_000:
                return self.respond(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Проверьте заполнение формы."})
            payload = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            return self.respond(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Не удалось прочитать заявку. Попробуйте ещё раз."})
        if not isinstance(payload, dict):
            return self.respond(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Проверьте заполнение формы."})
        if self.clean(payload.get("website"), 200):
            return self.respond(HTTPStatus.CREATED, {"ok": True, "message": "Спасибо! Заявка принята — мы скоро свяжемся."})
        name = self.clean(payload.get("name"), 100)
        phone = normalize_phone(self.clean(payload.get("phone"), 40))
        interest = self.clean(payload.get("interest"), 30)
        comment = self.clean(payload.get("comment"), 1_000)
        if not name:
            return self.respond(HTTPStatus.UNPROCESSABLE_ENTITY, {"ok": False, "error": "Напишите, как к вам обращаться."})
        if not phone:
            return self.respond(HTTPStatus.UNPROCESSABLE_ENTITY, {"ok": False, "error": "Введите белорусский номер: +375 29 123‑45‑67."})
        if interest not in {"subscription", "order", "question"}:
            return self.respond(HTTPStatus.UNPROCESSABLE_ENTITY, {"ok": False, "error": "Выберите, что вас интересует."})

        now = datetime.now(timezone.utc)
        with sqlite3.connect(DB_PATH, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            duplicate = db.execute(
                "SELECT id, created_at FROM leads WHERE phone = ? ORDER BY id DESC LIMIT 1", (phone,)
            ).fetchone()
            if duplicate:
                created = datetime.fromisoformat(duplicate[1])
                if now - created < timedelta(minutes=10):
                    db.commit()
                    return self.respond(HTTPStatus.OK, {"ok": True, "duplicate": True, "message": "Заявка уже у нас. Скоро позвоним!"})
            query = urlparse(self.path).query
            del query
            attribution = payload.get("attribution") if isinstance(payload.get("attribution"), dict) else {}
            fields = [self.clean(attribution.get(key), 500) for key in ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "page", "referrer")]
            cursor = db.execute(
                "INSERT INTO leads(name,phone,interest,comment,source,medium,campaign,content,term,landing_page,referrer,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (name, phone, interest, comment, *fields, now.isoformat()),
            )
            lead_id = cursor.lastrowid
            db.commit()
        print(f"Новая заявка #{lead_id}: {name} · {interest}", flush=True)
        queue_telegram_delivery(lead_id)
        return self.respond(HTTPStatus.CREATED, {"ok": True, "message": "Спасибо! Заявка принята — мы скоро свяжемся."})

    @staticmethod
    def clean(value: object, limit: int) -> str:
        if not isinstance(value, str):
            return ""
        return value.strip()[:limit]

    def respond(self, status: HTTPStatus, data: dict, extra_headers: dict[str, str] | None = None) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}", flush=True)


class LimitedThreadingHTTPServer(ThreadingHTTPServer):
    """Bound concurrent handlers so bursts cannot create unlimited threads."""
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, *args, max_handlers: int = 48, **kwargs):
        self._handler_slots = threading.BoundedSemaphore(max_handlers)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._handler_slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Retry-After: 5\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._handler_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._handler_slots.release()


def telegram_call(method: str, data: dict, timeout: int = 12) -> dict:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    request = urllib.request.Request(
        f"{TELEGRAM_API}/{method}",
        data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        safe_error = str(exc).replace(BOT_TOKEN, "[token скрыт]")
        raise RuntimeError(f"Telegram {method} request failed: {safe_error}") from exc
    if not result.get("ok"):
        raise RuntimeError(f"Telegram {method} rejected request: {result.get('description', 'unknown error')}")
    return result.get("result", {})


def setting(key: str) -> str:
    with sqlite3.connect(DB_PATH, timeout=10) as db:
        row = db.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else ""


def set_setting(key: str, value: str) -> None:
    with sqlite3.connect(DB_PATH, timeout=10) as db:
        db.execute("INSERT INTO app_settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def queue_telegram_delivery(lead_id: int) -> None:
    if not BOT_TOKEN:
        mark_telegram(lead_id, "disabled")
        return
    threading.Thread(target=deliver_lead, args=(lead_id,), daemon=True).start()


def mark_telegram(lead_id: int, status: str, message_id: str = "") -> None:
    with sqlite3.connect(DB_PATH, timeout=10) as db:
        db.execute("UPDATE leads SET telegram_status = ?, telegram_message_id = ? WHERE id = ?", (status, message_id, lead_id))


def local_now() -> datetime:
    return datetime.now().astimezone()


def load_lead(lead_id: int) -> tuple | None:
    with sqlite3.connect(DB_PATH, timeout=10) as db:
        return db.execute(
            "SELECT id,name,phone,interest,comment,created_at,lead_status FROM leads WHERE id=?",
            (lead_id,),
        ).fetchone()


def lead_notes(lead_id: int, limit: int = 4) -> list[tuple[str, str, str]]:
    with sqlite3.connect(DB_PATH, timeout=10) as db:
        rows = db.execute(
            "SELECT note,created_by,created_at FROM lead_notes WHERE lead_id=? ORDER BY id DESC LIMIT ?",
            (lead_id, limit),
        ).fetchall()
    return [(note, author, datetime.fromisoformat(created).astimezone().strftime("%d.%m %H:%M")) for note, author, created in reversed(rows)]


def lead_card(lead_id: int) -> str:
    lead = load_lead(lead_id)
    if not lead:
        return f"<b>Заявка №{lead_id} не найдена.</b>"
    _, name, phone, interest, comment, created_at, status = lead
    created = datetime.fromisoformat(created_at).astimezone().strftime("%d.%m.%Y · %H:%M")
    status_labels = {"new": "Новая", "accepted": "В работе · заказ", "reminder_set": "Ждёт напоминания", "declined": "Отклонена", "completed": "Завершена"}
    rows = [f"<b>Заявка №{lead_id}</b> · {escape(status_labels.get(status, status))}", "━━━━━━━━━━━━━━━━━━", f"👤 <b>{escape(name)}</b>", f"📞 <b>{escape(phone)}</b>", f"☕ {escape(INTEREST_LABELS.get(interest, interest))}"]
    if comment:
        rows.extend(["", f"💬 <b>Комментарий клиента:</b> {escape(comment)}"])
    notes = lead_notes(lead_id)
    if notes:
        rows.append("")
        rows.extend(f"📝 <i>{date} · {escape(author or 'без автора')}</i> · {escape(note)}" for note, author, date in notes)
    rows.extend(["", f"<i>{created} · Кварта</i>"])
    return "\n".join(rows)


def lead_keyboard(lead_id: int) -> dict:
    lead = load_lead(lead_id)
    status = lead[6] if lead else "new"
    first_row = [{"text": "📞 Позвонить", "callback_data": f"lead:{lead_id}:call"}]
    last_row = []
    if status == "accepted":
        first_row.append({"text": "✅ Заказ выполнен", "callback_data": f"lead:{lead_id}:done"})
        last_row.append({"text": "↩ Отменить заказ", "callback_data": f"lead:{lead_id}:decline"})
    elif status not in {"declined", "completed"}:
        first_row.append({"text": "✅ В работу", "callback_data": f"lead:{lead_id}:accept"})
        last_row.append({"text": "❌ Отклонить", "callback_data": f"lead:{lead_id}:decline"})
    rows = [first_row, [
        {"text": "📝 Добавить заметку", "callback_data": f"lead:{lead_id}:note"},
        {"text": "⏰ Напомнить позже", "callback_data": f"lead:{lead_id}:later"},
    ]]
    if last_row:
        rows.append(last_row)
    return {"inline_keyboard": rows}


def send_lead_card(chat_id: str, lead_id: int) -> None:
    telegram_call("sendMessage", {
        "chat_id": chat_id, "text": lead_card(lead_id), "parse_mode": "HTML",
        "reply_markup": lead_keyboard(lead_id), "disable_web_page_preview": True,
    })


def deliver_lead(lead_id: int) -> None:
    chat_id = setting("telegram_chat_id")
    if not chat_id:
        mark_telegram(lead_id, "waiting_for_start")
        return
    if not load_lead(lead_id):
        return
    mark_telegram(lead_id, "sending")
    try:
        sent = telegram_call("sendMessage", {
            "chat_id": chat_id, "text": lead_card(lead_id), "parse_mode": "HTML",
            "reply_markup": lead_keyboard(lead_id), "disable_web_page_preview": True,
        })
        mark_telegram(lead_id, "sent", str(sent.get("message_id", "")))
    except Exception as exc:
        mark_telegram(lead_id, "pending")
        print(f"Не удалось отправить заявку #{lead_id} в Telegram: {str(exc).replace(BOT_TOKEN, '[token скрыт]')}", flush=True)


def awaiting_key(user_id: str) -> str:
    return f"awaiting_input:{user_id}"


def clear_awaiting(user_id: str) -> None:
    with sqlite3.connect(DB_PATH, timeout=10) as db:
        db.execute("DELETE FROM app_settings WHERE key=?", (awaiting_key(user_id),))


def parse_reminder(value: str) -> datetime | None:
    text = " ".join(value.strip().lower().split())
    now = local_now()
    result: datetime | None = None
    relative = re.fullmatch(r"через\s+(\d+)\s*(минут(?:у|ы)?|мин|час(?:а|ов)?|ч|день|дня|дней)", text)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        delta = timedelta(minutes=amount) if unit.startswith("мин") else timedelta(hours=amount) if unit.startswith(("час", "ч")) else timedelta(days=amount)
        result = now + delta
    else:
        relative_words = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
        for word, days in relative_words.items():
            if text.startswith(word):
                clock = text[len(word):].strip().removeprefix("в ")
                try:
                    at = datetime.strptime(clock, "%H:%M").time()
                    result = (now + timedelta(days=days)).replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
                except ValueError:
                    return None
                break
        if result is None:
            for pattern in ("%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M", "%d.%m %H:%M"):
                try:
                    parsed = datetime.strptime(text, pattern)
                    if pattern == "%d.%m %H:%M":
                        parsed = parsed.replace(year=now.year)
                        if parsed <= now.replace(tzinfo=None):
                            parsed = parsed.replace(year=now.year + 1)
                    result = parsed.replace(tzinfo=now.tzinfo)
                    break
                except ValueError:
                    continue
    if result is None:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=now.tzinfo)
    if result < now + timedelta(minutes=1) or result > now + timedelta(days=365):
        return None
    return result


def prompt_for_input(user_id: str, kind: str, lead_id: int) -> None:
    set_setting(awaiting_key(user_id), json.dumps({"kind": kind, "lead_id": lead_id, "expires": time.time() + 86_400}))
    chat_id = setting("telegram_chat_id")
    if kind == "note":
        text = f"Пришлите заметку к заявке №{lead_id} одним сообщением. Она сохранится в истории.\n\n/cancel — отменить"
    else:
        text = (f"Когда напомнить по заявке №{lead_id}? Напишите дату и время, например:\n"
                "• 18.10.2026 14:30\n• 18.10 14:30\n• завтра 10:00\n• через 2 часа\n\n"
                "Время берётся по часовому поясу компьютера с сервером.\n/cancel — отменить")
    telegram_call("sendMessage", {"chat_id": chat_id, "text": text})


def _preview(value: str | None, limit: int = 72) -> str:
    value = " ".join((value or "").split())
    if len(value) > limit:
        value = value[:limit - 1].rstrip() + "…"
    return escape(value)


def send_leads_list(chat_id: str, all_orders: bool = False) -> None:
    with sqlite3.connect(DB_PATH, timeout=10) as db:
        if all_orders:
            orders = db.execute(
                """SELECT l.id,l.name,l.phone,l.interest,o.status,o.updated_at,l.comment,
                   (SELECT note FROM lead_notes n WHERE n.lead_id=l.id ORDER BY n.id DESC LIMIT 1),
                   (SELECT created_by FROM lead_notes n WHERE n.lead_id=l.id ORDER BY n.id DESC LIMIT 1)
                   FROM orders o JOIN leads l ON l.id=o.lead_id ORDER BY o.updated_at DESC LIMIT 10"""
            ).fetchall()
        else:
            orders = db.execute(
                """SELECT l.id,l.name,l.phone,l.interest,o.status,o.updated_at,l.comment,
                   (SELECT note FROM lead_notes n WHERE n.lead_id=l.id ORDER BY n.id DESC LIMIT 1),
                   (SELECT created_by FROM lead_notes n WHERE n.lead_id=l.id ORDER BY n.id DESC LIMIT 1)
                   FROM orders o JOIN leads l ON l.id=o.lead_id WHERE o.status='in_progress' ORDER BY o.updated_at DESC LIMIT 5"""
            ).fetchall()
        fresh = [] if all_orders else db.execute(
            """SELECT id,name,phone,interest,created_at,comment,
               (SELECT note FROM lead_notes n WHERE n.lead_id=leads.id ORDER BY n.id DESC LIMIT 1),
               (SELECT created_by FROM lead_notes n WHERE n.lead_id=leads.id ORDER BY n.id DESC LIMIT 1)
               FROM leads WHERE lead_status='new' AND telegram_status='sent' ORDER BY id DESC LIMIT 5"""
        ).fetchall()
        reminders = db.execute(
            "SELECT r.id,r.lead_id,r.due_at,l.name FROM lead_reminders r JOIN leads l ON l.id=r.lead_id WHERE r.status IN ('pending','sent') ORDER BY r.due_at LIMIT 5"
        ).fetchall()
    lines = ["<b>Все заказы</b>" if all_orders else "<b>Активные заявки и заказы</b>"]
    keyboard: list[list[dict]] = []
    if orders:
        lines.extend(["", "<b>Заказы:</b>"])
        labels = {"in_progress": "В работе", "completed": "Выполнен", "declined": "Отменён"}
        for lead_id, name, phone, interest, status, updated, comment, note, author in orders:
            lines.append(f"• <b>#{lead_id} · {escape(labels.get(status, status))}</b> · {escape(name)} · {escape(phone)} · {escape(INTEREST_LABELS.get(interest, interest))}")
            if comment:
                lines.append(f"  💬 Комментарий клиента: {_preview(comment, 58)}")
            if note:
                lines.append(f"  📝 Заметка · {escape(author or 'без автора')}: {_preview(note, 58)}")
            keyboard.append([{"text": f"Открыть заказ №{lead_id}", "callback_data": f"lead:{lead_id}:open"}])
    else:
        lines.extend(["", "<i>Заказов пока нет.</i>" if all_orders else "<i>Заказов в работе пока нет.</i>"])
    if fresh:
        lines.extend(["", "<b>Новые заявки:</b>"])
        for lead_id, name, phone, interest, created, comment, note, author in fresh:
            lines.append(f"🆕 <b>#{lead_id}</b> · {escape(name)} · {escape(phone)} · {escape(INTEREST_LABELS.get(interest, interest))}")
            if comment:
                lines.append(f"  💬 Комментарий клиента: {_preview(comment, 58)}")
            if note:
                lines.append(f"  📝 Заметка · {escape(author or 'без автора')}: {_preview(note, 58)}")
            keyboard.append([{"text": f"Открыть заявку №{lead_id}", "callback_data": f"lead:{lead_id}:open"}])
    if reminders:
        lines.extend(["", "<b>Напоминания:</b>"])
        for _, lead_id, due_at, name in reminders:
            due = datetime.fromisoformat(due_at).astimezone().strftime("%d.%m · %H:%M")
            lines.append(f"⏰ #{lead_id} · {escape(name)} · {due}")
    if not orders and not fresh and (all_orders or not reminders):
        lines.extend(["", "Заказов пока нет." if all_orders else "Пока нет активных заявок и заказов."])
    telegram_call("sendMessage", {"chat_id": chat_id, "text": "\n".join(lines), "parse_mode": "HTML", "reply_markup": {"inline_keyboard": keyboard[:20]}})


def handle_message(message: dict) -> None:
    sender = message.get("from", {})
    username = str(sender.get("username", "")).lower()
    user_id = str(sender.get("id", ""))
    chat = message.get("chat", {})
    chat_id = str(chat.get("id", ""))
    text = str(message.get("text", "")).strip()
    command = text.split(" ", 1)[0].split("@", 1)[0].lower() if text else ""

    if command == "/start":
        if username != RECIPIENT_USERNAME or chat.get("type") != "private":
            return
        set_setting("telegram_chat_id", chat_id)
        set_setting("telegram_recipient_user_id", user_id)
        telegram_call("sendMessage", {
            "chat_id": chat_id,
            "text": "Подключено. Новые заявки придут сюда.\n\n/leads — активные заявки и заказы\n/cancel — отменить ввод заметки или напоминания",
            "reply_markup": {
                "keyboard": [[{"text": "Активные заказы"}, {"text": "Все заказы"}]],
                "resize_keyboard": True,
                "is_persistent": True,
                "input_field_placeholder": "Выберите список заказов",
            },
        })
        with sqlite3.connect(DB_PATH, timeout=10) as db:
            pending = db.execute("SELECT id FROM leads WHERE telegram_status IN ('pending','waiting_for_start') ORDER BY id").fetchall()
        for (lead_id,) in pending:
            deliver_lead(lead_id)
        return

    if user_id != setting("telegram_recipient_user_id") or chat_id != setting("telegram_chat_id"):
        return
    menu_action = text.casefold()
    if command == "/leads" or menu_action == "активные заказы":
        send_leads_list(chat_id)
        return
    if menu_action == "все заказы":
        send_leads_list(chat_id, all_orders=True)
        return
    if command == "/cancel":
        clear_awaiting(user_id)
        telegram_call("sendMessage", {"chat_id": chat_id, "text": "Ввод отменён."})
        return

    pending_raw = setting(awaiting_key(user_id))
    if not pending_raw:
        return
    try:
        pending = json.loads(pending_raw)
        lead_id = int(pending["lead_id"])
        if float(pending["expires"]) < time.time():
            clear_awaiting(user_id)
            telegram_call("sendMessage", {"chat_id": chat_id, "text": "Время ввода истекло. Откройте карточку заявки и выберите действие ещё раз."})
            return
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        clear_awaiting(user_id)
        return

    if pending["kind"] == "note":
        note = text[:1_000]
        if not note:
            telegram_call("sendMessage", {"chat_id": chat_id, "text": "Заметка пустая. Напишите текст или отправьте /cancel."})
            return
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(DB_PATH, timeout=10) as db:
            db.execute("INSERT INTO lead_notes(lead_id,note,created_by,created_at) VALUES(?,?,?,?)", (lead_id, note, username or user_id, now))
        clear_awaiting(user_id)
        telegram_call("sendMessage", {"chat_id": chat_id, "text": f"Заметка сохранена к заявке №{lead_id}."})
        refresh_lead_card(lead_id)
    elif pending["kind"] == "reminder":
        due_at = parse_reminder(text)
        if not due_at:
            telegram_call("sendMessage", {"chat_id": chat_id, "text": "Не распознал дату. Примеры: 18.10 14:30, завтра 10:00, через 2 часа. Попробуйте ещё раз или /cancel."})
            return
        with sqlite3.connect(DB_PATH, timeout=10) as db:
            db.execute("INSERT INTO lead_reminders(lead_id,due_at,status,created_by,created_at) VALUES(?,?,'pending',?,?)", (lead_id, due_at.isoformat(), username or user_id, datetime.now(timezone.utc).isoformat()))
            db.execute("UPDATE leads SET lead_status=CASE WHEN lead_status='new' THEN 'reminder_set' ELSE lead_status END WHERE id=?", (lead_id,))
        clear_awaiting(user_id)
        telegram_call("sendMessage", {"chat_id": chat_id, "text": f"Напоминание поставлено на {due_at.strftime('%d.%m.%Y в %H:%M')} по местному времени."})
        refresh_lead_card(lead_id)


def refresh_lead_card(lead_id: int) -> None:
    message_id = ""
    with sqlite3.connect(DB_PATH, timeout=10) as db:
        row = db.execute("SELECT telegram_message_id FROM leads WHERE id=?", (lead_id,)).fetchone()
    if row:
        message_id = row[0]
    chat_id = setting("telegram_chat_id")
    if not chat_id or not message_id:
        return
    telegram_call("editMessageText", {
        "chat_id": chat_id, "message_id": int(message_id), "text": lead_card(lead_id),
        "parse_mode": "HTML", "reply_markup": lead_keyboard(lead_id), "disable_web_page_preview": True,
    })


def handle_callback(callback: dict) -> None:
    callback_id = callback.get("id", "")
    message = callback.get("message") or {}
    chat_id = str(message.get("chat", {}).get("id", ""))
    user_id = str((callback.get("from") or {}).get("id", ""))
    if (chat_id != setting("telegram_chat_id") or user_id != setting("telegram_recipient_user_id")):
        telegram_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": "Нет доступа к этой заявке.", "show_alert": True})
        return
    try:
        parts = callback.get("data", "").split(":", 3)
        _, raw_id, action = parts[:3]
        lead_id = int(raw_id)
        if action == "reminder_done" and len(parts) < 4:
            raise ValueError("reminder id is missing")
    except (ValueError, TypeError):
        telegram_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": "Не удалось распознать действие."})
        return
    lead = load_lead(lead_id)
    if not lead:
        telegram_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": "Заявка уже удалена.", "show_alert": True})
        return
    if action == "call":
        telegram_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": f"Номер: {lead[2]}\nНажмите на номер в карточке, чтобы позвонить.", "show_alert": True})
        return
    if action == "open":
        telegram_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": "Открываю карточку…"})
        send_lead_card(chat_id, lead_id)
        return
    if action == "note":
        telegram_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": "Жду заметку сообщением."})
        prompt_for_input(user_id, "note", lead_id)
        return
    if action == "later":
        telegram_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": "Укажите удобное время напоминания."})
        prompt_for_input(user_id, "reminder", lead_id)
        return
    now = datetime.now(timezone.utc).isoformat()
    if action == "accept":
        with sqlite3.connect(DB_PATH, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE leads SET lead_status='accepted' WHERE id=?", (lead_id,))
            db.execute("INSERT INTO orders(lead_id,status,created_at,updated_at) VALUES(?,'in_progress',?,?) ON CONFLICT(lead_id) DO UPDATE SET status='in_progress',updated_at=excluded.updated_at", (lead_id, now, now))
            db.commit()
        answer = "Добавлено в заказы и отмечено «В работе»."
    elif action == "decline":
        with sqlite3.connect(DB_PATH, timeout=10) as db:
            db.execute("UPDATE leads SET lead_status='declined' WHERE id=?", (lead_id,))
            db.execute("UPDATE orders SET status='declined',updated_at=? WHERE lead_id=?", (now, lead_id))
            db.execute("UPDATE lead_reminders SET status='cancelled' WHERE lead_id=? AND status IN ('pending','sent')", (lead_id,))
        answer = "Заявка отклонена."
    elif action == "done":
        with sqlite3.connect(DB_PATH, timeout=10) as db:
            db.execute("UPDATE orders SET status='completed',updated_at=? WHERE lead_id=?", (now, lead_id))
            db.execute("UPDATE leads SET lead_status='completed' WHERE id=?", (lead_id,))
            db.execute("UPDATE lead_reminders SET status='completed' WHERE lead_id=? AND status IN ('pending','sent')", (lead_id,))
        answer = f"Заказ №{lead_id} закрыт."
    elif action == "reminder_done":
        reminder_id = int(parts[3])
        with sqlite3.connect(DB_PATH, timeout=10) as db:
            db.execute("UPDATE lead_reminders SET status='completed' WHERE id=? AND lead_id=?", (reminder_id, lead_id))
        answer = "Напоминание закрыто."
    else:
        telegram_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": "Неизвестное действие."})
        return
    telegram_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": answer, "show_alert": action in {"decline", "done"}})
    if action in {"accept", "decline"}:
        telegram_call("editMessageText", {
            "chat_id": chat_id, "message_id": message.get("message_id"),
            "text": lead_card(lead_id), "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": []} if action == "decline" else lead_keyboard(lead_id),
            "disable_web_page_preview": True,
        })
    elif action in {"done", "reminder_done"}:
        telegram_call("editMessageText", {
            "chat_id": chat_id, "message_id": message.get("message_id"),
            "text": f"✅ {escape(answer)}", "reply_markup": {"inline_keyboard": []},
        })
        if action == "done":
            send_leads_list(chat_id)


def handle_update(update: dict) -> None:
    message = update.get("message") or update.get("edited_message")
    if message:
        handle_message(message)
    callback = update.get("callback_query")
    if callback:
        handle_callback(callback)


def deliver_due_reminders() -> None:
    chat_id = setting("telegram_chat_id")
    if not chat_id:
        return
    now = local_now().isoformat()
    with sqlite3.connect(DB_PATH, timeout=10) as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            "SELECT id,lead_id,due_at FROM lead_reminders WHERE status='pending' AND due_at<=? ORDER BY due_at LIMIT 10",
            (now,),
        ).fetchall()
        for reminder_id, _, _ in rows:
            db.execute("UPDATE lead_reminders SET status='sending' WHERE id=?", (reminder_id,))
        db.commit()
    for index, (reminder_id, lead_id, due_at) in enumerate(rows):
        lead = load_lead(lead_id)
        if not lead:
            with sqlite3.connect(DB_PATH, timeout=10) as db:
                db.execute("UPDATE lead_reminders SET status='cancelled' WHERE id=?", (reminder_id,))
            continue
        try:
            telegram_call("sendMessage", {
                "chat_id": chat_id,
                "text": f"⏰ <b>Напоминание · заявка №{lead_id}</b>\n{escape(lead[1])} · {escape(lead[2])}\nСрок: {datetime.fromisoformat(due_at).astimezone().strftime('%d.%m.%Y · %H:%M')}",
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": [[
                    {"text": "Открыть заявку", "callback_data": f"lead:{lead_id}:open"},
                    {"text": "Готово", "callback_data": f"lead:{lead_id}:reminder_done:{reminder_id}"},
                ]]},
            })
            with sqlite3.connect(DB_PATH, timeout=10) as db:
                db.execute("UPDATE lead_reminders SET status='sent',notified_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), reminder_id))
        except Exception as exc:
            with sqlite3.connect(DB_PATH, timeout=10) as db:
                remaining_ids = [row[0] for row in rows[index:]]
                placeholders = ",".join("?" for _ in remaining_ids)
                db.execute(f"UPDATE lead_reminders SET status='pending' WHERE id IN ({placeholders}) AND status='sending'", remaining_ids)
            print(f"Не удалось отправить напоминание #{reminder_id}: {str(exc).replace(BOT_TOKEN, '[token скрыт]')}", flush=True)
            break


def telegram_poll_worker() -> None:
    if not BOT_TOKEN:
        print("Telegram: TELEGRAM_BOT_TOKEN не задан; уведомления отключены.", flush=True)
        return
    offset: int | None = None
    print("Telegram: слушаю команды бота для подключения получателя и действий по заявкам.", flush=True)
    while True:
        try:
            params = {"timeout": 20, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset
            for update in telegram_call("getUpdates", params, timeout=25):
                offset = update.get("update_id", 0) + 1
                try:
                    handle_update(update)
                except Exception as exc:
                    print(f"Ошибка обработки действия Telegram: {str(exc).replace(BOT_TOKEN, '[token скрыт]')}", flush=True)
            with sqlite3.connect(DB_PATH, timeout=10) as db:
                pending = db.execute("SELECT id FROM leads WHERE telegram_status = 'pending' ORDER BY id LIMIT 20").fetchall()
            for (lead_id,) in pending:
                deliver_lead(lead_id)
            deliver_due_reminders()
        except Exception as exc:
            print(f"Telegram polling: {str(exc).replace(BOT_TOKEN, '[token скрыт]')}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    init_db()
    if BOT_TOKEN:
        threading.Thread(target=telegram_poll_worker, daemon=True, name="telegram-bot-poll").start()
    port = int(os.environ.get("PORT", "8000"))
    print(f"Кварта открыта: http://localhost:{port}", flush=True)
    LimitedThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
