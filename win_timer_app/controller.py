from __future__ import annotations

from datetime import date, datetime, time, timedelta

from .models import Session, Task, TaskStatus, make_id
from .storage import Storage


class AppController:
    reminder_grace = timedelta(minutes=5)
    _reminder_interval_min_clamp = 1
    _reminder_interval_max_clamp = 24 * 60

    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.state = storage.load()
        self.pending_confirmation_task_id: str | None = None
        self.pending_confirmation_deadline: datetime | None = None
        self.next_reminder_at: datetime | None = None
        self._migrate()
        self.ensure_plan_rollover()
        self.state.ui.setdefault("reminder_interval_minutes", 40)
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

    def _migrate(self) -> None:
        """One-time migration to the persistent-task + plan model (schema v2)."""
        if int(self.state.ui.get("schema_version", 1)) >= 2:
            return
        self._collapse_continuations()
        for task in self.state.tasks:
            if not task.planned_days:
                task.planned_days = [task.day]
        self.state.ui["schema_version"] = 2
        self.state.ui["plan_rollover_day"] = self.today_str()
        self.save()

    def _collapse_continuations(self) -> None:
        """Merge old '(продолжение)' chains into their root task, losing no sessions."""
        by_id = {task.id: task for task in self.state.tasks}

        def root_of(task: Task) -> Task:
            seen: set[str] = set()
            while task.continuation_of and task.continuation_of in by_id and task.id not in seen:
                seen.add(task.id)
                task = by_id[task.continuation_of]
            return task

        chains: dict[str, list[Task]] = {}
        for task in self.state.tasks:
            chains.setdefault(root_of(task).id, []).append(task)

        survivors: list[Task] = []
        for root_id, members in chains.items():
            root = by_id[root_id]
            members.sort(key=lambda item: item.day)
            for member in members:
                if member.id != root.id:
                    root.sessions.extend(member.sessions)
            latest = members[-1]
            root.status = latest.status
            root.completed_at = latest.completed_at if latest.is_completed() else None
            root.title = self._strip_continuation_suffix(root.title)
            root.sessions.sort(key=lambda item: item.started_at)
            root.planned_days = sorted({m.day for m in members} | set(root.planned_days or []))
            survivors.append(root)
        self.state.tasks = survivors

    @staticmethod
    def _strip_continuation_suffix(title: str) -> str:
        suffix = " (продолжение)"
        while title.endswith(suffix):
            title = title[: -len(suffix)]
        return title

    def ensure_plan_rollover(self, today: str | None = None) -> None:
        """Carry yesterday's unfinished plan into today's plan (once per day)."""
        today = today or self.today_str()
        self._close_cross_day_active_task(today)
        if self.state.ui.get("plan_rollover_day") == today:
            return
        yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
        for task in self.state.tasks:
            if task.is_completed():
                continue
            if yesterday in (task.planned_days or []) and today not in task.planned_days:
                task.planned_days.append(today)
        self.state.ui["plan_rollover_day"] = today
        self.save()

    def _close_cross_day_active_task(self, today: str) -> None:
        active = self.active_task()
        if active is None:
            return
        session = active.active_session()
        if session is None:
            return
        if session.start_dt.date().isoformat() == today:
            return
        previous_day = session.start_dt.date()
        session.ended_at = datetime.combine(previous_day, time(23, 59, 59)).isoformat()
        active.status = TaskStatus.PAUSED
        self.pending_confirmation_task_id = None
        self.pending_confirmation_deadline = None
        self.next_reminder_at = None
        self.save()

    def _rebuild_runtime_state(self) -> None:
        active = self.active_task()
        if active and active.active_session():
            self.next_reminder_at = active.active_session().start_dt + self._reminder_interval_td()
        else:
            self.next_reminder_at = None

    def _reminder_interval_td(self) -> timedelta:
        return timedelta(minutes=self.reminder_interval_minutes())

    def reminder_interval_minutes(self) -> int:
        raw = self.state.ui.get("reminder_interval_minutes", 40)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 40
        return max(self._reminder_interval_min_clamp, min(value, self._reminder_interval_max_clamp))

    def set_reminder_interval_minutes(self, minutes: int) -> None:
        before = self.reminder_interval_minutes()
        value = max(self._reminder_interval_min_clamp, min(int(minutes), self._reminder_interval_max_clamp))
        self.state.ui["reminder_interval_minutes"] = value
        if value != before:
            self._apply_reminder_interval_change()
        self.save()

    def _apply_reminder_interval_change(self) -> None:
        if self.pending_confirmation_task_id:
            return
        active = self.active_task()
        if active and active.active_session():
            self.next_reminder_at = datetime.now() + self._reminder_interval_td()

    def bitrix_webhook(self) -> str:
        bitrix = self.state.ui.get("bitrix")
        if not isinstance(bitrix, dict):
            return ""
        return str(bitrix.get("webhook_url", "") or "").strip()

    def set_bitrix_webhook(self, url: str) -> None:
        bitrix = self.state.ui.setdefault("bitrix", {})
        bitrix["webhook_url"] = (url or "").strip()
        self.save()

    def all_tasks(self) -> list[Task]:
        return sorted(self.state.tasks, key=lambda task: task.created_at, reverse=True)

    def _view_sorted(self, tasks: list[Task]) -> list[Task]:
        ordered = sorted(tasks, key=lambda task: task.created_at, reverse=True)
        active = self.active_task()
        if active is not None and active in ordered:
            ordered.remove(active)
            ordered.insert(0, active)
        return ordered

    def tasks_all(self) -> list[Task]:
        return self._view_sorted(self.state.tasks)

    def tasks_in_progress(self) -> list[Task]:
        return self._view_sorted([t for t in self.state.tasks if not t.is_completed()])

    def tasks_today_plan(self, today: str | None = None) -> list[Task]:
        today = today or self.today_str()
        return self._view_sorted([t for t in self.state.tasks if today in (t.planned_days or [])])

    def tasks_on_date(self, date_iso: str) -> list[Task]:
        """Tasks that have tracked time on the given date."""
        return self._view_sorted(
            [t for t in self.state.tasks if self.today_seconds(t, date_iso) > 0]
        )

    def in_today_plan(self, task: Task, today: str | None = None) -> bool:
        today = today or self.today_str()
        return today in (task.planned_days or [])

    def add_to_plan(self, task_id: str, today: str | None = None) -> None:
        today = today or self.today_str()
        task = self.find_task(task_id)
        if today not in task.planned_days:
            task.planned_days.append(today)
            self.save()

    def remove_from_plan(self, task_id: str, today: str | None = None) -> None:
        today = today or self.today_str()
        task = self.find_task(task_id)
        if today in task.planned_days:
            task.planned_days = [day for day in task.planned_days if day != today]
            self.save()

    def today_seconds(self, task: Task, today: str | None = None) -> int:
        today = today or self.today_str()
        now = datetime.now()
        return sum(
            session.duration_seconds(now=now)
            for session in task.sessions
            if session.start_dt.date().isoformat() == today
        )

    def today_total_seconds(self, today: str | None = None) -> int:
        today = today or self.today_str()
        return sum(self.today_seconds(task, today) for task in self.state.tasks)

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

    def create_task(
        self,
        title: str,
        description: str = "",
        start_now: bool = False,
        bitrix: dict | None = None,
    ) -> Task:
        task = Task(
            id=make_id(),
            day=self.today_str(),
            title=title.strip(),
            description=description.strip(),
            status=TaskStatus.OPEN,
            bitrix=bitrix,
            planned_days=[self.today_str()],
        )
        self.state.tasks.append(task)
        self.save()
        if start_now:
            self.start_task(task.id)
        return task

    def link_bitrix(self, task_id: str, link: dict) -> None:
        """Attach a Bitrix entity link to a task and persist."""
        self.find_task(task_id).bitrix = link
        self.save()

    def mark_sessions_transferred(self, task_id: str, session_ids, record_id) -> None:
        """Mark given sessions as transferred to Bitrix with the created record id."""
        ids = set(session_ids)
        for session in self.find_task(task_id).sessions:
            if session.id in ids:
                session.bitrix_record_id = str(record_id)
        self.save()

    def import_bitrix_items(self, items: list[dict]) -> tuple[int, int]:
        """Create tasks from imported portal items, skipping same-day duplicates.

        Each item is ``{"source", "id", "title"}``. Returns ``(imported, skipped)``.
        Re-importing the same item on a later day is allowed (new day's plan).
        """
        today = self.today_str()
        imported = skipped = 0
        for item in items:
            source = item.get("source")
            item_id = str(item.get("id"))
            if self._bitrix_task_exists(today, source, item_id):
                skipped += 1
                continue
            self.create_task(
                item.get("title", ""),
                bitrix={"source": source, "id": item_id},
            )
            imported += 1
        return imported, skipped

    def _bitrix_task_exists(self, day: str, source, item_id: str) -> bool:
        for task in self.state.tasks:
            link = task.bitrix
            if (
                task.day == day
                and isinstance(link, dict)
                and link.get("source") == source
                and str(link.get("id")) == item_id
            ):
                return True
        return False

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
        self.next_reminder_at = now + self._reminder_interval_td()
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

    def delete_task(self, task_id: str) -> None:
        task = self.find_task(task_id)
        if task.status == TaskStatus.RUNNING and task.active_session():
            self.stop_task(task_id)
        self.state.tasks = [item for item in self.state.tasks if item.id != task_id]
        if self.pending_confirmation_task_id == task_id:
            self.pending_confirmation_task_id = None
            self.pending_confirmation_deadline = None
        if self.active_task() is None:
            self.next_reminder_at = None
        self.save()

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
        self.ensure_plan_rollover()
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
            self.next_reminder_at = started_at + self._reminder_interval_td()
        return ("running", active)

    def confirm_continue(self, task_id: str) -> None:
        task = self.find_task(task_id)
        if task.status != TaskStatus.RUNNING:
            return
        self.pending_confirmation_task_id = None
        self.pending_confirmation_deadline = None
        self.next_reminder_at = datetime.now() + self._reminder_interval_td()
        self.save()

    def add_session(self, task_id: str, started_at: datetime, ended_at: datetime) -> Session:
        if ended_at <= started_at:
            raise ValueError("Время окончания должно быть позже начала.")
        task = self.find_task(task_id)
        session = Session(id=make_id(), started_at=started_at.isoformat(), ended_at=ended_at.isoformat())
        task.sessions.append(session)
        task.sessions.sort(key=lambda item: item.started_at)
        self.save()
        return session

    def delete_session(self, task_id: str, session_id: str) -> None:
        task = self.find_task(task_id)
        removed_running = False
        for index, session in enumerate(task.sessions):
            if session.id != session_id:
                continue
            if session.ended_at is None:
                removed_running = True
            del task.sessions[index]
            break
        else:
            raise KeyError(session_id)
        if removed_running and task.status == TaskStatus.RUNNING:
            task.status = TaskStatus.PAUSED if task.sessions else TaskStatus.OPEN
        if self.pending_confirmation_task_id == task_id:
            if task.active_session() is None:
                self.pending_confirmation_task_id = None
                self.pending_confirmation_deadline = None
        if self.active_task() is None:
            self.next_reminder_at = None
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


def format_hm(total_seconds: int) -> str:
    """Format a duration as HH:MM (seconds dropped)."""
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    return f"{hours:02d}:{minutes:02d}"


def format_day_label(day_iso: str) -> str:
    parsed = date.fromisoformat(day_iso)
    return parsed.strftime("%d.%m.%Y")
