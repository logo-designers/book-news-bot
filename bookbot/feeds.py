"""Завантаження та розбір RSS/Atom-стрічок."""
from __future__ import annotations

import hashlib
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin

import aiohttp
import feedparser

log = logging.getLogger(__name__)

FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json", "text/xml")
COMMON_FEED_PATHS = ("feed/", "rss/", "rss.xml", "feed.xml", "atom.xml")


@dataclass
class FeedEntry:
    uid: str
    title: str
    link: str
    summary: str
    image_url: str | None
    published: datetime | None
    origin: str | None = None  # оригінальне видання (напр. для Google Новин)


@dataclass
class FetchResult:
    status: int
    url: str
    body: bytes = b""
    etag: str | None = None
    last_modified: str | None = None
    content_type: str = ""


@dataclass
class ParsedFeed:
    title: str
    entries: list[FeedEntry] = field(default_factory=list)
    is_feed: bool = True


# ---------------------------------------------------------------- HTML helpers

class _TextExtractor(HTMLParser):
    BLOCK = {"p", "br", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "tr"}
    SKIP = {"script", "style", "figcaption", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


_WP_TRAILERS = [
    re.compile(r"The post .{1,300}? appeared first on .{1,200}?\.?\s*$", re.S | re.I),
    re.compile(r"Запис .{1,300}? (?:спершу|вперше|спочатку|першим) з.яв\w+ на .{1,200}?\.?\s*$", re.S | re.I),
    re.compile(r"(?:Читати|Читайте) (?:далі|більше|повністю).{0,80}$", re.S | re.I),
]


def html_to_text(raw: str) -> str:
    if not raw:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        parser.close()
        text = "".join(parser.parts)
    except Exception:  # noqa: BLE001 — зламаний HTML не має валити бота
        text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text).replace("\xa0", " ")
    for pattern in _WP_TRAILERS:
        text = pattern.sub("", text)
    text = text.strip()
    cut = re.sub(r"\s*\[(?:…|\.\.\.|&#8230;)\]$", "", text)
    if cut != text:  # WordPress обрізав анонс і поставив «[…]»
        text = cut if re.search(r"[.!?…]$", cut) else cut + "…"
    # Схлопуємо пробіли, залишаючи абзаци
    paragraphs = [re.sub(r"[ \t\r\f\v]+", " ", p).strip() for p in re.split(r"\n\s*\n|\n", text)]
    return "\n\n".join(p for p in paragraphs if p)


class _ImgFinder(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.src: str | None = None

    def handle_starttag(self, tag, attrs):
        if self.src or tag != "img":
            return
        a = dict(attrs)
        if a.get("width") in {"1", "0"} or a.get("height") in {"1", "0"}:
            return
        src = a.get("data-src") or a.get("src") or ""
        if src.startswith("data:") or "gravatar.com" in src or src.endswith(".svg"):
            return
        if src:
            self.src = src


def first_image(raw_html: str) -> str | None:
    if not raw_html or "<img" not in raw_html:
        return None
    finder = _ImgFinder()
    try:
        finder.feed(raw_html)
    except Exception:  # noqa: BLE001
        return None
    return finder.src


class _FeedLinkFinder(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        a = {k: (v or "") for k, v in attrs}
        if "alternate" in a.get("rel", "").lower() and a.get("type", "").lower() in FEED_TYPES and a.get("href"):
            self.links.append(a["href"])


def discover_feed_links(page_html: str, base_url: str) -> list[str]:
    finder = _FeedLinkFinder()
    try:
        finder.feed(page_html)
    except Exception:  # noqa: BLE001
        return []
    links = [urljoin(base_url, href) for href in finder.links]
    # Коментарі нам не потрібні
    return [l for l in links if "comments" not in l.lower()]


# ---------------------------------------------------------------- parsing

def _entry_datetime(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime(*value[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _entry_image(entry, raw_html: str, base: str) -> str | None:
    candidates: list[str] = []
    for media in entry.get("media_content") or []:
        if media.get("url") and (media.get("medium") == "image" or "image" in media.get("type", "") or not media.get("type")):
            candidates.append(media["url"])
    for thumb in entry.get("media_thumbnail") or []:
        if thumb.get("url"):
            candidates.append(thumb["url"])
    for link in entry.get("links") or []:
        if link.get("rel") == "enclosure" and link.get("type", "").startswith("image/"):
            candidates.append(link.get("href", ""))
    for enc in entry.get("enclosures") or []:
        if enc.get("type", "").startswith("image/"):
            candidates.append(enc.get("href", ""))
    img = first_image(raw_html)
    if img:
        candidates.append(img)
    for url in candidates:
        if url and not url.startswith("data:"):
            full = urljoin(base, url)
            if full.startswith(("http://", "https://")):
                return full
    return None


def make_uid(*parts: str) -> str:
    key = next((p for p in parts if p), "")
    return hashlib.sha1(key.strip().encode("utf-8")).hexdigest()


def parse_feed(data: bytes, base_url: str) -> ParsedFeed:
    parsed = feedparser.parse(data)
    feed_title = html.unescape(parsed.feed.get("title", "")).strip()
    is_feed = bool(parsed.entries) or bool(parsed.get("version"))
    entries: list[FeedEntry] = []
    for e in parsed.entries:
        title = html_to_text(e.get("title", "")).replace("\n", " ").strip()
        link = (e.get("link") or "").strip()
        if not title or not link:
            continue
        link = urljoin(base_url, link)

        content_html = ""
        if e.get("content"):
            content_html = max((c.get("value", "") for c in e.content), key=len, default="")
        summary_html = e.get("summary", "") or ""
        # Для анонсу беремо короткий summary, а якщо його нема — повний текст
        summary = html_to_text(summary_html) or html_to_text(content_html)

        origin = None
        src = e.get("source")
        if isinstance(src, dict) and src.get("title"):
            origin = src["title"].strip()
            suffix = f" - {origin}"
            if title.endswith(suffix):
                title = title[: -len(suffix)].strip()

        # Google Новини дублюють заголовок у summary — такий опис не потрібен
        if summary and _same_text(summary, title, origin):
            summary = ""

        entries.append(
            FeedEntry(
                uid=make_uid(e.get("id", ""), link, title),
                title=title,
                link=link,
                summary=summary,
                image_url=_entry_image(e, content_html + summary_html, link),
                published=_entry_datetime(e),
                origin=origin,
            )
        )
    return ParsedFeed(title=feed_title, entries=entries, is_feed=is_feed)


def _same_text(summary: str, title: str, origin: str | None) -> bool:
    def norm(s: str) -> str:
        return re.sub(r"\W+", " ", s.lower()).strip()

    s, t = norm(summary), norm(title)
    if origin:
        s = s.replace(norm(origin), "").strip()
    return s == t or bool(t and s.startswith(t) and len(s) - len(t) < 5)


# ---------------------------------------------------------------- network

class FeedFetcher:
    def __init__(self, user_agent: str, timeout: float = 25) -> None:
        self._ua = user_agent
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "FeedFetcher":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={
                    "User-Agent": self._ua,
                    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/html;q=0.8, */*;q=0.5",
                    "Accept-Language": "uk,en;q=0.7",
                },
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def get(self, url: str, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
        await self.start()
        assert self._session is not None
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        async with self._session.get(url, headers=headers, allow_redirects=True) as resp:
            body = b"" if resp.status == 304 else await resp.read()
            return FetchResult(
                status=resp.status,
                url=str(resp.url),
                body=body,
                etag=resp.headers.get("ETag"),
                last_modified=resp.headers.get("Last-Modified"),
                content_type=resp.headers.get("Content-Type", ""),
            )

    async def resolve_feed(self, url: str) -> tuple[str, ParsedFeed]:
        """Повертає (адреса_стрічки, розібрана_стрічка). Якщо дали адресу сайту — шукає RSS на сторінці."""
        res = await self.get(url)
        if res.status >= 400:
            raise FeedError(f"HTTP {res.status}")
        parsed = parse_feed(res.body, res.url)
        if parsed.entries:
            return res.url, parsed

        candidates: list[str] = []
        if "html" in res.content_type or b"<html" in res.body[:2000].lower():
            candidates += discover_feed_links(res.body.decode("utf-8", "replace"), res.url)
        base = res.url if res.url.endswith("/") else res.url + "/"
        candidates += [urljoin(base, p) for p in COMMON_FEED_PATHS]

        tried: set[str] = {res.url}
        for cand in candidates:
            if cand in tried:
                continue
            tried.add(cand)
            try:
                r = await self.get(cand)
            except (aiohttp.ClientError, TimeoutError):
                continue
            if r.status >= 400:
                continue
            p = parse_feed(r.body, r.url)
            if p.entries:
                return r.url, p
        raise FeedError("RSS-стрічку не знайдено за цією адресою")


class FeedError(RuntimeError):
    pass
