"""Общие фикстуры: временная база на каждый тест, приложение, клиент."""

from __future__ import annotations

import pytest

from timetracker import create_app, db

TEST_TZ = "Europe/Moscow"


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.sqlite3"


@pytest.fixture
def conn(db_path):
    """Чистое соединение для тестов бизнес-логики (без Flask)."""
    connection = db.connect(db_path)
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def make_app(db_path):
    """Фабрика: позволяет создать приложение повторно на той же базе
    и так проверить сохранность данных «после перезапуска»."""

    def _make():
        return create_app({"TESTING": True, "DATABASE": str(db_path), "TIMEZONE": TEST_TZ})

    return _make


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def client(app):
    return app.test_client()
