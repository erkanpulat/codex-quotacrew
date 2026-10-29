"""Render the real Qt interface using synthetic profiles and no external services."""

from __future__ import annotations

import os
import time
from html import escape
from tempfile import TemporaryDirectory
from xml.etree import ElementTree

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import QObject, QSignalBlocker, QTimer, Signal
from PySide6.QtWidgets import QApplication, QDialog

from codex_account_manager.continuity.tracking import ObservedWork
from codex_account_manager.domain.identity import AccountIdentity, IdentityState
from codex_account_manager.domain.models import (
    ProfileHealth,
    ResetCredit,
    ResetCredits,
    ThreadRecord,
)
from codex_account_manager.domain.states import QuotaState
from codex_account_manager.gui.i18n import set_language, tr
from codex_account_manager.gui.main_window import MainWindow
from codex_account_manager.gui.theme import stylesheet


class PreviewRunner(QObject):
    failed = Signal(str)

    def submit(self, coro, on_result=None, on_error=None):
        coro.close()


def sample_profiles() -> list[ProfileHealth]:
    return [
        ProfileHealth(
            alias=name,
            profile_id=name,
            identity=AccountIdentity(
                user_id=f"preview-{name.lower().replace(' ', '-')}",
                workspace_id="preview-workspace",
                state=IdentityState.COMPLETE,
            ),
            email=name.lower().replace(" ", ".") + "@example.com",
            plan_type=plan,
            primary_used_percent=primary,
            secondary_used_percent=secondary,
            primary_window_minutes=300,
            secondary_window_minutes=10080,
            primary_resets_at=1790856000,
            secondary_resets_at=1791028800,
            ordinary_usage_allowed=primary < 100,
            auth_present=True,
            account_match=True,
            is_active=active,
            quota_state=QuotaState.AVAILABLE if primary < 100 else QuotaState.LIMITED_WITH_RESET,
            last_checked_at=datetime.now(UTC),
            reset_credits=ResetCredits(
                available_count=1 if active else 0,
                credits=(
                    ResetCredit(
                        status="available",
                        reset_type="codexRateLimits",
                        granted_at=1790553600,
                        expires_at=1791158400,
                    ),
                )
                if active
                else (),
            )
            if name != "Open source"
            else None,
        )
        for name, plan, primary, secondary, active in (
            ("Personal", "plus", 28, 46, True),
            ("Studio", "pro", 12, 24, False),
            ("Client workspace", "business", 100, 78, False),
            ("Open source", "plus", 64, 32, False),
        )
    ]


def sample_threads() -> list[ThreadRecord]:
    return [
        ThreadRecord(
            id=f"example-{index}",
            title=title,
            cwd=folder,
            workspace=folder,
            source=source,
            project_id=project,
            preview="Example conversation for the public preview. All data is synthetic.",
            updated_at=datetime(2026, 9, 26, 9, 30 - index, tzinfo=UTC),
        )
        for index, (title, folder, source, project) in enumerate(
            (
                (
                    "Hesap ayarlarını sadeleştir",
                    "C:/Work/codex-quotacrew",
                    "vscode",
                    "manager",
                ),
                (
                    "Proje aramasını geliştir",
                    "C:/Work/customer-portal/packages/web/application",
                    "vscode",
                    "portal",
                ),
                (
                    "Komut satırı testlerini çalıştır",
                    "C:/Work/codex-quotacrew",
                    "cli",
                    "manager",
                ),
                (
                    "Giriş ekranının erişilebilirliğini incele",
                    "C:/Work/customer-portal/packages/web/application",
                    "subAgent",
                    "portal",
                ),
                ("Yayın belgelerini tamamla", "C:/Work/codex-quotacrew", "vscode", "manager"),
                (
                    "Veri tabanı yükseltmesini doğrula",
                    "C:/Work/customer-portal/services/api",
                    "appServer",
                    "portal",
                ),
            )
        )
    ]


def _show_monitoring_preview(window: MainWindow) -> None:
    with QSignalBlocker(window.settings.monitoring):
        window.settings.monitoring.setChecked(True)
    window._monitoring_changed(True)
    window.dashboard.sort_order.setCurrentIndex(window.dashboard.sort_order.findData("weekly_most"))
    window._work_changed(0, 0)


def render_installation_cards(output: Path) -> None:
    ElementTree.register_namespace("", "http://www.w3.org/2000/svg")
    logo = ElementTree.parse(output.parents[1] / "packaging/assets/app.svg").getroot()
    mark = (
        '<g transform="translate(48 36) scale(0.84375)">'
        + "".join(
            ElementTree.tostring(child, encoding="unicode")
            for child in logo
            if child.tag == "{http://www.w3.org/2000/svg}g"
        )
        + "</g>"
    )
    content = {
        "en": (
            "Download. Set up. Add an account.",
            "Python is included. If Codex CLI is missing, install it with your approval.",
            (
                ("Download for Windows", "Installer or portable ZIP"),
                ("Choose your preferences", "CLI check and optional tour"),
                ("Add your account", "Sign in and track your quota"),
            ),
            "Click this card to open Windows downloads",
            "Download for Windows",
        ),
        "tr": (
            "İndir. Kur. Hesabını ekle.",
            "Python pakete dahil. Codex CLI eksikse onayınızla kurulur.",
            (
                ("Windows paketini indir", "Kurulum veya taşınabilir ZIP"),
                ("Tercihlerini seç", "CLI kontrolü ve kısa tanıtım"),
                ("Hesabını ekle", "Giriş yap ve kotanı takip et"),
            ),
            "İndirme sayfasına gitmek için görsele tıklayın",
            "Windows için indir",
        ),
    }
    for language, (title, subtitle, steps, hint, action) in content.items():
        cards = []
        for index, (step, detail) in enumerate(steps):
            x = 48 + index * 376
            cards.append(
                f'<rect x="{x}" y="202" width="352" height="122" rx="14" fill="#142235" stroke="#2b3d53"/>'
                f'<text x="{x + 22}" y="237" fill="#5ee5be" font-size="13" letter-spacing="2">0{index + 1}</text>'
                f'<text x="{x + 22}" y="268" font-size="20" font-weight="600">{escape(step)}</text>'
                f'<text x="{x + 22}" y="297" fill="#a9b8cc" font-size="16">{escape(detail)}</text>'
            )
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="420" viewBox="0 0 1200 420" role="img" aria-labelledby="title desc">'
            f'<title id="title">{escape(title)}</title><desc id="desc">{escape(subtitle + " " + hint)}</desc>'
            '<rect width="1200" height="420" rx="24" fill="#0d1726"/>'
            + mark
            + '<g font-family="Segoe UI,Arial,sans-serif" fill="#f5f8fc">'
            '<text x="120" y="70" font-size="21" font-weight="600">QuotaCrew for Codex</text>'
            '<text x="1152" y="68" font-size="13" fill="#a9b8cc" text-anchor="end" letter-spacing="2">WINDOWS</text>'
            f'<text x="48" y="138" font-size="40" font-weight="600">{escape(title)}</text>'
            f'<text x="48" y="173" font-size="20" fill="#a9b8cc">{escape(subtitle)}</text>'
            + "".join(cards)
            + f'<text x="48" y="379" font-size="16" fill="#a9b8cc">{escape(hint)}</text>'
            '<rect x="864" y="353" width="288" height="44" rx="22" fill="#5ee5be"/>'
            f'<text x="1008" y="381" text-anchor="middle" font-size="16" font-weight="600" fill="#0d1726">{escape(action)} →</text>'
            "</g></svg>\n"
        )
        (output / f"installation-{language}.svg").write_text(svg, encoding="utf-8")


def capture_setup(window: MainWindow, output: Path, language: str) -> None:
    from codex_account_manager.gui.onboarding import SetupDialog

    app = QApplication.instance()
    window._apply_theme(True)
    window.resize(1280, 850)
    window.nav.setCurrentRow(6)
    window.settings.tabs.setCurrentWidget(window.updates)
    window.updates.status.setText(tr("No newer stable version is available."))
    app.processEvents()
    window.grab().save(str(output / f"updates-{language}.png"))
    setup = SetupDialog(PreviewRunner())
    setup.cli._checked(None)
    setup.show()
    app.processEvents()
    setup.grab().save(str(output / f"setup-cli-{language}.png"))
    setup.cli._checked("0.143.0")
    setup._advance()
    app.processEvents()
    setup.grab().save(str(output / f"setup-preferences-{language}.png"))
    setup.close()
    window.nav.setCurrentRow(0)


def render() -> None:
    set_language("en")
    app = QApplication.instance() or QApplication([])
    from PySide6.QtCore import QRect, QSize
    from PySide6.QtGui import QPainter
    from PySide6.QtSvg import QSvgGenerator

    from codex_account_manager.gui.design import app_icon, paint_brand

    assets = Path(__file__).resolve().parents[1] / "packaging/assets"
    app_icon(512).pixmap(512, 512).save(str(assets / "app.png"))
    generator = QSvgGenerator()
    generator.setFileName(str(assets / "app.svg"))
    generator.setSize(QSize(64, 64))
    generator.setViewBox(QRect(0, 0, 64, 64))
    generator.setTitle("QuotaCrew")
    generator.setDescription("Three accounts on a quota orbit, with an arrow continuing forward.")
    painter = QPainter(generator)
    paint_brand(painter)
    painter.end()
    logo = assets / "app.svg"
    logo.write_text(
        "\n".join(line.rstrip() for line in logo.read_text(encoding="utf-8").splitlines()) + "\n",
        encoding="utf-8",
    )
    import struct

    from PySide6.QtCore import QBuffer, QByteArray, QIODevice

    images = []
    for size in (16, 24, 32, 48, 64, 128, 256):
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        app_icon(size).pixmap(size, size).save(buffer, "PNG")
        images.append((size, bytes(data)))
    offset = 6 + 16 * len(images)
    entries = []
    for size, data in images:
        entries.append(
            struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        )
        offset += len(data)
    (assets / "app.ico").write_bytes(
        struct.pack("<HHH", 0, 1, len(images))
        + b"".join(entries)
        + b"".join(data for _, data in images)
    )
    from PySide6.QtGui import QFontDatabase

    font_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for font in ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf"):
        if (font_dir / font).exists():
            QFontDatabase.addApplicationFont(str(font_dir / font))
    app.setStyle("Fusion")
    app.setStyleSheet(stylesheet())
    window = MainWindow(PreviewRunner())
    window.tray.hide()
    window.dashboard._render(sample_profiles())
    window.resize(1480, 960)
    window.show()
    output = Path(__file__).resolve().parents[1] / "docs" / "images"
    output.mkdir(parents=True, exist_ok=True)
    render_installation_cards(output)
    review = Path(__file__).resolve().parents[1] / ".ruff_cache" / "previews"
    review.mkdir(parents=True, exist_ok=True)
    for dark, name in ((True, "overview-dark"), (False, "overview-light")):
        window._apply_theme(dark)
        window.dashboard._render(sample_profiles())
        window.dashboard._render_work(([], None))
        with QSignalBlocker(window.settings.theme):
            window.settings.theme.setCurrentIndex(0 if dark else 1)
        _show_monitoring_preview(window)
        app.processEvents()
        app.processEvents()
        window.grab().save(str(output / f"{name}.png"))
    window.nav.setCurrentRow(6)
    window.settings.tabs.setCurrentIndex(1)
    window.settings._startup_loaded(False)
    window.resize(1040, 700)
    with QSignalBlocker(window.settings.ide_continue):
        window.settings.ide_continue.setChecked(True)
    window._continuation_changed()
    app.processEvents()
    app.processEvents()
    window.grab().save(str(output / "continuation-en.png"))
    window.nav.setCurrentRow(0)
    window.power_view.controls.cancel()
    window._monitoring_changed(False)
    window.dashboard._render([])
    window.dashboard._render_work(([], None))
    app.processEvents()
    window.grab().save(str(review / "onboarding.png"))
    capture_setup(window, output, "en")
    window.dispose()
    window.close()
    set_language("tr")
    window = MainWindow(PreviewRunner())
    window.tray.hide()
    window.resize(1480, 960)
    window.show()
    capture_setup(window, output, "tr")
    window.resize(1480, 960)
    window._apply_theme(False)
    with QSignalBlocker(window.settings.theme):
        window.settings.theme.setCurrentIndex(1)
    window._monitoring_changed(False)
    window.dashboard._render([])
    window.dashboard._render_work(([], None))
    app.processEvents()
    window.grab().save(str(review / "onboarding-tr.png"))
    window.dashboard._render(sample_profiles())
    _show_monitoring_preview(window)
    app.processEvents()
    app.processEvents()
    window.grab().save(str(output / "overview-tr.png"))
    window.power_view.controls.cancel()
    from codex_account_manager.gui.widgets import AccountRow

    def capture_credit_dialog():
        for widget in app.topLevelWidgets():
            if isinstance(widget, QDialog) and widget.isVisible():
                widget.grab().save(str(output / "reset-credits-tr.png"))
                widget.accept()

    card = AccountRow(sample_profiles()[0])
    QTimer.singleShot(100, capture_credit_dialog)
    card._show_reset_credits(sample_profiles()[0])
    card.deleteLater()
    window.nav.setCurrentRow(2)
    window.conversations._render_threads(sample_threads())
    source_account = AccountIdentity(
        user_id="preview-user",
        workspace_id="preview-personal-account",
        state=IdentityState.COMPLETE,
    ).fingerprint
    window.conversations._render_tracking(
        (
            [
                ObservedWork(
                    thread_id="example-1",
                    account_hash=source_account,
                    turn_status="inProgress",
                    goal_status="active",
                    goal_present=True,
                    limited=False,
                    observed_at=time.time(),
                ),
                ObservedWork(
                    thread_id="example-2",
                    account_hash=source_account,
                    turn_status="completed",
                    goal_status="complete",
                    goal_present=True,
                    limited=False,
                    observed_at=time.time(),
                ),
                ObservedWork(
                    thread_id="example-3",
                    account_hash=source_account,
                    turn_status="failed",
                    goal_status="active",
                    goal_present=True,
                    limited=True,
                    observed_at=time.time(),
                ),
            ],
            {source_account: "Personal"},
        )
    )
    window.conversations.status.setText(
        tr(
            "{shown} of {total} local conversations",
            shown=5,
            total=6,
        )
    )
    window.conversations.show_tracking_status(
        {"state": "ready", "checked": 5, "checked_at": "12:30:00"}
    )
    window.conversations.tracked_table.selectRow(0)
    window.conversations.table.selectRow(1)
    app.processEvents()
    window.grab().save(str(output / "jobs-light-tr.png"))
    window._apply_theme(True)
    app.processEvents()
    window.grab().save(str(output / "jobs-dark-tr.png"))
    window.resize(1040, 720)
    app.processEvents()
    window.grab().save(str(review / "jobs-compact.png"))
    window.resize(1480, 960)
    window._apply_theme(False)
    window.conversations.tabs.setCurrentIndex(1)
    app.processEvents()
    window.grab().save(str(output / "conversations-tr.png"))
    from codex_account_manager.gui.dialogs import EntryDialog

    dialog = EntryDialog(
        window,
        tr("Add profile"),
        tr("Account name"),
        tr("Choose a name you will recognize, such as Personal or Work."),
        maximum=64,
        action="Add account",
    )
    dialog.show()
    app.processEvents()
    dialog.grab().save(str(output / "add-account-tr.png"))
    dialog.close()
    window.accounts_view._render(sample_profiles())
    window.settings._startup_loaded(False)
    with QSignalBlocker(window.settings.ide_continue):
        window.settings.ide_continue.setChecked(True)
    window._continuation_changed()
    for row, name in (
        (6, "settings-tr"),
        (7, "about-tr"),
        (1, "accounts-tr"),
        (3, "activity-tr"),
        (8, "power-tr"),
    ):
        window.nav.setCurrentRow(row)
        app.processEvents()
        window.grab().save(str(review / f"{name}.png"))
        if name == "power-tr":
            window.grab().save(str(output / "power-tr.png"))
    for dark, theme in ((True, "dark"), (False, "light")):
        window._apply_theme(dark)
        for width, height in ((1040, 700), (1480, 960)):
            window.resize(width, height)
            for page, name in (
                (0, "overview"),
                (1, "accounts"),
                (2, "jobs"),
                (3, "activity"),
                (4, "goals"),
                (5, "diagnostics"),
                (6, "settings"),
                (7, "help"),
                (8, "power"),
            ):
                window.nav.setCurrentRow(page)
                if page == 0:
                    window.dashboard._render(sample_profiles())
                    window.dashboard._render_work(
                        (window.conversations._tracked, sample_threads()[0])
                    )
                elif page == 1:
                    window.accounts_view._render(sample_profiles())
                elif page == 2:
                    window.conversations._render_threads(sample_threads())
                    window.conversations._render_tracking(
                        (window.conversations._tracked, window.conversations._account_names)
                    )
                elif page == 3:
                    window.activity._render([])
                elif page == 5:
                    window.diagnostics._render([])
                app.processEvents()
                app.processEvents()
                window.grab().save(str(review / f"{name}-{theme}-{width}.png"))
                if page == 1:
                    window.accounts_view.set_loading("preview", True)
                    app.processEvents()
                    window.grab().save(str(review / f"loading-{theme}-{width}.png"))
                    window.accounts_view.set_loading("preview", False)
            window.nav.setCurrentRow(6)
            window.settings.tabs.setCurrentIndex(1)
            app.processEvents()
            window.grab().save(str(review / f"continuation-{theme}-{width}.png"))
            if dark and width == 1040:
                window.grab().save(str(output / "continuation-tr.png"))
            window.settings.tabs.setCurrentIndex(0)
    window.dispose()
    window.close()
    set_language("en")


def main() -> None:
    from codex_account_manager.core.paths import paths

    fields = ("data_dir", "db_path", "profiles_dir", "logs_dir", "backups_dir", "shared_codex_home")
    originals = {name: getattr(paths, name) for name in fields}
    with TemporaryDirectory(prefix="account-manager-preview-") as directory:
        root = Path(directory)
        for name in fields:
            object.__setattr__(paths, name, root / name)
        try:
            render()
        finally:
            for name, value in originals.items():
                object.__setattr__(paths, name, value)


if __name__ == "__main__":
    main()
