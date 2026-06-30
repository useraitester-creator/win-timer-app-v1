"""Focus session tasks: tracked like regular tasks with auto-generated titles."""
from __future__ import annotations

from datetime import datetime

from .models import Session, Task, TaskStatus, make_id


FOCUS_TASK_DESCRIPTION = "Режим концентрации"


def focus_session_title(minutes: int, *, now: datetime | None = None) -> str:
    now = now or datetime.now()
    stamp = now.strftime("%d.%m.%Y %H:%M")
    return f"Концентрация · {minutes} мин · {stamp}"


def create_focus_session_task(
    tasks: list[Task],
    today: str,
    minutes: int,
    *,
    now: datetime | None = None,
) -> Task:
    now = now or datetime.now()
    task = Task(
        id=make_id(),
        day=today,
        title=focus_session_title(minutes, now=now),
        description=FOCUS_TASK_DESCRIPTION,
        status=TaskStatus.OPEN,
        planned_days=[today],
        sessions=[Session(id=make_id(), started_at=now.isoformat())],
    )
    tasks.append(task)
    return task


def finish_focus_session_task(task: Task, *, now: datetime | None = None) -> None:
    now = now or datetime.now()
    session = task.active_session()
    if session is not None and session.ended_at is None:
        session.ended_at = now.isoformat()
    if not task.is_completed():
        task.status = TaskStatus.COMPLETED
        task.completed_at = now.isoformat()


def is_focus_session_task(task: Task) -> bool:
    return task.description.strip() == FOCUS_TASK_DESCRIPTION
