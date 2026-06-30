from __future__ import annotations

from datetime import datetime
from typing import Callable

from PySide6.QtCore import (
    QDate,
    QDateTime,
    QEasingCurve,
    QEvent,
    QPropertyAnimation,
    QStringListModel,
    QThread,
    QTimer,
    Qt,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QIcon,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QCompleter,
    QDateEdit,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QSystemTrayIcon,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .app_info import APP_TITLE, resolve_app_title
from .bitrix import Bitrix24Client, entity_url, looks_like_webhook
from .controller import AppController, format_day_label, format_duration, format_hm
from .models import Task, TaskStatus
from .runtime_info import build_about_report

_TRAY_TOOLTIP_FLOATING_AUTO = object()


def format_tray_tooltip(
    *,
    window_visible: bool,
    app_title: str,
    task_titles: list[str],
) -> str:
    """Tray hover text: app name when the window is open, one line per task when hidden."""
    if window_visible:
        return app_title
    if task_titles:
        return "\n".join(task_titles)
    return "Нет активных таймеров"


def tray_tooltip_task_titles(
    *,
    running_task_titles: list[str],
    floating_task: Task | None,
) -> list[str]:
    """Running tasks plus paused mini-widget task (without duplicates)."""
    titles = list(running_task_titles)
    if floating_task is not None and floating_task.title not in titles:
        titles.append(floating_task.title)
    return titles


def resolve_floating_task(
    *,
    active: Task | None,
    tracked_task_id: str | None,
    find_task: Callable[[str], Task],
    panel_task: Task | None = None,
) -> tuple[Task | None, str | None]:
    """Running/paused task for mini-widget and tray tooltip (active → tracked → panel)."""
    if active is not None:
        return active, active.id
    if tracked_task_id is not None:
        try:
            task = find_task(tracked_task_id)
        except KeyError:
            task = None
        else:
            if task.status == TaskStatus.COMPLETED:
                pass
            elif task.status in (TaskStatus.RUNNING, TaskStatus.PAUSED):
                return task, tracked_task_id
    if panel_task is not None and panel_task.status in (TaskStatus.RUNNING, TaskStatus.PAUSED):
        return panel_task, panel_task.id
    return None, None


def main_window_is_open(*, is_visible: bool, is_minimized: bool) -> bool:
    return is_visible and not is_minimized


class CreateTaskCard(QFrame):
    create_requested = Signal(str, str, bool)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("createCard")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(12)

        title = QLabel("Новая задача")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Название задачи")
        layout.addWidget(self.title_edit)

        self.description_edit = QPlainTextEdit()
        self.description_edit.setPlaceholderText("Краткое описание (необязательно)")
        self.description_edit.setFixedHeight(76)
        layout.addWidget(self.description_edit)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)

        create_button = QPushButton("Добавить")
        create_button.clicked.connect(lambda: self._emit_request(False))
        buttons.addWidget(create_button)

        quick_start_button = QPushButton("Добавить и старт")
        quick_start_button.setObjectName("primaryButton")
        quick_start_button.clicked.connect(lambda: self._emit_request(True))
        buttons.addWidget(quick_start_button)

        layout.addLayout(buttons)

    def _emit_request(self, start_now: bool) -> None:
        title = self.title_edit.text().strip()
        description = self.description_edit.toPlainText().strip()
        if not title:
            self.title_edit.setFocus()
            return
        self.create_requested.emit(title, description, start_now)
        self.title_edit.clear()
        self.description_edit.clear()


class CreateTaskDialog(QDialog):
    create_requested = Signal(dict)

    def __init__(self, controller: AppController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("Новая задача")
        self.setModal(True)
        self.resize(460, 380)
        self._company_id: str | None = None
        self._company_by_title: dict[str, str] = {}
        self._company_thread: _CallableThread | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("Новая задача")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Название задачи")
        layout.addWidget(self.title_edit)

        self.description_edit = QPlainTextEdit()
        self.description_edit.setPlaceholderText("Краткое описание (необязательно)")
        self.description_edit.setFixedHeight(90)
        layout.addWidget(self.description_edit)

        self.portal_checkbox = QCheckBox("Создать задачу в Битрикс24")
        self.portal_checkbox.toggled.connect(self._toggle_portal)
        layout.addWidget(self.portal_checkbox)

        self.company_edit = QLineEdit()
        self.company_edit.setPlaceholderText("Компания (поиск от 3 символов)")
        self.company_edit.setEnabled(False)
        self._company_model = QStringListModel(self)
        completer = QCompleter(self)
        completer.setModel(self._company_model)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.activated[str].connect(self._on_company_selected)
        self.company_edit.setCompleter(completer)
        self.company_edit.textEdited.connect(self._on_company_text)
        layout.addWidget(self.company_edit)

        self._company_timer = QTimer(self)
        self._company_timer.setSingleShot(True)
        self._company_timer.setInterval(300)
        self._company_timer.timeout.connect(self._run_company_search)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)

        create_button = QPushButton("Добавить")
        create_button.clicked.connect(lambda: self._emit_request(False))
        buttons.addWidget(create_button)

        quick_start_button = QPushButton("Добавить и старт")
        quick_start_button.setObjectName("primaryButton")
        quick_start_button.clicked.connect(lambda: self._emit_request(True))
        buttons.addWidget(quick_start_button)

        layout.addLayout(buttons)

    def open_clean(self) -> None:
        self.title_edit.clear()
        self.description_edit.clear()
        self.portal_checkbox.setChecked(False)
        self.company_edit.clear()
        self._company_id = None
        self._company_by_title = {}
        self.show()
        self.raise_()
        self.activateWindow()
        self.title_edit.setFocus()

    def _toggle_portal(self, checked: bool) -> None:
        self.company_edit.setEnabled(checked)
        if not checked:
            self.company_edit.clear()
            self._company_id = None

    def _on_company_text(self, text: str) -> None:
        self._company_id = None  # text changed by hand -> require re-pick
        if len(text.strip()) >= 3:
            self._company_timer.start()
        else:
            self._company_timer.stop()

    def _run_company_search(self) -> None:
        text = self.company_edit.text().strip()
        if len(text) < 3:
            return
        webhook = self.controller.bitrix_webhook()
        if not looks_like_webhook(webhook):
            return
        client = Bitrix24Client(webhook)
        self._company_thread = _CallableThread(lambda q=text: client.search_companies(q), self)
        self._company_thread.succeeded.connect(self._on_companies)
        self._company_thread.failed.connect(lambda message: None)
        self._company_thread.start()

    def _on_companies(self, companies: object) -> None:
        companies = companies if isinstance(companies, list) else []
        self._company_by_title = {
            c["title"]: c["id"] for c in companies if isinstance(c, dict) and c.get("title")
        }
        self._company_model.setStringList(list(self._company_by_title.keys()))
        self.company_edit.completer().complete()

    def _on_company_selected(self, title: str) -> None:
        self._company_id = self._company_by_title.get(title)

    def _emit_request(self, start_now: bool) -> None:
        title = self.title_edit.text().strip()
        description = self.description_edit.toPlainText().strip()
        if not title:
            self.title_edit.setFocus()
            return
        company_id = None
        if self.portal_checkbox.isChecked():
            company_id = self._company_id or self._company_by_title.get(
                self.company_edit.text().strip()
            )
        self.create_requested.emit(
            {
                "title": title,
                "description": description,
                "start_now": start_now,
                "on_portal": self.portal_checkbox.isChecked(),
                "company_id": company_id,
            }
        )
        self.accept()


_CAL_ICON_PATH: str | None = None


def _calendar_icon_path() -> str:
    """Draw a small calendar icon to a PNG once and return its path (for QSS)."""
    global _CAL_ICON_PATH
    if _CAL_ICON_PATH:
        return _CAL_ICON_PATH
    import os
    import tempfile

    from PySide6.QtCore import QPointF, QRectF
    from PySide6.QtGui import QColor, QPainter, QPen, QPixmap

    path = os.path.join(tempfile.gettempdir(), "tasktimer_calendar.png")
    scale = 2
    pixmap = QPixmap(24 * scale, 24 * scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(scale, scale)
    pen = QPen(QColor("#5f6b7c"))
    pen.setWidthF(1.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.drawRoundedRect(QRectF(3, 5, 18, 16), 2.5, 2.5)
    painter.drawLine(QPointF(3, 9.5), QPointF(21, 9.5))
    painter.drawLine(QPointF(8, 2.5), QPointF(8, 6.5))
    painter.drawLine(QPointF(16, 2.5), QPointF(16, 6.5))
    painter.end()
    pixmap.save(path, "PNG")
    _CAL_ICON_PATH = path
    return path


def _style_calendar_field(widget) -> None:
    """Give a QDateEdit/QDateTimeEdit a calendar icon and rounded right corners."""
    name = widget.objectName() or "calendarField"
    widget.setObjectName(name)
    icon = _calendar_icon_path().replace("\\", "/")
    widget.setStyleSheet(
        f"""
        #{name} {{
            background: #FFFFFF;
            border: 1px solid #D0D2D8;
            border-radius: 10px;
            padding: 7px 11px;
            color: #252835;
            selection-background-color: #3B83F6;
        }}
        #{name}:focus {{ border-color: #3B83F6; }}
        #{name}::drop-down {{
            subcontrol-origin: padding;
            subcontrol-position: center right;
            width: 30px;
            border: none;
            background: transparent;
            border-top-right-radius: 10px;
            border-bottom-right-radius: 10px;
        }}
        #{name}::down-arrow {{
            image: url("{icon}");
            width: 16px;
            height: 16px;
        }}
        """
    )


_CHECK_ICON_PATH: str | None = None


def _check_icon_path() -> str:
    """Draw a white checkmark PNG once (for the QCheckBox checked indicator)."""
    global _CHECK_ICON_PATH
    if _CHECK_ICON_PATH:
        return _CHECK_ICON_PATH
    import os
    import tempfile

    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QColor, QPainter, QPen, QPixmap

    path = os.path.join(tempfile.gettempdir(), "tasktimer_check.png")
    scale = 4
    pixmap = QPixmap(16 * scale, 16 * scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(scale, scale)
    pen = QPen(QColor("#FFFFFF"))
    pen.setWidthF(2.0)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.drawPolyline([QPointF(4, 8.4), QPointF(7, 11.2), QPointF(12, 5)])
    painter.end()
    pixmap.save(path, "PNG")
    _CHECK_ICON_PATH = path
    return path


_ICON_CACHE: dict[str, QIcon] = {}


def _line_icon(key: str, draw, color: str = "#828B9A") -> QIcon:
    """Build (and cache) a crisp line-art QIcon drawn by ``draw(painter)``."""
    cache_key = f"{key}:{color}"
    if cache_key in _ICON_CACHE:
        return _ICON_CACHE[cache_key]

    from PySide6.QtGui import QColor, QPainter, QPen, QPixmap

    scale = 4
    pixmap = QPixmap(16 * scale, 16 * scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(scale, scale)
    pen = QPen(QColor(color))
    pen.setWidthF(1.3)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    draw(painter)
    painter.end()

    icon = QIcon(pixmap)
    _ICON_CACHE[cache_key] = icon
    return icon


def _draw_trash(painter) -> None:
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QPainterPath

    painter.drawLine(QPointF(3, 4.6), QPointF(13, 4.6))  # lid line
    handle = QPainterPath()  # lid handle
    handle.moveTo(6, 4.6)
    handle.lineTo(6, 3.3)
    handle.quadTo(6, 2.7, 6.6, 2.7)
    handle.lineTo(9.4, 2.7)
    handle.quadTo(10, 2.7, 10, 3.3)
    handle.lineTo(10, 4.6)
    painter.drawPath(handle)
    body = QPainterPath()  # tapered bin
    body.moveTo(4.4, 4.6)
    body.lineTo(5.1, 13.2)
    body.lineTo(10.9, 13.2)
    body.lineTo(11.6, 4.6)
    painter.drawPath(body)


def _draw_stopwatch(painter) -> None:
    from PySide6.QtCore import QPointF, QRectF

    painter.drawEllipse(QRectF(3, 4.7, 10, 10))  # dial
    painter.drawLine(QPointF(8, 9.7), QPointF(8, 6.8))  # minute hand
    painter.drawLine(QPointF(8, 9.7), QPointF(10.1, 10.5))  # hour hand
    painter.drawLine(QPointF(8, 2.3), QPointF(8, 3.9))  # crown stem
    painter.drawLine(QPointF(6, 2.3), QPointF(10, 2.3))  # crown bar


class _CallableThread(QThread):
    """Runs a callable off the UI thread and reports the outcome via signals."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:  # surfaced to the user as a status message
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(result)


class AboutDialog(QDialog):
    def __init__(self, controller: AppController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("О программе")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel(APP_TITLE)
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        details = QPlainTextEdit()
        details.setReadOnly(True)
        details.setPlainText(
            build_about_report(
                stored_webhook=controller.bitrix_webhook(),
                data_path=controller.storage.path,
            )
        )
        details.setMinimumHeight(280)
        layout.addWidget(details)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


class SettingsDialog(QDialog):
    def __init__(self, controller: AppController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("Настройки")
        self.resize(520, 300)
        self._test_thread: _CallableThread | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        hint = QLabel(
            "Через указанное время после старта таймера или после ответа «Продолжить» "
            "приложение снова спросит, продолжать ли работу над задачей."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        self.reminder_spin = QSpinBox()
        self.reminder_spin.setRange(1, 24 * 60)
        self.reminder_spin.setSuffix(" мин")
        self.reminder_spin.setValue(controller.reminder_interval_minutes())
        form.addRow("Интервал напоминания", self.reminder_spin)

        self.webhook_edit = QLineEdit()
        self.webhook_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.webhook_edit.setPlaceholderText("https://портал.bitrix24.ru/rest/1/токен/")
        self.webhook_edit.setText(controller.bitrix_webhook())
        form.addRow("URL вебхука Битрикс24", self.webhook_edit)
        layout.addLayout(form)

        webhook_controls = QHBoxLayout()
        self.show_webhook_checkbox = QCheckBox("Показать")
        self.show_webhook_checkbox.toggled.connect(self._toggle_webhook_echo)
        webhook_controls.addWidget(self.show_webhook_checkbox)
        webhook_controls.addStretch(1)
        self.test_button = QPushButton("Проверить соединение")
        self.test_button.clicked.connect(self._test_connection)
        webhook_controls.addWidget(self.test_button)
        layout.addLayout(webhook_controls)

        self.webhook_status = QLabel("")
        self.webhook_status.setWordWrap(True)
        layout.addWidget(self.webhook_status)

        layout.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _toggle_webhook_echo(self, shown: bool) -> None:
        self.webhook_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
        )

    def _test_connection(self) -> None:
        url = self.webhook_edit.text().strip()
        if not looks_like_webhook(url):
            self._set_status("✗ Похоже на неверный формат URL (ожидается …/rest/…)", ok=False)
            return
        self.test_button.setEnabled(False)
        self._set_status("Проверяю…", ok=None)

        self._test_thread = _CallableThread(
            lambda: Bitrix24Client(url).test_connection(), self
        )
        self._test_thread.succeeded.connect(self._on_test_ok)
        self._test_thread.failed.connect(self._on_test_failed)
        self._test_thread.finished.connect(lambda: self.test_button.setEnabled(True))
        self._test_thread.start()

    def _on_test_ok(self, profile: object) -> None:
        name = ""
        if isinstance(profile, dict):
            name = " ".join(
                str(profile.get(key, "")).strip() for key in ("NAME", "LAST_NAME")
            ).strip()
        suffix = f": {name}" if name else ""
        self._set_status(f"✓ Подключение успешно{suffix}", ok=True)

    def _on_test_failed(self, message: str) -> None:
        self._set_status(f"✗ Не удалось подключиться: {message}", ok=False)

    def _set_status(self, text: str, ok: bool | None) -> None:
        color = {True: "#2d6b40", False: "#9b3c3c", None: "#5f6b7c"}[ok]
        self.webhook_status.setText(text)
        self.webhook_status.setStyleSheet(f"color: {color}; background: transparent;")

    def _await_test_thread(self) -> None:
        thread = self._test_thread
        if thread is not None and thread.isRunning():
            thread.wait(5000)

    def accept(self) -> None:
        self._await_test_thread()
        super().accept()

    def reject(self) -> None:
        self._await_test_thread()
        super().reject()


class TaskEditDialog(QDialog):
    def __init__(self, controller: AppController, task: Task, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.task_id = task.id
        self.setWindowTitle("Редактировать задачу")
        self.resize(480, 300)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)

        heading = QLabel("Редактировать задачу")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        self.title_edit = QLineEdit(task.title)
        self.title_edit.setPlaceholderText("Название задачи")
        form.addRow("Название", self.title_edit)

        self.description_edit = QPlainTextEdit()
        self.description_edit.setPlaceholderText("Описание (необязательно)")
        self.description_edit.setPlainText(task.description)
        self.description_edit.setFixedHeight(90)
        form.addRow("Описание", self.description_edit)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self) -> None:
        title = self.title_edit.text().strip()
        if not title:
            QMessageBox.warning(self, "Ошибка", "Введите название задачи.")
            return
        try:
            self.controller.update_task(
                self.task_id,
                title=title,
                description=self.description_edit.toPlainText(),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Ошибка", str(exc))
            return
        super().accept()


class SessionEditDialog(QDialog):
    def __init__(self, controller: AppController, task: Task, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.task = task
        self.selected_session_id: str | None = None
        self.setWindowTitle("История сессий")
        self.resize(660, 480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)

        title = QLabel("История сессий")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        subtitle = QLabel(task.title)
        subtitle.setObjectName("descriptionLabel")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        layout.addSpacing(4)

        self.select_all_checkbox = QCheckBox("Выделить всё")
        self.select_all_checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
        self.select_all_checkbox.toggled.connect(self._toggle_select_all)
        layout.addWidget(self.select_all_checkbox)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["", "Начало", "Окончание", "Длительность", "Комментарий", "Передано"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setFrameShape(QFrame.Shape.NoFrame)
        self.table.horizontalHeader().setHighlightSections(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._load_current_session)
        layout.addWidget(self.table, 1)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)
        form.setContentsMargins(0, 6, 0, 0)
        self.start_edit = QDateTimeEdit()
        self.start_edit.setObjectName("historyStart")
        self.start_edit.setDisplayFormat("dd.MM.yyyy HH:mm:ss")
        self.start_edit.setCalendarPopup(True)
        self.start_edit.setFixedHeight(34)
        _style_calendar_field(self.start_edit)
        form.addRow("Начало", self.start_edit)

        self.end_edit = QDateTimeEdit()
        self.end_edit.setObjectName("historyEnd")
        self.end_edit.setDisplayFormat("dd.MM.yyyy HH:mm:ss")
        self.end_edit.setCalendarPopup(True)
        self.end_edit.setFixedHeight(34)
        _style_calendar_field(self.end_edit)
        form.addRow("Окончание", self.end_edit)

        self.comment_edit = QPlainTextEdit()
        self.comment_edit.setObjectName("historyComment")
        self.comment_edit.setPlaceholderText("Комментарий к интервалу (необязательно)")
        self.comment_edit.setFixedHeight(56)
        form.addRow("Комментарий", self.comment_edit)
        layout.addLayout(form)
        layout.addSpacing(4)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        add_button = QPushButton("Добавить запись")
        add_button.setObjectName("ghostButton")
        add_button.setFixedHeight(34)
        add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        add_button.clicked.connect(self._add_session)
        actions.addWidget(add_button)
        self.delete_session_button = QPushButton("Удалить запись")
        self.delete_session_button.setObjectName("deleteGhostButton")
        self.delete_session_button.setFixedHeight(34)
        self.delete_session_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_session_button.clicked.connect(self._delete_current_session)
        actions.addWidget(self.delete_session_button)
        self.transfer_button = QPushButton("Передать в Битрикс")
        self.transfer_button.setObjectName("ghostButton")
        self.transfer_button.setFixedHeight(34)
        self.transfer_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.transfer_button.clicked.connect(self._transfer_to_bitrix)
        link = self.task.bitrix
        self.transfer_button.setEnabled(
            isinstance(link, dict) and link.get("source") in ("project", "task") and bool(link.get("id"))
        )
        actions.addWidget(self.transfer_button)
        actions.addStretch()
        save_button = QPushButton("Сохранить интервал")
        save_button.setObjectName("primaryButton")
        save_button.setFixedHeight(34)
        save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        save_button.clicked.connect(self._save_current_session)
        actions.addWidget(save_button)
        layout.addLayout(actions)

        self._reload()

    @staticmethod
    def _readonly_cell(text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        return item

    def _toggle_select_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                item.setCheckState(state)
        self.table.blockSignals(False)

    def _reload(self) -> None:
        self.select_all_checkbox.blockSignals(True)
        self.select_all_checkbox.setChecked(False)
        self.select_all_checkbox.blockSignals(False)
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for session in self.task.sessions:
            start = datetime.fromisoformat(session.started_at)
            end = datetime.fromisoformat(session.ended_at) if session.ended_at else None
            duration = session.duration_seconds(datetime.now())
            row = self.table.rowCount()
            self.table.insertRow(row)
            check = QTableWidgetItem()
            check.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            check.setCheckState(Qt.CheckState.Unchecked)
            check.setData(Qt.ItemDataRole.UserRole, session.id)
            self.table.setItem(row, 0, check)
            self.table.setItem(row, 1, self._readonly_cell(start.strftime("%d.%m.%Y %H:%M:%S")))
            self.table.setItem(
                row, 2,
                self._readonly_cell(end.strftime("%d.%m.%Y %H:%M:%S") if end else "идёт"),
            )
            self.table.setItem(row, 3, self._readonly_cell(format_duration(duration)))
            self.table.setItem(row, 4, self._readonly_cell(session.comment))
            self.table.setItem(row, 5, self._readonly_cell(session.bitrix_record_id or ""))
        self.table.blockSignals(False)
        if self.table.rowCount():
            self.table.selectRow(0)
        else:
            self.selected_session_id = None
            end_q = QDateTime.currentDateTime()
            self.end_edit.setDateTime(end_q)
            self.start_edit.setDateTime(end_q.addSecs(-3600))
        self.delete_session_button.setEnabled(self.table.rowCount() > 0)

    def _session_id_at(self, row: int) -> str | None:
        item = self.table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _current_session_id(self) -> str | None:
        row = self.table.currentRow()
        return self._session_id_at(row) if row >= 0 else None

    def _add_session(self) -> None:
        start = self.start_edit.dateTime().toPython()
        end = self.end_edit.dateTime().toPython()
        try:
            session = self.controller.add_session(
                self.task.id,
                start,
                end,
                comment=self.comment_edit.toPlainText(),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Ошибка", str(exc))
            return
        self.task = self.controller.find_task(self.task.id)
        self._reload()
        for row in range(self.table.rowCount()):
            if self._session_id_at(row) == session.id:
                self.table.selectRow(row)
                break

    def _delete_current_session(self) -> None:
        ids = [
            self._session_id_at(row)
            for row in range(self.table.rowCount())
            if self.table.item(row, 0)
            and self.table.item(row, 0).checkState() == Qt.CheckState.Checked
        ]
        if not ids:
            current = self._current_session_id()
            ids = [current] if current else []
        ids = [sid for sid in ids if sid]
        if not ids:
            return
        answer = QMessageBox.question(
            self,
            "Удаление",
            f"Удалить выбранные записи ({len(ids)})?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        for sid in ids:
            try:
                self.controller.delete_session(self.task.id, sid)
            except KeyError:
                pass
        self.task = self.controller.find_task(self.task.id)
        self._reload()

    def _load_current_session(self) -> None:
        session_id = self._current_session_id()
        if session_id is None:
            return
        session = next((entry for entry in self.task.sessions if entry.id == session_id), None)
        if session is None:
            return
        self.selected_session_id = session.id
        self.start_edit.setDateTime(QDateTime.fromString(session.started_at, Qt.DateFormat.ISODate))
        end_value = session.ended_at or datetime.now().isoformat()
        self.end_edit.setDateTime(QDateTime.fromString(end_value, Qt.DateFormat.ISODate))
        self.comment_edit.setPlainText(session.comment)

    def _save_current_session(self) -> None:
        if not self.selected_session_id:
            return
        start = self.start_edit.dateTime().toPython()
        end = self.end_edit.dateTime().toPython()
        try:
            self.controller.update_session(
                self.task.id,
                self.selected_session_id,
                start,
                end,
                comment=self.comment_edit.toPlainText(),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Ошибка", str(exc))
            return
        self.task = self.controller.find_task(self.task.id)
        self._reload()
        QMessageBox.information(self, "Сохранено", "Интервал обновлен.")

    def _transfer_to_bitrix(self) -> None:
        link = self.task.bitrix
        if not (isinstance(link, dict) and link.get("source") in ("project", "task") and link.get("id")):
            QMessageBox.information(self, "Битрикс24", "Задача не связана с Битрикс24.")
            return
        sessions = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                session = next(
                    (s for s in self.task.sessions if s.id == self._session_id_at(row)), None
                )
                if session and not session.bitrix_record_id:
                    sessions.append(session)
        if not sessions:
            QMessageBox.information(self, "Битрикс24", "Отметьте непереданные интервалы галочками.")
            return
        webhook = self.controller.bitrix_webhook()
        if not looks_like_webhook(webhook):
            QMessageBox.warning(self, "Битрикс24", "Укажите URL вебхука в настройках.")
            return
        name, ok = QInputDialog.getText(
            self,
            "Передача времени",
            "Название записи:",
            text=next((s.comment for s in sessions if s.comment.strip()), "") or self.task.title,
        )
        name = (name or "").strip()
        if not ok or not name:
            return
        total_seconds = sum(s.duration_seconds(datetime.now()) for s in sessions)
        session_ids = [s.id for s in sessions]
        source = link["source"]
        entity_id = link["id"]
        last_date = max(s.start_dt for s in sessions).date().isoformat()
        self.transfer_button.setEnabled(False)

        def work():
            client = Bitrix24Client(webhook)
            if source == "project":
                hours = round(total_seconds / 3600, 2)
                return client.add_project_time(
                    entity_id, last_date, hours, name, client.current_user_id()
                )
            return client.add_task_time(entity_id, total_seconds, name)

        self._transfer_thread = _CallableThread(work, self)
        self._transfer_thread.succeeded.connect(
            lambda record_id: self._on_transferred(session_ids, record_id)
        )
        self._transfer_thread.failed.connect(self._on_transfer_failed)
        self._transfer_thread.start()

    def _on_transferred(self, session_ids, record_id) -> None:
        self.controller.mark_sessions_transferred(self.task.id, session_ids, record_id)
        self.task = self.controller.find_task(self.task.id)
        self.transfer_button.setEnabled(True)
        self._reload()

    def _on_transfer_failed(self, message: str) -> None:
        self.transfer_button.setEnabled(True)
        QMessageBox.warning(self, "Битрикс24", f"Не удалось передать время: {message}")


_STATUS_PROP = {
    TaskStatus.RUNNING: "running",
    TaskStatus.PAUSED: "paused",
    TaskStatus.COMPLETED: "done",
    TaskStatus.OPEN: "todo",
}


class TaskRow(QFrame):
    """Compact single-line task row (Bitrix24 style, 48px tall)."""

    start_requested = Signal(str)
    stop_requested = Signal(str)
    complete_requested = Signal(str)
    resume_requested = Signal(str)
    history_requested = Signal(str)
    edit_requested = Signal(str)
    delete_requested = Signal(str)
    plan_toggle_requested = Signal(str)

    def __init__(
        self, controller: AppController, task: Task, reference_date: str | None = None
    ) -> None:
        super().__init__()
        self.setObjectName("taskRow")
        self.setFixedHeight(48)
        self._title = task.title
        self._is_running = task.status == TaskStatus.RUNNING
        status = _STATUS_PROP.get(task.status, "todo")
        self.setProperty("status", status)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(13, 0, 12, 0)
        layout.setSpacing(10)

        dot = QFrame()
        dot.setObjectName("taskDot")
        dot.setProperty("status", status)
        dot.setFixedSize(8, 8)
        layout.addWidget(dot)

        self._name_label = QLabel(task.title)
        self._name_label.setObjectName("taskName")
        self._name_label.setToolTip(task.title)
        self._name_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self._name_label, 1)

        # ── inline time block: «сег.» · «всего» ───────────────
        reference = reference_date or controller.today_str()
        is_today = reference == controller.today_str()
        times = QHBoxLayout()
        times.setSpacing(4)
        today_label = QLabel("сег." if is_today else format_day_label(reference))
        today_label.setObjectName("rowTimeLbl")
        times.addWidget(today_label)
        today_value = QLabel(format_hm(controller.today_seconds(task, reference)))
        today_value.setObjectName("rowTimeVal")
        today_value.setProperty("live", self._is_running)
        times.addWidget(today_value)
        sep = QLabel("·")
        sep.setObjectName("rowTimeSep")
        times.addWidget(sep)
        total_label = QLabel("всего")
        total_label.setObjectName("rowTimeLbl")
        times.addWidget(total_label)
        total_value = QLabel(format_hm(task.total_seconds(datetime.now())))
        total_value.setObjectName("rowTimeVal")
        times.addWidget(total_value)
        layout.addLayout(times)

        # ── actions: left fade + buttons, revealed on hover ───
        self._actions = QWidget()
        self._actions.setObjectName("rowActions")
        actions = QHBoxLayout(self._actions)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(4)

        fade = QFrame()
        fade.setObjectName("rowActionsFade")
        fade.setFixedWidth(26)
        actions.addWidget(fade)

        is_done = task.status == TaskStatus.COMPLETED

        if not is_done:
            history_button = QPushButton()
            history_button.setObjectName("iconAction")
            history_button.setFixedSize(26, 26)
            history_button.setIcon(_line_icon("stopwatch", _draw_stopwatch))
            history_button.setToolTip("История сессий")
            history_button.setCursor(Qt.CursorShape.PointingHandCursor)
            history_button.clicked.connect(lambda: self.history_requested.emit(task.id))
            actions.addWidget(history_button)

            edit_button = QPushButton("Изменить")
            edit_button.setObjectName("linkAction")
            edit_button.setToolTip("Изменить название и описание")
            edit_button.setCursor(Qt.CursorShape.PointingHandCursor)
            edit_button.clicked.connect(lambda: self.edit_requested.emit(task.id))
            actions.addWidget(edit_button)

            portal_url = entity_url(controller.bitrix_webhook(), task.bitrix)
            if portal_url:
                open_button = QPushButton("Открыть в Б24")
                open_button.setObjectName("linkAction")
                open_button.setToolTip("Открыть сущность в Битрикс24")
                open_button.setCursor(Qt.CursorShape.PointingHandCursor)
                open_button.clicked.connect(
                    lambda checked=False, url=portal_url: QDesktopServices.openUrl(QUrl(url))
                )
                actions.addWidget(open_button)

            complete_button = QPushButton("Завершить")
            complete_button.setObjectName("linkAction")
            complete_button.setCursor(Qt.CursorShape.PointingHandCursor)
            complete_button.clicked.connect(lambda: self.complete_requested.emit(task.id))
            actions.addWidget(complete_button)

        in_plan = controller.in_today_plan(task)
        plan_button = QPushButton("Из плана" if in_plan else "В план")
        plan_button.setObjectName("linkAction")
        plan_button.setCursor(Qt.CursorShape.PointingHandCursor)
        plan_button.clicked.connect(lambda: self.plan_toggle_requested.emit(task.id))
        actions.addWidget(plan_button)

        delete_button = QPushButton()
        delete_button.setObjectName("iconActionDanger")
        delete_button.setFixedSize(26, 26)
        delete_button.setIcon(_line_icon("trash", _draw_trash))
        delete_button.setToolTip("Удалить задачу")
        delete_button.setCursor(Qt.CursorShape.PointingHandCursor)
        delete_button.clicked.connect(lambda: self.delete_requested.emit(task.id))
        actions.addWidget(delete_button)

        if is_done:
            resume_button = QPushButton("Возобновить")
            resume_button.setObjectName("rowResume")
            resume_button.setFixedHeight(26)
            resume_button.setCursor(Qt.CursorShape.PointingHandCursor)
            resume_button.clicked.connect(lambda: self.resume_requested.emit(task.id))
            actions.addWidget(resume_button)
        elif self._is_running:
            stop_button = QPushButton("Стоп")
            stop_button.setObjectName("rowStop")
            stop_button.setFixedHeight(26)
            stop_button.setCursor(Qt.CursorShape.PointingHandCursor)
            stop_button.clicked.connect(lambda: self.stop_requested.emit(task.id))
            actions.addWidget(stop_button)
        else:
            start_button = QPushButton("Старт")
            start_button.setObjectName("rowStart")
            start_button.setFixedHeight(26)
            start_button.setCursor(Qt.CursorShape.PointingHandCursor)
            start_button.clicked.connect(lambda: self.start_requested.emit(task.id))
            actions.addWidget(start_button)

        layout.addWidget(self._actions)

        # Smooth fade-in: actions keep their layout space (no reflow jump);
        # opacity is animated and interaction disabled while hidden.
        self._fade_effect = QGraphicsOpacityEffect(self._actions)
        self._fade_effect.setOpacity(1.0 if self._is_running else 0.0)
        self._actions.setGraphicsEffect(self._fade_effect)
        self._actions.setEnabled(self._is_running)
        self._fade_anim = QPropertyAnimation(self._fade_effect, b"opacity", self)
        self._fade_anim.setDuration(150)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._fade_anim.finished.connect(self._on_fade_finished)

    def _animate_actions(self, target: float) -> None:
        self._fade_anim.stop()
        self._fade_anim.setStartValue(self._fade_effect.opacity())
        self._fade_anim.setEndValue(target)
        self._fade_anim.start()

    def _on_fade_finished(self) -> None:
        if self._fade_effect.opacity() <= 0.01:
            self._actions.setEnabled(False)

    def enterEvent(self, event) -> None:
        self._actions.setEnabled(True)
        self._animate_actions(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if not self._is_running:
            self._animate_actions(0.0)
        super().leaveEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        width = self._name_label.width()
        if width <= 0:
            return
        metrics = self._name_label.fontMetrics()
        elided = metrics.elidedText(self._title, Qt.TextElideMode.ElideRight, width)
        self._name_label.setText(elided)


class FloatingTimer(QWidget):
    """Small always-on-top translucent widget shown while a task runs in the tray."""

    stop_requested = Signal()
    start_requested = Signal()
    restore_requested = Signal()
    close_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowOpacity(0.9)
        self._drag_offset = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QFrame()
        card.setObjectName("floatingCard")
        outer.addWidget(card)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(8)

        self.name_label = QLabel("Задача")
        self.name_label.setObjectName("floatingName")
        header.addWidget(self.name_label, 1)

        self.close_button = QPushButton("✕")
        self.close_button.setObjectName("floatingClose")
        self.close_button.setFixedSize(20, 20)
        self.close_button.setToolTip("Скрыть виджет (таймер продолжит работать)")
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.clicked.connect(self.close_requested.emit)
        header.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)

        layout.addLayout(header)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)

        self.time_label = QLabel("00:00:00")
        self.time_label.setObjectName("floatingTime")
        bottom.addWidget(self.time_label)
        bottom.addStretch(1)

        self.start_button = QPushButton("▶")
        self.start_button.setObjectName("floatingStart")
        self.start_button.setFixedSize(30, 26)
        self.start_button.setToolTip("Старт")
        self.start_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_button.clicked.connect(self.start_requested.emit)
        bottom.addWidget(self.start_button)

        self.stop_button = QPushButton("⏸")
        self.stop_button.setObjectName("floatingStop")
        self.stop_button.setFixedSize(30, 26)
        self.stop_button.setToolTip("Стоп")
        self.stop_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_button.clicked.connect(self.stop_requested.emit)
        bottom.addWidget(self.stop_button)

        layout.addLayout(bottom)

        self.setFixedWidth(232)
        self.setStyleSheet(
            """
            QFrame#floatingCard {
                background: rgba(18, 20, 25, 0.88);
                border: 1px solid rgba(255, 255, 255, 0.16);
                border-radius: 16px;
            }
            QLabel#floatingName {
                background: transparent;
                color: rgba(255, 255, 255, 0.82);
                font-size: 11px;
                font-weight: 600;
            }
            QLabel#floatingTime {
                background: transparent;
                color: #ffffff;
                font-size: 22px;
                font-weight: 800;
                letter-spacing: 1px;
            }
            QPushButton#floatingStart, QPushButton#floatingStop {
                background: rgba(255, 255, 255, 0.12);
                color: white;
                border: 1px solid rgba(255, 255, 255, 0.18);
                border-radius: 8px;
                padding: 0;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton#floatingStart:hover, QPushButton#floatingStop:hover {
                background: rgba(255, 255, 255, 0.24);
            }
            QPushButton#floatingStart:disabled, QPushButton#floatingStop:disabled {
                color: rgba(255, 255, 255, 0.32);
                background: rgba(255, 255, 255, 0.05);
            }
            QPushButton#floatingClose {
                background: transparent;
                color: rgba(255, 255, 255, 0.55);
                border: none;
                border-radius: 6px;
                padding: 0;
                font-size: 12px;
                font-weight: 700;
            }
            QPushButton#floatingClose:hover {
                background: rgba(255, 90, 90, 0.85);
                color: white;
            }
            """
        )

    def update_view(self, title: str, elapsed: str, running: bool) -> None:
        elided = self.name_label.fontMetrics().elidedText(
            title, Qt.TextElideMode.ElideRight, 168
        )
        self.name_label.setText(elided)
        self.time_label.setText(elapsed)
        self.stop_button.setEnabled(running)
        self.start_button.setEnabled(not running)

    def show_at_default_corner(self) -> None:
        if not self.isVisible():
            self.adjustSize()
            screen = QApplication.primaryScreen()
            if screen is not None:
                geo = screen.availableGeometry()
                x = geo.right() - self.width() - 24
                y = geo.bottom() - self.height() - 24
                self.move(max(geo.left(), x), max(geo.top(), y))
        self.show()
        self.raise_()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None

    def mouseDoubleClickEvent(self, event) -> None:
        self.restore_requested.emit()


class PortalImportDialog(QDialog):
    """Pick projects (СПА 150) or tasks from the portal and import them."""

    def __init__(self, controller: AppController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("Импорт с портала Битрикс24")
        self.resize(640, 540)
        self._load_thread: _CallableThread | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        # Loader shown while the portal lists are fetched.
        self.loader = QWidget()
        loader_layout = QVBoxLayout(self.loader)
        loader_layout.addStretch(1)
        self.loading_label = QLabel("Загрузка с портала…")
        self.loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        loader_layout.addWidget(self.loading_label)
        progress_row = QHBoxLayout()
        progress_row.addStretch(1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # indeterminate (busy) indicator
        self.progress.setTextVisible(False)
        self.progress.setFixedWidth(240)
        progress_row.addWidget(self.progress)
        progress_row.addStretch(1)
        loader_layout.addLayout(progress_row)
        loader_layout.addStretch(1)
        layout.addWidget(self.loader, 1)

        self.tabs = QTabWidget()
        self.project_list, project_tab = self._build_list_tab("Поиск проекта…")
        self.task_list, task_tab = self._build_list_tab("Поиск задачи…")
        self.tabs.addTab(project_tab, "Проекты")
        self.tabs.addTab(task_tab, "Задачи")
        self.tabs.hide()
        layout.addWidget(self.tabs, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close_button = QPushButton("Закрыть")
        close_button.clicked.connect(self.reject)
        buttons.addWidget(close_button)
        self.import_button = QPushButton("Импортировать выбранное")
        self.import_button.setObjectName("primaryButton")
        self.import_button.setEnabled(False)
        self.import_button.clicked.connect(self._do_import)
        buttons.addWidget(self.import_button)
        layout.addLayout(buttons)

        self._start_load()

    def _build_list_tab(self, placeholder: str):
        tab = QWidget()
        column = QVBoxLayout(tab)
        column.setContentsMargins(0, 8, 0, 0)
        column.setSpacing(8)
        search = QLineEdit()
        search.setPlaceholderText(placeholder)
        list_widget = QListWidget()
        list_widget.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        search.textChanged.connect(lambda text, lw=list_widget: self._filter_list(lw, text))
        column.addWidget(search)
        column.addWidget(list_widget, 1)
        return list_widget, tab

    def _filter_list(self, list_widget: QListWidget, text: str) -> None:
        needle = text.strip().lower()
        for index in range(list_widget.count()):
            item = list_widget.item(index)
            item.setHidden(needle not in item.text().lower())

    def _show_loader(self, text: str, busy: bool = True) -> None:
        self.loading_label.setText(text)
        self.loading_label.setStyleSheet(
            f"color: {'#5f6b7c' if busy else '#9b3c3c'}; background: transparent;"
        )
        self.progress.setVisible(busy)
        self.tabs.hide()
        self.loader.show()

    def _show_content(self) -> None:
        self.loader.hide()
        self.tabs.show()

    def _start_load(self) -> None:
        webhook = self.controller.bitrix_webhook()
        if not looks_like_webhook(webhook):
            self._show_loader("Сначала укажите URL вебхука в Настройках.", busy=False)
            return
        self._show_loader("Загрузка с портала…", busy=True)

        def work():
            client = Bitrix24Client(webhook)
            user_id = client.current_user_id()
            return {
                "projects": client.list_projects(user_id),
                "tasks": client.list_tasks(user_id),
            }

        self._load_thread = _CallableThread(work, self)
        self._load_thread.succeeded.connect(self._on_loaded)
        self._load_thread.failed.connect(self._on_failed)
        self._load_thread.start()

    def _on_failed(self, message: str) -> None:
        self._show_loader(f"Не удалось загрузить: {message}", busy=False)

    def _on_loaded(self, data: object) -> None:
        data = data if isinstance(data, dict) else {}
        projects = data.get("projects", [])
        tasks = data.get("tasks", [])
        self._fill(self.project_list, projects, "project")
        self._fill(self.task_list, tasks, "task")
        self._set_status(
            f"Проектов: {len(projects)} · Задач: {len(tasks)}. "
            "Выбери нужные (можно несколько) и нажми «Импортировать выбранное».",
            ok=True,
        )
        self.import_button.setEnabled(True)
        self._show_content()

    def _fill(self, list_widget: QListWidget, items: list, source: str) -> None:
        list_widget.clear()
        for entry in items:
            title = entry.get("title") or f"#{entry.get('id')}"
            item = QListWidgetItem(title)
            item.setData(
                Qt.ItemDataRole.UserRole,
                {"source": source, "id": str(entry.get("id")), "title": entry.get("title", "")},
            )
            list_widget.addItem(item)

    def _do_import(self) -> None:
        chosen = [
            item.data(Qt.ItemDataRole.UserRole)
            for list_widget in (self.project_list, self.task_list)
            for item in list_widget.selectedItems()
        ]
        if not chosen:
            self._set_status("Ничего не выбрано.", ok=False)
            return
        self.imported_count, _ = self.controller.import_bitrix_items(chosen)
        self.accept()

    def _set_status(self, text: str, ok: bool | None) -> None:
        color = {True: "#2d6b40", False: "#9b3c3c", None: "#5f6b7c"}[ok]
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color}; background: transparent;")

    def _await_thread(self) -> None:
        thread = self._load_thread
        if thread is not None and thread.isRunning():
            thread.wait(8000)

    def accept(self) -> None:
        self._await_thread()
        super().accept()

    def reject(self) -> None:
        self._await_thread()
        super().reject()


class MainWindow(QMainWindow):
    focus_presets = (5, 10, 20, 30, 40)

    def __init__(self, controller: AppController, app: QApplication) -> None:
        super().__init__()
        self.controller = controller
        self.app = app
        self._current_view = "plan"
        self._selected_date: str | None = None
        self._portal_sync_queue: list = []
        self._portal_sync_busy = False
        self.tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        self.app_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.setWindowIcon(self.app_icon)
        self.setWindowTitle("Task Timer")
        self.resize(980, 680)
        self.create_dialog = CreateTaskDialog(self.controller, self)
        self.create_dialog.create_requested.connect(self._create_task)
        self._mini_task_id: str | None = None
        self.floating = FloatingTimer()
        self.floating.stop_requested.connect(self._floating_stop)
        self.floating.start_requested.connect(self._floating_start)
        self.floating.restore_requested.connect(self._restore_from_tray)
        self.floating.close_requested.connect(self._floating_close)
        self._load_fonts()
        self._build_ui()
        self._build_tray()
        self._apply_styles()
        self.refresh_ui()
        self._update_main_window_min_height()

        self.clock_timer = QTimer(self)
        self.clock_timer.setInterval(1000)
        self.clock_timer.timeout.connect(self._tick)
        self.clock_timer.start()

        QTimer.singleShot(350, self._offer_focus_resume_if_pending)

    def _load_fonts(self) -> None:
        """Register bundled fonts (if any) and resolve Inter / Roboto Mono."""
        import os

        fonts_dir = os.path.join(os.path.dirname(__file__), "assets", "fonts")
        if os.path.isdir(fonts_dir):
            for name in os.listdir(fonts_dir):
                if name.lower().endswith((".ttf", ".otf")):
                    QFontDatabase.addApplicationFont(os.path.join(fonts_dir, name))

        families = set(QFontDatabase.families())

        def pick(*candidates: str) -> str:
            for family in candidates:
                if family in families:
                    return family
            return candidates[-1]

        # Inter / Roboto Mono are bundled (assets/fonts), so they are the primary
        # choice on every OS; the fallbacks cover Windows and macOS system fonts.
        self._sans_family = pick(
            "Inter", "Segoe UI", "SF Pro Text", "Helvetica Neue", "Arial"
        )
        self._mono_family = pick(
            "Roboto Mono", "SF Mono", "Menlo", "Consolas", "Cascadia Mono", "Courier New"
        )
        app_font = QFont(self._sans_family, 10)
        app_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        self.app.setFont(app_font)

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("rootArea")
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(self._build_sidebar())
        root_layout.addWidget(self._build_tasks_page(), 1)

        self.setCentralWidget(central)

    def _build_sidebar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("sidebar")
        bar.setFixedWidth(52)
        layout = QVBoxLayout(bar)
        layout.setContentsMargins(0, 16, 0, 16)
        layout.setSpacing(2)

        logo = QLabel("⏱")
        logo.setObjectName("sidebarLogo")
        logo.setFixedSize(32, 32)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(logo, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(12)

        tasks_button = QPushButton("≣")
        tasks_button.setObjectName("navButton")
        tasks_button.setFixedSize(38, 38)
        tasks_button.setToolTip("Задачи")
        tasks_button.setProperty("active", True)
        tasks_button.setEnabled(False)
        layout.addWidget(tasks_button, 0, Qt.AlignmentFlag.AlignHCenter)

        layout.addStretch(1)

        settings_button = QPushButton("⚙")
        settings_button.setObjectName("navButton")
        settings_button.setFixedSize(38, 38)
        settings_button.setToolTip("Настройки")
        settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        settings_button.clicked.connect(self._open_settings)
        layout.addWidget(settings_button, 0, Qt.AlignmentFlag.AlignHCenter)

        return bar

    def _build_tasks_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("tasksPage")
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)

        # ── Subbar: filter chips, date, portal, new task ───────
        subbar = QFrame()
        subbar.setObjectName("subbar")
        subbar.setFixedHeight(48)
        sub = QHBoxLayout(subbar)
        sub.setContentsMargins(20, 0, 20, 0)
        sub.setSpacing(6)

        self._view_buttons: dict[str, QPushButton] = {}
        for key, label in (("plan", "Сегодня"), ("in_progress", "В работе"), ("all", "Все")):
            chip = QPushButton(label)
            chip.setObjectName("filterChip")
            chip.setFixedHeight(28)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.clicked.connect(lambda _checked=False, view=key: self._set_view(view))
            self._view_buttons[key] = chip
            sub.addWidget(chip)

        self.date_edit = QDateEdit()
        self.date_edit.setObjectName("dateFilter")
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("dd.MM.yyyy")
        self.date_edit.setFixedWidth(124)
        self.date_edit.setToolTip("Показать задачи с затраченным временем за выбранный день")
        _style_calendar_field(self.date_edit)
        self.date_edit.blockSignals(True)
        self.date_edit.setDate(QDate.currentDate())
        self.date_edit.blockSignals(False)
        self.date_edit.dateChanged.connect(self._set_date)
        self.date_edit.calendarWidget().clicked.connect(self._set_date)
        sub.addWidget(self.date_edit)

        sub.addStretch(1)

        self.today_total_label = QLabel("")
        self.today_total_label.setObjectName("summaryLabel")
        sub.addWidget(self.today_total_label)

        portal_button = QPushButton("С портала")
        portal_button.setObjectName("ghostButton")
        portal_button.setFixedHeight(28)
        portal_button.setCursor(Qt.CursorShape.PointingHandCursor)
        portal_button.clicked.connect(self._open_portal_import)
        sub.addWidget(portal_button)

        add_button = QPushButton("＋ Новая задача")
        add_button.setObjectName("btnAccent")
        add_button.setFixedHeight(30)
        add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        add_button.clicked.connect(self._open_create_dialog)
        sub.addWidget(add_button)

        page_layout.addWidget(subbar)

        # ── Content row: task list + dark timer panel ──────────
        content = QWidget()
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("taskScroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.days_container = QWidget()
        self.days_container.setObjectName("taskListBg")
        self.days_layout = QVBoxLayout(self.days_container)
        self.days_layout.setContentsMargins(20, 12, 20, 16)
        self.days_layout.setSpacing(6)
        self.days_layout.addStretch(1)
        self.scroll_area.setWidget(self.days_container)
        content_layout.addWidget(self.scroll_area, 1)

        content_layout.addWidget(self._build_timer_panel())
        page_layout.addWidget(content, 1)
        return page

    def _build_timer_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("timerPanel")
        panel.setFixedWidth(268)
        self.timer_panel = panel
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 20, 18, 20)
        layout.setSpacing(0)

        timer_label = QLabel("ТАЙМЕР")
        timer_label.setObjectName("timerLbl")
        layout.addWidget(timer_label)
        layout.addSpacing(12)

        self.timer_card = QFrame()
        self.timer_card.setObjectName("timerCard")
        card = QVBoxLayout(self.timer_card)
        card.setContentsMargins(14, 14, 14, 14)
        card.setSpacing(0)

        self.active_task_name = QLabel("Выберите задачу\nи нажмите Старт")
        self.active_task_name.setObjectName("tcardName")
        self.active_task_name.setWordWrap(True)
        card.addWidget(self.active_task_name)
        card.addSpacing(14)

        self.timer_digits = QLabel("00:00:00")
        self.timer_digits.setObjectName("timerDigits")
        card.addWidget(self.timer_digits)
        card.addSpacing(10)

        sub = QHBoxLayout()
        sub.setSpacing(16)
        for caption, attr in (("СЕГОДНЯ", "timer_today_value"), ("ВСЕГО", "timer_total_value")):
            box = QVBoxLayout()
            box.setSpacing(1)
            cap = QLabel(caption)
            cap.setObjectName("tcsLbl")
            box.addWidget(cap)
            value = QLabel("0:00")
            value.setObjectName("tcsVal")
            setattr(self, attr, value)
            box.addWidget(value)
            sub.addLayout(box)
        sub.addStretch(1)
        card.addLayout(sub)

        layout.addWidget(self.timer_card)
        layout.addSpacing(16)

        self.timer_progress = QProgressBar()
        self.timer_progress.setObjectName("timerProgress")
        self.timer_progress.setTextVisible(False)
        self.timer_progress.setFixedHeight(3)
        self.timer_progress.setRange(0, 100)
        self.timer_progress.setValue(0)
        layout.addWidget(self.timer_progress)

        self.stop_active_button = QPushButton("Стоп")
        self.stop_active_button.setObjectName("btnStop")
        self.stop_active_button.setFixedHeight(38)
        self.stop_active_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_active_button.clicked.connect(self._stop_active)
        layout.addWidget(self.stop_active_button)
        layout.addSpacing(6)

        self.complete_active_button = QPushButton("Завершить задачу")
        self.complete_active_button.setObjectName("btnComplete")
        self.complete_active_button.setFixedHeight(38)
        self.complete_active_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.complete_active_button.clicked.connect(self._complete_active)
        layout.addWidget(self.complete_active_button)

        layout.addSpacing(12)
        self.focus_section = self._build_focus_section()
        layout.addWidget(self.focus_section)

        return panel

    def _build_focus_section(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("focusPanel")
        panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(0)

        self.focus_card = QFrame()
        self.focus_card.setObjectName("focusCard")
        self.focus_card.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum,
        )
        focus_layout = QVBoxLayout(self.focus_card)
        focus_layout.setContentsMargins(14, 16, 14, 16)
        focus_layout.setSpacing(12)
        focus_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        focus_title = QLabel("РЕЖИМ КОНЦЕНТРАЦИИ")
        focus_title.setObjectName("focusHeading")
        focus_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        focus_layout.addWidget(focus_title, 0, Qt.AlignmentFlag.AlignHCenter)

        self.focus_display = QLabel("20:00")
        self.focus_display.setObjectName("focusDisplay")
        self.focus_display.setAlignment(Qt.AlignmentFlag.AlignCenter)
        focus_layout.addWidget(self.focus_display, 0, Qt.AlignmentFlag.AlignHCenter)

        self.focus_status_label = QLabel("Готов к запуску")
        self.focus_status_label.setObjectName("focusStatusLabel")
        self.focus_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        focus_layout.addWidget(self.focus_status_label, 0, Qt.AlignmentFlag.AlignHCenter)

        self.focus_buttons: dict[int, QPushButton] = {}
        preset_wrap = QWidget()
        preset_wrap.setObjectName("focusPresetWrap")
        preset_wrap.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        preset_layout = QVBoxLayout(preset_wrap)
        preset_layout.setContentsMargins(0, 0, 0, 0)
        preset_layout.setSpacing(FOCUS_PRESET_ROW_SPACING)
        preset_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        preset_rows = 0
        for row_minutes in ((5, 10, 20), (30, 40)):
            row = QHBoxLayout()
            row.setSpacing(5)
            row.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            for minutes in row_minutes:
                button = QPushButton(f"{minutes} мин")
                button.setObjectName("focusDur")
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.setFixedHeight(FOCUS_PRESET_BUTTON_HEIGHT)
                button.setSizePolicy(
                    QSizePolicy.Policy.Fixed,
                    QSizePolicy.Policy.Fixed,
                )
                button.clicked.connect(
                    lambda _checked=False, value=minutes: self._start_focus_timer(value)
                )
                self.focus_buttons[minutes] = button
                row.addWidget(button)
            preset_layout.addLayout(row)
            preset_rows += 1
        preset_wrap.setMinimumHeight(
            preset_rows * FOCUS_PRESET_BUTTON_HEIGHT
            + max(0, preset_rows - 1) * FOCUS_PRESET_ROW_SPACING
        )
        focus_layout.addWidget(preset_wrap, 0, Qt.AlignmentFlag.AlignHCenter)

        self.focus_stop_button = QPushButton("Остановить таймер")
        self.focus_stop_button.setObjectName("focusGo")
        self.focus_stop_button.setFixedHeight(38)
        self.focus_stop_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.focus_stop_button.clicked.connect(self._stop_focus_timer)
        focus_layout.addWidget(self.focus_stop_button)

        panel_layout.addWidget(self.focus_card)
        return panel

    def _relayout_timer_card(self) -> None:
        card_layout = self.timer_card.layout()
        panel_layout = self.timer_panel.layout()
        if card_layout is None or panel_layout is None:
            return
        card_layout.invalidate()
        card_layout.activate()
        card_height = self.timer_card.sizeHint().height()
        self.timer_card.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )
        self.timer_card.setFixedHeight(card_height)
        self._sync_focus_section_height()
        panel_layout.invalidate()
        panel_layout.activate()
        self.timer_panel.updateGeometry()
        self.timer_card.updateGeometry()
        self._update_main_window_min_height()

    def _main_window_min_height(self) -> int:
        self._sync_focus_section_height()
        panel_height = self.timer_panel.sizeHint().height()
        menu_height = (
            self.menuBar().height()
            if self.menuBar() is not None
            else WINDOW_VERTICAL_CHROME
        )
        return max(WINDOW_MIN_HEIGHT, panel_height + menu_height + 4)

    def _update_main_window_min_height(self) -> None:
        min_height = self._main_window_min_height()
        if self.minimumHeight() != min_height:
            self.setMinimumHeight(min_height)

    def _sync_focus_section_height(self) -> None:
        focus_layout = self.focus_card.layout()
        panel_layout = self.timer_panel.layout()
        if focus_layout is None or panel_layout is None:
            return
        focus_layout.invalidate()
        focus_layout.activate()
        height = self.focus_card.sizeHint().height()
        self.focus_card.setMinimumHeight(height)
        self.focus_section.setMinimumHeight(height)

    def _offer_focus_resume_if_pending(self) -> None:
        if not self.controller.focus_resume_offer_pending:
            return
        paused_id = self.controller.focus_paused_task_id
        if not paused_id:
            self.controller.focus_resume_offer_pending = False
            return
        self.controller.focus_resume_offer_pending = False
        self._prompt_focus_resume(paused_id)

    def _prompt_focus_resume(self, paused_task_id: str) -> None:
        try:
            task = self.controller.find_task(paused_task_id)
        except KeyError:
            self.controller.take_focus_paused_task_id()
            return
        if task.is_completed():
            self.controller.take_focus_paused_task_id()
            return
        answer = QMessageBox.question(
            self,
            "Фокус-сессия завершена",
            f"Время концентрации вышло.\n\nПродолжить задачу «{task.title}»?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        self.controller.take_focus_paused_task_id()
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.start_task(paused_task_id)
        self.refresh_ui()

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self.app_icon, self)
        if not self.tray_available:
            return
        tray_menu = QMenu()

        show_action = QAction("Открыть", self)
        show_action.triggered.connect(self._restore_from_tray)
        tray_menu.addAction(show_action)

        show_widget_action = QAction("Показать виджет", self)
        show_widget_action.triggered.connect(self._show_floating)
        tray_menu.addAction(show_widget_action)

        settings_action = QAction("Настройки…", self)
        settings_action.triggered.connect(self._open_settings)
        tray_menu.addAction(settings_action)

        about_action = QAction("О программе", self)
        about_action.triggered.connect(self._open_about)
        tray_menu.addAction(about_action)

        tray_menu.addSeparator()

        exit_action = QAction("Выход", self)
        exit_action.triggered.connect(self._exit_application)
        tray_menu.addAction(exit_action)

        self.tray.setContextMenu(tray_menu)
        self.tray.activated.connect(self._handle_tray_activation)
        self.tray.show()
        self._update_tray_tooltip()

    def _apply_styles(self) -> None:
        qss = """
            /* ── Base ─────────────────────────────────────── */
            QWidget { background: #F2F3F7; color: #252835; font-family: "__SANS__"; }
            QMainWindow, QWidget#rootArea, QWidget#tasksPage { background: #F2F3F7; }
            QLabel { background: transparent; }
            QToolTip {
                background: #252835; color: #FFFFFF; border: none;
                padding: 4px 8px; border-radius: 6px;
            }

            /* ── Generic inputs (dialogs) ─────────────────── */
            QLineEdit, QPlainTextEdit, QListWidget, QDateTimeEdit, QSpinBox {
                background: #F5F6FA; border: 1px solid #D0D2D8; border-radius: 10px;
                padding: 8px 12px; color: #252835; selection-background-color: #3B83F6;
            }
            QLineEdit:focus, QPlainTextEdit:focus, QListWidget:focus,
            QDateTimeEdit:focus, QSpinBox:focus { border-color: #3B83F6; background: #FFFFFF; }

            /* ── Generic buttons (dialogs / fallback) ─────── */
            QPushButton {
                background: #F5F6FA; border: 1px solid #D0D2D8; border-radius: 8px;
                padding: 7px 14px; color: #252835; font-weight: 400;
            }
            QPushButton:hover { background: #ECEEF3; }
            QPushButton:disabled { color: #B8BDC9; }
            QPushButton#primaryButton {
                background: #3B83F6; border: none; color: #FFFFFF; font-weight: 500;
                padding: 8px 20px;
            }
            QPushButton#btnAccent {
                background: #3B83F6; border: none; color: #FFFFFF; font-weight: 500;
                padding: 0 16px;
            }
            QPushButton#primaryButton:hover, QPushButton#btnAccent:hover { background: #2563EB; }
            QPushButton#ghostButton {
                background: transparent; border: 1px solid #D0D2D8; border-radius: 8px;
                color: #828B9A; padding: 0 14px; font-weight: 400;
            }
            QPushButton#ghostButton:hover { background: #F5F6FA; color: #252835; }
            QPushButton#deleteGhostButton {
                background: transparent; border: none; border-radius: 7px;
                padding: 4px 6px; color: #828B9A;
            }
            QPushButton#deleteGhostButton:hover { background: #FDE8E8; color: #E05353; }

            /* ── Sidebar ──────────────────────────────────── */
            QFrame#sidebar { background: #FFFFFF; border-right: 1px solid #DCDEE3; }
            QLabel#sidebarLogo {
                background: #3B83F6; color: #FFFFFF; border-radius: 10px;
                font-size: 17px; font-weight: 600;
            }
            QPushButton#navButton {
                background: transparent; border: none; border-radius: 10px;
                color: #B8BDC9; font-size: 18px; padding: 0;
            }
            QPushButton#navButton:hover { background: #F5F6FA; color: #828B9A; }
            QPushButton#navButton[active="true"] { background: #E8F0FD; color: #3B83F6; }

            /* ── Subbar ───────────────────────────────────── */
            QFrame#subbar { background: #FFFFFF; border-bottom: 1px solid #DCDEE3; }
            QPushButton#filterChip {
                background: #F5F6FA; border: 1px solid transparent; border-radius: 8px;
                color: #828B9A; padding: 0 13px; font-size: 12px; font-weight: 400;
            }
            QPushButton#filterChip:hover { background: #FFFFFF; color: #828B9A; }
            QPushButton#filterChip[active="true"] {
                background: #FFFFFF; border: 1px solid #D0D2D8; color: #3B83F6; font-weight: 500;
            }
            QLabel#summaryLabel { color: #B8BDC9; font-size: 11px; }

            /* ── Task list ────────────────────────────────── */
            QScrollArea#taskScroll { background: #F2F3F7; border: none; }
            QWidget#taskListBg { background: #F2F3F7; }
            QScrollBar:vertical { width: 6px; background: transparent; margin: 2px; }
            QScrollBar::handle:vertical { background: #D0D2D8; border-radius: 3px; min-height: 24px; }
            QScrollBar::handle:vertical:hover { background: #B8BDC9; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }

            /* ── Task row ─────────────────────────────────── */
            QFrame#taskRow {
                background: #FFFFFF; border: 1px solid #DCDEE3; border-radius: 10px;
            }
            QFrame#taskRow:hover { border-color: #D0D2D8; }
            QFrame#taskRow[status="running"] {
                border: 1px solid rgba(39,174,96,0.45); border-left: 3px solid #27AE60;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(39,174,96,0.10), stop:0.52 #FFFFFF);
            }
            QFrame#taskRow[status="paused"] { border-color: rgba(224,123,53,0.40); }
            QFrame#taskDot { border-radius: 4px; }
            QFrame#taskDot[status="running"] { background: #27AE60; }
            QFrame#taskDot[status="paused"]  { background: #E07B35; }
            QFrame#taskDot[status="todo"]    { background: transparent; border: 1px solid #B8BDC9; }
            QFrame#taskDot[status="done"]    { background: #B8BDC9; }
            QLabel#taskName { color: #252835; font-size: 13px; }
            QFrame#taskRow[status="done"] QLabel#taskName {
                color: #B8BDC9; text-decoration: line-through;
            }
            QLabel#rowTimeLbl { color: #B8BDC9; font-size: 10px; }
            QLabel#rowTimeSep { color: #D0D2D8; font-size: 11px; }
            QLabel#rowTimeVal { color: #828B9A; font-size: 11px; font-family: "__MONO__"; }
            QLabel#rowTimeVal[live="true"] { color: #27AE60; }

            QWidget#rowActions { background: transparent; }
            QFrame#rowActionsFade {
                border: none;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(255,255,255,0), stop:1 rgba(255,255,255,255));
            }
            QPushButton#iconAction, QPushButton#iconActionDanger {
                background: transparent; border: none; border-radius: 7px;
                color: #B8BDC9; font-size: 14px; padding: 0;
            }
            QPushButton#iconAction:hover { background: #F5F6FA; color: #828B9A; }
            QPushButton#iconActionDanger:hover { background: #FDE8E8; color: #E05353; }
            QPushButton#linkAction {
                background: transparent; border: none; border-radius: 7px;
                color: #828B9A; font-size: 11px; padding: 0 9px;
            }
            QPushButton#linkAction:hover { background: #F5F6FA; color: #252835; }
            QPushButton#rowStart {
                background: #27AE60; border: none; border-radius: 7px;
                color: #FFFFFF; font-size: 11px; font-weight: 500; padding: 0 11px;
            }
            QPushButton#rowStart:hover { background: #22994F; }
            QPushButton#rowStop {
                background: #FDE8E8; border: 1px solid rgba(224,83,83,0.25); border-radius: 7px;
                color: #E05353; font-size: 11px; font-weight: 500; padding: 0 11px;
            }
            QPushButton#rowStop:hover { background: #FBD9D9; }
            QPushButton#rowResume {
                background: #E8F0FD; border: none; border-radius: 7px;
                color: #3B83F6; font-size: 11px; font-weight: 500; padding: 0 11px;
            }
            QPushButton#rowResume:hover { background: #DBE8FC; }

            /* ── Timer panel (dark) ───────────────────────── */
            QFrame#timerPanel {
                background: #131720; border-left: 1px solid rgba(255,255,255,0.06);
            }
            QFrame#timerPanel[running="true"] { border-left: 1px solid rgba(39,174,96,0.25); }
            QLabel#timerLbl {
                color: rgba(255,255,255,0.28); font-size: 10px; font-weight: 500;
                letter-spacing: 1px;
            }
            QFrame#timerCard {
                background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.08);
                border-radius: 12px;
            }
            QFrame#timerCard[running="true"] {
                background: rgba(39,174,96,0.10); border: 1px solid rgba(39,174,96,0.25);
            }
            QLabel#tcardName { color: rgba(255,255,255,0.75); font-size: 13px; }
            QLabel#timerDigits {
                color: #7EB3FA; font-family: "__MONO__"; font-size: 38px; font-weight: 300;
            }
            QFrame#timerPanel[running="true"] QLabel#timerDigits { color: #27AE60; }
            QLabel#tcsLbl {
                color: rgba(255,255,255,0.25); font-size: 9px; font-weight: 500; letter-spacing: 1px;
            }
            QLabel#tcsVal { color: rgba(255,255,255,0.45); font-family: "__MONO__"; font-size: 12px; }
            QFrame#timerPanel[running="true"] QLabel#tcsVal { color: rgba(39,174,96,0.80); }
            QProgressBar#timerProgress {
                background: rgba(255,255,255,0.08); border: none; border-radius: 2px;
            }
            QProgressBar#timerProgress::chunk { background: #27AE60; border-radius: 2px; }
            QPushButton#btnStop {
                background: rgba(255,255,255,0.07); border: 1px solid rgba(255,255,255,0.10);
                border-radius: 10px; color: rgba(255,255,255,0.55); font-weight: 500;
            }
            QPushButton#btnStop:hover { background: rgba(255,255,255,0.12); color: rgba(255,255,255,0.85); }
            QPushButton#btnStop:disabled { color: rgba(255,255,255,0.22); }
            QPushButton#btnComplete {
                background: rgba(224,83,83,0.14); border: 1px solid rgba(224,83,83,0.20);
                border-radius: 10px; color: #F47A7A; font-weight: 500;
            }
            QPushButton#btnComplete:hover { background: rgba(224,83,83,0.24); }
            QPushButton#btnComplete:disabled { color: rgba(224,83,83,0.35); }

            /* ── Focus card (under timer) ────────────────── */
            QFrame#focusPanel { background: transparent; }
            QFrame#focusCard {
                background: #FFFFFF; border: 1px solid #DCDEE3; border-radius: 12px;
            }
            QWidget#focusPresetWrap { background: transparent; }
            QLabel#focusHeading {
                color: #B8BDC9; font-size: 10px; font-weight: 500; letter-spacing: 1.5px;
            }
            QLabel#focusDisplay {
                color: #3B83F6; font-family: "__MONO__"; font-size: 44px; font-weight: 300;
            }
            QLabel#focusDisplay[done="true"] { color: #27AE60; }
            QLabel#focusStatusLabel { color: #B8BDC9; font-size: 11px; }
            QPushButton#focusDur {
                background: #F5F6FA; border: 1px solid #D0D2D8; border-radius: 10px;
                color: #828B9A; padding: 5px 7px; font-size: 12px; min-width: 0;
                min-height: 24px;
            }
            QPushButton#focusDur:hover { background: #ECEEF3; }
            QPushButton#focusDur[active="true"] {
                background: #3B83F6; border: 1px solid #3B83F6; color: #FFFFFF; font-weight: 500;
            }
            QPushButton#focusGo {
                background: #FFFFFF; border: 1px solid #D0D2D8; border-radius: 10px;
                color: #828B9A; font-weight: 500; letter-spacing: 1px;
            }
            QPushButton#focusGo:hover { background: #ECEEF3; color: #252835; }
            QPushButton#focusGo:disabled { color: #B8BDC9; }

            /* ── Dialogs ──────────────────────────────────── */
            QDialog { background: #FFFFFF; }
            QLabel#sectionTitle { color: #252835; font-size: 15px; font-weight: 500; }
            QLabel#descriptionLabel { color: #828B9A; font-size: 12px; }

            /* Checkboxes */
            QCheckBox { color: #252835; spacing: 8px; }
            QCheckBox::indicator {
                width: 16px; height: 16px; border-radius: 4px;
                border: 1px solid #D0D2D8; background: #FFFFFF;
            }
            QCheckBox::indicator:hover { border-color: #3B83F6; }
            QCheckBox::indicator:checked {
                background: #3B83F6; border-color: #3B83F6; image: url("__CHECK__");
            }

            /* Tables (history) */
            QTableWidget, QTableView {
                background: #FFFFFF; border: 1px solid #DCDEE3; border-radius: 10px;
                alternate-background-color: #FAFBFC; outline: none;
                selection-background-color: #E8F0FD; selection-color: #252835;
            }
            QTableView::item { padding: 4px 8px; border: none; }
            QTableView::item:selected { background: #E8F0FD; color: #252835; }
            QTableView::indicator {
                width: 16px; height: 16px; border-radius: 4px;
                border: 1px solid #D0D2D8; background: #FFFFFF;
            }
            QTableView::indicator:hover { border-color: #3B83F6; }
            QTableView::indicator:checked {
                background: #3B83F6; border-color: #3B83F6; image: url("__CHECK__");
            }
            QHeaderView { background: transparent; }
            QHeaderView::section {
                background: #F5F6FA; color: #828B9A; padding: 8px 8px;
                border: none; border-bottom: 1px solid #DCDEE3;
                font-size: 11px; font-weight: 500;
            }
            QHeaderView::section:first { border-top-left-radius: 10px; }
            QHeaderView::section:last { border-top-right-radius: 10px; }
            QTableCornerButton::section { background: #F5F6FA; border: none; }

            /* Destructive ghost button (Удалить запись) */
            QPushButton#deleteGhostButton {
                background: transparent; border: 1px solid rgba(224,83,83,0.30);
                border-radius: 8px; padding: 0 14px; color: #E05353; font-weight: 400;
            }
            QPushButton#deleteGhostButton:hover { background: #FDE8E8; }
            QPushButton#deleteGhostButton:disabled { color: #E0A8A8; border-color: #F1D6D6; }
            QPushButton#ghostButton:disabled { color: #C8CCD4; border-color: #E4E6EC; }

            QTabWidget::pane { border: 1px solid #DCDEE3; border-radius: 10px; top: -1px; }
            QTabBar::tab {
                background: #F5F6FA; color: #828B9A; padding: 7px 16px;
                margin-right: 4px; border-radius: 8px; font-weight: 500;
            }
            QTabBar::tab:selected { background: #3B83F6; color: #FFFFFF; }
            QProgressBar {
                background: #E4E6EC; border: none; border-radius: 3px; text-align: center;
                color: #828B9A;
            }
            QProgressBar::chunk { background: #3B83F6; border-radius: 3px; }
        """
        qss = (
            qss.replace("__SANS__", self._sans_family)
            .replace("__MONO__", self._mono_family)
            .replace("__CHECK__", _check_icon_path().replace("\\", "/"))
        )
        self.setStyleSheet(qss)

    def refresh_ui(self) -> None:
        for key, button in self._view_buttons.items():
            button.setProperty("active", key == self._current_view)
            button.style().unpolish(button)
            button.style().polish(button)

        reference_date = self._selected_date if self._current_view == "date" else None
        if reference_date:
            self.today_total_label.setText(
                f"За {format_day_label(reference_date)} всего: "
                f"{format_hm(self.controller.today_total_seconds(reference_date))}"
            )
        else:
            self.today_total_label.setText(
                f"Сегодня всего: {format_hm(self.controller.today_total_seconds())}"
            )

        while self.days_layout.count():
            item = self.days_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        if self._current_view == "plan":
            tasks = self.controller.tasks_today_plan()
        elif self._current_view == "in_progress":
            tasks = self.controller.tasks_in_progress()
        elif self._current_view == "date":
            tasks = self.controller.tasks_on_date(reference_date)
        else:
            tasks = self.controller.tasks_all()

        # Completed tasks sink to the bottom (stable: keeps prior order otherwise).
        tasks = sorted(tasks, key=lambda t: t.status == TaskStatus.COMPLETED)

        if not tasks:
            hint = QLabel(self._empty_hint())
            hint.setObjectName("descriptionLabel")
            hint.setWordWrap(True)
            self.days_layout.addWidget(hint)
        else:
            for task in tasks:
                row = TaskRow(self.controller, task, reference_date=reference_date)
                row.start_requested.connect(self._start_task)
                row.stop_requested.connect(self._stop_task)
                row.complete_requested.connect(self._confirm_complete_task)
                row.resume_requested.connect(self._resume_task)
                row.history_requested.connect(self._open_history)
                row.edit_requested.connect(self._open_edit_task)
                row.delete_requested.connect(self._confirm_delete_task)
                row.plan_toggle_requested.connect(self._toggle_plan)
                self.days_layout.addWidget(row)
        self.days_layout.addStretch(1)
        self._refresh_active_panel()
        self._update_tray_tooltip()

    def _empty_hint(self) -> str:
        if self._current_view == "plan":
            return (
                "В плане на сегодня пусто. Добавь задачи кнопкой «В план» "
                "в фильтрах «В работе» или «Все»."
            )
        if self._current_view == "in_progress":
            return "Нет незавершённых задач."
        if self._current_view == "date" and self._selected_date:
            return f"Нет задач с затраченным временем за {format_day_label(self._selected_date)}."
        return "Пока нет задач."

    def _set_timer_running(self, running: bool) -> None:
        for widget in (self.timer_panel, self.timer_card):
            if bool(widget.property("running")) != running:
                widget.setProperty("running", running)
                widget.style().unpolish(widget)
                widget.style().polish(widget)
                widget.update()

    def _refresh_active_panel(self) -> None:
        panel_task = self.controller.timer_panel_task()
        timer_running = (
            panel_task is not None
            and panel_task.status == TaskStatus.RUNNING
            and panel_task.active_session() is not None
        )
        self._set_timer_running(timer_running)
        if not panel_task:
            self.active_task_name.setText("Выберите задачу\nи нажмите Старт")
            self.timer_digits.setText("00:00:00")
            self.timer_today_value.setText("0:00")
            self.timer_total_value.setText("0:00")
            self.timer_progress.setValue(0)
            self.stop_active_button.setText("Стоп")
            self.stop_active_button.setEnabled(False)
            self.complete_active_button.setEnabled(False)
            self._relayout_timer_card()
            return
        now = datetime.now()
        total = panel_task.total_seconds(now)
        self.active_task_name.setText(panel_task.title)
        self.timer_digits.setText(format_duration(total))
        self.timer_today_value.setText(format_hm(self.controller.today_seconds(panel_task)))
        self.timer_total_value.setText(format_hm(total))
        interval = max(1, self.controller.reminder_interval_minutes()) * 60
        session = panel_task.active_session()
        elapsed = session.duration_seconds(now) if session else 0
        self.timer_progress.setValue(int(min(elapsed / interval, 1.0) * 100))
        if timer_running:
            self.stop_active_button.setText("Стоп")
        else:
            self.stop_active_button.setText("Продолжить")
        self.stop_active_button.setEnabled(True)
        self.complete_active_button.setEnabled(True)
        self._relayout_timer_card()

    def _refresh_focus_panel(self) -> None:
        focus_state = self.controller.focus_timer_state()
        selected_minutes = int(focus_state.get("selected_minutes") or 20)
        remaining_seconds = self.controller.focus_remaining_seconds()

        if remaining_seconds > 0:
            minutes = remaining_seconds // 60
            seconds = remaining_seconds % 60
            self.focus_display.setText(f"{minutes:02d}:{seconds:02d}")
            self.focus_status_label.setText("Идет фокус-сессия")
            self.focus_stop_button.setEnabled(True)
            self._set_focus_done(False)
        else:
            self.focus_display.setText(f"{selected_minutes:02d}:00")
            self.focus_status_label.setText("Готов к запуску")
            self.focus_stop_button.setEnabled(False)
            self._set_focus_done(False)

        for minutes, button in self.focus_buttons.items():
            button.setProperty("active", minutes == selected_minutes)
            button.style().unpolish(button)
            button.style().polish(button)
            button.update()

    def _set_focus_done(self, done: bool) -> None:
        if bool(self.focus_display.property("done")) != done:
            self.focus_display.setProperty("done", done)
            self.focus_display.style().unpolish(self.focus_display)
            self.focus_display.style().polish(self.focus_display)
            self.focus_display.update()

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.controller, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.controller.set_reminder_interval_minutes(dialog.reminder_spin.value())
            self.controller.set_bitrix_webhook(dialog.webhook_edit.text())

    def _open_about(self) -> None:
        AboutDialog(self.controller, self).exec()

    def _open_create_dialog(self) -> None:
        self.create_dialog.open_clean()

    def _open_portal_import(self) -> None:
        dialog = PortalImportDialog(self.controller, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh_ui()

    def _create_task(self, payload: dict) -> None:
        title = payload.get("title", "")
        description = payload.get("description", "")
        start_now = payload.get("start_now", False)
        task = self.controller.create_task(title, description, start_now=start_now)
        self.refresh_ui()
        if payload.get("on_portal"):
            self._create_portal_task_for(task.id, title, description, payload.get("company_id"))

    def _create_portal_task_for(self, task_id, title, description, company_id) -> None:
        webhook = self.controller.bitrix_webhook()
        if not looks_like_webhook(webhook):
            QMessageBox.warning(
                self, "Битрикс24",
                "Укажите URL вебхука в настройках, чтобы создавать задачи на портале.",
            )
            return

        def work():
            client = Bitrix24Client(webhook)
            return client.create_portal_task(
                title, description, client.current_user_id(), company_id
            )

        self._create_thread = _CallableThread(work, self)
        self._create_thread.succeeded.connect(
            lambda portal_id: self._on_portal_task_created(task_id, portal_id)
        )
        self._create_thread.failed.connect(
            lambda message: QMessageBox.warning(
                self, "Битрикс24", f"Не удалось создать задачу на портале: {message}"
            )
        )
        self._create_thread.start()

    def _on_portal_task_created(self, task_id, portal_id) -> None:
        self.controller.link_bitrix(task_id, {"source": "task", "id": str(portal_id)})
        self.refresh_ui()

    def _set_view(self, view: str) -> None:
        self._current_view = view
        self.refresh_ui()

    def _set_date(self, qdate: QDate) -> None:
        self._selected_date = qdate.toString("yyyy-MM-dd")
        self._current_view = "date"
        self.refresh_ui()

    def _toggle_plan(self, task_id: str) -> None:
        task = self.controller.find_task(task_id)
        if self.controller.in_today_plan(task):
            self.controller.remove_from_plan(task_id)
        else:
            self.controller.add_to_plan(task_id)
        self.refresh_ui()

    def _start_focus_timer(self, minutes: int) -> None:
        self.controller.start_focus_timer(minutes)
        self.refresh_ui()
        self._show_tray_message(
            "Режим концентрации",
            f"Запущен таймер на {minutes} мин.",
            QSystemTrayIcon.MessageIcon.Information,
            3000,
        )

    def _stop_focus_timer(self) -> None:
        paused_id = self.controller.focus_paused_task_id
        self.controller.stop_focus_timer()
        self.refresh_ui()
        if paused_id:
            self._prompt_focus_resume(paused_id)

    def _start_task(self, task_id: str) -> None:
        self.controller.start_task(task_id)
        self.refresh_ui()
        task = self.controller.find_task(task_id)
        self._show_tray_message("Таймер запущен", task.title, QSystemTrayIcon.MessageIcon.Information, 4000)

    def _stop_task(self, task_id: str) -> None:
        task = self.controller.stop_task(task_id)
        self.refresh_ui()
        self._show_tray_message("Таймер остановлен", task.title, QSystemTrayIcon.MessageIcon.Information, 4000)

    def _confirm_complete_task(self, task_id: str) -> None:
        task = self.controller.find_task(task_id)
        answer = QMessageBox.question(
            self,
            "Подтверждение",
            f"Задача завершена, закрываю?\n\n{task.title}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.complete_task(task_id)
            self.refresh_ui()
            self._sync_portal_completion(self.controller.find_task(task_id), complete=True)
            self._show_tray_message("Задача завершена", task.title, QSystemTrayIcon.MessageIcon.Information, 4000)

    def _resume_task(self, task_id: str) -> None:
        self.controller.resume_completed_task(task_id)
        self.refresh_ui()
        task = self.controller.find_task(task_id)
        self._sync_portal_completion(task, complete=False)
        self._show_tray_message("Задача возобновлена", task.title, QSystemTrayIcon.MessageIcon.Information, 4000)

    def _sync_portal_completion(self, task: Task, complete: bool) -> None:
        """Queue a complete/renew of the linked Bitrix24 task.

        Operations are serialized (one at a time, in action order) so that, e.g.,
        complete→resume→complete always leaves the portal task in the last state.
        """
        link = task.bitrix if task else None
        if not (isinstance(link, dict) and link.get("source") == "task" and link.get("id")):
            return
        webhook = self.controller.bitrix_webhook()
        if not looks_like_webhook(webhook):
            return
        self._portal_sync_queue.append((link["id"], complete, webhook))
        self._process_portal_sync_queue()

    def _process_portal_sync_queue(self) -> None:
        if self._portal_sync_busy or not self._portal_sync_queue:
            return
        portal_id, complete, webhook = self._portal_sync_queue.pop(0)
        self._portal_sync_busy = True

        def work():
            client = Bitrix24Client(webhook)
            if complete:
                client.complete_portal_task(portal_id)
            else:
                client.renew_portal_task(portal_id)

        self._portal_sync_thread = _CallableThread(work, self)
        self._portal_sync_thread.failed.connect(
            lambda message: QMessageBox.warning(
                self, "Битрикс24", f"Не удалось синхронизировать задачу на портале: {message}"
            )
        )
        self._portal_sync_thread.finished.connect(self._on_portal_sync_done)
        self._portal_sync_thread.start()

    def _on_portal_sync_done(self) -> None:
        self._portal_sync_busy = False
        self._process_portal_sync_queue()

    def _open_history(self, task_id: str) -> None:
        task = self.controller.find_task(task_id)
        dialog = SessionEditDialog(self.controller, task, self)
        dialog.exec()
        self.refresh_ui()

    def _open_edit_task(self, task_id: str) -> None:
        task = self.controller.find_task(task_id)
        dialog = TaskEditDialog(self.controller, task, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh_ui()

    def _confirm_delete_task(self, task_id: str) -> None:
        task = self.controller.find_task(task_id)
        answer = QMessageBox.question(
            self,
            "Удаление задачи",
            f"Действительно удалить задачу?\n\n{task.title}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.delete_task(task_id)
            self.refresh_ui()

    def _stop_active(self) -> None:
        panel_task = self.controller.timer_panel_task()
        if not panel_task:
            return
        if panel_task.status == TaskStatus.RUNNING and panel_task.active_session():
            self._stop_task(panel_task.id)
        else:
            self._start_task(panel_task.id)

    def _complete_active(self) -> None:
        panel_task = self.controller.timer_panel_task()
        if panel_task:
            self._confirm_complete_task(panel_task.id)

    def _tick(self) -> None:
        status, task = self.controller.check_reminders()
        self._refresh_active_panel()
        focus_status, focus_payload = self.controller.check_focus_timer()
        self._refresh_focus_panel()
        if status == "needs_confirmation" and task:
            self._show_continue_prompt(task)
        elif status == "auto_stopped" and task:
            self.refresh_ui()
            grace_minutes = int(self.controller.reminder_grace.total_seconds() // 60)
            self._show_tray_message(
                "Таймер поставлен на стоп",
                f"{task.title}: подтверждение не было получено в течение {grace_minutes} мин.",
                QSystemTrayIcon.MessageIcon.Warning,
                6000,
            )
        self._update_floating()
        self._update_tray_tooltip()
        if focus_status == "finished":
            paused_task_id = self.controller.focus_paused_task_id
            QApplication.beep()
            QApplication.beep()
            QApplication.beep()
            duration_label = f"{focus_payload} мин." if focus_payload else "выбранное время"
            self._show_tray_message(
                "Фокус-сессия завершена",
                f"Таймер концентрации на {duration_label} закончился.",
                QSystemTrayIcon.MessageIcon.Information,
                6000,
            )
            if paused_task_id:
                self._prompt_focus_resume(paused_task_id)
            else:
                self.controller.take_focus_paused_task_id()
                self.refresh_ui()
                QMessageBox.information(
                    self,
                    "Фокус-сессия завершена",
                    "Время концентрации вышло.",
                )
            return

    def _show_continue_prompt(self, task: Task) -> None:
        minutes = self.controller.reminder_interval_minutes()
        grace_minutes = int(self.controller.reminder_grace.total_seconds() // 60)
        self._show_tray_message(
            "Подтвердите продолжение",
            f"{task.title} выполняется уже {minutes} мин. "
            f"Без подтверждения через {grace_minutes} мин таймер остановится.",
            QSystemTrayIcon.MessageIcon.Information,
            6000,
        )
        answer = QMessageBox.question(
            self,
            "Подтверждение продолжения",
            f"Задача выполняется уже {minutes} мин.\n\n{task.title}\n\nПродолжаете работу?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.confirm_continue(task.id)
        else:
            self.controller.stop_task(task.id)
        self.refresh_ui()

    def _show_tray_message(
        self,
        title: str,
        text: str,
        icon: QSystemTrayIcon.MessageIcon = QSystemTrayIcon.MessageIcon.Information,
        timeout: int = 4000,
    ) -> None:
        if self.tray_available and self.tray.isVisible():
            self.tray.showMessage(title, text, icon, timeout)

    def _update_tray_tooltip(
        self,
        floating_task: Task | None | object = _TRAY_TOOLTIP_FLOATING_AUTO,
    ) -> None:
        if not self.tray_available:
            return
        window_open = main_window_is_open(
            is_visible=self.isVisible(),
            is_minimized=self.isMinimized(),
        )
        running_titles = [task.title for task in self.controller.running_tasks()]
        display_task: Task | None = None
        if not window_open:
            if floating_task is _TRAY_TOOLTIP_FLOATING_AUTO:
                display_task, tracked_id = self._floating_task_state()
                self._mini_task_id = tracked_id
            else:
                display_task = floating_task  # type: ignore[assignment]
        task_titles = tray_tooltip_task_titles(
            running_task_titles=running_titles,
            floating_task=display_task,
        )
        self.tray.setToolTip(
            format_tray_tooltip(
                window_visible=window_open,
                app_title=resolve_app_title(),
                task_titles=task_titles,
            )
        )

    def _floating_task_state(self) -> tuple[Task | None, str | None]:
        return resolve_floating_task(
            active=self.controller.active_task(),
            tracked_task_id=self._mini_task_id,
            find_task=self.controller.find_task,
            panel_task=self.controller.timer_panel_task(),
        )

    def _hide_to_tray(self) -> None:
        if not self.tray_available or not self.tray.isVisible():
            return
        self.hide()
        self._show_floating()
        self._update_tray_tooltip()
        self._show_tray_message(
            "Приложение свернуто",
            "Таймер продолжает работать в системном трее.",
            QSystemTrayIcon.MessageIcon.Information,
            4000,
        )

    def _show_floating(self) -> None:
        task, tracked_id = self._floating_task_state()
        if task is None or tracked_id is None:
            return
        self._mini_task_id = tracked_id
        try:
            self.controller.find_task(self._mini_task_id)
        except KeyError:
            self._mini_task_id = None
            return
        self.floating.show_at_default_corner()
        self._update_floating()

    def _update_floating(self) -> None:
        if not self.floating.isVisible():
            return
        if self._mini_task_id is None:
            self.floating.hide()
            return
        try:
            task = self.controller.find_task(self._mini_task_id)
        except KeyError:
            self._mini_task_id = None
            self.floating.hide()
            return
        running = task.status == TaskStatus.RUNNING and task.active_session() is not None
        elapsed = format_duration(task.total_seconds(datetime.now()))
        self.floating.update_view(task.title, elapsed, running)

    def _floating_stop(self) -> None:
        if self._mini_task_id is None:
            return
        self.controller.stop_task(self._mini_task_id)
        self.refresh_ui()
        self._update_floating()

    def _floating_start(self) -> None:
        if self._mini_task_id is None:
            return
        self.controller.start_task(self._mini_task_id)
        self.refresh_ui()
        self._update_floating()

    def _floating_close(self) -> None:
        """Hide the floating widget; the timer keeps running in the background."""
        self.floating.hide()
        self._update_tray_tooltip()
        self._show_tray_message(
            "Виджет скрыт",
            "Таймер продолжает работать. Откройте приложение или «Показать виджет» из трея.",
            QSystemTrayIcon.MessageIcon.Information,
            4000,
        )

    def _exit_application(self) -> None:
        active = self.controller.active_task()
        if active:
            self.controller.stop_task(active.id)
        self.floating.hide()
        if self.tray_available:
            self.tray.hide()
        self.app.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.tray_available and self.tray.isVisible():
            answer = QMessageBox.question(
                self,
                "Закрытие приложения",
                "Завершить работу с приложением?\n\nДа: остановить текущую задачу и закрыть приложение.\nНет: свернуть в трей.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                event.accept()
                self._exit_application()
                return
            self._hide_to_tray()
            event.ignore()
            return
        super().closeEvent(event)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and self.isMinimized():
            QTimer.singleShot(0, self._hide_to_tray)

    def bind_single_instance_server(self, server) -> None:
        """Surface this window when a second launch pings the local server."""
        self._instance_server = server
        server.newConnection.connect(self._on_second_instance)

    def _on_second_instance(self) -> None:
        connection = self._instance_server.nextPendingConnection()
        if connection is not None:
            connection.disconnected.connect(connection.deleteLater)
        self._restore_from_tray()

    def _restore_from_tray(self) -> None:
        self.floating.hide()
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self._update_tray_tooltip()

    def _handle_tray_activation(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._restore_from_tray()
