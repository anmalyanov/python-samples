"""Тесты HTTP-слоя через тестовый клиент Flask."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from timetracker import services
from timetracker.db import connect


def add_employee(client, name):
    return client.post("/employees", data={"name": name})


def employee_id(app, name):
    conn = connect(app.config["DATABASE"])
    try:
        return conn.execute("SELECT id FROM employees WHERE name = ?", (name,)).fetchone()[0]
    finally:
        conn.close()


def page(response):
    return response.get_data(as_text=True)


# --- главная и сотрудники ---------------------------------------------------


def test_index_without_employees(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Сотрудников пока нет" in page(response)


def test_add_employee_redirects_and_shows_name(client):
    response = add_employee(client, "Алиса Примерова")
    assert response.status_code == 303
    assert response.headers["Location"].endswith("/")

    html = page(client.get("/"))
    assert "Сотрудник Алиса Примерова добавлен." in html
    assert "Алиса Примерова" in html
    assert "таймер не запущен" in html


def test_add_employee_with_empty_name_returns_400(client):
    response = add_employee(client, "   ")
    assert response.status_code == 400
    assert "Имя сотрудника не может быть пустым." in page(response)


def test_add_duplicate_employee_returns_409(client):
    add_employee(client, "Алиса")
    response = add_employee(client, "алиса")
    assert response.status_code == 409
    assert "уже есть в списке" in page(response)


# --- таймер -----------------------------------------------------------------


def test_start_and_stop_timer(client, app):
    add_employee(client, "Алиса")
    emp_id = employee_id(app, "Алиса")

    response = client.post(f"/employees/{emp_id}/timer/start", data={"description": "Макет"})
    assert response.status_code == 303
    html = page(client.get("/"))
    assert "Таймер запущен: «Макет»." in html
    assert "идёт" in html and "«Макет»" in html
    assert "Стоп" in html

    response = client.post(f"/employees/{emp_id}/timer/stop")
    assert response.status_code == 303
    html = page(client.get("/"))
    assert "Таймер остановлен: «Макет»" in html
    assert "таймер не запущен" in html


def test_start_timer_twice_returns_409_and_keeps_single_entry(client, app):
    """Повторный запуск через HTTP: 409, сообщение, запись одна."""
    add_employee(client, "Алиса")
    emp_id = employee_id(app, "Алиса")
    client.post(f"/employees/{emp_id}/timer/start", data={"description": "Первая"})

    response = client.post(f"/employees/{emp_id}/timer/start", data={"description": "Вторая"})
    assert response.status_code == 409
    html = page(response)
    assert "уже идёт таймер «Первая»" in html

    conn = connect(app.config["DATABASE"])
    try:
        rows = conn.execute("SELECT description, stopped_at FROM time_entries").fetchall()
    finally:
        conn.close()
    assert [(r["description"], r["stopped_at"]) for r in rows] == [("Первая", None)]


def test_start_timer_without_description_returns_400(client, app):
    add_employee(client, "Алиса")
    emp_id = employee_id(app, "Алиса")
    response = client.post(f"/employees/{emp_id}/timer/start", data={"description": ""})
    assert response.status_code == 400
    assert "Описание задачи не может быть пустым." in page(response)


def test_stop_timer_when_not_running_returns_409(client, app):
    add_employee(client, "Алиса")
    emp_id = employee_id(app, "Алиса")
    response = client.post(f"/employees/{emp_id}/timer/stop")
    assert response.status_code == 409
    assert "нет идущего таймера" in page(response)


def test_timer_for_unknown_employee_returns_404(client):
    assert client.post("/employees/999/timer/start", data={"description": "x"}).status_code == 404
    assert client.post("/employees/999/timer/stop").status_code == 404


# --- отчёт ------------------------------------------------------------------


def test_report_form_without_parameters(client):
    add_employee(client, "Алиса")
    response = client.get("/report")
    assert response.status_code == 200
    html = page(response)
    assert "— выберите —" in html
    assert "Общее время" not in html


def test_empty_report(client, app):
    add_employee(client, "Алиса")
    emp_id = employee_id(app, "Алиса")
    response = client.get(f"/report?employee_id={emp_id}&date_from=2026-03-10&date_to=2026-03-16")
    assert response.status_code == 200
    html = page(response)
    assert "Нет записей за выбранный период." in html
    assert "<strong>0:00:00</strong>" in html
    assert "10.03.2026 — 16.03.2026" in html


def test_report_with_entries_shows_rows_and_total(client, app):
    add_employee(client, "Алиса")
    emp_id = employee_id(app, "Алиса")
    conn = connect(app.config["DATABASE"])
    try:
        t = datetime(2026, 3, 10, 6, 0, tzinfo=UTC)  # 09:00 по Москве
        services.start_timer(conn, emp_id, "Макет", now=t)
        services.stop_timer(conn, emp_id, now=t + timedelta(hours=1, minutes=30))
        services.start_timer(conn, emp_id, "Созвон", now=t + timedelta(hours=2))
        services.stop_timer(conn, emp_id, now=t + timedelta(hours=2, minutes=45))
        services.start_timer(conn, emp_id, "Вне периода", now=t + timedelta(days=3))
        services.stop_timer(conn, emp_id, now=t + timedelta(days=3, hours=1))
    finally:
        conn.close()

    response = client.get(f"/report?employee_id={emp_id}&date_from=2026-03-10&date_to=2026-03-10")
    assert response.status_code == 200
    html = page(response)
    assert "<strong>2:15:00</strong>" in html
    assert "Макет" in html and "Созвон" in html
    assert "Вне периода" not in html
    assert "10.03.2026 09:00:00" in html  # время показано в поясе приложения


def test_report_with_running_timer(client, app):
    add_employee(client, "Алиса")
    emp_id = employee_id(app, "Алиса")
    client.post(f"/employees/{emp_id}/timer/start", data={"description": "Сейчас"})
    # «сегодня» берём в поясе приложения: системный пояс может отличаться
    today = datetime.now(ZoneInfo(app.config["TIMEZONE"])).date().isoformat()

    html = page(client.get(f"/report?employee_id={emp_id}&date_from={today}&date_to={today}"))
    assert "Сейчас" in html
    assert "(не учтено)" in html
    assert "<strong>0:00:00</strong>" in html


def test_report_with_invalid_date_returns_400(client, app):
    add_employee(client, "Алиса")
    emp_id = employee_id(app, "Алиса")
    response = client.get(f"/report?employee_id={emp_id}&date_from=10.03.2026&date_to=2026-03-10")
    assert response.status_code == 400
    assert "ГГГГ-ММ-ДД" in page(response)


def test_report_with_inverted_period_returns_400(client, app):
    add_employee(client, "Алиса")
    emp_id = employee_id(app, "Алиса")
    response = client.get(f"/report?employee_id={emp_id}&date_from=2026-03-11&date_to=2026-03-10")
    assert response.status_code == 400
    assert "не может быть позже" in page(response)


def test_report_for_unknown_employee_returns_404(client):
    response = client.get("/report?employee_id=999&date_from=2026-03-10&date_to=2026-03-10")
    assert response.status_code == 404
    assert "Сотрудник не найден." in page(response)


# --- сохранность данных -----------------------------------------------------


def test_data_survive_app_restart(make_app):
    """Создаём приложение заново на той же базе — данные на месте."""
    first = make_app()
    client = first.test_client()
    add_employee(client, "Алиса")
    emp_id = employee_id(first, "Алиса")
    client.post(f"/employees/{emp_id}/timer/start", data={"description": "Долгая задача"})

    second = make_app()  # «перезапуск»
    html = page(second.test_client().get("/"))
    assert "Алиса" in html
    assert "«Долгая задача»" in html
    assert "Стоп" in html


# --- некорректный ввод, найденный при ревью ---------------------------------


def test_report_with_extreme_dates_returns_400(client, app):
    """Даты на краях календаря не переводятся в UTC — должен быть 400, а не 500."""
    add_employee(client, "Алиса")
    emp_id = employee_id(app, "Алиса")
    for date_from, date_to in [("0001-01-01", "0001-01-02"), ("9999-12-30", "9999-12-31")]:
        response = client.get(
            f"/report?employee_id={emp_id}&date_from={date_from}&date_to={date_to}"
        )
        assert response.status_code == 400
        assert "вне допустимого диапазона" in page(response)


def test_huge_employee_id_returns_404(client):
    """Число больше int64 нельзя подставить в SQLite — должен быть 404, а не 500."""
    huge = 10**20
    assert (
        client.get(
            f"/report?employee_id={huge}&date_from=2026-03-10&date_to=2026-03-10"
        ).status_code
        == 404
    )
    assert (
        client.post(f"/employees/{huge}/timer/start", data={"description": "x"}).status_code == 404
    )
    assert client.post(f"/employees/{huge}/timer/stop").status_code == 404


def test_form_values_are_kept_after_error(client, app):
    """При ошибке введённый текст возвращается в поле формы."""
    add_employee(client, "Алиса")
    emp_id = employee_id(app, "Алиса")

    response = add_employee(client, "алиса")
    assert response.status_code == 409
    assert 'value="алиса"' in page(response)

    long_description = "x" * (services.MAX_DESCRIPTION_LENGTH + 1)
    response = client.post(
        f"/employees/{emp_id}/timer/start", data={"description": long_description}
    )
    assert response.status_code == 400
    assert f'value="{long_description}"' in page(response)
