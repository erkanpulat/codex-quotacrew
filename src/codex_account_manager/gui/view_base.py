"""Shared view layout and accessible table building blocks."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from codex_account_manager.gui.design import DARK, Palette
from codex_account_manager.gui.i18n import tr


def view_header(title: str, subtitle: str, action: QWidget | None = None) -> QWidget:
    host = QWidget()
    row = QHBoxLayout(host)
    row.setContentsMargins(0, 0, 0, 0)
    text = QVBoxLayout()
    text.setSpacing(6)
    h1 = QLabel(title)
    h1.setObjectName("H1")
    h1.setWordWrap(True)
    sub = QLabel(subtitle)
    sub.setObjectName("Muted")
    sub.setWordWrap(True)
    text.addWidget(h1)
    text.addWidget(sub)
    row.addLayout(text, 1)
    if action is not None:
        row.addWidget(action, 0, Qt.AlignmentFlag.AlignVCenter)
    return host


def page_panel() -> tuple[QFrame, QVBoxLayout]:
    panel = QFrame()
    panel.setObjectName("PagePanel")
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(20, 18, 20, 18)
    layout.setSpacing(16)
    return panel, layout


def setting_row(form: QVBoxLayout, title: str, hint: str, control: QWidget) -> None:
    row = QFrame()
    row.setObjectName("SettingRow")
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 12, 0, 12)
    layout.setSpacing(20)
    text = QVBoxLayout()
    text.setSpacing(4)
    heading = QLabel(title)
    heading.setTextFormat(Qt.TextFormat.PlainText)
    heading.setObjectName("FieldTitle")
    heading.setWordWrap(True)
    heading.setBuddy(control)
    text.addWidget(heading)
    if hint:
        explanation = QLabel(hint)
        explanation.setTextFormat(Qt.TextFormat.PlainText)
        explanation.setObjectName("Caption")
        explanation.setWordWrap(True)
        text.addWidget(explanation)
    control.setAccessibleName(title)
    control.setFixedWidth(280)
    layout.addLayout(text, 1)
    layout.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
    form.addWidget(row)


class EmptyState(QWidget):
    def __init__(
        self,
        title: str,
        detail: str,
        action: str | None = None,
        on_action: Callable[[], None] | None = None,
    ):
        super().__init__()
        self.setMinimumHeight(220)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addStretch(1)
        self.title = QLabel(title)
        self.title.setObjectName("H2")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setWordWrap(True)
        layout.addWidget(self.title)
        self.detail = QLabel(detail)
        self.detail.setObjectName("Muted")
        self.detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)
        if action is not None:
            button = QPushButton(action)
            button.setObjectName("Primary")
            if on_action is not None:
                button.clicked.connect(on_action)
            layout.addWidget(button, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)

    def set_content(self, title: str, detail: str) -> None:
        self.title.setText(title)
        self.detail.setText(detail)


def metric_card(title: str) -> tuple[QFrame, QLabel]:
    card = QFrame()
    card.setObjectName("MetricCard")
    row = QHBoxLayout(card)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    value = QLabel("0")
    value.setObjectName("MetricValue")
    value.setAlignment(Qt.AlignmentFlag.AlignCenter)
    value.setMinimumWidth(18)
    row.addWidget(value)
    caption = QLabel(title)
    caption.setObjectName("MetricCaption")
    caption.setWordWrap(True)
    row.addWidget(caption, 1)
    return card, value


def tooltip_item(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setToolTip(text)
    return item


class SortableItem(QTableWidgetItem):
    def __init__(self, text: str, sort_value: int | float):
        super().__init__(text)
        self.setData(Qt.ItemDataRole.UserRole + 1, sort_value)

    def __lt__(self, other: QTableWidgetItem) -> bool:
        own = self.data(Qt.ItemDataRole.UserRole + 1)
        other_value = other.data(Qt.ItemDataRole.UserRole + 1)
        if own is not None and other_value is not None:
            return bool(own < other_value)
        return super().__lt__(other)


def table_widget(headers: list[str]) -> FittedTableWidget:
    table = FittedTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    table.horizontalHeader().setDefaultAlignment(
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    )
    table.horizontalHeader().setDefaultSectionSize(200)
    table.horizontalHeader().setMinimumSectionSize(80)
    table.horizontalHeader().setFixedHeight(44)
    table.horizontalHeader().setStretchLastSection(True)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setShowGrid(False)
    table.setAlternatingRowColors(False)
    table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    table.verticalHeader().setDefaultSectionSize(56)
    table.setWordWrap(False)
    table.setTextElideMode(Qt.TextElideMode.ElideRight)
    return table


def table_cell(*, vertical: bool = False) -> tuple[QWidget, QBoxLayout]:
    host = QWidget()
    layout = QVBoxLayout(host) if vertical else QHBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    return host, layout


class FittedTableWidget(QTableWidget):
    def __init__(self, rows: int, columns: int):
        super().__init__(rows, columns)
        self._base_widths: tuple[int, ...] = ()
        self._weights: tuple[float, ...] = ()
        self._fitting = False
        self._manually_resized = False
        self.horizontalHeader().sectionResized.connect(self._section_resized)
        self.empty = EmptyState(
            tr("No records yet"), tr("Refresh this page to load available records.")
        )
        self.empty.setParent(self.viewport())
        self.empty.setMinimumHeight(0)
        self.model().rowsInserted.connect(self._update_empty)
        self.model().rowsRemoved.connect(self._update_empty)
        self.model().modelReset.connect(self._update_empty)

    def _update_empty(self, *_args) -> None:
        self.empty.setVisible(self.rowCount() == 0)
        self.empty.setGeometry(self.viewport().rect())

    def fit_columns(self, widths: tuple[int, ...], weights: tuple[float, ...]) -> None:
        if len(widths) != self.columnCount() or len(weights) != len(widths):
            raise ValueError("Column widths and weights must match the table.")
        if (
            any(width <= 0 for width in widths)
            or any(weight < 0 for weight in weights)
            or sum(weights) <= 0
        ):
            raise ValueError(
                "Column widths must be positive; weights must be nonnegative with a positive total."
            )
        self._base_widths = widths
        self._weights = weights
        self.horizontalHeader().setMinimumSectionSize(min(80, min(widths)))
        self.horizontalHeader().setStretchLastSection(False)
        self._fit_columns()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_columns()
        self._update_empty()

    def _section_resized(self, _column: int, _old: int, _new: int) -> None:
        if (
            self._base_widths
            and not self._fitting
            and QApplication.mouseButtons() & Qt.MouseButton.LeftButton
        ):
            self._manually_resized = True

    def setColumnWidth(self, column: int, width: int) -> None:
        if self._base_widths and not self._fitting:
            self._manually_resized = True
        super().setColumnWidth(column, width)

    def _fit_columns(self) -> None:
        if not self._base_widths or self._manually_resized or self._fitting:
            return
        available = max(sum(self._base_widths), self.viewport().width())
        extra = available - sum(self._base_widths)
        weight_total = sum(self._weights)
        widths = [
            base + round(extra * weight / weight_total)
            for base, weight in zip(self._base_widths, self._weights, strict=True)
        ]
        flexible = self._weights.index(max(self._weights))
        widths[flexible] += available - sum(widths)
        self._fitting = True
        try:
            for column, width in enumerate(widths):
                self.setColumnWidth(column, width)
        finally:
            self._fitting = False


class BaseView(QWidget):
    def __init__(self, palette: Palette = DARK):
        super().__init__()
        self.palette_ = palette
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(0, 16, 0, 16)
        self._root.setSpacing(16)
        self._loading_tasks: set[str] = set()
        self.loading = QWidget()
        loading_policy = self.loading.sizePolicy()
        loading_policy.setRetainSizeWhenHidden(True)
        self.loading.setSizePolicy(loading_policy)
        self.loading.setFixedHeight(4)
        loading_row = QHBoxLayout(self.loading)
        loading_row.setContentsMargins(0, 0, 0, 0)
        loading_row.setSpacing(12)
        progress = QProgressBar()
        progress.setObjectName("LoadingProgress")
        progress.setRange(0, 0)
        progress.setTextVisible(False)
        progress.setFixedHeight(4)
        progress.setAccessibleName(tr("Loading…"))
        loading_row.addWidget(progress, 1)
        self._root.addWidget(self.loading)
        self.loading.hide()

    def set_loading(self, task: str, active: bool) -> None:
        if active:
            self._loading_tasks.add(task)
        else:
            self._loading_tasks.discard(task)
        self.loading.setVisible(bool(self._loading_tasks))

    def refresh(self) -> None: ...
