"""Команди адміністратора та кнопки модерації."""
from __future__ import annotations

import html
from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.types import BotCommand, CallbackQuery, LinkPreviewOptions, Message

from .db import utcnow
from .keyboards import ModCb
from .service import NewsService

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

ADMIN_COMMANDS = [
    BotCommand(command="status", description="Стан бота і черги"),
    BotCommand(command="sources", description="Список джерел"),
    BotCommand(command="add", description="Додати джерело: /add <адреса> [назва]"),
    BotCommand(command="toggle", description="Увімкнути/вимкнути джерело: /toggle <id>"),
    BotCommand(command="remove", description="Видалити джерело: /remove <id>"),
    BotCommand(command="fetch", description="Зібрати новини зараз"),
    BotCommand(command="queue", description="Наступні пости в черзі"),
    BotCommand(command="review", description="Новини, що чекають модерації"),
    BotCommand(command="post", description="Опублікувати наступний пост зараз"),
    BotCommand(command="pause", description="Призупинити публікації"),
    BotCommand(command="resume", description="Відновити публікації"),
]

HELP = (
    "📚 <b>Бот книжкових новин</b>\n\n"
    "Я збираю новини з RSS-стрічок і публікую їх у каналі {channel}.\n\n"
    "<b>Команди:</b>\n"
    + "\n".join(f"/{c.command} — {html.escape(c.description)}" for c in ADMIN_COMMANDS)
    + "\n\nРежим модерації: <b>{moderation}</b>"
)


class IsAdmin(BaseFilter):
    def __init__(self, admin_ids: frozenset[int]) -> None:
        self.admin_ids = admin_ids

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return bool(event.from_user and event.from_user.id in self.admin_ids)


def _ago(dt) -> str:
    if not dt:
        return "ще не було"
    delta = utcnow() - dt
    if delta < timedelta(minutes=1):
        return "щойно"
    if delta < timedelta(hours=1):
        return f"{int(delta.total_seconds() // 60)} хв тому"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)} год тому"
    return f"{delta.days} дн тому"


def _short(text: str, n: int = 70) -> str:
    return html.escape(text if len(text) <= n else text[: n - 1] + "…")


def create_router(admin_ids: frozenset[int]) -> Router:
    router = Router(name="admin")
    admin = IsAdmin(admin_ids)

    # ---------------------------------------------------------------- general

    @router.message(CommandStart(), admin)
    @router.message(Command("help"), admin)
    async def cmd_help(message: Message, service: NewsService) -> None:
        s = service.settings
        await message.answer(HELP.format(
            channel=html.escape(str(s.channel_id)),
            moderation="увімкнено" if s.moderation else "вимкнено",
        ))

    @router.message(CommandStart())
    async def cmd_start_public(message: Message, service: NewsService) -> None:
        channel = service.settings.channel_id
        text = "Привіт! Я публікую книжкові новини"
        if isinstance(channel, str) and channel.startswith("@"):
            text += f" у каналі {html.escape(channel)} — підписуйтеся 📚"
        else:
            text += " 📚"
        uid = message.from_user.id if message.from_user else "?"
        text += f"\n\nВаш Telegram ID: <code>{uid}</code>"
        await message.answer(text)

    # ---------------------------------------------------------------- status

    @router.message(Command("status"), admin)
    async def cmd_status(message: Message, service: NewsService) -> None:
        db, s = service.db, service.settings
        counts = await db.counts()
        sources = await db.list_sources()
        active = [x for x in sources if x.enabled]
        broken = [x for x in active if x.last_error]
        day = await db.published_since(utcnow() - timedelta(days=1))
        reason = await service.blocked_reason()
        last = await db.last_posted_at()
        lines = [
            "📊 <b>Стан бота</b>",
            f"Канал: {html.escape(str(s.channel_id))}",
            f"Джерела: {len(active)} активних з {len(sources)}" + (f", з помилками: {len(broken)}" if broken else ""),
            f"У черзі: {counts.get('queued', 0)}",
            f"На модерації: {counts.get('review', 0)}" if s.moderation else None,
            f"Опубліковано за добу: {day} (усього {counts.get('published', 0)})",
            f"Останній пост: {_ago(last)}",
            f"Інтервал: збір кожні {s.fetch_interval_min} хв, пост кожні {s.post_interval_min} хв",
            f"⏸ Зараз не публікую: {html.escape(reason)}" if reason else "▶️ Публікації активні",
        ]
        await message.answer("\n".join(l for l in lines if l))

    @router.message(Command("sources"), admin)
    async def cmd_sources(message: Message, service: NewsService) -> None:
        sources = await service.db.list_sources()
        if not sources:
            await message.answer("Джерел немає. Додайте їх у sources.yaml або командою /add")
            return
        lines = ["📰 <b>Джерела</b>"]
        for x in sources:
            icon = "⚪️" if not x.enabled else ("🔴" if x.last_error else ("🟢" if x.last_ok_at else "🟡"))
            line = f"{icon} <code>{x.id}</code> {_short(x.name, 40)} — перевірка {_ago(x.last_fetch_at)}"
            if x.enabled and x.last_error:
                line += f"\n     ↳ {_short(x.last_error, 90)}"
            lines.append(line)
        lines.append("\n🟢 працює  🟡 ще не перевірялось  🔴 помилка  ⚪️ вимкнено")
        await message.answer("\n".join(lines), link_preview_options=NO_PREVIEW)

    @router.message(Command("add"), admin)
    async def cmd_add(message: Message, command: CommandObject, service: NewsService) -> None:
        if not command.args:
            await message.answer(
                "Використання: <code>/add https://site.ua/feed/ Назва</code>\n"
                "Можна дати й адресу сайту — я спробую знайти RSS сам."
            )
            return
        url, _, name = command.args.strip().partition(" ")
        wait = await message.answer("⏳ Перевіряю стрічку…")
        src, info = await service.add_source(url, name.strip() or None)
        if src:
            await wait.edit_text(
                f"✅ Додано <b>{html.escape(src.name)}</b> (id <code>{src.id}</code>)\n"
                f"Стрічка: {html.escape(src.feed_url or src.url)}\n{html.escape(info)}",
                link_preview_options=NO_PREVIEW,
            )
        else:
            await wait.edit_text(f"❌ {html.escape(info)}")

    @router.message(Command("remove", "toggle"), admin)
    async def cmd_remove_toggle(message: Message, command: CommandObject, service: NewsService) -> None:
        arg = (command.args or "").strip()
        if not arg.isdigit():
            await message.answer(f"Вкажіть id джерела: <code>/{command.command} 3</code> (див. /sources)")
            return
        sid = int(arg)
        if command.command == "remove":
            src = await service.db.remove_source(sid)
            if not src:
                await message.answer("Джерело не знайдено")
            elif src.origin == "yaml":
                await message.answer(
                    f"⚪️ «{html.escape(src.name)}» вимкнено. Воно з sources.yaml — щоб прибрати назавжди, "
                    "видаліть його з файлу або поставте <code>enabled: false</code>."
                )
            else:
                await message.answer(f"🗑 «{html.escape(src.name)}» видалено")
            return
        src = await service.db.get_source(sid)
        if not src:
            await message.answer("Джерело не знайдено")
            return
        src = await service.db.set_source_enabled(sid, not src.enabled)
        await message.answer(f"{'🟢 Увімкнено' if src.enabled else '⚪️ Вимкнено'}: {html.escape(src.name)}")

    # ---------------------------------------------------------------- queue

    @router.message(Command("fetch"), admin)
    async def cmd_fetch(message: Message, service: NewsService) -> None:
        wait = await message.answer("⏳ Збираю новини…")
        report = await service.fetch_all()
        lines = [f"Готово. Нових новин: <b>{report.total_new}</b>"]
        for r in report.sources:
            lines.append(f"{'🔴' if r.error else '🟢'} {_short(r.name, 40)}: "
                         + (_short(r.error, 80) if r.error else f"+{r.new}"))
        if report.total_new and service.settings.moderation:
            lines.append("\nНові новини надіслано вам на модерацію.")
        await wait.edit_text("\n".join(lines))

    @router.message(Command("queue"), admin)
    async def cmd_queue(message: Message, service: NewsService) -> None:
        items = await service.db.queued(limit=10)
        if not items:
            await message.answer("Черга порожня. /fetch — зібрати новини зараз")
            return
        lines = ["🗂 <b>Наступні в черзі</b>"]
        for i, it in enumerate(items, 1):
            lines.append(f"{i}. {_short(it.title)} <i>({html.escape(it.source_name)})</i>")
        await message.answer("\n".join(lines))

    @router.message(Command("review"), admin)
    async def cmd_review(message: Message, service: NewsService) -> None:
        items = await service.db.in_review(limit=10)
        if not items:
            await message.answer("Немає новин, що чекають модерації")
            return
        await message.answer(f"Надсилаю {len(items)} новин на модерацію ще раз…")
        for it in items:
            await service.send_for_review(it)

    @router.message(Command("post"), admin)
    async def cmd_post(message: Message, service: NewsService) -> None:
        result = await service.publish_next(force=True)
        if result.ok:
            await message.answer(f"✅ Опубліковано: {_short(result.item.title)}")
        else:
            await message.answer(f"Не опубліковано: {html.escape(result.reason)}")

    @router.message(Command("pause"), admin)
    async def cmd_pause(message: Message, service: NewsService) -> None:
        await service.db.set_state("paused", "1")
        await message.answer("⏸ Публікації призупинено. Новини й далі збираються в чергу. /resume — відновити")

    @router.message(Command("resume"), admin)
    async def cmd_resume(message: Message, service: NewsService) -> None:
        await service.db.set_state("paused", "0")
        await message.answer("▶️ Публікації відновлено")

    # ---------------------------------------------------------------- moderation buttons

    @router.callback_query(ModCb.filter(F.action == "noop"))
    async def cb_noop(callback: CallbackQuery) -> None:
        await callback.answer()

    @router.callback_query(ModCb.filter(), admin)
    async def cb_moderate(callback: CallbackQuery, callback_data: ModCb, service: NewsService) -> None:
        ok, text = await service.moderate(callback_data.item_id, callback_data.action)
        await callback.answer(text, show_alert=not ok)

    @router.callback_query(ModCb.filter())
    async def cb_denied(callback: CallbackQuery) -> None:
        await callback.answer("Модерувати можуть лише адміністратори", show_alert=True)

    @router.message(admin)
    async def admin_fallback(message: Message) -> None:
        await message.answer("Не знаю такої команди. /help — список команд")

    return router
