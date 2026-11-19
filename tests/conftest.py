from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendPhoto

from bookbot.config import Settings
from bookbot.db import Database
from bookbot.feeds import FetchResult, parse_feed
from bookbot.sources import SourceConfig, SourcesFile


def rfc822(hours_ago: float) -> str:
    return format_datetime(datetime.now(timezone.utc) - timedelta(hours=hours_ago))


def wordpress_rss(items: list[dict]) -> bytes:
    body = []
    for it in items:
        body.append(f"""
    <item>
      <title>{it['title']}</title>
      <link>{it['link']}</link>
      <guid isPermaLink="false">{it.get('guid', it['link'])}</guid>
      <pubDate>{it.get('date', rfc822(2))}</pubDate>
      <description><![CDATA[{it.get('desc', '')}]]></description>
      {f'<content:encoded><![CDATA[{it["content"]}]]></content:encoded>' if it.get('content') else ''}
      {f'<media:content url="{it["media"]}" medium="image" />' if it.get('media') else ''}
    </item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:media="http://search.yahoo.com/mrss/">
  <channel>
    <title>Тестове медіа</title>
    <link>https://example.ua/</link>
    {''.join(body)}
  </channel>
</rss>""".encode("utf-8")


SAMPLE_ITEMS = [
    {
        "title": "Видавництво Старого Лева анонсувало новий роман Сергія Жадана",
        "link": "https://example.ua/2026/11/zhadan-novyi-roman/",
        "desc": "<p>Новинка вийде друком у грудні. Передпродаж уже відкрито.</p>"
                "<p>Запис <a href='#'>Видавництво…</a> спершу з'явився на <a href='#'>Тестове медіа</a>.</p>",
        "content": '<p><img src="https://example.ua/wp-content/uploads/cover.jpg" width="800"/></p><p>Повний текст…</p>',
        "date": rfc822(3),
    },
    {
        "title": "Оголошено шортлист премії «Книга року BBC»",
        "link": "https://example.ua/2026/11/shortlist/",
        "desc": "До короткого списку увійшли п'ять романів та три дитячі книжки. [&#8230;]",
        "media": "https://example.ua/img/bbc.jpg",
        "date": rfc822(5),
    },
    {
        "title": "Знижки до 70%: чорна п'ятниця в книгарнях",
        "link": "https://example.ua/2026/11/sale/",
        "desc": "Промокод на все. Розпродаж триває тиждень.",
        "date": rfc822(1),
    },
    {
        "title": "Старий архівний матеріал",
        "link": "https://example.ua/2020/01/old/",
        "desc": "Дуже давно.",
        "date": rfc822(24 * 30),
    },
]


class FakeFetcher:
    def __init__(self, feeds: dict[str, bytes]) -> None:
        self.feeds = feeds
        self.calls: list[str] = []

    async def get(self, url, etag=None, last_modified=None):
        self.calls.append(url)
        if url not in self.feeds:
            return FetchResult(status=404, url=url)
        return FetchResult(status=200, url=url, body=self.feeds[url], content_type="application/rss+xml")

    async def resolve_feed(self, url):
        res = await self.get(url)
        return url, parse_feed(res.body, url)

    async def close(self):
        pass


class FakeBot:
    def __init__(self, photo_fails: bool = False) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self.photo_fails = photo_fails
        self._ids = itertools.count(100)

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None, **kw):
        if self.photo_fails:
            raise TelegramBadRequest(SendPhoto(chat_id=chat_id, photo=photo), "Bad Request: wrong file identifier/HTTP URL specified")
        self.sent.append({"kind": "photo", "chat_id": chat_id, "photo": photo, "text": caption, "markup": reply_markup})
        return SimpleNamespace(message_id=next(self._ids))

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append({"kind": "text", "chat_id": chat_id, "text": text, "markup": reply_markup, **kw})
        return SimpleNamespace(message_id=next(self._ids))

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None, **kw):
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "markup": reply_markup})


def make_settings(tmp_path: Path, **over) -> Settings:
    base = dict(
        bot_token="x", channel_id="@test_channel", admin_ids=frozenset({111, 222}),
        db_path=tmp_path / "t.sqlite3", sources_file=tmp_path / "sources.yaml",
        fetch_interval_min=30, post_interval_min=20, max_age_days=3, max_new_per_source=5,
        max_posts_per_day=0, moderation=False, quiet_hours=None, timezone=ZoneInfo("Europe/Kyiv"),
        signature="", user_agent="test",
    )
    base.update(over)
    return Settings(**base)


FEED_URL = "https://example.ua/feed/"


def make_sources() -> SourcesFile:
    return SourcesFile(
        sources=[SourceConfig(name="Тестове медіа", url=FEED_URL, tags=["#Тест"])],
        exclude=["промокод", "розпродаж"],
        tag_rules={"#новинки": ["новинк", "вийде друком"], "#премії": ["премі", "шортлист"]},
    )


@pytest_asyncio.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.sqlite3")
    await d.connect()
    yield d
    await d.close()


@pytest.fixture
def feed_bytes():
    return wordpress_rss(SAMPLE_ITEMS)
