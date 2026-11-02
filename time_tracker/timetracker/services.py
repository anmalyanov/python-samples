"""Бизнес-логика учёта рабочего времени.

Функции принимают открытое соединение ``sqlite3`` и ничего не знают о Flask,
поэтому их легко тестировать без веб-слоя.

Время хранится в базе строками ISO-8601 в UTC с точностью до секунды
(``2026-11-02T16:38:00+00:00``). Формат строго единый: SQLite сравнивает
такие значения как текст, и только при одинаковом формате текстовый порядок
совпадает с хронологическим.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo

MAX_NAME_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 200
MAX_SQLITE_INT = 2**63 - 1  # больше этого SQLite в INTEGER не хранит


# --- ошибки -----------------------------------------------------------------


class TimeTrackerError(Exception):
    """Базовая ошибка. Текст можно показывать пользователю как есть."""

    status_code = 400


class ValidationError(TimeTrackerError):
    status_code = 400


class NotFoundError(TimeTrackerError):
    status_code = 404


class DuplicateEmployeeError(TimeTrackerError):
    status_code = 409


class TimerAlreadyRunningError(TimeTrackerError):
    status_code = 409


class TimerNotRunningError(TimeTrackerError):
    status_code = 409


# --- время ------------------------------------------------------------------


def utcnow() -> datetime:
    """Текущий момент в UTC без микросекунд (как хранится в базе)."""
    return datetime.now(UTC).replace(microsecond=0)


def normalize(moment: datetime) -> datetime:
    """Привести момент к виду, в котором он хранится в базе (UTC, секунды)."""
    if moment.tzinfo is None:
        raise ValueError("Ожидается datetime с часовым поясом.")
    return moment.astimezone(UTC).replace(microsecond=0)


def to_db(moment: datetime) -> str:
    return normalize(moment).isoformat(timespec="seconds")


def from_db(value: str) -> datetime:
    return datetime.fromisoformat(value)


def format_duration(total_seconds: int) -> str:
    """Секунды -> строка вида ``Ч:ММ:СС`` (часы не ограничены 24)."""
    hours, rest = divmod(max(0, int(total_seconds)), 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


# --- модели -----------------------------------------------------------------


@dataclass(frozen=True)
class Employee:
    id: int
    name: str


@dataclass(frozen=True)
class TimeEntry:
    id: int
    employee_id: int
    description: str
    started_at: datetime
    stopped_at: datetime | None

    @property
    def is_running(self) -> bool:
        return self.stopped_at is None

    def duration_seconds(self, now: datetime | None = None) -> int:
        """Длительность записи; для идущего таймера — до момента ``now``."""
        end = self.stopped_at if self.stopped_at is not None else normalize(now or utcnow())
        return max(0, int((end - self.started_at).total_seconds()))


@dataclass(frozen=True)
class EmployeeStatus:
    employee: Employee
    running_entry: TimeEntry | None


@dataclass(frozen=True)
class Report:
    employee: Employee
    date_from: date
    date_to: date
    entries: list[TimeEntry]
    total_seconds: int  # только завершённые записи

    @property
    def is_empty(self) -> bool:
        return not self.entries


# --- вспомогательное --------------------------------------------------------


def _clean_text(value: str | None, field: str, max_length: int) -> str:
    cleaned = " ".join((value or "").split())
    if not cleaned:
        raise ValidationError(f"{field} не может быть пустым.")
    if len(cleaned) > max_length:
        raise ValidationError(f"{field}: не более {max_length} символов.")
    return cleaned


def _row_to_entry(row: sqlite3.Row) -> TimeEntry:
    return TimeEntry(
        id=row["id"],
        employee_id=row["employee_id"],
        description=row["description"],
        started_at=from_db(row["started_at"]),
        stopped_at=from_db(row["stopped_at"]) if row["stopped_at"] else None,
    )


_ENTRY_COLUMNS = "id, employee_id, description, started_at, stopped_at"


# --- сотрудники -------------------------------------------------------------


def add_employee(conn: sqlite3.Connection, name: str | None) -> Employee:
    name = _clean_text(name, "Имя сотрудника", MAX_NAME_LENGTH)
    try:
        with conn:
            cursor = conn.execute(
                "INSERT INTO employees (name, name_key, created_at) VALUES (?, ?, ?)",
                (name, name.casefold(), to_db(utcnow())),
            )
    except sqlite3.IntegrityError as exc:
        raise DuplicateEmployeeError(f"Сотрудник «{name}» уже есть в списке.") from exc
    return Employee(id=cursor.lastrowid, name=name)


def get_employee(conn: sqlite3.Connection, employee_id: int) -> Employee:
    if not 0 < employee_id <= MAX_SQLITE_INT:
        # Иначе sqlite3 бросит OverflowError при подстановке параметра.
        raise NotFoundError("Сотрудник не найден.")
    row = conn.execute("SELECT id, name FROM employees WHERE id = ?", (employee_id,)).fetchone()
    if row is None:
        raise NotFoundError("Сотрудник не найден.")
    return Employee(id=row["id"], name=row["name"])


def all_employees(conn: sqlite3.Connection) -> list[Employee]:
    """Все сотрудники по алфавиту (для выпадающих списков)."""
    rows = conn.execute("SELECT id, name FROM employees ORDER BY name_key, id").fetchall()
    return [Employee(id=row["id"], name=row["name"]) for row in rows]


def list_employees(conn: sqlite3.Connection) -> list[EmployeeStatus]:
    """Все сотрудники по алфавиту вместе с их идущим таймером (если есть)."""
    rows = conn.execute(
        """
        SELECT e.id AS emp_id, e.name AS emp_name,
               t.id, t.employee_id, t.description, t.started_at, t.stopped_at
        FROM employees e
        LEFT JOIN time_entries t
               ON t.employee_id = e.id AND t.stopped_at IS NULL
        ORDER BY e.name_key, e.id
        """
    ).fetchall()
    return [
        EmployeeStatus(
            employee=Employee(id=row["emp_id"], name=row["emp_name"]),
            running_entry=_row_to_entry(row) if row["id"] is not None else None,
        )
        for row in rows
    ]


# --- таймер -----------------------------------------------------------------


def get_running_entry(conn: sqlite3.Connection, employee_id: int) -> TimeEntry | None:
    row = conn.execute(
        f"SELECT {_ENTRY_COLUMNS} FROM time_entries WHERE employee_id = ? AND stopped_at IS NULL",
        (employee_id,),
    ).fetchone()
    return _row_to_entry(row) if row is not None else None


def start_timer(
    conn: sqlite3.Connection,
    employee_id: int,
    description: str | None,
    now: datetime | None = None,
) -> TimeEntry:
    """Запустить таймер. Повторный запуск при идущем таймере — ошибка."""
    employee = get_employee(conn, employee_id)
    description = _clean_text(description, "Описание задачи", MAX_DESCRIPTION_LENGTH)
    started_at = normalize(now or utcnow())

    running = get_running_entry(conn, employee.id)
    if running is not None:
        raise TimerAlreadyRunningError(
            f"У сотрудника {employee.name} уже идёт таймер «{running.description}». "
            "Сначала остановите его."
        )
    try:
        with conn:
            cursor = conn.execute(
                "INSERT INTO time_entries (employee_id, description, started_at) VALUES (?, ?, ?)",
                (employee.id, description, to_db(started_at)),
            )
    except sqlite3.IntegrityError as exc:
        # Два запроса на запуск пришли одновременно: частичный уникальный
        # индекс не дал создать вторую открытую запись.
        raise TimerAlreadyRunningError(
            f"У сотрудника {employee.name} уже идёт таймер. Сначала остановите его."
        ) from exc
    return TimeEntry(
        id=cursor.lastrowid,
        employee_id=employee.id,
        description=description,
        started_at=started_at,
        stopped_at=None,
    )


def stop_timer(
    conn: sqlite3.Connection, employee_id: int, now: datetime | None = None
) -> TimeEntry:
    """Остановить идущий таймер и вернуть закрытую запись."""
    employee = get_employee(conn, employee_id)
    running = get_running_entry(conn, employee.id)
    if running is None:
        raise TimerNotRunningError(f"У сотрудника {employee.name} нет идущего таймера.")

    stopped_at = normalize(now or utcnow())
    if stopped_at < running.started_at:
        # Системные часы перевели назад — не допускаем отрицательной длительности.
        stopped_at = running.started_at

    with conn:
        cursor = conn.execute(
            "UPDATE time_entries SET stopped_at = ? WHERE id = ? AND stopped_at IS NULL",
            (to_db(stopped_at), running.id),
        )
    if cursor.rowcount == 0:
        # Таймер успели остановить параллельным запросом.
        raise TimerNotRunningError(f"У сотрудника {employee.name} нет идущего таймера.")
    return replace(running, stopped_at=stopped_at)


# --- отчёт ------------------------------------------------------------------


def build_report(
    conn: sqlite3.Connection,
    employee_id: int,
    date_from: date,
    date_to: date,
    tz: tzinfo,
) -> Report:
    """Записи сотрудника, начатые в период [date_from, date_to] (обе даты включительно).

    Границы дней берутся в часовом поясе ``tz`` и переводятся в UTC для
    сравнения с хранимыми значениями. В общее время входят только
    завершённые записи; идущий таймер показывается отдельно.
    """
    employee = get_employee(conn, employee_id)
    if date_from > date_to:
        raise ValidationError("Дата начала периода не может быть позже даты окончания.")

    try:
        period_start = to_db(datetime.combine(date_from, time.min, tzinfo=tz))
        period_end = to_db(datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=tz))
    except OverflowError as exc:
        # Даты вроде 0001-01-01 или 9999-12-31 не переводятся в UTC.
        raise ValidationError("Дата вне допустимого диапазона.") from exc

    rows = conn.execute(
        f"SELECT {_ENTRY_COLUMNS} FROM time_entries "
        "WHERE employee_id = ? AND started_at >= ? AND started_at < ? "
        "ORDER BY started_at, id",
        (employee.id, period_start, period_end),
    ).fetchall()
    entries = [_row_to_entry(row) for row in rows]
    total = sum(entry.duration_seconds() for entry in entries if not entry.is_running)
    return Report(
        employee=employee,
        date_from=date_from,
        date_to=date_to,
        entries=entries,
        total_seconds=total,
    )
