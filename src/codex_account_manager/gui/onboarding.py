"""First-run preferences and an optional tour of the real application pages."""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from codex_account_manager.core.privacy import policy_path
from codex_account_manager.gui.design import app_icon
from codex_account_manager.gui.i18n import tr
from codex_account_manager.gui.view_base import setting_row
from codex_account_manager.gui.widgets import ComboBox, ToggleSwitch, label
from codex_account_manager.platform import package
from codex_account_manager.storage.repositories import ProfileRepository, SettingsRepository


async def needs_setup() -> bool:
    settings = SettingsRepository()
    values = await settings.all()
    if package.is_packaged() and values.get("store_setup_completed") != "true":
        return True
    if values.get("setup_completed") == "true":
        return False
    if values or await ProfileRepository().list():
        await settings.set("setup_completed", "true")
        return False
    return True


class SetupDialog(QDialog):
    def __init__(self, runner):
        super().__init__()
        self.setObjectName("SetupDialog")
        self._packaged = package.is_packaged()
        self.setWindowTitle(tr("Welcome to QuotaCrew"))
        self.setMinimumSize(900, 620)
        screen = QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        self.resize(
            min(1200, available.width() - 40) if available else 1200,
            min(820, available.height() - 60) if available else 820,
        )
        shell = QHBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName("SetupSidebar")
        sidebar.setFixedWidth(250)
        rail = QVBoxLayout(sidebar)
        rail.setContentsMargins(28, 32, 24, 24)
        rail.setSpacing(12)
        brand = QHBoxLayout()
        mark = label()
        mark.setPixmap(app_icon(44).pixmap(44, 44))
        brand.addWidget(mark)
        brand.addWidget(label("QuotaCrew", "SetupBrand"))
        rail.addLayout(brand)
        tagline = label(tr("Smart quota management for developers."), "Caption")
        tagline.setWordWrap(True)
        rail.addWidget(tagline)
        rail.addSpacing(32)
        self.step_indicators = []
        for number, name in enumerate(
            ("Connection tools", "Preferences", "IDE connection", "Ready"), 1
        ):
            row = QWidget()
            row.setObjectName("GridHost")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 8, 0, 8)
            row_layout.setSpacing(14)
            circle = label(str(number), "SetupStepNumber")
            circle.setFixedSize(36, 36)
            circle.setAlignment(Qt.AlignmentFlag.AlignCenter)
            caption = label(tr(name), "SetupStepLabel")
            caption.setWordWrap(True)
            row_layout.addWidget(circle)
            row_layout.addWidget(caption, 1)
            rail.addWidget(row)
            self.step_indicators.append((circle, caption))
            if number < 4:
                connector = QFrame()
                connector.setObjectName("SetupConnector")
                connector.setFixedSize(2, 18)
                connector_row = QHBoxLayout()
                connector_row.setContentsMargins(17, 0, 0, 0)
                connector_row.addWidget(connector)
                connector_row.addStretch()
                rail.addLayout(connector_row)
        rail.addStretch()
        self.progress = label("", "Caption")
        rail.addWidget(self.progress)
        shell.addWidget(sidebar)
        main = QWidget()
        main.setObjectName("GridHost")
        root = QVBoxLayout(main)
        root.setContentsMargins(32, 32, 32, 24)
        root.setSpacing(24)
        shell.addWidget(main, 1)
        self.title = label("", "SetupTitle")
        self.title.setWordWrap(True)
        root.addWidget(self.title)
        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)
        from codex_account_manager.gui.cli_setup import CliSetup

        prerequisites = self._page("Prepare the connection tools, then choose how QuotaCrew works.")
        self.cli = CliSetup(runner)
        self.cli.guide.hide()
        self.cli_ready = False
        self.cli.ready.connect(self._cli_ready)
        prerequisites.addWidget(self.cli)
        from codex_account_manager.gui.desktop_setup import DesktopSetup, IDESetup

        self.desktop = DesktopSetup(runner)
        prerequisites.addWidget(self.desktop)
        self.ide = IDESetup(runner)
        prerequisites.addWidget(self.ide)
        prerequisites.addStretch()
        general = self._page(
            "Choose how the app works. Nothing starts until you save these preferences. You can change them later in Settings."
        )
        self.monitoring = ToggleSwitch(tr("Background monitoring"))
        self.monitoring.setChecked(True)
        setting_row(
            general,
            tr("Background monitoring"),
            tr("Check account usage every 60 seconds."),
            self.monitoring,
        )
        self.policy = ComboBox()
        for title, value in (
            ("Automatic when usage is limited", "availability_failover"),
            ("Ask before switching", "confirm"),
            ("Manual — I choose", "manual"),
        ):
            self.policy.addItem(tr(title), value)
        setting_row(
            general,
            tr("Switch policy"),
            tr(
                "Account switching works without Desktop. An installed Desktop app is restarted to reload its account."
            ),
            self.policy,
        )
        self.auto_continue = ToggleSwitch(tr("Automatic continuation"))
        self.auto_continue.setChecked(True)
        setting_row(
            general,
            tr("Automatic continuation"),
            tr(
                "Continue verified quota-interrupted conversations after switching. Pauses and approvals remain yours."
            ),
            self.auto_continue,
        )
        self.desktop_continue = ToggleSwitch(tr("Desktop continuation"))
        self.desktop_continue.setChecked(True)
        setting_row(
            general,
            tr("Desktop continuation"),
            tr(
                "Continue Desktop conversations only. Requires the Desktop app; VS Code does not require it."
            ),
            self.desktop_continue,
        )
        self.background = ToggleSwitch(tr("Keep running in the tray"))
        setting_row(
            general,
            tr("Keep running in the tray"),
            tr("Closing the window quits unless this is enabled."),
            self.background,
        )
        self.check_updates = ToggleSwitch(tr("Check for updates automatically"))
        self.check_updates.setChecked(True)
        if self._packaged:
            self.check_updates.setChecked(False)
            self.check_updates.setEnabled(False)
        setting_row(
            general,
            tr("Check for updates automatically"),
            tr(
                "Updates for this installation are managed by Microsoft Store."
                if self._packaged
                else "Check GitHub once a day. Download and installation start only when you choose Update."
            ),
            self.check_updates,
        )
        general.addStretch()
        ide = self._page(
            "IDE continuation and VS Code refresh are enabled by default. These experimental options use the local Codex extension. Turn them off if you do not use it."
        )
        self.ide_continue = ToggleSwitch(tr("IDE continuation"))
        self.ide_continue.setChecked(True)
        setting_row(
            ide,
            tr("IDE continuation · Experimental"),
            tr("Continue in the existing IDE conversation with its tools."),
            self.ide_continue,
        )
        self.ide_refresh = ToggleSwitch(tr("Refresh VS Code after switching"))
        self.ide_refresh.setChecked(True)
        self.ide_continue.toggled.connect(self._ide_changed)
        setting_row(
            ide,
            tr("Refresh VS Code after switching"),
            tr(
                "Close and reopen one local VS Code window to reload its account. Save prompts and other running IDE work are respected."
            ),
            self.ide_refresh,
        )
        note = label(
            tr(
                "Windows startup is optional in Settings. Automatic shutdown stays off and must be enabled separately for each session."
            ),
            "Muted",
        )
        note.setWordWrap(True)
        ide.addWidget(note)
        ide.addStretch()
        summary = self._page(
            "Review your choices. Account sign-in opens only when you add an account."
        )
        self.summary = label("", "Body")
        self.summary.setWordWrap(True)
        summary.addWidget(self.summary)
        retention = label(
            tr(
                "Uninstalling QuotaCrew keeps saved accounts, settings and backups on this PC. A separately installed Codex CLI and its PATH entry also remain. See Help → Privacy policy for removal instructions."
            ),
            "Muted",
        )
        retention.setWordWrap(True)
        summary.addWidget(retention)
        from codex_account_manager.gui.shortcuts import ShortcutPanel

        summary.addWidget(ShortcutPanel(runner))
        self.tour = QCheckBox(tr("Show me around after setup"))
        self.tour.setChecked(True)
        summary.addWidget(self.tour)
        summary.addStretch()
        footer_panel = QFrame()
        footer_panel.setObjectName("GridHost")
        footer = QHBoxLayout(footer_panel)
        footer.setContentsMargins(18, 16, 18, 16)
        privacy = QPushButton(tr("Privacy policy"))
        privacy.setObjectName("SetupLink")
        privacy.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(policy_path())))
        )
        guide = QPushButton(tr("Setup guide"))
        guide.setObjectName("SetupLink")
        guide.clicked.connect(self.cli.guide.click)
        self.back = QPushButton(tr("Back"))
        self.next = QPushButton(tr("Next"))
        self.next.setObjectName("Primary")
        self.back.clicked.connect(lambda: self._step(-1))
        self.next.clicked.connect(self._advance)
        footer.addWidget(privacy)
        footer.addWidget(label("|", "Caption"))
        footer.addWidget(guide)
        footer.addStretch()
        footer.addWidget(self.back)
        footer.addWidget(self.next)
        root.addWidget(footer_panel)
        self._step(0)
        for control in (
            self.monitoring,
            self.auto_continue,
            self.desktop_continue,
            self.background,
            self.check_updates,
            self.ide_continue,
            self.ide_refresh,
        ):
            control.setFixedWidth(120)
        self.policy.setFixedWidth(220)

    def _page(self, description: str) -> QVBoxLayout:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content.setObjectName("GridHost")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 12, 0)
        layout.setSpacing(20)
        body = label(tr(description), "Muted")
        body.setWordWrap(True)
        layout.addWidget(body)
        if self.pages.count() > 0:
            card = QFrame()
            card.setObjectName("SetupCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(24, 16, 24, 16)
            card_layout.setSpacing(16)
            layout.addWidget(card)
            layout = card_layout
        scroll.setWidget(content)
        self.pages.addWidget(scroll)
        return layout

    def _ide_changed(self, enabled: bool) -> None:
        self.ide_refresh.setEnabled(enabled)
        if not enabled:
            self.ide_refresh.setChecked(False)

    def values(self) -> dict[str, str]:
        values = {
            "monitor_enabled": str(self.monitoring.isChecked()).lower(),
            "switch_policy": self.policy.currentData(),
            "auto_continue": str(self.auto_continue.isChecked()).lower(),
            "desktop_continue": str(self.desktop_continue.isChecked()).lower(),
            "ide_continue": str(self.ide_continue.isChecked()).lower(),
            "ide_refresh": str(
                self.ide_continue.isChecked() and self.ide_refresh.isChecked()
            ).lower(),
            "keep_in_tray": str(self.background.isChecked()).lower(),
            "poll_seconds": "60",
            "setup_completed": "true",
            "check_updates": str(self.check_updates.isChecked()).lower(),
        }
        if self._packaged:
            values.pop("check_updates")
            values["store_setup_completed"] = "true"
        return values

    def _step(self, delta: int) -> None:
        index = self.pages.currentIndex() + delta
        self.pages.setCurrentIndex(index)
        self.progress.setText(tr("Step {current} of {total}", current=index + 1, total=4))
        self.title.setText(
            tr(
                (
                    "Let's prepare the Codex connection",
                    "Make it yours",
                    "Continue in your IDE",
                    "Ready when you are",
                )[index]
            )
        )
        for number, controls in enumerate(self.step_indicators):
            for indicator in controls:
                indicator.setProperty(
                    "state",
                    "current" if number == index else "done" if number < index else "pending",
                )
                indicator.style().unpolish(indicator)
                indicator.style().polish(indicator)
        self.back.setEnabled(index > 0)
        self.next.setEnabled(index != 0 or self.cli_ready)
        self.next.setText(tr("Save and start") if index == 3 else tr("Next"))
        if index == 3:
            rows = [
                (tr("Switch policy"), self.policy.currentText()),
                (tr("Check interval"), tr("60 seconds")),
            ]
            for title, control in (
                ("Background monitoring", self.monitoring),
                ("Automatic continuation", self.auto_continue),
                ("Desktop continuation", self.desktop_continue),
                ("IDE continuation", self.ide_continue),
                ("Refresh VS Code after switching", self.ide_refresh),
                ("Keep running in the tray", self.background),
                ("Check for updates automatically", self.check_updates),
            ):
                rows.append((tr(title), tr("On") if control.isChecked() else tr("Off")))
            self.summary.setText("\n".join(f"{title}: {value}" for title, value in rows))

    def _advance(self) -> None:
        if self.pages.currentIndex() == 0 and not self.cli_ready:
            return
        if self.pages.currentIndex() == 3:
            self.accept()
        else:
            self._step(1)

    def _cli_ready(self, ready: bool) -> None:
        self.cli_ready = ready
        if self.pages.currentIndex() == 0:
            self.next.setEnabled(ready)

    def reject(self) -> None:
        if self.cli.busy:
            return
        super().reject()


class ProductTour(QDialog):
    STEPS = (
        (
            0,
            "Overview",
            "See account capacity and monitoring status here. Start or pause monitoring from the top bar.",
        ),
        (
            1,
            "Accounts",
            "Add an account to open sign-in. Use its row menu to switch, rename or remove it.",
        ),
        (
            2,
            "Jobs",
            "Follow observed conversations and check each conversation's continuation details from its row menu.",
        ),
        (
            6,
            "Settings",
            "Change switching, continuation and tray preferences here. IDE continuation and VS Code refresh are separate choices.",
        ),
        (
            8,
            "Automatic shutdown",
            "Choose a condition and a countdown in minutes. Shutdown stays off until you explicitly enable it.",
        ),
    )

    def __init__(self, window):
        super().__init__(window)
        self.host_window = window
        self.original_page = window.stack.currentIndex()
        self.index = 0
        self.setWindowTitle(tr("Quick tour"))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(500, 240)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)
        self.progress = label("", "Caption")
        self.title = label("", "H2")
        self.body = QLabel()
        self.body.setWordWrap(True)
        layout.addWidget(self.progress)
        layout.addWidget(self.title)
        layout.addWidget(self.body, 1)
        footer = QHBoxLayout()
        skip = QPushButton(tr("Skip tour"))
        skip.clicked.connect(self.reject)
        self.back = QPushButton(tr("Back"))
        self.back.clicked.connect(lambda: self._show_step(-1))
        self.next = QPushButton(tr("Next"))
        self.next.setObjectName("Primary")
        self.next.clicked.connect(self._advance)
        footer.addWidget(skip)
        footer.addStretch()
        footer.addWidget(self.back)
        footer.addWidget(self.next)
        layout.addLayout(footer)
        self.finished.connect(lambda _: window.nav.setCurrentRow(self.original_page))
        self._show_step(0)

    def _show_step(self, delta: int) -> None:
        self.index += delta
        page, title, body = self.STEPS[self.index]
        self.host_window.nav.setCurrentRow(page)
        self.progress.setText(
            tr("Step {current} of {total}", current=self.index + 1, total=len(self.STEPS))
        )
        self.title.setText(tr(title))
        self.body.setText(tr(body))
        self.back.setEnabled(self.index > 0)
        self.next.setText(tr("Finish") if self.index == len(self.STEPS) - 1 else tr("Next"))

    def _advance(self) -> None:
        if self.index == len(self.STEPS) - 1:
            self.accept()
        else:
            self._show_step(1)
