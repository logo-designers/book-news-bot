"""Точки входу: запуск бота, перевірка джерел, попередній перегляд постів."""
from __future__ import annotations

import asyncio
import html
import logging
import re

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommandScopeChat

from .config import Settings
from .db import Database, utcnow
from .feeds import FeedFetcher
from .filters import auto_tags, is_fresh, passes, title_key
from .formatter import build_post
from .handlers import ADMIN_COMMANDS, create_router
from .service import NewsService
from .sources import load_sources

log = logging.getLogger("bookbot")


async def run_bot(settings: Settings) -> None:
    sources = load_sources(settings.sources_file)
    db = Database(settings.db_path)
    await db.connect()
    fetcher = FeedFetcher(settings.user_agent)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    service = NewsService(settings, db, bot, sources, fetcher)
    await service.setup()

    dp = Dispatcher()
    dp["service"] = service
    dp.include_router(create_router(settings.admin_ids))

    async def on_startup(bot: Bot) -> None:
        me = await bot.get_me()
        log.info("Бот @%s запущено. Канал: %s. Модерація: %s",
                 me.username, settings.channel_id, "так" if settings.moderation else "ні")
        await _check_channel_rights(bot, service, me.id)
        for admin_id in settings.admin_ids:
            try:
                await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
            except TelegramAPIError:
                log.warning("Адмін %s ще не написав боту /start — меню команд з'явиться пізніше", admin_id)
        if not settings.admin_ids:
            log.warning("ADMIN_IDS не задано — команди керування недоступні нікому")
        service.start_background()

    async def on_shutdown() -> None:
        await service.stop()
        await fetcher.close()
        await db.close()

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


async def _check_channel_rights(bot: Bot, service: NewsService, bot_id: int) -> None:
    channel = service.settings.channel_id
    try:
        chat = await bot.get_chat(channel)
        member = await bot.get_chat_member(chat.id, bot_id)
    except TelegramAPIError as exc:
        msg = f"⚠️ Не бачу канал {channel}: {exc.message}. Додайте бота в канал адміністратором."
        log.error(msg)
        await service.notify_admins("channel", msg)
        return
    can_post = member.status == ChatMemberStatus.CREATOR or (
        member.status == ChatMemberStatus.ADMINISTRATOR and getattr(member, "can_post_messages", True) is not False
    )
    if not can_post:
        msg = (f"⚠️ Бот не має права публікувати в «{chat.title}». "
               "Зробіть його адміністратором з дозволом «Публікувати повідомлення».")
        log.error(msg)
        await service.notify_admins("channel", msg)
    else:
        log.info("Доступ до каналу «%s» є", chat.title)


# ---------------------------------------------------------------- CLI helpers

async def check_sources(settings: Settings) -> int:
    """Перевіряє кожне джерело з sources.yaml і друкує звіт. Повертає кількість проблемних."""
    cfg = load_sources(settings.sources_file)
    now = utcnow()
    problems = 0
    async with FeedFetcher(settings.user_agent) as fetcher:
        async def one(src):
            try:
                return src, await fetcher.resolve_feed(src.url), None
            except Exception as exc:  # noqa: BLE001
                return src, None, exc

        results = await asyncio.gather(*(one(s) for s in cfg.sources))

    for src, res, err in results:
        flag = "" if src.enabled else " (вимкнено в sources.yaml)"
        if err is not None:
            problems += 1
            print(f"❌ {src.name}{flag}\n   {src.url}\n   помилка: {err or err.__class__.__name__}\n")
            continue
        feed_url, parsed = res
        fresh = [e for e in parsed.entries if is_fresh(e.published, now, settings.max_age_days)]
        ok = [e for e in fresh if passes(f"{e.title}\n{e.summary}", src.include, src.exclude + cfg.exclude)]
        with_img = sum(1 for e in parsed.entries if e.image_url)
        print(f"✅ {src.name}{flag}")
        if feed_url != src.url:
            print(f"   стрічку знайдено тут: {feed_url}  ← варто вписати в sources.yaml")
        print(f"   записів: {len(parsed.entries)}, свіжих (≤{settings.max_age_days} дн): {len(fresh)}, "
              f"проходять фільтри: {len(ok)}, з картинкою: {with_img}")
        if parsed.entries:
            print(f"   найновіше: {parsed.entries[0].title[:100]}")
        print()
    total = len(results)
    print(f"Підсумок: працює {total - problems} з {total}")
    return problems


async def preview_posts(settings: Settings, limit: int = 5) -> None:
    """Збирає новини і друкує, як виглядатимуть пости (нічого не надсилає)."""
    cfg = load_sources(settings.sources_file)
    now = utcnow()
    collected = []
    async with FeedFetcher(settings.user_agent) as fetcher:
        for src in (s for s in cfg.sources if s.enabled):
            try:
                _, parsed = await fetcher.resolve_feed(src.url)
            except Exception as exc:  # noqa: BLE001
                print(f"[{src.name}] помилка: {exc}")
                continue
            for e in parsed.entries:
                text = f"{e.title}\n{e.summary}"
                if is_fresh(e.published, now, settings.max_age_days) and passes(text, src.include, src.exclude + cfg.exclude):
                    tags = list(dict.fromkeys(auto_tags(text, cfg.tag_rules) + src.tags))[:4]
                    collected.append((e, src, tags))
    collected.sort(key=lambda x: x[0].published or now, reverse=True)
    seen: set[str] = set()
    unique = []
    for row in collected:  # та сама новина з кількох джерел — показуємо один раз, як і бот
        key = title_key(row[0].title)
        if key not in seen:
            seen.add(key)
            unique.append(row)
    collected = unique
    if not collected:
        print("Свіжих новин, що проходять фільтри, не знайдено.")
        return
    for e, src, tags in collected[:limit]:
        post = build_post(title=e.title, link=e.link, summary=e.summary, source_name=src.name,
                          origin=e.origin, image_url=e.image_url, tags=tags, signature=settings.signature)
        plain = html.unescape(re.sub(r"<[^>]+>", "", post.text))
        print("─" * 60)
        if post.photo_url:
            print(f"[фото: {post.photo_url}]")
        print(plain)
    print("─" * 60)
    print(f"Показано {min(limit, len(collected))} з {len(collected)} новин")
