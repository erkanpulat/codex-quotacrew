"""Saved accounts and sign-in controls."""

from __future__ import annotations

import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QMenu,
    QMessageBox,
    QPushButton,
    QToolButton,
)

from codex_account_manager.accounts.service import AccountService
from codex_account_manager.continuity.policy import SwitchPolicy
from codex_account_manager.core.errors import OperationBusyError
from codex_account_manager.domain.models import Profile, ProfileHealth
from codex_account_manager.gui.account_table import (
    AccountControls,
    AccountTable,
    account_sort_key,
    display_health,
)
from codex_account_manager.gui.async_runner import AsyncRunner
from codex_account_manager.gui.design import DARK, set_button_icon
from codex_account_manager.gui.dialogs import prompt_text
from codex_account_manager.gui.i18n import (
    tr,
)
from codex_account_manager.gui.view_base import (
    BaseView,
    EmptyState,
    page_panel,
    view_header,
)
from codex_account_manager.gui.widgets import label


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
        panel, panel_layout = page_panel()
        panel.setObjectName("AccountsPanel")
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
        self.controls = AccountControls(palette)
        self.filters_host = self.controls
        self.search = self.controls.search
        self.status_filter = self.controls.account_filter
        self.sort_order = self.controls.sort_order
        self.sort_order.setCurrentIndex(self.sort_order.findData(self._sort_mode))
        self.email_toggle = self.controls.email_toggle
        self.summary = self.controls.summary
        self.controls.changed.connect(self._sort_changed)
        panel_layout.addWidget(self.controls)
        self.controls.hide()
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
        self.controls.set_summary([display_health(profile) for profile in profiles])
        self._filter()

    def _sort_column(self, modes: tuple[str, str]) -> None:
        self._sort_mode = modes[1] if self._sort_mode == modes[0] else modes[0]
        self.sort_order.setCurrentIndex(self.sort_order.findData(self._sort_mode))

    def _sort_changed(self) -> None:
        self._sort_mode = self.sort_order.currentData()
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
                    and not self.email_toggle.isChecked()
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
                    mode == "credits"
                    and isinstance(profile, ProfileHealth)
                    and profile.reset_credits is not None
                    and profile.reset_credits.available_count > 0
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
            email_visible=not self.email_toggle.isChecked(),
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
                tr(
                    "Complete sign-in in your browser. You can copy the sign-in link from the terminal and open it in another browser. Keep the terminal open until QuotaCrew confirms the account was added."
                )
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
