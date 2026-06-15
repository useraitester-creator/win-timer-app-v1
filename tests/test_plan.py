from __future__ import annotations

from datetime import datetime

from win_timer_app.controller import AppController
from win_timer_app.models import Session, Task, TaskStatus, make_id
from win_timer_app.storage import AppState


def _sess(day: str, start="10:00:00", end="11:00:00") -> Session:
    return Session(id=make_id(), started_at=f"{day}T{start}", ended_at=f"{day}T{end}")


def _add(controller, title, *, planned_days=None, status=TaskStatus.OPEN, sessions=None, day=None):
    task = Task(
        id=make_id(),
        day=day or controller.today_str(),
        title=title,
        status=status,
        planned_days=list(planned_days or []),
        sessions=list(sessions or []),
    )
    controller.state.tasks.append(task)
    return task


# --- time per day -------------------------------------------------------------

def test_today_seconds_counts_only_today_sessions(controller):
    today, yesterday = "2026-06-11", "2026-06-10"
    task = _add(
        controller,
        "T",
        sessions=[_sess(today, "10:00:00", "10:30:00"), _sess(yesterday, "09:00:00", "10:00:00")],
    )
    assert controller.today_seconds(task, today) == 1800
    assert controller.today_total_seconds(today) == 1800
    assert task.total_seconds() == 1800 + 3600


# --- views --------------------------------------------------------------------

def test_created_and_imported_tasks_land_in_today_plan(controller):
    created = controller.create_task("Новая")
    assert controller.in_today_plan(created)
    controller.import_bitrix_items([{"source": "project", "id": "1", "title": "Проект"}])
    assert "Проект" in {t.title for t in controller.tasks_today_plan()}


def test_mark_sessions_transferred_persists(storage):
    controller = AppController(storage)
    task = controller.create_task("T")
    s1 = controller.add_session(task.id, datetime(2026, 6, 11, 10, 0, 0), datetime(2026, 6, 11, 11, 0, 0))
    s2 = controller.add_session(task.id, datetime(2026, 6, 11, 12, 0, 0), datetime(2026, 6, 11, 12, 30, 0))
    controller.mark_sessions_transferred(task.id, [s1.id], "999")
    reloaded = {s.id: s for s in AppController(storage).find_task(task.id).sessions}
    assert reloaded[s1.id].bitrix_record_id == "999"
    assert reloaded[s2.id].bitrix_record_id is None


def test_tasks_on_date_lists_only_tasks_worked_that_day(controller):
    d1, d2 = "2026-06-10", "2026-06-09"
    _add(controller, "A", sessions=[_sess(d1, "10:00:00", "10:30:00")])
    _add(controller, "B", sessions=[_sess(d2, "10:00:00", "11:00:00")])
    _add(controller, "C", sessions=[])  # no tracked time
    assert {t.title for t in controller.tasks_on_date(d1)} == {"A"}
    assert {t.title for t in controller.tasks_on_date(d2)} == {"B"}
    assert controller.tasks_on_date("2026-01-01") == []


def test_views_filter_tasks(controller):
    today = controller.today_str()
    _add(controller, "A", planned_days=[today], status=TaskStatus.OPEN)
    _add(controller, "B", planned_days=[], status=TaskStatus.OPEN)
    _add(controller, "C", planned_days=[today], status=TaskStatus.COMPLETED)
    assert {t.title for t in controller.tasks_all()} == {"A", "B", "C"}
    assert {t.title for t in controller.tasks_in_progress()} == {"A", "B"}
    assert {t.title for t in controller.tasks_today_plan(today)} == {"A", "C"}


# --- add / remove from plan ---------------------------------------------------

def test_add_and_remove_from_plan_idempotent(controller):
    today = controller.today_str()
    task = _add(controller, "T", planned_days=[])
    assert not controller.in_today_plan(task, today)
    controller.add_to_plan(task.id, today)
    controller.add_to_plan(task.id, today)  # idempotent
    assert controller.find_task(task.id).planned_days.count(today) == 1
    controller.remove_from_plan(task.id, today)
    assert not controller.in_today_plan(controller.find_task(task.id), today)


# --- plan rollover ------------------------------------------------------------

def test_plan_rollover_carries_unfinished_from_yesterday(controller):
    today, yesterday = "2026-06-11", "2026-06-10"
    a = _add(controller, "A", planned_days=[yesterday], status=TaskStatus.OPEN)
    b = _add(controller, "B", planned_days=[yesterday], status=TaskStatus.COMPLETED)
    c = _add(controller, "C", planned_days=[], status=TaskStatus.OPEN)
    controller.state.ui["plan_rollover_day"] = yesterday
    controller.ensure_plan_rollover(today)
    assert today in controller.find_task(a.id).planned_days       # carried
    assert today not in controller.find_task(b.id).planned_days   # completed
    assert today not in controller.find_task(c.id).planned_days   # not in yesterday's plan


def test_plan_rollover_runs_once_per_day(controller):
    today, yesterday = "2026-06-11", "2026-06-10"
    a = _add(controller, "A", planned_days=[yesterday], status=TaskStatus.OPEN)
    controller.state.ui["plan_rollover_day"] = yesterday
    controller.ensure_plan_rollover(today)
    controller.remove_from_plan(a.id, today)
    controller.ensure_plan_rollover(today)  # guarded: must not re-add
    assert today not in controller.find_task(a.id).planned_days


# --- migration ----------------------------------------------------------------

def test_migration_collapses_continuation_chain(storage):
    state = AppState()
    state.tasks = [
        Task(
            id="A",
            day="2026-06-09",
            title="Задача",
            status=TaskStatus.PAUSED,
            sessions=[_sess("2026-06-09", "10:00:00", "11:00:00")],
        ),
        Task(
            id="B",
            day="2026-06-10",
            title="Задача (продолжение)",
            status=TaskStatus.OPEN,
            continuation_of="A",
            sessions=[_sess("2026-06-10", "10:00:00", "10:30:00")],
        ),
    ]
    storage.save(state)

    controller = AppController(storage)  # _migrate runs in __init__
    tasks = controller.all_tasks()
    assert len(tasks) == 1
    root = tasks[0]
    assert root.title == "Задача"
    assert root.total_seconds() == 3600 + 1800  # no sessions lost
    assert {"2026-06-09", "2026-06-10"} <= set(root.planned_days)
    assert int(controller.state.ui["schema_version"]) == 2


def test_migration_sets_planned_days_for_plain_tasks(storage):
    state = AppState()
    state.tasks = [Task(id="X", day="2026-06-11", title="Одиночная", status=TaskStatus.OPEN)]
    storage.save(state)
    controller = AppController(storage)
    assert controller.find_task("X").planned_days == ["2026-06-11"]
