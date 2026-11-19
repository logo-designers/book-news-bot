from datetime import timedelta

import pytest

from bookbot.db import utcnow
from bookbot.service import NewsService

from .conftest import FEED_URL, SAMPLE_ITEMS, FakeBot, FakeFetcher, make_settings, make_sources, wordpress_rss

pytestmark = pytest.mark.asyncio


async def make_service(tmp_path, db, feed_bytes, bot=None, **settings_over):
    bot = bot or FakeBot()
    svc = NewsService(make_settings(tmp_path, **settings_over), db, bot, make_sources(),
                      FakeFetcher({FEED_URL: feed_bytes}))
    await svc.setup()
    return svc, bot


async def test_fetch_filters_and_dedups(tmp_path, db, feed_bytes):
    svc, _ = await make_service(tmp_path, db, feed_bytes)
    report = await svc.fetch_all()
    # 4 записи: 1 відсіяно стоп-словами, 1 застарілий → 2 нові
    assert report.total_new == 2
    assert (await db.counts()) == {"queued": 2}

    # повторний збір нічого не додає
    assert (await svc.fetch_all()).total_new == 0

    # та сама новина з іншим посиланням (інше джерело/дубль) теж не проходить
    dup = wordpress_rss([{**SAMPLE_ITEMS[1], "link": "https://other.ua/x", "guid": "other-1"}])
    svc.fetcher.feeds[FEED_URL] = dup
    assert (await svc.fetch_all()).total_new == 0


async def test_queue_order_tags_and_publish(tmp_path, db, feed_bytes):
    svc, bot = await make_service(tmp_path, db, feed_bytes)
    await svc.fetch_all()

    r1 = await svc.publish_next()
    assert r1.ok
    # першою йде старіша новина (хронологічний порядок)
    assert "шортлист" in r1.item.title
    sent = bot.sent[-1]
    assert sent["chat_id"] == "@test_channel" and sent["kind"] == "photo"
    assert "#премії" in sent["text"] and "#Тест" in sent["text"]

    # інтервал між постами ще не минув
    assert not await svc.is_publish_due()

    r2 = await svc.publish_next()
    assert r2.ok and "Жадана" in r2.item.title
    assert "#новинки" in bot.sent[-1]["text"]
    assert (await svc.publish_next()).reason == "черга порожня"
    assert (await db.counts()) == {"published": 2}


async def test_photo_failure_falls_back_to_text(tmp_path, db, feed_bytes):
    svc, bot = await make_service(tmp_path, db, feed_bytes, bot=FakeBot(photo_fails=True))
    await svc.fetch_all()
    r = await svc.publish_next()
    assert r.ok
    assert bot.sent[-1]["kind"] == "text"
    assert bot.sent[-1]["link_preview_options"].url == r.item.link


async def test_pause_quiet_hours_and_daily_limit(tmp_path, db, feed_bytes):
    svc, bot = await make_service(tmp_path, db, feed_bytes)
    await svc.fetch_all()

    await db.set_state("paused", "1")
    assert "паузі" in (await svc.publish_next()).reason
    # примусова публікація (/post) ігнорує паузу
    assert (await svc.publish_next(force=True)).ok
    await db.set_state("paused", "0")

    hour = utcnow().astimezone(svc.settings.timezone).hour
    quiet = NewsService(make_settings(tmp_path, quiet_hours=(hour, (hour + 1) % 24)), db, bot,
                        make_sources(), svc.fetcher)
    assert "тихі години" in (await quiet.publish_next()).reason

    limited = NewsService(make_settings(tmp_path, max_posts_per_day=1), db, bot, make_sources(), svc.fetcher)
    assert "ліміту" in (await limited.publish_next()).reason


async def test_moderation_flow(tmp_path, db, feed_bytes):
    svc, bot = await make_service(tmp_path, db, feed_bytes, moderation=True)
    await svc.fetch_all()
    assert (await db.counts()) == {"review": 2}
    # кожну новину надіслано обом адмінам з кнопками
    previews = [m for m in bot.sent if m["chat_id"] in (111, 222)]
    assert len(previews) == 4 and all(m["markup"] is not None for m in previews)
    # у канал поки нічого не пішло
    assert not [m for m in bot.sent if m["chat_id"] == "@test_channel"]
    assert (await svc.publish_next()).reason == "черга порожня"

    items = await db.in_review()
    ok, _ = await svc.moderate(items[0].id, "pub")
    assert ok and bot.sent[-1]["chat_id"] == "@test_channel"
    # кнопки в обох адмінів замінено на статус
    assert len(bot.edited) == 2
    assert bot.edited[0]["markup"].inline_keyboard[0][0].text == "✅ Опубліковано"

    # другий адмін натискає ту саму кнопку — подвійної публікації немає
    ok, msg = await svc.moderate(items[0].id, "pub")
    assert not ok and "вже опрацьовано" in msg

    ok, _ = await svc.moderate(items[1].id, "skip")
    assert ok
    assert (await db.counts()) == {"published": 1, "skipped": 1}


async def test_expiry_of_stale_queue(tmp_path, db, feed_bytes):
    svc, _ = await make_service(tmp_path, db, feed_bytes)
    await svc.fetch_all()
    expired = await db.expire_old(utcnow() + timedelta(days=1))
    assert expired == 2
    assert (await svc.publish_next()).reason == "черга порожня"


async def test_broken_source_reported(tmp_path, db, feed_bytes):
    svc, _ = await make_service(tmp_path, db, feed_bytes)
    svc.fetcher.feeds.clear()  # 404
    report = await svc.fetch_all()
    assert report.sources[0].error == "HTTP 404"
    src = (await db.list_sources())[0]
    assert src.last_error == "HTTP 404"


async def test_yaml_sync_disables_removed(tmp_path, db, feed_bytes):
    svc, _ = await make_service(tmp_path, db, feed_bytes)
    svc.sources.sources.clear()
    await svc.setup()
    assert not (await db.list_sources())[0].enabled


async def test_add_source_via_admin(tmp_path, db, feed_bytes):
    svc, _ = await make_service(tmp_path, db, feed_bytes)
    svc.fetcher.feeds["https://new.ua/feed/"] = feed_bytes
    src, info = await svc.add_source("new.ua/feed/", "Нове")
    assert src and src.name == "Нове" and "4" in info
    again, info = await svc.add_source("https://new.ua/feed/")
    assert again is None and "вже є" in info


async def test_page_image_used_when_feed_has_none(tmp_path, db):
    feed = wordpress_rss([{"title": "Нова книжка без картинки", "link": "https://example.ua/a/", "desc": "Анонс."}])
    svc, bot = await make_service(tmp_path, db, feed)
    svc.fetcher.page_images["https://example.ua/a/"] = "https://example.ua/og.jpg"
    await svc.fetch_all()
    assert (await svc.publish_next()).ok
    assert bot.sent[-1]["kind"] == "photo" and bot.sent[-1]["photo"] == "https://example.ua/og.jpg"


async def test_page_images_can_be_disabled(tmp_path, db):
    feed = wordpress_rss([{"title": "Нова книжка", "link": "https://example.ua/b/", "desc": "Анонс."}])
    svc, bot = await make_service(tmp_path, db, feed, page_images=False)
    svc.fetcher.page_images["https://example.ua/b/"] = "https://example.ua/og.jpg"
    await svc.fetch_all()
    assert (await svc.publish_next()).ok
    assert bot.sent[-1]["kind"] == "text"


async def test_run_once(tmp_path, feed_bytes, monkeypatch):
    from bookbot import app
    (tmp_path / "sources.yaml").write_text(f"sources:\n  - name: Тест\n    url: {FEED_URL}\n", encoding="utf-8")
    bot = FakeBot()
    monkeypatch.setattr(app, "Bot", lambda *a, **kw: bot)
    monkeypatch.setattr(app, "FeedFetcher", lambda *a, **kw: FakeFetcher({FEED_URL: feed_bytes}))
    settings = make_settings(tmp_path, moderation=True)
    assert await app.run_once(settings, max_posts=3) == 1  # інтервал між постами дотримано
    assert bot.sent[-1]["chat_id"] == "@test_channel"
    # другий запуск одразу після першого: новин не дублює, пост ще не час
    assert await app.run_once(settings, max_posts=3) == 0
    assert len(bot.sent) == 1
    wal = tmp_path / "t.sqlite3-wal"
    assert (tmp_path / "t.sqlite3").exists()
    assert not wal.exists() or wal.stat().st_size == 0  # база — один файл, її можна зберегти в git
