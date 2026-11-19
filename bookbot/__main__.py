"""python -m bookbot [run|check|preview]"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from .config import ConfigError, load_settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="bookbot", description="Telegram-бот книжкових новин")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run", help="запустити бота (за замовчуванням)")
    sub.add_parser("check", help="перевірити всі джерела з sources.yaml")
    o = sub.add_parser("once", help="один прохід: зібрати новини й опублікувати пост (для cron / GitHub Actions)")
    o.add_argument("--max-posts", type=int, default=1)
    p = sub.add_parser("preview", help="показати, як виглядатимуть пости, нічого не надсилаючи")
    p.add_argument("-n", "--limit", type=int, default=5)
    args = parser.parse_args()
    cmd = args.cmd or "run"

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if cmd in ("check", "preview"):
        logging.getLogger().setLevel(logging.WARNING)

    try:
        settings = load_settings(require_token=(cmd in ("run", "once")))
        from . import app  # імпорт тут, щоб помилки конфігурації показувались зрозуміло

        if cmd == "check":
            sys.exit(1 if asyncio.run(app.check_sources(settings)) else 0)
        elif cmd == "once":
            asyncio.run(app.run_once(settings, args.max_posts))
        elif cmd == "preview":
            asyncio.run(app.preview_posts(settings, args.limit))
        else:
            asyncio.run(app.run_bot(settings))
    except ConfigError as exc:
        print(f"Помилка налаштувань: {exc}", file=sys.stderr)
        if os.getenv("GITHUB_ACTIONS"):
            # покажеться в Annotations на сторінці запуску
            print(f"::error title=Помилка налаштувань::{exc}. "
                  "Перевірте секрети в Settings → Secrets and variables → Actions")
        sys.exit(2)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
