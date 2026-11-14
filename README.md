# Anonim Emoji (Render)

Telegram bot + Mini App: animatsiyali premium emoji, konkurs, admin panel.

## Render'ga qo'yish
1. Papkani GitHub'ga yuklang (`.env` yuklanmaydi).
2. Render → **New → Blueprint** → shu repo. `render.yaml` hammasini o'zi sozlaydi.
3. So'ralganda `BOT_TOKEN` ga @BotFather tokenini kiriting → Deploy.
4. Botga `/start`, birinchi marta egasidan `/brand`.

Manzil (`WEBAPP_URL`) va port Render'dan avtomatik olinadi. Ma'lumotlar `/var/data` diskida saqlanadi.
Lokal: `.env` (`.env.example` dan) → `pip install -r requirements.txt` → `python anonim_emoji.py` (Mini App uchun HTTPS `WEBAPP_URL` kerak).
