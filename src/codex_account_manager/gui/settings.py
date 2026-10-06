"""Settings screen."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QHBoxLayout,
    QLayout,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from codex_account_manager.gui.async_runner import AsyncRunner
from codex_account_manager.gui.design import DARK
from codex_account_manager.gui.i18n import (
    language,
    tr,
)
from codex_account_manager.gui.view_base import BaseView, setting_row, view_header
from codex_account_manager.gui.widgets import ComboBox, ToggleSwitch
from codex_account_manager.storage.repositories import SettingsRepository


class SettingsView(BaseView):
    policy_changed = Signal(str)
    monitoring_changed = Signal(bool)
    background_changed = Signal(bool)
    diagnostics_requested = Signal()
    notes_requested = Signal()
    continuation_changed = Signal()

    def __init__(self, runner: AsyncRunner, on_theme_toggle: Callable[[bool], None], palette=DARK):
        super().__init__(palette)
        self.runner = runner
        self._on_theme_toggle = on_theme_toggle
        self._root.addWidget(
            view_header(tr("Settings"), tr("Preferences are stored locally · no telemetry"))
        )

        self.tabs = QTabWidget()
        self.tabs.setObjectName("SettingsTabs")

        self.tab_general = QWidget()
        self.tab_general.setObjectName("PagePanel")
        general_scroll = QScrollArea()
        general_scroll.setWidgetResizable(True)
        general_scroll.setWidget(self.tab_general)
        general_layout = QVBoxLayout(self.tab_general)
        general_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        general_layout.setContentsMargins(24, 20, 24, 20)
        general_layout.setSpacing(8)

        self.tab_cont = QWidget()
        self.tab_cont.setObjectName("PagePanel")
        cont_scroll = QScrollArea()
        cont_scroll.setWidgetResizable(True)
        cont_scroll.setWidget(self.tab_cont)
        cont_layout = QVBoxLayout(self.tab_cont)
        cont_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        cont_layout.setContentsMargins(24, 20, 24, 20)
        cont_layout.setSpacing(8)

        self.tabs.addTab(general_scroll, tr("General"))
        self.tabs.addTab(cont_scroll, tr("Continuity"))

        self._root.addWidget(self.tabs, 1)

        self.policy = ComboBox()
        for title, value in (
            (tr("Manual — I choose"), "manual"),
            (tr("Ask before switching"), "confirm"),
            (tr("Automatic when usage is limited"), "availability_failover"),
        ):
            self.policy.addItem(title, value)
        self.policy.setCurrentIndex(self.policy.findData("availability_failover"))

        self.monitoring = ToggleSwitch(tr("Automatic monitoring"))
        self.monitoring.toggled.connect(self._save_monitoring)
        setting_row(
            general_layout,
            tr("Background monitoring"),
            tr("Checks are on by default for new installations. Pause them here at any time."),
            self.monitoring,
        )

        setting_row(
            general_layout,
            tr("Switch policy"),
            tr(
                "Account switching works without Desktop. An installed Desktop app is restarted to reload its account."
            ),
            self.policy,
        )

        self.background = ToggleSwitch(tr("Keep running in the tray"))
        self.background.toggled.connect(self._save_background)
        setting_row(
            general_layout,
            tr("Keep running in the tray"),
            tr(
                "When enabled, closing the window keeps monitoring in the tray. Exit from the tray menu to quit."
            ),
            self.background,
        )

        self.theme = ComboBox()
        self.theme.addItem(tr("Dark"), "dark")
        self.theme.addItem(tr("Light"), "light")
        self.theme.currentIndexChanged.connect(self._save_theme)
        setting_row(general_layout, tr("Appearance"), "", self.theme)

        self.language = ComboBox()
        self.language.addItem("Türkçe", "tr")
        self.language.addItem("English", "en")
        self.language.setCurrentIndex(self.language.findData(language()))
        self.language.currentIndexChanged.connect(self._save_language)
        setting_row(
            general_layout, tr("Language"), tr("Restart the application to apply."), self.language
        )

        self.startup = ComboBox()
        self.startup.addItem(tr("Off"), False)
        self.startup.addItem(tr("On"), True)
        self.startup.setEnabled(False)
        self.startup.currentIndexChanged.connect(self._toggle_startup)
        setting_row(
            general_layout,
            tr("Start with Windows"),
            tr("Keep monitoring available after you sign in."),
            self.startup,
        )

        tools = QHBoxLayout()
        tools.setSpacing(10)
        tools.setContentsMargins(0, 16, 0, 0)
        for title, signal in (
            ("Diagnostics", self.diagnostics_requested),
            ("Goals", self.notes_requested),
        ):
            button = QPushButton(tr(title))
            button.clicked.connect(signal.emit)
            tools.addWidget(button)
        tools.addStretch()
        general_layout.addLayout(tools)
        general_layout.addStretch()

        self.auto_continue = ToggleSwitch(tr("Auto continue"))
        self.auto_continue.setAccessibleName(tr("Continue interrupted work after switching"))
        self.auto_continue.setToolTip(
            tr(
                "After a verified usage-limit interruption, continue the same conversation and its active goal. Pauses, goal budgets and approval requests are respected. Turn this off to stop automatic work."
            )
        )
        self.auto_continue.setChecked(True)
        self.auto_continue.toggled.connect(self._save_auto_continue)
        setting_row(
            cont_layout,
            tr("Automatic continuation"),
            tr(
                "Master switch for automatic work. Desktop and IDE choices below are independent; missing applications do not disable other connections."
            ),
            self.auto_continue,
        )

        self.desktop_continue = ToggleSwitch(tr("Desktop continuation"))
        self.desktop_continue.setChecked(True)
        self.desktop_continue.toggled.connect(self._save_desktop_continue)
        setting_row(
            cont_layout,
            tr("Desktop continuation"),
            tr(
                "Continue Desktop conversations only. Requires the Desktop app; VS Code does not require it."
            ),
            self.desktop_continue,
        )

        self.ide_continue = ToggleSwitch(tr("IDE continuation"))
        self.ide_continue.toggled.connect(self._save_ide_continue)
        setting_row(
            cont_layout,
            tr("IDE continuation · Experimental"),
            tr(
                "Use the existing Codex conversation owner in a local IDE or Desktop. The Codex extension must remain open. Unsupported versions stop safely; no CLI fallback."
            ),
            self.ide_continue,
        )

        self.ide_refresh = ToggleSwitch(tr("Refresh VS Code after switching"))
        self.ide_refresh.toggled.connect(
            lambda enabled: self.runner.submit(
                SettingsRepository().set("ide_refresh", str(enabled).lower()),
                lambda _result: self._monitor_saved(),
            )
        )
        setting_row(
            cont_layout,
            tr("Refresh VS Code after switching"),
            tr(
                "Close and reopen one local VS Code window before IDE continuation to reload its account. Save prompts are never dismissed; other running IDE work blocks refresh."
            ),
            self.ide_refresh,
        )

        self.interval = QSpinBox()
        self.interval.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.interval.setRange(30, 3600)
        self.interval.setSingleStep(30)
        self.interval.setSuffix(tr(" seconds"))
        self.interval.setValue(60)
        self.interval.editingFinished.connect(self._save_interval)
        setting_row(
            cont_layout,
            tr("Check interval"),
            tr("30–3600 seconds. Changes apply immediately."),
            self.interval,
        )
        cont_layout.addStretch()

        from codex_account_manager.gui.cli_setup import CliSetup

        self.cli = CliSetup(runner)
        general_layout.addSpacing(12)
        general_layout.addWidget(self.cli)
        from codex_account_manager.gui.desktop_setup import DesktopSetup, IDESetup

        self.desktop = DesktopSetup(runner)
        general_layout.addWidget(self.desktop)
        self.ide = IDESetup(runner)
        general_layout.addWidget(self.ide)
        from codex_account_manager.gui.shortcuts import ShortcutPanel

        general_layout.addWidget(ShortcutPanel(runner))
        self._load()

    def _load(self) -> None:
        from codex_account_manager.storage.repositories import SettingsRepository

        self.runner.submit(SettingsRepository().all(), self._loaded)
        self.policy.currentIndexChanged.connect(self._save_policy)
        self._refresh_startup()

    def refresh(self) -> None:
        # Recheck after the user installs a dependency or an app update. Saved
        # choices remain untouched even if an optional application is absent.
        self.desktop.refresh()
        self.ide.refresh()

    def _loaded(self, values: dict[str, str]) -> None:
        with QSignalBlocker(self.policy):
            self.policy.setCurrentIndex(
                max(0, self.policy.findData(values.get("switch_policy", "availability_failover")))
            )
        from codex_account_manager.monitoring.settings import poll_interval

        with QSignalBlocker(self.interval):
            self.interval.setValue(poll_interval(values.get("poll_seconds")))
        with QSignalBlocker(self.auto_continue):
            self.auto_continue.setChecked(values.get("auto_continue", "true") == "true")
        with QSignalBlocker(self.ide_continue):
            self.ide_continue.setChecked(values.get("ide_continue", "false") == "true")
        with QSignalBlocker(self.desktop_continue):
            self.desktop_continue.setChecked(values.get("desktop_continue", "true") == "true")
        with QSignalBlocker(self.ide_refresh):
            self.ide_refresh.setChecked(values.get("ide_refresh", "false") == "true")
        with QSignalBlocker(self.monitoring):
            self.monitoring.setChecked(values.get("monitor_enabled", "true") == "true")
        with QSignalBlocker(self.background):
            self.background.setChecked(values.get("keep_in_tray", "false") == "true")
        self.monitoring_changed.emit(self.monitoring.isChecked())
        self.background_changed.emit(self.background.isChecked())
        self.continuation_changed.emit()
        dark = values.get("theme", "dark") == "dark"
        with QSignalBlocker(self.theme):
            self.theme.setCurrentIndex(0 if dark else 1)
        self._on_theme_toggle(dark)
        self.policy_changed.emit(self.policy.currentData())

    def _save_monitoring(self, enabled: bool) -> None:
        from codex_account_manager.core.events import bus
        from codex_account_manager.storage.repositories import SettingsRepository

        def saved(_result):
            self.monitoring_changed.emit(enabled)
            if not enabled:
                bus.publish("continuation.stop")
            self._monitor_saved()

        self.runner.submit(SettingsRepository().set("monitor_enabled", str(enabled).lower()), saved)

    def _save_ide_continue(self, enabled: bool) -> None:
        from codex_account_manager.core.events import bus
        from codex_account_manager.storage.repositories import SettingsRepository

        def saved(_result):
            if not enabled:
                bus.publish("continuation.stop", scope="ide")
            self.continuation_changed.emit()
            self._monitor_saved()

        self.runner.submit(SettingsRepository().set("ide_continue", str(enabled).lower()), saved)

    def _save_desktop_continue(self, enabled: bool) -> None:
        from codex_account_manager.core.events import bus

        def saved(_result):
            if not enabled:
                bus.publish("continuation.stop", scope="desktop")
            self.continuation_changed.emit()
            self._monitor_saved()

        self.runner.submit(
            SettingsRepository().set("desktop_continue", str(enabled).lower()), saved
        )

    def _save_background(self, enabled: bool) -> None:
        from codex_account_manager.storage.repositories import SettingsRepository

        self.runner.submit(
            SettingsRepository().set("keep_in_tray", str(enabled).lower()),
            lambda _: self.background_changed.emit(enabled),
        )

    def _save_auto_continue(self, enabled: bool) -> None:
        from codex_account_manager.core.events import bus
        from codex_account_manager.storage.repositories import SettingsRepository

        def saved(_result) -> None:
            if not enabled:
                bus.publish("continuation.stop")
            self.continuation_changed.emit()
            self._monitor_saved()

        self.runner.submit(SettingsRepository().set("auto_continue", str(enabled).lower()), saved)

    def _save_policy(self, _index: int) -> None:
        from codex_account_manager.storage.repositories import SettingsRepository

        value = self.policy.currentData()
        self.runner.submit(
            SettingsRepository().set("switch_policy", value),
            lambda _: self._monitor_saved(value),
        )

    def _monitor_saved(self, policy: str | None = None) -> None:
        from codex_account_manager.core.events import bus

        if policy:
            self.policy_changed.emit(policy)
        bus.publish("monitor.settings_changed")

    def _save_interval(self) -> None:
        from codex_account_manager.storage.repositories import SettingsRepository

        self.runner.submit(
            SettingsRepository().set("poll_seconds", str(self.interval.value())),
            lambda _: self._monitor_saved(),
        )

    def _save_theme(self, _index: int) -> None:
        from codex_account_manager.storage.repositories import SettingsRepository

        value = self.theme.currentData()
        self._on_theme_toggle(value == "dark")
        self.runner.submit(SettingsRepository().set("theme", value.lower()))

    def _save_language(self, _index: int) -> None:
        from codex_account_manager.storage.repositories import SettingsRepository

        self.runner.submit(SettingsRepository().set("language", self.language.currentData()))

    def _refresh_startup(self) -> None:
        import sys

        if sys.platform != "win32":
            self.startup.setEnabled(False)
            self.startup.setToolTip(tr("Startup is available on Windows"))
            return
        import asyncio

        from codex_account_manager.platform import startup

        self.runner.submit(
            asyncio.to_thread(startup.is_start_with_windows_enabled), self._startup_loaded
        )

    def _startup_loaded(self, enabled: bool) -> None:
        self._startup_enabled = enabled
        with QSignalBlocker(self.startup):
            self.startup.setCurrentIndex(self.startup.findData(enabled))
        self.startup.setEnabled(True)

    def _toggle_startup(self, _index: int) -> None:
        import asyncio
        import subprocess
        import sys

        from codex_account_manager.platform import startup

        args = (
            [sys.executable]
            if getattr(sys, "frozen", False)
            else [sys.executable, "-m", "codex_account_manager.gui.tray_main"]
        )
        action = (
            (lambda: startup.enable_start_with_windows(subprocess.list2cmdline(args)))
            if self.startup.currentData()
            else startup.disable_start_with_windows
        )
        self.startup.setEnabled(False)
        self.runner.submit(
            asyncio.to_thread(action), self._startup_done, lambda _: self._startup_done(False)
        )

    def _startup_done(self, success: bool) -> None:
        if not success:
            QMessageBox.warning(
                self, tr("Startup"), tr("Windows could not update the startup task.")
            )
        self._refresh_startup()
