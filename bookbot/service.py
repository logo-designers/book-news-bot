"""Логіка бота: збір новин, черга, модерація, публікація за розкладом."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import aiohttp
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
)

from .config import Settings
from .db import Database, Item, Source, utcnow
from .feeds import FeedError, FeedFetcher, parse_feed
from .filters import auto_tags, in_quiet_hours, is_fresh, passes, title_key
from .formatter import Post, build_post
from .keyboards import review_keyboard, status_keyboard
from .publisher import send_post
from .sources import SourcesFile

log = logging.getLogger(__name__)

DEDUP_WINDOW = timedelta(days=14)   # однакові заголовки з різних джерел
KEEP_HISTORY = timedelta(days=120)  # скільки тримати історію для дедуплікації
MAX_TAGS = 4


@dataclass
class SourceReport:
    name: str
    new: int = 0
    error: str | None = None


@dataclass
class FetchReport:
    sources: list[SourceReport] = field(default_factory=list)

    @property
    def total_new(self) -> int:
        return sum(s.new for s in self.sources)


@dataclass
class PublishResult:
    item: Item | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.item is not None and not self.reason


class NewsService:
    def __init__(self, settings: Settings, db: Database, bot, sources: SourcesFile,
                 fetcher: FeedFetcher, tick_seconds: int = 60) -> None:
        self.settings = settings
        self.db = db
        self.bot = bot
        self.sources = sources
        self.fetcher = fetcher
        self.tick_seconds = tick_seconds
        self._publish_lock = asyncio.Lock()
        self._fetch_lock = asyncio.Lock()
        self._fetch_sem = asyncio.Semaphore(4)
        self._notified: dict[str, datetime] = {}
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------ lifecycle

    async def setup(self) -> None:
        await self.db.sync_yaml_sources(self.sources.sources)
        await self.db.reset_stuck()

    def start_background(self) -> None:
        self._tasks = [
            asyncio.create_task(self._fetch_loop(), name="fetch-loop"),
            asyncio.create_task(self._publish_loop(), name="publish-loop"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def _fetch_loop(self) -> None:
        while True:
            try:
                report = await self.fetch_all()
                log.info("Збір завершено: нових %s", report.total_new)
            except Exception:  # noqa: BLE001
                log.exception("Помилка під час збору новин")
            await asyncio.sleep(self.settings.fetch_interval_min * 60)

    async def _publish_loop(self) -> None:
        await asyncio.sleep(min(15, self.tick_seconds))
        while True:
            try:
                if await self.is_publish_due():
                    result = await self.publish_next()
                    if result.reason and result.reason != "черга порожня":
                        log.debug("Публікацію відкладено: %s", result.reason)
            except Exception:  # noqa: BLE001
                log.exception("Помилка під час публікації")
            await asyncio.sleep(self.tick_seconds)

    # ------------------------------------------------------------ fetching

    async def fetch_all(self) -> FetchReport:
        async with self._fetch_lock:
            sources = await self.db.list_sources(enabled_only=True)
            reports = await asyncio.gather(*(self._fetch_source_guarded(s) for s in sources))
            now = utcnow()
            expired = await self.db.expire_old(now - timedelta(days=self.settings.max_age_days))
            if expired:
                log.info("Застарілих новин знято з черги: %s", expired)
            await self.db.cleanup(now - KEEP_HISTORY)
            return FetchReport(sources=list(reports))

    async def _fetch_source_guarded(self, src: Source) -> SourceReport:
        async with self._fetch_sem:
            try:
                new = await self._fetch_source(src)
                return SourceReport(src.name, new=new)
            except (FeedError, aiohttp.ClientError, asyncio.TimeoutError, UnicodeError) as exc:
                msg = str(exc) or exc.__class__.__name__
                log.warning("Джерело «%s»: %s", src.name, msg)
                await self.db.record_fetch(src.id, ok=False, error=msg)
                return SourceReport(src.name, error=msg)
            except Exception as exc:  # noqa: BLE001
                log.exception("Джерело «%s»: неочікувана помилка", src.name)
                await self.db.record_fetch(src.id, ok=False, error=repr(exc))
                return SourceReport(src.name, error=repr(exc))

    async def _fetch_source(self, src: Source) -> int:
        url = src.feed_url or src.url
        res = await self.fetcher.get(url, src.etag, src.last_modified)
        if res.status == 304:
            await self.db.record_fetch(src.id, ok=True, etag=src.etag, last_modified=src.last_modified)
            return 0
        if res.status >= 400:
            raise FeedError(f"HTTP {res.status}")

        parsed = parse_feed(res.body, res.url)
        feed_url, etag, last_mod = res.url, res.etag, res.last_modified
        if not parsed.entries and not parsed.is_feed:
            # Дали адресу сайту, а не стрічки — шукаємо RSS автоматично
            feed_url, parsed = await self.fetcher.resolve_feed(src.url)
            etag = last_mod = None
            log.info("«%s»: знайдено стрічку %s", src.name, feed_url)

        now = utcnow()
        entries = sorted(parsed.entries, key=lambda e: e.published or now, reverse=True)
        added = 0
        for entry in entries:
            if added >= self.settings.max_new_per_source:
                break
            if await self._accept(src, entry, now):
                added += 1
        await self.db.record_fetch(src.id, ok=True, feed_url=feed_url, etag=etag, last_modified=last_mod)
        return added

    async def _accept(self, src: Source, entry, now: datetime) -> bool:
        if not is_fresh(entry.published, now, self.settings.max_age_days):
            return False
        text = f"{entry.title}\n{entry.summary}"
        if not passes(text, src.include, src.exclude + self.sources.exclude):
            return False
        tkey = title_key(entry.title)
        if await self.db.seen(entry.uid, tkey, now - DEDUP_WINDOW):
            return False

        image_url = entry.image_url
        if not image_url and self.settings.page_images:
            # у стрічці картинки немає — беремо головне зображення зі сторінки статті
            image_url = await self.fetcher.page_image(entry.link)

        tags = list(dict.fromkeys(auto_tags(text, self.sources.tag_rules) + src.tags))[:MAX_TAGS]
        status = "review" if self.settings.moderation else "queued"
        item_id = await self.db.add_item(
            source_id=src.id, uid=entry.uid, title=entry.title, title_key=tkey, link=entry.link,
            summary=entry.summary, image_url=image_url, origin=entry.origin, tags=tags,
            published_at=entry.published or now, status=status,
        )
        if item_id is None:
            return False
        if status == "review":
            item = await self.db.get_item(item_id)
            if item:
                await self.send_for_review(item)
        return True

    # ------------------------------------------------------------ formatting

    def build(self, item: Item) -> Post:
        return build_post(
            title=item.title, link=item.link, summary=item.summary, source_name=item.source_name,
            origin=item.origin, image_url=item.image_url, tags=item.tags,
            signature=self.settings.signature,
        )

    # ------------------------------------------------------------ moderation

    async def send_for_review(self, item: Item) -> None:
        post = self.build(item)
        sent: list[list[int]] = []
        for admin_id in self.settings.admin_ids:
            try:
                msg = await send_post(self.bot, admin_id, post, reply_markup=review_keyboard(item.id))
                sent.append([admin_id, msg.message_id])
            except TelegramForbiddenError:
                log.warning("Адмін %s не запустив бота (/start) — не можу надіслати на модерацію", admin_id)
            except TelegramAPIError as exc:
                log.warning("Не вдалося надіслати на модерацію адміну %s: %s", admin_id, exc)
        await self.db.set_review_msgs(item.id, sent)

    async def moderate(self, item_id: int, action: str) -> tuple[bool, str]:
        """Обробляє натискання кнопки модерації. Повертає (успіх, текст для адміна)."""
        if action == "skip":
            if not await self.db.transition(item_id, "review", "skipped"):
                return False, "Цю новину вже опрацьовано"
            await self._mark_review_messages(item_id, "❌ Пропущено")
            return True, "Пропущено"
        if action == "queue":
            if not await self.db.transition(item_id, "review", "queued"):
                return False, "Цю новину вже опрацьовано"
            await self._mark_review_messages(item_id, "🕒 У черзі")
            return True, "Додано в чергу"
        if action == "pub":
            if not await self.db.transition(item_id, "review", "publishing"):
                return False, "Цю новину вже опрацьовано"
            item = await self.db.get_item(item_id)
            ok, err = await self._publish_claimed(item)
            label = "✅ Опубліковано" if ok else "⚠️ Помилка — повернуто в чергу"
            await self._mark_review_messages(item_id, label)
            return ok, "Опубліковано в каналі" if ok else f"Не вдалося: {err}"
        return False, ""

    async def _mark_review_messages(self, item_id: int, label: str) -> None:
        item = await self.db.get_item(item_id)
        if not item:
            return
        for chat_id, message_id in item.review_msgs:
            try:
                await self.bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=message_id, reply_markup=status_keyboard(item_id, label)
                )
            except TelegramAPIError:
                pass  # повідомлення могли видалити — не страшно

    # ------------------------------------------------------------ publishing

    async def is_publish_due(self) -> bool:
        last = await self.db.last_posted_at()
        if last is None:
            return True
        return utcnow() - last >= timedelta(minutes=self.settings.post_interval_min)

    async def blocked_reason(self) -> str:
        if await self.db.get_state("paused") == "1":
            return "публікації на паузі (/resume)"
        local = utcnow().astimezone(self.settings.timezone)
        if in_quiet_hours(local.hour, self.settings.quiet_hours):
            s, e = self.settings.quiet_hours
            return f"тихі години {s:02d}:00–{e:02d}:00"
        limit = self.settings.max_posts_per_day
        if limit:
            day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
            if await self.db.published_since(day_start) >= limit:
                return f"досягнуто ліміту {limit} постів на день"
        return ""

    async def publish_next(self, force: bool = False) -> PublishResult:
        async with self._publish_lock:
            if not force:
                reason = await self.blocked_reason()
                if reason:
                    return PublishResult(reason=reason)
            for _ in range(5):  # якщо пост не вдався — пробуємо наступний
                queued = await self.db.queued(limit=1)
                if not queued:
                    return PublishResult(reason="черга порожня")
                item = queued[0]
                if not await self.db.transition(item.id, "queued", "publishing"):
                    continue
                ok, err = await self._publish_claimed(item, locked=True)
                if ok:
                    return PublishResult(item=item)
                if err == "channel":
                    return PublishResult(item=item, reason="немає доступу до каналу")
            return PublishResult(reason="не вдалося опублікувати кілька новин поспіль")

    async def _publish_claimed(self, item: Item, locked: bool = False) -> tuple[bool, str]:
        if not locked:
            async with self._publish_lock:
                return await self._publish_claimed(item, locked=True)
        post = self.build(item)
        try:
            msg = await send_post(self.bot, self.settings.channel_id, post)
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            text = str(exc).lower()
            if isinstance(exc, TelegramForbiddenError) or "chat not found" in text or "not enough rights" in text:
                await self.db.transition(item.id, "publishing", "queued")
                await self.notify_admins(
                    "channel",
                    "⚠️ Не можу публікувати в канал "
                    f"{self.settings.channel_id}: {exc.message}\n"
                    "Перевірте, що бот доданий адміністратором каналу з правом публікації.",
                )
                return False, "channel"
            return await self._fail(item, exc)
        except TelegramAPIError as exc:
            return await self._fail(item, exc)
        await self.db.mark_published(item.id, msg.message_id)
        log.info("Опубліковано: %s", item.title)
        return True, ""

    async def _fail(self, item: Item, exc: Exception) -> tuple[bool, str]:
        log.warning("Не вдалося опублікувати «%s»: %s", item.title, exc)
        status = await self.db.mark_attempt_failed(item.id, str(exc))
        if status != "failed":
            await self.db.transition(item.id, "publishing", "queued")
        return False, str(exc)

    async def notify_admins(self, key: str, text: str, every: timedelta = timedelta(hours=6)) -> None:
        now = utcnow()
        last = self._notified.get(key)
        if last and now - last < every:
            return
        self._notified[key] = now
        for admin_id in self.settings.admin_ids:
            try:
                await self.bot.send_message(admin_id, text)
            except TelegramAPIError:
                pass

    # ------------------------------------------------------------ admin helpers

    async def add_source(self, url: str, name: str | None = None) -> tuple[Source | None, str]:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        if await self.db.source_by_url(url):
            return None, "Таке джерело вже є"
        try:
            feed_url, parsed = await self.fetcher.resolve_feed(url)
        except (FeedError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return None, f"Не вдалося прочитати стрічку: {exc or exc.__class__.__name__}"
        if await self.db.source_by_url(feed_url):
            return None, "Таке джерело вже є"
        name = name or parsed.title or url
        source_id = await self.db.add_source(name=name, url=url, feed_url=feed_url, tags=[])
        src = await self.db.get_source(source_id)
        return src, f"Знайдено записів у стрічці: {len(parsed.entries)}"
