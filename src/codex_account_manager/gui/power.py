"""Visible, session-only shutdown controls. No shutdown runs in widget tests."""

from __future__ import annotations

import sys
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from codex_account_manager.gui.design import DARK, make_icon
from codex_account_manager.gui.i18n import tr
from codex_account_manager.gui.view_base import BaseView, view_header
from codex_account_manager.gui.widgets import label
from codex_account_manager.monitoring.power import (
    CONDITION_COUNTDOWN_SECONDS,
    MAX_COUNTDOWN_MINUTES,
    PowerChecks,
    PowerEvidence,
    PowerTarget,
    ShutdownPlan,
)


class PowerControls(QFrame):
    status_changed = Signal(str, bool)
    countdown_started = Signal()

    def __init__(self, runner, accounts, parent=None):
        super().__init__(parent)
        self.setObjectName("PowerControls")
        self.runner = runner
        self.checks = PowerChecks(accounts)
        self.plan = ShutdownPlan()
        self._busy = False
        self._next_check = 0.0
        self._preparing = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)
        self.mode = QComboBox(self)
        modes = [
            ("When my selected work finishes", "work"),
            ("When all accounts reach their limits", "limits"),
            ("After a set time", "timer"),
        ]
        for title, mode in modes:
            self.mode.addItem(tr(title), mode)
        self.mode.setCurrentIndex(1)
        # Keep a single authoritative mode value; cards only select it.
        self.mode.hide()
        choices = QHBoxLayout()
        choices.setSpacing(14)
        self.mode_buttons = QButtonGroup(self)
        self.mode_marks = []
        for index, (title, hint, icon) in enumerate(
            [
                ("When work finishes", "Your selected conversation completes", "document"),
                (
                    "When all limits are reached",
                    "All 5-hour quotas are exhausted; no account remains to switch to",
                    "quota",
                ),
                ("When time runs out", "After your chosen duration", "history"),
            ]
        ):
            button = QPushButton()
            button.setObjectName("PowerChoice")
            button.setCheckable(True)
            button.setAccessibleName(tr(title))
            button.setAccessibleDescription(tr(hint))
            button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            card = QHBoxLayout(button)
            card.setContentsMargins(18, 20, 18, 20)
            card.setSpacing(16)
            mark = label("○", "PowerChoiceMark")
            mark.setFixedSize(26, 30)
            mark.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self.mode_marks.append(mark)
            card.addWidget(mark, 0, Qt.AlignmentFlag.AlignTop)
            content = QVBoxLayout()
            content.setSpacing(12)
            glyph = label()
            glyph.setPixmap(make_icon(icon, DARK.primary, 48).pixmap(48, 48))
            glyph.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            content.addWidget(glyph)
            for text, role in ((title, "FieldTitle"), (hint, "Caption")):
                caption = label(tr(text), role)
                caption.setWordWrap(True)
                caption.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                content.addWidget(caption)
            content.addStretch()
            card.addLayout(content, 1)
            button.setMinimumHeight(200)
            self.mode_buttons.addButton(button, index)
            choices.addWidget(button, 1)
        self.mode_buttons.idClicked.connect(self.mode.setCurrentIndex)
        layout.addLayout(choices)

        body = QHBoxLayout()
        body.setSpacing(18)
        details = QFrame()
        details.setObjectName("SetupCard")
        detail_layout = QVBoxLayout(details)
        detail_layout.setContentsMargins(20, 18, 20, 18)
        detail_layout.setSpacing(12)
        detail_layout.addWidget(label(tr("Condition settings"), "H2"))
        self.explanation = label("", "Muted")
        self.explanation.setWordWrap(True)
        detail_layout.addWidget(self.explanation)
        self.work_fields = QWidget()
        work_layout = QVBoxLayout(self.work_fields)
        work_layout.setContentsMargins(0, 0, 0, 0)
        self.conversation = QComboBox()
        self.conversation.setMinimumWidth(0)
        self.conversation.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.conversation.setAccessibleName(tr("Conversation"))
        self.refresh_button = QPushButton(tr("Refresh conversations"))
        self.refresh_button.clicked.connect(self.refresh_conversations)
        work_layout.addWidget(label(tr("Conversation"), "FieldTitle"))
        work_layout.addWidget(self.conversation)
        work_layout.addWidget(self.refresh_button)
        detail_layout.addWidget(self.work_fields)
        self.time_fields = QWidget()
        time_layout = QVBoxLayout(self.time_fields)
        time_layout.setContentsMargins(0, 0, 0, 0)
        self.countdown_minutes = QSpinBox()
        self.countdown_minutes.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.countdown_minutes.setRange(1, MAX_COUNTDOWN_MINUTES)
        self.countdown_minutes.setValue(120)
        self.countdown_minutes.setSuffix(tr(" minutes"))
        self.countdown_minutes.setAccessibleName(tr("Shut down after (minutes)"))
        time_layout.addWidget(label(tr("Shutdown duration"), "FieldTitle"))
        time_layout.addWidget(self.countdown_minutes)
        presets = QHBoxLayout()
        self.presets = {}
        for title, minutes in [("30 min", 30), ("1 hour", 60), ("2 hours", 120)]:
            button = QPushButton(tr(title))
            button.setObjectName("PowerPreset")
            button.setCheckable(True)
            button.clicked.connect(
                lambda _checked=False, minutes=minutes: self._choose_duration(minutes)
            )
            presets.addWidget(button)
            self.presets[minutes] = button
        time_layout.addLayout(presets)
        self.duration_hint = label("", "Caption")
        self.duration_hint.setWordWrap(True)
        time_layout.addWidget(self.duration_hint)
        detail_layout.addWidget(self.time_fields)
        detail_layout.addStretch()
        body.addWidget(details, 3)

        summary = QFrame()
        summary.setObjectName("SetupCard")
        summary_layout = QVBoxLayout(summary)
        summary_layout.setContentsMargins(20, 18, 20, 18)
        summary_layout.setSpacing(10)
        summary_layout.addWidget(label(tr("Plan summary"), "H2"))
        self.summary_values = {}
        for key in ("Condition", "Target", "Countdown", "Status"):
            row = QHBoxLayout()
            heading = label(tr(key), "Caption")
            value = label("", "FieldTitle")
            value.setWordWrap(True)
            row.addWidget(heading, 1)
            row.addWidget(value, 2)
            summary_layout.addLayout(row)
            self.summary_values[key] = value
        self.clock = label("", "PowerClock")
        self.clock.setAccessibleName(tr("Time remaining"))
        summary_layout.addWidget(self.clock)
        summary_layout.addStretch()
        summary_note = label(
            tr(
                "Time starts only after confirmation. Conditional plans wait for a fresh, verified check."
            ),
            "Caption",
        )
        summary_note.setWordWrap(True)
        summary_layout.addWidget(summary_note)
        body.addWidget(summary, 2)
        layout.addLayout(body)

        actions = QFrame()
        actions.setObjectName("SetupCard")
        action_layout = QHBoxLayout(actions)
        action_layout.setContentsMargins(20, 18, 20, 18)
        status_layout = QVBoxLayout()
        self.status_title = label(tr("Shutdown plan is off"), "H2")
        self.status_title.setWordWrap(True)
        status_layout.addWidget(self.status_title)
        self.status = label(tr("Shutdown is off."), "Caption")
        self.status.setWordWrap(True)
        status_layout.addWidget(self.status)
        action_layout.addLayout(status_layout, 1)
        self.schedule_button = QPushButton(tr("Confirm plan"))
        self.schedule_button.setObjectName("Primary")
        self.schedule_button.clicked.connect(self._schedule)
        self.cancel_button = QPushButton(tr("Cancel shutdown"))
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.setEnabled(False)
        action_layout.addWidget(self.schedule_button)
        action_layout.addWidget(self.cancel_button)
        layout.addWidget(actions)
        note = label(
            tr(
                "You can cancel from this page or the tray. Open applications are not forcibly closed."
            ),
            "Caption",
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        lifetime = label(
            tr(
                "The plan is for this session only. Keep QuotaCrew running; restarting it does not reactivate shutdown."
            ),
            "Caption",
        )
        lifetime.setWordWrap(True)
        layout.addWidget(lifetime)
        self._loading = False
        self._warned = False
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.countdown_minutes.valueChanged.connect(self._update_summary)
        self.conversation.currentIndexChanged.connect(self._update_summary)
        self._mode_changed()
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)

    def _mode_changed(self) -> None:
        mode = self.mode.currentData()
        self.mode_buttons.button(self.mode.currentIndex()).setChecked(True)
        for index, mark in enumerate(self.mode_marks):
            mark.setText("●" if index == self.mode.currentIndex() else "○")
        self.work_fields.setVisible(mode == "work")
        self.time_fields.setVisible(mode == "timer")
        explanations = {
            "work": "Select a running conversation. Once it completes and no other Codex work is active, a 2-minute countdown starts. Errors and requests for input do not count as completion.",
            "limits": "Once every saved account is verified out of available usage and no Codex work is active, a 2-minute countdown starts.",
            "timer": "Time starts when you confirm. The last 2 minutes are included in your chosen duration. This mode does not wait for Codex work to finish.",
        }
        self.explanation.setText(tr(explanations[mode]))
        self.schedule_button.setEnabled(sys.platform == "win32")
        if self.plan.target is None and not self._preparing:
            self.status.setText(
                tr("Loading conversations…")
                if mode == "work" and self._loading
                else tr("Shutdown is off.")
            )
        if mode == "work" and not self.conversation.count():
            self.refresh_conversations()
        self._update_summary()

    def _update_summary(self) -> None:
        target = self.plan.target
        mode = target.mode if target else self.mode.currentData()
        titles = {
            "work": "When work finishes",
            "limits": "When all limits are reached",
            "timer": "When time runs out",
        }
        minutes = self.countdown_minutes.value()
        hours, rest = divmod(minutes, 60)
        duration = tr("{hours} hr {minutes} min", hours=hours, minutes=rest)
        self.duration_hint.setText(
            tr("Duration: {duration}. Range: 1–1440 minutes.", duration=duration)
        )
        for value, button in self.presets.items():
            button.setChecked(value == minutes)
        remaining = self.plan.remaining(time.monotonic()) if target else None
        state = (
            "Countdown active"
            if remaining is not None
            else "Waiting for condition"
            if target
            else "Not started"
        )
        self.summary_values["Condition"].setText(tr(titles[mode]))
        self.summary_values["Target"].setText(
            self.conversation.currentText() or tr("Choose a conversation")
            if mode == "work"
            else duration
            if mode == "timer"
            else tr("All saved accounts")
        )
        self.summary_values["Countdown"].setText(
            tr("Last 2 minutes included") if mode == "timer" else tr("2 minutes after verification")
        )
        self.summary_values["Status"].setText(tr(state))
        self.status_title.setText(tr(state) if target else tr("Shutdown plan is off"))
        if remaining is not None:
            hours, seconds = divmod(remaining, 3600)
            minutes, seconds = divmod(seconds, 60)
            self.clock.setText(f"{hours:02}:{minutes:02}:{seconds:02}")
        self.clock.setVisible(remaining is not None)

    def _choose_duration(self, minutes: int) -> None:
        self.countdown_minutes.setValue(minutes)
        self._update_summary()

    def refresh_conversations(self) -> None:
        if self._loading or self.plan.target is not None or self._preparing:
            return
        self._loading = True
        selected = self.conversation.currentData()
        self.refresh_button.setEnabled(False)
        self.status.setText(tr("Loading conversations…"))

        def done(items):
            self._loading = False
            self.refresh_button.setEnabled(self.plan.target is None)
            if self.plan.target is not None or self._preparing:
                return
            self.conversation.clear()
            for thread_id, title in items:
                self.conversation.addItem(title, thread_id)
            index = self.conversation.findData(selected)
            if index >= 0:
                self.conversation.setCurrentIndex(index)
            if self.mode.currentData() == "work":
                self.status.setText(
                    tr("Shutdown is off.")
                    if items
                    else tr("No running conversations found. Open your conversation and refresh.")
                )

        def failed(_error):
            done([])
            if self.plan.target is None and self.mode.currentData() == "work":
                self.status.setText(
                    tr(
                        "Conversations could not be loaded. Keep Codex or your IDE open and refresh."
                    )
                )

        self.runner.submit(self.checks.conversations(), done, failed)

    def _schedule(self) -> None:
        if self._preparing or self.plan.target is not None:
            return
        mode = self.mode.currentData()
        if mode == "work":
            thread_id = self.conversation.currentData()
            if not thread_id:
                self.status.setText(tr("Select a verified running conversation first."))
                return
            self.arm_work(thread_id)
        else:
            self.arm(PowerTarget(mode))

    def _set_editable(self, editable: bool) -> None:
        self.mode.setEnabled(editable)
        self.conversation.setEnabled(editable)
        self.refresh_button.setEnabled(editable and not self._loading)
        self.countdown_minutes.setEnabled(editable)
        self.schedule_button.setEnabled(editable and sys.platform == "win32")
        self.cancel_button.setEnabled(not editable)
        for button in [*self.mode_buttons.buttons(), *self.presets.values()]:
            button.setEnabled(editable)
        self._update_summary()

    def arm_work(self, thread_id: str) -> None:
        if self._preparing or self.plan.target is not None:
            return
        self._preparing = True
        self._set_editable(False)
        self.status.setText(tr("Verifying selected work…"))
        generation = self.plan.generation

        def ready(target):
            if generation == self.plan.generation:
                self._preparing = False
                self._set_editable(True)
                self.arm(target)

        def failed(_error):
            if generation == self.plan.generation:
                self._preparing = False
                self._set_editable(True)
                self.status.setText(tr("Select a verified running conversation first."))

        self.runner.submit(self.checks.work_target(thread_id), ready, failed)

    def arm(self, target: PowerTarget) -> None:
        if sys.platform != "win32" or self.plan.target is not None:
            return
        condition = {
            "limits": "When all saved accounts reach their usage limits.",
            "work": "When the selected work is complete.",
            "timer": "After a set time",
        }[target.mode]
        minutes = self.countdown_minutes.value() if target.mode == "timer" else 2
        confirmation = (
            tr(
                "Shut down in {minutes} minutes from now, even if Codex work is still running? Save work in other applications.",
                minutes=minutes,
            )
            if target.mode == "timer"
            else tr(
                "Enable shutdown for this session? Condition: {condition} Countdown: {minutes} minutes. Save work in other applications.",
                condition=tr(condition),
                minutes=minutes,
            )
        )
        if (
            QMessageBox.question(
                self,
                tr("Schedule shutdown"),
                confirmation,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            self.status.setText(tr("Shutdown is off."))
            return
        self.plan.set_seconds(minutes * 60)
        self.plan.arm(target, now=time.monotonic())
        self._warned = False
        self.mode.setCurrentIndex(self.mode.findData(target.mode))
        if target.thread_id:
            index = self.conversation.findData(target.thread_id)
            if index < 0:
                self.conversation.addItem(target.thread_id[:12], target.thread_id)
                index = self.conversation.count() - 1
            self.conversation.setCurrentIndex(index)
        self._set_editable(False)
        self._next_check = 0.0
        self.timer.start()
        self._tick()

    def cancel(self) -> None:
        self.plan.cancel()
        self._preparing = False
        self.timer.stop()
        self._set_editable(True)
        self.status.setText(tr("Shutdown is off."))
        self.status_changed.emit(tr("Shutdown is off."), False)
        self._update_summary()

    def _tick(self) -> None:
        target = self.plan.target
        if target is None:
            return
        now = time.monotonic()
        remaining = self.plan.remaining(now)
        text = (
            tr(self.plan.reason)
            if remaining is None
            else tr("Shutting down in {seconds}s. Save your work or cancel.", seconds=remaining)
        )
        if target.mode == "timer" and remaining is not None:
            minutes, seconds = divmod(remaining, 60)
            hours, minutes = divmod(minutes, 60)
            text = tr(
                "Timed shutdown in {time}. Codex work will not delay it.",
                time=f"{hours:02}:{minutes:02}:{seconds:02}",
            )
        self.status.setText(text)
        self._update_summary()
        self.status_changed.emit(text, True)
        if target.mode == "timer":
            if (
                remaining is not None
                and remaining <= CONDITION_COUNTDOWN_SECONDS
                and not self._warned
            ):
                self._warned = True
                self.countdown_started.emit()
            if remaining == 0:
                self._execute()
            return
        if self._busy or now < self._next_check:
            return
        self._busy = True
        generation = self.plan.generation

        def checked(evidence: PowerEvidence):
            self._busy = False
            if generation != self.plan.generation or self.plan.target is None:
                return
            before = self.plan.deadline
            self.plan.observe(evidence, time.monotonic())
            if before is None and self.plan.deadline is not None:
                self.countdown_started.emit()
            if self.plan.remaining(time.monotonic()) == 0:
                self._execute()
                return
            self._next_check = time.monotonic() + (15 if self.plan.deadline is not None else 60)
            self._tick()

        self.runner.submit(
            self.checks.check(target),
            checked,
            lambda _error: checked(PowerEvidence(False, "Waiting for a fresh check.")),
        )

    def _execute(self) -> None:
        from codex_account_manager.platform.power import shutdown_windows

        self.cancel()
        try:
            from codex_account_manager.core.operation_lock import OperationLock
            from codex_account_manager.core.paths import paths

            with OperationLock(paths.data_dir / "account-operation.lock"):
                shutdown_windows()
        except Exception:
            self.status.setText(tr("Windows did not accept shutdown. The plan was cancelled."))
        else:
            self.status.setText(
                tr("Shutdown requested. Windows may ask you to save open applications.")
            )


class PowerView(BaseView):
    def __init__(self, runner, accounts, palette=DARK):
        super().__init__(palette)
        self._root.addWidget(
            view_header(
                tr("Automatic shutdown"),
                tr("Choose a condition, review your plan and confirm."),
            )
        )
        self.controls = PowerControls(runner, accounts)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setWidget(self.controls)
        self._root.addWidget(self.scroll_area, 1)
