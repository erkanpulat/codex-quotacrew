"""Goals screen."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QTableWidgetItem,
)

from codex_account_manager.gui.async_runner import AsyncRunner
from codex_account_manager.gui.design import DARK, set_button_icon
from codex_account_manager.gui.dialogs import choose_item, prompt_text
from codex_account_manager.gui.i18n import (
    state_label,
    tr,
)
from codex_account_manager.gui.view_base import (
    BaseView,
    SortableItem,
    page_panel,
    table_widget,
    tooltip_item,
    view_header,
)


class GoalsView(BaseView):
    def __init__(self, runner: AsyncRunner, palette=DARK):
        super().__init__(palette)
        self.runner = runner
        refresh = QPushButton(tr(" Refresh"))
        set_button_icon(refresh, "refresh", palette.text)
        refresh.clicked.connect(self.refresh)
        self._root.addWidget(
            view_header(tr("Goals"), tr("Saved objectives and their continuity status"), refresh)
        )
        panel, panel_layout = page_panel()
        self._root.addWidget(panel, 1)
        self.table = table_widget(
            [tr("Thread"), tr("Objective"), tr("Status"), tr("Native"), tr("Rev")]
        )
        self.table.fit_columns((120, 280, 120, 130, 80), (1, 5, 2, 2, 1))
        self.table.setSortingEnabled(True)
        panel_layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        for title, handler in (
            (tr("Save objective"), self._set_goal),
            (tr("Clear selected checkpoint"), self._clear_goal),
        ):
            button = QPushButton(title)
            button.clicked.connect(handler)
            actions.addWidget(button)
        actions.addStretch()
        panel_layout.addLayout(actions)

    def refresh(self) -> None:
        from codex_account_manager.storage.repositories import GoalRepository

        self.runner.submit(GoalRepository().list_active(), self._render)

    def _set_goal(self) -> None:
        from codex_account_manager.storage.repositories import ThreadRepository

        self.runner.submit(ThreadRepository().list(), self._choose_thread)

    def _choose_thread(self, threads) -> None:
        if not threads:
            QMessageBox.information(
                self,
                tr("Goals"),
                tr("Refresh Conversations first, then select a conversation to save a local note."),
            )
            return
        choices = [
            f"{thread.title or thread.preview or thread.id} [{thread.id[:8]}]" for thread in threads
        ]
        selected = choose_item(
            self,
            tr("Save local goal note"),
            tr("Conversation"),
            tr("Choose the conversation for this local note."),
            choices,
        )
        if selected is not None:
            self.save_for_thread(threads[selected].id)

    def save_for_thread(self, thread_id: str) -> None:
        from codex_account_manager.goals.service import GoalService

        objective = prompt_text(
            self,
            tr("Save local goal note"),
            tr("Objective"),
            tr("This note stays local. Saving it does not start work or change a Codex goal."),
            multiline=True,
        )
        if objective:
            self.runner.submit(
                GoalService().set_objective(thread_id, objective), lambda _: self.refresh()
            )

    def _clear_goal(self) -> None:
        from codex_account_manager.goals.service import GoalService

        item = self.table.item(self.table.currentRow(), 0)
        if item is None:
            return
        if (
            QMessageBox.question(
                self,
                tr("Clear checkpoint"),
                tr("Clear this local goal note? Native Codex goals are unchanged."),
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.runner.submit(
                GoalService().user_clear(item.data(Qt.ItemDataRole.UserRole)),
                lambda _: self.refresh(),
            )

    def _render(self, goals) -> None:
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(goals))
        for row, g in enumerate(goals):
            item = QTableWidgetItem(g.thread_id[:12])
            item.setData(Qt.ItemDataRole.UserRole, g.thread_id)
            item.setToolTip(g.thread_id)
            self.table.setItem(row, 0, item)
            self.table.setItem(row, 1, tooltip_item(g.objective))
            self.table.setItem(row, 2, QTableWidgetItem(state_label(g.local_status.value)))
            self.table.setItem(
                row,
                3,
                QTableWidgetItem(
                    tr("Unknown")
                    if g.native_goal_present is None
                    else tr("present")
                    if g.native_goal_present
                    else tr("missing")
                ),
            )
            self.table.setItem(row, 4, SortableItem(str(g.revision), g.revision))
        self.table.setSortingEnabled(True)
