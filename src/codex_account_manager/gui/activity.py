"""Read-only account handoff history."""

from __future__ import annotations

from datetime import datetime, timedelta

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QPushButton, QTableWidgetItem, QWidget

from codex_account_manager.continuity.service import ContinuityService
from codex_account_manager.gui.design import DARK, make_icon
from codex_account_manager.gui.i18n import state_label, tr
from codex_account_manager.gui.view_base import (
    BaseView,
    EmptyState,
    SortableItem,
    page_panel,
    table_widget,
    view_header,
)
from codex_account_manager.gui.widgets import ComboBox, label
from codex_account_manager.storage.repositories import ProfileRepository


class ActivityView(BaseView):
    def __init__(self, runner):
        super().__init__()
        self.runner = runner
        self._busy = False
        self._handoffs = []
        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.clicked.connect(self.refresh)
        self._root.addWidget(
            view_header(
                tr("Activity"),
                tr("Recent account switches and their results."),
                self.refresh_button,
            )
        )
        panel, panel_layout = page_panel()
        self._root.addWidget(panel, 1)
        self.filters_host = QWidget()
        filters = QHBoxLayout(self.filters_host)
        filters.setContentsMargins(0, 0, 0, 0)
        filters.setSpacing(12)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search account switches…"))
        self.search.textChanged.connect(self._filter)
        filters.addWidget(self.search, 1)
        self.result_filter = ComboBox()
        for title, value in (
            ("All results", "all"),
            ("Completed", "completed"),
            ("Failed", "failed"),
            ("In progress", "pending"),
        ):
            self.result_filter.addItem(tr(title), value)
        self.result_filter.currentIndexChanged.connect(self._filter)
        filters.addWidget(self.result_filter)
        self.date_filter = ComboBox()
        for title, days in (("All dates", 0), ("Last 7 days", 7), ("Last 30 days", 30)):
            self.date_filter.addItem(tr(title), days)
        self.date_filter.currentIndexChanged.connect(self._filter)
        filters.addWidget(self.date_filter)
        panel_layout.addWidget(self.filters_host)
        self.filters_host.hide()
        self.table = table_widget(
            [tr("When"), tr("Reason"), tr("Accounts"), tr("Result"), tr("Duration")]
        )
        self.table.fit_columns((150, 180, 200, 130, 100), (2, 3, 4, 2, 1))
        self.table.horizontalHeader().setSortIndicator(0, Qt.SortOrder.DescendingOrder)
        self.table.setSortingEnabled(True)
        panel_layout.addWidget(self.table, 1)
        self.empty = EmptyState(
            tr("No account switches yet."),
            tr("Manual and automatic switches will appear here."),
        )
        panel_layout.addWidget(self.empty)
        self.table.hide()
        self.status = label("", "Muted")
        panel_layout.addWidget(self.status)

    def refresh(self):
        if self._busy:
            return
        self._busy = True
        self.set_loading("activity", True)
        self.refresh_button.setEnabled(False)
        self.runner.submit(self._snapshot(), self._render, self._failed)

    @staticmethod
    async def _snapshot():
        handoffs = await ContinuityService().recent_handoffs(50)
        profiles = await ProfileRepository().list()
        return handoffs, {profile.id: profile.alias for profile in profiles}

    def _render(self, snapshot):
        self._busy = False
        self.set_loading("activity", False)
        self.refresh_button.setEnabled(True)
        handoffs, names = snapshot if isinstance(snapshot, tuple) else (snapshot, {})
        self._handoffs = list(handoffs)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(handoffs))
        for row, handoff in enumerate(handoffs):
            source = names.get(getattr(handoff, "from_profile_id", None))
            target = names.get(getattr(handoff, "to_profile_id", None))
            accounts = f"{source} → {target}" if source and target else target or source or "—"
            finished = getattr(handoff, "finished_at", None)
            duration = "—"
            if finished is not None:
                seconds = max(0, round((finished - handoff.started_at).total_seconds()))
                hours, remainder = divmod(seconds, 3600)
                minutes, seconds = divmod(remainder, 60)
                duration = (
                    f"{hours:02}:{minutes:02}:{seconds:02}"
                    if hours
                    else f"{minutes:02}:{seconds:02}"
                )
            for column, value in enumerate(
                (
                    handoff.started_at.astimezone().strftime("%d.%m.%Y %H:%M"),
                    state_label(handoff.reason.value),
                    accounts,
                    state_label("inProgress")
                    if handoff.success is None
                    else state_label("completed")
                    if handoff.success
                    else state_label("failed"),
                    duration,
                )
            ):
                item = (
                    SortableItem(value, handoff.started_at.timestamp())
                    if column == 0
                    else QTableWidgetItem(value)
                )
                if column == 1:
                    item.setIcon(make_icon("history", DARK.primary, 16))
                elif column == 3:
                    color = (
                        DARK.muted
                        if handoff.success is None
                        else (DARK.success if handoff.success else DARK.danger)
                    )
                    item.setForeground(QBrush(QColor(color)))
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)
        self._filter()

    def _filter(self, *_args):
        query = self.search.text().casefold().strip()
        result = self.result_filter.currentData()
        days = self.date_filter.currentData() or 0
        cutoff = datetime.now().astimezone() - timedelta(days=days) if days else None
        visible = 0
        for row in range(self.table.rowCount()):
            timestamp = self.table.item(row, 0)
            reason = self.table.item(row, 1)
            outcome = self.table.item(row, 3)
            account = self.table.item(row, 2)
            if not (timestamp and reason and account and outcome):
                continue
            matches_result = (
                result == "all"
                or (result == "completed" and outcome.text() == state_label("completed"))
                or (result == "failed" and outcome.text() == state_label("failed"))
                or (result == "pending" and outcome.text() == state_label("inProgress"))
            )
            recorded = datetime.fromtimestamp(
                timestamp.data(Qt.ItemDataRole.UserRole + 1)
            ).astimezone()
            show = (
                matches_result
                and (cutoff is None or recorded >= cutoff)
                and query in (reason.text() + " " + account.text()).casefold()
            )
            self.table.setRowHidden(row, not show)
            visible += show
        self.table.setVisible(bool(visible))
        self.filters_host.setVisible(bool(self._handoffs))
        self.empty.setVisible(not visible)
        self.empty.set_content(
            tr("No account switches yet.")
            if not self._handoffs
            else tr("No account switches match these filters."),
            tr("Manual and automatic switches will appear here.") if not self._handoffs else "",
        )
        self.status.setText(
            tr(
                "{shown} of {total} recent switches shown (maximum 50)",
                shown=visible,
                total=len(self._handoffs),
            )
            if self._handoffs
            else ""
        )

    def _failed(self, _error):
        self._busy = False
        self.set_loading("activity", False)
        self.refresh_button.setEnabled(True)
        self.status.setText(tr("Activity could not be loaded. Try again."))
