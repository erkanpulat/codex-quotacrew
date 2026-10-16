"""About screen."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from codex_account_manager.core.privacy import policy_path
from codex_account_manager.gui.design import DARK
from codex_account_manager.gui.i18n import (
    tr,
)
from codex_account_manager.gui.view_base import BaseView, view_header
from codex_account_manager.gui.widgets import label
from codex_account_manager.updates import REPOSITORY


class AboutView(BaseView):
    def __init__(self, palette=DARK):
        super().__init__(palette)
        self._root.addWidget(
            view_header(
                tr("About this application"),
                tr("QuotaCrew for Codex · Independent, open-source desktop utility"),
            )
        )
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content.setObjectName("GridHost")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)
        support = QFrame()
        support.setObjectName("Card")
        buttons = QGridLayout(support)
        buttons.setContentsMargins(22, 20, 22, 20)
        buttons.setSpacing(10)
        buttons.addWidget(label(tr("Support the project"), "H2"), 0, 0, 1, 2)
        for index, (title, url) in enumerate(
            (
                ("Star on GitHub", f"https://github.com/{REPOSITORY}"),
                ("Report an issue", f"https://github.com/{REPOSITORY}/issues"),
                ("Contribute a pull request", f"https://github.com/{REPOSITORY}/pulls"),
                ("Developer profile", "https://github.com/erkanpulat"),
            )
        ):
            button = QPushButton(tr(title))
            button.clicked.connect(
                lambda _checked=False, url=url: QDesktopServices.openUrl(QUrl(url))
            )
            buttons.addWidget(button, 1 + index // 2, index % 2)
        layout.addWidget(support)
        privacy = QPushButton(tr("Privacy policy"))
        privacy.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(policy_path())))
        )
        layout.addWidget(privacy)
        licenses = QPushButton(tr("Open-source licenses (Qt / PySide6 · LGPL-3.0)"))
        notices = (
            Path(vars(sys)["_MEIPASS"]) / "licenses/THIRD_PARTY_NOTICES.md"
            if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parents[3] / "THIRD_PARTY_NOTICES.md"
        )
        licenses.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(notices))))
        layout.addWidget(licenses)
        for title, description in (
            (
                tr("What does it do?"),
                tr(
                    "Manage multiple Codex accounts on one device, monitor their usage quotas, and switch the account used by Codex Desktop. Browse local conversations by project and choose automatic, confirmation or manual switching."
                ),
            ),
            (
                tr("Desktop, CLI and IDE support"),
                tr(
                    "This is a desktop application; Codex CLI provides its connection. Desktop continuation is experimental. Optional IDE continuation uses the existing local Codex conversation owner and preserves its tools; live account-switch continuation in an IDE has not yet been verified. A running IDE may need reloading after a switch. Remote and WSL environments may use separate login storage."
                ),
            ),
            (
                tr("Does it work automatically?"),
                tr(
                    "Checks are on by default for new installations. Choose automatic, confirmation or manual switching in Settings; pause or resume checks from Overview. Checks run every 60 seconds by default; the interval is adjustable from 30 to 3600 seconds. Closing the window quits unless you explicitly enable running in the tray. The sidebar always shows monitoring status."
                ),
            ),
            (
                tr("What happens during a switch?"),
                tr(
                    "The target account is verified and the current login is saved for recovery. Desktop restarts if installed; CLI and IDE switching work without it. Conversation files stay in the shared Codex folder. Save your work first. Failed switches attempt to restore the previous login; recovery failures are reported."
                ),
            ),
            (
                tr("Will my conversation continue by itself?"),
                tr(
                    "With monitoring and continuation enabled, a verified usage-limit interruption can continue after an account switch. The exact turn and goal are checked again before input is sent. Unknown state, approval requests and user pauses stop automation. Merely opening a conversation or saving a local goal note does not start work."
                ),
            ),
            (
                tr("Automatic shutdown"),
                tr(
                    "Shutdown is off by default and must be enabled for each session. Choose all accounts limited or a selected conversation completed. Set a countdown from 1 to 1440 minutes (120 = 2 hours, 180 = 3 hours); cancel it from the window or tray. Closing the app cancels the plan. Other applications are not forcibly closed; save their work yourself."
                ),
            ),
            (
                tr("Where is my information stored?"),
                tr(
                    "Account credentials and settings are stored locally. This application has no telemetry or credential proxy. Codex connects to OpenAI for sign-in and account information. Diagnostic exports include check statuses, the CLI version and profile count. They exclude account labels, local paths, credentials and conversation content."
                ),
            ),
            (
                tr("Uninstalling and removing local data"),
                tr(
                    "Uninstalling QuotaCrew keeps saved accounts, settings and backups on this PC. A separately installed Codex CLI and its PATH entry also remain. See Help → Privacy policy for removal instructions."
                ),
            ),
            (
                tr("Independent project"),
                tr(
                    "This project is not an official OpenAI product and is not affiliated with OpenAI. It does not increase account limits or provide extra capacity. Each account remains subject to its own plan and service terms."
                ),
            ),
        ):
            panel = QFrame()
            panel.setObjectName("Card")
            box = QVBoxLayout(panel)
            box.setContentsMargins(22, 20, 22, 20)
            box.setSpacing(10)
            box.addWidget(label(title, "H2"))
            body = label(description, "Body")
            body.setWordWrap(True)
            box.addWidget(body)
            layout.addWidget(panel)
        layout.addStretch()
        scroll.setWidget(content)
        self._root.addWidget(scroll, 1)
