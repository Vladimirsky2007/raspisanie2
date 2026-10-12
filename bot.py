"""Telegram-бот расписания Политеха. Запуск: python bot.py"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
from render import fmt_date, render_day, short_date
from storage import Storage, course_of
from watcher import Watcher

log = logging.getLogger("bot")
router = Router()

cfg: config.Config
storage: Storage
watcher: Watcher

ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI"}


# ---------- вспомогательное -------------------------------------------------

def today() -> dt.date:
    return dt.datetime.now(cfg.tz).date()


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def chunk(items: list, n: int) -> list[list]:
    return [items[i : i + n] for i in range(0, len(items), n)]


def visible_group(group: str) -> str:
    return group.split("::", 1)[-1]


def kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def msg_of(target: Message | CallbackQuery) -> Message:
    return target.message if isinstance(target, CallbackQuery) else target


async def show(target: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    """Для кнопок редактируем текущее сообщение, для команд — отправляем новое."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def allowed(target: Message | CallbackQuery) -> bool:
    """В личке можно всем; в группе менять настройки могут только админы."""
    msg = msg_of(target)
    chat = msg.chat
    if chat.type == ChatType.PRIVATE:
        return True
    if isinstance(target, Message) and target.sender_chat and target.sender_chat.id == chat.id:
        return True  # анонимный админ
    try:
        member = await msg.bot.get_chat_member(chat.id, target.from_user.id)
    except Exception:
        return False
    return member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)


async def deny(target: Message | CallbackQuery) -> None:
    text = "Менять настройки в групповом чате могут только администраторы."
    if isinstance(target, CallbackQuery):
        await target.answer(text, show_alert=True)
    else:
        await target.answer(text)


# ---------- экраны ------------------------------------------------------------

async def menu_screen(target: Message | CallbackQuery, note: str = "") -> None:
    chat_id = msg_of(target).chat.id
    storage.ensure_user(chat_id)
    user = storage.get_user(chat_id)
    if not user["group_name"]:
        return await location_screen(target)
    notify = bool(user["notify"])
    text = (
        (note + "\n\n" if note else "")
        + f"👥 Группа: <b>{html.escape(visible_group(user['group_name']))}</b>\n"
        + f"🔔 Уведомления о новом расписании: <b>{'включены' if notify else 'выключены'}</b>"
    )
    markup = kb(
        [
            [btn("📅 Сегодня", "today"), btn("📅 Завтра", "tomorrow")],
            [btn("🗓 Выбрать дату", "dates")],
            [btn("👥 Сменить группу", "chg")],
            [btn(f"🔔 Уведомления: {'выкл' if notify else 'вкл'}", "notify")],
        ]
    )
    await show(target, text, markup)


async def location_screen(target: Message | CallbackQuery) -> None:
    user = storage.get_user(msg_of(target).chat.id)
    rows = [[btn("🏫 ул. Юбилейная", "loc:Юбилейная")], [btn("🏫 ул. Луначарского", "loc:Луначарского")]]
    if user and user["group_name"]:
        rows.append([btn("🏠 Меню", "menu")])
    await show(target, "Сначала выберите корпус колледжа:", kb(rows))


async def course_screen(target: Message | CallbackQuery, location: str | None = None) -> None:
    user = storage.get_user(msg_of(target).chat.id)
    location = location or (user["location"] if user else None)
    if not location:
        return await location_screen(target)
    t = today()
    groups = storage.groups(t - dt.timedelta(days=30), location)
    if not groups:
        return await show(
            target,
            "Расписание пока не загружено. Попробуйте через пару минут.",
            kb([[btn("🔄 Повторить", "chg")]]),
        )
    courses = sorted({course_of(g, t) or 0 for g in groups})
    buttons = [btn(f"{ROMAN[c]} курс" if c else "Другие", f"c:{c}") for c in courses]
    rows = chunk(buttons, 2)
    rows.append([btn("◀️ Выбрать улицу", "chg")])
    await show(target, f"Корпус: {location}. Выберите курс:", kb(rows))


async def groups_screen(target: CallbackQuery, course: int) -> None:
    t = today()
    user = storage.get_user(msg_of(target).chat.id)
    location = user["location"] if user else None
    groups = [g for g in storage.groups(t - dt.timedelta(days=30), location) if (course_of(g, t) or 0) == course]
    rows = chunk([btn(visible_group(g), f"g:{g}") for g in groups], 3)
    rows.append([btn("◀️ Назад", "chg")])
    title = f"{ROMAN[course]} курс" if course else "Другие группы"
    await show(target, f"{location} — {title}. Выберите группу:", kb(rows))


async def dates_screen(target: Message | CallbackQuery) -> None:
    ds = storage.dates(today() - dt.timedelta(days=7))[:12]
    if not ds:
        return await show(target, "Расписание пока не загружено.", kb([[btn("🏠 Меню", "menu")]]))
    rows = chunk([btn(short_date(d), f"d:{d.isoformat()}") for d in ds], 3)
    rows.append([btn("🏠 Меню", "menu")])
    await show(target, "На какую дату показать расписание?", kb(rows))


async def day_screen(target: Message | CallbackQuery, d: dt.date) -> None:
    chat_id = msg_of(target).chat.id
    storage.ensure_user(chat_id)
    user = storage.get_user(chat_id)
    if not user["group_name"]:
        return await course_screen(target)

    known = storage.dates(today() - dt.timedelta(days=7))
    if d not in known:
        text = f"Расписания на <b>{html.escape(fmt_date(d))}</b> пока нет — оно ещё не опубликовано."
        return await show(target, text, kb([[btn("🗓 Другие даты", "dates")], [btn("🏠 Меню", "menu")]]))

    text = render_day(d, visible_group(user["group_name"]), storage.get_group_day(d, user["group_name"]))
    prev = max((x for x in known if x < d), default=None)
    nxt = min((x for x in known if x > d), default=None)
    nav = []
    if prev:
        nav.append(btn(f"◀️ {short_date(prev)}", f"d:{prev.isoformat()}"))
    if nxt:
        nav.append(btn(f"{short_date(nxt)} ▶️", f"d:{nxt.isoformat()}"))
    rows = ([nav] if nav else []) + [[btn("🗓 Даты", "dates"), btn("🏠 Меню", "menu")]]
    await show(target, text, kb(rows))


# ---------- команды -------------------------------------------------------------

@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(m: Message) -> None:
    await menu_screen(m)


@router.message(Command("group"))
async def cmd_group(m: Message) -> None:
    if not await allowed(m):
        return await deny(m)
    storage.ensure_user(m.chat.id)
    await location_screen(m)


@router.message(Command("today"))
async def cmd_today(m: Message) -> None:
    await day_screen(m, today())


@router.message(Command("tomorrow"))
async def cmd_tomorrow(m: Message) -> None:
    await day_screen(m, today() + dt.timedelta(days=1))


@router.message(Command("dates"))
async def cmd_dates(m: Message) -> None:
    await dates_screen(m)


@router.message(Command("status"))
async def cmd_status(m: Message) -> None:
    if m.from_user.id not in cfg.admin_ids:
        return
    total, subs = storage.stats()
    ok = (
        dt.datetime.fromtimestamp(watcher.last_ok, cfg.tz).strftime("%d.%m %H:%M")
        if watcher.last_ok
        else "ещё не было"
    )
    text = (
        f"Чатов: {total}, с уведомлениями: {subs}\n"
        f"Последняя успешная проверка: {ok}\n"
        f"Ошибка: {html.escape(watcher.last_error or 'нет')}"
    )
    await m.answer(text)


@router.message(Command("refresh"))
async def cmd_refresh(m: Message) -> None:
    if m.from_user.id not in cfg.admin_ids:
        return
    await m.answer("Проверяю сайт...")
    result = await watcher.check_once(force=True)
    await m.answer(html.escape(result))


# ---------- кнопки --------------------------------------------------------------

@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery) -> None:
    await menu_screen(cb)


@router.callback_query(F.data == "today")
async def cb_today(cb: CallbackQuery) -> None:
    await day_screen(cb, today())


@router.callback_query(F.data == "tomorrow")
async def cb_tomorrow(cb: CallbackQuery) -> None:
    await day_screen(cb, today() + dt.timedelta(days=1))


@router.callback_query(F.data == "dates")
async def cb_dates(cb: CallbackQuery) -> None:
    await dates_screen(cb)


@router.callback_query(F.data.startswith("d:"))
async def cb_date(cb: CallbackQuery) -> None:
    try:
        d = dt.date.fromisoformat(cb.data[2:])
    except ValueError:
        return await cb.answer()
    await day_screen(cb, d)


@router.callback_query(F.data == "chg")
async def cb_change_group(cb: CallbackQuery) -> None:
    if not await allowed(cb):
        return await deny(cb)
    await location_screen(cb)


@router.callback_query(F.data.startswith("loc:"))
async def cb_location(cb: CallbackQuery) -> None:
    if not await allowed(cb):
        return await deny(cb)
    location = cb.data[4:]
    if location not in cfg.pages:
        return await cb.answer()
    storage.set_location(cb.message.chat.id, location)
    await course_screen(cb, location)


@router.callback_query(F.data.startswith("c:"))
async def cb_course(cb: CallbackQuery) -> None:
    if not await allowed(cb):
        return await deny(cb)
    try:
        course = int(cb.data[2:])
    except ValueError:
        return await cb.answer()
    await groups_screen(cb, course)


@router.callback_query(F.data.startswith("g:"))
async def cb_group(cb: CallbackQuery) -> None:
    if not await allowed(cb):
        return await deny(cb)
    group = cb.data[2:]
    user = storage.get_user(cb.message.chat.id)
    if group not in storage.groups(today() - dt.timedelta(days=30), user["location"] if user else None):
        return await cb.answer("Такой группы нет в расписании", show_alert=True)
    storage.set_group(cb.message.chat.id, group)
    await menu_screen(cb, note="✅ Группа сохранена.")


@router.callback_query(F.data == "notify")
async def cb_notify(cb: CallbackQuery) -> None:
    if not await allowed(cb):
        return await deny(cb)
    storage.ensure_user(cb.message.chat.id)
    storage.toggle_notify(cb.message.chat.id)
    await menu_screen(cb)


async def start_health_server() -> web.AppRunner | None:
    """Мини-сервер для хостингов вроде Render: отвечает «ok» на GET/HEAD /.
    Его пингует UptimeRobot / cron-job.org, чтобы сервис не засыпал. Включается, если задана PORT."""
    port = int(os.environ.get("PORT", "0") or 0)
    if not port:
        return None
    app = web.Application()
    app.router.add_get("/", lambda request: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("health-сервер запущен на порту %s", port)
    return runner


# ---------- запуск --------------------------------------------------------------

async def main() -> None:
    global cfg, storage, watcher
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load()
    storage = Storage(cfg.db_path)

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    async def send(chat_id: int, text: str) -> None:
        for attempt in range(2):
            try:
                await bot.send_message(chat_id, text)
                return
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                storage.deactivate(chat_id)  # бот заблокирован / удалён из чата
                return
            except TelegramBadRequest as e:
                if "chat not found" in str(e).lower():
                    storage.deactivate(chat_id)
                    return
                raise

    async def notify_admin(text: str) -> None:
        for admin_id in cfg.admin_ids:
            try:
                await bot.send_message(admin_id, text, parse_mode=None)
            except Exception:
                log.exception("не удалось написать админу %s", admin_id)

    watcher = Watcher(cfg, storage, send, notify_admin)
    health = await start_health_server()
    task = asyncio.create_task(watcher.run_forever())

    await bot.set_my_commands(
        [
            BotCommand(command="menu", description="Главное меню"),
            BotCommand(command="today", description="Расписание на сегодня"),
            BotCommand(command="tomorrow", description="Расписание на завтра"),
            BotCommand(command="dates", description="Выбрать дату"),
            BotCommand(command="group", description="Сменить группу"),
        ]
    )
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        if health:
            await health.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
