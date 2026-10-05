"""Reusable presentation widgets for account health and capacity."""

from __future__ import annotations

import math
from datetime import datetime

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from codex_account_manager.domain.models import ProfileHealth
from codex_account_manager.domain.states import QuotaState
from codex_account_manager.gui.design import DARK, Palette, make_icon
from codex_account_manager.gui.i18n import language, tr


class ElidedLabel(QLabel):
    def __init__(self, text: str):
        super().__init__(text)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setToolTip(text)
        self.setAccessibleName(text)

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setToolTip(text)
        self.setAccessibleName(text)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setPen(self.palette().windowText().color())
        painter.setFont(self.font())
        text = self.fontMetrics().elidedText(
            self.text(), Qt.TextElideMode.ElideRight, self.contentsRect().width()
        )
        painter.drawText(
            self.contentsRect(), self.alignment() | Qt.AlignmentFlag.AlignVCenter, text
        )


def label(text: str = "", role: str = "", *, elide: bool = False) -> QLabel:
    widget = ElidedLabel(text) if elide else QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if role:
        widget.setObjectName(role)
    return widget


def account_status(health: ProfileHealth) -> tuple[str, str]:
    if health.reauth_required or not health.auth_present:
        return tr("SIGN IN"), "warning"
    if health.error or health.stale:
        return (tr("STALE") if health.stale else tr("UNVERIFIED")), "warning"
    if health.account_match is not True:
        return (tr("UNBOUND") if health.account_match is None else tr("MISMATCH")), "warning"
    if health.quota_state in {QuotaState.LIMITED_NO_RESET, QuotaState.LIMITED_WITH_RESET}:
        return tr("LIMITED"), "warning"
    if health.is_active:
        return tr("ACTIVE"), "success"
    if health.quota_state == QuotaState.AVAILABLE:
        return tr("READY"), "primary"
    return tr("UNKNOWN"), "muted"


class ToggleSwitch(QCheckBox):
    """Keyboard-accessible switch with distinct knob positions and state text."""

    def __init__(self, name: str):
        super().__init__()
        self.setAccessibleName(name)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumSize(110, 32)

    def sizeHint(self):
        return QSize(120, 32)

    def hitButton(self, point):
        return self.rect().contains(point)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        painter.setOpacity(1.0 if self.isEnabled() else 0.45)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(palette.highlight() if self.isChecked() else palette.mid())
        y = (self.height() - 24) / 2
        painter.drawRoundedRect(QRectF(2, y, 44, 24), 12, 12)
        painter.setBrush(Qt.GlobalColor.white)
        painter.drawEllipse(QRectF(24 if self.isChecked() else 6, y + 4, 16, 16))
        painter.setPen(palette.text().color())
        painter.drawText(
            QRectF(58, 0, self.width() - 58, self.height()),
            Qt.AlignmentFlag.AlignVCenter,
            tr("On") if self.isChecked() else tr("Off"),
        )
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(palette.highlight().color(), 1, Qt.PenStyle.DashLine))
            painter.drawRoundedRect(QRectF(0, y - 2, 48, 28), 14, 14)


class CompactSwitch(QCheckBox):
    def __init__(self, name: str):
        super().__init__()
        self.setAccessibleName(name)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(48, 28)

    def hitButton(self, point):
        return self.rect().contains(point)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(1.0 if self.isEnabled() else 0.45)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.palette().highlight() if self.isChecked() else self.palette().mid())
        painter.drawRoundedRect(QRectF(0, 3, 48, 22), 11, 11)
        painter.setBrush(Qt.GlobalColor.white)
        painter.drawEllipse(QRectF(26 if self.isChecked() else 4, 6, 16, 16))
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(self.palette().highlight().color(), 1, Qt.PenStyle.DashLine))
            painter.drawRoundedRect(QRectF(1, 1, 46, 26), 12, 12)


class ComboBox(QComboBox):
    """Keep the dropdown affordance visible with both application palettes."""

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(self.palette().buttonText().color(), 1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        x, y = self.width() - 15, self.height() / 2
        painter.drawPolyline([QPointF(x - 4, y - 2), QPointF(x, y + 2), QPointF(x + 4, y - 2)])
        painter.end()


def format_reset(value: int | None) -> str:
    if value is None:
        return tr("Unknown")
    try:
        return datetime.fromtimestamp(value).astimezone().strftime("%d.%m.%Y %H:%M")
    except (OSError, OverflowError, ValueError):
        return tr("Unknown")


class Pill(QLabel):
    def __init__(self, text: str = "", tone: str = "muted"):
        super().__init__(text)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setObjectName("Pill")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setProperty("tone", tone)


class UsageBar(QWidget):
    def __init__(self, title: str, palette: Palette = DARK):
        super().__init__()
        self._palette = palette
        self._used: float | None = None
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.setAccessibleName(title)
        self._value = label(tr("Unknown"), "FieldTitle")
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(13)
        self._bar.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        layout.addWidget(self._bar, 1)
        layout.addWidget(self._value)

    def set_remaining(self, used: float | None) -> None:
        if used is not None and not math.isfinite(used):
            used = None
        self._used = used
        remaining = 100 - max(0, min(100, round(used))) if used is not None else 0
        self._bar.setValue(remaining)
        self._value.setText(tr("{value}%", value=remaining) if used is not None else "—")
        color = self._palette.primary if used is not None else self._palette.muted
        self._bar.setStyleSheet(
            f"QProgressBar {{ background: {self._palette.track}; border: none; border-radius: 5px; }} "
            f"QProgressBar::chunk {{ background: {color}; border-radius: 5px; }}"
        )

    def set_palette(self, palette: Palette) -> None:
        self._palette = palette
        self.set_remaining(self._used)


class AccountRow(QFrame):
    switch_requested = Signal(str)
    sign_in_requested = Signal()
    refresh_requested = Signal()
    manage_requested = Signal()

    def __init__(self, health: ProfileHealth, palette: Palette = DARK):
        super().__init__()
        self.setObjectName("AccountRow")
        self.setProperty("active", "true" if health.is_active else "false")
        self.alias = health.alias
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(88)

        root = QGridLayout(self)
        self._grid_layout = root
        root.setContentsMargins(8, 6, 8, 6)
        root.setHorizontalSpacing(10)
        root.setVerticalSpacing(4)

        self._identity = QWidget()
        self._identity.setFixedWidth(260)
        self._identity_row = QHBoxLayout(self._identity)
        self._identity_row.setContentsMargins(0, 0, 0, 0)
        self._identity_row.setSpacing(10)
        self._avatar = QLabel(health.alias[:1].upper())
        self._avatar.setObjectName("AccountAvatar")
        self._avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._avatar.setFixedSize(44, 44)
        self._avatar.setProperty(
            "variant", "0" if health.is_active else str(1 + sum(health.alias.encode("utf-8")) % 2)
        )
        self._identity_row.addWidget(self._avatar)
        identity = QVBoxLayout()
        identity.setSpacing(2)
        identity.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        name_row = QHBoxLayout()
        name_row.setSpacing(12)
        self._name = label(health.alias, "FieldTitle", elide=True)
        self._name.setToolTip(health.alias)
        self._name.setAccessibleName(health.alias)
        self._name.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        name_row.addWidget(self._name)
        plan_text = (health.plan_type or tr("Unknown plan")).capitalize()
        plan_tip = plan_text
        until = health.subscription_until
        if (
            until is not None
            and until.tzinfo is not None
            and not health.stale
            and health.account_match is True
        ):
            local = until.astimezone()
            now = datetime.now().astimezone()
            if local > now:
                pattern = "%d.%m" if language() == "tr" else "%m/%d"
                if local.year != now.year:
                    pattern += ".%y" if language() == "tr" else "/%y"
                plan_text += " · " + local.strftime(pattern)
                full_pattern = "%d.%m.%Y" if language() == "tr" else "%m/%d/%Y"
                plan_tip = tr(
                    "Subscription period recorded at sign-in: {date}. Renewal or cancellation is not confirmed.",
                    date=local.strftime(full_pattern),
                )
                if health.subscription_checked_at is not None:
                    plan_tip += "\n" + tr(
                        "Last checked: {date}",
                        date=health.subscription_checked_at.astimezone().strftime(full_pattern),
                    )
        self._plan = label(plan_text, "PlanBadge")
        self._plan.setFixedWidth(
            min(160, max(44, self._plan.fontMetrics().horizontalAdvance(plan_text) + 16))
        )
        self._plan.setToolTip(plan_tip)
        name_row.addWidget(self._plan)
        name_row.addStretch()
        identity.addLayout(name_row)
        self._email = health.email if health.account_match is True else None
        if self._email:
            self._email_button = QPushButton(tr("Show email address"))
            self._email_button.setObjectName("InlineAction")
            self._email_button.setCheckable(True)
            self._email_button.setMinimumWidth(0)
            self._email_button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            self._email_button.setToolTip(tr("Email stays hidden until you choose to show it."))
            self._email_button.toggled.connect(
                lambda visible: self._show_email(health.email or "", visible)
            )
            identity.addWidget(self._email_button)
        self._candidate = Pill("", "primary")
        self._candidate.hide()
        identity.addWidget(self._candidate, 0, Qt.AlignmentFlag.AlignLeft)
        credits = health.reset_credits
        if credits is not None:
            count = credits.available_count
            credit_button = QPushButton(
                tr("{count} reset credit", count=count)
                if count == 1
                else tr("{count} reset credits", count=count)
            )
            credit_button.setObjectName("CreditLink")
            credit_button.clicked.connect(lambda: self._show_reset_credits(health))
            identity.addWidget(credit_button, 0, Qt.AlignmentFlag.AlignLeft)
        self._identity_row.addLayout(identity, 1)
        root.addWidget(self._identity, 0, 0, Qt.AlignmentFlag.AlignVCenter)

        state, tone = account_status(health)
        from codex_account_manager.continuity.policy import automatic_exclusion_reason

        exclusion = automatic_exclusion_reason(health)
        if exclusion and not health.error and not health.stale and health.account_match is True:
            candidate_hint = label(tr(exclusion), "Caption")
            candidate_hint.setWordWrap(True)
            root.addWidget(candidate_hint, 2, 0, 1, 5)

        self._primary_usage = UsageBar(tr("5-hour remaining"), palette)
        self._secondary_usage = UsageBar(tr("Weekly remaining"), palette)
        windows = {
            health.primary_window_minutes: (health.primary_used_percent, health.primary_resets_at),
            health.secondary_window_minutes: (
                health.secondary_used_percent,
                health.secondary_resets_at,
            ),
        }
        verified = (
            not health.error
            and not health.stale
            and not health.reauth_required
            and health.auth_present
            and health.account_match is True
        )
        self._usage_cells: list[QWidget] = []
        for column, usage, (percent, reset) in (
            (1, self._primary_usage, windows.get(300, (None, None))),
            (2, self._secondary_usage, windows.get(10080, (None, None))),
        ):
            usage.set_remaining(percent if verified else None)
            cell = QWidget()
            cell.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setSpacing(5)
            cell_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
            cell_layout.addWidget(usage)
            renewal = label(format_reset(reset), "Caption", elide=True)
            renewal.setToolTip(tr("Renews {time}", time=format_reset(reset)))
            renewal.setAccessibleName(renewal.toolTip())
            cell_layout.addWidget(renewal)
            root.setColumnMinimumWidth(column, 130)
            root.addWidget(cell, 0, column, Qt.AlignmentFlag.AlignVCenter)
            self._usage_cells.append(cell)

        self._status_host = QWidget()
        status_layout = QHBoxLayout(self._status_host)
        status_layout.setContentsMargins(0, 0, 0, 0)
        pill = Pill(state, tone)
        pill.setWordWrap(True)
        status_layout.addWidget(pill, 0, Qt.AlignmentFlag.AlignVCenter)
        status_layout.addStretch()
        root.addWidget(self._status_host, 0, 3, Qt.AlignmentFlag.AlignVCenter)
        self._action_host = QWidget()
        actions = QHBoxLayout(self._action_host)
        actions.setContentsMargins(0, 0, 0, 0)
        self._switch_btn = QToolButton()
        self._switch_btn.setObjectName("RowMenu")
        self._switch_btn.setIcon(make_icon("more", palette.muted, 24))
        self._switch_btn.setIconSize(QSize(24, 24))
        self._switch_btn.setAccessibleName(tr("Actions for {account}", account=self.alias))
        self._switch_btn.setToolTip(tr("Account actions"))
        self._switch_btn.setFixedSize(32, 38)
        self._switch_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self._switch_btn)
        if health.reauth_required or not health.auth_present:
            menu.addAction(tr("Sign in"), self.sign_in_requested.emit)
        elif not health.is_active:
            switch = menu.addAction(tr("Switch account"))
            switch.setEnabled(verified)
            switch.triggered.connect(lambda: self.switch_requested.emit(self.alias))
        else:
            menu.addAction(tr("Active account")).setEnabled(False)
        menu.addSeparator()
        menu.addAction(tr("Refresh usage"), self.refresh_requested.emit)
        menu.addAction(tr("Manage profiles"), self.manage_requested.emit)
        self._switch_btn.setMenu(menu)
        actions.addWidget(self._switch_btn)
        actions.addStretch()
        root.addWidget(self._action_host, 0, 4, Qt.AlignmentFlag.AlignVCenter)

        if health.error:
            explanation = (
                tr("Sign in again from My accounts. Your saved profile has been kept.")
                if health.reauth_required
                else tr("Could not refresh. This does not mean your sign-in is invalid.")
            )
            if health.stale and health.last_checked_at:
                explanation += " " + tr(
                    "Last verified: {time}",
                    time=health.last_checked_at.astimezone().strftime("%d.%m %H:%M"),
                )
            notice = label(explanation, "Caption")
            notice.setWordWrap(True)
            root.addWidget(notice, 1, 0, 1, 5)
            self.setToolTip(health.error)

    def set_column_widths(self, widths: tuple[int, int, int, int, int]) -> None:
        identity, five_hour, weekly, status, actions = widths
        self._identity.setFixedWidth(identity)
        for cell, width in zip(self._usage_cells, (five_hour, weekly), strict=True):
            cell.setFixedWidth(width)
        self._status_host.setFixedWidth(status)
        self._action_host.setFixedWidth(actions)
        self.setFixedWidth(sum(widths) + 56)
        for column, width in enumerate(widths):
            self._grid_layout.setColumnMinimumWidth(column, width)
        self._avatar.setFixedSize(44, 44)
        self._identity_row.setSpacing(10)
        available = (
            identity - self._avatar.width() - self._identity_row.spacing() - self._plan.width() - 12
        )
        self._name.ensurePolished()
        desired = self._name.fontMetrics().horizontalAdvance(self.alias) + 4
        self._name.setFixedWidth(max(0, min(desired, available)))
        self._grid_layout.activate()

    def set_email_visible(self, visible: bool) -> None:
        if self._email and hasattr(self, "_email_button"):
            self._email_button.setChecked(visible)

    def set_candidate(self, visible: bool, confirmation: bool = False) -> None:
        self._candidate.setText(tr("Suggested account") if confirmation else tr("Next candidate"))
        self._candidate.setToolTip(
            tr("Based on the latest check. Availability is verified again before switching.")
        )
        self._candidate.setVisible(visible)
        self.setMinimumHeight(104 if visible else 88)

    def _show_email(self, email: str, visible: bool) -> None:
        self._email_button.setText(email if visible else tr("Show email address"))
        self._email_button.setToolTip(
            email if visible else tr("Email stays hidden until you choose to show it.")
        )
        self._email_button.setAccessibleName(
            tr("Hide email address") if visible else tr("Show email address")
        )

    def _show_reset_credits(self, health: ProfileHealth) -> None:
        self.show_reset_credits(self, health)

    @staticmethod
    def show_reset_credits(parent: QWidget, health: ProfileHealth) -> None:
        credits = health.reset_credits
        if credits is None:
            return

        dialog = QDialog(parent)
        dialog.setWindowTitle(tr("Usage reset credits"))
        dialog.resize(520, 440)
        dialog.setMinimumSize(400, 300)
        outer = QVBoxLayout(dialog)
        outer.setContentsMargins(32, 32, 32, 28)
        outer.setSpacing(16)

        header_widget = QWidget()
        header_layout = QVBoxLayout(header_widget)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(3)
        title_lbl = QLabel(health.alias)
        title_lbl.setObjectName("H2")
        header_layout.addWidget(title_lbl)
        count_text = (
            tr("{count} reset credits available", count=credits.available_count)
            if credits.available_count != 1
            else tr("1 reset credit available")
        )
        count_lbl = QLabel(count_text)
        count_lbl.setObjectName("Muted")
        header_layout.addWidget(count_lbl)
        checked_text = tr(
            "Last checked: {time}",
            time=health.last_checked_at.astimezone().strftime("%d.%m.%Y %H:%M")
            if health.last_checked_at
            else tr("Unknown"),
        )
        checked_lbl = QLabel(checked_text)
        checked_lbl.setObjectName("Caption")
        header_layout.addWidget(checked_lbl)
        outer.addWidget(header_widget)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        cards_host = QWidget()
        cards_layout = QVBoxLayout(cards_host)
        cards_layout.setContentsMargins(0, 0, 0, 0)
        cards_layout.setSpacing(8)

        if credits.credits is None:
            no_detail = QLabel(tr("Credit details are unavailable."))
            no_detail.setObjectName("Muted")
            cards_layout.addWidget(no_detail)
        elif not credits.credits:
            no_detail = QLabel(tr("No credit details returned."))
            no_detail.setObjectName("Muted")
            cards_layout.addWidget(no_detail)
        else:
            for credit in credits.credits:
                raw_state = credit.status
                state_label_text = {
                    "available": tr("Available"),
                    "redeeming": tr("Being redeemed"),
                    "redeemed": tr("Redeemed"),
                }.get(raw_state, tr("Unknown"))
                tone = {
                    "available": "available",
                    "redeeming": "redeeming",
                    "redeemed": "redeemed",
                }.get(raw_state, "redeemed")
                if (
                    raw_state == "available"
                    and credit.expires_at is not None
                    and credit.expires_at <= datetime.now().timestamp()
                ):
                    state_label_text = tr("Expired; refresh usage")
                    tone = "expired"

                card = QFrame()
                card.setObjectName("CreditCard")
                card_layout = QVBoxLayout(card)
                card_layout.setContentsMargins(14, 12, 14, 12)
                card_layout.setSpacing(6)

                top_row = QWidget()
                top_layout = QHBoxLayout(top_row)
                top_layout.setContentsMargins(0, 0, 0, 0)
                credit_title = QLabel(credit.title or tr("Usage reset credit"))
                credit_title.setObjectName("FieldTitle")
                top_layout.addWidget(credit_title, 1)
                status_lbl = QLabel(state_label_text)
                status_lbl.setObjectName("CreditStatus")
                status_lbl.setProperty("tone", tone)
                top_layout.addWidget(status_lbl)
                card_layout.addWidget(top_row)

                scope_text = (
                    tr("Codex usage limits")
                    if credit.reset_type == "codexRateLimits"
                    else tr("Unknown scope")
                )
                for row_text in (
                    tr("Scope: {value}", value=scope_text),
                    tr("Granted: {time}", time=format_reset(credit.granted_at)),
                    tr(
                        "Expires: {time}",
                        time=format_reset(credit.expires_at)
                        if credit.expires_at is not None
                        else tr("No expiry"),
                    ),
                ):
                    row_lbl = QLabel(row_text)
                    row_lbl.setObjectName("Caption")
                    card_layout.addWidget(row_lbl)

                if credit.description:
                    desc_lbl = QLabel(credit.description)
                    desc_lbl.setObjectName("Muted")
                    desc_lbl.setWordWrap(True)
                    card_layout.addWidget(desc_lbl)

                cards_layout.addWidget(card)

            if len(credits.credits) < credits.available_count:
                note = QLabel(tr("The server returned details for only some credits."))
                note.setObjectName("Caption")
                cards_layout.addWidget(note)

        cards_layout.addStretch()
        scroll.setWidget(cards_host)
        outer.addWidget(scroll, 1)

        footer = QLabel(
            tr(
                "To redeem a credit, open the matching account in Codex and review its usage settings. Times use your local timezone."
            )
        )
        footer.setObjectName("Caption")
        footer.setWordWrap(True)
        outer.addWidget(footer)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(tr("Close"))
        buttons.rejected.connect(dialog.reject)
        outer.addSpacing(16)
        outer.addWidget(buttons, 0, Qt.AlignmentFlag.AlignRight)
        dialog.exec()
