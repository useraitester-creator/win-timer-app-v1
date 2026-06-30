# PR: Focus mode — logic (PR5) + right-column UI (PR8) + layout subset (PR7)

Combined upstream PR replacing the separate «Фокус» tab with enhanced focus sessions under the task timer.

## Summary

- **PR5 — Focus logic:** starting focus pauses the active/panel task, creates a tracked task «Концентрация · N мин · …», persists `session_task_id` / `paused_task_id`, offers resume dialog when focus ends.
- **PR8 — UI:** focus block moved to the right column under the timer; sidebar «Фокус» tab removed.
- **PR7 layout subset (bundled):** timer panel layout fixes so focus controls fit at minimum window height.

## Files changed

| File | Change |
|------|--------|
| `win_timer_app/focus_ops.py` | New — focus session task helpers |
| `win_timer_app/controller.py` | Focus lifecycle, `timer_panel_task()`, sanitize on load |
| `win_timer_app/main_window.py` | Focus section under timer, resume dialogs, dynamic min height |
| `tests/test_controller.py` | Focus mode unit tests |
| `ИНСТРУКЦИЯ.md` | Focus under timer, not separate tab |

## PR7 layout checklist (included)

- [x] Remove `addStretch(1)` before Stop/Complete in timer panel
- [x] `_sync_focus_section_height()` — focus card height from sizeHint
- [x] `_relayout_timer_card()` — timer card fixed height from sizeHint
- [x] `_update_main_window_min_height()` — `WINDOW_MIN_HEIGHT = 680`
- [x] Focus preset buttons in two rows (5/10/20, 30/40) for narrow panel
- [x] Focus stop button visible at computed minimum height

## PR7 layout (not included — separate PR)

- Expandable task rows
- Task row action button layout overhaul
- Settings dialog tabbed layout (PR6)

## Test plan

```bash
cd win-timer-upstream-pr58
PYTHONPATH=. pytest tests/test_controller.py -q
```

Manual:

1. Start a task → start focus 10 min → task pauses, «Концентрация · …» appears in list.
2. Stop focus manually → dialog «Продолжить задачу …?».
3. Let focus expire → same resume dialog if a task was paused.
4. Resize window to minimum height → focus stop button still visible.
5. Sidebar shows only «Задачи» (disabled) + settings — no «Фокус» tab.

## Merge notes

- Independent of WebDAV / Android fork features.
- Builds on upstream after session comments + edit task PRs; no conflict with tray tooltip PR expected (different files).
