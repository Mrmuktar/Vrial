"""
Bangla Viral — Telegram video bot + "Viral Zone" Mini App
Python 3.10+  |  aiogram 3  |  aiohttp  |  SQLite  |  optional Firebase Firestore sync
Run:  python main.py
"""
import asyncio, hashlib, hmac, json, logging, os, random, sqlite3, time
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl, quote

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BufferedInputFile, CallbackQuery, KeyboardButton, MenuButtonWebApp, Message,
                           ReplyKeyboardMarkup, WebAppInfo)
from aiogram.utils.keyboard import InlineKeyboardBuilder

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO)

# ───────────────────────── config ─────────────────────────
TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(i) for i in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if i}
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")          # public https URL of this server
PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "data.db")
MENU_TEXT = os.getenv("MENU_BUTTON_TEXT", "Viral Zone")
AUTO_MINUTES = int(os.getenv("AUTO_POST_MINUTES", "0"))        # 0 = off; else publish 1 pending post every N min
FIREBASE_CRED = os.getenv("FIREBASE_CRED", "serviceAccount.json")
UNLOCK_HOURS = int(os.getenv("UNLOCK_HOURS", "24"))        # how long a watched-ad unlock lasts
MIN_AD_SECONDS = int(os.getenv("MIN_AD_SECONDS", "8"))     # minimum time between ad start and unlock

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# ───────────────────────── database ─────────────────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
create table if not exists posts(
  id integer primary key autoincrement, title text, video_file_id text, video_url text, thumb_file_id text,
  category text default 'General', premium int default 0, status text default 'pending',
  views int default 0, created int, channel_msg_id int, broken int default 0);
create table if not exists categories(id integer primary key autoincrement, name text unique);
create table if not exists users(id integer primary key, name text, ref_by int, refs int default 0, joined int);
create table if not exists settings(k text primary key, v text);
create table if not exists unlocks(user_id int, post_id int, expires int, primary key(user_id,post_id));
create table if not exists join_buttons(id integer primary key autoincrement, label text, url text);
""")
db.commit()


def q(sql, a=(), one=False):
    c = db.execute(sql, a)
    return c.fetchone() if one else c.fetchall()


def x(sql, a=()):
    c = db.execute(sql, a)
    db.commit()
    return c.lastrowid


def gs(k, d=""):
    r = q("select v from settings where k=?", (k,), True)
    return r["v"] if r else d


def ss(k, v):
    x("insert into settings(k,v) values(?,?) on conflict(k) do update set v=excluded.v", (k, str(v)))


def gj(k, d):
    try:
        return json.loads(gs(k, "")) 
    except Exception:
        return d


def sj(k, v):
    ss(k, json.dumps(v, ensure_ascii=False))


# ───────────────────────── helpers ─────────────────────────
async def is_member(uid: int) -> bool:
    ch = gs("join_channel")
    if not ch:
        return True
    try:
        m = await bot.get_chat_member(int(ch), uid)
        return m.status in ("member", "administrator", "creator", "restricted")
    except Exception:
        return True  # if the bot cannot check, do not block users


def check_init(init: str):
    """Validate Telegram Mini App initData, return user dict or None."""
    try:
        d = dict(parse_qsl(init, strict_parsing=True))
        h = d.pop("hash")
        s = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(key, s.encode(), hashlib.sha256).hexdigest(), h):
            return None
        if time.time() - int(d["auth_date"]) > 86400:
            return None
        return json.loads(d["user"])
    except Exception:
        return None


def is_unlocked(uid: int, pid: int) -> bool:
    return bool(q("select 1 from unlocks where user_id=? and post_id=? and expires>?", (uid, pid, int(time.time())), True))


async def can_watch(init: str, pid: int) -> bool:
    u = check_init(init)
    return bool(u) and (is_unlocked(u["id"], pid) or await is_member(u["id"]))


PENDING_UNLOCK = {}  # nonce -> (uid, pid, started_at)


async def publish_post(pid: int) -> bool:
    """Mark published, post to channel (if set). Returns True if posted to channel."""
    p = q("select * from posts where id=?", (pid,), True)
    if not p:
        return False
    x("update posts set status='published' where id=?", (pid,))
    ch = gs("post_channel")
    if not ch:
        return False
    me = await bot.get_me()
    kb = InlineKeyboardBuilder()
    kb.button(text="🔓 Unlock Video" if p["premium"] else "▶️ Watch in App", url=f"https://t.me/{me.username}?start=p{pid}")
    for b in q("select * from join_buttons"):
        kb.button(text=b["label"], url=b["url"])
    kb.adjust(1)
    tag = "#" + p["category"].replace(" ", "_")
    cap = f"{'💎' if p['premium'] else '🔥'} <b>{escape(p['title'])}</b>\n\n{escape(tag)}"
    try:
        if not p["premium"] and p["video_file_id"]:
            msg = await bot.send_video(int(ch), p["video_file_id"], caption=cap, reply_markup=kb.as_markup())
        elif p["thumb_file_id"]:
            msg = await bot.send_photo(int(ch), p["thumb_file_id"], caption=cap, reply_markup=kb.as_markup())
        else:
            msg = await bot.send_message(int(ch), cap, reply_markup=kb.as_markup())
    except Exception as e:
        logging.warning("channel post failed: %s", e)
        return False
    x("update posts set channel_msg_id=? where id=?", (msg.message_id, pid))
    asyncio.create_task(react(int(ch), msg.message_id))
    return True


async def react(ch: int, mid: int):
    emojis = gj("react_emojis", ["🔥", "❤", "😍", "👍", "🤩"])
    async with aiohttp.ClientSession() as s:
        for t in gj("react_tokens", []):
            try:
                await s.post(f"https://api.telegram.org/bot{t}/setMessageReaction",
                             json={"chat_id": ch, "message_id": mid,
                                   "reaction": [{"type": "emoji", "emoji": random.choice(emojis)}]})
            except Exception:
                pass
            await asyncio.sleep(random.uniform(0.5, 2))


async def check_broken():
    bad = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        for p in q("select * from posts where status='published'"):
            ok = True
            if p["video_url"]:
                try:
                    async with s.get(p["video_url"], headers={"Range": "bytes=0-0"}) as r:
                        ok = r.status < 400
                except Exception:
                    ok = False
            elif p["video_file_id"]:
                try:
                    await bot.get_file(p["video_file_id"])
                except TelegramBadRequest as e:
                    ok = "too big" in str(e).lower()
                except Exception:
                    ok = False
            x("update posts set broken=? where id=?", (0 if ok else 1, p["id"]))
            if not ok:
                bad.append(p)
    return bad


async def auto_loop():
    while AUTO_MINUTES > 0:
        await asyncio.sleep(AUTO_MINUTES * 60)
        p = q("select id from posts where status='pending' order by id limit 1", (), True)
        if p:
            await publish_post(p["id"])


# ───────────────────────── Firebase (optional) ─────────────────────────
def fb():
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
    except ImportError:
        return None
    if not os.path.exists(FIREBASE_CRED):
        return None
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(FIREBASE_CRED))
    return firestore.client()


def backup_dict():
    return {"posts": [dict(r) for r in q("select * from posts")],
            "categories": [r["name"] for r in q("select name from categories")]}


def restore_dict(d):
    n = 0
    for c in d.get("categories", []):
        x("insert or ignore into categories(name) values(?)", (c,))
    for p in d.get("posts", []):
        cols = ["id", "title", "video_file_id", "video_url", "thumb_file_id", "category", "premium",
                "status", "views", "created", "channel_msg_id", "broken"]
        x(f"insert or replace into posts({','.join(cols)}) values({','.join('?' * len(cols))})",
          [p.get(c) for c in cols])
        n += 1
    return n


def fb_export(client):
    d = backup_dict()
    for p in d["posts"]:
        client.collection("posts").document(str(p["id"])).set(p)
    client.collection("meta").document("categories").set({"names": d["categories"]})
    return len(d["posts"])


def fb_import(client):
    posts = [doc.to_dict() for doc in client.collection("posts").stream()]
    cats = client.collection("meta").document("categories").get()
    return restore_dict({"posts": posts, "categories": (cats.to_dict() or {}).get("names", []) if cats.exists else []})


# ───────────────────────── bot: user side ─────────────────────────
user = Router()

B_UP, B_FT = "🚀 Upload New Post", "📝 Fixed Title"
B_CAT, B_DEL = "🗂 Manage Categories", "🗑 Delete Posts"
B_AD, B_POLL = "⚙️ Ad Settings", "📊 Create Poll"
B_JB, B_RB = "🔗 Manage Join Buttons", "🤖 Manage React Bots"
B_FI, B_FE = "📥 Firebase Import", "📤 Firebase Export"
B_PP, B_BK = "📡 Publish Pending", "⚠️ Broken Link Videos"
B_CH, B_BC = "📢 Manage Channels", "💬 All Member Message"
ADMIN_ROWS = [(B_UP, B_FT), (B_CAT, B_DEL), (B_AD, B_POLL), (B_JB, B_RB), (B_FI, B_FE), (B_PP, B_BK), (B_CH, B_BC)]


def admin_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in r] for r in ADMIN_ROWS],
                               resize_keyboard=True)


def user_kb():
    rows = []
    if WEBAPP_URL:
        rows.append([KeyboardButton(text="🎬 Viral Zone", web_app=WebAppInfo(url=WEBAPP_URL))])
    rows.append([KeyboardButton(text="👥 Referral"), KeyboardButton(text="ℹ️ Help")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


@user.message(CommandStart())
async def start(m: Message, command: CommandObject):
    uid = m.from_user.id
    arg = command.args or ""
    if not q("select 1 from users where id=?", (uid,), True):
        ref = int(arg[4:]) if arg.startswith("ref_") and arg[4:].isdigit() and int(arg[4:]) != uid else None
        x("insert into users(id,name,ref_by,joined) values(?,?,?,?)", (uid, m.from_user.full_name, ref, int(time.time())))
        if ref:
            x("update users set refs=refs+1 where id=?", (ref,))
    if not await is_member(uid):
        kb = InlineKeyboardBuilder()
        kb.button(text="📢 Join Channel", url=gs("join_link") or "https://t.me")
        kb.button(text="✅ আমি Join করেছি", callback_data="chk")
        kb.adjust(1)
        return await m.answer("🔒 কন্টেন্ট দেখতে আগে আমাদের চ্যানেলে Join করুন।", reply_markup=kb.as_markup())
    if arg.startswith("p") and arg[1:].isdigit() and WEBAPP_URL:
        ib = InlineKeyboardBuilder()
        ib.button(text="🔓 Unlock & Watch", web_app=WebAppInfo(url=f"{WEBAPP_URL}/?p={int(arg[1:])}"))
        return await m.answer("🎬 ভিডিওটি দেখতে নিচের বাটনে ট্যাপ করুন", reply_markup=ib.as_markup())
    kb = admin_kb() if uid in ADMINS else user_kb()
    await m.answer(f"👋 স্বাগতম <b>{escape(m.from_user.first_name)}</b>!\n🔥 Viral Zone-এ নতুন ভিডিও প্রতিদিন।", reply_markup=kb)
    if WEBAPP_URL:
        ib = InlineKeyboardBuilder()
        ib.button(text="🎬 Open Viral Zone", web_app=WebAppInfo(url=WEBAPP_URL))
        await m.answer("👇 ট্যাপ করে অ্যাপ খুলুন", reply_markup=ib.as_markup())


@user.callback_query(F.data == "chk")
async def chk(c: CallbackQuery):
    if await is_member(c.from_user.id):
        await c.message.delete()
        await c.message.answer("✅ ধন্যবাদ! এখন /start দিন।")
    else:
        await c.answer("আগে চ্যানেলে Join করুন", show_alert=True)


@user.message(F.text == "👥 Referral")
async def referral(m: Message):
    me = await bot.get_me()
    r = q("select refs from users where id=?", (m.from_user.id,), True)
    await m.answer(f"👥 আপনার রেফারেল: <b>{r['refs'] if r else 0}</b>\n\n🔗 <code>https://t.me/{me.username}?start=ref_{m.from_user.id}</code>")


@user.message(F.text == "ℹ️ Help")
async def help_(m: Message):
    await m.answer("🎬 <b>Viral Zone</b> বাটনে ট্যাপ করে সব ভিডিও দেখুন।\n💎 Premium কন্টেন্টের জন্য চ্যানেলে Join থাকতে হবে।")


# ───────────────────────── bot: admin side ─────────────────────────
admin = Router()
admin.message.filter(F.from_user.id.in_(ADMINS))
admin.callback_query.filter(F.from_user.id.in_(ADMINS))


class U(StatesGroup):
    video = State(); thumb = State(); title = State(); cat = State()


class Ch(StatesGroup):
    wait = State()


class Ed(StatesGroup):
    val = State()


@admin.message(Command("admin"))
async def admin_cmd(m: Message):
    await m.answer("🛠 Admin Panel", reply_markup=admin_kb())


@admin.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("❌ বাতিল করা হয়েছে", reply_markup=admin_kb())


# ---- upload ----
@admin.message(F.text == B_UP)
async def up_start(m: Message, state: FSMContext):
    await state.clear(); await state.set_state(U.video)
    await m.answer("🎥 ভিডিও ফাইল পাঠান, অথবা সরাসরি ভিডিও লিংক (https://...mp4) পেস্ট করুন।\n/cancel")


@admin.message(U.video)
async def up_video(m: Message, state: FSMContext):
    if m.video or (m.document and (m.document.mime_type or "").startswith("video")):
        v = m.video or m.document
        await state.update_data(video_file_id=v.file_id, thumb_file_id=v.thumbnail.file_id if v.thumbnail else None)
        return await after_media(m, state)
    if m.text and m.text.startswith("http"):
        await state.update_data(video_url=m.text.strip())
        await state.set_state(U.thumb)
        return await m.answer("🖼 থাম্বনেইল ছবি পাঠান (বা /skip)")
    await m.answer("⚠️ ভিডিও ফাইল বা http লিংক পাঠান।")


@admin.message(U.thumb)
async def up_thumb(m: Message, state: FSMContext):
    if m.photo:
        await state.update_data(thumb_file_id=m.photo[-1].file_id)
    elif not (m.text and m.text.startswith("/skip")):
        return await m.answer("ছবি পাঠান বা /skip")
    await after_media(m, state)


async def after_media(m: Message, state: FSMContext):
    ft = gs("fixed_title")
    if ft:
        await state.update_data(title=ft)
        return await ask_cat(m, state)
    await state.set_state(U.title)
    await m.answer("📝 টাইটেল লিখুন")


@admin.message(U.title, ~F.text.in_({t for r in ADMIN_ROWS for t in r}))
async def up_title(m: Message, state: FSMContext):
    if not m.text:
        return
    await state.update_data(title=m.text.strip())
    await ask_cat(m, state)


async def ask_cat(m: Message, state: FSMContext):
    await state.set_state(U.cat)
    cats = q("select * from categories order by name")
    if not cats:
        return await ask_pub(m)
    kb = InlineKeyboardBuilder()
    for c in cats:
        kb.button(text=c["name"], callback_data=f"pick:{c['id']}")
    kb.adjust(2)
    await m.answer("🗂 ক্যাটাগরি বাছাই করুন", reply_markup=kb.as_markup())


async def ask_pub(m: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🆓 Free • Publish", callback_data="pub:free")
    kb.button(text="💎 Premium • Publish", callback_data="pub:prem")
    kb.button(text="🕓 Pending-এ রাখুন", callback_data="pub:pend")
    kb.adjust(1)
    await m.answer("কীভাবে সেভ করবেন?", reply_markup=kb.as_markup())


@admin.callback_query(F.data.startswith("pick:"))
async def pick(c: CallbackQuery, state: FSMContext):
    r = q("select name from categories where id=?", (int(c.data[5:]),), True)
    await state.update_data(category=r["name"] if r else "General")
    await c.message.delete()
    await ask_pub(c.message)


@admin.callback_query(F.data.startswith("pub:"))
async def pub(c: CallbackQuery, state: FSMContext):
    d = await state.get_data(); mode = c.data[4:]
    if not d.get("title"):
        return await c.answer("সেশন শেষ, আবার শুরু করুন", show_alert=True)
    pid = x("insert into posts(title,video_file_id,video_url,thumb_file_id,category,premium,status,created) values(?,?,?,?,?,?,?,?)",
            (d["title"], d.get("video_file_id"), d.get("video_url"), d.get("thumb_file_id"),
             d.get("category", "General"), 1 if mode == "prem" else 0, "pending", int(time.time())))
    await state.clear()
    if mode == "pend":
        return await c.message.edit_text(f"🕓 Pending-এ সেভ হয়েছে (#{pid})")
    ok = await publish_post(pid)
    await c.message.edit_text(f"✅ পাবলিশ হয়েছে (#{pid})" + ("" if ok else "\n⚠️ Post Channel সেট নেই — শুধু অ্যাপে দেখাবে।"))


# ---- fixed title / publish pending / broken ----
@admin.message(F.text == B_FT)
async def ft(m: Message, state: FSMContext):
    await state.clear(); await state.update_data(act="fixed"); await state.set_state(Ed.val)
    await m.answer(f"📝 বর্তমান Fixed Title: <b>{escape(gs('fixed_title') or 'নেই')}</b>\n\nনতুন টাইটেল পাঠান। বন্ধ করতে <code>-</code> পাঠান।")


@admin.message(F.text == B_PP)
async def pp(m: Message, state: FSMContext):
    await state.clear()
    rows = q("select id from posts where status='pending' order by id")
    if not rows:
        return await m.answer("📭 কোনো Pending পোস্ট নেই।")
    await m.answer(f"📡 {len(rows)}টি পোস্ট পাবলিশ হচ্ছে…")
    for r in rows:
        await publish_post(r["id"]); await asyncio.sleep(1.5)
    await m.answer("✅ সব Pending পাবলিশ হয়েছে।")


@admin.message(F.text == B_BK)
async def bk(m: Message, state: FSMContext):
    await state.clear()
    wait = await m.answer("🔎 চেক করা হচ্ছে…")
    bad = await check_broken()
    if not bad:
        return await wait.edit_text("✅ কোনো Broken ভিডিও নেই।")
    kb = InlineKeyboardBuilder(); kb.button(text="🗑 সব Broken ডিলিট", callback_data="bk:del"); 
    txt = "\n".join(f"#{p['id']} {escape(p['title'])}" for p in bad[:40])
    await wait.edit_text(f"⚠️ {len(bad)}টি Broken:\n\n{txt}", reply_markup=kb.as_markup())


@admin.callback_query(F.data == "bk:del")
async def bk_del(c: CallbackQuery):
    for p in q("select * from posts where broken=1"):
        await delete_post(p["id"])
    await c.message.edit_text("🗑 Broken পোস্টগুলো ডিলিট হয়েছে।")


async def delete_post(pid: int):
    p = q("select * from posts where id=?", (pid,), True)
    if p and p["channel_msg_id"] and gs("post_channel"):
        try:
            await bot.delete_message(int(gs("post_channel")), p["channel_msg_id"])
        except Exception:
            pass
    x("delete from posts where id=?", (pid,))


# ---- delete posts ----
@admin.message(F.text == B_DEL)
async def dl(m: Message, state: FSMContext):
    await state.clear()
    rows = q("select id,title,premium from posts order by id desc limit 15")
    if not rows:
        return await m.answer("📭 কোনো পোস্ট নেই।")
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"🗑 #{r['id']} {'💎' if r['premium'] else ''}{r['title'][:28]}", callback_data=f"del:{r['id']}")
    kb.adjust(1)
    await m.answer("কোন পোস্ট ডিলিট করবেন? (সর্বশেষ ১৫টি)", reply_markup=kb.as_markup())


@admin.callback_query(F.data.startswith("del:"))
async def dl_do(c: CallbackQuery):
    await delete_post(int(c.data[4:]))
    await c.answer("ডিলিট হয়েছে")
    await c.message.delete()


# ---- categories ----
async def cat_view(target: Message, edit=False):
    kb = InlineKeyboardBuilder()
    for r in q("select * from categories order by name"):
        kb.button(text=f"🗑 {r['name']}", callback_data=f"cat:del:{r['id']}")
    kb.button(text="➕ নতুন ক্যাটাগরি", callback_data="cat:add")
    kb.adjust(1)
    f = target.edit_text if edit else target.answer
    await f("🗂 <b>ক্যাটাগরি</b> (ডিলিট করতে ট্যাপ করুন)", reply_markup=kb.as_markup())


@admin.message(F.text == B_CAT)
async def cat(m: Message, state: FSMContext):
    await state.clear(); await cat_view(m)


@admin.callback_query(F.data == "cat:add")
async def cat_add(c: CallbackQuery, state: FSMContext):
    await state.update_data(act="cat_add"); await state.set_state(Ed.val)
    await c.message.answer("➕ ক্যাটাগরির নাম লিখুন")
    await c.answer()


@admin.callback_query(F.data.startswith("cat:del:"))
async def cat_del(c: CallbackQuery):
    x("delete from categories where id=?", (int(c.data[8:]),))
    await cat_view(c.message, edit=True)


# ---- join buttons ----
async def jb_view(target: Message, edit=False):
    kb = InlineKeyboardBuilder()
    for r in q("select * from join_buttons"):
        kb.button(text=f"🗑 {r['label']}", callback_data=f"jb:del:{r['id']}")
    kb.button(text="➕ নতুন বাটন", callback_data="jb:add")
    kb.adjust(1)
    f = target.edit_text if edit else target.answer
    await f("🔗 <b>Join Buttons</b> — প্রতিটি চ্যানেল পোস্টের নিচে দেখাবে।", reply_markup=kb.as_markup())


@admin.message(F.text == B_JB)
async def jb(m: Message, state: FSMContext):
    await state.clear(); await jb_view(m)


@admin.callback_query(F.data == "jb:add")
async def jb_add(c: CallbackQuery, state: FSMContext):
    await state.update_data(act="jb_add"); await state.set_state(Ed.val)
    await c.message.answer("ফরম্যাট: <code>বাটনের নাম | https://t.me/yourchannel</code>")
    await c.answer()


@admin.callback_query(F.data.startswith("jb:del:"))
async def jb_del(c: CallbackQuery):
    x("delete from join_buttons where id=?", (int(c.data[7:]),))
    await jb_view(c.message, edit=True)


# ---- react bots ----
async def rb_view(target: Message):
    n = len(gj("react_tokens", []))
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ React Bot যোগ", callback_data="rb:add")
    kb.button(text="😀 Emoji বদলান", callback_data="rb:emo")
    kb.button(text="🗑 সব React Bot মুছুন", callback_data="rb:clear")
    kb.adjust(1)
    await target.answer(f"🤖 <b>React Bots</b>: {n}টি\nEmoji: {' '.join(gj('react_emojis', ['🔥', '❤', '😍', '👍', '🤩']))}\n\n"
                        "প্রতিটি বট (চ্যানেলে অ্যাডমিন) নতুন পোস্টে র‍্যান্ডম রিঅ্যাকশন দেবে।", reply_markup=kb.as_markup())


@admin.message(F.text == B_RB)
async def rb(m: Message, state: FSMContext):
    await state.clear(); await rb_view(m)


@admin.callback_query(F.data.in_({"rb:add", "rb:emo"}))
async def rb_edit(c: CallbackQuery, state: FSMContext):
    act = "rb_add" if c.data == "rb:add" else "rb_emo"
    await state.update_data(act=act); await state.set_state(Ed.val)
    await c.message.answer("🔑 React Bot-এর টোকেন পাঠান" if act == "rb_add" else "Emoji গুলো স্পেস দিয়ে পাঠান (যেমন: 🔥 ❤ 👍)")
    await c.answer()


@admin.callback_query(F.data == "rb:clear")
async def rb_clear(c: CallbackQuery):
    sj("react_tokens", [])
    await c.answer("মুছে ফেলা হয়েছে")


# ---- ad settings ----
async def ad_view(target: Message, edit=False):
    on = gs("ads_on") == "1"
    kb = InlineKeyboardBuilder()
    kb.button(text=f"{'🟢 Ads ON' if on else '🔴 Ads OFF'} (ট্যাপ করে বদলান)", callback_data="ad:toggle")
    kb.button(text=f"Adsgram Block ID: {gs('adsgram') or '—'}", callback_data="ad:set:adsgram")
    kb.button(text=f"Monetag Zone ID: {gs('monetag') or '—'}", callback_data="ad:set:monetag")
    kb.button(text=f"Adsterra Direct Link: {'✅' if gs('adsterra') else '—'}", callback_data="ad:set:adsterra")
    kb.adjust(1)
    f = target.edit_text if edit else target.answer
    await f("⚙️ <b>Ad Settings</b>\nভিডিও প্লে করার আগে অ্যাপে বিজ্ঞাপন দেখাবে (Adsgram → Monetag → Adsterra অগ্রাধিকার)।", reply_markup=kb.as_markup())


@admin.message(F.text == B_AD)
async def ad(m: Message, state: FSMContext):
    await state.clear(); await ad_view(m)


@admin.callback_query(F.data == "ad:toggle")
async def ad_toggle(c: CallbackQuery):
    ss("ads_on", "0" if gs("ads_on") == "1" else "1")
    await ad_view(c.message, edit=True)


@admin.callback_query(F.data.startswith("ad:set:"))
async def ad_set(c: CallbackQuery, state: FSMContext):
    key = c.data[7:]
    await state.update_data(act=f"ad_{key}"); await state.set_state(Ed.val)
    await c.message.answer(f"{key} এর মান পাঠান। মুছতে <code>-</code> পাঠান।")
    await c.answer()


# ---- poll / broadcast ----
@admin.message(F.text == B_POLL)
async def poll(m: Message, state: FSMContext):
    await state.clear(); await state.update_data(act="poll_q"); await state.set_state(Ed.val)
    await m.answer("📊 পোলের প্রশ্ন লিখুন")


@admin.message(F.text == B_BC)
async def bc(m: Message, state: FSMContext):
    await state.clear(); await state.update_data(act="bc"); await state.set_state(Ed.val)
    await m.answer("💬 যে মেসেজ (টেক্সট/ছবি/ভিডিও) সবাইকে পাঠাবেন সেটা এখন পাঠান। /cancel")


# ---- channels ----
async def ch_text():
    def line(k, t):
        return f"{t}: <b>{escape(gs(k + '_title') or 'নেই')}</b>" + (f"\n<code>{gs(k)}</code>" if gs(k) else "")
    return "📢 <b>চ্যানেল সেটিংস</b>\n\n" + line("join_channel", "📣 App Join") + "\n\n" + line("post_channel", "📡 Post Channel")


@admin.message(F.text == B_CH)
async def chn(m: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="📣 App Join Channel-এ যোগ করুন", callback_data="ch:join_channel")
    kb.button(text="📡 Post Channel-এ যোগ করুন", callback_data="ch:post_channel")
    kb.button(text="❌ কিছু করবেন না", callback_data="ch:no")
    kb.adjust(1)
    await m.answer(await ch_text(), reply_markup=kb.as_markup())


@admin.callback_query(F.data.startswith("ch:"))
async def ch_pick(c: CallbackQuery, state: FSMContext):
    k = c.data[3:]
    if k == "no":
        return await c.message.delete()
    await state.update_data(which=k); await state.set_state(Ch.wait)
    await c.message.answer("চ্যানেল থেকে যেকোনো পোস্ট ফরওয়ার্ড করুন, অথবা @username / -100… আইডি পাঠান।\n⚠️ বটকে আগে চ্যানেলের Admin বানান।")
    await c.answer()


@admin.message(Ch.wait)
async def ch_set(m: Message, state: FSMContext):
    d = await state.get_data(); k = d["which"]
    ref = getattr(getattr(m, "forward_origin", None), "chat", None)
    target = ref.id if ref else (m.text or "").strip()
    try:
        chat = await bot.get_chat(int(target) if str(target).lstrip("-").isdigit() else target)
        me = await bot.get_me()
        st = await bot.get_chat_member(chat.id, me.id)
        if st.status not in ("administrator", "creator"):
            return await m.answer("⚠️ বট এই চ্যানেলের Admin নয়।")
    except Exception as e:
        return await m.answer(f"⚠️ চ্যানেল পাওয়া যায়নি: {escape(str(e))}")
    ss(k, chat.id); ss(k + "_title", chat.title or str(chat.id))
    if k == "join_channel":
        link = f"https://t.me/{chat.username}" if chat.username else None
        if not link:
            try:
                link = await bot.export_chat_invite_link(chat.id)
            except Exception:
                link = chat.invite_link or ""
        ss("join_link", link)
    await state.clear()
    await m.answer("✅ যোগ হয়েছে!\n\n" + await ch_text(), reply_markup=admin_kb())


# ---- firebase ----
@admin.message(F.text == B_FE)
async def fe(m: Message, state: FSMContext):
    await state.clear()
    data = json.dumps(backup_dict(), ensure_ascii=False, indent=1).encode()
    await m.answer_document(BufferedInputFile(data, "backup.json"), caption="📤 JSON ব্যাকআপ")
    client = await asyncio.to_thread(fb)
    if client:
        n = await asyncio.to_thread(fb_export, client)
        await m.answer(f"☁️ Firebase-এ {n}টি পোস্ট এক্সপোর্ট হয়েছে।")
    else:
        await m.answer("ℹ️ Firebase কনফিগ নেই (serviceAccount.json) — শুধু JSON ব্যাকআপ দেওয়া হলো।")


@admin.message(F.text == B_FI)
async def fi(m: Message, state: FSMContext):
    await state.clear()
    client = await asyncio.to_thread(fb)
    if client:
        n = await asyncio.to_thread(fb_import, client)
        return await m.answer(f"☁️ Firebase থেকে {n}টি পোস্ট ইমপোর্ট হয়েছে।")
    await state.update_data(act="import"); await state.set_state(Ed.val)
    await m.answer("📥 backup.json ফাইলটি পাঠান।")


# ---- generic value handler (must stay last) ----
@admin.message(Ed.val)
async def on_value(m: Message, state: FSMContext):
    act = (await state.get_data()).get("act")
    t = (m.text or "").strip()
    if act == "bc":
        await state.clear()
        ok = bad = 0
        for u in q("select id from users"):
            try:
                await m.copy_to(u["id"]); ok += 1
            except Exception:
                bad += 1
            await asyncio.sleep(0.05)
        return await m.answer(f"💬 পাঠানো: {ok} | ব্যর্থ: {bad}")
    if act == "import":
        if not m.document:
            return await m.answer("JSON ফাইল পাঠান।")
        f = await bot.download(m.document)
        n = restore_dict(json.loads(f.read().decode()))
        await state.clear()
        return await m.answer(f"📥 {n}টি পোস্ট ইমপোর্ট হয়েছে।")
    if not t:
        return await m.answer("টেক্সট পাঠান।")
    if act == "fixed":
        ss("fixed_title", "" if t == "-" else t)
        await state.clear(); return await m.answer("✅ সেভ হয়েছে")
    if act == "cat_add":
        x("insert or ignore into categories(name) values(?)", (t,))
        await state.clear(); return await cat_view(m)
    if act == "jb_add":
        if "|" not in t:
            return await m.answer("ফরম্যাট: নাম | লিংক")
        label, url = [s.strip() for s in t.split("|", 1)]
        x("insert into join_buttons(label,url) values(?,?)", (label, url))
        await state.clear(); return await jb_view(m)
    if act and act.startswith("ad_"):
        ss(act[3:], "" if t == "-" else t)
        await state.clear(); return await ad_view(m)
    if act == "rb_add":
        try:
            async with aiohttp.ClientSession() as s, s.get(f"https://api.telegram.org/bot{t}/getMe") as r:
                ok = (await r.json()).get("ok")
        except Exception:
            ok = False
        try:
            await m.delete()
        except Exception:
            pass
        if not ok:
            return await m.answer("⚠️ টোকেন ঠিক নয়।")
        toks = gj("react_tokens", []); toks.append(t); sj("react_tokens", toks)
        await state.clear(); return await m.answer("✅ React Bot যোগ হয়েছে।")
    if act == "rb_emo":
        sj("react_emojis", t.split())
        await state.clear(); return await m.answer("✅ Emoji সেভ হয়েছে।")
    if act == "poll_q":
        await state.update_data(act="poll_o", q=t)
        return await m.answer("অপশনগুলো প্রতি লাইনে একটি করে লিখুন (২–১০টি)")
    if act == "poll_o":
        d = await state.get_data()
        opts = [o.strip() for o in t.split("\n") if o.strip()][:10]
        if len(opts) < 2 or not gs("post_channel"):
            return await m.answer("⚠️ কমপক্ষে ২টি অপশন লাগবে এবং Post Channel সেট থাকতে হবে।")
        await bot.send_poll(int(gs("post_channel")), d["q"], opts, is_anonymous=True)
        await state.clear(); return await m.answer("✅ পোল পাঠানো হয়েছে।")


# ───────────────────────── Mini App web server ─────────────────────────
BASE = Path(__file__).parent


async def idx(_):
    return web.FileResponse(BASE / "index.html")


async def api_config(_):
    return web.json_response({
        "join_link": gs("join_link"),
        "ads": {"on": gs("ads_on") == "1", "adsgram": gs("adsgram"), "monetag": gs("monetag"), "adsterra": gs("adsterra")},
    })


async def api_categories(_):
    rows = q("select category as name, count(*) as n from posts where status='published' and broken=0 group by category order by name")
    total = sum(r["n"] for r in rows)
    return web.json_response({"total": total, "items": [dict(r) for r in rows]})


async def api_posts(req):
    cat = req.query.get("cat", "")
    page = max(int(req.query.get("page", 0)), 0); size = 12
    sql = ("select id,title,category,premium,views,created,(thumb_file_id is not null) as th "
           "from posts where status='published' and broken=0")
    a = []
    if cat:
        sql += " and category=?"; a.append(cat)
    sql += " order by id desc limit ? offset ?"; a += [size + 1, page * size]
    rows = q(sql, a)
    return web.json_response({"items": [dict(r) for r in rows[:size]], "more": len(rows) > size})


async def api_play(req):
    pid = int(req.match_info["pid"])
    init = req.query.get("init", "")
    p = q("select * from posts where id=? and status='published'", (pid,), True)
    if not p:
        raise web.HTTPNotFound()
    if p["premium"] and not await can_watch(init, pid):
        return web.json_response({"locked": True, "join_link": gs("join_link"), "title": p["title"],
                                  "hours": UNLOCK_HOURS})
    x("update posts set views=views+1 where id=?", (pid,))
    url = p["video_url"] or f"/media/video/{pid}?init={quote(init)}"
    return web.json_response({"url": url, "title": p["title"]})


async def api_unlock_start(req):
    u = check_init(req.query.get("init", ""))
    pid = int(req.match_info["pid"])
    if not u or not q("select 1 from posts where id=? and premium=1", (pid,), True):
        raise web.HTTPForbidden()
    nonce = hashlib.sha1(os.urandom(16)).hexdigest()
    now = time.time()
    for k in [k for k, v in PENDING_UNLOCK.items() if now - v[2] > 900]:
        PENDING_UNLOCK.pop(k, None)
    PENDING_UNLOCK[nonce] = (u["id"], pid, now)
    return web.json_response({"nonce": nonce})


async def api_unlock_done(req):
    u = check_init(req.query.get("init", ""))
    rec = PENDING_UNLOCK.pop(req.query.get("nonce", ""), None)
    if not u or not rec or rec[0] != u["id"] or rec[1] != int(req.match_info["pid"]):
        raise web.HTTPForbidden()
    if gs("ads_on") == "1" and time.time() - rec[2] < MIN_AD_SECONDS:
        return web.json_response({"ok": False, "reason": "too_fast"})
    x("insert or replace into unlocks(user_id,post_id,expires) values(?,?,?)",
      (u["id"], rec[1], int(time.time()) + UNLOCK_HOURS * 3600))
    return web.json_response({"ok": True})


async def media(req):
    kind, pid = req.match_info["kind"], int(req.match_info["pid"])
    p = q("select * from posts where id=?", (pid,), True)
    if not p:
        raise web.HTTPNotFound()
    if kind == "video" and p["premium"] and not await can_watch(req.query.get("init", ""), pid):
        raise web.HTTPForbidden()
    fid = p["thumb_file_id"] if kind == "thumb" else p["video_file_id"]
    if not fid:
        raise web.HTTPNotFound()
    try:
        f = await bot.get_file(fid)
    except Exception:
        if kind == "video":
            x("update posts set broken=1 where id=?", (pid,))
        raise web.HTTPNotFound()
    hdr = {"Range": req.headers["Range"]} if "Range" in req.headers else {}
    async with aiohttp.ClientSession() as s, s.get(f"https://api.telegram.org/file/bot{TOKEN}/{f.file_path}", headers=hdr) as r:
        resp = web.StreamResponse(status=r.status)
        for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
            if h in r.headers:
                resp.headers[h] = r.headers[h]
        resp.headers["Cache-Control"] = "private, max-age=3600"
        await resp.prepare(req)
        async for chunk in r.content.iter_chunked(65536):
            await resp.write(chunk)
        return resp


async def main():
    app = web.Application()
    app.add_routes([web.get("/", idx), web.get("/api/config", api_config), web.get("/api/categories", api_categories),
                    web.get("/api/posts", api_posts), web.get("/api/play/{pid}", api_play),
                    web.post("/api/unlock/{pid}/start", api_unlock_start), web.post("/api/unlock/{pid}/done", api_unlock_done),
                    web.get("/media/{kind:(video|thumb)}/{pid}", media)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logging.info("Mini App server on :%s", PORT)
    if WEBAPP_URL:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text=MENU_TEXT, web_app=WebAppInfo(url=WEBAPP_URL)))
    dp.include_router(admin)   # admin first so menu buttons win
    dp.include_router(user)
    asyncio.create_task(auto_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
