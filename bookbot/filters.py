"""Фільтри за ключовими словами, автоматичні хештеги, дедуплікація заголовків."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

_APOSTROPHES = str.maketrans({"’": "'", "ʼ": "'", "`": "'", "‘": "'"})


def _norm(text: str) -> str:
    return text.lower().translate(_APOSTROPHES).replace("ё", "е")


def title_key(title: str) -> str:
    """Ключ для пошуку однакових новин з різних джерел."""
    t = _norm(title)
    t = re.sub(r"[«»\"„“”]", "", t)
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:90]


def contains_any(text: str, keywords: list[str]) -> bool:
    if not keywords:
        return False
    hay = _norm(text)
    return any(_norm(k) in hay for k in keywords if k)


def passes(text: str, include: list[str], exclude: list[str]) -> bool:
    """include порожній → пропускаємо все; exclude має пріоритет."""
    if exclude and contains_any(text, exclude):
        return False
    if include and not contains_any(text, include):
        return False
    return True


def auto_tags(text: str, rules: dict[str, list[str]], limit: int = 3) -> list[str]:
    tags = [tag for tag, words in rules.items() if contains_any(text, words)]
    return tags[:limit]


def is_fresh(published: datetime | None, now: datetime, max_age_days: int) -> bool:
    if published is None:
        return True
    if published > now + timedelta(days=1):
        # дата в майбутньому — найчастіше помилка в стрічці; вважаємо свіжою
        return True
    return now - published <= timedelta(days=max_age_days)


def in_quiet_hours(local_hour: int, quiet: tuple[int, int] | None) -> bool:
    if not quiet:
        return False
    start, end = quiet
    if start < end:
        return start <= local_hour < end
    return local_hour >= start or local_hour < end
