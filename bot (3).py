"""
LumenЧат — бот-модератор для Telegram-чатов (аналог Iris, но с упором на удобство).
Стек: aiogram 3 + aiosqlite
"""
import asyncio
import html
import logging
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.filters import JOIN_TRANSITION, ChatMemberUpdatedFilter, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

TOKEN = os.getenv("BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")
DB_PATH = "lumenchat.db"

logging.basicConfig(level=logging.INFO)
router = Router()
db: aiosqlite.Connection
BOT_USERNAME = ""

FULL_PERMS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_invite_users=True,
)
MUTE_PERMS = ChatPermissions(can_send_messages=False)

LINK_RE = re.compile(r"(https?://|t\.me/|telegram\.me/|www\.|@\w{4,})", re.I)
flood_log: dict = defaultdict(lambda: deque(maxlen=10))
pending_captcha: dict = {}

# ───────────────────────── БД ─────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats(
    chat_id INTEGER PRIMARY KEY,
    welcome TEXT DEFAULT 'Привет, {name}! Добро пожаловать в <b>{chat}</b> 💜',
    rules TEXT DEFAULT '',
    antilink INTEGER DEFAULT 0,
    antiflood INTEGER DEFAULT 1,
    captcha INTEGER DEFAULT 1,
    maxwarns INTEGER DEFAULT 3
);
CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, username TEXT, name TEXT);
CREATE TABLE IF NOT EXISTS mods(chat_id INTEGER, user_id INTEGER, PRIMARY KEY(chat_id, user_id));
CREATE TABLE IF NOT EXISTS warns(chat_id INTEGER, user_id INTEGER, count INTEGER DEFAULT 0, PRIMARY KEY(chat_id, user_id));
CREATE TABLE IF NOT EXISTS words(chat_id INTEGER, word TEXT, PRIMARY KEY(chat_id, word));
CREATE TABLE IF NOT EXISTS stats(chat_id INTEGER, user_id INTEGER, msgs INTEGER DEFAULT 0, PRIMARY KEY(chat_id, user_id));
"""


async def init_db():
    global db
    db = await aiosqlite.connect(DB_PATH)
    await db.executescript(SCHEMA)
    await db.commit()


async def chat_cfg(chat_id: int) -> dict:
    await db.execute("INSERT OR IGNORE INTO chats(chat_id) VALUES(?)", (chat_id,))
    await db.commit()
    cur = await db.execute(
        "SELECT welcome, rules, antilink, antiflood, captcha, maxwarns FROM chats WHERE chat_id=?",
        (chat_id,),
    )
    r = await cur.fetchone()
    return dict(zip(["welcome", "rules", "antilink", "antiflood", "captcha", "maxwarns"], r))


async def set_cfg(chat_id: int, key: str, value):
    await chat_cfg(chat_id)
    await db.execute(f"UPDATE chats SET {key}=? WHERE chat_id=?", (value, chat_id))
    await db.commit()


# ───────────────────────── Утилиты ─────────────────────────

def mention(uid: int, name: str) -> str:
    return f'<a href="tg://user?id={uid}">{html.escape(name)}</a>'


def parse_duration(s: str):
    m = re.fullmatch(r"(\d+)\s*(с|сек|s|м|мин|m|ч|час|h|д|дн|d)?", s.lower())
    if not m:
        return None
    n, unit = int(m.group(1)), (m.group(2) or "м")
    mult = {"с": 1, "сек": 1, "s": 1, "м": 60, "мин": 60, "m": 60,
            "ч": 3600, "час": 3600, "h": 3600, "д": 86400, "дн": 86400, "d": 86400}[unit]
    return n * mult


def fmt_duration(sec) -> str:
    if not sec:
        return "навсегда"
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    mi, s = divmod(r, 60)
    parts = [f"{d} д" if d else "", f"{h} ч" if h else "", f"{mi} мин" if mi else "", f"{s} с" if s and not (d or h) else ""]
    return " ".join(p for p in parts if p) or "0 с"


async def is_tg_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        mem = await bot.get_chat_member(chat_id, user_id)
        return mem.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)
    except Exception:
        return False


async def is_owner(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        mem = await bot.get_chat_member(chat_id, user_id)
        return mem.status == ChatMemberStatus.CREATOR
    except Exception:
        return False


async def is_mod(bot: Bot, chat_id: int, user_id: int) -> bool:
    if await is_tg_admin(bot, chat_id, user_id):
        return True
    cur = await db.execute("SELECT 1 FROM mods WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    return await cur.fetchone() is not None


async def remember(user):
    await db.execute(
        "INSERT INTO users(user_id, username, name) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, name=excluded.name",
        (user.id, (user.username or "").lower(), user.full_name),
    )


async def get_target(m: Message, args: str):
    """Возвращает (uid, name, остаток_аргументов) или None."""
    if m.reply_to_message and m.reply_to_message.from_user:
        u = m.reply_to_message.from_user
        return u.id, u.full_name, args
    tok = args.split(maxsplit=1)
    if not tok:
        return None
    t, rest = tok[0], (tok[1] if len(tok) > 1 else "")
    uid = None
    if t.isdigit():
        uid = int(t)
    elif t.startswith("@"):
        cur = await db.execute("SELECT user_id FROM users WHERE username=?", (t[1:].lower(),))
        row = await cur.fetchone()
        uid = row[0] if row else None
    if uid is None:
        return None
    cur = await db.execute("SELECT name FROM users WHERE user_id=?", (uid,))
    row = await cur.fetchone()
    return uid, (row[0] if row else str(uid)), rest


def split_duration_reason(rest: str):
    tok = rest.split(maxsplit=1)
    if tok:
        d = parse_duration(tok[0])
        if d is not None:
            return d, (tok[1] if len(tok) > 1 else "")
    return None, rest


async def guard(m: Message, bot: Bot, args: str, need_target=True):
    """Общая проверка прав. Возвращает (uid, name, rest) или None (уже ответили)."""
    if not await is_mod(bot, m.chat.id, m.from_user.id):
        return None
    t = await get_target(m, args)
    if not t:
        await m.reply("⚠️ Ответь на сообщение пользователя или укажи @username / ID.")
        return None
    uid, name, rest = t
    if uid == bot.id:
        await m.reply("🙃 Себя я наказывать не буду.")
        return None
    if await is_mod(bot, m.chat.id, uid):
        await m.reply("🛡 Нельзя наказать модератора или администратора.")
        return None
    return uid, name, rest


# ───────────────────────── Команды модерации ─────────────────────────

async def cmd_ban(m: Message, bot: Bot, args: str):
    g = await guard(m, bot, args)
    if not g:
        return
    uid, name, reason = g
    await bot.ban_chat_member(m.chat.id, uid)
    await m.answer(
        f"🔨 {mention(uid, name)} <b>забанен</b>\n"
        f"👮 Модератор: {mention(m.from_user.id, m.from_user.full_name)}"
        + (f"\n📝 Причина: {html.escape(reason)}" if reason else "")
    )


async def cmd_unban(m: Message, bot: Bot, args: str):
    if not await is_mod(bot, m.chat.id, m.from_user.id):
        return
    t = await get_target(m, args)
    if not t:
        return await m.reply("⚠️ Укажи пользователя.")
    uid, name, _ = t
    await bot.unban_chat_member(m.chat.id, uid, only_if_banned=True)
    await m.answer(f"✅ {mention(uid, name)} <b>разбанен</b>")


async def cmd_kick(m: Message, bot: Bot, args: str):
    g = await guard(m, bot, args)
    if not g:
        return
    uid, name, reason = g
    await bot.ban_chat_member(m.chat.id, uid)
    await bot.unban_chat_member(m.chat.id, uid)
    await m.answer(f"👢 {mention(uid, name)} <b>кикнут</b>" + (f"\n📝 Причина: {html.escape(reason)}" if reason else ""))


async def cmd_mute(m: Message, bot: Bot, args: str):
    g = await guard(m, bot, args)
    if not g:
        return
    uid, name, rest = g
    dur, reason = split_duration_reason(rest)
    until = datetime.now() + timedelta(seconds=dur) if dur else None
    await bot.restrict_chat_member(m.chat.id, uid, MUTE_PERMS, until_date=until)
    await m.answer(
        f"🔇 {mention(uid, name)} <b>в муте</b> — {fmt_duration(dur)}\n"
        f"👮 Модератор: {mention(m.from_user.id, m.from_user.full_name)}"
        + (f"\n📝 Причина: {html.escape(reason)}" if reason else "")
    )


async def cmd_unmute(m: Message, bot: Bot, args: str):
    if not await is_mod(bot, m.chat.id, m.from_user.id):
        return
    t = await get_target(m, args)
    if not t:
        return await m.reply("⚠️ Укажи пользователя.")
    uid, name, _ = t
    await bot.restrict_chat_member(m.chat.id, uid, FULL_PERMS)
    await m.answer(f"🔊 {mention(uid, name)} <b>размучен</b>")


async def apply_warn(m: Message, bot: Bot, uid: int, name: str, reason: str, by: str):
    cfg = await chat_cfg(m.chat.id)
    await db.execute(
        "INSERT INTO warns(chat_id,user_id,count) VALUES(?,?,1) "
        "ON CONFLICT(chat_id,user_id) DO UPDATE SET count=count+1",
        (m.chat.id, uid),
    )
    await db.commit()
    cur = await db.execute("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (m.chat.id, uid))
    cnt = (await cur.fetchone())[0]
    text = f"⚠️ {mention(uid, name)} получил <b>предупреждение</b> [{cnt}/{cfg['maxwarns']}]\n👮 {by}"
    if reason:
        text += f"\n📝 Причина: {html.escape(reason)}"
    if cnt >= cfg["maxwarns"]:
        await bot.ban_chat_member(m.chat.id, uid)
        await db.execute("DELETE FROM warns WHERE chat_id=? AND user_id=?", (m.chat.id, uid))
        await db.commit()
        text += "\n🔨 Лимит предупреждений — <b>бан</b>."
    await m.answer(text)


async def cmd_warn(m: Message, bot: Bot, args: str):
    g = await guard(m, bot, args)
    if not g:
        return
    uid, name, reason = g
    await apply_warn(m, bot, uid, name, reason, mention(m.from_user.id, m.from_user.full_name))


async def cmd_unwarn(m: Message, bot: Bot, args: str):
    if not await is_mod(bot, m.chat.id, m.from_user.id):
        return
    t = await get_target(m, args)
    if not t:
        return await m.reply("⚠️ Укажи пользователя.")
    uid, name, _ = t
    await db.execute("UPDATE warns SET count=MAX(count-1,0) WHERE chat_id=? AND user_id=?", (m.chat.id, uid))
    await db.commit()
    await m.answer(f"✅ С {mention(uid, name)} снято одно предупреждение.")


async def cmd_warns(m: Message, bot: Bot, args: str):
    t = await get_target(m, args)
    uid, name = (t[0], t[1]) if t else (m.from_user.id, m.from_user.full_name)
    cfg = await chat_cfg(m.chat.id)
    cur = await db.execute("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (m.chat.id, uid))
    row = await cur.fetchone()
    await m.answer(f"📋 Предупреждения {mention(uid, name)}: <b>{row[0] if row else 0}/{cfg['maxwarns']}</b>")


async def cmd_del(m: Message, bot: Bot, args: str):
    if not await is_mod(bot, m.chat.id, m.from_user.id) or not m.reply_to_message:
        return
    try:
        await m.reply_to_message.delete()
        await m.delete()
    except Exception:
        await m.reply("❌ Не удалось удалить (нужны права на удаление).")


async def cmd_purge(m: Message, bot: Bot, args: str):
    """Удалить все сообщения от ответа до текущего."""
    if not await is_mod(bot, m.chat.id, m.from_user.id) or not m.reply_to_message:
        return
    ids = list(range(m.reply_to_message.message_id, m.message_id + 1))
    for i in range(0, len(ids), 100):
        try:
            await bot.delete_messages(m.chat.id, ids[i:i + 100])
        except Exception:
            pass
    note = await m.answer(f"🧹 Удалено сообщений: {len(ids)}")
    await asyncio.sleep(4)
    await note.delete()


async def cmd_pin(m: Message, bot: Bot, args: str):
    if await is_mod(bot, m.chat.id, m.from_user.id) and m.reply_to_message:
        await bot.pin_chat_message(m.chat.id, m.reply_to_message.message_id)


async def cmd_unpin(m: Message, bot: Bot, args: str):
    if await is_mod(bot, m.chat.id, m.from_user.id):
        await bot.unpin_chat_message(m.chat.id, m.reply_to_message.message_id if m.reply_to_message else None)


async def cmd_addmod(m: Message, bot: Bot, args: str):
    if not await is_tg_admin(bot, m.chat.id, m.from_user.id):
        return
    t = await get_target(m, args)
    if not t:
        return await m.reply("⚠️ Укажи пользователя.")
    await db.execute("INSERT OR IGNORE INTO mods VALUES(?,?)", (m.chat.id, t[0]))
    await db.commit()
    await m.answer(f"🛡 {mention(t[0], t[1])} назначен <b>модератором</b> бота.")


async def cmd_delmod(m: Message, bot: Bot, args: str):
    if not await is_tg_admin(bot, m.chat.id, m.from_user.id):
        return
    t = await get_target(m, args)
    if not t:
        return await m.reply("⚠️ Укажи пользователя.")
    await db.execute("DELETE FROM mods WHERE chat_id=? AND user_id=?", (m.chat.id, t[0]))
    await db.commit()
    await m.answer(f"✅ {mention(t[0], t[1])} больше не модератор.")


async def cmd_mods(m: Message, bot: Bot, args: str):
    cur = await db.execute(
        "SELECT m.user_id, COALESCE(u.name, m.user_id) FROM mods m LEFT JOIN users u ON u.user_id=m.user_id WHERE m.chat_id=?",
        (m.chat.id,),
    )
    rows = await cur.fetchall()
    admins = await bot.get_chat_administrators(m.chat.id)
    lines = ["👑 <b>Администраторы чата:</b>"]
    lines += [f"• {mention(a.user.id, a.user.full_name)}" for a in admins if not a.user.is_bot]
    if rows:
        lines.append("\n🛡 <b>Модераторы бота:</b>")
        lines += [f"• {mention(r[0], str(r[1]))}" for r in rows]
    await m.answer("\n".join(lines))


# ───────────────────────── Настройки чата ─────────────────────────

async def cmd_setwelcome(m: Message, bot: Bot, args: str):
    if not await is_mod(bot, m.chat.id, m.from_user.id):
        return
    if not args:
        return await m.reply("Использование: <code>+приветствие текст</code>\nПеременные: {name}, {chat}")
    await set_cfg(m.chat.id, "welcome", args)
    await m.reply("✅ Приветствие сохранено.")


async def cmd_setrules(m: Message, bot: Bot, args: str):
    if not await is_mod(bot, m.chat.id, m.from_user.id):
        return
    if not args:
        return await m.reply("Использование: <code>+правила текст</code>")
    await set_cfg(m.chat.id, "rules", args)
    await m.reply("✅ Правила сохранены.")


async def cmd_rules(m: Message, bot: Bot, args: str):
    cfg = await chat_cfg(m.chat.id)
    await m.answer("📜 <b>Правила чата</b>\n\n" + (cfg["rules"] or "Правила пока не заданы."))


async def cmd_toggle(m: Message, bot: Bot, args: str, key: str, title: str):
    if not await is_mod(bot, m.chat.id, m.from_user.id):
        return
    cfg = await chat_cfg(m.chat.id)
    if args.lower() in ("вкл", "on", "1"):
        val = 1
    elif args.lower() in ("выкл", "off", "0"):
        val = 0
    else:
        val = 0 if cfg[key] else 1
    await set_cfg(m.chat.id, key, val)
    await m.reply(f"{'✅' if val else '❌'} {title}: <b>{'включено' if val else 'выключено'}</b>")


async def cmd_maxwarns(m: Message, bot: Bot, args: str):
    if not await is_mod(bot, m.chat.id, m.from_user.id) or not args.isdigit():
        return
    n = max(1, min(int(args), 20))
    await set_cfg(m.chat.id, "maxwarns", n)
    await m.reply(f"✅ Лимит предупреждений: <b>{n}</b>")


async def cmd_addword(m: Message, bot: Bot, args: str):
    if not await is_mod(bot, m.chat.id, m.from_user.id) or not args:
        return
    await db.execute("INSERT OR IGNORE INTO words VALUES(?,?)", (m.chat.id, args.lower().strip()))
    await db.commit()
    await m.reply("✅ Слово добавлено в чёрный список.")


async def cmd_delword(m: Message, bot: Bot, args: str):
    if not await is_mod(bot, m.chat.id, m.from_user.id) or not args:
        return
    await db.execute("DELETE FROM words WHERE chat_id=? AND word=?", (m.chat.id, args.lower().strip()))
    await db.commit()
    await m.reply("✅ Слово удалено.")


async def cmd_words(m: Message, bot: Bot, args: str):
    if not await is_mod(bot, m.chat.id, m.from_user.id):
        return
    cur = await db.execute("SELECT word FROM words WHERE chat_id=?", (m.chat.id,))
    ws = [r[0] for r in await cur.fetchall()]
    await m.reply("🚫 Чёрный список:\n" + (", ".join(ws) if ws else "пусто"))


async def cmd_settings(m: Message, bot: Bot, args: str):
    if not await is_mod(bot, m.chat.id, m.from_user.id):
        return
    c = await chat_cfg(m.chat.id)
    on = lambda v: "✅" if v else "❌"
    await m.answer(
        "⚙️ <b>Настройки LumenЧат</b>\n\n"
        f"{on(c['captcha'])} Капча для новичков (<code>капча</code>)\n"
        f"{on(c['antiflood'])} Антифлуд (<code>антифлуд</code>)\n"
        f"{on(c['antilink'])} Антиссылки (<code>антиссылки</code>)\n"
        f"⚠️ Лимит варнов: <b>{c['maxwarns']}</b> (<code>лимит 3</code>)"
    )


# ───────────────────────── Информация / статистика ─────────────────────────

async def cmd_info(m: Message, bot: Bot, args: str):
    t = await get_target(m, args)
    uid, name = (t[0], t[1]) if t else (m.from_user.id, m.from_user.full_name)
    try:
        mem = await bot.get_chat_member(m.chat.id, uid)
        status = {"creator": "👑 Владелец", "administrator": "🛡 Администратор", "member": "👤 Участник",
                  "restricted": "🔇 Ограничен", "left": "🚪 Вышел", "kicked": "🔨 Забанен"}.get(mem.status.value, mem.status.value)
    except Exception:
        status = "❓ неизвестно"
    cur = await db.execute("SELECT msgs FROM stats WHERE chat_id=? AND user_id=?", (m.chat.id, uid))
    row = await cur.fetchone()
    cur = await db.execute("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (m.chat.id, uid))
    w = await cur.fetchone()
    await m.answer(
        f"ℹ️ <b>Информация</b>\n\n👤 {mention(uid, name)}\n🆔 <code>{uid}</code>\n"
        f"{status}\n💬 Сообщений: <b>{row[0] if row else 0}</b>\n⚠️ Варнов: <b>{w[0] if w else 0}</b>"
    )


async def cmd_top(m: Message, bot: Bot, args: str):
    cur = await db.execute(
        "SELECT s.user_id, COALESCE(u.name,'?'), s.msgs FROM stats s LEFT JOIN users u ON u.user_id=s.user_id "
        "WHERE s.chat_id=? ORDER BY s.msgs DESC LIMIT 10",
        (m.chat.id,),
    )
    rows = await cur.fetchall()
    medals = ["🥇", "🥈", "🥉"] + ["▫️"] * 7
    text = "🏆 <b>Топ активных</b>\n\n" + "\n".join(
        f"{medals[i]} {mention(r[0], r[1])} — <b>{r[2]}</b>" for i, r in enumerate(rows)
    )
    await m.answer(text if rows else "Пока нет статистики.")


async def cmd_help(m: Message, bot: Bot, args: str):
    await m.answer(
        "💜 <b>LumenЧат — команды</b>\n"
        "<i>Работают и с /, и без. Отвечай на сообщение или указывай @username/ID.</i>\n\n"
        "<b>Модерация</b>\n"
        "• <code>бан</code> / <code>разбан</code> / <code>кик</code> [причина]\n"
        "• <code>мут 30м причина</code> / <code>размут</code> (с/м/ч/д)\n"
        "• <code>варн</code> / <code>снять варн</code> / <code>варны</code>\n"
        "• <code>удалить</code> — удалить сообщение\n"
        "• <code>очистить</code> — удалить всё от сообщения-ответа\n"
        "• <code>закреп</code> / <code>открепить</code>\n\n"
        "<b>Роли</b>\n"
        "• <code>+модер</code> / <code>-модер</code> / <code>модеры</code>\n\n"
        "<b>Настройки</b>\n"
        "• <code>настройки</code> • <code>капча</code> • <code>антифлуд</code> • <code>антиссылки</code>\n"
        "• <code>лимит 3</code> • <code>+приветствие текст</code> • <code>+правила текст</code>\n"
        "• <code>+слово</code> / <code>-слово</code> / <code>слова</code> — чёрный список\n\n"
        "<b>Для всех</b>\n"
        "• <code>правила</code> • <code>инфо</code> • <code>топ</code>"
    )


# ───────────────────────── Роутинг команд ─────────────────────────

COMMANDS = {}


def reg(names, func, **kw):
    for n in names:
        COMMANDS[n] = (func, kw)


reg(["бан", "ban"], cmd_ban)
reg(["разбан", "unban"], cmd_unban)
reg(["кик", "kick"], cmd_kick)
reg(["мут", "mute"], cmd_mute)
reg(["размут", "unmute"], cmd_unmute)
reg(["варн", "warn", "пред"], cmd_warn)
reg(["снять варн", "unwarn", "-варн", "снятьварн"], cmd_unwarn)
reg(["варны", "warns", "преды"], cmd_warns)
reg(["удалить", "del", "дел"], cmd_del)
reg(["очистить", "purge", "чистка"], cmd_purge)
reg(["закреп", "pin"], cmd_pin)
reg(["открепить", "unpin"], cmd_unpin)
reg(["+модер", "addmod"], cmd_addmod)
reg(["-модер", "delmod"], cmd_delmod)
reg(["модеры", "mods", "админы"], cmd_mods)
reg(["+приветствие", "setwelcome"], cmd_setwelcome)
reg(["+правила", "setrules"], cmd_setrules)
reg(["правила", "rules"], cmd_rules)
reg(["капча", "captcha"], cmd_toggle, key="captcha", title="Капча")
reg(["антифлуд", "antiflood"], cmd_toggle, key="antiflood", title="Антифлуд")
reg(["антиссылки", "antilink"], cmd_toggle, key="antilink", title="Антиссылки")
reg(["лимит", "maxwarns"], cmd_maxwarns)
reg(["+слово", "addword"], cmd_addword)
reg(["-слово", "delword"], cmd_delword)
reg(["слова", "words"], cmd_words)
reg(["настройки", "settings"], cmd_settings)
reg(["инфо", "info", "кто"], cmd_info)
reg(["топ", "top"], cmd_top)
reg(["помощь", "help", "команды"], cmd_help)


def parse_command(text: str):
    text = text.strip()
    low = text.lower()
    if low.startswith("/"):
        low, text = low[1:], text[1:]
    # двухсловные команды
    if low.startswith("снять варн"):
        return "снять варн", text[len("снять варн"):].strip()
    first, _, rest = text.partition(" ")
    first_low = first.lower()
    if "@" in first_low and not first_low.startswith("@"):
        name, _, bot_name = first_low.partition("@")
        if bot_name != BOT_USERNAME.lower():
            return None
        first_low = name
    return first_low, rest.strip()


# ───────────────────────── Автомодерация ─────────────────────────

async def automod(m: Message, bot: Bot) -> bool:
    """True — сообщение удалено/обработано."""
    if await is_mod(bot, m.chat.id, m.from_user.id):
        return False
    cfg = await chat_cfg(m.chat.id)
    text = m.text or m.caption or ""

    # антифлуд: 6 сообщений за 5 сек
    if cfg["antiflood"]:
        key = (m.chat.id, m.from_user.id)
        now = time.time()
        flood_log[key].append(now)
        recent = [t for t in flood_log[key] if now - t <= 5]
        if len(recent) >= 6:
            flood_log[key].clear()
            try:
                await bot.restrict_chat_member(
                    m.chat.id, m.from_user.id, MUTE_PERMS,
                    until_date=datetime.now() + timedelta(minutes=5),
                )
                await m.answer(f"🌊 {mention(m.from_user.id, m.from_user.full_name)} — <b>флуд</b>, мут на 5 минут.")
            except Exception:
                pass
            return True

    # антиссылки
    if cfg["antilink"] and LINK_RE.search(text):
        try:
            await m.delete()
            await m.answer(f"🔗 {mention(m.from_user.id, m.from_user.full_name)}, ссылки в чате запрещены.")
        except Exception:
            pass
        return True

    # чёрный список слов
    if text:
        cur = await db.execute("SELECT word FROM words WHERE chat_id=?", (m.chat.id,))
        low = text.lower()
        for (w,) in await cur.fetchall():
            if w in low:
                try:
                    await m.delete()
                except Exception:
                    pass
                await apply_warn(m, bot, m.from_user.id, m.from_user.full_name, "запрещённое слово", "🤖 Автомодерация")
                return True
    return False


# ───────────────────────── Хэндлеры ─────────────────────────

GROUP = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def start(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➕ Добавить в чат", url=f"https://t.me/{BOT_USERNAME}?startgroup=true&admin=delete_messages+restrict_members+pin_messages+invite_users")
    ]])
    await m.answer(
        "💜 <b>LumenЧат</b> — модератор для твоего чата.\n\n"
        "Бан, мут, варны, капча, антифлуд, антиссылки, чёрный список слов, статистика и многое другое.\n\n"
        "Добавь меня в чат, дай права администратора и напиши <code>помощь</code>.",
        reply_markup=kb,
    )


@router.message(GROUP, F.text | F.caption)
async def on_message(m: Message, bot: Bot):
    if not m.from_user or m.from_user.is_bot:
        return
    await remember(m.from_user)
    await db.execute(
        "INSERT INTO stats(chat_id,user_id,msgs) VALUES(?,?,1) "
        "ON CONFLICT(chat_id,user_id) DO UPDATE SET msgs=msgs+1",
        (m.chat.id, m.from_user.id),
    )
    await db.commit()
    if m.reply_to_message and m.reply_to_message.from_user:
        await remember(m.reply_to_message.from_user)

    if await automod(m, bot):
        return
    if not m.text:
        return

    parsed = parse_command(m.text)
    if not parsed:
        return
    name, args = parsed
    entry = COMMANDS.get(name)
    if not entry:
        return
    func, kw = entry
    try:
        await func(m, bot, args, **kw)
    except Exception as e:
        logging.exception("command error")
        await m.reply(f"❌ Ошибка: <code>{html.escape(str(e))}</code>\nПроверь, что у бота есть права администратора.")


@router.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_join(ev: ChatMemberUpdated, bot: Bot):
    user = ev.new_chat_member.user
    if user.is_bot:
        return
    await remember(user)
    cfg = await chat_cfg(ev.chat.id)

    if cfg["captcha"]:
        try:
            await bot.restrict_chat_member(ev.chat.id, user.id, MUTE_PERMS)
        except Exception:
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Я не бот", callback_data=f"cap:{user.id}")
        ]])
        msg = await bot.send_message(
            ev.chat.id,
            f"👋 {mention(user.id, user.full_name)}, нажми кнопку в течение 2 минут, чтобы получить доступ к чату.",
            reply_markup=kb,
        )
        pending_captcha[(ev.chat.id, user.id)] = msg.message_id
        asyncio.create_task(captcha_timeout(bot, ev.chat.id, user.id, msg.message_id))
    else:
        await send_welcome(bot, ev.chat, user, cfg)


async def send_welcome(bot: Bot, chat, user, cfg):
    text = cfg["welcome"].replace("{name}", mention(user.id, user.full_name)).replace("{chat}", html.escape(chat.title or ""))
    await bot.send_message(chat.id, text)


async def captcha_timeout(bot: Bot, chat_id: int, user_id: int, msg_id: int):
    await asyncio.sleep(120)
    if pending_captcha.pop((chat_id, user_id), None):
        try:
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)
            await bot.delete_message(chat_id, msg_id)
        except Exception:
            pass


@router.callback_query(F.data.startswith("cap:"))
async def captcha_cb(cb: CallbackQuery, bot: Bot):
    uid = int(cb.data.split(":")[1])
    if cb.from_user.id != uid:
        return await cb.answer("Эта кнопка не для тебя 🙂", show_alert=True)
    chat_id = cb.message.chat.id
    pending_captcha.pop((chat_id, uid), None)
    try:
        await bot.restrict_chat_member(chat_id, uid, FULL_PERMS)
        await cb.message.delete()
    except Exception:
        pass
    await cb.answer("Добро пожаловать! 💜")
    cfg = await chat_cfg(chat_id)
    await send_welcome(bot, cb.message.chat, cb.from_user, cfg)


# ───────────────────────── Запуск ─────────────────────────

async def main():
    global BOT_USERNAME
    await init_db()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    BOT_USERNAME = (await bot.get_me()).username
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("LumenЧат запущен как @%s", BOT_USERNAME)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "chat_member", "my_chat_member"])


if __name__ == "__main__":
    asyncio.run(main())
