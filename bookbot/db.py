"""Сховище SQLite: джерела, новини, стан бота."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    url           TEXT NOT NULL UNIQUE,
    feed_url      TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    include_kw    TEXT NOT NULL DEFAULT '[]',
    exclude_kw    TEXT NOT NULL DEFAULT '[]',
    tags          TEXT NOT NULL DEFAULT '[]',
    origin        TEXT NOT NULL DEFAULT 'yaml',   -- yaml | admin
    etag          TEXT,
    last_modified TEXT,
    last_fetch_at TEXT,
    last_ok_at    TEXT,
    last_error    TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    uid           TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    title_key     TEXT NOT NULL,
    link          TEXT NOT NULL,
    summary       TEXT NOT NULL DEFAULT '',
    image_url     TEXT,
    origin        TEXT,
    tags          TEXT NOT NULL DEFAULT '[]',
    published_at  TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    status        TEXT NOT NULL,   -- review | queued | published | skipped | expired | failed
    attempts      INTEGER NOT NULL DEFAULT 0,
    posted_at     TEXT,
    message_id    INTEGER,
    review_msgs   TEXT NOT NULL DEFAULT '[]',
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status, published_at);
CREATE INDEX IF NOT EXISTS idx_items_title_key ON items(title_key, fetched_at);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass
class Source:
    id: int
    name: str
    url: str
    feed_url: str | None
    enabled: bool
    include: list[str]
    exclude: list[str]
    tags: list[str]
    origin: str
    etag: str | None
    last_modified: str | None
    last_fetch_at: datetime | None
    last_ok_at: datetime | None
    last_error: str | None

    @classmethod
    def from_row(cls, r: aiosqlite.Row) -> "Source":
        return cls(
            id=r["id"], name=r["name"], url=r["url"], feed_url=r["feed_url"],
            enabled=bool(r["enabled"]), include=json.loads(r["include_kw"]),
            exclude=json.loads(r["exclude_kw"]), tags=json.loads(r["tags"]),
            origin=r["origin"], etag=r["etag"], last_modified=r["last_modified"],
            last_fetch_at=from_iso(r["last_fetch_at"]), last_ok_at=from_iso(r["last_ok_at"]),
            last_error=r["last_error"],
        )


@dataclass
class Item:
    id: int
    source_id: int
    source_name: str
    title: str
    link: str
    summary: str
    image_url: str | None
    origin: str | None
    tags: list[str]
    published_at: datetime
    status: str
    attempts: int
    review_msgs: list[list[int]]

    @classmethod
    def from_row(cls, r: aiosqlite.Row) -> "Item":
        return cls(
            id=r["id"], source_id=r["source_id"], source_name=r["source_name"],
            title=r["title"], link=r["link"], summary=r["summary"], image_url=r["image_url"],
            origin=r["origin"], tags=json.loads(r["tags"]),
            published_at=from_iso(r["published_at"]), status=r["status"],
            attempts=r["attempts"], review_msgs=json.loads(r["review_msgs"]),
        )


_ITEM_SELECT = "SELECT i.*, s.name AS source_name FROM items i JOIN sources s ON s.id = i.source_id"


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("База даних не підключена")
        return self._db

    # ------------------------------------------------------------ sources

    async def sync_yaml_sources(self, sources) -> None:
        """Джерела з sources.yaml — головні: оновлюємо їх і вимикаємо видалені з файлу."""
        now = iso(utcnow())
        urls = []
        for s in sources:
            urls.append(s.url)
            await self.db.execute(
                """INSERT INTO sources (name, url, enabled, include_kw, exclude_kw, tags, origin, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'yaml', ?)
                   ON CONFLICT(url) DO UPDATE SET
                     name=excluded.name, enabled=excluded.enabled, include_kw=excluded.include_kw,
                     exclude_kw=excluded.exclude_kw, tags=excluded.tags, origin='yaml'""",
                (s.name, s.url, int(s.enabled), json.dumps(s.include, ensure_ascii=False),
                 json.dumps(s.exclude, ensure_ascii=False), json.dumps(s.tags, ensure_ascii=False), now),
            )
        placeholders = ",".join("?" * len(urls)) or "''"
        await self.db.execute(
            f"UPDATE sources SET enabled = 0 WHERE origin = 'yaml' AND url NOT IN ({placeholders})", urls
        )
        await self.db.commit()

    async def add_source(self, name: str, url: str, feed_url: str, tags: list[str]) -> int:
        cur = await self.db.execute(
            """INSERT INTO sources (name, url, feed_url, tags, origin, created_at)
               VALUES (?, ?, ?, ?, 'admin', ?)""",
            (name, url, feed_url, json.dumps(tags, ensure_ascii=False), iso(utcnow())),
        )
        await self.db.commit()
        return cur.lastrowid

    async def source_by_url(self, url: str) -> Source | None:
        async with self.db.execute("SELECT * FROM sources WHERE url = ? OR feed_url = ?", (url, url)) as cur:
            row = await cur.fetchone()
        return Source.from_row(row) if row else None

    async def remove_source(self, source_id: int) -> Source | None:
        src = await self.get_source(source_id)
        if not src:
            return None
        if src.origin == "yaml":
            # з yaml видаляти безглуздо — повернеться при перезапуску; просто вимикаємо
            await self.db.execute("UPDATE sources SET enabled = 0 WHERE id = ?", (source_id,))
        else:
            await self.db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        await self.db.commit()
        return src

    async def set_source_enabled(self, source_id: int, enabled: bool) -> Source | None:
        await self.db.execute("UPDATE sources SET enabled = ? WHERE id = ?", (int(enabled), source_id))
        await self.db.commit()
        return await self.get_source(source_id)

    async def get_source(self, source_id: int) -> Source | None:
        async with self.db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)) as cur:
            row = await cur.fetchone()
        return Source.from_row(row) if row else None

    async def list_sources(self, enabled_only: bool = False) -> list[Source]:
        sql = "SELECT * FROM sources" + (" WHERE enabled = 1" if enabled_only else "") + " ORDER BY id"
        async with self.db.execute(sql) as cur:
            return [Source.from_row(r) for r in await cur.fetchall()]

    async def record_fetch(self, source_id: int, *, ok: bool, error: str | None = None,
                           feed_url: str | None = None, etag: str | None = None,
                           last_modified: str | None = None) -> None:
        now = iso(utcnow())
        if ok:
            await self.db.execute(
                """UPDATE sources SET last_fetch_at = ?, last_ok_at = ?, last_error = NULL,
                   feed_url = COALESCE(?, feed_url), etag = ?, last_modified = ? WHERE id = ?""",
                (now, now, feed_url, etag, last_modified, source_id),
            )
        else:
            await self.db.execute(
                "UPDATE sources SET last_fetch_at = ?, last_error = ? WHERE id = ?",
                (now, (error or "помилка")[:500], source_id),
            )
        await self.db.commit()

    # ------------------------------------------------------------ items

    async def seen(self, uid: str, tkey: str, since: datetime) -> bool:
        async with self.db.execute(
            "SELECT 1 FROM items WHERE uid = ? OR (title_key = ? AND fetched_at >= ?) LIMIT 1",
            (uid, tkey, iso(since)),
        ) as cur:
            return await cur.fetchone() is not None

    async def add_item(self, *, source_id: int, uid: str, title: str, title_key: str, link: str,
                       summary: str, image_url: str | None, origin: str | None, tags: list[str],
                       published_at: datetime, status: str) -> int | None:
        cur = await self.db.execute(
            """INSERT OR IGNORE INTO items
               (source_id, uid, title, title_key, link, summary, image_url, origin, tags,
                published_at, fetched_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (source_id, uid, title, title_key, link, summary, image_url, origin,
             json.dumps(tags, ensure_ascii=False), iso(published_at), iso(utcnow()), status),
        )
        await self.db.commit()
        return cur.lastrowid if cur.rowcount else None

    async def get_item(self, item_id: int) -> Item | None:
        async with self.db.execute(_ITEM_SELECT + " WHERE i.id = ?", (item_id,)) as cur:
            row = await cur.fetchone()
        return Item.from_row(row) if row else None

    async def queued(self, limit: int = 1) -> list[Item]:
        async with self.db.execute(
            _ITEM_SELECT + " WHERE i.status = 'queued' ORDER BY i.published_at ASC, i.id ASC LIMIT ?",
            (limit,),
        ) as cur:
            return [Item.from_row(r) for r in await cur.fetchall()]

    async def in_review(self, limit: int = 20) -> list[Item]:
        async with self.db.execute(
            _ITEM_SELECT + " WHERE i.status = 'review' ORDER BY i.published_at ASC LIMIT ?", (limit,)
        ) as cur:
            return [Item.from_row(r) for r in await cur.fetchall()]

    async def transition(self, item_id: int, from_status: str | tuple[str, ...], to_status: str) -> bool:
        """Атомарна зміна статусу — захищає від подвійного натискання кнопок двома адмінами."""
        froms = (from_status,) if isinstance(from_status, str) else from_status
        cur = await self.db.execute(
            f"UPDATE items SET status = ? WHERE id = ? AND status IN ({','.join('?' * len(froms))})",
            (to_status, item_id, *froms),
        )
        await self.db.commit()
        return cur.rowcount == 1

    async def reset_stuck(self) -> None:
        """Після аварійної зупинки повертаємо в чергу пости, які «зависли» на публікації."""
        await self.db.execute("UPDATE items SET status = 'queued' WHERE status = 'publishing'")
        await self.db.commit()

    async def mark_published(self, item_id: int, message_id: int) -> None:
        await self.db.execute(
            "UPDATE items SET status = 'published', posted_at = ?, message_id = ?, error = NULL WHERE id = ?",
            (iso(utcnow()), message_id, item_id),
        )
        await self.db.commit()

    async def mark_attempt_failed(self, item_id: int, error: str, max_attempts: int = 3) -> str:
        await self.db.execute(
            "UPDATE items SET attempts = attempts + 1, error = ? WHERE id = ?", (error[:500], item_id)
        )
        await self.db.execute(
            "UPDATE items SET status = 'failed' WHERE id = ? AND attempts >= ?", (item_id, max_attempts)
        )
        await self.db.commit()
        item = await self.get_item(item_id)
        return item.status if item else "failed"

    async def set_review_msgs(self, item_id: int, msgs: list[list[int]]) -> None:
        await self.db.execute("UPDATE items SET review_msgs = ? WHERE id = ?", (json.dumps(msgs), item_id))
        await self.db.commit()

    async def expire_old(self, older_than: datetime) -> int:
        cur = await self.db.execute(
            "UPDATE items SET status = 'expired' WHERE status IN ('queued', 'review') AND published_at < ?",
            (iso(older_than),),
        )
        await self.db.commit()
        return cur.rowcount

    async def cleanup(self, older_than: datetime) -> int:
        cur = await self.db.execute("DELETE FROM items WHERE fetched_at < ?", (iso(older_than),))
        await self.db.commit()
        return cur.rowcount

    async def counts(self) -> dict[str, int]:
        async with self.db.execute("SELECT status, COUNT(*) AS n FROM items GROUP BY status") as cur:
            return {r["status"]: r["n"] for r in await cur.fetchall()}

    async def published_since(self, since: datetime) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) AS n FROM items WHERE status = 'published' AND posted_at >= ?", (iso(since),)
        ) as cur:
            return (await cur.fetchone())["n"]

    async def last_posted_at(self) -> datetime | None:
        async with self.db.execute(
            "SELECT MAX(posted_at) AS t FROM items WHERE status = 'published'"
        ) as cur:
            return from_iso((await cur.fetchone())["t"])

    # ------------------------------------------------------------ state

    async def get_state(self, key: str, default: str | None = None) -> str | None:
        async with self.db.execute("SELECT value FROM state WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row["value"] if row else default

    async def set_state(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.db.commit()

