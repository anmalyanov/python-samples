"""HTTP-слой: маршруты, фильтры и контекст шаблонов. Вся логика — в ``services``."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from . import services
from .db import get_db
from .services import Report, TimeTrackerError, ValidationError

bp = Blueprint("tracker", __name__)

REPORT_DEFAULT_DAYS = 7  # период отчёта по умолчанию: последняя неделя
MAX_TIMELINE_DAYS = 31  # дольше — шкала по дням не помещается, остаётся таблица

MONTHS_GENITIVE = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]  # fmt: skip
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def app_tz() -> ZoneInfo:
    return ZoneInfo(current_app.config["TIMEZONE"])


# --- контекст и фильтры шаблонов --------------------------------------------


@bp.app_context_processor
def inject_clock() -> dict:
    """Текущее время для живых часов в шапке и счётчиков идущих таймеров."""
    now = services.utcnow()
    return {"now_utc": now, "now_local": now.astimezone(app_tz())}


@bp.app_template_filter("local_dt")
def local_dt(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M:%S") -> str:
    """UTC из базы -> время в поясе приложения."""
    if value is None:
        return "—"
    return value.astimezone(app_tz()).strftime(fmt)


@bp.app_template_filter("duration")
def duration(seconds: int) -> str:
    return services.format_duration(seconds)


@bp.app_template_filter("weekday_ru")
def weekday_ru(value: date) -> str:
    return WEEKDAYS_SHORT[value.weekday()]


@bp.app_template_filter("date_ru")
def date_ru(value: date, with_year: bool = True) -> str:
    text = f"{value.day} {MONTHS_GENITIVE[value.month - 1]}"
    return f"{text} {value.year}" if with_year else text


def format_period(date_from: date, date_to: date) -> str:
    """«10 марта 2026», «10–16 марта 2026», «27 октября – 2 ноября 2026»."""
    if date_from == date_to:
        return date_ru(date_from)
    if (date_from.year, date_from.month) == (date_to.year, date_to.month):
        return f"{date_from.day}–{date_to.day} {MONTHS_GENITIVE[date_to.month - 1]} {date_to.year}"
    if date_from.year == date_to.year:
        return f"{date_ru(date_from, with_year=False)} – {date_ru(date_to)}"
    return f"{date_ru(date_from)} – {date_ru(date_to)}"


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


def _report_form(args) -> dict[str, str]:
    """Параметры отчёта из query string с значениями по умолчанию (последняя неделя)."""
    today = datetime.now(app_tz()).date()
    default_from = today - timedelta(days=REPORT_DEFAULT_DAYS - 1)
    return {
        "employee_id": args.get("employee_id", ""),
        "date_from": args.get("date_from", default_from.isoformat()),
        "date_to": args.get("date_to", today.isoformat()),
    }


def _build_report(form: dict[str, str]) -> Report:
    employee_id = _parse_int(form["employee_id"], "Сотрудник")
    date_from = _parse_date(form["date_from"], "Начало периода")
    date_to = _parse_date(form["date_to"], "Конец периода")
    return services.build_report(get_db(), employee_id, date_from, date_to, app_tz())


@bp.get("/report")
def report():
    form = _report_form(request.args)
    context = {
        "employees": services.all_employees(get_db()),
        "form": form,
        "report": None,
        "days": None,
        "timeline_skipped": False,
        "period_label": "",
        "error": None,
    }
    if not form["employee_id"]:
        # Форма открыта впервые — просто показываем её.
        return render_template("report.html", **context)

    try:
        rep = _build_report(form)
    except TimeTrackerError as exc:
        context["error"] = str(exc)
        return render_template("report.html", **context), exc.status_code

    context["report"] = rep
    context["period_label"] = format_period(rep.date_from, rep.date_to)
    if (rep.date_to - rep.date_from).days < MAX_TIMELINE_DAYS:
        context["days"] = services.daily_breakdown(rep, app_tz())
    else:
        context["timeline_skipped"] = True
    return render_template("report.html", **context)


@bp.get("/report.csv")
def report_csv():
    try:
        rep = _build_report(_report_form(request.args))
    except TimeTrackerError as exc:
        return str(exc), exc.status_code, {"Content-Type": "text/plain; charset=utf-8"}

    # BOM нужен, чтобы Excel сразу понял кодировку UTF-8.
    body = "﻿" + services.report_to_csv(rep, app_tz())
    filename = f"report_{rep.employee.id}_{rep.date_from}_{rep.date_to}.csv"
    return Response(
        body,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
