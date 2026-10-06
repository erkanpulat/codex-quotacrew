"""A single account table used by Overview and My accounts."""

from __future__ import annotations

import math
from collections.abc import Callable

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from codex_account_manager.continuity.policy import SwitchPolicy
from codex_account_manager.domain.models import Profile, ProfileHealth
from codex_account_manager.domain.states import QuotaState
from codex_account_manager.gui.design import DARK, Palette, make_icon, set_button_icon
from codex_account_manager.gui.i18n import tr
from codex_account_manager.gui.widgets import AccountRow, ComboBox, CompactSwitch, label


def account_sort_key(health: ProfileHealth, mode: str, now: float) -> tuple:
    tie = (health.alias.casefold(), health.profile_id)
    if mode == "active":
        return (not health.is_active, *tie)
    if mode in {"name", "name_desc"}:
        return tie
    if mode in {"status", "status_desc"}:
        rank = (
            0
            if health.is_active
            else 1
            if SwitchPolicy._is_available(health)
            else 2
            if health.auth_present and health.account_match is True
            else 3
        )
        return (rank, *tie)
    verified = (
        health.auth_present
        and health.account_match is True
        and not health.stale
        and not health.error
        and not health.reauth_required
    )
    if mode in {"subscription_soonest", "subscription_latest"}:
        until = health.subscription_until
        expiry = until.timestamp() if until is not None else None
        known = verified and expiry is not None and expiry > now
        expiry_order = 0.0
        if known and expiry is not None:
            expiry_order = -expiry if mode == "subscription_latest" else expiry
        return (not known, expiry_order, *tie)
    value: float | None = None
    if mode in {"five_most", "five_least", "weekly_most", "weekly_least"}:
        duration = 300 if mode.startswith("five") else 10080
        windows = (
            (health.primary_window_minutes, health.primary_used_percent),
            (health.secondary_window_minutes, health.secondary_used_percent),
        )
        values = [used for minutes, used in windows if minutes == duration]
        if len(values) == 1 and values[0] is not None and math.isfinite(values[0]):
            value = min(100, max(0, values[0]))
            if mode.endswith("least"):
                value = -value
    elif mode == "renewal":
        times = [
            reset
            for reset in (health.primary_resets_at, health.secondary_resets_at)
            if reset is not None and math.isfinite(reset) and reset > now
        ]
        value = min(times) if times else None
    elif mode == "credits" and health.reset_credits is not None:
        value = -health.reset_credits.available_count
    return (not verified or value is None, value if verified and value is not None else 0, *tie)


def display_health(profile: Profile | ProfileHealth) -> ProfileHealth:
    if isinstance(profile, ProfileHealth):
        return profile
    return ProfileHealth(
        alias=profile.alias,
        profile_id=profile.id,
        plan_type=None,
        primary_used_percent=None,
        secondary_used_percent=None,
        primary_resets_at=None,
        secondary_resets_at=None,
        ordinary_usage_allowed=None,
        auth_present=bool(profile.bound_account_id),
        account_match=None,
        is_active=False,
        quota_state=QuotaState.UNKNOWN,
        last_checked_at=None,
    )


class AccountTable(QWidget):
    sort_requested = Signal(tuple)

    HEADINGS = (
        ("Account", ("name", "name_desc")),
        ("5-hour remaining", ("five_most", "five_least")),
        ("Weekly remaining", ("weekly_most", "weekly_least")),
        ("Status", ("status", "status_desc")),
    )

    def __init__(self, palette: Palette = DARK):
        super().__init__()
        self.setObjectName("AccountTable")
        self.palette_ = palette
        self.rows: dict[str, AccountRow] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        self.header = QWidget()
        self.header.setObjectName("AccountHeader")
        self._headers = QGridLayout(self.header)
        self._headers.setContentsMargins(8, 6, 8, 6)
        self._headers.setHorizontalSpacing(10)
        self.header_buttons: dict[tuple[str, str], tuple[QPushButton, str]] = {}
        self._header_cells: list[QWidget] = []
        for column, (title, modes) in enumerate(self.HEADINGS):
            button = QPushButton(tr(title))
            button.setObjectName("TableHeading")
            button.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
            button.clicked.connect(
                lambda _checked=False, pair=modes: self.sort_requested.emit(pair)
            )
            cell = QWidget()
            cell_layout = QHBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
            cell_layout.addStretch()
            self._headers.addWidget(cell, 0, column)
            self._header_cells.append(cell)
            self.header_buttons[modes] = (button, title)
        self.actions_heading = label(tr("Menu"), "TableColumn")
        self._headers.addWidget(self.actions_heading, 0, 4, Qt.AlignmentFlag.AlignLeft)
        self._header_cells.append(self.actions_heading)
        layout.addWidget(self.header)
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("AccountTableScroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.scroll_area.viewport().installEventFilter(self)
        self._host = QWidget()
        self._host.setObjectName("GridHost")
        self._grid = QGridLayout(self._host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(0)
        self._grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll_area.setWidget(self._host)
        layout.addWidget(self.scroll_area, 1)

    def set_sort(self, selected: str) -> None:
        for modes, (button, title) in self.header_buttons.items():
            glyph = (
                "sort_desc"
                if selected == modes[1]
                else "sort_asc"
                if selected == modes[0]
                else "sort"
            )
            color = self.palette_.primary if selected in modes else self.palette_.muted
            set_button_icon(button, glyph, color, 14)
            button.setText(tr(title))
        self._update_columns()

    def clear(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self.rows.clear()
        self._host.setMinimumHeight(0)

    def show_state(self, widget: QWidget) -> None:
        self.clear()
        self.header.hide()
        self._grid.addWidget(widget, 0, 0, 1, 2, Qt.AlignmentFlag.AlignTop)

    def set_profiles(
        self,
        profiles: list[ProfileHealth],
        *,
        email_visible: bool,
        candidate_id: str | None = None,
        confirmation: bool = False,
        on_switch: Callable[[str], None] | None = None,
        on_sign_in: Callable[[], None] | None = None,
        on_refresh: Callable[[], None] | None = None,
        on_manage: Callable[[], None] | None = None,
        menu_factory: Callable[[str, QToolButton], QMenu] | None = None,
    ) -> None:
        self.clear()
        self.header.setVisible(bool(profiles))
        for index, profile in enumerate(profiles):
            row = AccountRow(profile, self.palette_)
            if on_switch is not None:
                row.switch_requested.connect(on_switch)
            if on_sign_in is not None:
                row.sign_in_requested.connect(on_sign_in)
            if on_refresh is not None:
                row.refresh_requested.connect(on_refresh)
            if on_manage is not None:
                row.manage_requested.connect(on_manage)
            if menu_factory is not None:
                row._switch_btn.setMenu(menu_factory(profile.alias, row._switch_btn))
            row.set_email_visible(email_visible)
            row.set_candidate(candidate_id == profile.profile_id, confirmation)
            self.rows[profile.alias] = row
            self._grid.addWidget(row, index, 0, 1, 2, Qt.AlignmentFlag.AlignTop)
        self._host.setMinimumHeight(sum(row.minimumHeight() for row in self.rows.values()))
        self._grid.setColumnStretch(0, 1)
        self._grid.setColumnStretch(1, 1)
        self._update_columns()

    def eventFilter(self, watched, event) -> bool:
        if watched is self.scroll_area.viewport() and event.type() == QEvent.Type.Resize:
            self._update_columns()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_columns()

    def _update_columns(self) -> None:
        width = max(724, self.scroll_area.viewport().width())
        available = width - 56
        identity = max(210, round(available * 0.30))
        status, actions = 110, 84
        five_hour = (available - identity - status - actions) // 2
        weekly = available - identity - five_hour - status - actions
        widths = (identity, five_hour, weekly, status, actions)
        self.header.setFixedWidth(width)
        self.actions_heading.setFixedWidth(actions)
        for column, size in enumerate(widths):
            self._headers.setColumnMinimumWidth(column, size)
            self._header_cells[column].setFixedWidth(size)
        for modes, (button, title) in self.header_buttons.items():
            column = ("name", "five_most", "weekly_most", "status").index(modes[0])
            button.setFixedWidth(
                min(widths[column], button.fontMetrics().horizontalAdvance(tr(title)) + 30)
            )
        for row in self.rows.values():
            row.set_column_widths(widths)
        self._headers.activate()


class AccountControls(QWidget):
    """Shared heading, privacy, filtering and sorting for both account views."""

    changed = Signal()

    def __init__(self, palette=DARK):
        super().__init__()
        self.palette_ = palette
        self.summary = label("", "Muted")
        self.summary.setWordWrap(True)
        self.hero = QFrame()
        self.hero.setMinimumHeight(40)
        hero_box = QHBoxLayout(self.hero)
        self.heading_layout = hero_box
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
        self.search.textChanged.connect(lambda _text: self.changed.emit())
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
        self.account_filter.currentIndexChanged.connect(lambda _index: self.changed.emit())
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
            ("Subscription ending soonest", "subscription_soonest"),
            ("Subscription ending latest", "subscription_latest"),
            ("Most reset credits", "credits"),
            ("Status: available first", "status"),
            ("Status: attention first", "status_desc"),
        ):
            self.sort_order.addItem(tr(title), value)
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
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        layout.addWidget(self.hero)
        layout.addWidget(self.toolbar_host)

    def _privacy_changed(self, hidden: bool) -> None:
        self.email_toggle.setAccessibleName(
            tr("Show email addresses") if hidden else tr("Hide email addresses")
        )
        self.changed.emit()

    def _sort_changed(self, _index: int) -> None:
        self.sort_order.setToolTip(self.sort_order.currentText())
        self.sort_button.setToolTip(self.sort_order.currentText())
        for action in self.sort_button.menu().actions():
            action.setChecked(action.text() == self.sort_order.currentText())
        self.changed.emit()

    def set_summary(self, health: list[ProfileHealth]) -> None:
        ready = sum(SwitchPolicy._is_available(item) for item in health)
        self.summary.setText(
            tr(
                "{total} accounts · {ready} available · {attention} need attention",
                total=len(health),
                ready=ready,
                attention=len(health) - ready,
            )
        )
