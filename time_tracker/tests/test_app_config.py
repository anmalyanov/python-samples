"""Тесты конфигурации приложения, демо-данных и экранирования."""

from __future__ import annotations

import pytest

from timetracker import create_app, services
from timetracker.db import connect
from timetracker.seed import seed


def test_invalid_timezone_fails_fast(db_path):
    with pytest.raises(RuntimeError, match="часовой пояс"):
        create_app({"TESTING": True, "DATABASE": str(db_path), "TIMEZONE": "Mars/Olympus"})


def test_database_directory_is_created(tmp_path):
    db_file = tmp_path / "nested" / "dir" / "tracker.sqlite3"
    app = create_app({"TESTING": True, "DATABASE": str(db_file)})
    assert db_file.exists()
    assert app.test_client().get("/").status_code == 200


def test_seed_adds_demo_data_once(app):
    assert seed(app.config["DATABASE"]) is True
    assert seed(app.config["DATABASE"]) is False  # повторный запуск ничего не меняет

    conn = connect(app.config["DATABASE"])
    try:
        statuses = services.list_employees(conn)
        running = [s for s in statuses if s.running_entry is not None]
        total_entries = conn.execute("SELECT COUNT(*) FROM time_entries").fetchone()[0]
        future = conn.execute(
            "SELECT COUNT(*) FROM time_entries WHERE started_at > ?",
            (services.to_db(services.utcnow()),),
        ).fetchone()[0]
    finally:
        conn.close()

    assert len(statuses) == 3
    assert len(running) == 1
    assert total_entries == 8
    assert future == 0  # демо-записи не должны начинаться «в будущем»

    html = app.test_client().get("/").get_data(as_text=True)
    assert "Подготовка демо" in html


def test_user_input_is_html_escaped(client):
    client.post("/employees", data={"name": "<script>alert(1)</script>"})
    html = client.get("/").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_in_memory_database_is_rejected():
    with pytest.raises(RuntimeError, match="в памяти"):
        create_app({"TESTING": True, "DATABASE": ":memory:"})
