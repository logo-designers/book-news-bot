"""Надсилання постів у Telegram з запасними варіантами."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions, Message

from .formatter import Post

log = logging.getLogger(__name__)


async def _with_retry(coro_factory, attempts: int = 3):
    for i in range(attempts):
        try:
            return await coro_factory()
        except TelegramRetryAfter as exc:  # флуд-контроль Telegram
            if i == attempts - 1:
                raise
            log.warning("Telegram просить зачекати %s с", exc.retry_after)
            await asyncio.sleep(exc.retry_after + 1)


async def send_post(bot: Bot, chat_id: int | str, post: Post,
                    reply_markup: InlineKeyboardMarkup | None = None) -> Message:
    """Пост з фото, якщо є картинка; якщо Telegram не зміг її завантажити — текстом із прев'ю посилання."""
    if post.photo_url:
        try:
            return await _with_retry(lambda: bot.send_photo(
                chat_id=chat_id, photo=post.photo_url, caption=post.text, reply_markup=reply_markup,
            ))
        except TelegramBadRequest as exc:
            log.info("Фото не підійшло (%s), надсилаю текстом: %s", exc.message, post.photo_url)

    return await _with_retry(lambda: bot.send_message(
        chat_id=chat_id,
        text=post.text,
        reply_markup=reply_markup,
        link_preview_options=LinkPreviewOptions(url=post.link, prefer_large_media=True),
    ))
