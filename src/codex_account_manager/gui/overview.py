"""Overview screen."""

from __future__ import annotations

import time
from collections.abc import Callable

from PySide6.QtCore import QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from codex_account_manager.accounts.service import AccountService
from codex_account_manager.core.errors import OperationBusyError
from codex_account_manager.domain.models import ProfileHealth
from codex_account_manager.gui.account_table import AccountTable, account_sort_key
from codex_account_manager.gui.async_runner import AsyncRunner
from codex_account_manager.gui.design import DARK, make_icon, set_button_icon
from codex_account_manager.gui.i18n import (
    tr,
)
from codex_account_manager.gui.view_base import BaseView, EmptyState, view_header
from codex_account_manager.gui.widgets import AccountRow, ComboBox, CompactSwitch, label


class DashboardView(BaseView):
    manage_requested = Signal()
    work_requested = Signal()
    monitor_requested = Signal()

    def __init__(
        self,
        runner: AsyncRunner,
        accounts: AccountService,
        on_switch: Callable[[str], None],
        palette=DARK,
    ):
        super().__init__(palette)
        self.setObjectName("Dashboard")
        self.runner = runner
        self.accounts = accounts
        self.on_switch = on_switch
        self._account_rows: dict[str, AccountRow] = {}
        self._health: list[ProfileHealth] = []
        self._busy = False
        self._work_busy = False
        self._loaded = False
        self._load_error: str | None = None
        self.refresh_btn = QPushButton(tr("Refresh usage"))
        self.refresh_btn.setObjectName("Primary")
        set_button_icon(self.refresh_btn, "refresh", "#ffffff", 18)
        self.refresh_btn.clicked.connect(self.refresh)
        header = view_header(
            tr("Overview"),
            tr("Usage and availability at a glance."),
            self.refresh_btn,
        )
        header.setMaximumHeight(100)
        self._root.addWidget(header)
        self.summary = label("", "Muted")
        self.summary.setWordWrap(True)
        self.alert = label("", "InlineWarning")
        self.alert.setWordWrap(True)
        self.alert.hide()
        self._root.addWidget(self.alert)
        self._switch_mode = "manual"
        self._monitoring = False
        self.mode_label = label(tr("Account and work checks paused"), "FieldTitle")
        self.mode_label.setWordWrap(True)
        self.mode_strip = QFrame()
        mode_layout = QVBoxLayout(self.mode_strip)
        mode_layout.setContentsMargins(0, 4, 0, 8)
        mode_layout.setSpacing(4)
        mode_actions = QHBoxLayout()
        mode_actions.addWidget(self.mode_label, 1)
        self.monitor_action = QPushButton(tr("Start"))
        self.monitor_action.setObjectName("Ghost")
        self.monitor_action.clicked.connect(self.monitor_requested.emit)
        mode_actions.addWidget(self.monitor_action)
        mode_layout.addLayout(mode_actions)
        self.mode_detail = label("", "Caption")
        self.mode_detail.setWordWrap(True)
        mode_layout.addWidget(self.mode_detail)
        self._root.addWidget(self.mode_strip)
        self.hero = QFrame()
        hero_box = QHBoxLayout(self.hero)
        hero_box.setContentsMargins(0, 0, 0, 0)
        hero_box.setSpacing(12)
        hero_box.addWidget(label(tr("Accounts"), "H2"))
        hero_box.addWidget(self.summary, 1)
        hero_box.addWidget(label(tr("Hide email addresses"), "Caption"))
        self.email_toggle = CompactSwitch(tr("Hide email addresses"))
        self.email_toggle.setObjectName("PrivacySwitch")
        self.email_toggle.setAccessibleName(tr("Hide email addresses"))
        self.email_toggle.toggled.connect(self._privacy_changed)
        hero_box.addWidget(self.email_toggle)
        manage = QPushButton(tr("Manage profiles"))
        set_button_icon(manage, "settings", self.palette_.muted, 18)
        manage.clicked.connect(self.manage_requested.emit)
        hero_box.addWidget(manage)
        self.work_panel = QFrame()
        self.work_panel.setObjectName("Panel")
        self.work_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        work_layout = QHBoxLayout(self.work_panel)
        work_layout.setContentsMargins(16, 12, 16, 12)
        work_icon = QLabel()
        work_icon.setObjectName("WorkIcon")
        work_icon.setFixedSize(40, 40)
        work_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        work_icon.setPixmap(make_icon("document", self.palette_.primary, 24).pixmap(24, 24))
        work_layout.addWidget(work_icon)
        work_layout.setSpacing(12)
        work_text = QVBoxLayout()
        work_text.setSpacing(4)
        self.work_title = label(tr("No verified running work yet"), "FieldTitle", elide=True)
        self.work_detail = label(
            tr("Start monitoring to follow running Codex conversations."), "Caption"
        )
        self.work_detail.setWordWrap(True)
        work_text.addWidget(self.work_title)
        work_text.addWidget(self.work_detail)
        work_layout.addLayout(work_text, 1)
        self.view_work = QPushButton(tr("View conversations"))
        set_button_icon(self.view_work, "chat", self.palette_.text, 20)
        self.view_work.clicked.connect(self.work_requested.emit)
        work_layout.addWidget(self.view_work)
        self._root.addWidget(self.work_panel)
        self.accounts_panel = QFrame()
        self.accounts_panel.setObjectName("AccountsPanel")
        accounts_layout = QVBoxLayout(self.accounts_panel)
        accounts_layout.setContentsMargins(20, 18, 20, 18)
        accounts_layout.setSpacing(16)
        accounts_layout.addWidget(self.hero)
        self.toolbar_host = QWidget()
        toolbar = QHBoxLayout(self.toolbar_host)
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(10)
        self.search = QLineEdit()
        self.search.setObjectName("AccountSearch")
        self.search.setPlaceholderText(tr("Search by account name or email…"))
        self.search.addAction(
            make_icon("diagnostics", self.palette_.muted, 19),
            QLineEdit.ActionPosition.LeadingPosition,
        )
        self.search.setAccessibleName(tr("Filter profiles"))
        self.search.setMinimumWidth(120)
        self.search.textChanged.connect(self._filter)
        toolbar.addWidget(self.search, 1)
        self.account_filter = ComboBox()
        self.account_filter.setAccessibleName(tr("Filter accounts by availability"))
        for title, value in (
            ("All accounts", "all"),
            ("Ready to use", "ready"),
            ("Has reset credits", "credits"),
            ("Needs attention", "attention"),
        ):
            self.account_filter.addItem(tr(title), value)
        self.account_filter.currentIndexChanged.connect(lambda _index: self._filter())
        self.account_filter.setFixedWidth(200)
        toolbar.addWidget(self.account_filter)
        self.sort_order = ComboBox()
        self.sort_order.setAccessibleName(tr("Sort accounts"))
        for title, value in (
            ("Active account first", "active"),
            ("Account name A–Z", "name"),
            ("Account name Z–A", "name_desc"),
            ("5-hour: most remaining", "five_most"),
            ("5-hour: least remaining", "five_least"),
            ("Weekly: most remaining", "weekly_most"),
            ("Weekly: least remaining", "weekly_least"),
            ("Next renewal first", "renewal"),
            ("Most reset credits", "credits"),
            ("Status: available first", "status"),
            ("Status: attention first", "status_desc"),
        ):
            self.sort_order.addItem(tr(title), value)
        self._sort_chosen = False
        self.sort_order.currentIndexChanged.connect(self._sort_changed)
        self.sort_order.setToolTip(self.sort_order.currentText())
        sort_menu = QMenu(self)
        for index in range(self.sort_order.count()):
            action = sort_menu.addAction(self.sort_order.itemText(index))
            action.setCheckable(True)
            action.triggered.connect(
                lambda _checked=False, value=index: self.sort_order.setCurrentIndex(value)
            )
        self.sort_button = QPushButton(tr("Sort"))
        set_button_icon(self.sort_button, "sort", self.palette_.muted, 18)
        self.sort_button.setFixedWidth(104)
        self.sort_button.setMenu(sort_menu)
        self.sort_button.setAccessibleName(tr("Sort accounts"))
        toolbar.addWidget(self.sort_button)
        self.sort_order.hide()
        accounts_layout.addWidget(self.toolbar_host)
        self.account_table = AccountTable(self.palette_)
        self.account_table.sort_requested.connect(self._sort_column)
        self.table_header = self.account_table.header
        self.accounts_scroll = self.account_table.scroll_area
        self._header_buttons = self.account_table.header_buttons
        accounts_layout.addWidget(self.account_table, 1)
        self.status = label(tr("Ready to refresh"), "Muted")
        accounts_layout.addWidget(self.status)
        self._root.addWidget(self.accounts_panel, 1)
        self._filter()
        from codex_account_manager.storage.repositories import SettingsRepository

        self.runner.submit(SettingsRepository().get("account_sort", "active"), self._restore_sort)

    def _privacy_changed(self, hidden: bool) -> None:
        self.email_toggle.setAccessibleName(
            tr("Show email addresses") if hidden else tr("Hide email addresses")
        )
        self._filter()

    def _sort_column(self, modes: tuple[str, str]) -> None:
        mode = modes[1] if self.sort_order.currentData() == modes[0] else modes[0]
        self.sort_order.setCurrentIndex(self.sort_order.findData(mode))

    def set_automation(self, mode: str, enabled: bool, seconds: int) -> None:
        self._switch_mode = mode
        self._monitoring = enabled
        self.monitor_action.setText(tr("Pause") if enabled else tr("Start"))
        if self.work_title.text() == tr("No verified running work yet"):
            self.work_detail.setText(
                tr("Monitoring is on. Running work will appear here after a check.")
                if enabled
                else tr("Start monitoring to follow running Codex conversations.")
            )
        self.mode_detail.setText(
            tr(
                "Account limits and running work are checked every {seconds} seconds.",
                seconds=seconds,
            )
            if enabled
            else tr("Automatic checks and account switching are paused.")
        )
        self._filter()

    def _restore_sort(self, mode: str) -> None:
        if self._sort_chosen:
            return
        with QSignalBlocker(self.sort_order):
            self.sort_order.setCurrentIndex(max(0, self.sort_order.findData(mode)))
        self.sort_order.setToolTip(self.sort_order.currentText())
        self.sort_button.setToolTip(self.sort_order.currentText())
        for action in self.sort_button.menu().actions():
            action.setChecked(action.text() == self.sort_order.currentText())
        self._filter()

    def _sort_changed(self, _index: int) -> None:
        from codex_account_manager.storage.repositories import SettingsRepository

        self._sort_chosen = True
        self.sort_order.setToolTip(self.sort_order.currentText())
        self.sort_button.setToolTip(self.sort_order.currentText())
        for action in self.sort_button.menu().actions():
            action.setChecked(action.text() == self.sort_order.currentText())
        self._filter()
        self.runner.submit(SettingsRepository().set("account_sort", self.sort_order.currentData()))

    def refresh(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.set_loading("accounts", True)
        self.refresh_work()
        self._load_error = None
        self._filter()
        self.status.setText(tr("Checking account health…"))
        self.refresh_btn.setEnabled(False)
        self.runner.submit(self.accounts.all_health(), self._render, self._on_error)

    def _render(self, health: list[ProfileHealth]) -> None:
        from datetime import datetime

        from codex_account_manager.continuity.policy import SwitchPolicy

        self._busy = False
        self.set_loading("accounts", False)
        self.refresh_btn.setEnabled(True)
        self._loaded = True
        self._load_error = None
        self.alert.hide()
        self._health = health
        for widget in (self.summary, self.hero, self.toolbar_host):
            widget.setVisible(bool(health))
        available = sum(SwitchPolicy._is_available(item) for item in health)
        self.summary.setText(
            tr(
                "{total} accounts · {ready} available · {attention} need attention",
                total=len(health),
                ready=available,
                attention=len(health) - available,
            )
        )
        self.status.setText(
            tr(
                "Updated {time} · {count} accounts",
                time=datetime.now().strftime("%H:%M"),
                count=len(health),
            )
        )
        self._filter()

    def _filter(self, _text: str = "") -> None:
        self.account_table.palette_ = self.palette_
        self.account_table.clear()
        self._account_rows = self.account_table.rows
        for widget in (self.hero, self.toolbar_host):
            widget.setVisible(self._loaded and bool(self._health))
        if not self._loaded:
            self._show_pending_state()
            return
        self.account_table.set_sort(self.sort_order.currentData())
        query = self.search.text().casefold().strip()
        from codex_account_manager.continuity.policy import SwitchPolicy

        mode = self.account_filter.currentData()
        health = [
            item
            for item in self._health
            if (
                query in item.alias.casefold()
                or (
                    not self.email_toggle.isChecked()
                    and item.account_match is True
                    and query in (item.email or "").casefold()
                )
            )
            and (
                mode == "all"
                or (mode == "ready" and SwitchPolicy._is_available(item))
                or (mode == "attention" and not SwitchPolicy._is_available(item))
                or (
                    mode == "credits"
                    and item.reset_credits is not None
                    and item.reset_credits.available_count > 0
                )
            )
        ]
        if not health:
            empty = EmptyState(
                tr("No matching profiles") if self._health else tr("Bring your first account"),
                tr("Try another name or account filter.")
                if self._health
                else tr(
                    "Start with a name you recognize. Sign in securely through Codex; your account is linked automatically."
                ),
                tr("Add a profile") if not self._health else None,
                self.manage_requested.emit if not self._health else None,
            )
            self.account_table.show_state(empty)
            return
        now = time.time()
        ordered = sorted(
            health,
            key=lambda h: account_sort_key(h, self.sort_order.currentData(), now),
            reverse=self.sort_order.currentData() in {"name_desc", "status_desc"},
        )
        current = next((h for h in self._health if h.is_active), None)
        candidate = None
        if self._monitoring and self._switch_mode != "manual" and current is not None:
            candidate = SwitchPolicy()._best_candidate(current, self._health)
        self.account_table.set_profiles(
            ordered,
            email_visible=not self.email_toggle.isChecked(),
            candidate_id=candidate.profile_id if candidate is not None else None,
            confirmation=self._switch_mode == "confirm",
            on_switch=self.on_switch,
            on_sign_in=self.manage_requested.emit,
            on_refresh=self.refresh,
            on_manage=self.manage_requested.emit,
        )
        self._account_rows = self.account_table.rows

    def refresh_work(self) -> None:
        if self._work_busy:
            return
        self._work_busy = True
        self.set_loading("work", True)

        async def read():
            from codex_account_manager.continuity.tracking import WorkTracker
            from codex_account_manager.storage.repositories import ThreadRepository

            work = await WorkTracker().visible()
            thread = await ThreadRepository().get(work[0].thread_id) if work else None
            return work, thread

        self.runner.submit(read(), self._render_work, self._work_failed)

    def _render_work(self, result) -> None:
        from datetime import datetime

        from codex_account_manager.gui.i18n import state_label

        self._work_busy = False
        work, thread = result
        self.set_loading("work", False)
        if not work:
            self.work_title.setText(tr("No verified running work yet"))
            self.work_detail.setText(
                tr("Monitoring is on. Running work will appear here after a check.")
                if self._monitoring
                else tr("Start monitoring to follow running Codex conversations.")
            )
            return
        selected = work[0]
        title = (thread.title or thread.preview) if thread else None
        self.work_title.setText(title or tr("Tracked conversation"))
        state = (
            tr("Live state unavailable")
            if not selected.verified
            else tr("Limit reached")
            if selected.limited
            else state_label(selected.turn_status)
        )
        goal = (
            state_label(selected.goal_status or "unknown")
            if selected.goal_present
            else tr("No Codex goal")
        )
        self.work_detail.setText(
            tr(
                "{count} tracked · Last observed: {state} · Goal: {goal} · {time}",
                count=len(work),
                state=state,
                goal=goal,
                time=datetime.fromtimestamp(selected.observed_at).strftime("%d.%m %H:%M"),
            )
        )

    def _work_failed(self, _error) -> None:
        self._work_busy = False
        self.set_loading("work", False)
        self.work_detail.setText(tr("Tracked work could not be loaded. Refresh to try again."))

    def _on_error(self, exc: Exception) -> None:
        self._busy = False
        self.set_loading("accounts", False)
        self.refresh_btn.setEnabled(True)
        self._load_error = str(exc)
        if isinstance(exc, OperationBusyError):
            self._load_error = None
            self.alert.hide()
            self.status.setText(
                tr("Account operation in progress. Refresh will retry automatically.")
            )
            QTimer.singleShot(2000, self, self.refresh)
            return
        self.alert.setText(
            tr("Account usage could not be refreshed. Check your connection and try again.")
        )
        self.alert.show()
        self.status.setText(tr("Could not refresh: {error}", error=exc))
        self._filter()

    def _show_pending_state(self) -> None:
        panel = QFrame()
        panel.setObjectName("Empty")
        panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        panel.setMaximumHeight(210)
        box = QVBoxLayout(panel)
        box.setContentsMargins(32, 32, 32, 32)
        box.setSpacing(14)
        failed = self._load_error is not None
        title = label(
            tr("Accounts could not be loaded") if failed else tr("Loading accounts…"), "H2"
        )
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(title)
        hint = label(
            tr("Check your connection and Codex installation, then try again.")
            if failed
            else tr("Reading saved accounts and checking usage. This may take a moment."),
            "Muted",
        )
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        box.addWidget(hint)
        if failed:
            retry = QPushButton(tr("Try again"))
            retry.setObjectName("Primary")
            retry.clicked.connect(self.refresh)
            box.addWidget(retry, 0, Qt.AlignmentFlag.AlignCenter)
        else:
            progress = QProgressBar()
            progress.setRange(0, 0)
            progress.setTextVisible(False)
            progress.setFixedHeight(4)
            progress.setMaximumWidth(240)
            box.addWidget(progress, 0, Qt.AlignmentFlag.AlignHCenter)
        self.account_table.show_state(panel)
