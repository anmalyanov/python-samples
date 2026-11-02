"""Шкала по дням: разложение записей по календарным дням в поясе приложения."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from hypothesis import given, settings
from hypothesis import strategies as st

from timetracker import services
from timetracker.services import Report, TimeEntry, daily_breakdown

MSK = ZoneInfo("Europe/Moscow")
BERLIN = ZoneInfo("Europe/Berlin")
EMPLOYEE = services.Employee(id=1, name="Алиса")


def entry(entry_id, start: datetime, minutes: int | None, description="Задача") -> TimeEntry:
    """Запись длиной ``minutes``; ``None`` — идущий таймер."""
    start = services.normalize(start)
    stop = services.normalize(start + timedelta(minutes=minutes)) if minutes is not None else None
    return TimeEntry(entry_id, EMPLOYEE.id, description, start, stop)


def report(entries, date_from: date, date_to: date) -> Report:
    total = sum(e.duration_seconds() for e in entries if not e.is_running)
    return Report(EMPLOYEE, date_from, date_to, list(entries), total)


def test_every_day_of_period_is_present_even_if_empty():
    days = daily_breakdown(report([], date(2026, 3, 9), date(2026, 3, 15)), MSK)
    assert [d.day for d in days] == [date(2026, 3, 9) + timedelta(days=i) for i in range(7)]
    assert all(d.is_empty and d.total_seconds == 0 for d in days)
    assert [d.is_weekend for d in days] == [False] * 5 + [True, True]


def test_segment_positions_are_fractions_of_the_local_day():
    e = entry(1, datetime(2026, 3, 10, 6, 0, tzinfo=MSK), 180)  # 06:00–09:00 по Москве
    [day] = daily_breakdown(report([e], date(2026, 3, 10), date(2026, 3, 10)), MSK)
    [seg] = day.segments
    assert seg.start == 0.25
    assert seg.end == 0.375
    assert not seg.continues_next_day
    assert day.total_seconds == 3 * 3600


def test_entry_crossing_midnight_belongs_to_start_day():
    e = entry(1, datetime(2026, 3, 10, 23, 30, tzinfo=MSK), 90)  # до 01:00 следующего дня
    days = daily_breakdown(report([e], date(2026, 3, 10), date(2026, 3, 11)), MSK)
    first, second = days
    [seg] = first.segments
    assert seg.start == 23.5 / 24
    assert seg.end == 1.0  # отрезок обрезан по полуночи
    assert seg.continues_next_day
    assert first.total_seconds == 90 * 60  # а длительность учтена целиком
    assert second.is_empty  # на следующий день ничего не переносится


def test_running_entry_ends_at_now_and_is_marked_running():
    e = entry(1, datetime(2026, 3, 10, 12, 0, tzinfo=MSK), None)
    now = datetime(2026, 3, 10, 18, 0, tzinfo=MSK)
    [day] = daily_breakdown(report([e], date(2026, 3, 10), date(2026, 3, 10)), MSK, now=now)
    [seg] = day.segments
    assert seg.entry.is_running
    assert seg.start == 0.5
    assert seg.end == 0.75
    assert day.total_seconds == 0  # идущий таймер в итог не входит


def test_segments_sorted_by_start_within_day():
    late = entry(1, datetime(2026, 3, 10, 15, 0, tzinfo=MSK), 30)
    early = entry(2, datetime(2026, 3, 10, 9, 0, tzinfo=MSK), 30)
    [day] = daily_breakdown(report([late, early], date(2026, 3, 10), date(2026, 3, 10)), MSK)
    assert [s.entry.id for s in day.segments] == [2, 1]


def test_day_with_clock_change_has_real_length():
    """29.03.2026 в Берлине 23 часа: 02:00 сразу становится 03:00.

    12:00 по стене — это 11 реальных часов после полуночи, поэтому отрезок
    начинается на 11/23 дорожки, а не на половине. Наивное вычитание
    datetime с одним tzinfo дало бы 12/24.
    """
    e = entry(1, datetime(2026, 3, 29, 12, 0, tzinfo=BERLIN), 60)
    [day] = daily_breakdown(report([e], date(2026, 3, 29), date(2026, 3, 29)), BERLIN)
    [seg] = day.segments
    assert abs(seg.start - 11 / 23) < 1e-9
    assert abs(seg.end - 12 / 23) < 1e-9


def test_utc_entry_lands_on_local_date_not_utc_date():
    # 23:30 UTC 9 марта — это уже 02:30 10 марта по Москве
    e = entry(1, datetime(2026, 3, 9, 23, 30, tzinfo=UTC), 30)
    days = daily_breakdown(report([e], date(2026, 3, 9), date(2026, 3, 10)), MSK)
    assert days[0].is_empty
    assert len(days[1].segments) == 1


# --- property-based: инварианты для случайных наборов записей ----------------

PERIOD_FROM = date(2026, 3, 26)  # неделя с переводом часов в Берлине (29.03)
PERIOD_TO = date(2026, 4, 1)
NOW = datetime(2026, 4, 2, 12, 0, tzinfo=BERLIN)
PERIOD_START = datetime.combine(PERIOD_FROM, datetime.min.time(), tzinfo=BERLIN)

random_entries = st.lists(
    st.tuples(
        st.integers(min_value=0, max_value=7 * 24 * 60 - 1),  # минут от начала периода
        st.one_of(st.none(), st.integers(min_value=0, max_value=48 * 60)),  # длительность
    ),
    max_size=15,
)


@settings(max_examples=200)
@given(random_entries)
def test_daily_breakdown_invariants(raw):
    entries = [
        entry(i + 1, PERIOD_START + timedelta(minutes=offset), minutes)
        for i, (offset, minutes) in enumerate(raw)
    ]
    rep = report(entries, PERIOD_FROM, PERIOD_TO)
    days = daily_breakdown(rep, BERLIN, now=NOW)

    # каждый день периода ровно один раз, по порядку
    assert [d.day for d in days] == [PERIOD_FROM + timedelta(days=i) for i in range(7)]

    seen = []
    for day in days:
        starts = [s.start for s in day.segments]
        assert starts == sorted(starts)
        for seg in day.segments:
            seen.append(seg.entry.id)
            assert 0.0 <= seg.start <= seg.end <= 1.0
            assert seg.entry.started_at.astimezone(BERLIN).date() == day.day
            local_end = (seg.entry.stopped_at or services.normalize(NOW)).astimezone(BERLIN)
            assert seg.continues_next_day == (local_end.date() > day.day)
        assert day.total_seconds == sum(
            s.entry.duration_seconds() for s in day.segments if not s.entry.is_running
        )

    # каждая запись попала ровно в один день, итоги по дням сходятся с отчётом
    assert sorted(seen) == [e.id for e in entries]
    assert sum(d.total_seconds for d in days) == rep.total_seconds
