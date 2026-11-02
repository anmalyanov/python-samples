"""Консольный интерфейс использует тот же сервисный слой, что и веб."""

from __future__ import annotations

import pytest

from timetracker import services
from timetracker.cli import main
from timetracker.db import connect


@pytest.fixture
def cli_env(db_path, monkeypatch):
    monkeypatch.setenv("TIMETRACKER_DB", str(db_path))
    monkeypatch.setenv("TIMETRACKER_TZ", "Europe/Moscow")
    return db_path


def run(capsys, *argv):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_add_start_stop_report_roundtrip(cli_env, capsys):
    code, out, _ = run(capsys, "add", "Алиса Примерова")
    assert code == 0 and "Добавлен сотрудник Алиса Примерова" in out

    code, out, _ = run(capsys, "start", "алиса примерова", "Макет", "главной")
    assert code == 0 and "«Макет главной»" in out

    code, out, _ = run(capsys, "employees")
    assert code == 0 and "идёт «Макет главной»" in out

    code, out, _ = run(capsys, "stop", "Алиса Примерова")
    assert code == 0 and out.startswith("Стоп: Алиса Примерова")

    code, out, _ = run(capsys, "report", "Алиса Примерова")
    assert code == 0
    assert "Макет главной" in out
    assert "Итого: 0:00:0" in out


def test_second_start_is_rejected_with_exit_code(cli_env, capsys):
    run(capsys, "add", "Алиса")
    run(capsys, "start", "Алиса", "Первая")
    code, out, err = run(capsys, "start", "Алиса", "Вторая")
    assert code == 1
    assert out == ""
    assert "уже идёт таймер «Первая»" in err


def test_stop_without_timer_and_unknown_employee(cli_env, capsys):
    run(capsys, "add", "Алиса")
    code, _, err = run(capsys, "stop", "Алиса")
    assert code == 1 and "нет идущего таймера" in err
    code, _, err = run(capsys, "start", "Борис", "Задача")
    assert code == 1 and "«Борис» не найден" in err


def test_empty_report(cli_env, capsys):
    run(capsys, "add", "Алиса")
    code, out, _ = run(capsys, "report", "Алиса", "--from", "2020-01-01", "--to", "2020-01-07")
    assert code == 0
    assert "01.01.2020 — 07.01.2020" in out
    assert "Нет записей за выбранный период." in out
    assert "Итого: 0:00:00" in out


def test_cli_and_web_share_the_database(cli_env, make_app, capsys):
    run(capsys, "add", "Алиса")
    run(capsys, "start", "Алиса", "Из терминала")
    html = make_app().test_client().get("/").get_data(as_text=True)
    assert "Из терминала" in html

    conn = connect(cli_env)
    try:
        assert services.list_employees(conn)[0].running_entry.description == "Из терминала"
    finally:
        conn.close()


def test_invalid_date_is_rejected_by_argparse(cli_env, capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["report", "Алиса", "--from", "10.03.2026"])
    assert exc_info.value.code == 2
