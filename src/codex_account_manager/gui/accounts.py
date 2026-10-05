"""Saved accounts and sign-in controls."""

from __future__ import annotations

import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QToolButton,
    QWidget,
)

from codex_account_manager.accounts.service import AccountService
from codex_account_manager.continuity.policy import SwitchPolicy
from codex_account_manager.core.errors import OperationBusyError
from codex_account_manager.domain.models import Profile, ProfileHealth
from codex_account_manager.gui.account_table import AccountTable, account_sort_key, display_health
from codex_account_manager.gui.async_runner import AsyncRunner
from codex_account_manager.gui.design import DARK, set_button_icon
from codex_account_manager.gui.dialogs import prompt_text
from codex_account_manager.gui.i18n import (
    tr,
)
from codex_account_manager.gui.view_base import (
    BaseView,
    EmptyState,
    metric_card,
    page_panel,
    view_header,
)
from codex_account_manager.gui.widgets import ComboBox, label


class AccountsView(BaseView):
    def __init__(self, runner: AsyncRunner, accounts: AccountService, palette=DARK):
        super().__init__(palette)
        self.runner = runner
        self.accounts = accounts
        self._login_busy = False
        self._busy = False
        self._loaded = False
        self._menu_buttons: list[QToolButton] = []
        self._profiles: list[Profile | ProfileHealth] = []
        self._sort_mode = "name"

        add = QPushButton(tr(" Add profile"))
        add.setObjectName("Primary")
        set_button_icon(add, "accounts", palette.on_primary)
        add.clicked.connect(self._add)
        self.add_button = add
        self._root.addWidget(
            view_header(
                tr("Accounts"),
                tr(
                    "Add an account to open Codex sign-in automatically. Use each row's actions to manage saved accounts."
                ),
                add,
            )
        )
        summary = QHBoxLayout()
        summary.setSpacing(24)
        self.total_card, self.total_value = metric_card(tr("Total accounts"))
        self.ready_card, self.ready_value = metric_card(tr("Available accounts"))
        self.attention_card, self.attention_value = metric_card(tr("Accounts needing attention"))
        for card in (self.total_card, self.ready_card, self.attention_card):
            summary.addWidget(card, 0)
        summary.addStretch()
        self._root.addLayout(summary)
        panel, panel_layout = page_panel()
        self._root.addWidget(panel, 1)

        self.guide = EmptyState(
            tr("Bring your first account"),
            tr(
                "Start with a name you recognize. Sign in securely through Codex; your account is linked automatically."
            ),
            tr("Add a profile"),
            self._add,
        )
        self.guide.hide()
        panel_layout.addWidget(self.guide)
        self.filters_host = QWidget()
        filters = QHBoxLayout(self.filters_host)
        filters.setContentsMargins(0, 0, 0, 0)
        filters.setSpacing(12)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search accounts…"))
        self.search.textChanged.connect(self._filter)
        filters.addWidget(self.search, 1)
        self.status_filter = ComboBox()
        for text, value in (
            ("All accounts", "all"),
            ("Available accounts", "ready"),
            ("Accounts needing attention", "attention"),
        ):
            self.status_filter.addItem(tr(text), value)
        self.status_filter.currentIndexChanged.connect(self._filter)
        filters.addWidget(self.status_filter)
        panel_layout.addWidget(self.filters_host)
        self.filters_host.hide()
        self.table = AccountTable(palette)
        self.table.sort_requested.connect(self._sort_column)
        self.table.set_sort(self._sort_mode)
        panel_layout.addWidget(self.table, 1)
        self.operation_status = label(tr("Loading saved accounts…"), "Accent")
        self.operation_status.setWordWrap(True)
        panel_layout.addWidget(self.operation_status)

    def refresh(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.set_loading("accounts", True)
        if not self._loaded:
            self.operation_status.setText(tr("Loading saved accounts…"))
            self.operation_status.show()
        self.runner.submit(self.accounts.all_health(), self._render, self._load_failed)

    def _load_failed(self, error: Exception) -> None:
        self._busy = False
        self.set_loading("accounts", False)
        if isinstance(error, OperationBusyError):
            if not self._login_busy:
                self.operation_status.setText(
                    tr("Account operation in progress. Refresh will retry automatically.")
                )
                self.operation_status.show()
            QTimer.singleShot(2000, self, self.refresh)
            return
        self.operation_status.setText(tr("Could not load accounts: {error}", error=error))
        self.operation_status.show()

    def _render(self, profiles: list[Profile | ProfileHealth]) -> None:
        self._busy = False
        self.set_loading("accounts", False)
        self._loaded = True
        self._profiles = profiles
        if not self._login_busy:
            self.operation_status.clear()
        self.operation_status.setVisible(bool(self.operation_status.text()))
        self.guide.setVisible(not profiles)
        self.filters_host.setVisible(bool(profiles))
        self.table.setVisible(bool(profiles))
        self.total_value.setText(str(len(profiles)))
        ready = sum(
            SwitchPolicy._is_available(profile)
            for profile in profiles
            if isinstance(profile, ProfileHealth)
        )
        self.ready_value.setText(str(ready))
        self.attention_value.setText(str(len(profiles) - ready))
        self._filter()

    def _sort_column(self, modes: tuple[str, str]) -> None:
        self._sort_mode = modes[1] if self._sort_mode == modes[0] else modes[0]
        self._filter()

    def _filter(self, *_args) -> None:
        self.table.palette_ = self.palette_
        query = self.search.text().casefold().strip()
        mode = self.status_filter.currentData()
        profiles = [
            display_health(profile)
            for profile in self._profiles
            if (
                query in profile.alias.casefold()
                or (
                    isinstance(profile, ProfileHealth)
                    and profile.account_match is True
                    and query in (profile.email or "").casefold()
                )
            )
            and (
                mode == "all"
                or (
                    mode == "ready"
                    and isinstance(profile, ProfileHealth)
                    and SwitchPolicy._is_available(profile)
                )
                or (
                    mode == "attention"
                    and (
                        not isinstance(profile, ProfileHealth)
                        or not SwitchPolicy._is_available(profile)
                    )
                )
            )
        ]
        ordered = sorted(
            profiles,
            key=lambda profile: account_sort_key(profile, self._sort_mode, time.time()),
            reverse=self._sort_mode in {"name_desc", "status_desc"},
        )
        self.table.set_sort(self._sort_mode)
        if self._profiles and not ordered:
            self.table.show_state(
                EmptyState(tr("No matching profiles"), tr("Try another name or account filter."))
            )
            self._menu_buttons.clear()
            return
        self.table.set_profiles(
            ordered,
            email_visible=True,
            menu_factory=self._profile_menu,
        )
        self._menu_buttons = [row._switch_btn for row in self.table.rows.values()]
        self._set_actions_enabled(not self._login_busy)

    def _profile_menu(self, alias: str, button: QToolButton) -> QMenu:
        menu = QMenu(button)
        for title, handler in (
            ("Sign in", self._login),
            ("Rename", self._rename),
            ("Bind account", self._bind),
            ("Remove", self._remove),
        ):
            if title == "Remove":
                menu.addSeparator()
            action = menu.addAction(tr(title))
            action.triggered.connect(
                lambda _checked=False, account=alias, command=handler: command(account)
            )
        return menu

    def _set_actions_enabled(self, enabled: bool) -> None:
        self.add_button.setEnabled(enabled)
        for button in self._menu_buttons:
            button.setEnabled(enabled)

    def _add(self) -> None:
        if self._login_busy:
            return
        alias = prompt_text(
            self,
            tr("Add profile"),
            tr("Account name"),
            tr("Choose a name you will recognize, such as Personal or Work."),
            maximum=64,
            action="Add account",
        )
        if alias:
            self._login_busy = True
            self._set_actions_enabled(False)
            self.runner.submit(
                self.accounts.create_profile(alias), self._created, self._login_failed
            )

    def _created(self, profile) -> None:
        self._login_busy = False
        self._login(profile.alias)

    def _login(self, alias: str) -> None:
        if not self._login_busy:
            self._login_busy = True
            self._set_actions_enabled(False)
            self.operation_status.setText(
                tr("Waiting for Codex sign-in… Complete the sign-in flow in the window that opens.")
            )
            self.operation_status.show()
            self.runner.submit(
                self.accounts.login_profile(alias), self._login_done, self._login_failed
            )

    def _login_done(self, _account_id: str) -> None:
        self._login_busy = False
        self.operation_status.setText(
            tr("Sign-in complete. Your account is linked. Refresh usage in Overview.")
        )
        self.operation_status.show()
        self._set_actions_enabled(True)
        self.refresh()

    def _login_failed(self, error: Exception) -> None:
        self._login_busy = False
        self.operation_status.setText(
            tr("Sign-in could not be completed. Open the account menu and try again.")
        )
        self.operation_status.show()
        self._set_actions_enabled(True)
        QMessageBox.warning(self, tr("Operation failed"), tr(str(error)))
        self.refresh()

    def _bind(self, alias: str) -> None:
        if not self._login_busy:
            self.runner.submit(
                self.accounts.bind_current_account(alias),
                lambda _: self.refresh(),
                lambda e: QMessageBox.warning(self, tr("Bind failed"), str(e)),
            )

    def _rename(self, alias: str) -> None:
        if self._login_busy:
            return
        new = prompt_text(
            self,
            tr("Rename"),
            tr("Account name"),
            tr("This changes the name shown in this application."),
            initial=alias,
            maximum=64,
            action="Save",
        )
        if new and new != alias:
            self.runner.submit(self.accounts.rename_profile(alias, new), lambda _: self.refresh())

    def _remove(self, alias: str) -> None:
        if self._login_busy:
            return
        if (
            QMessageBox.question(
                self,
                tr("Remove profile"),
                tr(
                    "Remove '{alias}'? Shared Codex conversation history will be preserved.",
                    alias=alias,
                ),
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.runner.submit(self.accounts.remove_profile(alias), lambda _: self.refresh())
