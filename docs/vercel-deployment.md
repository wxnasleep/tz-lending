# Vercel: сайт и backend

Лендинг отдаётся как статические файлы из `static/`. Функции Python находятся в `api/`: `/api/lead` принимает заявку, `/api/health` показывает состояние конфигурации, `/api/telegram` принимает Telegram webhook, `/api/cron` отправляет наступившие напоминания. Локальный запуск через `python main.py` сохраняет SQLite; Vercel использует PostgreSQL из `DATABASE_URL`.

## Подключение

1. Импортировать GitHub-репозиторий `vovavovavovan2009-lang/tz-lending` в Vercel с preset `Other`.
2. Подключить PostgreSQL в Vercel Marketplace (например, Neon), выбрать доступный бесплатный тариф и привязать ресурс к проекту. Интеграция добавит `DATABASE_URL` в среду проекта.
3. Добавить Production environment variables: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_RECIPIENT_USERNAME` (имя получателя без `@`), `TELEGRAM_WEBHOOK_SECRET` (случайная строка от 16 символов), `CRON_SECRET` (другая случайная строка) и `APP_TIMEZONE=Europe/Minsk`. Те же значения задаются отдельно для Preview только если нужны тестовые уведомления.
4. После первого deployment установить Telegram webhook командой `python scripts/set_telegram_webhook.py https://<deployment-domain>`, предварительно задав в локальном окружении токен и webhook secret. Затем указанный в `TELEGRAM_RECIPIENT_USERNAME` пользователь один раз отправляет боту `/start`.
5. Для автоматических напоминаний каждую минуту в `vercel.json` добавить:

   ```json
   "crons": [{ "path": "/api/cron", "schedule": "* * * * *" }]
   ```

   Endpoint защищён `CRON_SECRET`. На текущем Vercel Hobby такой интервал не поддерживается: Hobby допускает cron не чаще одного раза в день и с точностью до часа. Для соблюдения выбранного пользователем времени нужен Vercel Pro с минутным расписанием либо внешний scheduler/queue; без одного из них точную доставку обещать нельзя.

В репозитории нет production-токена и URL базы. Секреты нужно вводить только в Environment Variables целевого Vercel-проекта; `.env` остаётся локальным и исключён Git.
