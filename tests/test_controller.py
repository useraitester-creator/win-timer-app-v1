from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from win_timer_app.controller import (
    AppController,
    format_day_label,
    format_duration,
    format_hm,
)
from win_timer_app.models import TaskStatus
from win_timer_app.storage import Storage


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_format_duration() -> None:
    assert format_duration(0) == "00:00:00"
    assert format_duration(59) == "00:00:59"
    assert format_duration(3661) == "01:01:01"
    assert format_duration(36 * 3600) == "36:00:00"


def test_format_hm() -> None:
    assert format_hm(0) == "00:00"
    assert format_hm(59) == "00:00"
    assert format_hm(3661) == "01:01"
    assert format_hm(36 * 3600 + 59) == "36:00"


def test_format_day_label() -> None:
    assert format_day_label("2026-01-09") == "09.01.2026"


# ---------------------------------------------------------------------------
# Task lifecycle
# ---------------------------------------------------------------------------

def test_create_task(controller: AppController) -> None:
    task = controller.create_task("  Hello  ", "  desc  ")
    assert task.title == "Hello"
    assert task.description == "desc"
    assert task.status == TaskStatus.OPEN
    assert task.day == controller.today_str()
    assert task.active_session() is None


def test_create_task_start_now_opens_session(controller: AppController) -> None:
    task = controller.create_task("Work", start_now=True)
    assert task.status == TaskStatus.RUNNING
    assert task.active_session() is not None
    assert controller.active_task() is task


def test_start_stops_previous_active_task(controller: AppController) -> None:
    a = controller.create_task("A", start_now=True)
    b = controller.create_task("B")
    controller.start_task(b.id)

    a = controller.find_task(a.id)
    assert a.status == TaskStatus.PAUSED
    assert a.active_session() is None
    assert controller.active_task().id == b.id


def test_stop_task_sets_paused_and_closes_session(controller: AppController) -> None:
    task = controller.create_task("A", start_now=True)
    controller.stop_task(task.id)
    task = controller.find_task(task.id)
    assert task.status == TaskStatus.PAUSED
    assert task.active_session() is None
    assert controller.active_task() is None


def test_complete_task(controller: AppController) -> None:
    task = controller.create_task("A", start_now=True)
    controller.complete_task(task.id)
    task = controller.find_task(task.id)
    assert task.is_completed()
    assert task.completed_at is not None
    assert task.active_session() is None
    assert controller.active_task() is None


def test_resume_completed_task_restarts_it(controller: AppController) -> None:
    task = controller.create_task("A", start_now=True)
    controller.complete_task(task.id)
    controller.resume_completed_task(task.id)
    task = controller.find_task(task.id)
    assert task.status == TaskStatus.RUNNING
    assert task.completed_at is None
    assert controller.active_task().id == task.id


def test_delete_task(controller: AppController) -> None:
    a = controller.create_task("A", start_now=True)
    b = controller.create_task("B")
    controller.delete_task(a.id)
    with pytest.raises(KeyError):
        controller.find_task(a.id)
    assert controller.active_task() is None
    assert controller.find_task(b.id) is not None


def test_state_persists_across_controllers(storage: Storage) -> None:
    first = AppController(storage)
    task = first.create_task("Persisted", start_now=True)

    second = AppController(storage)
    reloaded = second.find_task(task.id)
    assert reloaded.title == "Persisted"
    assert second.active_task() is not None


# ---------------------------------------------------------------------------
# Filtering / grouping
# ---------------------------------------------------------------------------

def test_filter_open_only_toggle(controller: AppController) -> None:
    assert controller.filter_open_only() is False
    controller.set_filter_open_only(True)
    assert controller.filter_open_only() is True


def test_tasks_by_day_open_only_hides_completed(controller: AppController) -> None:
    a = controller.create_task("A")
    controller.create_task("B")
    controller.complete_task(a.id)

    grouped = controller.tasks_by_day(open_only=True)
    titles = [t.title for _, tasks in grouped for t in tasks]
    assert "A" not in titles
    assert "B" in titles


def test_day_total_seconds_sums_tasks(controller: AppController) -> None:
    task = controller.create_task("A")
    start = datetime(2026, 1, 1, 10, 0, 0)
    controller.add_session(task.id, start, start + timedelta(minutes=10))
    controller.add_session(task.id, start + timedelta(hours=1), start + timedelta(hours=1, minutes=5))
    assert controller.day_total_seconds(task.day) == 15 * 60


# ---------------------------------------------------------------------------
# Reminder interval
# ---------------------------------------------------------------------------

def test_reminder_interval_default(controller: AppController) -> None:
    assert controller.reminder_interval_minutes() == 40


def test_reminder_interval_is_clamped(controller: AppController) -> None:
    controller.set_reminder_interval_minutes(0)
    assert controller.reminder_interval_minutes() == 1
    controller.set_reminder_interval_minutes(10_000)
    assert controller.reminder_interval_minutes() == 24 * 60


def test_reminder_interval_handles_garbage_value(controller: AppController) -> None:
    controller.state.ui["reminder_interval_minutes"] = "not-a-number"
    assert controller.reminder_interval_minutes() == 40


# ---------------------------------------------------------------------------
# Reminder state machine
# ---------------------------------------------------------------------------

def test_check_reminders_idle_without_active_task(controller: AppController) -> None:
    status, task = controller.check_reminders()
    assert status == "idle"
    assert task is None


def test_check_reminders_running_before_interval(controller: AppController) -> None:
    controller.create_task("A", start_now=True)
    status, task = controller.check_reminders()
    assert status == "running"
    assert task is not None


def test_check_reminders_needs_confirmation_after_interval(controller: AppController) -> None:
    controller.create_task("A", start_now=True)
    controller.next_reminder_at = datetime.now() - timedelta(seconds=1)
    status, task = controller.check_reminders()
    assert status == "needs_confirmation"
    assert controller.pending_confirmation_task_id == task.id


def test_confirm_continue_reschedules_reminder(controller: AppController) -> None:
    task = controller.create_task("A", start_now=True)
    controller.next_reminder_at = datetime.now() - timedelta(seconds=1)
    controller.check_reminders()  # -> needs_confirmation
    controller.confirm_continue(task.id)
    assert controller.pending_confirmation_task_id is None
    assert controller.next_reminder_at > datetime.now()


def test_check_reminders_auto_stop_after_grace(controller: AppController) -> None:
    task = controller.create_task("A", start_now=True)
    controller.next_reminder_at = datetime.now() - timedelta(seconds=1)
    controller.check_reminders()  # -> needs_confirmation, sets deadline
    controller.pending_confirmation_deadline = datetime.now() - timedelta(seconds=1)
    status, stopped = controller.check_reminders()
    assert status == "auto_stopped"
    assert controller.find_task(task.id).status == TaskStatus.PAUSED
    assert controller.active_task() is None


def test_changing_interval_reschedules_running_reminder(controller: AppController) -> None:
    controller.create_task("A", start_now=True)
    controller.set_reminder_interval_minutes(5)
    assert controller.next_reminder_at <= datetime.now() + timedelta(minutes=5)


# ---------------------------------------------------------------------------
# Focus timer
# ---------------------------------------------------------------------------

def test_focus_timer_start_and_remaining(controller: AppController) -> None:
    controller.start_focus_timer(10)
    remaining = controller.focus_remaining_seconds()
    assert 9 * 60 < remaining <= 10 * 60
    assert controller.check_focus_timer()[0] == "running"


def test_focus_timer_stop(controller: AppController) -> None:
    controller.start_focus_timer(10)
    controller.stop_focus_timer()
    assert controller.focus_remaining_seconds() == 0
    assert controller.check_focus_timer()[0] == "idle"


def test_focus_timer_finishes_and_reports_duration(controller: AppController) -> None:
    controller.start_focus_timer(10)
    controller._focus_timer()["ends_at"] = (datetime.now() - timedelta(seconds=1)).isoformat()
    status, payload = controller.check_focus_timer()
    assert status == "finished"
    assert payload == 10
    # finishing clears the timer
    assert controller.check_focus_timer()[0] == "idle"


# ---------------------------------------------------------------------------
# Session editing
# ---------------------------------------------------------------------------

def test_add_session_validates_order(controller: AppController) -> None:
    task = controller.create_task("A")
    start = datetime(2026, 1, 1, 10, 0, 0)
    with pytest.raises(ValueError):
        controller.add_session(task.id, start, start)
    with pytest.raises(ValueError):
        controller.add_session(task.id, start, start - timedelta(minutes=1))


def test_add_session_keeps_sessions_sorted(controller: AppController) -> None:
    task = controller.create_task("A")
    base = datetime(2026, 1, 1, 10, 0, 0)
    controller.add_session(task.id, base + timedelta(hours=2), base + timedelta(hours=2, minutes=5))
    controller.add_session(task.id, base, base + timedelta(minutes=5))
    task = controller.find_task(task.id)
    starts = [s.started_at for s in task.sessions]
    assert starts == sorted(starts)


def test_update_session(controller: AppController) -> None:
    task = controller.create_task("A")
    start = datetime(2026, 1, 1, 10, 0, 0)
    session = controller.add_session(task.id, start, start + timedelta(minutes=10))
    controller.update_session(task.id, session.id, start, start + timedelta(minutes=30))
    task = controller.find_task(task.id)
    assert task.sessions[0].duration_seconds() == 30 * 60


def test_delete_session_of_running_task_pauses_it(controller: AppController) -> None:
    task = controller.create_task("A", start_now=True)
    running_session = task.active_session()
    controller.delete_session(task.id, running_session.id)
    task = controller.find_task(task.id)
    assert task.active_session() is None
    assert task.status in (TaskStatus.OPEN, TaskStatus.PAUSED)
    assert controller.active_task() is None


def test_delete_unknown_session_raises(controller: AppController) -> None:
    task = controller.create_task("A")
    with pytest.raises(KeyError):
        controller.delete_session(task.id, "does-not-exist")


# ---------------------------------------------------------------------------
# Daily rollover
# ---------------------------------------------------------------------------

def test_plan_rollover_closes_cross_midnight_active_session(controller: AppController) -> None:
    task = controller.create_task("Overnight", start_now=True)
    # Move the running session to a previous day; rollover should close it.
    task.active_session().started_at = "2020-01-01T23:00:00"
    controller.ensure_plan_rollover()

    task = controller.find_task(task.id)
    session = task.sessions[-1]
    assert session.ended_at is not None
    assert session.ended_at.startswith("2020-01-01T23:59:59")
    assert task.status == TaskStatus.PAUSED
