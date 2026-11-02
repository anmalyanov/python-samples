"""Наполнить базу вымышленными данными: ``python -m timetracker.seed``.

Создаёт трёх сотрудников и несколько завершённых записей за последние дни,
у одного сотрудника оставляет идущий таймер. Все моменты отсчитываются
назад от текущего времени, поэтому записи никогда не попадают «в будущее».
Повторный запуск ничего не делает, если сотрудники уже есть.
"""

from __future__ import annotations

from datetime import timedelta

from . import create_app, services
from .db import connect

EMPLOYEES = ["Алиса Примерова", "Борис Тестов", "Виктория Демина"]

# (сотрудник, часов назад от текущего момента — начало, минут длительности, задача)
ENTRIES = [
    ("Алиса Примерова", 52, 95, "Макет главной страницы"),
    ("Алиса Примерова", 48, 50, "Созвон с заказчиком"),
    ("Алиса Примерова", 27, 120, "Вёрстка формы отчёта"),
    ("Борис Тестов", 53, 180, "Настройка CI"),
    ("Борис Тестов", 28, 45, "Код-ревью"),
    ("Борис Тестов", 4, 90, "Исправление бага с датами"),
    ("Виктория Демина", 26, 60, "Написание документации"),
]

RUNNING = ("Виктория Демина", 17, "Подготовка демо")  # идёт уже 17 минут


def seed(db_path: str) -> bool:
    """Вернуть True, если данные были добавлены."""
    conn = connect(db_path)
    try:
        if conn.execute("SELECT COUNT(*) FROM employees").fetchone()[0]:
            return False

        now = services.utcnow()
        ids = {name: services.add_employee(conn, name).id for name in EMPLOYEES}
        for name, hours_ago, minutes, task in ENTRIES:
            started = now - timedelta(hours=hours_ago)
            services.start_timer(conn, ids[name], task, now=started)
            services.stop_timer(conn, ids[name], now=started + timedelta(minutes=minutes))

        name, minutes_ago, task = RUNNING
        services.start_timer(conn, ids[name], task, now=now - timedelta(minutes=minutes_ago))
        return True
    finally:
        conn.close()


def main() -> None:
    app = create_app()
    if seed(app.config["DATABASE"]):
        print(f"Демо-данные добавлены в {app.config['DATABASE']}")
    else:
        print("В базе уже есть сотрудники — ничего не делаю.")


if __name__ == "__main__":
    main()
