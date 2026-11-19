"""Завантаження списку джерел і правил фільтрації з sources.yaml."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import ConfigError


@dataclass
class SourceConfig:
    name: str
    url: str
    enabled: bool = True
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class SourcesFile:
    sources: list[SourceConfig]
    exclude: list[str] = field(default_factory=list)
    tag_rules: dict[str, list[str]] = field(default_factory=dict)


def _str_list(value, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where}: очікується список рядків")
    return [v for v in value if v.strip()]


def normalize_tag(tag: str) -> str:
    tag = tag.strip().replace(" ", "_").replace("-", "_")
    return tag if tag.startswith("#") else f"#{tag}"


def load_sources(path: Path) -> SourcesFile:
    if not path.exists():
        raise ConfigError(f"Файл джерел не знайдено: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Помилка в {path}: {exc}") from exc

    raw_sources = data.get("sources") or []
    if not isinstance(raw_sources, list):
        raise ConfigError(f"{path}: 'sources' має бути списком")

    sources: list[SourceConfig] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_sources, 1):
        where = f"{path}: джерело №{i}"
        if not isinstance(raw, dict) or not raw.get("url"):
            raise ConfigError(f"{where}: потрібне поле 'url'")
        url = str(raw["url"]).strip()
        if url in seen:
            raise ConfigError(f"{where}: url повторюється ({url})")
        seen.add(url)
        sources.append(
            SourceConfig(
                name=str(raw.get("name") or url),
                url=url,
                enabled=bool(raw.get("enabled", True)),
                include=_str_list(raw.get("include"), where + " include"),
                exclude=_str_list(raw.get("exclude"), where + " exclude"),
                tags=[normalize_tag(t) for t in _str_list(raw.get("tags"), where + " tags")],
            )
        )

    tag_rules_raw = data.get("tag_rules") or {}
    if not isinstance(tag_rules_raw, dict):
        raise ConfigError(f"{path}: 'tag_rules' має бути словником")
    tag_rules = {
        normalize_tag(tag): _str_list(words, f"{path}: tag_rules {tag}")
        for tag, words in tag_rules_raw.items()
    }

    return SourcesFile(
        sources=sources,
        exclude=_str_list(data.get("exclude"), f"{path}: exclude"),
        tag_rules=tag_rules,
    )
