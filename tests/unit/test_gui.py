import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from scripts.render_preview import PreviewRunner, sample_profiles

from codex_account_manager.gui.async_runner import AsyncRunner
from codex_account_manager.gui.main_window import MainWindow
from codex_account_manager.gui.theme import stylesheet


@pytest.fixture(scope="module")
def app(qt_app):
    qt_app.setStyle("Fusion")
    qt_app.setStyleSheet(stylesheet())
    return qt_app


@pytest.fixture
def window(app):
    window = MainWindow(PreviewRunner())
    window.dashboard._render(sample_profiles())
    yield window
    window.dispose()
    window.close()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def test_all_screens_and_theme_switches(window, app):
    for dark in (True, False):
        window._apply_theme(dark)
        for row in range(len(window._views)):
            window.nav.setCurrentRow(row)
            app.processEvents()
            assert window.stack.currentIndex() == row
    assert window.dashboard.palette_ == window.settings.palette_
    window.dashboard._filter()
    assert window.dashboard.account_table.palette_ == window.dashboard.palette_


@pytest.mark.parametrize(
    "setting,scope",
    [
        ("desktop_continue", "desktop"),
        ("ide_continue", "ide"),
        ("auto_continue", None),
        ("monitoring", None),
    ],
)
def test_continuation_stop_setting_preserves_its_scope(window, monkeypatch, setting, scope):
    from codex_account_manager.core.events import bus

    events = []

    def saved(coro, on_result=None, on_error=None):
        coro.close()
        if on_result:
            on_result(None)

    monkeypatch.setattr(window.settings.runner, "submit", saved)
    unsubscribe = bus.subscribe("continuation.stop", lambda event: events.append(event.payload))
    try:
        getattr(window.settings, "_save_" + setting)(False)
    finally:
        unsubscribe()
    assert events == ([{"scope": scope}] if scope else [{}])


def test_installation_blocks_close_until_safe_handoff(window, monkeypatch):
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "information", lambda *_: None)
    window.settings.cli.installing = True
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted()
    window.settings.cli.installing = False
    window.updates.preparing = True
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted()
    window.updates._failed(RuntimeError("test failure"))
    assert not window._installation_in_progress()


def test_tour_visits_pages_and_restores_previous_selection(window):
    from codex_account_manager.gui.onboarding import ProductTour

    window.nav.setCurrentRow(1)
    tour = ProductTour(window)
    for index, (page, _, _) in enumerate(tour.STEPS):
        assert window.stack.currentIndex() == page
        assert tour.index == index
        tour._advance()
    assert window.stack.currentIndex() == 1


def test_support_buttons_open_only_public_project_links(window, monkeypatch):
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QPushButton

    from codex_account_manager.gui.about import AboutView
    from codex_account_manager.gui.i18n import tr

    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    view = window.findChild(AboutView)
    for title in (
        "Star on GitHub",
        "Report an issue",
        "Contribute a pull request",
        "Developer profile",
    ):
        next(
            button for button in view.findChildren(QPushButton) if button.text() == tr(title)
        ).click()
    assert opened == [
        "https://github.com/erkanpulat/codex-quotacrew",
        "https://github.com/erkanpulat/codex-quotacrew/issues",
        "https://github.com/erkanpulat/codex-quotacrew/pulls",
        "https://github.com/erkanpulat",
    ]


def test_filter_and_empty_state(window):
    window.dashboard.search.setText("studio")
    assert set(window.dashboard._account_rows) == {"Studio"}
    window.dashboard.search.setText("no match")
    assert not window.dashboard._account_rows
    window.dashboard._render([])
    assert "0 accounts" in window.dashboard.summary.text()


def test_update_availability_opens_shared_settings_tab(window, monkeypatch):
    from codex_account_manager import updates
    from tests.unit.test_updates import metadata

    monkeypatch.setattr(updates, "installed_directory", lambda: None)
    panel = window.updates
    panel._checked(updates.parse_release(metadata()))
    assert not window.update_button.isHidden()
    assert "2.0.0" in window.update_button.text()
    window.update_button.click()
    assert window.stack.currentIndex() == 6
    assert window.settings.tabs.currentWidget() is panel
    panel._checked(None)
    assert window.update_button.isHidden() and panel.install.isHidden()


def test_update_errors_restore_controls_and_shutdown_plan_blocks_install(window, monkeypatch):
    from unittest.mock import Mock

    from codex_account_manager import updates
    from tests.unit.test_updates import metadata

    monkeypatch.setattr(
        updates, "installed_directory", lambda: __import__("pathlib").Path("C:/app")
    )
    panel = window.updates
    panel._checked(updates.parse_release(metadata()))
    panel._working(True)
    panel._failed(RuntimeError("offline"))
    assert panel.progress.isHidden() and panel.check.isEnabled() and panel.install.isEnabled()
    submit = Mock()
    monkeypatch.setattr(panel.runner, "submit", submit)
    window._power_active = True
    panel._update()
    submit.assert_not_called()
    assert "shutdown" in panel.status.text()


def test_loading_waits_for_both_job_lists_and_clears_on_error(window):
    view = window.conversations
    view._render_threads([])
    view._render_tracking(([], {}))
    view.refresh()
    assert not view.loading.isHidden()
    view._render_tracking(([], {}))
    assert not view.loading.isHidden()
    view._sync_failed(RuntimeError("offline"))
    assert view.loading.isHidden()
    assert view.sync.isEnabled()


def test_account_refresh_keeps_rows_and_clears_loading_on_failure(window):
    view = window.accounts_view
    view._render(sample_profiles())
    aliases = set(view.table.rows)
    view.refresh()
    assert not view.loading.isHidden()
    assert set(view.table.rows) == aliases
    view._load_failed(RuntimeError("offline"))
    assert view.loading.isHidden()
    assert set(view.table.rows) == aliases
    view.refresh()
    view._render(sample_profiles())
    assert view.loading.isHidden()


def test_busy_account_refresh_preserves_rows_and_retries(window, monkeypatch):
    from codex_account_manager.core.errors import OperationBusyError
    from codex_account_manager.gui.i18n import tr
    from codex_account_manager.gui.overview import QTimer

    retries = []
    monkeypatch.setattr(
        QTimer, "singleShot", lambda delay, context, callback: retries.append(callback)
    )
    message = tr("Account operation in progress. Refresh will retry automatically.")
    error = OperationBusyError("busy", stage="prepare")
    for view, table, callback, status in (
        (
            window.dashboard,
            window.dashboard.account_table,
            window.dashboard._on_error,
            window.dashboard.status,
        ),
        (
            window.accounts_view,
            window.accounts_view.table,
            window.accounts_view._load_failed,
            window.accounts_view.operation_status,
        ),
    ):
        view._render(sample_profiles())
        aliases = set(table.rows)
        view.refresh()
        view.set_loading("work", False)
        callback(error)
        assert set(table.rows) == aliases
        assert status.text() == message
        assert view.loading.isHidden()
        retries.pop()()
        assert not view.loading.isHidden()
        view._render(sample_profiles())
        assert status.text() != message
    assert window.dashboard.alert.isHidden()


def test_diagnostic_failure_clears_loading_and_allows_retry(window):
    view = window.diagnostics
    view.refresh()
    assert not view.loading.isHidden()
    assert not view.run_button.isEnabled()
    view._failed(RuntimeError("offline"))
    assert view.loading.isHidden()
    assert view.run_button.isEnabled()
    assert not view.status.isHidden()
    view.refresh()
    view._render([])
    assert view.loading.isHidden()


def test_active_account_cannot_be_switched(window):
    menu = window.dashboard._account_rows["Personal"]._switch_btn.menu()
    assert "Switch account" not in [action.text() for action in menu.actions()]
    assert "Refresh usage" in [action.text() for action in menu.actions()]
    assert window.dashboard._account_rows["Studio"]._switch_btn.isEnabled()


def test_explicit_continuation_cancel_never_queues_work(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    submitted = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    monkeypatch.setattr(window.runner, "submit", lambda *args: submitted.append(args))
    window.conversations._continue_verified("thread")
    assert not submitted and not window.conversations._continue_busy


def test_waiting_connection_notice_clears_after_confirmed_submission(window):
    from codex_account_manager.core.events import bus

    bus.publish("continuation.status", state="waiting_connection", stage="desktop")
    assert not window.notice.isHidden()
    assert "Nothing has been sent" in window.notice_text.text()
    bus.publish("continuation.status", state="desktop_submitted", stage="execution")
    assert window.notice.isHidden()


def test_background_health_updates_both_account_pages(window):
    from codex_account_manager.core.events import bus

    profiles = sample_profiles()
    bus.publish("health.updated", health=profiles, count=len(profiles))
    assert set(window.accounts_view.table.rows) == {p.alias for p in profiles}
    assert set(window.dashboard._account_rows) == {p.alias for p in profiles}


def test_running_work_clears_its_previous_attention_notice(window):
    from codex_account_manager.core.events import bus

    bus.publish("continuation.status", thread_id="ide", state="needs_user", stage="ide")
    assert not window.notice.isHidden()
    bus.publish("continuation.status", thread_id="ide", state="running", stage="execution")
    assert window.notice.isHidden()
    assert not window._continuation_issues


def test_successful_work_does_not_hide_another_conversations_failure(window):
    from codex_account_manager.core.events import bus

    bus.publish("continuation.status", thread_id="ide", state="needs_user", stage="ide")
    bus.publish(
        "continuation.status", thread_id="desktop", state="desktop_submitted", stage="execution"
    )
    assert not window.notice.isHidden()
    assert "IDE" in window.notice_text.text()
    bus.publish("continuation.status", thread_id="ide", state="ide_submitted", stage="execution")
    assert window.notice.isHidden()


def test_completed_switch_clears_only_shared_dependency_not_other_errors(window):
    from codex_account_manager.core.events import bus

    bus.publish(
        "continuation.status", thread_id="desktop", state="waiting_shared", stage="desktop_restart"
    )
    bus.publish("continuation.status", thread_id="ide", state="needs_user", stage="ide")
    bus.publish("switch.completed", target="example")
    assert not window.notice.isHidden()
    assert "IDE" in window.notice_text.text()
    assert set(window._continuation_issues) == {"ide"}
    bus.publish("continuation.status", thread_id="ide", state="completed", stage="execution")
    assert window.notice.isHidden()


def test_turkish_account_status_preserves_dotted_and_dotless_letters(window):
    from dataclasses import replace

    from PySide6.QtWidgets import QLabel

    from codex_account_manager.gui.i18n import set_language
    from codex_account_manager.gui.widgets import AccountRow, account_status

    set_language("tr")
    try:
        health = sample_profiles()[1]
        assert account_status(health)[0] == "Kullanılabilir"
        assert account_status(replace(health, is_active=True))[0] == "Etkin"
        assert account_status(replace(health, stale=True))[0] == "Güncel değil"
        assert account_status(replace(health, auth_present=False))[0] == "Giriş gerekli"
        row = AccountRow(health, window.dashboard.palette_)
        assert "Kullanılabilir" in [label.text() for label in row.findChildren(QLabel)]
        row.deleteLater()
    finally:
        set_language("en")


def test_account_creation_starts_login_and_blocks_duplicate_adds(window, monkeypatch):
    from types import SimpleNamespace

    from codex_account_manager.gui import accounts as module

    submissions = []

    def submit(coro, on_result=None, on_error=None):
        submissions.append((coro.cr_code.co_name, on_result, on_error))
        coro.close()

    monkeypatch.setattr(module, "prompt_text", lambda *_args, **_kwargs: "New account")
    monkeypatch.setattr(window.runner, "submit", submit)
    view = window.accounts_view
    view._add()
    assert submissions[0][0] == "create_profile"
    assert not view.add_button.isEnabled()
    view._add()
    assert len(submissions) == 1
    submissions[0][1](SimpleNamespace(alias="New account"))
    assert submissions[1][0] == "login_profile"
    assert view._login_busy and not view.add_button.isEnabled()


def test_continuation_badges_and_tray_follow_loaded_settings(window):
    window.settings.desktop._checked(True)
    window.settings.ide._checked("installed")
    window.settings._loaded(
        {"auto_continue": "true", "ide_continue": "true", "monitor_enabled": "true"}
    )
    assert window.tray_auto.isChecked() and window.tray_ide.isChecked()
    assert not window.ide_badge.isHidden() and "Enabled" in window.ide_badge.text()
    window._work_changed(3, 2)
    assert "3 running" in window.tray_work.text()
    assert "3 running" in window.work_badge.text()
    window._monitoring_changed(False)
    assert "Paused" in window.ide_badge.text()
    assert window._running_work == 3
    window.settings._loaded({"ide_continue": "false", "auto_continue": "false"})
    assert not window.ide_badge.isHidden()
    assert "Off" in window.ide_badge.text()
    assert "Off" in window.tray_ide.text()
    assert "Off" in window.tray_auto.text()


def test_missing_desktop_explains_setup_and_preserves_off_preferences(window):
    window.settings._loaded({"ide_continue": "true", "auto_continue": "true"})
    window.settings.ide._checked("installed")
    window.settings.desktop._checked(False)
    assert "Needs setup" in window.desktop_badge.text()
    assert "Enabled" in window.tray_ide.text()
    assert window.settings.ide_continue.isChecked()
    window.settings.ide._checked("missing_editor")
    window.settings.desktop._checked(True)
    assert "Enabled" in window.desktop_badge.text()
    assert "Needs setup" in window.ide_badge.text()
    window.settings._loaded({"ide_continue": "false", "auto_continue": "false"})
    assert "Off" in window.tray_ide.text()
    assert not window.ide_badge.isHidden()


@pytest.mark.parametrize("page", [0, 1, 2, 3])
def test_account_and_job_tables_use_remaining_height(window, app, page):
    window.resize(1180, 760)
    window.accounts_view._render(sample_profiles())
    window.nav.setCurrentRow(page)
    window.show()
    app.processEvents()
    view = window._views[page]
    if page == 0:
        panel = view.accounts_panel
    elif page == 2:
        panel = view.tabs
    else:
        panel = view.table.parentWidget()
    assert view.height() - panel.geometry().bottom() <= 20


def test_startup_select_updates_and_restores_verified_state(window, monkeypatch):
    from codex_account_manager.platform import startup

    submissions = []

    def submit(coro, on_result=None, on_error=None):
        submissions.append(on_result)
        coro.close()

    monkeypatch.setattr(window.runner, "submit", submit)
    monkeypatch.setattr(startup, "enable_start_with_windows", lambda _command: True)
    settings = window.settings
    settings._startup_loaded(False)
    assert settings.startup.currentData() is False
    settings.startup.setCurrentIndex(settings.startup.findData(True))
    assert not settings.startup.isEnabled() and len(submissions) == 1
    settings._startup_loaded(False)
    assert settings.startup.currentData() is False and settings.startup.isEnabled()


def test_account_rows_have_their_own_menu_without_selection(window):
    view = window.accounts_view
    view._render(sample_profiles())
    assert not hasattr(view, "action_bar")
    assert len(view.table.rows) == 4
    assert all(row._switch_btn.menu() is not None for row in view.table.rows.values())


def test_async_callbacks_run_on_gui_thread_and_errors_are_visible(app):
    runner = AsyncRunner()
    main_thread = threading.get_ident()
    deliveries = []
    failures = []
    runner.failed.connect(failures.append)

    async def success():
        return threading.get_ident()

    async def failure():
        raise ValueError("password=short-secret")

    runner.submit(success(), lambda worker: deliveries.append((worker, threading.get_ident())))
    runner.submit(failure())
    deadline = time.monotonic() + 3
    while (not deliveries or not failures) and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    runner.shutdown()
    assert deliveries and deliveries[0][0] != main_thread
    assert deliveries[0][1] == main_thread
    assert failures and "short-secret" not in failures[0]
    assert not runner._thread.is_alive()


def test_turkish_navigation_and_settings_use_stable_ids(app):
    from codex_account_manager.gui.i18n import set_language

    set_language("tr")
    window = MainWindow(PreviewRunner())
    try:
        assert window.nav.item(0).text().strip() == "Genel Bakış"
        assert window.nav.item(7).text().strip() == "Yardım"
        assert window.settings.policy.itemData(0) == "manual"
        assert window.settings.policy.itemData(2) == "availability_failover"
        window.settings._loaded({"switch_policy": "confirm", "theme": "light"})
        assert window.settings.policy.currentData() == "confirm"
        assert "Geçiş yapmadan önce sor" in window.dashboard.mode_label.text()
        assert "Onaylı mod" in window.dashboard.mode_label.toolTip()
        assert window.settings.theme.currentData() == "light"
        window.dashboard._render([])
        assert window.dashboard.summary.isHidden()
        assert window.dashboard.hero.isHidden()
    finally:
        window.dispose()
        window.close()
        app.processEvents()
        set_language("en")


def test_conversation_filters_full_paths_and_resizable_columns(window):
    from PySide6.QtWidgets import QHeaderView

    from codex_account_manager.domain.models import ThreadRecord

    view = window.conversations
    long_path = "C:/Work/An example project with a long name/packages/application"
    view._render_threads(
        [
            ThreadRecord(
                id="one", title="Improve navigation", cwd=long_path, source="vscode", project_id="a"
            ),
            ThreadRecord(
                id="two", title="Check CLI", cwd="C:/Work/cli", source="cli", project_id="b"
            ),
            ThreadRecord(
                id="three",
                title="Review navigation",
                cwd=long_path,
                source="subAgent",
                project_id="a",
            ),
        ]
    )
    assert view.table.rowCount() == 2
    view.source.setCurrentIndex(view.source.findData(""))
    assert view.table.rowCount() == 3
    view.project.setCurrentIndex(view.project.findData("a"))
    assert view.table.rowCount() == 2
    view.source.setCurrentIndex(view.source.findData("vscode"))
    assert view.table.rowCount() == 1
    assert view.table.item(0, 4).toolTip() == long_path
    assert view.table.horizontalHeader().sectionResizeMode(4) == QHeaderView.ResizeMode.Interactive
    view.table.selectRow(0)
    assert long_path in view.details.toPlainText()
    assert all(button.isEnabled() for button in view._actions)
    view.search.setText("no matching conversation")
    assert view.table.rowCount() == 0
    assert all(not button.isEnabled() for button in view._actions)


def test_tracking_failure_is_visible_and_not_replaced_by_empty_snapshot(window):
    view = window.conversations
    view.show_tracking_status({"state": "timed_out"})
    view._render_tracking(([], {}))
    assert "timed out" in view.tracking_status.text()
    assert view.check_tracking.isEnabled()
    view.show_tracking_status({"state": "checking"})
    assert not view.check_tracking.isEnabled()
    view.show_tracking_status(
        {"state": "partial", "checked": 3, "unavailable": 2, "checked_at": "12:00:00"}
    )
    assert "2 conversations" in view.tracking_status.text()
    assert "12:00:00" in view.tracking_status.text()


def test_tracked_work_explains_turn_and_goal_without_storing_objective(window):
    from codex_account_manager.continuity.tracking import ObservedWork

    view = window.conversations
    view._render_tracking(
        (
            [
                ObservedWork(
                    "thread-a", "account-hash", "failed", "usageLimited", True, True, 1_800_000_000
                )
            ],
            {"account-hash": "Work"},
        )
    )
    assert view.tracked_table.rowCount() == 1
    assert view.tracked_table.item(0, 1).text() == "Work"
    assert view.tracked_table.item(0, 2).data(Qt.ItemDataRole.UserRole + 2) == "Limit reached"
    assert "usage" in view.tracked_table.item(0, 3).text().lower()
    assert view.tracked_empty.isHidden()
    view.tracked_table.selectRow(0)
    assert view.tracked_goal.isEnabled()


def test_conversation_refresh_failure_keeps_rows_and_unlocks_button(window):
    from codex_account_manager.domain.models import ThreadRecord

    view = window.conversations
    view._render_threads([ThreadRecord(id="one", title="Previous result")])
    view._sync()
    assert not view.sync.isEnabled()
    view._sync_failed(RuntimeError("Unavailable"))
    assert view.sync.isEnabled()
    assert view.table.rowCount() == 1
    assert "Unavailable" in view.status.text()


def test_settings_defaults_and_minimal_navigation(window):
    window.settings._loaded({})
    assert window.settings.policy.currentData() == "availability_failover"
    assert window.settings.interval.value() == 60
    assert window.settings.auto_continue.isChecked()
    assert window.settings.interval.minimum() == 30
    assert window.settings.interval.maximum() == 3600
    assert window.nav.item(4).isHidden()
    assert window.nav.item(5).isHidden()
    window._open_tool(4)
    assert window.stack.currentIndex() == 4


def test_small_window_and_keyboard_search(window, app):
    window.resize(1180, 760)
    window.show()
    app.processEvents()
    assert window.width() == 1180 and window.height() == 760
    window._focus_search()
    assert window.focusWidget() is window.dashboard.search
    window.nav.setCurrentRow(6)
    app.processEvents()
    assert window.settings.monitoring.isVisible()
    window._open_tool(4)
    assert not window.back_to_settings.isHidden()
    window.back_to_settings.click()
    assert window.stack.currentIndex() == 6


def test_refresh_preserves_selected_conversation_and_column_width(window):
    from datetime import UTC, datetime

    from codex_account_manager.domain.models import ThreadRecord

    view = window.conversations
    rows = [ThreadRecord(id="selected", title="Chosen conversation"), ThreadRecord(id="other")]
    view._render_threads(rows)
    view.table.selectRow(
        next(
            i
            for i in range(view.table.rowCount())
            if view.table.item(i, 0).text() == "Chosen conversation"
        )
    )
    view.table.setColumnWidth(4, 450)
    rows.append(ThreadRecord(id="newest", updated_at=datetime(2090, 1, 1, tzinfo=UTC)))
    view._render_threads(rows)
    assert view._current().id == "selected"
    assert view.table.columnWidth(4) == 450
    view._render_threads([row for row in rows if row.id != "selected"])
    assert view._current() is None
    assert view.action_host.isHidden()


def test_transaction_detail_does_not_duplicate_switch_failure_dialog(window, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    report = Mock()
    monkeypatch.setattr(window, "_switch_failed", report)
    window._domain_event(
        SimpleNamespace(topic="switch.failed", payload={"alias": "example", "detail": "Failure"})
    )
    report.assert_not_called()
    window._domain_event(SimpleNamespace(topic="switch.failed", payload={"detail": "Failure"}))
    assert report.call_count == 1


def test_about_explains_purpose_and_local_storage(window):
    from PySide6.QtWidgets import QLabel

    text = " ".join(label.text() for label in window.about.findChildren(QLabel)).casefold()
    assert "multiple codex accounts" in text
    assert "account" in text and "local" in text


def test_error_callback_failure_is_visible_and_redacted(app):
    runner = AsyncRunner()
    failures = []
    runner.failed.connect(failures.append)

    def broken_callback(_error):
        raise ValueError("password=callback-secret")

    try:
        runner._deliver_error(broken_callback, ValueError("original"))
        assert len(failures) == 1 and "callback-secret" not in failures[0]
    finally:
        runner.shutdown()


def test_switch_confirmation_does_not_open_nested_dialogs(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    calls = []

    def question(*_args):
        calls.append(True)
        window._switch("Another account")
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", question)
    window._switch("Example")
    assert len(calls) == 1
    assert not window._switching


def test_login_error_is_translated_and_allows_retry(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from codex_account_manager.gui.i18n import set_language

    view = window.accounts_view
    from codex_account_manager.domain.models import Profile

    view._render([Profile(alias="test", codex_home="unused")])
    view._login_busy = True
    view._set_actions_enabled(False)
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: messages.append(args[2]))
    try:
        set_language("tr")
        view._login_failed(
            RuntimeError(
                "Codex sign-in failed. Check Codex in Settings > System check, then try again."
            )
        )
        assert "Ayarlar > Sistem Kontrolü" in messages[0]
        assert not view._login_busy
        assert all(button.isEnabled() for button in view._menu_buttons)
    finally:
        set_language("en")


def test_automatic_continuation_setting_and_status_are_visible(window):
    from codex_account_manager.core.events import Event
    from codex_account_manager.gui.i18n import set_language

    window.settings._loaded({"auto_continue": "false"})
    assert not window.settings.auto_continue.isChecked()
    try:
        set_language("tr")
        window._domain_event(Event("continuation.status", {"state": "running"}))
        assert "otomatik devam" in window.statusBar().currentMessage()
        window._domain_event(Event("continuation.status", {"state": "needs_user"}))
        assert "müdahaleniz gerekiyor" in window.statusBar().currentMessage()
        assert not window.notice.isHidden()
        assert window.notice_text.text() == window.statusBar().currentMessage()
    finally:
        set_language("en")


def test_dismissed_continuation_warning_stays_closed_until_state_changes(window):
    from codex_account_manager.core.events import Event

    payload = {"thread_id": "test-thread", "state": "needs_user", "stage": "verification"}
    window._domain_event(Event("continuation.status", payload))
    assert "could not be verified" in window.notice_text.text()
    window._dismiss_notice()
    window._domain_event(Event("continuation.status", payload))
    assert window.notice.isHidden()
    window._domain_event(Event("continuation.status", {**payload, "stage": "connection"}))
    assert not window.notice.isHidden()
    window._dismiss_notice()
    window._domain_event(Event("continuation.status", {**payload, "state": "running"}))
    window._domain_event(Event("continuation.status", payload))
    assert not window.notice.isHidden()


def test_transient_continuation_notice_expires_without_hiding_other_issues(window, monkeypatch):
    from codex_account_manager.core.events import Event
    from codex_account_manager.gui.main_window import QTimer

    timers = []
    monkeypatch.setattr(QTimer, "singleShot", lambda *args: timers.append(args))
    transient = {"thread_id": "a", "state": "needs_user", "stage": "verification"}
    window._domain_event(Event("continuation.status", transient))
    expiry = next(args[-1] for args in timers if args[0] == 30000)
    window._domain_event(Event("continuation.status", {"thread_id": "b", "state": "needs_user"}))
    expiry()
    assert not window.notice.isHidden()
    assert window.notice_text.text() == window._continuation_issues["b"]
    window._domain_event(Event("continuation.status", {"thread_id": "b", "state": "completed"}))
    assert window.notice.isHidden()
    window._domain_event(Event("continuation.status", transient))
    assert window.notice.isHidden()


def test_sidebar_tools_remain_selected_across_theme_and_nested_pages(window):
    for dark in (True, False):
        for page, active in ((6, 6), (5, 6), (4, 6), (7, 7), (8, 8), (0, None)):
            window._on_nav(page)
            window._apply_theme(dark)
            assert [
                key for key, (button, _) in window.sidebar_actions.items() if button.isChecked()
            ] == ([active] if active is not None else [])


def test_empty_table_message_tracks_data_changes(window, app):
    window.nav.setCurrentRow(5)
    window.show()
    app.processEvents()
    table = window.diagnostics.table
    assert table.empty.isVisibleTo(window)
    table.setRowCount(1)
    assert table.empty.isHidden()
    table.setRowCount(0)
    assert table.empty.isVisibleTo(window)


def test_account_row_menu_targets_alias_after_sort_and_locks_during_login(window, monkeypatch):
    from unittest.mock import AsyncMock

    from codex_account_manager.domain.models import Profile
    from codex_account_manager.gui.i18n import tr

    view = window.accounts_view
    profiles = [Profile(alias=name, codex_home="unused") for name in ("First", "Second")]
    view._render(profiles)
    assert not view.filters_host.isHidden()
    view._render(list(reversed(profiles)))
    button = view.table.rows["Second"]._switch_btn
    assert button is not None
    rename = AsyncMock()
    monkeypatch.setattr(view.accounts, "rename_profile", rename)
    monkeypatch.setattr(
        "codex_account_manager.gui.accounts.prompt_text", lambda *args, **kwargs: "Renamed"
    )
    action = next(action for action in button.menu().actions() if action.text() == tr("Rename"))
    action.trigger()
    rename.assert_called_once_with("Second", "Renamed")
    assert list(view.table.rows) == ["First", "Second"]
    login = AsyncMock()
    monkeypatch.setattr(view.accounts, "login_profile", login)
    sign_in = next(action for action in button.menu().actions() if action.text() == tr("Sign in"))
    sign_in.trigger()
    login.assert_called_once_with("Second")
    assert all(not button.isEnabled() for button in view._menu_buttons)
    view._login_busy = False
    view._set_actions_enabled(True)
    assert all(button.isEnabled() for button in view._menu_buttons)
    view._render([])
    assert not view.guide.isHidden()
    assert view.filters_host.isHidden()
    assert view.table.isHidden()


def test_conversation_menu_actions_and_optional_details(window, monkeypatch):
    from unittest.mock import AsyncMock

    from PySide6.QtGui import QAction

    from codex_account_manager.continuity.service import ContinuityService
    from codex_account_manager.domain.models import ThreadRecord
    from codex_account_manager.gui.i18n import tr

    view = window.conversations
    view._render_threads([ThreadRecord(id="chosen", title="Example", cwd="C:/Example/project")])
    view.table.selectRow(0)
    assert view.details.isHidden()
    view.details_toggle.trigger()
    assert not view.details.isHidden()
    assert "C:/Example/project" in view.details.toPlainText()
    notes = []
    monkeypatch.setattr(window.goals, "save_for_thread", notes.append)
    read_goal = AsyncMock()
    opened = []
    monkeypatch.setattr(view, "open_desktop", opened.append)
    load = AsyncMock()
    monkeypatch.setattr(ContinuityService, "read_native_goal", read_goal)
    monkeypatch.setattr(ContinuityService, "resume_conversation", load)
    for title in ("Read Codex goal", "Save local goal note"):
        next(
            action for action in view.findChildren(QAction) if action.text() == tr(title)
        ).trigger()
    view._actions[0].click()
    read_goal.assert_called_once_with("chosen")
    load.assert_not_called()
    assert opened == ["chosen"]
    assert notes == ["chosen"]
    view.table.clearSelection()
    assert view._current() is None
    assert view.details.isHidden() and view.action_host.isHidden()


@pytest.mark.parametrize("locale", ["tr", "en"])
@pytest.mark.parametrize("dark", [True, False])
def test_settings_controls_fit_minimum_window_and_remain_reachable(app, locale, dark):
    from PySide6.QtCore import QPoint

    from codex_account_manager.gui.i18n import set_language

    set_language(locale)
    window = MainWindow(PreviewRunner())
    try:
        window.resize(1040, 700)
        window.nav.setCurrentRow(6)
        window.settings._loaded({"theme": "dark" if dark else "light"})
        window.show()
        app.processEvents()
        assert window.size().width() == 1040 and window.size().height() == 700

        window.settings.tabs.setCurrentIndex(0)
        app.processEvents()
        scroll = window.settings.tabs.currentWidget()
        assert scroll.horizontalScrollBar().maximum() == 0
        for control in (
            window.settings.monitoring,
            window.settings.policy,
            window.settings.background,
            window.settings.theme,
            window.settings.language,
            window.settings.startup,
        ):
            scroll.ensureWidgetVisible(control.parentWidget())
            app.processEvents()
            top_left = control.mapTo(scroll.viewport(), QPoint(0, 0))
            bottom_right = control.mapTo(scroll.viewport(), control.rect().bottomRight())
            assert scroll.viewport().rect().contains(top_left)
            assert bottom_right.y() <= scroll.viewport().rect().bottom() + 2

        window.settings.tabs.setCurrentIndex(1)
        app.processEvents()
        scroll2 = window.settings.tabs.currentWidget()
        for control in (
            window.settings.ide_continue,
            window.settings.ide_refresh,
            window.settings.auto_continue,
            window.settings.interval,
        ):
            scroll2.ensureWidgetVisible(control.parentWidget())
            app.processEvents()
            top_left = control.mapTo(scroll2.viewport(), QPoint(0, 0))
            bottom_right = control.mapTo(scroll2.viewport(), control.rect().bottomRight())
            assert scroll2.viewport().rect().contains(top_left)
            assert bottom_right.y() <= scroll2.viewport().rect().bottom() + 2
    finally:
        window.dispose()
        window.close()
        app.processEvents()
        set_language("en")


def test_initial_account_loading_is_distinct_from_empty_and_failed(app):
    from PySide6.QtWidgets import QLabel

    from codex_account_manager.gui.i18n import tr

    window = MainWindow(PreviewRunner())
    try:
        view = window.dashboard

        def labels():
            return [item.text() for item in view.account_table._host.findChildren(QLabel)]

        assert not view._loaded
        assert tr("Loading accounts…") in labels()
        assert window.accounts_view.guide.isHidden()
        assert window.accounts_view.operation_status.text() == tr("Loading saved accounts…")
        view._on_error(RuntimeError("offline"))
        assert tr("Accounts could not be loaded") in labels()
        assert tr("Bring your first account") not in labels()
        view._render([])
        assert tr("Bring your first account") in labels()
        window.accounts_view._render([])
        assert not window.accounts_view.guide.isHidden()
        assert not window.accounts_view.operation_status.text()
    finally:
        window.dispose()
        window.close()
        app.processEvents()


def test_desktop_open_uses_native_link_and_reports_handler_failure(window, monkeypatch):
    from codex_account_manager.platform import windows

    opened = []
    monkeypatch.setattr(windows, "open_conversation", opened.append)
    view = window.conversations
    view.open_desktop("thread-a")
    assert opened == ["thread-a"]

    def missing_handler(_thread_id):
        raise OSError("no registered handler")

    monkeypatch.setattr(windows, "open_conversation", missing_handler)
    view.open_desktop("thread-a")
    assert "could not be opened" in view.tracking_status.text()


def test_tracking_selection_follows_identity_after_refresh(window):
    from PySide6.QtCore import Qt

    from codex_account_manager.continuity.tracking import ObservedWork

    view = window.conversations
    first = ObservedWork("first", "account", "inProgress", None, False, False, 1)
    second = ObservedWork("second", "account", "awaitingDesktop", None, False, False, 2)
    view._render_tracking(([first, second], {}))
    view.tracked_table.selectRow(0)
    view._render_tracking(([second, first], {}))
    assert (
        view.tracked_table.item(view.tracked_table.currentRow(), 0).data(Qt.ItemDataRole.UserRole)
        == "first"
    )
    assert view.tracked_open.isEnabled()
    view._render_tracking(([second], {}))
    assert not view.tracked_table.selectedItems()
    assert not view.tracked_open.isEnabled()


def test_missing_account_catalogue_shows_recovery_dialog_before_starting(app, monkeypatch):
    from unittest.mock import AsyncMock

    from PySide6.QtWidgets import QMessageBox

    from codex_account_manager.core.errors import AccountRecoveryRequired
    from codex_account_manager.gui import app as entry

    messages = []
    monkeypatch.setattr(
        entry,
        "initialize_database",
        AsyncMock(side_effect=AccountRecoveryRequired("missing catalogue")),
    )
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: messages.append(args))
    assert entry.run_gui() == 1
    assert len(messages) == 1
    assert messages[0][0] is None


@pytest.mark.parametrize("reason", ["Trigger", "DoubleClick"])
def test_tray_restores_hidden_minimized_window(window, app, reason):
    from PySide6.QtWidgets import QSystemTrayIcon

    window.showMinimized()
    window.hide()
    app.processEvents()
    window._on_tray_activated(getattr(QSystemTrayIcon.ActivationReason, reason))
    app.processEvents()
    assert window.isVisible()
    assert not window.isMinimized()
    assert window.tray.contextMenu().parent() is window


def test_reset_credit_details_are_plain_text_and_separate_from_renewals(app, monkeypatch):
    from dataclasses import replace

    from PySide6.QtWidgets import QDialog, QLabel, QPushButton

    from codex_account_manager.domain.models import ResetCredit, ResetCredits
    from codex_account_manager.gui.i18n import set_language
    from codex_account_manager.gui.widgets import AccountRow

    set_language("en")
    health = replace(
        sample_profiles()[0],
        reset_credits=ResetCredits(
            available_count=2,
            credits=(
                ResetCredit(
                    status="available",
                    reset_type="codexRateLimits",
                    granted_at=1800000000,
                    expires_at=None,
                    title="<b>Untrusted title</b>",
                    description="Example details",
                ),
            ),
        ),
    )
    card = AccountRow(health)
    displayed_labels = []

    def _capture_dialog(dialog):
        # Collect all QLabel texts from the card-layout dialog
        displayed_labels.extend(lbl.text() for lbl in dialog.findChildren(QLabel))

    monkeypatch.setattr(QDialog, "exec", _capture_dialog)
    # Button text is now "2 reset credits" (count-based) or "View reset credits"
    button = next(
        b
        for b in card.findChildren(QPushButton)
        if "reset credit" in b.text().lower() or "view reset" in b.text().lower()
    )
    button.click()
    all_text = " ".join(displayed_labels)
    assert "2" in all_text  # available count shown in header
    assert "No expiry" in all_text
    assert "only some credits" in all_text
    assert sum(label.toolTip().startswith("Renews ") for label in card.findChildren(QLabel)) == 2
    card.close()


def test_support_probe_drops_stale_results_and_prevents_duplicate_requests(window):
    from codex_account_manager.domain.models import ThreadRecord

    view = window.conversations
    view._render_threads([ThreadRecord(id="a", title="A"), ThreadRecord(id="b", title="B")])
    view.table.selectRow(0)
    calls = []

    def submit(coro, on_result=None, on_error=None):
        coro.close()
        calls.append(on_result)

    view.runner.submit = submit
    try:
        view._check_support()
        view._check_support()
        assert len(calls) == 1
        view.table.selectRow(1)
        calls[0]("desktop")
        assert "can read this conversation" not in view.support_status.text()
        view._check_support()
        assert len(calls) == 2
        calls[1]("unverified")
        assert "No message was sent" in view.support_status.text()
        assert not view._support_busy
    finally:
        view.runner.submit = PreviewRunner().submit


def test_account_filters_combine_with_search_without_turning_unknown_into_ready(window):
    view = window.dashboard
    view.account_filter.setCurrentIndex(view.account_filter.findData("credits"))
    assert set(view._account_rows) == {"Personal"}
    view.search.setText("studio")
    assert not view._account_rows
    view.search.clear()
    view.account_filter.setCurrentIndex(view.account_filter.findData("attention"))
    assert set(view._account_rows) == {"Client workspace"}
    view.account_filter.setCurrentIndex(view.account_filter.findData("ready"))
    assert "Client workspace" not in view._account_rows
    view.account_filter.setCurrentIndex(view.account_filter.findData("all"))
    assert len(view._account_rows) == 4


def test_global_email_privacy_survives_refresh_and_hides_unverified_addresses(window):
    from dataclasses import replace

    health = replace(sample_profiles()[0], email="owner@example.test")
    view = window.dashboard
    view._render([health])
    assert view._account_rows[health.alias]._email_button.toolTip() == health.email
    view.email_toggle.click()
    view._render([health])
    card = view._account_rows[health.alias]
    assert health.email not in card._email_button.text()
    assert health.email not in card._email_button.toolTip()
    view.email_toggle.click()
    assert view._account_rows[health.alias]._email_button.toolTip() == health.email
    view._render([replace(health, account_match=False)])
    assert not hasattr(view._account_rows[health.alias], "_email_button")


def test_stale_card_keeps_usage_but_cannot_switch(window):
    from dataclasses import replace

    from PySide6.QtWidgets import QLabel

    from codex_account_manager.domain.states import QuotaState

    health = replace(
        sample_profiles()[1], stale=True, error="offline", quota_state=QuotaState.UNKNOWN
    )
    window.dashboard._render([health])
    card = window.dashboard._account_rows[health.alias]
    switch = next(
        action for action in card._switch_btn.menu().actions() if action.text() == "Switch account"
    )
    assert not switch.isEnabled()
    texts = [item.text() for item in card.findChildren(QLabel)]
    assert "Stale" in texts
    assert any("does not mean your sign-in is invalid" in text for text in texts)


def test_new_install_starts_monitoring_and_preserves_explicit_pause(window):
    window.settings._loaded({})
    assert window.settings.monitoring.isChecked()
    assert window.settings.theme.currentData() == "dark"
    assert not window.settings.ide_continue.isChecked()
    assert not window.settings.background.isChecked()
    assert not window._keep_in_tray
    assert not window.power_view.controls.timer.isActive()
    assert window.power_view.controls.plan.target is None
    assert "checked every" in window.dashboard.mode_detail.text().lower()
    window.settings._loaded({"monitor_enabled": "false"})
    assert not window.settings.monitoring.isChecked()
    assert "paused" in window.dashboard.mode_detail.text().lower()


def test_cancel_ignores_pending_power_check_result(window, monkeypatch):
    from unittest.mock import Mock

    from codex_account_manager.monitoring.power import PowerEvidence, PowerTarget
    from codex_account_manager.platform import power

    widget = window.power_view.controls
    callbacks = []

    def submit(coro, on_result=None, on_error=None):
        coro.close()
        callbacks.append(on_result)

    widget.runner.submit = submit
    shutdown = Mock()
    monkeypatch.setattr(power, "shutdown_windows", shutdown)
    widget.plan.arm(PowerTarget("limits"))
    widget._tick()
    assert len(callbacks) == 1
    widget.cancel()
    callbacks[0](PowerEvidence(True, "done", "a"))
    assert widget.plan.target is None
    assert not widget.timer.isActive()
    shutdown.assert_not_called()


def test_quit_cancels_shutdown_plan(window, monkeypatch):
    from PySide6.QtGui import QCloseEvent

    from codex_account_manager.monitoring.power import PowerTarget

    window.power_view.controls.plan.arm(PowerTarget("limits"))
    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted()
    assert window.power_view.controls.plan.target is None


def test_shutdown_is_delivered_once_after_fresh_countdown(window, monkeypatch):
    from unittest.mock import Mock

    from codex_account_manager.gui import power as gui_power
    from codex_account_manager.monitoring.power import PowerEvidence, PowerTarget
    from codex_account_manager.platform import power

    widget = window.power_view.controls
    callbacks = []
    clock = [0.0]
    monkeypatch.setattr(gui_power.time, "monotonic", lambda: clock[0])
    shutdown = Mock()
    monkeypatch.setattr(power, "shutdown_windows", shutdown)

    def submit(coro, on_result=None, on_error=None):
        coro.close()
        callbacks.append(on_result)

    widget.runner.submit = submit
    widget.plan.arm(PowerTarget("limits"))
    for second in range(0, 121, 15):
        clock[0] = float(second)
        widget._tick()
        callbacks.pop(0)(PowerEvidence(True, "done", "a"))
    shutdown.assert_called_once()
    assert widget.plan.target is None
    assert not widget.timer.isActive()


def test_shutdown_status_and_cancel_are_visible_in_tray(window):
    from codex_account_manager.monitoring.power import PowerTarget

    window.power_view.controls.plan.arm(PowerTarget("limits"))
    normal_key = window.tray.icon().cacheKey()
    window._power_status("Shutting down in 102s", True)
    assert "102s" in window.tray.toolTip()
    assert window.tray_cancel_power.isVisible() and window.tray_cancel_power.isEnabled()
    assert "102s" in window.tray_power_detail.text()
    assert window.tray.icon().cacheKey() != normal_key
    assert not window.power_badge.isHidden()
    pending_key = window.tray.icon().cacheKey()
    window._power_status("Shutting down in 101s", True)
    assert window.tray.icon().cacheKey() == pending_key
    window.tray_cancel_power.trigger()
    assert window.power_view.controls.plan.target is None
    assert not window.tray_cancel_power.isVisible()
    assert window.power_badge.isHidden()
    assert "Shutting down" not in window.tray.toolTip()


@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("dark", [True, False])
def test_compact_account_rows_keep_quota_columns_aligned(app, language, dark):
    from dataclasses import replace

    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtWidgets import QLabel, QScrollArea

    from codex_account_manager.gui.i18n import set_language

    set_language(language)
    window = MainWindow(PreviewRunner())
    try:
        window._apply_theme(dark)
        profiles = sample_profiles()
        profiles[1] = replace(profiles[1], alias="A very long account name " * 3)
        window.dashboard._render(profiles)
        window.resize(1040, 700)
        window.show()
        app.processEvents()
        rows = list(window.dashboard._account_rows.values())
        assert len({row._primary_usage.x() for row in rows}) == 1
        assert len({row._secondary_usage.x() for row in rows}) == 1
        assert all(
            rows[index].geometry().bottom() < rows[index + 1].geometry().top()
            for index in range(len(rows) - 1)
        )
        assert any(
            area.verticalScrollBar().maximum() > 0
            for area in window.dashboard.findChildren(QScrollArea)
        )
        for row in rows:
            name = next(
                item for item in row.findChildren(QLabel) if item.objectName() == "FieldTitle"
            )
            assert name.width() > 0
            assert name.text() == row.alias
            assert name.toolTip() == row.alias
            assert name.textFormat() == Qt.TextFormat.PlainText
            assert row._plan.isVisibleTo(window)
            assert (
                abs(
                    row._plan.mapTo(row, QPoint()).x()
                    - name.mapTo(row, QPoint()).x()
                    - name.width()
                    - 12
                )
                <= 1
            )
            status = next(
                item
                for item in row.findChildren(QLabel)
                if item.objectName() == "Pill" and item.isVisibleTo(window)
            )
            assert (
                abs(status.mapTo(row, status.rect().center()).y() - row.rect().center().y()) <= 12
            )
            assert row._switch_btn.isVisibleTo(window) or not row._switch_btn.isEnabled()
            if row._switch_btn.isVisibleTo(window):
                assert row._switch_btn.parentWidget().rect().contains(row._switch_btn.geometry())
            assert row._primary_usage.width() >= 130
        assert all(
            area.horizontalScrollBar().maximum() == 0
            for area in window.dashboard.findChildren(QScrollArea)
        )
        headings = [button for button, _title in window.dashboard._header_buttons.values()]
        first = rows[0]
        cells = [first._identity, *first._usage_cells, first._status_host]
        for heading, cell in zip(headings, cells, strict=True):
            assert (
                abs(
                    heading.mapTo(window.dashboard, QPoint()).x()
                    - cell.mapTo(window.dashboard, QPoint()).x()
                )
                <= 2
            )
        window.resize(1600, 940)
        app.processEvents()
        assert (
            window.dashboard.table_header.width()
            == window.dashboard.accounts_scroll.viewport().width()
        )
        for heading, cell in zip(headings, cells, strict=True):
            assert (
                abs(
                    heading.mapTo(window.dashboard, QPoint()).x()
                    - cell.mapTo(window.dashboard, QPoint()).x()
                )
                <= 2
            )
    finally:
        window.dispose()
        window.close()
        app.processEvents()
        set_language("en")


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("active", ["Personal", "Client workspace", "Open source", "Studio"]),
        ("name", ["Client workspace", "Open source", "Personal", "Studio"]),
        ("five_most", ["Studio", "Personal", "Open source", "Client workspace"]),
        ("five_least", ["Client workspace", "Open source", "Personal", "Studio"]),
        ("weekly_most", ["Studio", "Open source", "Personal", "Client workspace"]),
        ("weekly_least", ["Client workspace", "Personal", "Open source", "Studio"]),
        ("credits", ["Personal", "Client workspace", "Studio", "Open source"]),
    ],
)
def test_account_sorting_respects_selected_order_after_refresh(window, mode, expected):
    view = window.dashboard
    view.sort_order.setCurrentIndex(view.sort_order.findData(mode))
    assert list(view._account_rows) == expected
    view._render(sample_profiles())
    assert list(view._account_rows) == expected
    view.search.setText("studio")
    assert list(view._account_rows) == ["Studio"]
    view.search.clear()
    assert list(view._account_rows) == expected


def test_quota_sorting_uses_window_duration_and_places_unverified_last(window):
    from dataclasses import replace

    base = sample_profiles()[0]
    swapped = replace(
        base,
        alias="Weekly first",
        profile_id="swapped",
        is_active=False,
        primary_window_minutes=10080,
        primary_used_percent=90,
        secondary_window_minutes=300,
        secondary_used_percent=1,
    )
    stale = replace(
        base, alias="A stale account", profile_id="stale", stale=True, primary_used_percent=0
    )
    missing = replace(
        base, alias="Unknown duration", profile_id="missing", primary_window_minutes=None
    )
    invalid = replace(
        base, alias="Invalid usage", profile_id="invalid", primary_used_percent=float("nan")
    )
    view = window.dashboard
    view._render([stale, missing, invalid, base, swapped])
    view.sort_order.setCurrentIndex(view.sort_order.findData("five_most"))
    assert list(view._account_rows) == [
        "Weekly first",
        "Personal",
        "A stale account",
        "Invalid usage",
        "Unknown duration",
    ]
    view.sort_order.setCurrentIndex(view.sort_order.findData("weekly_most"))
    assert list(view._account_rows)[-1] == "A stale account"
    assert list(view._account_rows)[-2] == "Weekly first"


def test_renewal_sorting_does_not_treat_expired_or_missing_time_as_fresh(window, monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr("codex_account_manager.gui.overview.time.time", lambda: 1000)
    base = sample_profiles()[0]
    view = window.dashboard
    view._render(
        [
            replace(base, alias="Expired", primary_resets_at=900, secondary_resets_at=None),
            replace(base, alias="Later", primary_resets_at=2000, secondary_resets_at=3000),
            replace(base, alias="Sooner", primary_resets_at=4000, secondary_resets_at=1200),
            replace(base, alias="Unknown", primary_resets_at=None, secondary_resets_at=None),
        ]
    )
    view.sort_order.setCurrentIndex(view.sort_order.findData("renewal"))
    assert list(view._account_rows) == ["Sooner", "Later", "Expired", "Unknown"]


def test_restored_sort_cannot_override_a_new_user_choice(window):
    view = window.dashboard
    view._restore_sort("weekly_most")
    assert view.sort_order.currentData() == "weekly_most"
    view.sort_order.setCurrentIndex(view.sort_order.findData("name"))
    view._restore_sort("five_least")
    assert view.sort_order.currentData() == "name"


def test_header_sort_does_not_change_next_candidate(window):
    view = window.dashboard
    view._render(sample_profiles())
    view.set_automation("availability_failover", True, 90)
    assert not view._account_rows["Studio"]._candidate.isHidden()
    view._sort_column(("five_most", "five_least"))
    assert list(view._account_rows)[0] == "Studio"
    view._sort_column(("five_most", "five_least"))
    assert list(view._account_rows)[0] == "Client workspace"
    assert not view._account_rows["Studio"]._candidate.isHidden()
    view.set_automation("availability_failover", False, 90)
    assert all(row._candidate.isHidden() for row in view._account_rows.values())


def test_stale_candidate_and_unknown_windows_are_not_presented_as_capacity(window):
    from dataclasses import replace

    profiles = sample_profiles()
    profiles[1] = replace(profiles[1], stale=True, error=None)
    profiles[3] = replace(profiles[3], reauth_required=True)
    view = window.dashboard
    view._render(profiles)
    view.set_automation("availability_failover", True, 60)
    assert all(row._candidate.isHidden() for row in view._account_rows.values())
    assert view._account_rows["Studio"]._primary_usage._value.text() == "—"
    view._render([replace(profiles[0], primary_window_minutes=60)])
    assert view._account_rows["Personal"]._primary_usage._value.text() == "—"


def test_free_account_is_manual_only_and_never_badged_as_next_candidate(window):
    from dataclasses import replace

    from PySide6.QtWidgets import QLabel

    profiles = sample_profiles()
    profiles[1] = replace(
        profiles[1], plan_type="free", primary_used_percent=None, secondary_used_percent=None
    )
    view = window.dashboard
    view._render(profiles)
    view.set_automation("availability_failover", True, 60)
    free = view._account_rows["Studio"]
    assert free._candidate.isHidden()
    assert free._primary_usage._value.text() == "—"
    assert any("manual switching only" in label.text() for label in free.findChildren(QLabel))
    assert any(
        action.isEnabled() and action.text() == "Switch account"
        for action in free._switch_btn.menu().actions()
    )
    assert not view._account_rows["Open source"]._candidate.isHidden()


def test_refresh_error_banner_clears_only_after_success(window):
    view = window.dashboard
    view._on_error(RuntimeError("offline"))
    assert not view.alert.isHidden()
    view._render(sample_profiles())
    assert view.alert.isHidden()


def test_nonmodal_feedback_keeps_errors_visible_and_success_ephemeral(window):
    window._switch_failed(RuntimeError("offline"))
    assert not window.notice.isHidden()
    assert "offline" in window.notice_text.text()
    window._switch_done("Private account alias")
    assert window.notice.isHidden()
    assert not window.toast.isHidden()
    assert "Private account alias" not in window.toast.text()
    assert window.toast_timer.isActive()
    window.toast_timer.timeout.emit()
    assert window.toast.isHidden()


def test_jobs_filter_clears_actions_and_shutdown_uses_selected_identity(window):
    from codex_account_manager.continuity.tracking import ObservedWork

    view = window.conversations
    jobs = [
        ObservedWork("running", "account", "inProgress", "active", True, False, 1),
        ObservedWork("done", "account", "completed", "complete", True, False, 2),
    ]
    view._render_tracking((jobs, {"account": "Example"}))
    assert view.work_fields["account"].text() == "Example"
    requested = []
    view.shutdown_requested.connect(requested.append)
    view._shutdown_tracked()
    assert requested == ["running"]
    view.work_filter.setCurrentIndex(view.work_filter.findData("completed"))
    assert view.tracked_table.isRowHidden(0)
    assert not view.tracked_open.isEnabled()
    assert not view.work_more.isEnabled()
    view._shutdown_tracked()
    assert requested == ["running"]
    view.tracked_table.selectRow(1)
    assert view.work_fields["turn"].text() == "Completed"
    view.work_search.setText("missing")
    assert not view.tracked_open.isEnabled()


def test_activity_excludes_raw_details_and_recovers_after_error(window):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    activity = window.activity
    record = SimpleNamespace(
        started_at=datetime.now(UTC),
        reason=SimpleNamespace(value="manual"),
        success=False,
        detail="token=private-value",
    )
    activity._render([record])
    assert activity.table.rowCount() == 1
    for column in range(activity.table.columnCount()):
        item = activity.table.item(0, column)
        assert "private-value" not in item.text() + item.toolTip()
    activity._failed(RuntimeError("token=private-value"))
    assert activity.refresh_button.isEnabled()
    assert "private-value" not in activity.status.text()
    activity._render([])
    assert activity.table.rowCount() == 0


def test_global_navigation_and_status_follow_monitor_state(window):
    assert [window.nav.item(i).isHidden() for i in range(4)] == [False] * 4
    window.dashboard.manage_requested.emit()
    assert window.stack.currentWidget() is window.accounts_view
    window.dashboard.work_requested.emit()
    assert window.stack.currentWidget() is window.conversations
    window._monitoring_changed(False)
    assert window.global_monitor.text() == "Start"
    window._monitoring_changed(True)
    assert window.global_monitor.text() == "Pause"


def test_account_table_headers_sort_each_column_and_plan_is_badged(window):
    from PySide6.QtWidgets import QLabel

    view = window.dashboard
    view._render(sample_profiles())
    for choices in (
        ("name", "name_desc"),
        ("five_most", "five_least"),
        ("weekly_most", "weekly_least"),
        ("status", "status_desc"),
    ):
        button, _title = view._header_buttons[choices]
        button.click()
        assert view.sort_order.currentData() == choices[0]
        assert not button.icon().isNull()
        button.click()
        assert view.sort_order.currentData() == choices[1]
        assert not button.icon().isNull()
    profile = view._account_rows["Personal"]
    assert any(
        item.objectName() == "PlanBadge" and item.text() == "Plus"
        for item in profile.findChildren(QLabel)
    )


def test_conversation_and_activity_dates_sort_chronologically(window):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from PySide6.QtCore import Qt

    from codex_account_manager.domain.models import ThreadRecord

    threads = [
        ThreadRecord(
            id="later", title="Later", source="cli", updated_at=datetime(2026, 10, 1, tzinfo=UTC)
        ),
        ThreadRecord(
            id="earlier",
            title="Earlier",
            source="cli",
            updated_at=datetime(2026, 9, 30, tzinfo=UTC),
        ),
    ]
    conversations = window.conversations
    conversations._render_threads(threads)
    conversations.table.sortItems(3, Qt.SortOrder.AscendingOrder)
    assert conversations.table.item(0, 0).text() == "Earlier"
    conversations.table.sortItems(3, Qt.SortOrder.DescendingOrder)
    assert conversations.table.item(0, 0).text() == "Later"

    activity = window.activity
    records = [
        SimpleNamespace(
            started_at=thread.updated_at, reason=SimpleNamespace(value="manual"), success=True
        )
        for thread in threads
    ]
    activity._render(records)
    activity.table.sortItems(0, Qt.SortOrder.AscendingOrder)
    assert activity.table.item(0, 0).text().startswith("30.09")
    activity.table.sortItems(0, Qt.SortOrder.DescendingOrder)
    assert activity.table.item(0, 0).text().startswith("01.10")


def test_account_list_sorts_names_without_selecting_a_row(window):
    from codex_account_manager.domain.models import Profile

    table_view = window.accounts_view
    table_view._render(
        [
            Profile(alias="Zulu", codex_home="unused", bound_account_id="1"),
            Profile(alias="Alpha", codex_home="unused", bound_account_id="2"),
        ]
    )
    assert list(table_view.table.rows) == ["Alpha", "Zulu"]
    table_view._sort_column(("name", "name_desc"))
    assert list(table_view.table.rows) == ["Zulu", "Alpha"]


def test_account_summary_and_filter_reflect_verified_availability(window):
    view = window.accounts_view
    view._render(sample_profiles())
    assert view.summary.text() == window.dashboard.summary.text()
    assert "4 accounts" in view.summary.text()
    assert not hasattr(view, "action_bar")
    view.status_filter.setCurrentIndex(view.status_filter.findData("ready"))
    shown = list(view.table.rows)
    assert len(shown) == 3
    view.search.setText("no such account")
    assert not view.table.rows


def test_account_usage_sort_and_email_search(window):
    view = window.accounts_view
    view._render(sample_profiles())
    view._sort_column(("five_most", "five_least"))
    assert list(view.table.rows)[0] == "Studio"
    view._sort_column(("five_most", "five_least"))
    assert list(view.table.rows)[0] == "Client workspace"
    view.search.setText("studio@example.com")
    assert list(view.table.rows) == ["Studio"]
    assert view.table.rows["Studio"]._status_host is not None
    view.search.clear()
    window.nav.setCurrentRow(1)
    window.show()
    assert all(row._switch_btn.menu() for row in view.table.rows.values())
    assert not hasattr(view, "action_bar")


def test_loading_never_moves_content_and_hidden_notice_reserves_no_space(window, app):
    window.resize(1600, 940)
    window.show()
    app.processEvents()
    view = window.dashboard
    before = view.geometry()
    table_before = view.account_table.geometry()
    assert view.loading.height() == 4
    assert not window.notice.sizePolicy().retainSizeWhenHidden()
    for visible in (True, False, True, False):
        view.set_loading("regression", visible)
        app.processEvents()
        assert view.geometry() == before
        assert view.account_table.geometry() == table_before


def test_subscription_sort_keeps_unknown_and_stale_accounts_last(window):
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    template = sample_profiles()[0]
    profiles = [
        replace(
            template,
            alias="Later",
            profile_id="later",
            subscription_until=now + timedelta(days=30),
            stale=False,
            error=None,
            account_match=True,
            auth_present=True,
        ),
        replace(
            template,
            alias="Soon",
            profile_id="soon",
            subscription_until=now + timedelta(days=2),
            stale=False,
            error=None,
            account_match=True,
            auth_present=True,
        ),
        replace(template, alias="Unknown", profile_id="unknown", subscription_until=None),
        replace(
            template,
            alias="Stale",
            profile_id="stale",
            subscription_until=now + timedelta(days=1),
            stale=True,
        ),
    ]
    view = window.accounts_view
    view._render(profiles)
    view.sort_order.setCurrentIndex(view.sort_order.findData("subscription_soonest"))
    assert list(view.table.rows)[:2] == ["Soon", "Later"]
    view.sort_order.setCurrentIndex(view.sort_order.findData("subscription_latest"))
    assert list(view.table.rows)[:2] == ["Later", "Soon"]


def test_activity_filters_real_handoffs_and_reports_visible_count(window):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    view = window.activity
    now = datetime.now(UTC)
    view._render(
        [
            SimpleNamespace(started_at=now, reason=SimpleNamespace(value="manual"), success=True),
            SimpleNamespace(
                started_at=now - timedelta(days=20),
                reason=SimpleNamespace(value="usage_limited"),
                success=False,
            ),
        ]
    )
    view.date_filter.setCurrentIndex(view.date_filter.findData(7))
    assert sum(not view.table.isRowHidden(row) for row in range(2)) == 1
    assert "1 of 2" in view.status.text()
    view.result_filter.setCurrentIndex(view.result_filter.findData("failed"))
    assert view.table.isHidden()
    assert "filters" in view.empty.title.text()


def test_activity_shows_account_handoff_and_elapsed_time(window):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    start = datetime.now(UTC)
    record = SimpleNamespace(
        started_at=start,
        finished_at=start + timedelta(seconds=37),
        reason=SimpleNamespace(value="manual"),
        from_profile_id="first",
        to_profile_id="second",
        success=True,
        detail="token=private-value",
    )
    view = window.activity
    view._render(([record], {"first": "Personal", "second": "Studio"}))
    assert view.table.item(0, 2).text() == "Personal → Studio"
    assert view.table.item(0, 4).text() == "00:37"
    view.search.setText("studio")
    assert not view.table.isRowHidden(0)
    assert all(
        "private-value" not in view.table.item(0, column).text()
        for column in range(view.table.columnCount())
    )


def test_work_details_are_optional_and_list_keeps_all_columns(window):
    from codex_account_manager.continuity.tracking import ObservedWork

    view = window.conversations
    view._render_tracking(
        ([ObservedWork("job", "account", "inProgress", "active", True, False, 1)], {})
    )
    assert all(not view.tracked_table.isColumnHidden(column) for column in range(5))
    assert view.detail_scroll.isHidden()
    view.details_button.setChecked(True)
    assert not view.detail_scroll.isHidden()


def test_tracked_jobs_sort_by_observation_time(window):
    from PySide6.QtCore import Qt

    from codex_account_manager.continuity.tracking import ObservedWork

    view = window.conversations
    view._render_tracking(
        (
            [
                ObservedWork("older", "account", "inProgress", "active", True, False, 1),
                ObservedWork("newer", "account", "completed", "complete", True, False, 2),
            ],
            {},
        )
    )
    view.tracked_table.sortItems(4, Qt.SortOrder.DescendingOrder)
    assert view.tracked_table.item(0, 0).data(Qt.ItemDataRole.UserRole) == "newer"
    assert view.tracked_table.cellWidget(0, 5) is not None
    assert view._select_work("older")
    assert (
        view.tracked_table.item(view.tracked_table.currentRow(), 0).data(Qt.ItemDataRole.UserRole)
        == "older"
    )


def test_account_and_work_columns_fill_width_and_keep_manual_resize(window, app):
    from codex_account_manager.continuity.tracking import ObservedWork
    from codex_account_manager.gui.widgets import Pill

    window.resize(1600, 940)
    window.show()
    window.nav.setCurrentRow(0)
    app.processEvents()
    assert (
        window.dashboard.search.height()
        == window.dashboard.account_filter.height()
        == window.dashboard.sort_button.height()
    )
    accounts = window.accounts_view
    accounts._render(sample_profiles())
    window.nav.setCurrentRow(1)
    app.processEvents()
    assert accounts.search.height() == accounts.status_filter.height()
    assert type(accounts.table) is type(window.dashboard.account_table)
    assert (
        accounts.table.actions_heading.text()
        == window.dashboard.account_table.actions_heading.text()
    )
    account_row = next(iter(accounts.table.rows.values()))
    overview_row = window.dashboard._account_rows[account_row.alias]
    assert [
        cell.x()
        for cell in [account_row._identity, *account_row._usage_cells, account_row._status_host]
    ] == [
        cell.x()
        for cell in [overview_row._identity, *overview_row._usage_cells, overview_row._status_host]
    ]
    assert abs(accounts.table.header.width() - accounts.table.scroll_area.viewport().width()) <= 1
    window.resize(1500, 900)
    app.processEvents()
    assert abs(accounts.table.header.width() - accounts.table.scroll_area.viewport().width()) <= 1

    work = window.conversations
    work._render_tracking(
        ([ObservedWork("job", "account", "inProgress", None, False, False, 1)], {})
    )
    window.nav.setCurrentRow(2)
    app.processEvents()
    assert work.work_search.height() == work.work_filter.height()
    assert (
        abs(
            sum(work.tracked_table.columnWidth(i) for i in range(6))
            - work.tracked_table.viewport().width()
        )
        <= 2
    )
    work_badge = work.tracked_table.cellWidget(0, 2).findChild(Pill)
    assert work_badge is not None
    assert (
        abs(
            work_badge.mapTo(work.tracked_table.viewport(), QPoint()).x()
            - (work.tracked_table.columnViewportPosition(2) + 14)
        )
        <= 2
    )
    window.resize(950, 800)
    app.processEvents()
    assert all(
        work.tracked_table.columnWidth(i) >= minimum
        for i, minimum in enumerate((180, 90, 130, 110, 110, 90))
    )
    assert work.tracked_table.horizontalScrollBar().maximum() == 0

    window.resize(1480, 960)
    window.nav.setCurrentRow(5)
    app.processEvents()
    diagnostics = window.diagnostics
    assert (
        abs(
            sum(diagnostics.table.columnWidth(i) for i in range(diagnostics.table.columnCount()))
            - diagnostics.table.viewport().width()
        )
        <= 2
    )


def test_shutdown_duration_and_confirmation_are_turkish(window, monkeypatch):
    from types import SimpleNamespace

    from PySide6.QtWidgets import QMessageBox

    from codex_account_manager.gui import power
    from codex_account_manager.gui.i18n import language, set_language
    from codex_account_manager.gui.power import PowerControls
    from codex_account_manager.monitoring.power import PowerTarget

    monkeypatch.setattr(power, "sys", SimpleNamespace(platform="win32"))
    previous = language()
    set_language("tr")
    try:
        controls = PowerControls(PreviewRunner(), window.accounts)
        assert controls.status.text() == "Otomatik kapatma kapalı."
        assert controls.plan.target is None
        controls.countdown_minutes.setValue(7)
        assert controls.time_fields.isHidden()
        prompts = []

        def decline(_parent, title, message, _buttons, _default):
            prompts.append((title, message))
            return QMessageBox.StandardButton.No

        monkeypatch.setattr(QMessageBox, "question", decline)
        controls.arm(PowerTarget("limits"))
        assert prompts and "2 dakika" in prompts[0][1]
        assert "Otomatik kapatmayı etkinleştir" == prompts[0][0]
        assert controls.plan.target is None
        assert not controls.timer.isActive()
        assert controls.plan.target is None
        monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
        monkeypatch.setattr(controls, "_tick", lambda: None)
        controls.schedule_button.click()
        assert controls.plan.target == PowerTarget("limits")
        assert not controls.countdown_minutes.isEnabled()
        controls.cancel()
        assert controls.plan.target is None
        assert controls.countdown_minutes.isEnabled()
        controls.deleteLater()
    finally:
        set_language(previous)


@pytest.mark.parametrize("minutes", [1, 120, 180])
def test_timed_shutdown_warns_within_total_duration_and_executes_once(window, monkeypatch, minutes):
    from unittest.mock import Mock

    from codex_account_manager.gui import power as gui_power
    from codex_account_manager.monitoring.power import PowerEvidence, PowerTarget
    from codex_account_manager.platform import power

    widget = window.power_view.controls
    clock = [100.0]
    monkeypatch.setattr(gui_power.time, "monotonic", lambda: clock[0])
    shutdown = Mock()
    monkeypatch.setattr(power, "shutdown_windows", shutdown)
    submit = Mock(side_effect=AssertionError("Timer must not query accounts or conversations"))
    monkeypatch.setattr(widget.runner, "submit", submit)
    warning = Mock()
    widget.countdown_started.connect(warning)
    widget.plan.set_seconds(minutes * 60)
    widget.plan.arm(PowerTarget("timer"), now=clock[0])
    widget._tick()
    widget.plan.observe(PowerEvidence(False, "connection lost"), clock[0])
    clock[0] = 100 + max(0, minutes * 60 - 120)
    widget._tick()
    widget._tick()
    warning.assert_called_once()
    shutdown.assert_not_called()
    clock[0] = 100 + minutes * 60
    widget._tick()
    widget._tick()
    shutdown.assert_called_once()
    assert widget.plan.target is None
    submit.assert_not_called()


def test_timer_cancel_and_monitoring_pause_are_independent(window, monkeypatch):
    from unittest.mock import Mock

    from codex_account_manager.monitoring.power import PowerTarget
    from codex_account_manager.platform import power

    widget = window.power_view.controls
    shutdown = Mock()
    monkeypatch.setattr(power, "shutdown_windows", shutdown)
    widget.plan.arm(PowerTarget("timer"), now=time.monotonic())
    window._monitor_enabled = True
    window._monitoring_changed(False)
    assert widget.plan.target.mode == "timer"
    widget.cancel()
    widget._tick()
    shutdown.assert_not_called()


def test_shutdown_fields_only_show_relevant_inputs(window):
    widget = window.power_view.controls
    for mode in ("work", "limits", "timer"):
        widget.mode.setCurrentIndex(widget.mode.findData(mode))
        assert widget.time_fields.isHidden() == (mode != "timer")
        assert widget.work_fields.isHidden() == (mode != "work")


def test_shutdown_cards_and_presets_review_without_starting(window):
    from codex_account_manager.gui.i18n import tr

    widget = window.power_view.controls
    widget.mode_buttons.button(2).click()
    assert widget.mode.currentData() == "timer"
    for minutes in (30, 60, 120, 120):
        widget.presets[minutes].click()
        assert widget.countdown_minutes.value() == minutes
        assert [value for value, button in widget.presets.items() if button.isChecked()] == [
            minutes
        ]
    widget.countdown_minutes.setValue(147)
    assert not any(button.isChecked() for button in widget.presets.values())
    assert widget.summary_values["Target"].text() == tr(
        "{hours} hr {minutes} min", hours=2, minutes=27
    )
    assert widget.plan.target is None
    assert not widget.timer.isActive()
    widget.mode_buttons.button(1).click()
    assert widget.mode.currentData() == "limits"
    assert widget.summary_values["Target"].text() == tr("All saved accounts")


def test_shutdown_conversation_error_does_not_leak_into_timer_mode(window, monkeypatch):
    from codex_account_manager.gui.i18n import tr

    widget = window.power_view.controls
    callbacks = []

    def submit(coro, on_result=None, on_error=None):
        coro.close()
        callbacks.append(on_error)

    monkeypatch.setattr(widget.runner, "submit", submit)
    widget.mode_buttons.button(0).click()
    assert widget.status.text() == tr("Loading conversations…")
    widget.mode_buttons.button(2).click()
    callbacks.pop()(RuntimeError("offline"))
    assert widget.status.text() == tr("Shutdown is off.")
    assert widget.schedule_button.isEnabled() == (os.name == "nt")
    assert widget.plan.target is None


def test_shutdown_summary_and_editing_follow_confirm_and_cancel(window, monkeypatch):
    from types import SimpleNamespace

    from PySide6.QtWidgets import QMessageBox

    from codex_account_manager.gui import power
    from codex_account_manager.gui.i18n import tr

    monkeypatch.setattr(power, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(power.time, "monotonic", lambda: 100.0)
    widget = window.power_view.controls
    widget.mode_buttons.button(2).click()
    widget.presets[30].click()
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.No)
    widget.schedule_button.click()
    assert widget.plan.target is None
    assert widget.clock.isHidden()
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    widget.schedule_button.click()
    assert widget.plan.target.mode == "timer"
    assert widget.summary_values["Status"].text() == tr("Countdown active")
    assert widget.clock.text() == "00:30:00"
    assert not widget.clock.isHidden()
    assert not any(button.isEnabled() for button in widget.mode_buttons.buttons())
    assert not any(button.isEnabled() for button in widget.presets.values())
    widget.cancel_button.click()
    assert widget.plan.target is None
    assert not widget.timer.isActive()
    assert widget.clock.isHidden()
    assert widget.summary_values["Status"].text() == tr("Not started")
    assert all(button.isEnabled() for button in widget.mode_buttons.buttons())
    assert all(button.isEnabled() for button in widget.presets.values())


@pytest.mark.parametrize("locale", ["en", "tr"])
@pytest.mark.parametrize("dark", [True, False])
def test_shutdown_controls_are_reachable_in_small_window(app, locale, dark):
    from PySide6.QtCore import QPoint

    from codex_account_manager.gui.i18n import set_language

    set_language(locale)
    window = MainWindow(PreviewRunner())
    try:
        window._apply_theme(dark)
        window.resize(1040, 700)
        window.nav.setCurrentRow(window.stack.indexOf(window.power_view))
        window.show()
        app.processEvents()
        area = window.power_view.scroll_area
        widget = window.power_view.controls
        for mode in range(3):
            widget.mode_buttons.button(mode).click()
            app.processEvents()
            assert area.horizontalScrollBar().maximum() == 0
            for control in (
                widget.mode_buttons.button(mode),
                widget.schedule_button,
                widget.cancel_button,
            ):
                area.ensureWidgetVisible(control)
                app.processEvents()
                position = control.mapTo(area.viewport(), QPoint())
                assert area.viewport().rect().contains(position)
                assert area.viewport().rect().contains(position + control.rect().bottomRight())
            assert widget.plan.target is None
    finally:
        window.dispose()
        window.close()
        app.processEvents()
        set_language("en")


def test_cancelled_work_preparation_cannot_arm_a_plan(window, monkeypatch):
    from unittest.mock import Mock

    from codex_account_manager.monitoring.power import PowerTarget

    widget = window.power_view.controls
    callbacks = []

    def submit(coro, on_result=None, on_error=None):
        coro.close()
        callbacks.append(on_result)

    monkeypatch.setattr(widget.runner, "submit", submit)
    arm = Mock()
    monkeypatch.setattr(widget, "arm", arm)
    widget.arm_work("thread")
    widget.cancel()
    callbacks.pop()(PowerTarget("work", "thread", None, "turn"))
    arm.assert_not_called()
    assert widget.schedule_button.isEnabled() == (os.name == "nt")


@pytest.mark.parametrize("locale", ["tr", "en"])
@pytest.mark.parametrize("next_year", [False, True])
def test_subscription_badge_uses_local_dates_and_hides_unreliable_metadata(app, locale, next_year):
    from dataclasses import replace
    from datetime import datetime

    from codex_account_manager.gui.i18n import set_language
    from codex_account_manager.gui.widgets import AccountRow

    now = datetime.now().astimezone()
    until = now.replace(year=now.year + int(next_year), month=12, day=31, hour=23, minute=59)
    health = replace(
        sample_profiles()[0],
        plan_type="plus",
        account_match=True,
        subscription_until=until,
        subscription_checked_at=now,
        stale=False,
    )
    set_language(locale)
    try:
        card = AccountRow(health)
        expected = "31.12" if locale == "tr" else "12/31"
        if next_year:
            expected += ("." if locale == "tr" else "/") + until.strftime("%y")
        assert card._plan.text() == "Plus · " + expected
        assert str(until.year) in card._plan.toolTip()
        for changes in ({"stale": True}, {"account_match": False}, {"subscription_until": now}):
            hidden = AccountRow(replace(health, **changes))
            assert hidden._plan.text() == "Plus"
            hidden.deleteLater()
        card.deleteLater()
    finally:
        set_language("en")


def test_account_controls_match_and_privacy_hides_email_search(window):
    view = window.accounts_view
    view._render(sample_profiles())
    dashboard = window.dashboard
    assert view.summary.text() == dashboard.summary.text()
    assert view.search.placeholderText() == dashboard.search.placeholderText()
    for left, right in (
        (view.status_filter, dashboard.account_filter),
        (view.sort_order, dashboard.sort_order),
    ):
        assert [(left.itemText(i), left.itemData(i)) for i in range(left.count())] == [
            (right.itemText(i), right.itemData(i)) for i in range(right.count())
        ]
    view.email_toggle.setChecked(True)
    view.search.setText("studio@example.com")
    assert not view.table.rows
    view.email_toggle.setChecked(False)
    assert list(view.table.rows) == ["Studio"]
