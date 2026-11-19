"""Прогін команд через справжній Dispatcher aiogram з підміненим HTTP-сеансом Telegram."""
from __future__ import annotations

import itertools
from datetime import datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import (
    AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText, SendMessage, SendPhoto, TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bookbot.handlers import create_router
from bookbot.keyboards import ModCb
from bookbot.service import NewsService

from .conftest import FEED_URL, FakeFetcher, make_settings, make_sources

pytestmark = pytest.mark.asyncio
ADMIN, STRANGER = 111, 999


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod] = []
        self._ids = itertools.count(1000)

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, (SendMessage, SendPhoto, EditMessageText)):
            chat_id = getattr(method, "chat_id", None) or 0
            chat = Chat(id=chat_id if isinstance(chat_id, int) else -100, type="private")
            return Message(message_id=next(self._ids), date=datetime.now(), chat=chat,
                           text=getattr(method, "text", None) or getattr(method, "caption", None)).as_(bot)
        if isinstance(method, (AnswerCallbackQuery, EditMessageReplyMarkup)):
            return True
        raise AssertionError(f"Неочікуваний запит: {type(method).__name__}")

    async def close(self):
        pass

    async def stream_content(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError

    def texts(self) -> list[str]:
        return [getattr(r, "text", None) or getattr(r, "caption", "") or "" for r in self.requests]


def _msg(text: str, user_id: int, update_id=itertools.count(1)) -> Update:
    user = User(id=user_id, is_bot=False, first_name="T")
    return Update(update_id=next(update_id), message=Message(
        message_id=next(update_id), date=datetime.now(), chat=Chat(id=user_id, type="private"),
        from_user=user, text=text,
        entities=[{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if text.startswith("/") else None,
    ))


@pytest.fixture
async def env(tmp_path, db, feed_bytes):
    session = RecordingSession()
    bot = Bot("42:TEST", session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    settings = make_settings(tmp_path, moderation=True, admin_ids=frozenset({ADMIN}))
    svc = NewsService(settings, db, bot, make_sources(), FakeFetcher({FEED_URL: feed_bytes}))
    await svc.setup()
    dp = Dispatcher()
    dp["service"] = svc
    dp.include_router(create_router(settings.admin_ids))
    return dp, bot, session, svc


async def test_admin_commands(env):
    dp, bot, session, svc = env
    await dp.feed_update(bot, _msg("/help", ADMIN))
    assert "Бот книжкових новин" in session.texts()[-1]

    await dp.feed_update(bot, _msg("/fetch", ADMIN))
    assert any("Нових новин: <b>2</b>" in t for t in session.texts())
    # у режимі модерації адмін отримав превʼю з кнопками
    assert sum(1 for r in session.requests if isinstance(r, SendPhoto) and r.reply_markup) == 2

    await dp.feed_update(bot, _msg("/status", ADMIN))
    status = session.texts()[-1]
    assert "На модерації: 2" in status and "Джерела: 1 активних" in status

    await dp.feed_update(bot, _msg("/sources", ADMIN))
    assert "🟢" in session.texts()[-1]

    await dp.feed_update(bot, _msg("/toggle 1", ADMIN))
    assert "Вимкнено" in session.texts()[-1]
    await dp.feed_update(bot, _msg("/toggle abc", ADMIN))
    assert "Вкажіть id" in session.texts()[-1]

    await dp.feed_update(bot, _msg("/pause", ADMIN))
    assert await svc.db.get_state("paused") == "1"
    await dp.feed_update(bot, _msg("/resume", ADMIN))
    assert await svc.db.get_state("paused") == "0"

    await dp.feed_update(bot, _msg("/queue", ADMIN))
    assert "Черга порожня" in session.texts()[-1]

    await dp.feed_update(bot, _msg("/whatever", ADMIN))
    assert "Не знаю такої команди" in session.texts()[-1]


async def test_moderation_button(env):
    dp, bot, session, svc = env
    await svc.fetch_all()
    item = (await svc.db.in_review())[0]
    cb = Update(update_id=500, callback_query=CallbackQuery(
        id="cb1", from_user=User(id=ADMIN, is_bot=False, first_name="A"), chat_instance="x",
        data=ModCb(action="pub", item_id=item.id).pack(),
    ))
    await dp.feed_update(bot, cb)
    channel_posts = [r for r in session.requests if isinstance(r, (SendPhoto, SendMessage)) and r.chat_id == "@test_channel"]
    assert len(channel_posts) == 1
    answer = [r for r in session.requests if isinstance(r, AnswerCallbackQuery)][-1]
    assert answer.text == "Опубліковано в каналі"
    assert (await svc.db.get_item(item.id)).status == "published"


async def test_stranger_cannot_manage(env):
    dp, bot, session, svc = env
    await dp.feed_update(bot, _msg("/start", STRANGER))
    assert "Ваш Telegram ID: <code>999</code>" in session.texts()[-1]
    n = len(session.requests)
    await dp.feed_update(bot, _msg("/fetch", STRANGER))
    await dp.feed_update(bot, _msg("/pause", STRANGER))
    assert len(session.requests) == n  # чужим нічого не відповідаємо
    assert await svc.db.get_state("paused") is None

    await svc.fetch_all()
    item = (await svc.db.in_review())[0]
    cb = Update(update_id=501, callback_query=CallbackQuery(
        id="cb2", from_user=User(id=STRANGER, is_bot=False, first_name="S"), chat_instance="x",
        data=ModCb(action="pub", item_id=item.id).pack(),
    ))
    await dp.feed_update(bot, cb)
    assert "лише адміністратори" in session.requests[-1].text
    assert (await svc.db.get_item(item.id)).status == "review"
