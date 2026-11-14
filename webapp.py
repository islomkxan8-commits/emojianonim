"""webapp.py — Telegram Mini App: foydalanuvchilar uchun emoji yaratish (jonli ko'rinish) + admin panel.
Ishga tushirish: muhit o'zgaruvchisi WEBAPP_URL (HTTPS manzil) berilsa yoqiladi. Port: WEBAPP_PORT (8080)."""
import asyncio, hashlib, hmac, json, logging, os, random, re, secrets, time
from urllib.parse import parse_qsl
from aiohttp import web
from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, FSInputFile, InputProfilePhotoStatic, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, MenuButtonWebApp,
                           Message, PreCheckoutQuery, User, WebAppInfo)

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DATA_DIR") or HERE
WEB = os.path.join(HERE, "webapp")
ORDERS = os.path.join(DATA, "webapp_orders.json")
BANNED = os.path.join(DATA, "banned.json")
SUBSEEN = os.path.join(DATA, "sub_state.json")
APPDATA = os.path.join(DATA, "app_data.json")  # to'plamlar tarixi, kunlik bonus, promokodlar
_hits: dict = {}


def _dump(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _ad() -> dict:
    return _jload(APPDATA, {})


def add_pack(uid, title, url, kind):
    """_finalize_pack chaqiradi: foydalanuvchi to'plamlari tarixiga yozadi."""
    d = _ad()
    lst = d.setdefault("packs", {}).setdefault(str(uid), [])
    lst.append({"title": str(title)[:64], "url": url, "kind": kind, "ts": int(time.time())})
    d["packs"][str(uid)] = lst[-100:]
    day = time.strftime("%Y-%m-%d")
    days = d.setdefault("days", {})
    days[day] = days.get(day, 0) + 1
    d["days"] = dict(sorted(days.items())[-30:])
    c = d.get("contest")
    if c and c.get("status") == "active" and time.time() < c["end"]:  # konkurs hisobi: har bir yangi to'plam +1
        c["counts"][str(uid)] = c["counts"].get(str(uid), 0) + 1
    _dump(APPDATA, d)
_subcache: dict = {}


def _jload(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _banned() -> set:
    return set(_jload(BANNED, []))


async def sub_status(bot, ctx, uid):
    """(yetishmayotgan kanallar, oldin obuna bo'lib ko'rganmi) — 8 soniya keshlanadi."""
    now, hit = time.time(), _subcache.get(uid)
    if hit and now - hit[0] < 8:
        return hit[1], hit[2]
    seen = str(uid) in _jload(SUBSEEN, {})
    missing = []
    for ch in ctx.get_channels():
        try:
            if (await bot.get_chat_member(ch, uid)).status in ("left", "kicked"):
                missing.append(ch)
        except Exception as e:
            logging.warning("channel check failed %s: %s", ch, e)
    if not missing and not seen:
        d = _jload(SUBSEEN, {})
        d[str(uid)] = 1
        ctx._atomic_save_json(SUBSEEN, d)
    _subcache[uid] = (now, missing, seen)
    return missing, seen


def _err(code, **kw):
    return web.HTTPForbidden(text=json.dumps({"error": code, **kw}), content_type="application/json")


class Guard(BaseMiddleware):
    """Bloklanganlarni va kanaldan chiqib ketganlarni botdan foydalanishdan to'xtatadi."""
    def __init__(self, ctx):
        self.ctx = ctx

    async def __call__(self, handler, event, data):
        ctx, u = self.ctx, event.from_user
        if u is None or ctx.is_admin(u.id) or getattr(event, "successful_payment", None):
            return await handler(event, data)
        cb = isinstance(event, CallbackQuery)
        if u.id in _banned():
            if cb:
                await event.answer("🚫 Siz bloklangansiz", show_alert=True)
            else:
                await event.answer("🚫 Siz bloklangansiz. Botdan foydalana olmaysiz.")
            return
        if _ad().get("cfg", {}).get("maint"):
            if cb:
                await event.answer("🛠 Ta'mirlash ishlari olib borilmoqda", show_alert=True)
            else:
                await event.answer("🛠 Ta'mirlash ishlari olib borilmoqda. Tez orada qaytamiz!")
            return
        if cb and event.data == "checksub":
            _subcache.pop(u.id, None)
            return await handler(event, data)
        missing, seen = await sub_status(data["bot"], ctx, u.id)
        if not missing or (not cb and (event.text or "").startswith("/start") and not seen):
            return await handler(event, data)  # yangi foydalanuvchi /start: asosiy handler yozib oladi
        text = ("⚠️ Siz kanaldan chiqib ketdingiz. Kanalga obuna bo'ling, shunda botdan foydalana olasiz:" if seen else
                "Botdan foydalanish uchun avval kanal(lar)ga a'zo bo'ling, so'ng \"Tekshirish\" tugmasini bosing:")
        if cb:
            await event.answer()
        await (event.message.answer if cb else event.answer)(text, reply_markup=ctx.subscribe_keyboard(ctx.get_channels()))


async def _uinfo(bot, ctx, uid):
    st, name, uname = ctx.load_stats().get(str(uid), {}), None, None
    try:
        ch = await bot.get_chat(uid)
        name, uname = " ".join(filter(None, [ch.first_name, ch.last_name])), ch.username
    except Exception:
        pass
    return {"id": uid, "name": name, "username": uname or st.get("username"), "started": uid in ctx.load_users(),
            "packs": st.get("packs", 0), "stars": st.get("stars", 0), "banned": uid in _banned(), "admin": ctx.is_admin(uid)}


def verify(init_data: str, token: str):
    """Telegram initData imzosini tekshiradi -> user dict yoki None."""
    try:
        d = dict(parse_qsl(init_data, keep_blank_values=True))
        got = d.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(key, check.encode(), hashlib.sha256).hexdigest(), got):
            return None
        if time.time() - int(d["auth_date"]) > 86400:
            return None
        return json.loads(d["user"])
    except Exception:
        return None


def _orders() -> dict:
    try:
        with open(ORDERS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(o: dict, ctx):
    ctx._atomic_save_json(ORDERS, o)


async def _auth(request):
    u = verify(request.headers.get("X-Init-Data", ""), request.app["bot"].token)
    if not u:
        raise web.HTTPUnauthorized(text="initData noto'g'ri")
    now = time.time()
    hits = [t for t in _hits.get(u["id"], []) if now - t < 10]
    if len(hits) >= 40:
        raise web.HTTPTooManyRequests(text="Juda tez")
    _hits[u["id"]] = hits + [now]
    ctx, bot = request.app["ctx"], request.app["bot"]
    if not ctx.is_admin(u["id"]):
        if u["id"] in _banned():
            raise _err("banned")
        if _ad().get("cfg", {}).get("maint"):
            raise _err("maint")
        missing, seen = await sub_status(bot, ctx, u["id"])
        if missing:
            raise _err("subscribe", left=seen, channels=missing)
    return u


def _clean(ctx, d: dict) -> dict:
    le = ctx.logo_engine
    if d.get("kind") == "name":
        word, tpl = str(d.get("word", "")).strip(), d.get("template")
        if not word or len(word) > ctx.MAX_LEN or (tpl != "all" and tpl not in ctx.TEMPLATES):
            raise ValueError("So'z yoki shablon noto'g'ri")
        return {"kind": "name", "word": word, "template": tpl}
    if d.get("kind") == "logo":
        n, text = int(d.get("n", 0)), str(d.get("text", "")).strip()
        if not 1 <= n <= len(le.list_tgs_templates()) or not text or len(text) > 20:
            raise ValueError("Shablon yoki matn noto'g'ri")
        font = d.get("font") if d.get("font") in le.FONT_OPTIONS else "classic"
        outer, inner = ctx._normalize_hex(str(d.get("outer", ""))), ctx._normalize_hex(str(d.get("inner", "")))
        if not outer or not inner:
            raise ValueError("Rang noto'g'ri")
        return {"kind": "logo", "n": n, "text": text, "font": font, "outer": outer, "inner": inner,
                "logo": ctx._normalize_hex(str(d.get("logo", ""))) or None}
    raise ValueError("kind noto'g'ri")


def _preview(ctx, r: dict):
    le = ctx.logo_engine
    if r["kind"] == "name":
        keys = ctx.TEMPLATE_ORDER[:6] if r["template"] == "all" else [r["template"]]
        out = []
        for k in keys:
            lot = ctx.render_template(ctx.TEMPLATES[k], r["word"])
            le.add_preview_watermark(lot)
            out.append(lot)
        return out
    svg = le.text_to_svg(r["text"], font_path=le.FONT_OPTIONS[r["font"]][1])
    _, raw = le.build_tgs_sticker(f"{r['n']:03d}.json", svg, r["outer"], r["inner"], r["logo"],
                                  size_percent=le.DEFAULT_SIZE_PERCENT, watermark=True)
    return [raw]


def _final(ctx, r: dict, uid: int, oid: str) -> list:
    le, out = ctx.logo_engine, os.path.join(DATA, "output")
    os.makedirs(out, exist_ok=True)
    paths = []
    if r["kind"] == "name":
        for i, k in enumerate(ctx.TEMPLATE_ORDER if r["template"] == "all" else [r["template"]]):
            p = os.path.join(out, f"{uid}_web_{oid}_{i}.tgs")
            ctx.save_as_tgs(ctx.render_template(ctx.TEMPLATES[k], r["word"]), p)
            paths.append(p)
    else:
        svg = le.text_to_svg(r["text"], font_path=le.FONT_OPTIONS[r["font"]][1])
        b, _ = le.build_tgs_sticker(f"{r['n']:03d}.json", svg, r["outer"], r["inner"], r["logo"],
                                    size_percent=le.DEFAULT_SIZE_PERCENT)
        p = os.path.join(out, f"{uid}_web_{oid}_0.tgs")
        with open(p, "wb") as f:
            f.write(b)
        paths.append(p)
    return paths


async def api_config(request):
    u, ctx = await _auth(request), request.app["ctx"]
    le, st, c = ctx.logo_engine, ctx.load_stats().get(str(u["id"]), {}), _ad().get("contest")
    return web.json_response({
        "lang": (u.get("language_code") or "")[:2],
        "contest": {"end": c["end"], "prize": c["prize"]} if c and c.get("status") == "active" else None,
        "profile": {"id": u["id"], "name": " ".join(filter(None, [u.get("first_name"), u.get("last_name")])), "username": u.get("username"),
                    "photo": u.get("photo_url"), "packs": st.get("packs", 0), "stars": st.get("stars", 0)},
        "templates": [{"key": k, "label": ctx.TEMPLATES[k]["label"]} for k in ctx.TEMPLATE_ORDER],
        "max_len": ctx.MAX_LEN, "logo_count": len(le.list_tgs_templates()),
        "fonts": [{"key": k, "label": v[0]} for k, v in le.FONT_OPTIONS.items()],
        "support": ctx.get_support_contact(), "prices": {k: ctx.load_price(k) for k in ("name", "logo")},
        "free": ctx.is_admin(u["id"]), "admin": ctx.is_admin(u["id"]), "owner": ctx.is_owner(u["id"])})


async def api_preview(request):
    await _auth(request)
    ctx = request.app["ctx"]
    try:
        r = _clean(ctx, await request.json())
        data = await asyncio.get_running_loop().run_in_executor(None, _preview, ctx, r)
    except Exception as e:
        return web.json_response({"error": str(e)[:200]}, status=400)
    resp = web.json_response({"anims": data})
    resp.enable_compression()
    return resp


async def api_order(request):
    u, ctx, bot = await _auth(request), request.app["ctx"], request.app["bot"]
    uid = u["id"]
    try:
        d = await request.json()
        r = _clean(ctx, d)
        oid = secrets.token_hex(6)
        paths = await asyncio.get_running_loop().run_in_executor(None, _final, ctx, r, uid, oid)
    except Exception as e:
        return web.json_response({"error": str(e)[:200]}, status=400)
    title = (str(d.get("title", "")).strip() or "Emoji")[:64]
    kind, nick = r["kind"], ctx._random_nick()
    pack_kind = "sticker" if kind == "name" and d.get("pack_kind") == "sticker" else "emoji"
    price = ctx.load_price(kind) * len(paths)
    user = User(id=uid, is_bot=False, first_name=u.get("first_name", "-"), username=u.get("username"))
    rec = {"user": uid, "paths": paths, "nick": nick, "pack_kind": pack_kind, "title": title,
           "kind": kind, "price": price, "status": "pending", "ts": int(time.time())}
    orders = _orders()
    if ctx.is_admin(uid):
        rec["status"] = "done"
        orders[oid] = rec
        _save(orders, ctx)
        asyncio.create_task(ctx._finalize_pack(bot, uid, paths, nick, pack_kind, user=user, title=title))
        return web.json_response({"status": "started"})
    orders[oid] = rec
    _save(orders, ctx)
    link = await bot.create_invoice_link(
        title=f"Emoji Pack ({nick})", description=f"{len(paths)} ta animatsiyali emoji/stiker",
        payload=f"webpack:{oid}", provider_token="", currency="XTR",
        prices=[LabeledPrice(label="Emoji Pack", amount=price)])
    return web.json_response({"status": "invoice", "link": link, "price": price})


async def _say(bot, chat, text):
    try:
        if chat:
            await bot.send_message(chat, text)
    except Exception as e:
        logging.warning("send failed: %s", e)


def _who(stats, k):  # maxfiylik: username qisman yashiriladi
    un = stats.get(str(k), {}).get("username")
    return "@" + un[:2] + "•••" if un else "ID …" + str(k)[-4:]


def _standings(ctx, c):
    ban = _banned()
    return sorted(((int(k), n) for k, n in c.get("counts", {}).items() if not ctx.is_admin(int(k)) and int(k) not in ban),
                  key=lambda x: -x[1])


async def _send_prize(bot, ctx):
    ad = _ad()
    c = ad["contest"]
    w, st = c.get("winner"), ctx.load_stats()
    if not w:
        return
    try:
        gifts = (await bot.get_available_gifts()).gifts
        g = next((x for x in sorted(gifts, key=lambda x: x.star_count) if x.star_count == c["prize"]
                  and getattr(x, "remaining_count", None) != 0 and not getattr(x, "is_premium", False)), None)
        if not g:
            raise RuntimeError(f"{c['prize']} ⭐ lik gift topilmadi")
        await bot.send_gift(gift_id=g.id, user_id=w, text="🏆 Konkurs g'olibi! Tabriklaymiz!", pay_for_upgrade=False)
        c["gift_sent"], c["note"] = True, ""
    except Exception as e:
        c["gift_sent"], c["note"] = False, str(e)[:150]
    _dump(APPDATA, ad)
    if c["gift_sent"]:
        await _say(bot, w, f"🎉 Tabriklaymiz! Konkursda g'olib bo'ldingiz ({c.get('best', 0)} ta emoji). {c['prize']} ⭐ lik gift sizga yuborildi!")
        tie = f"\n🎲 {c['tied']} kishi teng chiqdi, tasodifiy tanlandi." if c.get("tied", 1) > 1 else ""
        await _say(bot, ctx.LOG_CHAT_ID, f"🎁 Konkurs g'olibi: {_who(st, w)} (id {w}), {c.get('best', 0)} ta emoji. Gift yuborildi ✅{tie}")
    else:
        await _say(bot, ctx.LOG_CHAT_ID, f"⚠️ Konkurs g'olibi: id {w}, lekin gift yuborilmadi: {c['note']}\nBalansni to'ldirib, ilovada Admin → Konkurs → «Giftni qayta yuborish» ni bosing.")


async def _finish(bot, ctx):
    ad = _ad()
    c = ad.get("contest")
    if not c or c["status"] != "active":
        return
    rows = [r for r in _standings(ctx, c) if r[1] >= c["min"]]
    c.update(status="finished", finished=int(time.time()), winner=None)
    if rows:
        tied = [u for u, n in rows if n == rows[0][1]]
        c.update(winner=random.choice(tied), best=rows[0][1], tied=len(tied))
    _dump(APPDATA, ad)
    st = ctx.load_stats()
    top = "\n".join(f"{i + 1}. {_who(st, u)} — {n}" for i, (u, n) in enumerate(_standings(ctx, c)[:5])) or "—"
    await _say(bot, ctx.LOG_CHAT_ID, f"🏆 Konkurs yakunlandi!\nShartga mos ishtirokchilar: {len(rows)}\n{top}")
    if c["winner"]:
        await _send_prize(bot, ctx)


def _start_contest(ad, days=30, prize=15, mn=5):
    now = int(time.time())
    ad["contest"] = {"status": "active", "start": now, "end": now + days * 86400, "prize": prize, "min": mn,
                     "counts": {}, "winner": None, "gift_sent": False, "note": ""}


async def contest_loop(bot, ctx):
    """Har daqiqada: muddat tugagan konkursni yakunlaydi; konkurs bo'lmasa yoki tugab 1 soat o'tgan bo'lsa yangisini boshlaydi."""
    while True:
        try:
            c = _ad().get("contest")
            if c and c["status"] == "active" and time.time() >= c["end"]:
                await _finish(bot, ctx)
                c = _ad().get("contest")
            ad = _ad()
            done = not c or (c["status"] == "finished" and (c.get("gift_sent") or not c.get("winner"))
                             and time.time() - c.get("finished", 0) > 3600)
            if done and ad.get("cfg", {}).get("auto", 1):
                if c:
                    h = ad.setdefault("history", [])
                    h.append({k: c.get(k) for k in ("end", "prize", "best", "winner", "tied")})
                    ad["history"] = h[-10:]
                _start_contest(ad)
                _dump(APPDATA, ad)
                await _say(bot, ctx.LOG_CHAT_ID, "🏆 Yangi konkurs boshlandi!\n🎁 Sovrin: 15 ⭐ lik gift\n⏳ Davomiyligi: 30 kun\n✅ Shart: kamida 5 ta emoji yasash")
        except Exception:
            logging.exception("contest loop")
        await asyncio.sleep(60)


async def api_contest(request):
    u, ctx = await _auth(request), request.app["ctx"]
    ad = _ad()
    c, st = ad.get("contest"), ctx.load_stats()
    hist = [{"who": _who(st, h["winner"]), "n": h.get("best", 0), "prize": h.get("prize", 15)}
            for h in reversed(ad.get("history", [])[-5:]) if h.get("winner")]
    if not c:
        return web.json_response({"none": True, "prize": 15, "min": 5, "history": hist})
    rows = _standings(ctx, c)
    return web.json_response({
        "status": c["status"], "start": c["start"], "end": c["end"], "prize": c["prize"], "min": c["min"],
        "mine": int(c.get("counts", {}).get(str(u["id"]), 0)), "players": sum(1 for _, n in rows if n >= c["min"]),
        "rank": next((i + 1 for i, (k, _) in enumerate(rows) if k == u["id"]), None), "total": len(rows), "history": hist,
        "top": [{"who": _who(st, k), "n": n, "me": k == u["id"]} for k, n in rows[:10]],
        "winner": _who(st, c["winner"]) if c.get("winner") else None, "you_won": c.get("winner") == u["id"], "tied": c.get("tied", 1)})


_thumbs: dict = {}


async def api_thumbs(request):
    """Shablon galereyasi: 10 tagacha shablonning kichik animatsiyasi (kesh bilan)."""
    await _auth(request)
    ctx = request.app["ctx"]
    try:
        d = await request.json()
        out = {}
        for it in list(d.get("items", []))[:10]:
            extra = {"template": it, "word": d.get("word") or "Ism"} if d.get("kind") == "name" else {"n": it, "text": d.get("text") or "Abc"}
            r = _clean(ctx, {**d, **extra})
            key = json.dumps(r, sort_keys=True)
            if key not in _thumbs:
                _thumbs[key] = (await asyncio.get_running_loop().run_in_executor(None, _preview, ctx, r))[0]
                if len(_thumbs) > 300:
                    _thumbs.pop(next(iter(_thumbs)))
            out[str(it)] = _thumbs[key]
    except Exception as e:
        return web.json_response({"error": str(e)[:200]}, status=400)
    resp = web.json_response({"anims": out})
    resp.enable_compression()
    return resp


async def api_packs(request):
    u = await _auth(request)
    return web.json_response({"packs": _ad().get("packs", {}).get(str(u["id"]), [])[::-1][:30]})


async def api_top(request):
    await _auth(request)
    ctx = request.app["ctx"]
    stats = ctx.load_stats()
    top = sorted(((k, e.get("packs", 0)) for k, e in stats.items()), key=lambda x: x[1], reverse=True)[:10]
    return web.json_response({"packs": [{"who": _who(stats, k), "n": n} for k, n in top if n]})


async def _broadcast(bot, ctx, text, admin_id):
    ok = bad = 0
    ban = _banned()
    for uid in list(ctx.load_users()):
        if uid in ban:
            continue
        try:
            await bot.send_message(uid, text)
            ok += 1
        except Exception:
            bad += 1
        await asyncio.sleep(0.06)
    try:
        await bot.send_message(admin_id, f"📣 Xabar yuborildi: {ok} ta, yetib bormadi: {bad} ta")
    except Exception:
        pass


async def api_admin(request):
    u, ctx, bot = await _auth(request), request.app["ctx"], request.app["bot"]
    if not ctx.is_admin(u["id"]):
        raise web.HTTPForbidden(text="Admin emas")
    d = await request.json()
    a, v, owner = d.get("action"), d.get("value"), ctx.is_owner(u["id"])
    try:
        if a in ("user_info", "ban", "unban"):
            uid = int(v)
            if a == "ban" and ctx.is_admin(uid):
                raise ValueError("Adminni bloklab bo'lmaydi")
            if a != "user_info":
                b = _banned()
                b.add(uid) if a == "ban" else b.discard(uid)
                _dump(BANNED, sorted(b))
            return web.json_response({"user": await _uinfo(bot, ctx, uid)})
        if a == "price" and d.get("kind") in ("name", "logo") and int(v) > 0:
            old = ctx.load_price(d["kind"])
            ctx.save_price(d["kind"], int(v))
            await ctx.notify_price_change(bot, d["kind"], old, int(v), "@" + u["username"] if u.get("username") else f"id {u['id']}")
        elif a == "broadcast":
            text = str(v or "").strip()
            if not 1 <= len(text) <= 1000:
                raise ValueError("Xabar 1–1000 belgi bo'lsin")
            asyncio.create_task(_broadcast(bot, ctx, text, u["id"]))
        elif a in ("admin_add", "admin_del") and owner:
            s = ctx.load_admins()
            s.add(int(v)) if a == "admin_add" else s.discard(int(v))
            ctx.save_admins(s)
        elif a == "channels":
            raw = str(v or "").replace(",", " ").split()
            ctx.set_channels([] if raw == ["off"] else [c if c.startswith("@") else "@" + c for c in raw])
        elif a == "support" and str(v or "").strip():
            ctx.set_support_contact(str(v).strip())
        elif a in ("maint_set", "auto_set"):
            ad = _ad()
            ad.setdefault("cfg", {})["maint" if a == "maint_set" else "auto"] = 1 if int(v) else 0
            _dump(APPDATA, ad)
        elif a == "contest_start":
            days, (prize, mn) = int(v), (int(x) for x in str(d.get("kind", "")).split(":"))
            ad = _ad()
            if ad.get("contest", {}).get("status") == "active":
                raise ValueError("Konkurs allaqachon davom etmoqda")
            if not (1 <= days <= 90 and prize >= 1 and mn >= 1):
                raise ValueError("Qiymatlar noto'g'ri")
            _start_contest(ad, days, prize, mn)
            _dump(APPDATA, ad)
            await _say(bot, ctx.LOG_CHAT_ID, f"🏆 Konkurs boshlandi!\n🎁 Sovrin: {prize} ⭐ lik gift\n⏳ {days} kun\n✅ Shart: kamida {mn} ta emoji")
        elif a == "contest_stop":
            await _finish(bot, ctx)
        elif a == "contest_retry":
            c = _ad().get("contest")
            if c and c.get("winner") and not c.get("gift_sent"):
                await _send_prize(bot, ctx)
        elif a != "overview":
            raise ValueError("Ruxsat yo'q yoki noto'g'ri amal")
    except (ValueError, KeyError, TypeError) as e:
        return web.json_response({"error": str(e)[:200] or "Noto'g'ri qiymat"}, status=400)
    ad, stats = _ad(), ctx.load_stats()
    top = lambda key: [{"who": ("@" + e["username"]) if e.get("username") else "id " + k, "n": e.get(key, 0)}
                       for k, e in sorted(stats.items(), key=lambda kv: kv[1].get(key, 0), reverse=True)[:10] if e.get(key, 0)]
    days = [time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400)) for i in range(6, -1, -1)]
    orders = sorted(_orders().values(), key=lambda o: o.get("ts", 0), reverse=True)[:8]
    c = ad.get("contest")
    return web.json_response({
        "users": len(ctx.load_users()), "prices": {k: ctx.load_price(k) for k in ("name", "logo")}, "owner": owner,
        "admins": sorted(ctx.load_admins()) if owner else None, "banned": sorted(_banned()),
        "channels": " ".join(ctx.get_channels()), "support": ctx.get_support_contact(),
        "packs_total": sum(e.get("packs", 0) for e in stats.values()), "stars_total": sum(e.get("stars", 0) for e in stats.values()),
        "today": ad.get("days", {}).get(days[-1], 0), "chart": [{"d": x[5:], "n": ad.get("days", {}).get(x, 0)} for x in days],
        "orders": [{"u": o["user"], "k": o["kind"], "p": o["price"], "s": o["status"]} for o in orders],
        "maint": int(ad.get("cfg", {}).get("maint", 0)), "auto": int(ad.get("cfg", {}).get("auto", 1)),
        "top_packs": top("packs"), "top_stars": top("stars"),
        "contest": None if not c else {**{k: c.get(k) for k in ("status", "start", "end", "prize", "min", "gift_sent", "note", "winner")},
                                       "players": sum(1 for n in c.get("counts", {}).values() if n >= c["min"])}})


@web.middleware
async def cors(request, handler):
    """Mini App Netlify'da turganda API'ga faqat WEBAPP_ORIGIN manzilidan kirishga ruxsat beradi."""
    allowed = os.environ.get("WEBAPP_ORIGIN", "").strip().rstrip("/")
    h = {}
    if allowed and request.headers.get("Origin", "") == allowed:
        h = {"Access-Control-Allow-Origin": allowed, "Access-Control-Allow-Headers": "Content-Type, X-Init-Data",
             "Access-Control-Allow-Methods": "POST, OPTIONS", "Vary": "Origin"}
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=h)
    try:
        resp = await handler(request)
    except web.HTTPException as e:
        e.headers.update(h)
        raise
    resp.headers.update(h)
    return resp


async def _own_config(_):
    # Sahifa shu serverning o'zidan ochilsa, API manzili nisbiy bo'ladi
    return web.Response(text='window.API_BASE="";', content_type="application/javascript")


def _page(name, ctype):
    async def h(_):
        return web.FileResponse(os.path.join(WEB, name), headers={"Content-Type": ctype, "Cache-Control": "no-store"})
    return h


def setup(dp, ctx):
    """Asosiy router'dan OLDIN chaqiring: Mini App to'lovlarini (webpack:) qabul qiladi."""
    r = Router(name="webapp")
    guard = Guard(ctx)
    dp.message.outer_middleware(guard)
    dp.callback_query.outer_middleware(guard)

    @r.message(Command("brand"))
    async def brand(m: Message):
        if not ctx.is_owner(m.from_user.id):
            return
        try:
            await m.bot.set_my_name(name="Anonim Emoji Bot")
            await m.bot.set_my_short_description(short_description="Animatsiyali premium emoji yasang 🕶")
            await m.bot.set_my_profile_photo(photo=InputProfilePhotoStatic(photo=FSInputFile(os.path.join(WEB, "avatar.jpg"))))
            await m.answer("✅ Bot nomi va avatari o'rnatildi.")
        except Exception as e:
            await m.answer(f"⚠️ To'liq o'rnatib bo'lmadi: {e}\nAvatarni @BotFather → /setuserpic orqali qo'ying (fayl: webapp/avatar.jpg).")

    @r.callback_query(F.data.in_({"section:name", "section:logo"}))
    async def moved(c: CallbackQuery):
        await c.answer()
        url = os.environ.get("WEBAPP_URL", "").strip()
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="🚀 Ilovani ochish", web_app=WebAppInfo(url=url), style="success")]]) if url else None
        await c.message.answer("Emoji endi faqat ilovada yaratiladi 👇", reply_markup=kb)

    @r.pre_checkout_query(F.invoice_payload.startswith("webpack:"))
    async def pre(q: PreCheckoutQuery):
        o = _orders().get(q.invoice_payload.split(":", 1)[1])
        ok = o and o["status"] == "pending" and o["user"] == q.from_user.id and o["price"] == q.total_amount
        await q.answer(ok=bool(ok), error_message=None if ok else "Buyurtma eskirgan yoki narx o'zgargan.")

    @r.message(F.successful_payment.invoice_payload.startswith("webpack:"))
    async def paid(m: Message):
        sp, orders = m.successful_payment, _orders()
        oid = sp.invoice_payload.split(":", 1)[1]
        o, amount = orders.get(oid), sp.total_amount or 0
        ctx.record_stars_spent(m.from_user.id, m.from_user.username, amount)
        if not o or o["status"] != "pending" or o["price"] != amount or o["user"] != m.from_user.id:
            ref = ctx.create_refund_request(m.from_user.id, sp.telegram_payment_charge_id, amount)
            await m.answer(f"❌ Buyurtma tasdiqlanmadi (eskirgan yoki narx o'zgargan). {ctx.get_support_contact()} ga yozing.")
            try:
                await m.bot.send_message(ctx.LOG_CHAT_ID, f"⚠️ Mini App to'lovi tasdiqlanmadi: id {m.from_user.id}, {amount} ⭐",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                        text=f"💸 {amount} ⭐ ni qaytarish", callback_data=f"refundcode:{ref}")]]))
            except Exception:
                logging.warning("log post failed")
            return
        o["status"] = "done"
        _save(orders, ctx)
        await ctx._finalize_pack(m.bot, m.chat.id, o["paths"], o["nick"], o["pack_kind"], user=m.from_user, title=o["title"])

    dp.include_router(r)


async def run_server(bot, ctx):
    url = os.environ.get("WEBAPP_URL", "").strip()
    if not url:
        logging.info("WEBAPP_URL berilmagan — Mini App o'chiq")
        return
    app = web.Application(client_max_size=64 * 1024, middlewares=[cors])
    app["bot"], app["ctx"] = bot, ctx
    app.add_routes([web.get("/", _page("index.html", "text/html; charset=utf-8")),
                    web.get("/lottie.js", _page("lottie.min.js", "application/javascript")),
                    web.get("/lottie.min.js", _page("lottie.min.js", "application/javascript")),
                    web.get("/config.js", _own_config), web.get("/avatar.jpg", _page("avatar.jpg", "image/jpeg")),
                    web.post("/api/config", api_config), web.post("/api/preview", api_preview),
                    web.post("/api/order", api_order), web.post("/api/packs", api_packs),
                    web.post("/api/top", api_top), web.post("/api/contest", api_contest), web.post("/api/thumbs", api_thumbs), web.post("/api/admin", api_admin)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, os.environ.get("WEBAPP_HOST", "0.0.0.0"), int(os.environ.get("WEBAPP_PORT", "8080"))).start()
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Emoji yaratish", web_app=WebAppInfo(url=url)))
    except Exception:
        logging.exception("menu tugmasi o'rnatilmadi")
