import os, io, re, time, random, asyncio, logging
import aiosqlite
from PIL import Image, ImageDraw, ImageFont, ImageOps
from vkbottle import Bot, PhotoMessageUploader, Keyboard, Text, KeyboardButtonColor
from vkbottle.bot import Message

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("VK_TOKEN", "vk1.a.zxiA4gfk415niAe_EHA-urx3VEjA7SGCkX-v9fB9U6_54bpykh_9brA0XfgtnuKjSInJVziAoo0czDtN4noKJwqR2m2gLiEoq4-duwClOLGWrsY3Xj6nvccuCFX_wtxwO_0AJpjD0Ycnt9xbxGBNw_IP-14JjdeoOEKC-uvTcVOniIdNJorY8zxh5cC_O7joIs2sXy5KNtyszPOSkNO3YQ")
OWNER_ID = int(os.getenv("OWNER_ID", "1020224655"))
DB_PATH = os.getenv("DB_PATH", "bomzh.db")
BOT_NAME = "Король Помойки"

bot = Bot(token=TOKEN)
uploader = PhotoMessageUploader(bot.api)

P, G, N, R = (KeyboardButtonColor.PRIMARY, KeyboardButtonColor.POSITIVE,
              KeyboardButtonColor.SECONDARY, KeyboardButtonColor.NEGATIVE)

ROLES = ["Бомж", "Модератор", "Админ", "Владелец"]
COOLDOWN = {"bottles": 45, "beg": 60, "trash": 90, "job": 600, "sleep": 300, "fight": 300, "casino": 5}
ACTIONS = {"бутылки": "bottles", "попрошайничать": "beg", "мусорка": "trash", "подработка": "job"}

ITEMS = {
    "хлеб":    {"price": 30,  "food": 25, "mood": 0,  "hp": 0,   "e": "🍞"},
    "шаурма":  {"price": 120, "food": 60, "mood": 10, "hp": 0,   "e": "🌯"},
    "водка":   {"price": 80,  "food": 0,  "mood": 35, "hp": -10, "e": "🍾"},
    "аптечка": {"price": 200, "food": 0,  "mood": 0,  "hp": 50,  "e": "💊"},
}
HOMES = [("Картонка", 0), ("Палатка", 500), ("Гараж", 3000), ("Комната", 15000), ("Ларёк", 100000)]
BOTTLE_PRICE = 3
CLAN_COST = 5000
CLAN_MAX = 20
CASINO_MIN, CASINO_MAX = 10, 50000

FONT_PATHS = ["fonts/font.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "arial.ttf"]

# ---------------- DB ----------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
 id INTEGER PRIMARY KEY, name TEXT, role INTEGER DEFAULT 0,
 money INTEGER DEFAULT 50, bottles INTEGER DEFAULT 0,
 hp INTEGER DEFAULT 100, food INTEGER DEFAULT 70, mood INTEGER DEFAULT 60,
 rep INTEGER DEFAULT 0, home INTEGER DEFAULT 0,
 banned INTEGER DEFAULT 0, ban_reason TEXT DEFAULT '',
 last_daily INTEGER DEFAULT 0, ref_by INTEGER DEFAULT 0, refs INTEGER DEFAULT 0,
 created INTEGER);
CREATE TABLE IF NOT EXISTS inv(uid INTEGER, item TEXT, qty INTEGER, PRIMARY KEY(uid,item));
CREATE TABLE IF NOT EXISTS cooldowns(uid INTEGER, act TEXT, until REAL, PRIMARY KEY(uid,act));
CREATE TABLE IF NOT EXISTS promos(code TEXT PRIMARY KEY, money INTEGER, max_uses INTEGER,
 uses INTEGER DEFAULT 0, expires INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS promo_used(code TEXT, uid INTEGER, PRIMARY KEY(code,uid));
CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, actor INTEGER, text TEXT);
CREATE TABLE IF NOT EXISTS clans(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE COLLATE NOCASE,
 owner INTEGER, bank INTEGER DEFAULT 0, created INTEGER);
"""
MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN clan INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN wins INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN losses INTEGER DEFAULT 0",
]
_db = None
_lock = asyncio.Lock()


async def db():
    global _db
    async with _lock:
        if _db is None:
            _db = await aiosqlite.connect(DB_PATH)
            _db.row_factory = aiosqlite.Row
            await _db.executescript(SCHEMA)
            for m in MIGRATIONS:
                try:
                    await _db.execute(m)
                except Exception:
                    pass
            await _db.commit()
    return _db


async def q1(sql, args=()):
    d = await db()
    c = await d.execute(sql, args)
    r = await c.fetchone()
    await c.close()
    return dict(r) if r else None


async def qa(sql, args=()):
    d = await db()
    c = await d.execute(sql, args)
    r = await c.fetchall()
    await c.close()
    return [dict(x) for x in r]


async def ex(sql, args=()):
    d = await db()
    await d.execute(sql, args)
    await d.commit()


async def upd(uid, **kw):
    sets = ", ".join(f"{k}=?" for k in kw)
    await ex(f"UPDATE users SET {sets} WHERE id=?", (*kw.values(), uid))


async def log(actor, text):
    await ex("INSERT INTO logs(ts,actor,text) VALUES(?,?,?)", (int(time.time()), actor, text))


async def cd_left(uid, act):
    r = await q1("SELECT until FROM cooldowns WHERE uid=? AND act=?", (uid, act))
    return max(int(r["until"] - time.time()), 0) if r else 0


async def cd_set(uid, act, sec):
    await ex("INSERT OR REPLACE INTO cooldowns(uid,act,until) VALUES(?,?,?)", (uid, act, time.time() + sec))


async def add_item(uid, item, n=1):
    await ex("INSERT INTO inv(uid,item,qty) VALUES(?,?,?) ON CONFLICT(uid,item) DO UPDATE SET qty=qty+?",
             (uid, item, n, n))


async def take_item(uid, item):
    r = await q1("SELECT qty FROM inv WHERE uid=? AND item=?", (uid, item))
    if not r or r["qty"] < 1:
        return False
    if r["qty"] == 1:
        await ex("DELETE FROM inv WHERE uid=? AND item=?", (uid, item))
    else:
        await ex("UPDATE inv SET qty=qty-1 WHERE uid=? AND item=?", (uid, item))
    return True


async def get_user(message):
    uid = message.from_id
    u = await q1("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        try:
            info = await message.get_user()
            name = f"{info.first_name} {info.last_name}"
        except Exception:
            name = f"id{uid}"
        await ex("INSERT INTO users(id,name,role,created) VALUES(?,?,?,?)",
                 (uid, name, 3 if uid == OWNER_ID else 0, int(time.time())))
        u = await q1("SELECT * FROM users WHERE id=?", (uid,))
    elif uid == OWNER_ID and u["role"] != 3:
        await upd(uid, role=3)
        u["role"] = 3
    return u


# ---------------- utils ----------------
def clamp(x):
    return max(0, min(100, int(x)))


def fmt(n):
    return f"{int(n):,}".replace(",", " ")


def short(s, n=18):
    return s if len(s) <= n else s[:n - 1] + "…"


def font(size):
    for p in FONT_PATHS:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


# ---------------- images ----------------
W, H = 900, 500


def load_bg(name):
    for ext in ("jpg", "jpeg", "png"):
        p = f"img/{name}.{ext}"
        if os.path.exists(p):
            return ImageOps.fit(Image.open(p).convert("RGB"), (W, H))
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        d.line((0, y, W, y), fill=(int(25 + 40 * t), int(30 + 10 * t), int(60 + 50 * t)))
    return img


def make_card(title, lines=(), bars=(), bg="default"):
    img = load_bg(bg).convert("RGBA")
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(ov).rounded_rectangle((30, 30, W - 30, H - 30), radius=30, fill=(0, 0, 0, 150))
    img = Image.alpha_composite(img, ov)
    d = ImageDraw.Draw(img)
    d.text((60, 55), title, font=font(52), fill=(255, 214, 10))
    y = 150
    for ln in list(lines)[:6]:
        d.text((60, y), ln, font=font(32), fill=(255, 255, 255))
        y += 52
    d.text((60, H - 90), BOT_NAME.upper(), font=font(22), fill=(200, 200, 200))
    by = 150
    for label, val, color in bars:
        d.text((560, by), f"{label}: {val}", font=font(26), fill=(255, 255, 255))
        d.rounded_rectangle((560, by + 36, 840, by + 58), radius=10, fill=(60, 60, 60))
        if val > 3:
            d.rounded_rectangle((560, by + 36, 560 + int(280 * val / 100), by + 58), radius=10, fill=color)
        by += 85
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def bars_of(u):
    return [("Здоровье", u["hp"], (220, 60, 60)),
            ("Сытость", u["food"], (240, 160, 40)),
            ("Настроение", u["mood"], (70, 140, 240))]


# ---------------- keyboards ----------------
def kb_from(rows, inline=False):
    kb = Keyboard(one_time=False, inline=inline)
    for i, row in enumerate(rows):
        if i:
            kb.row()
        for label, color in row:
            kb.add(Text(label), color=color)
    return kb.get_json()


def main_kb(role=0):
    rows = [
        [("🍾 Бутылки", P), ("🙏 Попрошайничать", P)],
        [("🗑 Мусорка", P), ("🔨 Подработка", P)],
        [("💰 Сдать бутылки", G), ("😴 Спать", N)],
        [("👤 Профиль", N), ("🎒 Инвентарь", N)],
        [("🛒 Магазин", N), ("🏠 Жильё", N)],
        [("⚔️ Драка", R), ("🎰 Казино", R)],
        [("🏴 Клан", N), ("🏆 Топ", N)],
        [("🎁 Бонус", G)],
    ]
    if role >= 1:
        rows[-1].append(("⚙️ Админка", R))
    return kb_from(rows)


async def send(message, text, u=None, card=None, kb=None):
    att = None
    if card:
        try:
            att = await uploader.upload(file_source=card, peer_id=message.peer_id)
        except Exception:
            logging.exception("upload failed")
    if kb is None:
        kb = main_kb(u["role"] if u else 0)
    await message.answer(text, attachment=att, keyboard=kb)


async def card_send(message, u, text, title, lines=(), bars=(), bg="default", kb=None):
    card = await asyncio.to_thread(make_card, title, lines, bars, bg)
    await send(message, text, u, card, kb)


async def notify(uid, text):
    try:
        await bot.api.messages.send(user_id=uid, message=text, random_id=0)
    except Exception:
        pass


# ---------------- game ----------------
async def do_action(message, u, act):
    uid = u["id"]
    left = await cd_left(uid, act)
    if left > 0:
        return await send(message, f"⏳ Ещё {left} сек. Не части.", u)
    if act == "job" and (u["hp"] < 30 or u["food"] < 20):
        return await send(message, "🤒 Ты слишком слаб для работы. Поешь или полечись (🛒 Магазин).", u)

    mult = 1 + 0.2 * u["home"] + (0.1 if u.get("clan") else 0)
    money = bottles = hp = food = mood = rep = 0
    story, item = [], None
    r = random.random()

    if act == "bottles":
        title = "Бутылки"
        bottles = random.randint(2, 8)
        food = -5
        story.append(f"🍾 Собрал {bottles} бутылок")
        if r < .15:
            m = random.randint(20, 80)
            money += m
            story.append(f"💵 Под лавкой нашёл заначку: +{m} ₽")
        elif r < .25:
            loss = min(u["money"], random.randint(10, 30))
            money -= loss
            mood -= 5
            story.append(f"👮 Менты прогнали и отжали {loss} ₽")
    elif act == "beg":
        title = "Попрошайничество"
        m = int(random.randint(15, 60) * mult)
        if u["mood"] < 20:
            m //= 2
            story.append("😞 Вид грустный, подают хуже")
        money += m
        food, mood = -4, -3
        story.append(f"🙏 Настрелял {m} ₽")
        if r < .12:
            loss = min(u["money"] + m, random.randint(20, 70))
            money -= loss
            story.append(f"🦹 Мошенник развёл на {loss} ₽")
        elif r < .22:
            money += 100
            food += 10
            story.append("👵 Добрая бабушка дала 100 ₽ и пирожок")
    elif act == "trash":
        title = "Мусорка"
        food = -6
        if r < .5:
            item = "хлеб"
            story.append("🍞 В мусорке нашёл целый хлеб")
        elif r < .75:
            m = random.randint(10, 50)
            money += m
            story.append(f"💵 Нашёл в кармане старой куртки {m} ₽")
        elif r < .9:
            b = random.randint(3, 10)
            bottles += b
            story.append(f"🍾 Нашёл {b} бутылок")
        else:
            d = random.randint(15, 30)
            hp -= d
            mood -= 10
            story.append(f"🥊 Подрался с другим бомжом за контейнер: -{d} HP")
    else:
        title = "Подработка"
        m = int(random.randint(150, 350) * mult)
        money += m
        food, hp, mood, rep = -20, -10, -5, 1
        story.append(f"🔨 Разгрузил фуру, заплатили {m} ₽")

    await cd_set(uid, act, COOLDOWN[act])
    nm = max(0, u["money"] + money)
    nh, nf, nmo = clamp(u["hp"] + hp), clamp(u["food"] + food), clamp(u["mood"] + mood)
    if nf == 0:
        nh = clamp(nh - 10)
        story.append("🥴 Голодный, здоровье падает")
    if nh <= 0:
        nh = 20
        lost = nm // 10
        nm -= lost
        story.append(f"💀 Отключился... очнулся в больнице, потерял {lost} ₽")
    nb = u["bottles"] + bottles
    await upd(uid, money=nm, bottles=nb, hp=nh, food=nf, mood=nmo, rep=u["rep"] + rep)
    if item:
        await add_item(uid, item)
    u.update(money=nm, hp=nh, food=nf, mood=nmo)
    text = "\n".join(story) + f"\n\n💰 {fmt(nm)} ₽ | 🍾 {nb}"
    await card_send(message, u, text, title,
                    [f"Деньги: {fmt(nm)} ₽", f"Бутылки: {nb}"], bars_of(u), "work")


async def profile(message, u):
    home = HOMES[u["home"]][0]
    clan = await q1("SELECT name FROM clans WHERE id=?", (u["clan"],)) if u["clan"] else None
    cn = clan["name"] if clan else "нет"
    text = (f"👤 {u['name']}\n🆔 ID: {u['id']}\n🎖 Роль: {ROLES[u['role']]}\n"
            f"💰 {fmt(u['money'])} ₽ | 🍾 {u['bottles']}\n🏠 {home}\n⭐ Авторитет: {u['rep']}\n"
            f"🏴 Клан: {cn}\n⚔️ Победы/поражения: {u['wins']}/{u['losses']}\n"
            f"👥 Приглашено: {u['refs']}")
    await card_send(message, u, text, short(u["name"], 20),
                    [ROLES[u["role"]], f"Деньги: {fmt(u['money'])} ₽", f"Бутылки: {u['bottles']}",
                     f"Жильё: {home}", f"Клан: {short(cn, 14)}", f"Бои: {u['wins']}/{u['losses']}"],
                    bars_of(u), "profile")


async def shop(message, u):
    lines = [f"{k.capitalize()} - {v['price']} ₽" for k, v in ITEMS.items()]
    text = "🛒 Магазин:\n" + "\n".join(
        f"{v['e']} {k} - {v['price']} ₽ (сытость {v['food']:+d}, настроение {v['mood']:+d}, HP {v['hp']:+d})"
        for k, v in ITEMS.items()) + "\n\nКупить: «купить хлеб» или «купить хлеб 3»"
    rows = [[(f"Купить {k}", G) for k in list(ITEMS)[:2]], [(f"Купить {k}", G) for k in list(ITEMS)[2:]],
            [("🎒 Инвентарь", N), ("Меню", N)]]
    await card_send(message, u, text, "Магазин", lines, (), "shop", kb_from(rows))


async def buy(message, u, args):
    if not args:
        return await shop(message, u)
    name = args[0].lower()
    qty = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
    qty = max(1, min(qty, 50))
    if name not in ITEMS:
        return await send(message, "Такого товара нет. Смотри 🛒 Магазин.", u)
    cost = ITEMS[name]["price"] * qty
    if u["money"] < cost:
        return await send(message, f"💸 Не хватает денег. Надо {fmt(cost)} ₽.", u)
    await upd(u["id"], money=u["money"] - cost)
    await add_item(u["id"], name, qty)
    await send(message, f"{ITEMS[name]['e']} Купил {name} x{qty} за {fmt(cost)} ₽. Использовать: «юз {name}»", u)


async def inventory(message, u):
    rows_db = await qa("SELECT item, qty FROM inv WHERE uid=?", (u["id"],))
    rows_db = [r for r in rows_db if r["item"] in ITEMS]
    if not rows_db:
        return await send(message, "🎒 Пусто. Загляни в 🛒 Магазин или порыскай по 🗑 Мусорке.", u)
    text = "🎒 Инвентарь:\n" + "\n".join(f"{ITEMS[r['item']]['e']} {r['item']} x{r['qty']}" for r in rows_db)
    btns = [(f"Юз {r['item']}", P) for r in rows_db][:6]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)] + [[("Меню", N)]]
    await card_send(message, u, text, "Инвентарь",
                    [f"{r['item'].capitalize()}: {r['qty']}" for r in rows_db], (), "inv", kb_from(rows[:5]))


async def use_item(message, u, args):
    if not args or args[0].lower() not in ITEMS:
        return await send(message, "Напиши: «юз хлеб»", u)
    name = args[0].lower()
    if not await take_item(u["id"], name):
        return await send(message, "У тебя этого нет.", u)
    it = ITEMS[name]
    nh, nf, nm = clamp(u["hp"] + it["hp"]), clamp(u["food"] + it["food"]), clamp(u["mood"] + it["mood"])
    await upd(u["id"], hp=nh, food=nf, mood=nm)
    await send(message, f"{it['e']} Использовал {name}.\n❤️ {nh} | 🍗 {nf} | 😊 {nm}", u)


async def sell_bottles(message, u):
    if u["bottles"] <= 0:
        return await send(message, "У тебя нет бутылок. Сначала собери 🍾", u)
    gain = u["bottles"] * BOTTLE_PRICE
    await upd(u["id"], money=u["money"] + gain, bottles=0)
    await send(message, f"💰 Сдал {u['bottles']} бутылок в приёмку: +{fmt(gain)} ₽", u)


async def sleep_cmd(message, u):
    left = await cd_left(u["id"], "sleep")
    if left > 0:
        return await send(message, f"⏳ Ещё {left} сек. до следующего сна.", u)
    heal = 30 + 15 * u["home"]
    nh, nf, nm = clamp(u["hp"] + heal), clamp(u["food"] - 10), clamp(u["mood"] + 10)
    await upd(u["id"], hp=nh, food=nf, mood=nm)
    await cd_set(u["id"], "sleep", COOLDOWN["sleep"])
    await send(message, f"😴 Поспал ({HOMES[u['home']][0]}): +{heal} HP\n❤️ {nh} | 🍗 {nf} | 😊 {nm}", u)


async def daily(message, u):
    left = u["last_daily"] + 86400 - int(time.time())
    if left > 0:
        return await send(message, f"⏳ Бонус будет через {left // 3600} ч {left % 3600 // 60} мин.", u)
    gain = 100 * (u["home"] + 1)
    await upd(u["id"], money=u["money"] + gain, last_daily=int(time.time()), rep=u["rep"] + 1)
    u["money"] += gain
    await card_send(message, u, f"🎁 Ежедневный бонус: +{gain} ₽ и +1 авторитет", "Бонус",
                    [f"+{gain} RUB", "+1 авторитет"], (), "bonus")


async def top(message, u, args=()):
    mode = args[0].lower() if args else "деньги"
    if mode in ("драки", "бои"):
        rows = await qa("SELECT name, wins v FROM users WHERE banned=0 ORDER BY wins DESC LIMIT 10")
        head, unit = "⚔️ Топ бойцов:", "поб."
    elif mode in ("авторитет", "рейтинг"):
        rows = await qa("SELECT name, rep v FROM users WHERE banned=0 ORDER BY rep DESC LIMIT 10")
        head, unit = "⭐ Топ авторитетов:", ""
    else:
        rows = await qa("SELECT name, money v FROM users WHERE banned=0 ORDER BY money DESC LIMIT 10")
        head, unit = "🏆 Топ богачей:", "₽"
    text = head + "\n" + "\n".join(f"{i + 1}. {r['name']} - {fmt(r['v'])} {unit}" for i, r in enumerate(rows))
    text += "\n\nДругие топы: «топ драки», «топ авторитет», «клан топ»"
    lines = [f"{i + 1}. {short(r['name'])} - {fmt(r['v'])}" for i, r in enumerate(rows[:6])]
    await card_send(message, u, text, "Топ", lines, (), "top")


async def home_cmd(message, u, upgrade=False):
    h = u["home"]
    if upgrade:
        if h + 1 >= len(HOMES):
            return await send(message, "🏆 У тебя уже максимальное жильё.", u)
        name, price = HOMES[h + 1]
        if u["money"] < price:
            return await send(message, f"💸 Для «{name}» нужно {fmt(price)} ₽.", u)
        await upd(u["id"], money=u["money"] - price, home=h + 1)
        return await send(message, f"🏠 Поздравляю! Теперь у тебя: {name}.\nДоходы +20%, сон лечит лучше, бонус больше.", u)
    nxt = f"Следующее: {HOMES[h + 1][0]} за {fmt(HOMES[h + 1][1])} ₽" if h + 1 < len(HOMES) else "Максимум!"
    kb = kb_from([[("⬆️ Улучшить жильё", G)], [("Меню", N)]])
    await card_send(message, u, f"🏠 Сейчас: {HOMES[h][0]}\n{nxt}", "Жильё",
                    [f"Сейчас: {HOMES[h][0]}", nxt, f"Доход: +{20 * h}%"], (), "home", kb)


async def ref_cmd(message, u, args):
    if not args or not args[0].isdigit():
        return await send(message, f"👥 Твой ID для приглашений: {u['id']}\nДруг пишет «реф {u['id']}» - вы оба получите по 200 ₽.", u)
    inv = int(args[0])
    if u["ref_by"] or inv == u["id"]:
        return await send(message, "Нельзя.", u)
    owner = await q1("SELECT * FROM users WHERE id=?", (inv,))
    if not owner:
        return await send(message, "Игрок не найден.", u)
    await upd(u["id"], ref_by=inv, money=u["money"] + 200)
    await upd(inv, money=owner["money"] + 200, refs=owner["refs"] + 1)
    await send(message, "✅ Реферал засчитан! +200 ₽ вам обоим.", u)


# ---------------- PvP ----------------
async def fight(message, u, args):
    if not args or not args[0].isdigit():
        rows = await qa("SELECT id, name, money, hp FROM users WHERE banned=0 AND id!=? AND hp>=20 "
                        "ORDER BY RANDOM() LIMIT 5", (u["id"],))
        t = "⚔️ Драка: «драка ID»\nПобедитель отбирает до 10% денег (макс. 300 ₽), оба теряют HP.\n\nСоперники:\n"
        t += "\n".join(f"{r['name']} - ID {r['id']} ({fmt(r['money'])} ₽)" for r in rows) or "Пока никого."
        return await send(message, t, u)
    tid = int(args[0])
    if tid == u["id"]:
        return await send(message, "Сам с собой драться нельзя.", u)
    t = await q1("SELECT * FROM users WHERE id=?", (tid,))
    if not t or t["banned"]:
        return await send(message, "Игрок не найден.", u)
    left = await cd_left(u["id"], "fight")
    if left > 0:
        return await send(message, f"⏳ Руки болят. Ещё {left} сек.", u)
    if u["hp"] < 30:
        return await send(message, "🤕 Ты слишком слаб для драки. Полечись.", u)
    if t["hp"] < 20:
        return await send(message, "Лежачих не бьют. Он слишком слаб.", u)
    if u["clan"] and u["clan"] == t["clan"]:
        return await send(message, "🏴 Своих из клана бить нельзя.", u)

    pw_u = u["hp"] + u["rep"] * 2 + u["home"] * 10 + random.randint(0, 60)
    pw_t = t["hp"] + t["rep"] * 2 + t["home"] * 10 + random.randint(0, 60)
    win = pw_u >= pw_t
    w, l = (u, t) if win else (t, u)
    loot = min(l["money"] // 10, 300)
    w_hp, l_hp = clamp(w["hp"] - random.randint(5, 15)), clamp(l["hp"] - random.randint(15, 30))
    await upd(w["id"], money=w["money"] + loot, hp=w_hp, wins=w["wins"] + 1, rep=w["rep"] + 1)
    await upd(l["id"], money=l["money"] - loot, hp=l_hp, losses=l["losses"] + 1)
    await cd_set(u["id"], "fight", COOLDOWN["fight"])
    await log(u["id"], f"fight vs {tid} win={win} loot={loot}")
    u.update(hp=w_hp if win else l_hp)
    if win:
        u["money"] += loot
        text = f"🥊 Ты победил {t['name']}!\n💰 +{fmt(loot)} ₽, +1 авторитет\n❤️ HP: {u['hp']}"
        await notify(tid, f"🥊 {u['name']} напал на тебя и отжал {fmt(loot)} ₽. Ты потерял HP.")
    else:
        u["money"] -= loot
        text = f"🤕 {t['name']} тебя побил!\n💸 -{fmt(loot)} ₽\n❤️ HP: {u['hp']}"
        await notify(tid, f"🛡 {u['name']} полез к тебе, но ты отбился и забрал {fmt(loot)} ₽ (+1 авторитет).")
    await card_send(message, u, text, "Победа!" if win else "Поражение",
                    [f"Соперник: {short(t['name'], 16)}", f"{'+' if win else '-'}{fmt(loot)} RUB"],
                    bars_of(u), "fight")


# ---------------- casino ----------------
async def casino(message, u, args):
    if not args:
        return await send(message, f"🎰 Казино: «казино СУММА» или «казино всё»\nМин. {CASINO_MIN} ₽, макс. {fmt(CASINO_MAX)} ₽.\n"
                                    "🎯 40% - x2, 4% - x4 (джекпот), иначе проигрыш.", u,
                          kb=kb_from([[("Казино 100", P), ("Казино 500", P)], [("Казино 1000", P), ("Казино всё", R)],
                                      [("Меню", N)]]))
    a = args[0].lower()
    bet = u["money"] if a in ("всё", "все", "all") else (int(a) if a.isdigit() else 0)
    bet = min(bet, CASINO_MAX)
    if bet < CASINO_MIN:
        return await send(message, f"Минимальная ставка {CASINO_MIN} ₽.", u)
    if u["money"] < bet:
        return await send(message, "💸 Не хватает денег.", u)
    left = await cd_left(u["id"], "casino")
    if left > 0:
        return await send(message, f"⏳ Подожди {left} сек.", u)
    await cd_set(u["id"], "casino", COOLDOWN["casino"])
    r = random.random()
    if r < .04:
        delta, res = bet * 3, f"💎 ДЖЕКПОТ! x4: +{fmt(bet * 3)} ₽"
    elif r < .44:
        delta, res = bet, f"🎉 Выиграл x2: +{fmt(bet)} ₽"
    else:
        delta, res = -bet, f"😭 Проиграл: -{fmt(bet)} ₽"
    nm = u["money"] + delta
    await upd(u["id"], money=nm)
    kb = kb_from([[("Казино 100", P), ("Казино 500", P)], [("Казино 1000", P), ("Казино всё", R)], [("Меню", N)]])
    await send(message, f"{res}\n💰 Баланс: {fmt(nm)} ₽", u, kb=kb)


# ---------------- clans ----------------
async def clan_cmd(message, u, args):
    sub = args[0].lower() if args else "инфо"
    rest = " ".join(args[1:]).strip()
    if sub == "создать":
        if u["clan"]:
            return await send(message, "Ты уже в клане.", u)
        if not (3 <= len(rest) <= 20):
            return await send(message, f"Формат: клан создать НАЗВАНИЕ (3-20 символов). Цена {fmt(CLAN_COST)} ₽.", u)
        if u["money"] < CLAN_COST:
            return await send(message, f"💸 Нужно {fmt(CLAN_COST)} ₽.", u)
        if await q1("SELECT 1 FROM clans WHERE name=?", (rest,)):
            return await send(message, "Такое название уже занято.", u)
        await ex("INSERT INTO clans(name,owner,created) VALUES(?,?,?)", (rest, u["id"], int(time.time())))
        c = await q1("SELECT id FROM clans WHERE name=?", (rest,))
        await upd(u["id"], money=u["money"] - CLAN_COST, clan=c["id"])
        await log(u["id"], f"clan create {rest}")
        return await send(message, f"🏴 Клан «{rest}» создан! Бонус к доходу +10% всем участникам.\nПриглашай: «клан вступить {rest}»", u)
    if sub == "вступить":
        if u["clan"]:
            return await send(message, "Сначала выйди из текущего клана.", u)
        c = await q1("SELECT * FROM clans WHERE name=?", (rest,))
        if not c:
            return await send(message, "Клан не найден.", u)
        n = (await q1("SELECT COUNT(*) n FROM users WHERE clan=?", (c["id"],)))["n"]
        if n >= CLAN_MAX:
            return await send(message, "В клане нет мест.", u)
        await upd(u["id"], clan=c["id"])
        return await send(message, f"🏴 Ты вступил в клан «{c['name']}».", u)
    if sub == "выйти":
        if not u["clan"]:
            return await send(message, "Ты не в клане.", u)
        c = await q1("SELECT * FROM clans WHERE id=?", (u["clan"],))
        if c and c["owner"] == u["id"]:
            await ex("UPDATE users SET clan=0 WHERE clan=?", (c["id"],))
            await ex("DELETE FROM clans WHERE id=?", (c["id"],))
            return await send(message, "🏴 Ты вышел, клан распущен (ты был главой).", u)
        await upd(u["id"], clan=0)
        return await send(message, "Ты вышел из клана.", u)
    if sub == "взнос":
        if not u["clan"]:
            return await send(message, "Ты не в клане.", u)
        if not rest.isdigit() or int(rest) < 1 or int(rest) > u["money"]:
            return await send(message, "Формат: клан взнос СУММА (сумма должна быть у тебя в наличии).", u)
        s = int(rest)
        await upd(u["id"], money=u["money"] - s)
        await ex("UPDATE clans SET bank=bank+? WHERE id=?", (s, u["clan"]))
        return await send(message, f"💰 Внёс {fmt(s)} ₽ в казну клана.", u)
    if sub == "выдать":
        c = await q1("SELECT * FROM clans WHERE id=?", (u["clan"],)) if u["clan"] else None
        if not c or c["owner"] != u["id"]:
            return await send(message, "Только глава клана.", u)
        p = rest.split()
        if len(p) != 2 or not all(x.isdigit() for x in p):
            return await send(message, "Формат: клан выдать ID СУММА", u)
        t = await q1("SELECT * FROM users WHERE id=? AND clan=?", (int(p[0]), c["id"]))
        s = int(p[1])
        if not t or s < 1 or s > c["bank"]:
            return await send(message, "Игрок не в клане или в казне нет столько.", u)
        await ex("UPDATE clans SET bank=bank-? WHERE id=?", (s, c["id"]))
        await upd(t["id"], money=t["money"] + s)
        return await send(message, f"✅ {t['name']} получил {fmt(s)} ₽ из казны.", u)
    if sub == "топ":
        rows = await qa("SELECT c.name, c.bank, (SELECT COUNT(*) FROM users WHERE clan=c.id) m "
                        "FROM clans c ORDER BY c.bank DESC LIMIT 10")
        t = "🏴 Топ кланов:\n" + "\n".join(f"{i + 1}. {r['name']} - {fmt(r['bank'])} ₽ ({r['m']} чел.)"
                                          for i, r in enumerate(rows))
        return await send(message, t if rows else "Кланов пока нет.", u)
    # инфо
    if not u["clan"]:
        kb = kb_from([[("Клан топ", N)], [("Меню", N)]])
        return await send(message, f"🏴 Ты не в клане.\n\nклан создать НАЗВАНИЕ ({fmt(CLAN_COST)} ₽)\nклан вступить НАЗВАНИЕ\n"
                                    "клан топ", u, kb=kb)
    c = await q1("SELECT * FROM clans WHERE id=?", (u["clan"],))
    members = await qa("SELECT name, id FROM users WHERE clan=?", (c["id"],))
    owner = next((m["name"] for m in members if m["id"] == c["owner"]), "?")
    text = (f"🏴 Клан «{c['name']}»\n👑 Глава: {owner}\n👥 Участников: {len(members)}/{CLAN_MAX}\n"
            f"💰 Казна: {fmt(c['bank'])} ₽\n\nКоманды: клан взнос СУММА | клан выйти | клан выдать ID СУММА (глава)")
    kb = kb_from([[("Клан топ", N), ("Клан выйти", R)], [("Меню", N)]])
    await card_send(message, u, text, short(c["name"], 18),
                    [f"Глава: {short(owner, 16)}", f"Участников: {len(members)}", f"Казна: {fmt(c['bank'])} RUB",
                     "Бонус дохода: +10%"], (), "clan", kb)


# ---------------- promo ----------------
async def promo_use(message, u, code):
    code = code.upper()
    p = await q1("SELECT * FROM promos WHERE code=?", (code,))
    if not p:
        return await send(message, "❌ Такого промокода нет.", u)
    if p["expires"] and p["expires"] < time.time():
        return await send(message, "⌛ Промокод истёк.", u)
    if p["max_uses"] and p["uses"] >= p["max_uses"]:
        return await send(message, "😔 Промокод закончился.", u)
    if await q1("SELECT 1 FROM promo_used WHERE code=? AND uid=?", (code, u["id"])):
        return await send(message, "Ты уже активировал этот промокод.", u)
    await ex("INSERT INTO promo_used(code,uid) VALUES(?,?)", (code, u["id"]))
    await ex("UPDATE promos SET uses=uses+1 WHERE code=?", (code,))
    await upd(u["id"], money=u["money"] + p["money"])
    await send(message, f"🎉 Промокод активирован: +{fmt(p['money'])} ₽", u)


async def promo_admin(message, u, args):
    sub = args[0].lower()
    if u["role"] < 2:
        return await send(message, "⛔ Только для админов.", u)
    if sub == "создать":
        if len(args) < 4 or not all(a.isdigit() for a in args[2:5]):
            return await send(message, "Формат: промо создать КОД СУММА ЛИМИТ [ДНЕЙ]\nЛимит 0 = безлимит, дней 0 = бессрочно.", u)
        code, money, lim = args[1].upper(), int(args[2]), int(args[3])
        days = int(args[4]) if len(args) > 4 and args[4].isdigit() else 0
        exp = int(time.time() + days * 86400) if days else 0
        await ex("INSERT OR REPLACE INTO promos(code,money,max_uses,uses,expires) VALUES(?,?,?,0,?)",
                 (code, money, lim, exp))
        await log(u["id"], f"promo create {code} {money} lim={lim} days={days}")
        return await send(message, f"✅ Промокод {code}: {fmt(money)} ₽, лимит {lim or '∞'}, срок {days or '∞'} дн.", u)
    if sub == "удалить" and len(args) > 1:
        await ex("DELETE FROM promos WHERE code=?", (args[1].upper(),))
        await log(u["id"], f"promo delete {args[1].upper()}")
        return await send(message, "🗑 Удалено.", u)
    if sub == "список":
        rows = await qa("SELECT * FROM promos")
        t = "\n".join(f"{r['code']}: {fmt(r['money'])} ₽ ({r['uses']}/{r['max_uses'] or '∞'})" for r in rows)
        return await send(message, t or "Промокодов нет.", u)
    await send(message, "промо создать | промо удалить КОД | промо список", u)


# ---------------- admin ----------------
def admin_help(role):
    t = "⚙️ Админка\n\n👮 Модератор:\nбан ID [причина]\nразбан ID\nинфо ID\n"
    if role >= 2:
        t += ("\n🛠 Админ:\nвыдать ID СУММА (минус - забрать)\nпредмет ID ТОВАР КОЛ\n"
              "промо создать КОД СУММА ЛИМИТ [ДНЕЙ]\nпромо удалить КОД\nпромо список\n"
              "рассылка ТЕКСТ\nстата\nлоги\n")
    if role >= 3:
        t += "\n👑 Владелец:\nроль ID 0-2\n"
    return t


async def admin_cmds(message, u, cmd, args, raw):
    role = u["role"]
    if cmd in ("бан", "разбан"):
        if role < 1:
            return False
        if not args or not args[0].isdigit():
            await send(message, f"Формат: {cmd} ID", u)
            return True
        t = await q1("SELECT * FROM users WHERE id=?", (int(args[0]),))
        if not t:
            await send(message, "Игрок не найден.", u)
            return True
        if t["role"] >= role:
            await send(message, "⛔ Нельзя трогать равного или старшего.", u)
            return True
        if cmd == "бан":
            reason = " ".join(args[1:]) or "без причины"
            await upd(t["id"], banned=1, ban_reason=reason)
        else:
            await upd(t["id"], banned=0, ban_reason="")
        await log(u["id"], f"{cmd} {t['id']}")
        await send(message, f"✅ {cmd}: {t['name']} ({t['id']})", u)
        return True
    if cmd == "инфо":
        if role < 1:
            return False
        if not args or not args[0].isdigit():
            await send(message, "Формат: инфо ID", u)
            return True
        t = await q1("SELECT * FROM users WHERE id=?", (int(args[0]),))
        if not t:
            await send(message, "Игрок не найден.", u)
            return True
        await send(message, f"👤 {t['name']} ({t['id']})\n🎖 {ROLES[t['role']]} | 🚫 бан: {'да, ' + t['ban_reason'] if t['banned'] else 'нет'}\n"
                            f"💰 {fmt(t['money'])} ₽ | 🍾 {t['bottles']}\n❤️ {t['hp']} 🍗 {t['food']} 😊 {t['mood']}\n"
                            f"🏠 {HOMES[t['home']][0]} | ⭐ {t['rep']} | ⚔️ {t['wins']}/{t['losses']}", u)
        return True
    if role < 2 and cmd in ("выдать", "предмет", "рассылка", "стата", "логи"):
        return False
    if cmd == "выдать":
        try:
            t = await q1("SELECT * FROM users WHERE id=?", (int(args[0]),))
            amount = int(args[1])
        except Exception:
            await send(message, "Формат: выдать ID СУММА", u)
            return True
        if not t:
            await send(message, "Игрок не найден.", u)
            return True
        await upd(t["id"], money=max(0, t["money"] + amount))
        await log(u["id"], f"give {t['id']} {amount}")
        await send(message, f"✅ {t['name']}: {amount:+d} ₽", u)
        return True
    if cmd == "предмет":
        try:
            t = await q1("SELECT * FROM users WHERE id=?", (int(args[0]),))
            item = args[1].lower()
            qty = int(args[2]) if len(args) > 2 else 1
            assert item in ITEMS and 1 <= qty <= 1000
        except Exception:
            await send(message, f"Формат: предмет ID ТОВАР КОЛ\nТовары: {', '.join(ITEMS)}", u)
            return True
        if not t:
            await send(message, "Игрок не найден.", u)
            return True
        await add_item(t["id"], item, qty)
        await log(u["id"], f"item {t['id']} {item} x{qty}")
        await send(message, f"✅ {t['name']} получил {item} x{qty}", u)
        return True
    if cmd == "рассылка":
        parts = raw.split(maxsplit=1)
        if len(parts) < 2:
            await send(message, "Формат: рассылка ТЕКСТ", u)
            return True
        ids = [r["id"] for r in await qa("SELECT id FROM users WHERE banned=0")]
        ok = 0
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            try:
                await bot.api.messages.send(peer_ids=chunk, message=parts[1], random_id=0)
                ok += len(chunk)
            except Exception:
                logging.exception("broadcast chunk failed")
            await asyncio.sleep(0.4)
        await log(u["id"], f"broadcast {ok}")
        await send(message, f"📢 Рассылка отправлена: {ok}/{len(ids)}", u)
        return True
    if cmd == "стата":
        c = await q1("SELECT COUNT(*) n, COALESCE(SUM(money),0) m, SUM(banned) b FROM users")
        pc = await q1("SELECT COUNT(*) n FROM promos")
        cc = await q1("SELECT COUNT(*) n FROM clans")
        await send(message, f"📊 Игроков: {c['n']}\n💰 Всего денег: {fmt(c['m'])} ₽\n🚫 Забанено: {c['b'] or 0}\n"
                            f"🎟 Промокодов: {pc['n']}\n🏴 Кланов: {cc['n']}", u)
        return True
    if cmd == "логи":
        rows = await qa("SELECT * FROM logs ORDER BY id DESC LIMIT 10")
        t = "\n".join(f"{time.strftime('%d.%m %H:%M', time.localtime(r['ts']))} [{r['actor']}] {r['text']}" for r in rows)
        await send(message, t or "Логов нет.", u)
        return True
    if cmd == "роль":
        if role < 3:
            return False
        try:
            t = await q1("SELECT * FROM users WHERE id=?", (int(args[0]),))
            nr = int(args[1])
            assert 0 <= nr <= 2
        except Exception:
            await send(message, "Формат: роль ID 0-2 (0 игрок, 1 модер, 2 админ)", u)
            return True
        if not t:
            await send(message, "Игрок не найден.", u)
            return True
        await upd(t["id"], role=nr)
        await log(u["id"], f"role {t['id']} -> {nr}")
        await send(message, f"✅ {t['name']} теперь: {ROLES[nr]}", u)
        return True
    return False


# ---------------- handler ----------------
@bot.on.message()
async def handler(message: Message):
    uid = message.from_id
    if uid <= 0:
        return
    raw = re.sub(r"^[^\w]+", "", message.text or "").strip()
    if not raw:
        return
    parts = raw.split()
    cmd, args = parts[0].lower(), parts[1:]
    private = message.peer_id == message.from_id
    u = await get_user(message)

    if u["banned"]:
        if private:
            await message.answer(f"🚫 Ты забанен. Причина: {u['ban_reason']}")
        return

    if cmd in ACTIONS:
        return await do_action(message, u, ACTIONS[cmd])
    if cmd in ("start", "начать", "старт", "меню", "help", "помощь"):
        return await send(message,
            f"👑 Добро пожаловать в «{BOT_NAME}»!\nСобирай бутылки, клянчи, рыскай по мусоркам, дерись, играй в казино, "
            "создавай клан и выбирайся в люди.\n\n"
            "Команды: профиль, магазин, купить, юз, инвентарь, сдать бутылки, спать, бонус, жильё, топ, "
            "драка ID, казино СУММА, клан, промо КОД, реф ID", u)
    if cmd == "профиль":
        return await profile(message, u)
    if cmd == "магазин":
        return await shop(message, u)
    if cmd == "купить":
        return await buy(message, u, args)
    if cmd == "инвентарь":
        return await inventory(message, u)
    if cmd in ("юз", "использовать"):
        return await use_item(message, u, args)
    if cmd == "сдать":
        return await sell_bottles(message, u)
    if cmd == "спать":
        return await sleep_cmd(message, u)
    if cmd == "бонус":
        return await daily(message, u)
    if cmd == "топ":
        return await top(message, u, args)
    if cmd in ("жильё", "жилье"):
        return await home_cmd(message, u)
    if cmd == "улучшить":
        return await home_cmd(message, u, upgrade=True)
    if cmd == "реф":
        return await ref_cmd(message, u, args)
    if cmd in ("драка", "бой", "напасть"):
        return await fight(message, u, args)
    if cmd in ("казино", "ставка"):
        return await casino(message, u, args)
    if cmd == "клан":
        return await clan_cmd(message, u, args)
    if cmd == "промо":
        if not args:
            return await send(message, "Напиши: промо КОД", u)
        if args[0].lower() in ("создать", "удалить", "список"):
            return await promo_admin(message, u, args)
        return await promo_use(message, u, args[0])
    if cmd == "админка" and u["role"] >= 1:
        rows = [[("стата", N), ("логи", N)], [("промо список", N), ("Меню", G)]] if u["role"] >= 2 else [[("Меню", G)]]
        return await send(message, admin_help(u["role"]), u, kb=kb_from(rows))
    if await admin_cmds(message, u, cmd, args, raw):
        return
    if private:
        await send(message, "Не понял. Жми кнопки или напиши «меню».", u)


if __name__ == "__main__":
    bot.run_forever()
