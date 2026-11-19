"""Оформлення поста для Telegram (HTML-розмітка)."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

CAPTION_LIMIT = 1024   # підпис під фото
MESSAGE_LIMIT = 4096   # звичайне повідомлення
SUMMARY_SOFT_LIMIT = 700  # довші анонси погано читаються в каналі
TITLE_LIMIT = 250


@dataclass
class Post:
    text: str
    link: str
    photo_url: str | None = None


def tg_len(text: str) -> int:
    """Telegram рахує довжину в UTF-16 одиницях після розбору розмітки."""
    return len(text.encode("utf-16-le")) // 2


def visible_len(html_text: str) -> int:
    return tg_len(html.unescape(re.sub(r"<[^>]+>", "", html_text)))


def truncate(text: str, limit: int) -> str:
    """Обрізає текст до limit символів, по можливості — на межі речення."""
    if limit <= 0:
        return ""
    if tg_len(text) <= limit:
        return text
    cut = text[: max(limit - 1, 0)]
    while cut and tg_len(cut) > limit - 1:
        cut = cut[:-1]
    # шукаємо кінець речення в другій половині
    best = -1
    for m in re.finditer(r"[.!?…](?=\s|$)", cut):
        if m.end() >= len(cut) * 0.5:
            best = m.end()
    if best > 0:
        return cut[:best].rstrip()
    space = cut.rfind(" ")
    if space > len(cut) * 0.5:
        cut = cut[:space]
    return cut.rstrip(" ,;:—-") + "…"


def _summary_paragraphs(summary: str, max_paragraphs: int = 2) -> str:
    paragraphs = [p.strip() for p in summary.split("\n\n") if p.strip()]
    return "\n\n".join(paragraphs[:max_paragraphs])


def build_post(
    *,
    title: str,
    link: str,
    summary: str,
    source_name: str,
    origin: str | None = None,
    image_url: str | None = None,
    tags: list[str] | None = None,
    signature: str = "",
) -> Post:
    def esc(text: str) -> str:
        return html.escape(text, quote=False)

    title = truncate(title.strip(), TITLE_LIMIT)
    header = f"📚 <b>{esc(title)}</b>"

    via = esc(origin) if origin and origin != source_name else esc(source_name)
    footer_lines = [f'👉 <a href="{html.escape(link, quote=True)}">Читати повністю</a> · <i>{via}</i>']
    if tags:
        footer_lines.append(" ".join(dict.fromkeys(tags)))  # без повторів, порядок зберігаємо
    if signature:
        footer_lines.append(esc(signature))
    footer = "\n".join(footer_lines)

    limit = CAPTION_LIMIT if image_url else MESSAGE_LIMIT
    summary = _summary_paragraphs(summary or "")
    # 4 — це два розриви "\n\n" між блоками
    budget = min(SUMMARY_SOFT_LIMIT, limit - visible_len(header) - visible_len(footer) - 4)
    body = truncate(summary, budget) if summary else ""

    parts = [header]
    if body:
        parts.append(esc(body))
    parts.append(footer)
    text = "\n\n".join(parts)

    # Страховка: якщо навіть без опису не влазимо (дуже довгий заголовок) — ріжемо заголовок
    if visible_len(text) > limit:
        short_title = truncate(title, max(40, limit - visible_len(footer) - 10))
        text = f"📚 <b>{esc(short_title)}</b>\n\n{footer}"

    return Post(text=text, link=link, photo_url=image_url)
