"""Тесты бизнес-логики: сотрудники, таймер, отчёт."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from timetracker import db, services
from timetracker.services import (
    DuplicateEmployeeError,
    NotFoundError,
    TimerAlreadyRunningError,
    TimerNotRunningError,
    ValidationError,
)

MSK = ZoneInfo("Europe/Moscow")
T0 = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)


def running_entries(conn, employee_id):
    return conn.execute(
        "SELECT * FROM time_entries WHERE employee_id = ? AND stopped_at IS NULL",
        (employee_id,),
    ).fetchall()


# --- сотрудники -------------------------------------------------------------


def test_add_employee_trims_whitespace(conn):
    employee = services.add_employee(conn, "  Алиса   Примерова ")
    assert employee.name == "Алиса Примерова"
    assert [s.employee.name for s in services.list_employees(conn)] == ["Алиса Примерова"]


@pytest.mark.parametrize("name", ["", "   ", None])
def test_add_employee_rejects_empty_name(conn, name):
    with pytest.raises(ValidationError):
        services.add_employee(conn, name)
    assert services.list_employees(conn) == []


def test_add_employee_rejects_too_long_name(conn):
    with pytest.raises(ValidationError):
        services.add_employee(conn, "А" * (services.MAX_NAME_LENGTH + 1))


def test_add_employee_duplicate_is_case_insensitive_for_cyrillic(conn):
    services.add_employee(conn, "Борис Тестов")
    with pytest.raises(DuplicateEmployeeError):
        services.add_employee(conn, "борис тестов")
    with pytest.raises(DuplicateEmployeeError):
        services.add_employee(conn, "БОРИС  ТЕСТОВ")
    assert len(services.list_employees(conn)) == 1


def test_list_employees_sorted_by_name(conn):
    for name in ["Виктория", "алиса", "Борис"]:
        services.add_employee(conn, name)
    assert [s.employee.name for s in services.list_employees(conn)] == [
        "алиса",
        "Борис",
        "Виктория",
    ]


def test_get_unknown_employee(conn):
    with pytest.raises(NotFoundError):
        services.get_employee(conn, 999)


# --- таймер -----------------------------------------------------------------


def test_start_timer_creates_running_entry(conn):
    emp = services.add_employee(conn, "Алиса")
    entry = services.start_timer(conn, emp.id, "  Макет  страницы ", now=T0)

    assert entry.is_running
    assert entry.description == "Макет страницы"
    assert entry.started_at == T0
    status = services.list_employees(conn)[0]
    assert status.running_entry is not None
    assert status.running_entry.id == entry.id


def test_start_timer_twice_is_rejected(conn):
    """Повторный запуск при идущем таймере: ошибка, вторая запись не создаётся."""
    emp = services.add_employee(conn, "Алиса")
    first = services.start_timer(conn, emp.id, "Первая задача", now=T0)

    with pytest.raises(TimerAlreadyRunningError) as exc_info:
        services.start_timer(conn, emp.id, "Вторая задача", now=T0 + timedelta(minutes=5))

    assert "Первая задача" in str(exc_info.value)
    open_entries = running_entries(conn, emp.id)
    assert len(open_entries) == 1
    assert open_entries[0]["id"] == first.id
    assert conn.execute("SELECT COUNT(*) FROM time_entries").fetchone()[0] == 1


def test_database_forbids_second_running_entry_even_bypassing_service(conn):
    """Частичный уникальный индекс защищает от гонки двух запросов."""
    import sqlite3

    emp = services.add_employee(conn, "Алиса")
    services.start_timer(conn, emp.id, "Задача", now=T0)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO time_entries (employee_id, description, started_at) VALUES (?, ?, ?)",
            (emp.id, "Гонка", services.to_db(T0)),
        )


def test_restart_after_stop_is_allowed(conn):
    emp = services.add_employee(conn, "Алиса")
    services.start_timer(conn, emp.id, "Задача 1", now=T0)
    services.stop_timer(conn, emp.id, now=T0 + timedelta(minutes=30))
    second = services.start_timer(conn, emp.id, "Задача 2", now=T0 + timedelta(hours=1))

    assert second.is_running
    assert conn.execute("SELECT COUNT(*) FROM time_entries").fetchone()[0] == 2
    assert len(running_entries(conn, emp.id)) == 1


@pytest.mark.parametrize("description", ["", "   ", None])
def test_start_timer_requires_description(conn, description):
    emp = services.add_employee(conn, "Алиса")
    with pytest.raises(ValidationError):
        services.start_timer(conn, emp.id, description, now=T0)
    assert running_entries(conn, emp.id) == []


def test_start_timer_unknown_employee(conn):
    with pytest.raises(NotFoundError):
        services.start_timer(conn, 42, "Задача", now=T0)


def test_timers_of_different_employees_are_independent(conn):
    alice = services.add_employee(conn, "Алиса")
    boris = services.add_employee(conn, "Борис")
    services.start_timer(conn, alice.id, "Задача А", now=T0)
    services.start_timer(conn, boris.id, "Задача Б", now=T0)

    statuses = {s.employee.name: s.running_entry for s in services.list_employees(conn)}
    assert statuses["Алиса"].description == "Задача А"
    assert statuses["Борис"].description == "Задача Б"

    services.stop_timer(conn, alice.id, now=T0 + timedelta(minutes=10))
    statuses = {s.employee.name: s.running_entry for s in services.list_employees(conn)}
    assert statuses["Алиса"] is None
    assert statuses["Борис"] is not None


def test_stop_timer_closes_entry_and_computes_duration(conn):
    emp = services.add_employee(conn, "Алиса")
    services.start_timer(conn, emp.id, "Задача", now=T0)
    entry = services.stop_timer(conn, emp.id, now=T0 + timedelta(hours=1, minutes=2, seconds=3))

    assert not entry.is_running
    assert entry.duration_seconds() == 3723
    assert services.format_duration(entry.duration_seconds()) == "1:02:03"
    assert running_entries(conn, emp.id) == []
    assert services.list_employees(conn)[0].running_entry is None


def test_stop_timer_without_running_timer(conn):
    emp = services.add_employee(conn, "Алиса")
    with pytest.raises(TimerNotRunningError):
        services.stop_timer(conn, emp.id, now=T0)

    services.start_timer(conn, emp.id, "Задача", now=T0)
    services.stop_timer(conn, emp.id, now=T0 + timedelta(minutes=1))
    with pytest.raises(TimerNotRunningError):
        services.stop_timer(conn, emp.id, now=T0 + timedelta(minutes=2))


def test_stop_timer_when_clock_went_backwards(conn):
    emp = services.add_employee(conn, "Алиса")
    services.start_timer(conn, emp.id, "Задача", now=T0)
    entry = services.stop_timer(conn, emp.id, now=T0 - timedelta(minutes=5))
    assert entry.stopped_at == T0
    assert entry.duration_seconds() == 0


def test_timestamps_are_stored_in_utc_without_microseconds(conn):
    emp = services.add_employee(conn, "Алиса")
    local = datetime(2026, 3, 10, 12, 0, 0, 123456, tzinfo=MSK)  # 09:00 UTC
    services.start_timer(conn, emp.id, "Задача", now=local)
    stored = conn.execute("SELECT started_at FROM time_entries").fetchone()[0]
    assert stored == "2026-03-10T09:00:00+00:00"


def test_running_entry_duration_uses_now(conn):
    emp = services.add_employee(conn, "Алиса")
    entry = services.start_timer(conn, emp.id, "Задача", now=T0)
    assert entry.duration_seconds(now=T0 + timedelta(minutes=15)) == 900


# --- отчёт ------------------------------------------------------------------


def test_report_counts_only_entries_within_period(conn):
    emp = services.add_employee(conn, "Алиса")
    # 1 марта — вне периода
    services.start_timer(conn, emp.id, "Старое", now=datetime(2026, 3, 1, 9, 0, tzinfo=MSK))
    services.stop_timer(conn, emp.id, now=datetime(2026, 3, 1, 10, 0, tzinfo=MSK))
    # 10 и 11 марта — в периоде
    services.start_timer(conn, emp.id, "Задача 1", now=datetime(2026, 3, 10, 9, 0, tzinfo=MSK))
    services.stop_timer(conn, emp.id, now=datetime(2026, 3, 10, 10, 30, tzinfo=MSK))
    services.start_timer(conn, emp.id, "Задача 2", now=datetime(2026, 3, 11, 14, 0, tzinfo=MSK))
    services.stop_timer(conn, emp.id, now=datetime(2026, 3, 11, 14, 45, tzinfo=MSK))
    # 12 марта — вне периода
    services.start_timer(conn, emp.id, "Новое", now=datetime(2026, 3, 12, 9, 0, tzinfo=MSK))
    services.stop_timer(conn, emp.id, now=datetime(2026, 3, 12, 12, 0, tzinfo=MSK))

    report = services.build_report(conn, emp.id, date(2026, 3, 10), date(2026, 3, 11), MSK)

    assert [e.description for e in report.entries] == ["Задача 1", "Задача 2"]
    assert report.total_seconds == 90 * 60 + 45 * 60
    assert services.format_duration(report.total_seconds) == "2:15:00"
    assert not report.is_empty


def test_report_is_empty_when_no_entries(conn):
    """Пустой отчёт: записей нет, общее время 0, ошибки нет."""
    emp = services.add_employee(conn, "Алиса")
    report = services.build_report(conn, emp.id, date(2026, 3, 10), date(2026, 3, 16), MSK)

    assert report.is_empty
    assert report.entries == []
    assert report.total_seconds == 0
    assert services.format_duration(report.total_seconds) == "0:00:00"
    assert report.employee == emp


def test_report_is_empty_when_entries_are_outside_period(conn):
    emp = services.add_employee(conn, "Алиса")
    services.start_timer(conn, emp.id, "Задача", now=datetime(2026, 3, 1, 9, 0, tzinfo=MSK))
    services.stop_timer(conn, emp.id, now=datetime(2026, 3, 1, 10, 0, tzinfo=MSK))

    report = services.build_report(conn, emp.id, date(2026, 3, 10), date(2026, 3, 10), MSK)
    assert report.is_empty
    assert report.total_seconds == 0


def test_report_period_boundaries_in_app_timezone(conn):
    """Период — календарные дни в поясе приложения, обе границы включительно."""
    emp = services.add_employee(conn, "Алиса")
    # 23:59:59 последнего дня периода по Москве — входит
    services.start_timer(conn, emp.id, "Поздно", now=datetime(2026, 3, 10, 23, 59, 59, tzinfo=MSK))
    services.stop_timer(conn, emp.id, now=datetime(2026, 3, 11, 0, 30, tzinfo=MSK))
    # 00:00:00 следующего дня — не входит
    services.start_timer(conn, emp.id, "Полночь", now=datetime(2026, 3, 11, 0, 0, 0, tzinfo=MSK))
    services.stop_timer(conn, emp.id, now=datetime(2026, 3, 11, 1, 0, tzinfo=MSK))
    # 00:00:00 первого дня периода — входит (по UTC это ещё 9 марта!)
    services.start_timer(conn, emp.id, "Рано", now=datetime(2026, 3, 10, 0, 0, 0, tzinfo=MSK))
    services.stop_timer(conn, emp.id, now=datetime(2026, 3, 10, 0, 10, tzinfo=MSK))

    report = services.build_report(conn, emp.id, date(2026, 3, 10), date(2026, 3, 10), MSK)
    assert sorted(e.description for e in report.entries) == ["Поздно", "Рано"]


def test_report_rejects_inverted_period(conn):
    emp = services.add_employee(conn, "Алиса")
    with pytest.raises(ValidationError):
        services.build_report(conn, emp.id, date(2026, 3, 11), date(2026, 3, 10), MSK)


def test_report_unknown_employee(conn):
    with pytest.raises(NotFoundError):
        services.build_report(conn, 777, date(2026, 3, 10), date(2026, 3, 10), MSK)


def test_report_shows_running_entry_but_excludes_it_from_total(conn):
    emp = services.add_employee(conn, "Алиса")
    services.start_timer(conn, emp.id, "Готово", now=datetime(2026, 3, 10, 9, 0, tzinfo=MSK))
    services.stop_timer(conn, emp.id, now=datetime(2026, 3, 10, 10, 0, tzinfo=MSK))
    services.start_timer(conn, emp.id, "В работе", now=datetime(2026, 3, 10, 11, 0, tzinfo=MSK))

    report = services.build_report(conn, emp.id, date(2026, 3, 10), date(2026, 3, 10), MSK)
    assert [e.description for e in report.entries] == ["Готово", "В работе"]
    assert report.entries[1].is_running
    assert report.total_seconds == 3600


def test_report_isolates_employees(conn):
    alice = services.add_employee(conn, "Алиса")
    boris = services.add_employee(conn, "Борис")
    services.start_timer(conn, boris.id, "Чужое", now=datetime(2026, 3, 10, 9, 0, tzinfo=MSK))
    services.stop_timer(conn, boris.id, now=datetime(2026, 3, 10, 10, 0, tzinfo=MSK))

    report = services.build_report(conn, alice.id, date(2026, 3, 10), date(2026, 3, 10), MSK)
    assert report.is_empty


# --- форматирование и сохранность -------------------------------------------


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (0, "0:00:00"),
        (59, "0:00:59"),
        (60, "0:01:00"),
        (3723, "1:02:03"),
        (90000, "25:00:00"),
        (-5, "0:00:00"),
    ],
)
def test_format_duration(seconds, expected):
    assert services.format_duration(seconds) == expected


def test_data_survive_reconnect(db_path):
    """Имитация перезапуска: закрываем соединение и открываем базу заново."""
    conn = db.connect(db_path)
    db.init_db(conn)
    emp = services.add_employee(conn, "Алиса")
    services.start_timer(conn, emp.id, "Задача", now=T0)
    services.stop_timer(conn, emp.id, now=T0 + timedelta(minutes=20))
    services.start_timer(conn, emp.id, "Ещё идёт", now=T0 + timedelta(hours=1))
    conn.close()

    conn = db.connect(db_path)
    db.init_db(conn)  # повторная инициализация не должна ничего стереть
    statuses = services.list_employees(conn)
    assert [s.employee.name for s in statuses] == ["Алиса"]
    assert statuses[0].running_entry.description == "Ещё идёт"
    report = services.build_report(conn, emp.id, T0.date(), T0.date(), UTC)
    assert [e.description for e in report.entries] == ["Задача", "Ещё идёт"]
    assert report.total_seconds == 1200
    conn.close()


def test_get_employee_with_id_outside_sqlite_range(conn):
    with pytest.raises(NotFoundError):
        services.get_employee(conn, 2**64)
    with pytest.raises(NotFoundError):
        services.get_employee(conn, 0)


def test_report_with_dates_outside_representable_range(conn):
    emp = services.add_employee(conn, "Алиса")
    with pytest.raises(ValidationError):
        services.build_report(conn, emp.id, date(1, 1, 1), date(1, 1, 2), MSK)
    with pytest.raises(ValidationError):
        services.build_report(conn, emp.id, date(9999, 12, 30), date(9999, 12, 31), MSK)


# --- гонка, поиск по имени, долгие таймеры, CSV ------------------------------


def test_concurrent_start_requests_create_single_entry(db_path, monkeypatch):
    """Два «Старт» одновременно: оба прошли проверку в коде, но индекс пропустил одного."""
    import threading

    conn = db.connect(db_path)
    db.init_db(conn)
    emp = services.add_employee(conn, "Алиса")
    conn.close()

    barrier = threading.Barrier(2)
    original = services.get_running_entry

    def get_running_entry_then_wait(connection, employee_id):
        result = original(connection, employee_id)
        barrier.wait(timeout=5)  # оба потока увидели «таймер не идёт» — и только потом пишут
        return result

    monkeypatch.setattr(services, "get_running_entry", get_running_entry_then_wait)

    outcomes: list[str] = []

    def worker(label: str) -> None:
        connection = db.connect(db_path)
        try:
            services.start_timer(connection, emp.id, f"Задача {label}")
            outcomes.append("ok")
        except TimerAlreadyRunningError:
            outcomes.append("rejected")
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(label,)) for label in "AB"]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(outcomes) == ["ok", "rejected"]
    conn = db.connect(db_path)
    try:
        assert len(running_entries(conn, emp.id)) == 1
    finally:
        conn.close()


def test_find_employee_ignores_case_and_spaces(conn):
    emp = services.add_employee(conn, "Алиса Примерова")
    assert services.find_employee(conn, "  алиса   ПРИМЕРОВА ") == emp
    with pytest.raises(NotFoundError):
        services.find_employee(conn, "Алиса")
    with pytest.raises(ValidationError):
        services.find_employee(conn, "   ")


def test_suspiciously_long_timer(conn):
    emp = services.add_employee(conn, "Алиса")
    entry = services.start_timer(conn, emp.id, "Задача", now=T0)
    assert not entry.is_suspiciously_long(now=T0 + timedelta(hours=12))
    assert entry.is_suspiciously_long(now=T0 + timedelta(hours=12, seconds=1))
    stopped = services.stop_timer(conn, emp.id, now=T0 + timedelta(hours=20))
    assert not stopped.is_suspiciously_long()  # завершённые записи не предупреждают


def test_report_to_csv(conn):
    emp = services.add_employee(conn, "Алиса")
    services.start_timer(
        conn, emp.id, "Макет; с точкой с запятой", now=datetime(2026, 3, 10, 9, 0, tzinfo=MSK)
    )
    services.stop_timer(conn, emp.id, now=datetime(2026, 3, 10, 10, 30, tzinfo=MSK))
    services.start_timer(conn, emp.id, "Идёт", now=datetime(2026, 3, 10, 11, 0, tzinfo=MSK))
    report = services.build_report(conn, emp.id, date(2026, 3, 10), date(2026, 3, 10), MSK)

    lines = services.report_to_csv(report, MSK).split("\r\n")
    assert lines[0] == "Сотрудник;Начало;Окончание;Длительность;Секунды;Задача"
    assert (
        lines[1]
        == 'Алиса;2026-03-10 09:00:00;2026-03-10 10:30:00;1:30:00;5400;"Макет; с точкой с запятой"'
    )
    assert lines[2] == "Алиса;2026-03-10 11:00:00;;идёт;;Идёт"
    assert lines[3] == "Итого;;;1:30:00;5400;"
