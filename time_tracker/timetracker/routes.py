"""HTTP-слой: маршруты и шаблоны. Вся логика — в ``services``."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from . import services
from .db import get_db
from .services import TimeTrackerError, ValidationError

bp = Blueprint("tracker", __name__)

REPORT_DEFAULT_DAYS = 7  # период отчёта по умолчанию: последняя неделя


def app_tz() -> ZoneInfo:
    return ZoneInfo(current_app.config["TIMEZONE"])


# --- фильтры для шаблонов ---------------------------------------------------


@bp.app_template_filter("local_dt")
def local_dt(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M:%S") -> str:
    """UTC из базы -> время в поясе приложения."""
    if value is None:
        return "—"
    return value.astimezone(app_tz()).strftime(fmt)


@bp.app_template_filter("duration")
def duration(seconds: int) -> str:
    return services.format_duration(seconds)


# --- главная: сотрудники и таймеры ------------------------------------------


def _render_index(status: int = 200, error: str | None = None, form_values: dict | None = None):
    """``form_values`` — что пользователь ввёл; при ошибке возвращаем это в поля формы."""
    statuses = services.list_employees(get_db())
    return render_template(
        "index.html", statuses=statuses, error=error, form_values=form_values or {}
    ), status


@bp.get("/")
def index():
    return _render_index()


@bp.post("/employees")
def add_employee():
    try:
        employee = services.add_employee(get_db(), request.form.get("name"))
    except TimeTrackerError as exc:
        return _render_index(exc.status_code, str(exc), {"name": request.form.get("name", "")})
    flash(f"Сотрудник {employee.name} добавлен.", "success")
    return redirect(url_for("tracker.index"), code=303)


@bp.post("/employees/<int:employee_id>/timer/start")
def start_timer(employee_id: int):
    try:
        entry = services.start_timer(get_db(), employee_id, request.form.get("description"))
    except TimeTrackerError as exc:
        return _render_index(
            exc.status_code,
            str(exc),
            {"employee_id": employee_id, "description": request.form.get("description", "")},
        )
    flash(f"Таймер запущен: «{entry.description}».", "success")
    return redirect(url_for("tracker.index"), code=303)


@bp.post("/employees/<int:employee_id>/timer/stop")
def stop_timer(employee_id: int):
    try:
        entry = services.stop_timer(get_db(), employee_id)
    except TimeTrackerError as exc:
        return _render_index(exc.status_code, str(exc))
    flash(
        f"Таймер остановлен: «{entry.description}», "
        f"{services.format_duration(entry.duration_seconds())}.",
        "success",
    )
    return redirect(url_for("tracker.index"), code=303)


# --- отчёт ------------------------------------------------------------------


def _parse_int(value: str, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field}: некорректное значение.") from exc


def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field}: укажите дату в формате ГГГГ-ММ-ДД.") from exc


@bp.get("/report")
def report():
    conn = get_db()
    employees = services.all_employees(conn)

    today = datetime.now(app_tz()).date()
    form = {
        "employee_id": request.args.get("employee_id", ""),
        "date_from": request.args.get(
            "date_from", (today - timedelta(days=REPORT_DEFAULT_DAYS - 1)).isoformat()
        ),
        "date_to": request.args.get("date_to", today.isoformat()),
    }
    context = {"employees": employees, "form": form, "report": None, "error": None}

    if not form["employee_id"]:
        # Форма открыта впервые — просто показываем её.
        return render_template("report.html", **context)

    try:
        employee_id = _parse_int(form["employee_id"], "Сотрудник")
        date_from = _parse_date(form["date_from"], "Начало периода")
        date_to = _parse_date(form["date_to"], "Конец периода")
        context["report"] = services.build_report(conn, employee_id, date_from, date_to, app_tz())
    except TimeTrackerError as exc:
        context["error"] = str(exc)
        return render_template("report.html", **context), exc.status_code
    return render_template("report.html", **context)
