"""Консольный интерфейс к тому же сервисному слою, что и веб-приложение.

    python -m timetracker.cli employees
    python -m timetracker.cli add "Алиса Примерова"
    python -m timetracker.cli start "Алиса Примерова" Макет главной страницы
    python -m timetracker.cli stop "Алиса Примерова"
    python -m timetracker.cli report "Алиса Примерова" --from 2026-03-10 --to 2026-03-16

База и часовой пояс берутся из тех же переменных окружения, что и у веб-версии
(``TIMETRACKER_DB``, ``TIMETRACKER_TZ``), поэтому CLI и браузер видят одни данные.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import services
from .db import connect, default_db_path, init_db
from .services import format_duration

REPORT_DEFAULT_DAYS = 7


def _local(moment: datetime, tz: tzinfo) -> str:
    return moment.astimezone(tz).strftime("%d.%m.%Y %H:%M:%S")


def cmd_employees(conn, args, tz) -> int:
    statuses = services.list_employees(conn)
    if not statuses:
        print("Сотрудников нет.")
        return 0
    for status in statuses:
        running = status.running_entry
        if running is None:
            state = "таймер не запущен"
        else:
            state = (
                f"идёт «{running.description}» с {_local(running.started_at, tz)}, "
                f"{format_duration(running.duration_seconds())}"
            )
        print(f"{status.employee.id:>3}  {status.employee.name:<28}  {state}")
    return 0


def cmd_add(conn, args, tz) -> int:
    employee = services.add_employee(conn, args.name)
    print(f"Добавлен сотрудник {employee.name} (id {employee.id}).")
    return 0


def cmd_start(conn, args, tz) -> int:
    employee = services.find_employee(conn, args.name)
    entry = services.start_timer(conn, employee.id, " ".join(args.task))
    print(f"Старт: {employee.name} — «{entry.description}» в {_local(entry.started_at, tz)}.")
    return 0


def cmd_stop(conn, args, tz) -> int:
    employee = services.find_employee(conn, args.name)
    entry = services.stop_timer(conn, employee.id)
    print(
        f"Стоп: {employee.name} — «{entry.description}», "
        f"{format_duration(entry.duration_seconds())}."
    )
    return 0


def cmd_report(conn, args, tz) -> int:
    employee = services.find_employee(conn, args.name)
    date_to = args.date_to or datetime.now(tz).date()
    date_from = args.date_from or date_to - timedelta(days=REPORT_DEFAULT_DAYS - 1)
    report = services.build_report(conn, employee.id, date_from, date_to, tz)

    print(f"{employee.name}: {date_from:%d.%m.%Y} — {date_to:%d.%m.%Y}")
    if report.is_empty:
        print("Нет записей за выбранный период.")
    for entry in report.entries:
        stopped = _local(entry.stopped_at, tz) if entry.stopped_at else "идёт"
        length = format_duration(entry.duration_seconds()) + ("" if entry.stopped_at else " *")
        print(f"{_local(entry.started_at, tz)}  {stopped:<19}  {length:>11}  {entry.description}")
    print(f"Итого: {format_duration(report.total_seconds)}")
    if any(entry.is_running for entry in report.entries):
        print("* идущий таймер в итог не входит")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m timetracker.cli", description="Учёт рабочего времени из терминала."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("employees", help="список сотрудников и их таймеров").set_defaults(
        func=cmd_employees
    )

    add = sub.add_parser("add", help="добавить сотрудника")
    add.add_argument("name", help="имя сотрудника")
    add.set_defaults(func=cmd_add)

    start = sub.add_parser("start", help="запустить таймер")
    start.add_argument("name", help="имя сотрудника")
    start.add_argument("task", nargs="+", help="описание задачи")
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop", help="остановить таймер")
    stop.add_argument("name", help="имя сотрудника")
    stop.set_defaults(func=cmd_stop)

    report = sub.add_parser("report", help="отчёт за период (по умолчанию последняя неделя)")
    report.add_argument("name", help="имя сотрудника")
    report.add_argument("--from", dest="date_from", type=date.fromisoformat, metavar="ГГГГ-ММ-ДД")
    report.add_argument("--to", dest="date_to", type=date.fromisoformat, metavar="ГГГГ-ММ-ДД")
    report.set_defaults(func=cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        tz = ZoneInfo(os.environ.get("TIMETRACKER_TZ", "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        print("Ошибка: неизвестный часовой пояс TIMETRACKER_TZ.", file=sys.stderr)
        return 2

    db_path = os.environ.get("TIMETRACKER_DB") or default_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = connect(db_path)
    try:
        init_db(conn)
        return args.func(conn, args, tz)
    except services.TimeTrackerError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
