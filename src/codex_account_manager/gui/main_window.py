"""Main window: branded sidebar, stacked views, live theming and system tray."""

from __future__ import annotations

import sys

from PySide6.QtCore import QObject, QSignalBlocker, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from codex_account_manager.accounts.service import AccountService
from codex_account_manager.continuity.service import ContinuityService
from codex_account_manager.core.events import bus
from codex_account_manager.core.logging import get_logger
from codex_account_manager.gui.about import AboutView
from codex_account_manager.gui.accounts import AccountsView
from codex_account_manager.gui.activity import ActivityView
from codex_account_manager.gui.async_runner import AsyncRunner
from codex_account_manager.gui.conversations import ConversationsView
from codex_account_manager.gui.design import (
    DARK,
    LIGHT,
    app_icon,
    set_button_icon,
    spaced_icon,
    tray_icon,
)
from codex_account_manager.gui.diagnostics import DiagnosticsView
from codex_account_manager.gui.goals import GoalsView
from codex_account_manager.gui.i18n import continuation_detail, tr
from codex_account_manager.gui.overview import DashboardView
from codex_account_manager.gui.power import PowerView
from codex_account_manager.gui.settings import SettingsView
from codex_account_manager.gui.theme import application_palette, stylesheet
from codex_account_manager.gui.widgets import label

log = get_logger(__name__)

_NAV = [
    ("Overview", "home"),
    ("Accounts", "accounts"),
    ("Jobs", "list"),
    ("Activity", "history"),
    ("Goals", "goals"),
    ("Diagnostics", "diagnostics"),
    ("Settings", "settings"),
    ("Help", "help"),
    ("Shutdown", "power"),
]


class _EventBridge(QObject):
    received = Signal(object)


class MainWindow(QMainWindow):
    def __init__(self, runner: AsyncRunner, *, dark: bool = True):
        super().__init__()
        self.runner = runner
        self.accounts = AccountService()
        self._dark = dark
        self._switching = False
        self._keep_in_tray = False
        self._monitor_enabled = False
        self._monitor_message = tr("Monitoring paused")
        self._power_active = False
        self._power_message = ""
        self._running_work = 0
        self._unverified_work = 0
        self._continuation_issues: dict[str, str] = {}
        self._dismissed_continuation_issues: dict[str, str] = {}
        self._shared_waits: set[str] = set()
        self.runner.failed.connect(self._operation_failed)
        self._bridge = _EventBridge(self)
        self._bridge.received.connect(self._domain_event)
        self._unsubscribe = bus.subscribe("*", self._bridge.received.emit)
        self.setWindowTitle(tr("QuotaCrew"))
        self.setWindowIcon(app_icon())
        self.setMinimumSize(1040, 700)
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        self.resize(
            min(1536, available.width() - 40) if available else 1536,
            min(1024, available.height() - 60) if available else 1024,
        )

        root = QWidget()
        root.setObjectName("Root")
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.sidebar = self._build_sidebar()
        layout.addWidget(self.sidebar)

        content = QWidget()
        content.setObjectName("Content")
        content_layout = QVBoxLayout(content)
        self._content_layout = content_layout
        content_layout.setContentsMargins(28, 24, 34, 0)
        content_layout.setSpacing(0)
        self.stack = QStackedWidget()
        self._build_views()
        for view in self._views:
            self.stack.addWidget(view)
        self.back_to_settings = QPushButton(tr("Back to settings"))
        self.back_to_settings.setObjectName("Ghost")
        self.back_to_settings.clicked.connect(lambda: self._on_nav(6))
        self.back_to_settings.hide()
        self.topbar = QFrame()
        self.topbar.setObjectName("Topbar")
        top = QHBoxLayout(self.topbar)
        top.setContentsMargins(24, 12, 16, 12)
        top.setSpacing(14)
        self.status_dot = label("●", "StatusDot")
        self.status_dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_dot.setFixedSize(42, 42)
        top.addWidget(self.status_dot)
        top_text = QVBoxLayout()
        top_text.setSpacing(3)
        self.global_status = label(tr("Monitoring paused"), "FieldTitle")
        top_text.addWidget(self.global_status)
        self.global_detail = label("", "Caption")
        self.global_detail.setWordWrap(True)
        top_text.addWidget(self.global_detail)
        badges = QHBoxLayout()
        badges.setSpacing(6)
        self.desktop_badge = label("", "Pill")
        self.ide_badge = label("", "Pill")
        self.work_badge = label("", "Pill")
        for badge in (self.desktop_badge, self.ide_badge, self.work_badge):
            badge.setProperty("tone", "muted")
            badges.addWidget(badge, 0, Qt.AlignmentFlag.AlignLeft)
        badges.addStretch()
        top_text.addLayout(badges)
        top.addLayout(top_text, 1)
        self.global_monitor = QPushButton(tr("Start"))
        self.global_monitor.setObjectName("MonitorButton")
        self.global_monitor.setMinimumWidth(110)
        self.global_monitor.clicked.connect(self._toggle_monitoring)
        top.addWidget(self.global_monitor)
        content_layout.addWidget(self.topbar)
        content_layout.addWidget(self.back_to_settings, 0, Qt.AlignmentFlag.AlignLeft)
        self.dashboard.mode_strip.hide()
        self.power_banner = QFrame()
        self.power_banner.setObjectName("PowerBanner")
        power_layout = QHBoxLayout(self.power_banner)
        power_layout.setContentsMargins(20, 10, 20, 10)
        self.power_message = label("", "Body")
        self.power_message.setWordWrap(True)
        cancel_power = QPushButton(tr("Cancel shutdown"))
        cancel_power.clicked.connect(self.power_view.controls.cancel)
        power_text = QVBoxLayout()
        power_text.setSpacing(3)
        power_text.addWidget(label(tr("Shutdown plan active"), "PowerTitle"))
        power_text.addWidget(self.power_message)
        power_layout.addLayout(power_text, 1)
        power_layout.addWidget(cancel_power)
        self.power_banner.hide()
        self.notice = QFrame()
        self.notice.setObjectName("NoticeBanner")
        notice_layout = QHBoxLayout(self.notice)
        notice_layout.setContentsMargins(20, 12, 20, 12)
        self.notice_text = label("", "Body")
        self.notice_text.setWordWrap(True)
        notice_layout.addWidget(self.notice_text, 1)
        details = QPushButton(tr("Diagnostics"))
        details.clicked.connect(lambda: self._open_tool(5))
        notice_layout.addWidget(details)
        self.desktop_download = QPushButton(tr("Official Desktop download"))
        self.desktop_download.clicked.connect(self._open_desktop_download)
        self.desktop_download.hide()
        notice_layout.addWidget(self.desktop_download)
        dismiss = QPushButton(tr("Close"))
        dismiss.clicked.connect(self._dismiss_notice)
        notice_layout.addWidget(dismiss)
        self.notice.hide()
        content_layout.addWidget(self.notice)
        content_layout.addWidget(self.stack, 1)
        content_layout.addWidget(self.power_banner)
        layout.addWidget(content, 1)

        self.setCentralWidget(root)
        self.toast = label("", "Toast")
        self.toast.setParent(self)
        self.toast.setWordWrap(True)
        self.toast.hide()
        self.toast_timer = QTimer(self)
        self.toast_timer.setSingleShot(True)
        self.toast_timer.timeout.connect(self.toast.hide)
        self.statusBar().showMessage(tr("Monitoring is paused. Start it when you are ready."))
        self.statusBar().hide()
        self.nav.setCurrentRow(0)
        self._build_tray()
        self.work_timer = QTimer(self)
        self.work_timer.setInterval(15000)
        self.work_timer.timeout.connect(self.conversations.refresh_tracking)
        self.work_timer.start()
        self._continuation_changed()
        QShortcut(QKeySequence("Ctrl+F"), self, self._focus_search)
        QShortcut(QKeySequence("Ctrl+R"), self, self._refresh_current)

    def _build_sidebar(self) -> QWidget:
        side = QWidget()
        side.setObjectName("Sidebar")
        side.setFixedWidth(248)
        layout = QVBoxLayout(side)
        self._sidebar_layout = layout
        layout.setContentsMargins(16, 24, 16, 20)
        layout.setSpacing(10)

        brand = QHBoxLayout()
        brand.setContentsMargins(0, 0, 0, 0)
        brand.setSpacing(12)
        logo = QLabel()
        logo.setPixmap(app_icon(56).pixmap(56, 56))
        self.brand_logo = logo
        name = label("QuotaCrew", "Brand", elide=True)
        self.brand_name = name
        brand.addWidget(logo)
        brand.addWidget(name, 1)
        brand.addStretch()
        layout.addLayout(brand)
        layout.addSpacing(40)

        self.nav = QListWidget()
        self.nav.setObjectName("Nav")
        self.nav.setIconSize(QSize(36, 24))
        for title, glyph in _NAV:
            item = QListWidgetItem(spaced_icon(glyph, DARK.muted, 24), tr(title))
            item.setSizeHint(QSize(0, 48))
            self.nav.addItem(item)
        for row in (4, 5, 6, 7, 8):
            self.nav.item(row).setHidden(True)
        self.nav.currentRowChanged.connect(self._on_nav)
        layout.addWidget(self.nav, 1)

        self.sidebar_actions = {}
        for text, index, glyph in (
            ("Settings", 6, "settings"),
            ("Shutdown", 8, "power"),
            ("Help", 7, "help"),
        ):
            button = QPushButton(tr(text))
            button.setIcon(spaced_icon(glyph, DARK.muted, 22))
            button.setIconSize(QSize(34, 22))
            button.setObjectName("SidebarAction")
            button.setCheckable(True)
            self.sidebar_actions[index] = (button, glyph)
            button.clicked.connect(lambda _checked=False, page=index: self.nav.setCurrentRow(page))
            layout.addWidget(button)
        self.power_badge = label(tr("Shutdown plan active"), "Pill")
        self.power_badge.setProperty("tone", "warning")
        self.power_badge.setWordWrap(True)
        self.power_badge.hide()
        layout.addWidget(self.power_badge)

        return side

    def _build_views(self) -> None:
        self.dashboard = DashboardView(self.runner, self.accounts, self._switch)
        self.dashboard.manage_requested.connect(lambda: self.nav.setCurrentRow(1))
        self.dashboard.monitor_requested.connect(self._toggle_monitoring)
        self.dashboard.work_requested.connect(lambda: self.nav.setCurrentRow(2))
        self.conversations = ConversationsView(self.runner)
        self.conversations.work_changed.connect(self._work_changed)
        self.conversations.tracking_reset.connect(self._tracking_reset)
        self.accounts_view = AccountsView(self.runner, self.accounts)
        self.activity = ActivityView(self.runner)
        self.power_view = PowerView(self.runner, self.accounts)
        self.goals = GoalsView(self.runner)
        self.diagnostics = DiagnosticsView(self.runner)
        self.settings = SettingsView(self.runner, self._apply_theme)
        from codex_account_manager.gui.updates import UpdatesPanel

        self.updates = UpdatesPanel(
            self.runner,
            lambda: not self._switching and not self._power_active and not self.settings.cli.busy,
        )
        self.settings.tabs.addTab(self.updates, tr("Updates"))
        self.updates.restart_requested.connect(self._quit)
        self.update_button = QPushButton(tr("Updates"))
        self.update_button.setObjectName("Primary")
        self.update_button.hide()
        sidebar_layout = self.sidebar.layout()
        assert sidebar_layout is not None
        sidebar_layout.addWidget(self.update_button)
        self.update_button.clicked.connect(self._show_updates)
        self.updates.available.connect(self._update_available)
        self.settings.policy_changed.connect(self._policy_changed)
        self.settings.continuation_changed.connect(self._continuation_changed)
        self.settings.desktop.availability_changed.connect(self._desktop_availability_changed)
        self.settings.ide.availability_changed.connect(self._ide_availability_changed)
        self.settings.interval.valueChanged.connect(
            lambda _value: self._policy_changed(self.settings.policy.currentData())
        )
        self.settings.monitoring_changed.connect(self._monitoring_changed)
        self.settings.background_changed.connect(self._background_changed)
        self.power_view.controls.status_changed.connect(self._power_status)
        self.power_view.controls.countdown_started.connect(self._countdown_started)
        self.conversations.shutdown_requested.connect(self.power_view.controls.arm_work)
        self.settings.diagnostics_requested.connect(lambda: self._open_tool(5))
        self.settings.notes_requested.connect(lambda: self._open_tool(4))
        self.conversations.notes_requested.connect(self._save_note)
        self.about = AboutView()
        from codex_account_manager.gui.onboarding import ProductTour

        tour = QPushButton(tr("Quick tour"))
        tour.clicked.connect(self.start_tour)
        self.about._root.insertWidget(1, tour)
        self._tour: ProductTour | None = None
        self._views = [
            self.dashboard,
            self.accounts_view,
            self.conversations,
            self.activity,
            self.goals,
            self.diagnostics,
            self.settings,
            self.about,
            self.power_view,
        ]

    def _toggle_monitoring(self) -> None:
        enabled = not self.settings.monitoring.isChecked()
        if (
            enabled
            and QMessageBox.question(
                self,
                tr("Start monitoring"),
                tr(
                    "Start periodic checks with the selected switching policy? Automatic mode can restart Codex Desktop and continue verified interrupted work."
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.settings.monitoring.setChecked(enabled)

    def _monitoring_changed(self, enabled: bool) -> None:
        self._monitor_message = tr("Monitoring active") if enabled else tr("Monitoring paused")
        target = self.power_view.controls.plan.target
        if not enabled and self._monitor_enabled and target is not None and target.mode != "timer":
            self.power_view.controls.cancel()
        self._monitor_enabled = enabled
        self._policy_changed(self.settings.policy.currentData())
        if hasattr(self, "tray"):
            self._update_tray_status()
            self.tray_monitor.setChecked(enabled)
            self.tray_monitor.setText(
                tr("Monitoring active") if enabled else tr("Monitoring paused")
            )

    def _background_changed(self, enabled: bool) -> None:
        self._keep_in_tray = enabled

    def _tracking_reset(self) -> None:
        with QSignalBlocker(self.settings.monitoring):
            self.settings.monitoring.setChecked(False)
        self._monitoring_changed(False)
        self.dashboard.refresh_work()

    def _work_changed(self, running: int, unverified: int) -> None:
        self._running_work = running
        self._unverified_work = unverified
        self._continuation_changed()

    def _continuation_changed(self) -> None:
        if not hasattr(self, "desktop_badge"):
            return
        enabled = self._monitor_enabled and self.settings.auto_continue.isChecked()
        for badge, name, checked, available in (
            (
                self.desktop_badge,
                "Desktop",
                self.settings.desktop_continue.isChecked(),
                getattr(self, "_desktop_available", None),
            ),
            (
                self.ide_badge,
                "IDE",
                self.settings.ide_continue.isChecked(),
                getattr(self, "_ide_available", None),
            ),
        ):
            badge.setText(
                tr(
                    "{surface}: {state}",
                    surface=name,
                    state=tr("Off")
                    if not checked
                    else tr("Needs setup")
                    if available is False
                    else tr("Check connection")
                    if available is None
                    else tr("Enabled")
                    if enabled
                    else tr("Paused"),
                )
            )
            badge.setVisible(True)
            badge.setProperty("tone", "primary" if enabled and checked and available else "muted")
            badge.style().unpolish(badge)
            badge.style().polish(badge)
        self.work_badge.setText(tr("{count} running", count=self._running_work))
        self.work_badge.setToolTip(
            tr("{count} saved observations could not be verified.", count=self._unverified_work)
        )
        if hasattr(self, "tray"):
            self._update_tray_status()

    def _desktop_availability_changed(self, available) -> None:
        self._desktop_available = available
        self._continuation_changed()

    def _ide_availability_changed(self, available) -> None:
        self._ide_available = available
        self._continuation_changed()

    def _open_desktop_download(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from codex_account_manager.platform.windows import CODEX_DESKTOP_DOWNLOAD_URL

        QDesktopServices.openUrl(QUrl(CODEX_DESKTOP_DOWNLOAD_URL))

    def _power_status(self, message: str, active: bool) -> None:
        self._power_active = active
        self._power_message = message
        self.power_message.setText(message)
        self.power_banner.setVisible(active)
        self.power_badge.setVisible(active)
        self.power_badge.setToolTip(message)
        if hasattr(self, "tray"):
            self._update_tray_status()

    def _update_tray_status(self) -> None:
        detail = self._power_message if self._power_active else self._monitor_message
        work = tr("{count} running", count=self._running_work)
        self.tray.setToolTip(
            (tr("QuotaCrew") + "\n" + detail + "\n" + work + " · " + self.ide_badge.text())[:127]
        )
        self.tray_auto.setChecked(self.settings.auto_continue.isChecked())
        self.tray_ide.setChecked(self.settings.ide_continue.isChecked())
        self.tray_auto.setText(
            tr("Auto continue")
            + " · "
            + tr(
                "Off"
                if not self.settings.auto_continue.isChecked()
                else "Enabled"
                if self._monitor_enabled
                else "Paused"
            )
        )
        self.tray_desktop.setChecked(self.settings.desktop_continue.isChecked())
        self.tray_desktop.setText(self.desktop_badge.text())
        self.tray_ide.setText(self.ide_badge.text())
        self.tray_work.setText(work)
        self.tray_power_status.setText(
            tr("Shutdown plan active") if self._power_active else tr("Shutdown is off.")
        )
        self.tray_power_detail.setText(detail)
        self.tray_power_detail.setVisible(self._power_active)
        self.tray_cancel_power.setVisible(self._power_active)
        self.tray_cancel_power.setEnabled(self._power_active)
        icon_state = (self._power_active, bool(self._running_work))
        if getattr(self, "_tray_icon_state", None) != icon_state:
            self.tray.setIcon(
                tray_icon(shutdown_pending=self._power_active, active_work=bool(self._running_work))
            )
            self._tray_icon_state = icon_state

    def _countdown_started(self) -> None:
        import math
        import time

        remaining = self.power_view.controls.plan.remaining(time.monotonic())
        minutes = max(1, math.ceil((remaining if remaining is not None else 120) / 60))
        self.tray.showMessage(
            tr("Shutdown countdown started"),
            tr(
                "Your computer will shut down in {minutes} minutes. You can cancel from the tray menu.",
                minutes=minutes,
            ),
            QSystemTrayIcon.MessageIcon.Warning,
            15000,
        )
        self._show()

    def closeEvent(self, event) -> None:
        if self._installation_in_progress():
            event.ignore()
            return
        if self._keep_in_tray and QSystemTrayIcon.isSystemTrayAvailable():
            self.hide()
            event.ignore()
            self.tray.showMessage(
                tr("QuotaCrew"),
                tr("Still running in the tray. Choose Quit to stop monitoring."),
            )
        else:
            self.power_view.controls.cancel()
            event.accept()

    def _focus_search(self) -> None:
        view = self._views[self.stack.currentIndex()]
        if search := getattr(view, "search", None):
            search.setFocus()
            search.selectAll()

    def _refresh_current(self) -> None:
        self._views[self.stack.currentIndex()].refresh()

    def _open_tool(self, index: int) -> None:
        with QSignalBlocker(self.nav):
            self.nav.setCurrentRow(6)
            self.stack.setCurrentIndex(index)
            self.back_to_settings.show()
        self._views[index].refresh()

    def _save_note(self, thread_id: str) -> None:
        self._open_tool(4)
        self.goals.save_for_thread(thread_id)

    def _policy_changed(self, mode: str) -> None:
        messages = {
            "manual": tr("Manual mode · You choose when to switch accounts."),
            "confirm": tr("Confirmation mode · We ask before changing a limited account."),
            "availability_failover": tr(
                "Automatic mode · A limited account is replaced when verified capacity is available."
            ),
        }
        message = messages.get(mode, messages["manual"])
        if not self._monitor_enabled:
            message = (
                tr("Monitoring paused") + " · " + tr("Selected policy: {policy}", policy=message)
            )
        titles = {
            "manual": "Manual switching",
            "confirm": "Ask before switching",
            "availability_failover": "Automatic switching",
        }
        short = tr(titles.get(mode, "Manual switching"))
        if not self._monitor_enabled:
            short = tr("Monitoring off")
        self.dashboard.set_automation(mode, self._monitor_enabled, self.settings.interval.value())
        if self._monitor_enabled and mode == "availability_failover":
            short = tr("Automatic account switching is on")
        self.dashboard.mode_label.setText(short)
        if hasattr(self, "global_status"):
            self.global_status.setText(short)
            self.global_status.setToolTip(self.dashboard.mode_detail.text())
            self.global_detail.setText(self.dashboard.mode_detail.text())
            self.status_dot.setProperty("active", "true" if self._monitor_enabled else "false")
            self.status_dot.style().unpolish(self.status_dot)
            self.status_dot.style().polish(self.status_dot)
            self.global_monitor.setText(tr("Pause") if self._monitor_enabled else tr("Start"))
            set_button_icon(
                self.global_monitor,
                "pause" if self._monitor_enabled else "play",
                DARK.text if self._dark else LIGHT.text,
                18,
            )
        self.dashboard.mode_label.setToolTip(message)
        self._continuation_changed()
        self.statusBar().showMessage(message)

    def _on_nav(self, row: int) -> None:
        if row < 0:
            return
        self.back_to_settings.setVisible(row in {4, 5})
        self.stack.setCurrentIndex(row)
        self._style_navigation(row)
        view = self._views[row]
        if hasattr(view, "refresh"):
            view.refresh()

    def _style_navigation(self, row: int) -> None:
        palette = DARK if self._dark else LIGHT
        for i, (_, glyph) in enumerate(_NAV):
            item = self.nav.item(i)
            if item is not None:
                color = palette.primary if i == row else palette.muted
                item.setIcon(spaced_icon(glyph, color, 24))
        for index, (button, glyph) in self.sidebar_actions.items():
            selected = index == row or (index == 6 and row in {4, 5})
            button.setChecked(selected)
            button.setIcon(spaced_icon(glyph, palette.primary if selected else palette.muted, 22))

    def _apply_theme(self, dark: bool) -> None:
        self._dark = dark
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.setPalette(application_palette(dark))
            theme_css = stylesheet(dark=dark)
            if app.styleSheet() != theme_css:
                app.setStyleSheet(theme_css)

        if sys.platform == "win32":
            try:
                import ctypes

                HWND = int(self.winId())

                # Use immersive dark mode (20 is the attribute for Windows 11)
                is_dark = ctypes.c_int(1 if dark else 0)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    HWND, 20, ctypes.byref(is_dark), ctypes.sizeof(is_dark)
                )

                # Caption color (35 is the attribute for Windows 11 titlebar color)
                # BGR format
                caption_color = ctypes.c_int(0x002B190D if dark else 0x00FCFAF8)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    HWND, 35, ctypes.byref(caption_color), ctypes.sizeof(caption_color)
                )

                # Mica backdrop (38 is the attribute for System Backdrop Type)
                mica = ctypes.c_int(1)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    HWND, 38, ctypes.byref(mica), ctypes.sizeof(mica)
                )
            except Exception as e:
                import logging

                logging.getLogger(__name__).warning("Could not apply DWM window attributes: %s", e)
        palette = DARK if dark else LIGHT
        from codex_account_manager.gui.widgets import UsageBar

        for bar in self.findChildren(UsageBar):
            bar.set_palette(palette)
        for view in self._views:
            view.palette_ = palette
        from PySide6.QtWidgets import QAbstractButton

        for button in self.findChildren(QAbstractButton):
            name = button.property("iconName")
            if name:
                color = palette.on_primary if button.objectName() == "Primary" else palette.text
                set_button_icon(button, name, color, button.property("iconPixelSize"))
        self.dashboard._filter()
        self.accounts_view._filter()
        self._policy_changed(self.settings.policy.currentData())
        self._style_navigation(self.stack.currentIndex())

    def _switch(self, alias: str, *, follow_latest: bool = False) -> None:
        if self._switching:
            return
        self._switching = True
        if (
            QMessageBox.question(
                self,
                tr("Switch account"),
                tr(
                    "Switch to '{alias}'? An installed Codex Desktop app will restart; Desktop is not required. Save your work first; conversation history is preserved.",
                    alias=alias,
                ),
            )
            != QMessageBox.StandardButton.Yes
        ):
            self._switching = False
            return
        self.dashboard.setEnabled(False)
        self.statusBar().showMessage(tr("Switching to {alias}…", alias=alias))

        async def _do():
            continuity = ContinuityService(accounts=self.accounts)
            if follow_latest:
                return await continuity.continue_on_limit(alias)
            return await continuity.handoff(alias)

        self.runner.submit(_do(), lambda _: self._switch_done(alias), self._switch_failed)

    def _switch_done(self, alias: str) -> None:
        self._switching = False
        self.dashboard.setEnabled(True)
        self.statusBar().showMessage(
            tr("Switched to {alias}. Shared history preserved.", alias=alias)
        )
        self.notice.hide()
        self.desktop_download.hide()
        self._show_toast(tr("Account switched successfully."))
        self.dashboard.refresh()

    def _switch_failed(self, exc: Exception) -> None:
        self._switching = False
        self.dashboard.setEnabled(True)
        self.statusBar().showMessage(tr("Switch failed. Check the error and Diagnostics."))
        self.notice_text.setText(tr("Switch failed") + ": " + tr(str(exc)))
        self.notice.show()
        from codex_account_manager.platform.windows import DESKTOP_MISSING_MESSAGE

        missing = str(exc) == DESKTOP_MISSING_MESSAGE
        self.desktop_download.setVisible(missing)
        if missing:
            self._desktop_availability_changed(False)
        # A transaction holds the shared operation lock. Refresh after its
        # release so a transient busy observation does not look like lost sign-in.
        self.dashboard.refresh()
        self.accounts_view.refresh()

    @Slot(str)
    def _operation_failed(self, message: str) -> None:
        self.statusBar().showMessage(message)
        self.notice_text.setText(message)
        self.notice.show()

    def _show_toast(self, message: str) -> None:
        self.toast.setText(message)
        self.toast.setFixedWidth(min(360, max(180, self.width() - 48)))
        self.toast.adjustSize()
        self._position_toast()
        self.toast.show()
        self.toast.raise_()
        self.toast_timer.start(4500)

    def _dismiss_notice(self) -> None:
        self._dismissed_continuation_issues.update(self._continuation_issues)
        self._dismissed_continuation_issues[""] = self.notice_text.text()
        self.notice.hide()

    def _expire_continuation_notice(self, thread_id: str, message: str) -> None:
        current = self._continuation_issues.get(thread_id) if thread_id else self.notice_text.text()
        if current != message:
            return
        self._dismissed_continuation_issues[thread_id] = message
        if not self._show_continuation_issues():
            self.notice.hide()

    def _show_continuation_issues(self) -> bool:
        visible = {
            key: message
            for key, message in self._continuation_issues.items()
            if self._dismissed_continuation_issues.get(key) != message
        }
        if not visible:
            return False
        self.notice_text.setText(
            next(iter(visible.values()))
            if len(visible) == 1
            else tr(
                "{count} conversations need attention. Other independent work can continue. See Jobs for details.",
                count=len(visible),
            )
        )
        self.notice.show()
        return True

    def _position_toast(self) -> None:
        bottom = (self.statusBar().height() if self.statusBar().isVisible() else 0) + 20
        if self.power_banner.isVisible():
            bottom += self.power_banner.height()
        self.toast.move(
            self.width() - self.toast.width() - 24, self.height() - self.toast.height() - bottom
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "sidebar"):
            compact = self.width() <= 1150
            self.sidebar.setFixedWidth(208 if compact else 248)
            self._sidebar_layout.setContentsMargins(
                8 if compact else 16, 24, 8 if compact else 16, 20
            )
            self.brand_logo.setPixmap(app_icon(40 if compact else 56).pixmap(40 if compact else 56))
            margin = 16 if compact else 28
            self._content_layout.setContentsMargins(margin, 18 if compact else 24, margin, 0)
        if hasattr(self, "toast"):
            self._position_toast()

    @Slot(object)
    def _domain_event(self, event) -> None:
        if event.topic == "monitor.status":
            self._monitoring_changed(event.payload["enabled"])
        elif event.topic in {
            "switch.started",
            "handoff.started",
            "switch.completed",
            "continuation.status",
        }:
            if self.power_view.controls.plan.target is not None:
                import time

                from codex_account_manager.monitoring.power import PowerEvidence

                self.power_view.controls.plan.observe(
                    PowerEvidence(False, "Work is active or could not be verified."),
                    time.monotonic(),
                )
        if event.topic == "health.updated":
            self.dashboard._render(event.payload["health"])
        elif event.topic == "switch.suggested":
            self._switch(event.payload["target"], follow_latest=True)
        elif event.topic == "switch.failed" and "alias" not in event.payload:
            self._switch_failed(RuntimeError(event.payload["detail"]))
        elif event.topic == "switch.completed":
            self.dashboard.refresh()
            if self._shared_waits:
                for thread_id in self._shared_waits:
                    self._continuation_issues.pop(thread_id, None)
                self._shared_waits.clear()
                if not self._show_continuation_issues():
                    self.notice.hide()
        elif event.topic == "switch.deferred":
            message = tr(event.payload["detail"])
            self.statusBar().showMessage(message)
            self.notice_text.setText(message)
            self.notice.show()
        elif event.topic == "work.observed":
            self.conversations.refresh_tracking()
            if self.stack.currentIndex() == 0:
                self.dashboard.refresh_work()
        elif event.topic == "work.observation_status":
            self.conversations.show_tracking_status(event.payload)
            if event.payload.get("state") != "checking":
                self.conversations.refresh_tracking()
        elif event.topic == "continuation.status":
            if event.payload.get("state") == "desktop_required" and event.payload.get("thread_id"):
                self.conversations.open_desktop(event.payload["thread_id"])
            messages = {
                "waiting_shared": "Account switching is waiting because this conversation shares the Desktop process that must restart. Other independent work can continue.",
                "refreshing_ide": "Refreshing VS Code",
                "waiting_connection": "Waiting for the conversation connection. Nothing has been sent. Checks will retry for up to 10 minutes while monitoring remains enabled.",
                "ide_submitted": "Continuation sent to the existing Codex owner. Work stays in the IDE or Desktop with its tools.",
                "desktop_submitted": "Continuation sent to Codex Desktop. The conversation stays in Desktop with its tools.",
                "desktop_required": "Account switched. Continue the conversation in Codex Desktop to keep its browser and app tools.",
                "running": "Continuing interrupted work automatically. Disable continuation in Settings to stop.",
                "completed": "Automatic continuation finished. Review the result in Codex.",
                "stopped": "Automatic continuation stopped. Your pauses and goal limits are preserved.",
                "needs_user": "Automatic continuation needs attention. Open the conversation in Codex; approval, input or an error may require your action.",
                "skipped": "Automatic continuation was skipped. See the work details.",
            }
            message = tr(messages.get(event.payload["state"], messages["needs_user"]))
            transient_issue = event.payload.get("state") == "needs_user" and event.payload.get(
                "stage"
            ) in {
                "verification",
                "connection",
                "recovery",
                "owner_busy",
                "checkpoint",
                "preparation",
                "journal",
            }
            if transient_issue:
                message = tr(
                    "Automatic continuation could not be verified. This does not mean your account quota is exhausted. See Jobs for details."
                )
            if event.payload.get("stage"):
                message += " " + continuation_detail(event.payload)
            self.statusBar().showMessage(message)
            thread_id = event.payload.get("thread_id")
            needs_attention = event.payload.get("state") in {
                "needs_user",
                "skipped",
                "waiting_connection",
                "waiting_shared",
            }
            if thread_id:
                if event.payload.get("state") == "waiting_shared":
                    self._shared_waits.add(thread_id)
                else:
                    self._shared_waits.discard(thread_id)
                if needs_attention:
                    self._continuation_issues[thread_id] = message
                else:
                    self._continuation_issues.pop(thread_id, None)
                    self._dismissed_continuation_issues.pop(thread_id, None)
            elif not needs_attention:
                self._dismissed_continuation_issues.pop("", None)
            if (
                transient_issue
                and self._dismissed_continuation_issues.get(thread_id or "") != message
            ):
                QTimer.singleShot(
                    30000,
                    self,
                    lambda key=thread_id or "", text=message: self._expire_continuation_notice(
                        key, text
                    ),
                )
            if self._show_continuation_issues():
                return
            if needs_attention:
                if self._dismissed_continuation_issues.get(thread_id or "") == message:
                    return
                self.notice_text.setText(message)
                self.notice.show()
            else:
                if event.payload.get("state") in {
                    "running",
                    "refreshing_ide",
                    "ide_submitted",
                    "desktop_submitted",
                    "completed",
                    "stopped",
                }:
                    self.notice.hide()
                self._show_toast(message)

    def dispose(self) -> None:
        self.updates.stop()
        if hasattr(self, "work_timer"):
            self.work_timer.stop()
        self.power_view.controls.cancel()
        self._unsubscribe()
        self.tray.hide()

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip(tr("QuotaCrew"))
        menu = QMenu(self)
        self.tray_work = menu.addAction(tr("{count} running", count=0))
        self.tray_work.triggered.connect(self._show_work)
        self.tray_power_status = menu.addAction(tr("Shutdown is off."))
        self.tray_power_status.setEnabled(False)
        self.tray_power_detail = menu.addAction("")
        self.tray_power_detail.setVisible(False)

        self.tray_cancel_power = menu.addAction(tr("Cancel shutdown"))
        self.tray_cancel_power.triggered.connect(self.power_view.controls.cancel)
        self.tray_cancel_power.setVisible(False)
        menu.addSeparator()

        self.tray_monitor = menu.addAction(tr("Monitoring paused"))
        self.tray_monitor.setCheckable(True)
        self.tray_monitor.triggered.connect(self._toggle_monitoring)

        self.tray_auto = menu.addAction(tr("Auto continue"))
        self.tray_auto.setCheckable(True)
        self.tray_auto.triggered.connect(
            lambda checked: self.settings.auto_continue.setChecked(checked)
        )

        self.tray_ide = menu.addAction(tr("IDE continuation"))
        self.tray_ide.setCheckable(True)
        self.tray_ide.triggered.connect(
            lambda checked: self.settings.ide_continue.setChecked(checked)
        )

        self.tray_desktop = menu.addAction(tr("Desktop continuation"))
        self.tray_desktop.setCheckable(True)
        self.tray_desktop.triggered.connect(
            lambda checked: self.settings.desktop_continue.setChecked(checked)
        )

        menu.addSeparator()
        for title, callback in (
            (tr("Open QuotaCrew"), self._show),
            (tr("Refresh usage"), self.dashboard.refresh),
            (tr("Quit"), self._quit),
        ):
            action = QAction(title, self)
            action.triggered.connect(callback)
            menu.addAction(action)
        self.tray.setContextMenu(menu)
        self._update_tray_status()
        self.tray.activated.connect(self._on_tray_activated)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self._show()

    def _show(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _show_work(self) -> None:
        self.nav.setCurrentRow(2)
        self._show()

    def _quit(self) -> None:
        from PySide6.QtWidgets import QApplication

        if self._installation_in_progress():
            return
        self.power_view.controls.cancel()
        self.tray.hide()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _installation_in_progress(self) -> bool:
        if self.settings.cli.installing:
            QMessageBox.information(
                self,
                tr("Install Codex CLI"),
                tr("Installing Codex CLI… This may take a few minutes."),
            )
            return True
        return self.updates.preparing

    def _show_updates(self) -> None:
        self.nav.setCurrentRow(6)
        self.settings.tabs.setCurrentWidget(self.updates)
        self._show()

    def start_tour(self) -> None:
        from codex_account_manager.gui.onboarding import ProductTour

        if self._tour is not None:
            self._tour.raise_()
            return
        self._tour = ProductTour(self)
        self._tour.finished.connect(lambda _: setattr(self, "_tour", None))
        self._tour.show()

    def _update_available(self, version: str) -> None:
        self.update_button.setVisible(bool(version))
        if version:
            self.update_button.setText(tr("Update available · {version}", version=version))
