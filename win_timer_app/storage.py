from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import QStandardPaths

from .models import Task


def default_data_path() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    if base:
        path = Path(base)
        try:
            path.mkdir(parents=True, exist_ok=True)
            test_file = path / ".write_test"
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink(missing_ok=True)
            return path / "data.json"
        except OSError:
            pass

    fallback = Path.cwd() / ".localdata"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback / "data.json"


@dataclass
class AppState:
    tasks: list[Task] = field(default_factory=list)
    ui: dict[str, Any] = field(
        default_factory=lambda: {
            "filter_open_only": False,
            "focus_timer": {
                "selected_minutes": 20,
                "duration_minutes": None,
                "ends_at": None,
            },
        }
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tasks": [task.to_dict() for task in self.tasks],
            "ui": self.ui,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppState":
        ui = data.get("ui", {})
        ui.setdefault("filter_open_only", False)
        ui.setdefault(
            "focus_timer",
            {
                "selected_minutes": 20,
                "duration_minutes": None,
                "ends_at": None,
            },
        )
        return cls(
            tasks=[Task.from_dict(item) for item in data.get("tasks", [])],
            ui=ui,
        )


class Storage:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_data_path()

    def load(self) -> AppState:
        if not self.path.exists():
            return AppState()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return AppState.from_dict(data)

    def save(self, state: AppState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
