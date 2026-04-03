from __future__ import annotations

from datetime import date, datetime, time, timedelta

from .models import Session, Task, TaskStatus, make_id
from .storage import Storage


class AppController:
    reminder_interval = timedelta(minutes=40)
    reminder_grace = timedelta(minutes=5)

    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.state = storage.load()
        self.pending_confirmation_task_id: str | None = None
        self.pending_confirmation_deadline: datetime | None = None
        self.next_reminder_at: datetime | None = None
        self.ensure_rollover()
        self._rebuild_runtime_state()

    def _focus_timer(self) -> dict[str, object]:
        focus_timer = self.state.ui.setdefault("focus_timer", {})
        focus_timer.setdefault("selected_minutes", 20)
        focus_timer.setdefault("duration_minutes", None)
        focus_timer.setdefault("ends_at", None)
        return focus_timer

    def save(self) -> None:
        self.storage.save(self.state)

    def today_str(self) -> str:
        return date.today().isoformat()

    def ensure_rollover(self) -> None:
        today = self.today_str()
        self._close_cross_day_active_task(today)
        existing_today = {task.continuation_of for task in self.state.tasks if task.day == today}
        latest_by_title: dict[str, Task] = {}
        for task in sorted(self.state.tasks, key=lambda item: (item.day, item.created_at)):
            latest_by_title[self._base_title(task.title)] = task
        changed = False
        for task in list(latest_by_title.values()):
            if task.day == today or task.is_completed():
                continue
            if task.id in existing_today:
                continue
            continuation = Task(
                id=make_id(),
                day=today,
                title=f"{self._base_title(task.title)} (продолжение)",
                description=task.description,
                status=TaskStatus.OPEN,
                continuation_of=task.id,
            )
            self.state.tasks.append(continuation)
            changed = True
        if changed:
            self.save()

    def _close_cross_day_active_task(self, today: str) -> None:
        active = self.active_task()
        if active is None or active.day == today:
            return
        session = active.active_session()
        if session is None:
            return
        previous_day = date.fromisoformat(active.day)
        session.ended_at = datetime.combine(previous_day, time(23, 59, 59)).isoformat()
        active.status = TaskStatus.PAUSED
        self.pending_confirmation_task_id = None
        self.pending_confirmation_deadline = None
        self.next_reminder_at = None
        self.save()

    def _base_title(self, title: str) -> str:
        suffix = " (продолжение)"
        if title.endswith(suffix):
            return title[: -len(suffix)]
        return title

    def _rebuild_runtime_state(self) -> None:
        active = self.active_task()
        if active and active.active_session():
            self.next_reminder_at = active.active_session().start_dt + self.reminder_interval
        else:
            self.next_reminder_at = None

    def all_tasks(self) -> list[Task]:
        self.ensure_rollover()
        return sorted(self.state.tasks, key=lambda task: (task.day, task.created_at), reverse=True)

    def tasks_by_day(self, open_only: bool = False) -> list[tuple[str, list[Task]]]:
        grouped: dict[str, list[Task]] = {}
        for task in self.all_tasks():
            if open_only and task.is_completed():
                continue
            grouped.setdefault(task.day, []).append(task)
        return sorted(grouped.items(), key=lambda item: item[0], reverse=True)

    def day_total_seconds(self, day: str) -> int:
        now = datetime.now()
        return sum(task.total_seconds(now=now) for task in self.state.tasks if task.day == day)

    def find_task(self, task_id: str) -> Task:
        for task in self.state.tasks:
            if task.id == task_id:
                return task
        raise KeyError(task_id)

    def create_task(self, title: str, description: str = "", start_now: bool = False) -> Task:
        task = Task(
            id=make_id(),
            day=self.today_str(),
            title=title.strip(),
            description=description.strip(),
            status=TaskStatus.OPEN,
        )
        self.state.tasks.append(task)
        self.save()
        if start_now:
            self.start_task(task.id)
        return task

    def active_task(self) -> Task | None:
        for task in self.state.tasks:
            if task.status == TaskStatus.RUNNING and task.active_session():
                return task
        return None

    def start_task(self, task_id: str) -> Task:
        now = datetime.now()
        current = self.active_task()
        if current and current.id != task_id:
            self.stop_task(current.id, now=now)
        task = self.find_task(task_id)
        if task.is_completed():
            task.status = TaskStatus.OPEN
            task.completed_at = None
        if task.active_session() is None:
            task.sessions.append(Session(id=make_id(), started_at=now.isoformat()))
        task.status = TaskStatus.RUNNING
        self.pending_confirmation_task_id = None
        self.pending_confirmation_deadline = None
        self.next_reminder_at = now + self.reminder_interval
        self.save()
        return task

    def stop_task(self, task_id: str, now: datetime | None = None) -> Task:
        now = now or datetime.now()
        task = self.find_task(task_id)
        session = task.active_session()
        if session and session.ended_at is None:
            session.ended_at = now.isoformat()
        if task.status != TaskStatus.COMPLETED:
            task.status = TaskStatus.PAUSED if task.sessions else TaskStatus.OPEN
        if self.pending_confirmation_task_id == task_id:
            self.pending_confirmation_task_id = None
            self.pending_confirmation_deadline = None
        if self.active_task() is None:
            self.next_reminder_at = None
        self.save()
        return task

    def complete_task(self, task_id: str) -> Task:
        task = self.find_task(task_id)
        if task.active_session():
            self.stop_task(task_id)
        task.status = TaskStatus.COMPLETED
        task.completed_at = datetime.now().isoformat()
        self.pending_confirmation_task_id = None
        self.pending_confirmation_deadline = None
        if self.active_task() is None:
            self.next_reminder_at = None
        self.save()
        return task

    def resume_completed_task(self, task_id: str) -> Task:
        task = self.find_task(task_id)
        task.status = TaskStatus.OPEN
        task.completed_at = None
        self.save()
        return self.start_task(task_id)

    def set_filter_open_only(self, value: bool) -> None:
        self.state.ui["filter_open_only"] = value
        self.save()

    def filter_open_only(self) -> bool:
        return bool(self.state.ui.get("filter_open_only", False))

    def focus_timer_state(self) -> dict[str, object]:
        return dict(self._focus_timer())

    def start_focus_timer(self, minutes: int) -> None:
        focus_timer = self._focus_timer()
        focus_timer["selected_minutes"] = minutes
        focus_timer["duration_minutes"] = minutes
        focus_timer["ends_at"] = (datetime.now() + timedelta(minutes=minutes)).isoformat()
        self.save()

    def stop_focus_timer(self) -> None:
        focus_timer = self._focus_timer()
        focus_timer["ends_at"] = None
        focus_timer["duration_minutes"] = None
        self.save()

    def focus_remaining_seconds(self) -> int:
        focus_timer = self._focus_timer()
        ends_at = focus_timer.get("ends_at")
        if not ends_at:
            return 0
        end_dt = datetime.fromisoformat(str(ends_at))
        return max(0, int((end_dt - datetime.now()).total_seconds()))

    def check_focus_timer(self) -> tuple[str, int | None]:
        focus_timer = self._focus_timer()
        ends_at = focus_timer.get("ends_at")
        if not ends_at:
            return ("idle", None)
        end_dt = datetime.fromisoformat(str(ends_at))
        if datetime.now() >= end_dt:
            duration_minutes = focus_timer.get("duration_minutes")
            focus_timer["ends_at"] = None
            focus_timer["duration_minutes"] = None
            self.save()
            if isinstance(duration_minutes, int):
                return ("finished", duration_minutes)
            return ("finished", None)
        return ("running", self.focus_remaining_seconds())

    def check_reminders(self) -> tuple[str, Task | None]:
        now = datetime.now()
        self.ensure_rollover()
        active = self.active_task()
        if active is None:
            self.pending_confirmation_task_id = None
            self.pending_confirmation_deadline = None
            self.next_reminder_at = None
            return ("idle", None)

        if self.pending_confirmation_task_id == active.id and self.pending_confirmation_deadline:
            if now >= self.pending_confirmation_deadline:
                self.stop_task(active.id, now=now)
                return ("auto_stopped", active)
            return ("awaiting_confirmation", active)

        if self.next_reminder_at and now >= self.next_reminder_at:
            self.pending_confirmation_task_id = active.id
            self.pending_confirmation_deadline = now + self.reminder_grace
            self.save()
            return ("needs_confirmation", active)

        if self.next_reminder_at is None:
            started_at = active.active_session().start_dt if active.active_session() else now
            self.next_reminder_at = started_at + self.reminder_interval
        return ("running", active)

    def confirm_continue(self, task_id: str) -> None:
        task = self.find_task(task_id)
        if task.status != TaskStatus.RUNNING:
            return
        self.pending_confirmation_task_id = None
        self.pending_confirmation_deadline = None
        self.next_reminder_at = datetime.now() + self.reminder_interval
        self.save()

    def update_session(self, task_id: str, session_id: str, started_at: datetime, ended_at: datetime) -> None:
        if ended_at <= started_at:
            raise ValueError("Время окончания должно быть позже начала.")
        task = self.find_task(task_id)
        for session in task.sessions:
            if session.id == session_id:
                session.started_at = started_at.isoformat()
                session.ended_at = ended_at.isoformat()
                break
        else:
            raise KeyError(session_id)
        task.sessions.sort(key=lambda item: item.started_at)
        self.save()

    def task_elapsed_text(self, task: Task) -> str:
        return format_duration(task.total_seconds(datetime.now()))


def format_duration(total_seconds: int) -> str:
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_day_label(day_iso: str) -> str:
    parsed = date.fromisoformat(day_iso)
    return parsed.strftime("%d.%m.%Y")
