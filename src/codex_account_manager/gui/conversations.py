"""Local conversation browsing, filters and explicit conversation actions."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QPoint, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from codex_account_manager.continuity.tracking import ObservedWork, WorkTracker, account_hash
from codex_account_manager.core.events import bus
from codex_account_manager.domain.models import ThreadRecord
from codex_account_manager.gui.async_runner import AsyncRunner
from codex_account_manager.gui.design import DARK, make_icon, set_button_icon
from codex_account_manager.gui.i18n import source_label, state_label, tr, work_status
from codex_account_manager.gui.view_base import (
    BaseView,
    SortableItem,
    table_cell,
    table_widget,
    view_header,
)
from codex_account_manager.gui.widgets import ComboBox, Pill, label
from codex_account_manager.storage.repositories import ProfileRepository


class ConversationsView(BaseView):
    notes_requested = Signal(str)
    shutdown_requested = Signal(str)
    work_changed = Signal(int, int)
    tracking_reset = Signal()

    def __init__(self, runner: AsyncRunner, palette=DARK):
        super().__init__(palette)
        self.runner = runner
        self._threads: list[ThreadRecord] = []
        self._project_names: dict[str, str] = {}
        self._busy = False
        self._tracking_busy = False
        self._tracked: list[ObservedWork] = []
        self._support_busy = False
        self._continue_busy = False
        self._support_generation = 0
        self._support_selection: str | None = None
        self._account_names: dict[str, str] = {}
        self.sync = QPushButton(tr("Refresh conversations"))
        self.sync.clicked.connect(self._sync)
        self._root.addWidget(
            view_header(
                tr("Jobs"),
                tr("Follow running work, inspect its goal and open it in Codex."),
                self.sync,
            )
        )
        self.content_scroll = QScrollArea()
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("PagePanel")
        self._content = QVBoxLayout(content)
        self._content.setContentsMargins(24, 20, 24, 20)
        self._content.setSpacing(16)
        self.content_scroll.setWidget(content)
        self.tabs = QTabWidget()
        self._root.addWidget(self.tabs, 1)
        tracked_panel = QFrame()
        tracked_panel.setObjectName("Panel")
        tracked_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tracked_panel.setMinimumHeight(340)
        tracked_layout = QVBoxLayout(tracked_panel)
        tracked_layout.setContentsMargins(20, 18, 20, 18)
        tracked_layout.setSpacing(12)
        tracked_header = QHBoxLayout()
        tracked_header.addWidget(label(tr("Tracked work"), "H2"))
        self.tracked_count = label("", "Accent")
        tracked_header.addWidget(self.tracked_count)
        tracked_header.addStretch()
        self.reset_tracking = QPushButton(tr("Clear tracking"))
        self.reset_tracking.setToolTip(
            tr("Clear saved observations and pause automatic monitoring.")
        )
        self.reset_tracking.clicked.connect(self._clear_tracking)
        tracked_header.addWidget(self.reset_tracking)
        self.check_tracking = QPushButton()
        set_button_icon(self.check_tracking, "refresh", palette.muted)
        self.check_tracking.setToolTip(tr("Check tracking now"))
        self.check_tracking.setAccessibleName(tr("Check tracking now"))
        self.check_tracking.setObjectName("Ghost")
        self.check_tracking.clicked.connect(self._request_tracking)
        tracked_header.addWidget(self.check_tracking)
        tracked_layout.addLayout(tracked_header)
        help_button = QPushButton()
        help_button.setIcon(make_icon("help", palette.muted, 18))
        help_button.setToolTip(tr("How tracking works"))
        help_button.setAccessibleName(tr("How tracking works"))
        help_button.setObjectName("Ghost")
        help_button.clicked.connect(self._tracking_help)
        tracked_header.insertWidget(2, help_button)
        self.tracking_status = label(tr("Waiting for the first monitoring check…"), "Muted")
        self.tracking_status.setWordWrap(True)
        tracked_layout.addWidget(self.tracking_status)
        self.tracked_table = table_widget(
            [
                tr("Conversation"),
                tr("Account"),
                tr("Turn"),
                tr("Codex goal"),
                tr("Last check"),
                tr("Menu"),
            ]
        )
        self.tracked_table.itemSelectionChanged.connect(self._tracked_selected)
        self.work_search = QLineEdit()
        self.work_search.setPlaceholderText(tr("Search tracked work…"))
        self.work_search.textChanged.connect(self._filter_work)
        self.work_filter = ComboBox()
        for title, value in (
            ("All work", "all"),
            ("Running", "running"),
            ("Needs attention", "attention"),
            ("Completed", "completed"),
        ):
            self.work_filter.addItem(tr(title), value)
        self.work_filter.currentIndexChanged.connect(self._filter_work)
        work_filters = QHBoxLayout()
        work_filters.setSpacing(12)
        work_filters.addWidget(self.work_search, 1)
        self.work_filter.setMinimumWidth(180)
        work_filters.addWidget(self.work_filter)
        tracked_layout.addLayout(work_filters)
        self.tracked_table.fit_columns((180, 90, 130, 110, 110, 90), (4, 2, 3, 3, 2, 0))
        self.tracked_table.setObjectName("WorkList")
        self.tracked_table.horizontalHeader().setSortIndicator(2, Qt.SortOrder.AscendingOrder)
        self.tracked_table.setSortingEnabled(True)
        tracked_layout.addWidget(self.tracked_table, 1)
        self.tracked_table.hide()
        self.tracked_empty = label(
            tr(
                "No work is being tracked yet. Active Codex work appears here after a monitoring check."
            ),
            "Muted",
        )
        self.tracked_empty.setWordWrap(True)
        self.tracked_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tracked_layout.addWidget(self.tracked_empty, 1)
        self.tracked_goal = QPushButton(tr("View goal"))
        self.tracked_goal.setObjectName("Secondary")
        self.tracked_goal.clicked.connect(self._read_tracked_goal)
        self.tracked_goal.setEnabled(False)
        self.tracked_goal.hide()
        tracked_actions = QHBoxLayout()
        self.tracked_open = QPushButton(tr("Open in Codex Desktop"))
        self.tracked_open.setObjectName("Primary")
        self.tracked_open.setEnabled(False)
        self.tracked_open.hide()
        self.tracked_open.clicked.connect(self._open_tracked)
        tracked_actions.addWidget(self.tracked_open)
        tracked_actions.addWidget(self.tracked_goal)
        self.details_button = QPushButton(tr("Work details"))
        self.details_button.setCheckable(True)
        self.details_button.setEnabled(False)
        self.details_button.hide()
        tracked_actions.addWidget(self.details_button)
        tracked_actions.addStretch()
        tracked_layout.addLayout(tracked_actions)
        self.work_detail_panel = QFrame()
        self.work_detail_panel.setObjectName("Panel")
        detail_layout = QVBoxLayout(self.work_detail_panel)
        detail_layout.setContentsMargins(24, 24, 24, 24)
        detail_layout.setSpacing(18)
        self.work_detail_title = label(tr("Select a tracked job"), "H2")
        self.work_detail_title.setWordWrap(True)
        self.work_detail_source = label("", "Muted")
        detail_header = QHBoxLayout()
        detail_header.addWidget(self.work_detail_title, 1)
        self.work_more = QPushButton()
        self.work_more.setIcon(make_icon("more", palette.muted, 20))
        self.work_more.setAccessibleName(tr("More actions"))
        work_menu = QMenu(self.work_more)
        self.work_shutdown = work_menu.addAction(tr("Shut down after this work"))
        self.work_shutdown.triggered.connect(self._shutdown_tracked)
        self.work_more.setMenu(work_menu)
        self.work_more.setEnabled(False)
        detail_header.addWidget(self.work_more)
        detail_layout.addLayout(detail_header)
        detail_layout.addWidget(self.work_detail_source)
        self.work_fields = {}
        for key, title in (
            ("turn", "Turn"),
            ("goal", "Codex goal"),
            ("account", "Account"),
            ("checked", "Last check"),
        ):
            field = QFrame()
            field.setObjectName("DetailRow")
            field_row = QHBoxLayout(field)
            field_row.setContentsMargins(0, 10, 0, 10)
            field.setMinimumHeight(44)
            field_row.addWidget(label(tr(title), "Muted"), 1)
            field_value = label("—", "FieldTitle")
            field_value.setWordWrap(True)
            field_row.addWidget(field_value, 2)
            self.work_fields[key] = field_value
            detail_layout.addWidget(field)
        note = label(
            tr(
                "Continuation is requested only after a verified usage-limit interruption of the same turn."
            ),
            "Caption",
        )
        note.setWordWrap(True)
        detail_layout.addWidget(note)
        detail_layout.addStretch()
        detail_scroll = QScrollArea()
        self.detail_scroll = detail_scroll
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        detail_scroll.setWidget(self.work_detail_panel)
        detail_scroll.setMaximumHeight(320)
        detail_scroll.hide()
        self.details_button.toggled.connect(detail_scroll.setVisible)
        work_page = QWidget()
        work_page_layout = QVBoxLayout(work_page)
        work_page_layout.setContentsMargins(0, 0, 0, 0)
        work_page_layout.setSpacing(12)
        work_page_layout.addWidget(tracked_panel, 1)
        work_page_layout.addWidget(detail_scroll)
        self.tabs.addTab(work_page, tr("Tracked work"))
        self.tabs.addTab(self.content_scroll, tr("Local conversations"))
        self.tabs.setTabToolTip(0, tr("Conversations monitored for work and goal status."))
        self.tabs.setTabToolTip(1, tr("All local Codex conversations, including completed work."))
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search conversations or folders…"))
        self.search.textChanged.connect(self._filter)
        self.project = ComboBox()
        self.project.setMinimumWidth(180)
        self.project.setAccessibleName(tr("Project"))
        self.project.addItem(tr("All projects"), "")
        self.project.currentIndexChanged.connect(self._filter)
        self.source = ComboBox()
        self.source.setAccessibleName(tr("Source"))
        for title, value in (
            ("Main conversations", "main"),
            ("All sources", ""),
            ("Desktop / VS Code", "vscode"),
            ("CLI", "cli"),
            ("App Server", "appServer"),
            ("Exec", "exec"),
            ("Subagents", "subAgent"),
        ):
            self.source.addItem(tr(title), value)
        self.source.currentIndexChanged.connect(self._filter)
        filters.addWidget(self.search, 1)
        filters.addWidget(self.project)
        filters.addWidget(self.source)
        self._content.addLayout(filters)
        self.status = label(tr("Refresh to read local conversations."), "Muted")
        self.status.setWordWrap(True)
        self._content.addWidget(self.status)
        self.table = table_widget(
            [tr("Conversation"), tr("Project"), tr("Source"), tr("Last activity"), tr("Workspace")]
        )
        self.table.fit_columns((230, 140, 135, 135, 180), (4, 2, 2, 2, 4))
        self.table.setMinimumHeight(220)
        self.table.horizontalHeader().setSortIndicator(3, Qt.SortOrder.DescendingOrder)
        self.table.setSortingEnabled(True)
        self.table.itemSelectionChanged.connect(self._selected)
        self._content.addWidget(self.table, 1)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText(
            tr("Select a conversation to see its full title, folder and preview.")
        )
        self.details.setMaximumHeight(110)
        self.details.hide()
        self._content.addWidget(self.details)
        self.action_host = QWidget()
        actions = QHBoxLayout(self.action_host)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(10)
        self.selection_label = label(tr("Selected conversation"), "FieldTitle")
        actions.addWidget(self.selection_label, 1)
        load = QPushButton(tr("Open in Codex Desktop"))
        load.setObjectName("Primary")
        load.clicked.connect(self._resume)
        more = QPushButton(tr("More actions"))
        more.setObjectName("MenuButton")
        menu = QMenu(more)
        self._actions: list[QPushButton | QAction] = [load]
        for text, handler in (
            ("Check continuation support", self._check_support),
            ("Continue after verified limit", self._continue_selected),
            ("Read Codex goal", self._native_goal),
            ("Save local goal note", self._note),
            ("Shut down after this work", self._shutdown_after),
        ):
            action = menu.addAction(tr(text))
            action.triggered.connect(handler)
            self._actions.append(action)
        menu.addSeparator()
        self.editor_menu = menu.addMenu(tr("Open project in editor"))
        self.editor_menu.aboutToShow.connect(self._editor_choices)
        self.details_toggle = menu.addAction(tr("Show conversation details"))
        self.details_toggle.setCheckable(True)
        self.details_toggle.toggled.connect(self._selected)
        self._actions.append(self.details_toggle)
        more.setMenu(menu)
        actions.addWidget(load)
        actions.addWidget(more)
        self._content.insertWidget(self._content.indexOf(self.table), self.action_host)
        self.action_host.hide()
        self.support_status = label("", "Muted")
        self.support_status.setWordWrap(True)
        self.support_status.hide()
        self._content.insertWidget(self._content.indexOf(self.table), self.support_status)

    @staticmethod
    def _project_key(thread) -> str:
        return thread.project_id or thread.cwd or "__none__"

    def _project_title(self, thread: ThreadRecord) -> str:
        from pathlib import PureWindowsPath

        if name := self._project_names.get(self._project_key(thread)):
            return name
        if thread.cwd:
            return PureWindowsPath(thread.cwd).name or thread.cwd
        return (
            tr("Project") + " " + thread.project_id[:8] if thread.project_id else tr("No project")
        )

    @staticmethod
    def _title(thread: ThreadRecord | None, fallback: str) -> str:
        if thread and thread.title and thread.title.strip():
            return thread.title.strip().replace("\n", " ")
        return fallback

    def refresh(self) -> None:
        self._sync()
        self.refresh_tracking()

    def refresh_tracking(self) -> None:
        if self._tracking_busy:
            return
        self._tracking_busy = True
        self.set_loading("tracking", True)
        self.runner.submit(self._tracking_snapshot(), self._render_tracking, self._tracking_failed)

    def _tracking_help(self) -> None:
        QMessageBox.information(
            self,
            tr("How tracking works"),
            tr(
                "These rows are saved observations of Codex conversations, not separate background processes. Pause monitoring to stop this app's automatic checks and continuation. To stop work started in Codex, open that conversation and press Stop there. Clear tracking removes observations and pauses monitoring."
            )
            + "\n\n"
            + tr(
                "Desktop conversations continue inside Codex Desktop. An unavailable connection is reported without moving work to another process."
            )
            + "\n\n"
            + tr(
                "Source labels do not prove where a conversation is running. Use More actions > Check continuation support."
            ),
        )

    def _clear_tracking(self) -> None:
        self.reset_tracking.setEnabled(False)

        def cleared(_result):
            self.reset_tracking.setEnabled(True)
            self.tracking_reset.emit()
            self._render_tracking(([], {}))
            self.tracking_status.setText(tr("Tracking cleared. Monitoring is paused."))

        def failed(_error):
            self.reset_tracking.setEnabled(True)
            self.tracking_status.setText(tr("Tracking could not be cleared. Try again."))

        self.runner.submit(WorkTracker().reset(), cleared, failed)

    def _request_tracking(self) -> None:
        self.tracking_status.setText(tr("Monitoring check requested…"))
        bus.publish("monitor.refresh_requested")

    def show_tracking_status(self, payload: dict) -> None:
        state = str(payload.get("state", "failed"))
        self.check_tracking.setEnabled(state != "checking")
        messages = {
            "response_too_large": "The conversation responded, but its state exceeds the supported size. No message was sent.",
            "checking": "Checking conversation activity…",
            "timed_out": "Tracking check timed out. Previous observations are preserved; try again.",
            "failed": "Tracking could not be checked. Previous observations are preserved; try again.",
            "signed_out": "Sign in to Codex to verify conversation tracking.",
        }
        if state in {"ready", "partial"}:
            message = tr(
                "Last check {time}: {count} conversations verified.",
                time=payload.get("checked_at", "—"),
                count=payload.get("checked", 0),
            )
            if state == "partial":
                message += " " + tr(
                    "Live state could not be verified for {count} conversations.",
                    count=payload.get("unavailable", 0),
                )
        else:
            message = tr(messages.get(state, messages["failed"]))
        if payload.get("unsupported", 0):
            message += " " + tr(
                "{count} conversations use a source without automatic continuation support.",
                count=payload["unsupported"],
            )
        self.tracking_status.setText(message)

    @staticmethod
    async def _tracking_snapshot() -> tuple[list[ObservedWork], dict[str, str]]:
        from codex_account_manager.adapters.credential_store import FileCredentialStore

        account_id = FileCredentialStore().active_account_hint()
        tracked = await WorkTracker().visible() if account_id else []
        profiles = await ProfileRepository().list()
        names = {
            account_hash(profile.bound_account_id): profile.alias
            for profile in profiles
            if profile.bound_account_id
        }
        return tracked, names

    def _tracking_failed(self, _error: Exception) -> None:
        self._tracking_busy = False
        self.set_loading("tracking", False)
        self.tracking_status.setText(tr("Tracked work could not be loaded. Refresh to try again."))

    def _render_tracking(self, snapshot: tuple[list[ObservedWork], dict[str, str]]) -> None:
        self.set_loading("tracking", False)
        selected = self.tracked_table.item(self.tracked_table.currentRow(), 0)
        selected_id = (
            selected.data(Qt.ItemDataRole.UserRole)
            if selected and self.tracked_table.selectedItems()
            else None
        )
        self._tracking_busy = False
        self._tracked, self._account_names = snapshot
        running = sum(
            w.verified and w.turn_status == "inProgress" and not w.limited for w in self._tracked
        )
        self.work_changed.emit(running, sum(not w.verified for w in self._tracked))
        self.tracked_count.setText(tr("{count} tracked", count=len(self._tracked)))
        if self._tracked and self.tracking_status.text() == tr(
            "Waiting for the first monitoring check…"
        ):
            self.tracking_status.setText(tr("Showing saved observations. Live check pending."))
        self.tracked_empty.setVisible(not self._tracked)
        self.tracked_table.setVisible(bool(self._tracked))
        self.tracked_goal.setVisible(bool(self._tracked))
        self.tracked_open.setVisible(bool(self._tracked))
        self.details_button.setVisible(bool(self._tracked))
        if not self._tracked:
            self.details_button.setChecked(False)
        self.tracked_table.clearSelection()
        self.tracked_table.setCurrentCell(-1, -1)
        sort_column = self.tracked_table.horizontalHeader().sortIndicatorSection()
        sort_order = self.tracked_table.horizontalHeader().sortIndicatorOrder()
        self.tracked_table.setSortingEnabled(False)
        self.tracked_table.setRowCount(len(self._tracked))
        titles = {
            thread.id: self._title(thread, tr("Tracked conversation")) for thread in self._threads
        }
        for row, work in enumerate(self._tracked):
            title = titles.get(work.thread_id, work.thread_id[:12])
            goal = (
                state_label(work.goal_status or "unknown")
                if work.goal_present
                else tr("No Codex goal")
            )
            turn_label, turn_detail = work_status(work)
            values = (
                title,
                self._account_names.get(work.account_hash, tr("Unknown account")),
                turn_label,
                goal,
                datetime.fromtimestamp(work.observed_at).strftime("%d.%m %H:%M"),
            )
            for column, value in enumerate(values):
                item = (
                    SortableItem(value.replace("\n", " "), work.observed_at)
                    if column == 4
                    else SortableItem(
                        "",
                        0
                        if work.turn_status == "inProgress"
                        else 1
                        if work.turn_status == "awaitingDesktop"
                        else 2,
                    )
                    if column == 2
                    else QTableWidgetItem(value.replace("\n", " "))
                )
                item.setToolTip(work.thread_id if column == 0 else value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, work.thread_id)
                elif column == 2:
                    item.setData(Qt.ItemDataRole.UserRole + 2, value)
                    item.setData(Qt.ItemDataRole.AccessibleTextRole, value)
                    item.setToolTip(turn_detail or value)
                self.tracked_table.setItem(row, column, item)
            status_host, status_layout = table_cell()
            status_host.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            tone = (
                "warning"
                if not work.verified or work.limited or work.turn_status == "awaitingDesktop"
                else "primary"
                if work.turn_status == "inProgress"
                else "success"
                if work.turn_status == "completed"
                else "muted"
            )
            badge = Pill(values[2], tone)
            badge.setWordWrap(True)
            badge.setToolTip(turn_detail or turn_label)
            status_host.setToolTip(badge.toolTip())
            status_layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
            status_layout.addStretch()
            self.tracked_table.setCellWidget(row, 2, status_host)
            self.tracked_table.setItem(row, 5, QTableWidgetItem(""))
            action_host, action_layout = table_cell()
            action_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
            row_more = QToolButton()
            row_more.setObjectName("RowMore")
            row_more.setToolTip(tr("More actions"))
            row_more.setAccessibleName(tr("Actions for {work}", work=title))
            set_button_icon(row_more, "more", self.palette_.muted)
            row_more.clicked.connect(
                lambda _checked=False, thread_id=work.thread_id, button=row_more: (
                    self._show_work_menu(thread_id, button)
                )
            )
            action_layout.addWidget(row_more)
            self.tracked_table.setCellWidget(row, 5, action_host)
            if work.thread_id == selected_id:
                self.tracked_table.selectRow(row)
        self.tracked_table.setSortingEnabled(True)
        self.tracked_table.sortItems(sort_column, sort_order)
        self.tracked_table.verticalHeader().setDefaultSectionSize(58)
        self._filter_work()
        if selected_id is None and self.tracked_table.rowCount():
            for row in range(self.tracked_table.rowCount()):
                if not self.tracked_table.isRowHidden(row):
                    self.tracked_table.selectRow(row)
                    break
        self._tracked_selected()

    def _tracked_selected(self) -> None:
        selected = bool(self.tracked_table.selectedItems())
        self.tracked_goal.setEnabled(selected)
        self.tracked_open.setEnabled(selected)
        self.details_button.setEnabled(selected)
        self.work_more.setEnabled(selected)
        item = self.tracked_table.item(self.tracked_table.currentRow(), 0) if selected else None
        work = next(
            (
                w
                for w in self._tracked
                if item and w.thread_id == item.data(Qt.ItemDataRole.UserRole)
            ),
            None,
        )
        thread = next((t for t in self._threads if work and t.id == work.thread_id), None)
        self.work_detail_title.setText(
            self._title(thread, tr("Tracked conversation"))
            if thread
            else tr("Tracked conversation")
            if work
            else tr("Select a tracked job")
        )
        self.work_detail_source.setText(source_label(thread.source) if thread else "")
        for key, column in (("turn", 2), ("goal", 3), ("account", 1), ("checked", 4)):
            cell = (
                self.tracked_table.item(self.tracked_table.currentRow(), column)
                if selected
                else None
            )
            self.work_fields[key].setText(
                cell.toolTip() if cell and key == "turn" else cell.text() if cell else "—"
            )

    def _filter_work(self, *_args) -> None:
        query = self.work_search.text().casefold().strip()
        mode = self.work_filter.currentData()
        for row in range(self.tracked_table.rowCount()):
            item = self.tracked_table.item(row, 0)
            if item is None:
                continue
            work = next(
                (w for w in self._tracked if w.thread_id == item.data(Qt.ItemDataRole.UserRole)),
                None,
            )
            running = bool(
                work
                and work.verified
                and work.turn_status in {"inProgress", "running"}
                and not work.limited
            )
            completed = bool(work and work.turn_status == "completed")
            matches = (
                mode == "all"
                or (mode == "running" and running)
                or (mode == "completed" and completed)
                or (mode == "attention" and not running and not completed)
            )
            self.tracked_table.setRowHidden(row, query not in item.text().casefold() or not matches)
        if self.tracked_table.currentRow() >= 0 and self.tracked_table.isRowHidden(
            self.tracked_table.currentRow()
        ):
            self.tracked_table.clearSelection()
        self._tracked_selected()

    def _shutdown_tracked(self) -> None:
        item = self.tracked_table.item(self.tracked_table.currentRow(), 0)
        if item and self.tracked_table.selectedItems():
            self.shutdown_requested.emit(item.data(Qt.ItemDataRole.UserRole))

    def _select_work(self, thread_id: str) -> bool:
        for row in range(self.tracked_table.rowCount()):
            item = self.tracked_table.item(row, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == thread_id:
                self.tracked_table.selectRow(row)
                return True
        return False

    def _show_work_menu(self, thread_id: str, button: QToolButton) -> None:
        if not self._select_work(thread_id):
            return
        menu = QMenu(button)
        open_action = menu.addAction(tr("Open in Codex Desktop"))
        goal_action = menu.addAction(tr("View goal"))
        continuation_action = menu.addAction(tr("Continuation details"))
        retry_action = menu.addAction(tr("Continue after verified limit"))
        menu.addSeparator()
        shutdown_action = menu.addAction(tr("Shut down after this work"))
        remove_action = menu.addAction(tr("Remove observation"))
        chosen = menu.exec(button.mapToGlobal(QPoint(0, button.height())))
        if chosen is open_action:
            self._open_tracked()
        elif chosen is goal_action:
            self._read_tracked_goal()
        elif chosen is continuation_action:
            from codex_account_manager.storage.repositories import EventRepository

            self.runner.submit(
                EventRepository().continuation_history(thread_id), self._show_continuation_history
            )
        elif chosen is retry_action:
            self._continue_verified(thread_id)
        elif chosen is shutdown_action:
            self._shutdown_tracked()
        elif chosen is remove_action:
            self.runner.submit(WorkTracker().forget([thread_id]), lambda _: self.refresh_tracking())

    def _show_continuation_history(self, events: list[dict]) -> None:
        from codex_account_manager.gui.i18n import continuation_detail

        text = "\n\n".join(
            datetime.fromisoformat(event["at"]).astimezone().strftime("%d.%m %H:%M:%S")
            + "\n"
            + continuation_detail(event["payload"])
            for event in events
        )
        QMessageBox.information(
            self,
            tr("Continuation details"),
            text
            or tr(
                "No continuation result was recorded for this conversation. Older versions did not save preparation failures."
            ),
        )

    def _open_tracked(self) -> None:
        row = self.tracked_table.currentRow()
        item = self.tracked_table.item(row, 0) if row >= 0 else None
        if item:
            self.open_desktop(item.data(Qt.ItemDataRole.UserRole))

    def open_desktop(self, thread_id: str) -> None:
        from codex_account_manager.platform.windows import open_conversation

        try:
            open_conversation(thread_id)
        except (OSError, RuntimeError, ValueError):
            self.tracking_status.setText(
                tr("Codex Desktop could not be opened. Open the conversation from Codex.")
            )

    def _read_tracked_goal(self) -> None:
        row = self.tracked_table.currentRow()
        item = self.tracked_table.item(row, 0) if row >= 0 else None
        if not item:
            return
        from codex_account_manager.continuity.service import ContinuityService

        self.runner.submit(
            ContinuityService().read_native_goal(item.data(Qt.ItemDataRole.UserRole)),
            self._show_native_goal,
        )

    def _sync(self) -> None:
        if self._busy:
            return
        from codex_account_manager.continuity.service import ContinuityService

        self._busy = True
        self.set_loading("conversations", True)
        self.sync.setEnabled(False)
        self.status.setText(tr("Reading all local conversations…"))
        self.runner.submit(
            ContinuityService().sync_threads(), self._render_threads, self._sync_failed
        )

    def _sync_failed(self, error: Exception) -> None:
        self._busy = False
        self.set_loading("conversations", False)
        self.sync.setEnabled(True)
        self.status.setText(
            tr("Could not refresh. The previous list is unchanged: {error}", error=error)
        )

    def _render_threads(self, threads) -> None:
        self._busy = False
        self.set_loading("conversations", False)
        self.sync.setEnabled(True)
        self._threads = sorted(threads, key=lambda thread: thread.updated_at, reverse=True)
        import ntpath
        from pathlib import PureWindowsPath

        self._project_names.clear()
        folders: dict[str, list[str]] = {}
        for thread in self._threads:
            if thread.cwd:
                folders.setdefault(self._project_key(thread), []).append(thread.cwd)
        for key, members in folders.items():
            try:
                common = ntpath.commonpath(members)
            except ValueError:
                continue
            if name := PureWindowsPath(common).name:
                self._project_names[key] = name
        previous = self.project.currentData()
        with QSignalBlocker(self.project):
            self.project.clear()
            self.project.addItem(tr("All projects"), "")
            projects: dict[str, ThreadRecord] = {}
            for thread in self._threads:
                projects.setdefault(self._project_key(thread), thread)
            for key, thread in sorted(
                projects.items(), key=lambda item: self._project_title(item[1]).casefold()
            ):
                self.project.addItem(self._project_title(thread), key)
                self.project.setItemData(
                    self.project.count() - 1, thread.cwd or key, Qt.ItemDataRole.ToolTipRole
                )
            self.project.setCurrentIndex(max(0, self.project.findData(previous)))
        self._filter()
        if self._tracked:
            self._render_tracking((self._tracked, self._account_names))

    def _filter(self, *_args) -> None:
        from codex_account_manager.core.paths import paths

        query = self.search.text().casefold().strip()
        project, source = self.project.currentData(), self.source.currentData()
        rows = [
            thread
            for thread in self._threads
            if (not project or self._project_key(thread) == project)
            and (
                not source
                or (source == "main" and not (thread.source or "").startswith("subAgent"))
                or (thread.source or "").startswith(source)
            )
            and query
            in " ".join(
                (thread.title or "", thread.preview or "", thread.cwd or "", thread.id)
            ).casefold()
        ]
        selected = self._current()
        selected_id = selected.id if selected else None
        with QSignalBlocker(self.table):
            sort_column = self.table.horizontalHeader().sortIndicatorSection()
            sort_order = self.table.horizontalHeader().sortIndicatorOrder()
            self.table.setSortingEnabled(False)
            self.table.clearSelection()
            self.table.setRowCount(0)
            self.table.setRowCount(len(rows))
            for row, thread in enumerate(rows):
                title = self._title(thread, thread.id)
                values = (
                    title,
                    self._project_title(thread),
                    source_label(thread.source),
                    thread.updated_at.astimezone().strftime("%d.%m.%Y %H:%M"),
                    thread.cwd or "—",
                )
                for column, value in enumerate(values):
                    item = (
                        SortableItem(value, thread.updated_at.timestamp())
                        if column == 3
                        else QTableWidgetItem(value.replace("\n", " "))
                    )
                    item.setToolTip(value)
                    if column == 0:
                        item.setData(Qt.ItemDataRole.UserRole, thread)
                    self.table.setItem(row, column, item)
            self.table.setSortingEnabled(True)
            self.table.sortItems(sort_column, sort_order)
            if selected_id is not None:
                for row in range(self.table.rowCount()):
                    selected_item = self.table.item(row, 0)
                    if (
                        selected_item
                        and selected_item.data(Qt.ItemDataRole.UserRole).id == selected_id
                    ):
                        self.table.selectRow(row)
                        break
        self.status.setText(
            tr(
                "{shown} of {total} local conversations",
                shown=len(rows),
                total=len(self._threads),
            )
        )
        self.status.setToolTip(
            str(paths.shared_codex_home)
            + "\n"
            + tr(
                "Includes local projects and subagents. Archived, cloud-only and other-device conversations are not included."
            )
        )
        self._selected()

    def _current(self):
        if not self.table.selectedItems():
            return None
        item = self.table.item(self.table.currentRow(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _selected(self) -> None:
        thread = self._current()
        self.details.setVisible(thread is not None and self.details_toggle.isChecked())
        self.action_host.setVisible(thread is not None)
        self.selection_label.setText(tr("Selected conversation"))
        selection = thread.id if thread else None
        if selection != self._support_selection:
            self._support_generation += 1
            self._support_selection = selection
            self.support_status.hide()
            self.support_status.clear()
        for button in self._actions:
            button.setEnabled(thread is not None)
        self.details.setPlainText(
            ""
            if thread is None
            else "\n".join(
                (
                    thread.title or thread.id,
                    thread.cwd or tr("No project"),
                    source_label(thread.source),
                    thread.preview or "",
                )
            )
        )

    def _check_support(self) -> None:
        thread = self._current()
        if thread is None or self._support_busy:
            return
        from codex_account_manager.continuity.service import ContinuityService

        self._support_busy = True
        self._support_generation += 1
        generation = self._support_generation
        self.support_status.show()
        self.support_status.setText(tr("Checking continuation support without starting work…"))
        self.runner.submit(
            ContinuityService().continuation_support(thread.id),
            lambda result: self._show_support(generation, result),
            lambda error: self._show_support(generation, "unverified"),
        )

    def _continue_selected(self) -> None:
        thread = self._current()
        if thread:
            self._continue_verified(thread.id)

    def _continue_verified(self, thread_id: str) -> None:
        if self._continue_busy:
            return
        if (
            QMessageBox.question(
                self,
                tr("Continue after verified limit"),
                tr(
                    "Verify this conversation's quota failure and queue one continuation on the active account? This can use quota. Changed, paused or already attempted work will not be sent."
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        from codex_account_manager.continuity.service import ContinuityService

        self._continue_busy = True

        def completed(error=None):
            self._continue_busy = False
            message = (
                tr(str(error))
                if error
                else tr(
                    "Continuation queued for verification. Its result will appear in continuation details."
                )
            )
            self.tracking_status.setText(message)
            self.support_status.setText(message)
            self.support_status.show()

        self.runner.submit(
            ContinuityService().queue_verified_continuation(thread_id),
            lambda _: completed(),
            completed,
        )

    def _show_support(self, generation: int, result: str) -> None:
        self._support_busy = False
        if generation != self._support_generation:
            return
        messages = {
            "ide": "The local Codex conversation owner responded. IDE continuation is experimental and still requires a verified usage-limit interruption.",
            "cli": "CLI / App Server source verified. Automatic continuation still requires an observed usage-limit interruption and a valid goal state.",
            "desktop": "Codex Desktop can read this conversation now. Native continuation is experimental; an observed usage-limit interruption is still required.",
            "unsupported": "Automatic continuation is unavailable for this source. Continue in the application where the conversation was started.",
            "unverified": "The running conversation owner could not be verified. Keep the original Codex app or IDE extension open and check the continuation setting. No message was sent.",
        }
        self.support_status.show()
        self.support_status.setText(tr(messages.get(result, messages["unverified"])))

    def _editor_choices(self) -> None:
        from codex_account_manager.platform.editors import installed_editors

        self.editor_menu.clear()
        thread = self._current()
        for editor in installed_editors():
            action = self.editor_menu.addAction(editor)
            action.setEnabled(thread is not None and bool(thread.cwd))
            action.triggered.connect(lambda checked=False, name=editor: self._open_editor(name))
        if not self.editor_menu.actions():
            self.editor_menu.addAction(
                tr("No supported editor found in standard install locations.")
            ).setEnabled(False)

    def _open_editor(self, editor: str) -> None:
        from codex_account_manager.platform.editors import open_project

        thread = self._current()
        if thread is None:
            return
        self.support_status.show()
        try:
            open_project(editor, thread.cwd or "")
        except (OSError, ValueError):
            self.support_status.setText(
                tr(
                    "The project could not be opened. Check that its local folder and editor still exist."
                )
            )
        else:
            self.support_status.setText(
                tr(
                    "Project opened in the editor. Select the conversation in its Codex extension; this action does not switch the IDE account or send a message."
                )
            )

    def _shutdown_after(self) -> None:
        thread = self._current()
        if thread:
            self.shutdown_requested.emit(thread.id)

    def _note(self) -> None:
        thread = self._current()
        if thread:
            self.notes_requested.emit(thread.id)

    def _native_goal(self) -> None:
        thread = self._current()
        if not thread:
            return
        from codex_account_manager.continuity.service import ContinuityService

        self.runner.submit(ContinuityService().read_native_goal(thread.id), self._show_native_goal)

    def _show_native_goal(self, goal) -> None:
        message = (
            tr("The Codex goal could not be read. This is not evidence that no goal exists.")
            if goal is None
            else tr("This conversation has no Codex goal.")
            if not goal.present
            else state_label(goal.status or "unknown") + "\n\n" + (goal.objective or "")
        )
        dialog = QMessageBox(self)
        dialog.setWindowTitle(tr("Read Codex goal"))
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(message)
        dialog.exec()

    def _resume(self) -> None:
        thread = self._current()
        if thread:
            self.open_desktop(thread.id)
