"""Учёт рабочего времени — небольшое веб-приложение на Flask + SQLite."""

from __future__ import annotations

import os
import secrets
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask

from . import db
from .routes import bp


def create_app(test_config: dict | None = None) -> Flask:
    """Фабрика приложения. ``test_config`` переопределяет настройки в тестах."""
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        # Нужен только для flash-сообщений. Без переменной окружения ключ
        # случайный на каждый запуск: сессии не переживут рестарт, зато
        # его нельзя подделать, зная исходники.
        SECRET_KEY=os.environ.get("TIMETRACKER_SECRET_KEY") or secrets.token_hex(32),
        DATABASE=os.environ.get(
            "TIMETRACKER_DB", os.path.join(app.instance_path, "timetracker.sqlite3")
        ),
        # часовой пояс для отображения и границ периода в отчёте
        TIMEZONE=os.environ.get("TIMETRACKER_TZ", "UTC"),
    )
    if test_config:
        app.config.update(test_config)

    if ":memory:" in app.config["DATABASE"]:
        # У каждого запроса своё соединение, а база в памяти живёт только
        # внутри одного соединения — данные бы терялись между запросами.
        raise RuntimeError("База в памяти не поддерживается: укажите путь к файлу.")
    try:
        ZoneInfo(app.config["TIMEZONE"])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise RuntimeError(
            f"Неизвестный часовой пояс TIMETRACKER_TZ={app.config['TIMEZONE']!r}; "
            "укажите имя из базы IANA, например Europe/Moscow."
        ) from exc

    db.init_app(app)
    app.register_blueprint(bp)
    return app
