"""GUI entry point."""

from __future__ import annotations

import asyncio
import os
import sys

from codex_account_manager.core.errors import AccountRecoveryRequired, TransactionError
from codex_account_manager.core.logging import configure_logging, get_logger
from codex_account_manager.core.protection import CredentialProtectionError
from codex_account_manager.core.windows_shell import set_application_identity
from codex_account_manager.storage.database import initialize_database

log = get_logger(__name__)


def run_gui() -> int:
    try:
        configure_logging()
        log.info("QuotaCrew started (pid=%d).", os.getpid())
        asyncio.run(initialize_database())
    except AccountRecoveryRequired:
        from PySide6.QtCore import QLocale
        from PySide6.QtWidgets import QApplication, QMessageBox

        from codex_account_manager.gui.i18n import set_language, tr

        app = QApplication.instance() or QApplication(sys.argv)
        set_language("tr" if QLocale.system().name().startswith("tr") else "en")
        QMessageBox.critical(
            None,
            tr("Account recovery required"),
            tr(
                "Saved sign-in files exist, but the account list is missing. Startup was stopped to protect your data. Do not reset or delete the data folder. Restore the account database from a verified backup."
            ),
        )
        return 1
    except OSError:
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(
            None,
            "QuotaCrew",
            "Local data could not be accessed or protected. Close other account operations and check the data folder permissions. Do not delete your account data.\n\n"
            "Yerel verilere erişilemedi veya veriler korunamadı. Diğer hesap işlemlerini kapatıp veri klasörünün izinlerini kontrol edin. Hesap verilerinizi silmeyin.",
        )
        return 1
    from codex_account_manager.auth.transaction import AuthTransaction

    try:
        AuthTransaction().recover()
    except (CredentialProtectionError, TransactionError, OSError, ValueError):
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(
            None,
            "QuotaCrew",
            "Stored sign-in data could not be recovered safely. Your files were retained. "
            "Use the original Windows user account and close other account operations before retrying.\n\n"
            "Kaydedilmiş giriş bilgileri güvenli biçimde kurtarılamadı. Dosyalarınız korundu. "
            "Özgün Windows hesabını kullanın ve yeniden denemeden önce diğer hesap işlemlerini kapatın.",
        )
        return 1

    from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
    from PySide6.QtGui import QFontDatabase
    from PySide6.QtWidgets import QApplication

    from codex_account_manager.gui.i18n import set_language, tr
    from codex_account_manager.storage.repositories import SettingsRepository

    default_language = "tr" if QLocale.system().name().startswith("tr") else "en"
    selected_language = asyncio.run(SettingsRepository().get("language", default_language))
    selected_theme = asyncio.run(SettingsRepository().get("theme", "dark"))
    set_language(selected_language or default_language)

    from codex_account_manager.gui.async_runner import AsyncRunner
    from codex_account_manager.gui.design import app_icon
    from codex_account_manager.gui.main_window import MainWindow
    from codex_account_manager.gui.theme import stylesheet

    set_application_identity()
    existing = QApplication.instance()
    app = existing if isinstance(existing, QApplication) else QApplication(sys.argv)
    app.setStyle("Fusion")
    if sys.platform == "win32":
        from pathlib import Path

        font_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for filename in ("segoeui.ttf", "segoeuib.ttf", "segoeuisl.ttf"):
            font_path = font_dir / filename
            if font_path.is_file():
                QFontDatabase.addApplicationFont(str(font_path))
    app.setApplicationName("QuotaCrew")
    app.setApplicationDisplayName(tr("QuotaCrew"))
    translator = QTranslator(app)
    if selected_language == "tr" and translator.load(
        "qtbase_tr", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    ):
        app.installTranslator(translator)
    app.setWindowIcon(app_icon())
    app.setStyleSheet(stylesheet(dark=selected_theme == "dark"))
    app.setQuitOnLastWindowClosed(True)
    app.aboutToQuit.connect(lambda: log.info("QuotaCrew exit requested (pid=%d).", os.getpid()))

    runner = AsyncRunner()
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QDialog, QMessageBox

    from codex_account_manager.gui.onboarding import SetupDialog, needs_setup

    show_tour = False
    if asyncio.run(needs_setup()):
        app.setQuitOnLastWindowClosed(False)
        setup = SetupDialog(runner)
        if setup.exec() != QDialog.DialogCode.Accepted:
            runner.shutdown()
            return 0
        try:
            asyncio.run(SettingsRepository().set_many(setup.values()))
        except Exception:
            QMessageBox.critical(
                None,
                tr("Setup"),
                tr(
                    "Your preferences could not be saved. Please reopen the application to try again."
                ),
            )
            runner.shutdown()
            return 1
        show_tour = setup.tour.isChecked()
        setup.deleteLater()
    window = MainWindow(runner, dark=selected_theme == "dark")
    window.show()
    app.setQuitOnLastWindowClosed(True)
    window.updates.start()
    if show_tour:
        QTimer.singleShot(250, window.start_tour)
    from codex_account_manager.monitoring.watcher import Watcher

    watcher = Watcher(accounts=window.accounts)
    runner.submit(watcher.run())

    try:
        return app.exec()
    finally:
        window.dispose()
        runner.shutdown()
        log.info("QuotaCrew stopped (pid=%d).", os.getpid())


if __name__ == "__main__":
    raise SystemExit(run_gui())
