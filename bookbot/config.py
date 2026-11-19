"""Налаштування бота: читаються зі змінних середовища (або файлу .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    bot_token: str
    channel_id: str | int
    admin_ids: frozenset[int]
    db_path: Path
    sources_file: Path
    fetch_interval_min: int
    post_interval_min: int
    max_age_days: int
    max_new_per_source: int
    max_posts_per_day: int
    moderation: bool
    quiet_hours: tuple[int, int] | None
    timezone: ZoneInfo
    signature: str
    user_agent: str


def _int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} має бути цілим числом, отримано: {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} не може бути меншим за {minimum}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "так"}


def parse_quiet_hours(raw: str) -> tuple[int, int] | None:
    """'23-8' -> (23, 8). Порожній рядок або 'off' вимикає тихі години."""
    raw = raw.strip().lower()
    if not raw or raw in {"off", "none", "0", "ні"}:
        return None
    try:
        start, end = (int(part) for part in raw.split("-", 1))
    except ValueError as exc:
        raise ConfigError(f"QUIET_HOURS має формат '23-8', отримано: {raw!r}") from exc
    if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
        raise ConfigError("QUIET_HOURS: години мають бути 0–23 і не однакові")
    return start, end


def parse_channel(raw: str) -> str | int:
    raw = raw.strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    if raw and not raw.startswith("@"):
        raw = "@" + raw
    return raw


def load_settings(require_token: bool = True) -> Settings:
    load_dotenv()

    token = os.getenv("BOT_TOKEN", "").strip()
    channel = parse_channel(os.getenv("CHANNEL_ID", ""))
    if require_token:
        if not token:
            raise ConfigError("Не задано BOT_TOKEN (отримайте його в @BotFather)")
        if not channel:
            raise ConfigError("Не задано CHANNEL_ID (наприклад, @my_books_channel)")

    admin_raw = os.getenv("ADMIN_IDS", "")
    try:
        admins = frozenset(int(x) for x in admin_raw.replace(";", ",").split(",") if x.strip())
    except ValueError as exc:
        raise ConfigError("ADMIN_IDS — числові Telegram ID через кому") from exc

    moderation = _bool("MODERATION", False)
    if require_token and moderation and not admins:
        raise ConfigError("MODERATION=true потребує хоча б одного ADMIN_IDS")

    tz_name = os.getenv("TIMEZONE", "Europe/Kyiv").strip() or "Europe/Kyiv"
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"Невідома часова зона TIMEZONE={tz_name!r}") from exc

    return Settings(
        bot_token=token,
        channel_id=channel,
        admin_ids=admins,
        db_path=Path(os.getenv("DB_PATH", "data/bookbot.sqlite3")),
        sources_file=Path(os.getenv("SOURCES_FILE", "sources.yaml")),
        fetch_interval_min=_int("FETCH_INTERVAL_MIN", 30, minimum=5),
        post_interval_min=_int("POST_INTERVAL_MIN", 20, minimum=1),
        max_age_days=_int("MAX_AGE_DAYS", 3, minimum=1),
        max_new_per_source=_int("MAX_NEW_PER_SOURCE", 5, minimum=1),
        max_posts_per_day=_int("MAX_POSTS_PER_DAY", 0),
        moderation=moderation,
        quiet_hours=parse_quiet_hours(os.getenv("QUIET_HOURS", "23-8")),
        timezone=tz,
        signature=os.getenv("POST_SIGNATURE", "").strip(),
        user_agent=os.getenv(
            "USER_AGENT",
            "Mozilla/5.0 (compatible; BookNewsBot/1.0; +https://telegram.org)",
        ),
    )
