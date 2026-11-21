import ast
from datetime import datetime
from pathlib import Path
from string import Formatter

import pytest

from codex_account_manager.domain.states import GoalState, HandoffReason
from codex_account_manager.gui import i18n
from codex_account_manager.gui.i18n import (
    EN,
    TR,
    diagnostic_detail,
    diagnostic_name,
    format_timestamp,
    language,
    set_language,
    state_label,
    tr,
)
from codex_account_manager.gui.locales import LOCALES, language_for_locale


@pytest.fixture(autouse=True)
def restore_language():
    previous = language()
    yield
    set_language(previous)


def fields(text):
    return sorted(
        (field, spec, conv) for _, field, spec, conv in Formatter().parse(text) if field is not None
    )


@pytest.mark.parametrize("locale", ["tr", "pt_BR", "es", "ja", "de"])
def test_translation_placeholders_match_source(locale):
    for source, translation in i18n._CATALOGS[locale].items():
        assert translation.strip(), source
        assert fields(source) == fields(translation), source


def test_language_switch_and_dynamic_messages():
    set_language("tr")
    assert tr("Accounts") == "Hesaplarım"
    assert tr("Switching to {alias}…", alias="İş") == "İş hesabına geçiliyor…"
    assert diagnostic_name("codex_version") == "Codex sürümü"
    assert tr("Desktop readiness timed out.") == "Codex Desktop belirtilen sürede hazır olmadı."
    for state in (*GoalState, *HandoffReason):
        assert state_label(state.value) != state.value
    assert "codex --version" in diagnostic_detail("codex_version", False, "Timeout")
    set_language("unsupported")
    assert language() == "en"
    assert tr("Accounts") == "My accounts"


def test_improved_english_copy_has_turkish_equivalents():
    assert EN.keys() <= TR.keys()


@pytest.mark.parametrize(
    "os_locale,expected",
    [
        ("pt_BR", "pt_BR"),
        ("pt_PT", "pt_BR"),
        ("pt-BR", "pt_BR"),
        ("tr_TR", "tr"),
        ("en_US", "en"),
        ("es_MX", "es"),
        ("es-ES", "es"),
        ("ja_JP", "ja"),
        ("de_DE", "de"),
        ("de-AT", "de"),
        ("fr_FR", "en"),
        ("", "en"),
    ],
)
def test_language_for_locale(os_locale, expected):
    assert language_for_locale(os_locale) == expected


@pytest.mark.parametrize(
    "locale,full,short",
    [
        ("en", "02.01.2027", "02.01"),
        ("tr", "02.01.2027", "02.01"),
        ("pt_BR", "02.01.2027", "02.01"),
        ("es", "02/01/2027", "02/01"),
        ("ja", "2027/01/02", "01/02"),
        ("de", "02.01.2027", "02.01."),
    ],
)
def test_timestamp_format_preserves_existing_dates_and_localizes_new_languages(locale, full, short):
    set_language(locale)
    timestamp = datetime(2027, 1, 2, 15, 4, 5)
    for value in (timestamp, timestamp.astimezone()):
        assert format_timestamp(value) == f"{full} 15:04"
        assert format_timestamp(value, year=False, seconds=True) == f"{short} 15:04:05"


@pytest.mark.parametrize(
    "selected,label",
    [
        ("en", "English"),
        ("tr", "Türkçe"),
        ("pt_BR", "Português (Brasil)"),
        ("es", "Español"),
        ("ja", "日本語"),
        ("de", "Deutsch"),
    ],
)
def test_settings_select_each_language(qt_app, selected, label):
    from scripts.render_preview import PreviewRunner

    from codex_account_manager.gui.settings import SettingsView

    set_language(selected)
    view = SettingsView(PreviewRunner(), lambda _dark: None)
    try:
        assert view.language.currentData() == selected
        assert view.language.currentText() == label
        assert view.language.count() == len(LOCALES)
    finally:
        view.close()
        view.deleteLater()


def test_catalogs_cover_gui_strings_and_state_labels():
    assert LOCALES.keys() == i18n._CATALOGS.keys()
    messages = set(TR)
    messages.update(english for english, _ in i18n._STATE_LABELS.values())
    for path in Path(i18n.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "tr"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                messages.add(node.args[0].value)
    for locale in ("pt_BR", "es", "ja", "de"):
        assert messages <= i18n._CATALOGS[locale].keys(), locale


def test_portuguese_diagnostics_and_fallback():
    set_language("pt_BR")
    assert tr("Weekly remaining") == "Restante na semana"
    assert tr("Accounts") == "Minhas contas"
    assert tr("Bind account") == "Verificar conta novamente"
    assert tr("Saved objectives and their continuity status") == (
        "Notas locais não são objetivos do Codex. Salvar uma nota não inicia trabalho, "
        "recria um objetivo nativo nem redefine seu orçamento."
    )
    assert tr("{count} accounts", count=3) == "3 contas"
    assert state_label("completed") == "Concluído"
    assert diagnostic_name("codex_cli") == "Instalação do Codex CLI"
    assert diagnostic_name("profile:Example") == "Conta: Example"
    assert tr("Future unknown message") == "Future unknown message"
