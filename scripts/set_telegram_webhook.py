from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path


def load_local_env() -> None:
    env_file = Path(__file__).resolve().parents[1] / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main() -> None:
    load_local_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if len(sys.argv) != 2 or not token or len(secret) < 16:
        raise SystemExit("Usage: python scripts/set_telegram_webhook.py https://your-project.vercel.app (set TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET first)")
    url = sys.argv[1].rstrip("/") + "/api/telegram"
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/setWebhook",
        data=json.dumps({
            "url": url,
            "secret_token": secret,
            "allowed_updates": ["message", "callback_query"],
            "drop_pending_updates": False,
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise SystemExit("Telegram rejected the webhook configuration.")
    print(f"Webhook enabled: {url}")


if __name__ == "__main__":
    main()
