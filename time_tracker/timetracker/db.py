"""Работа с SQLite: подключение, схема, привязка к Flask.

База — один файл SQLite, поэтому данные переживают перезапуск приложения.
Схема создаётся идемпотентно (``CREATE ... IF NOT EXISTS``) при старте.
"""

from __future__ import annotations

import os
import sqlite3

from flask import Flask, current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    -- имя после casefold(): SQLite не умеет приводить кириллицу к нижнему
    -- регистру, поэтому ключ для проверки уникальности считаем в Python
    name_key   TEXT    NOT NULL UNIQUE,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS time_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    description TEXT    NOT NULL,
    started_at  TEXT    NOT NULL,          -- ISO-8601, UTC, до секунды
    stopped_at  TEXT,                      -- NULL, пока таймер идёт
    CHECK (stopped_at IS NULL OR stopped_at >= started_at)
);

-- У сотрудника не может быть двух одновременно идущих таймеров.
-- Проверка есть и в коде, но индекс защищает от гонки двух запросов.
CREATE UNIQUE INDEX IF NOT EXISTS one_running_timer_per_employee
    ON time_entries(employee_id) WHERE stopped_at IS NULL;

CREATE INDEX IF NOT EXISTS time_entries_by_employee_and_start
    ON time_entries(employee_id, started_at);
"""


def connect(path: str | os.PathLike[str]) -> sqlite3.Connection:
    """Открыть соединение с нужными настройками (используется и в тестах)."""
    conn = sqlite3.connect(os.fspath(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Создать таблицы и индексы, если их ещё нет."""
    conn.executescript(SCHEMA)


def get_db() -> sqlite3.Connection:
    """Соединение на время одного HTTP-запроса (хранится во flask.g)."""
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_app(app: Flask) -> None:
    """Подготовить файл базы и зарегистрировать закрытие соединения."""
    db_path = app.config["DATABASE"]
    directory = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(directory, exist_ok=True)

    conn = connect(db_path)
    try:
        init_db(conn)
    finally:
        conn.close()

    app.teardown_appcontext(close_db)
