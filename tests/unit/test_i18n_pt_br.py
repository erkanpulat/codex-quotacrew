import ast
import os
from pathlib import Path
from string import Formatter

import pytest

from codex_account_manager.gui import i18n
from codex_account_manager.gui.i18n_pt_br import PT_BR

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def restore_language():
    previous = i18n.language()
    yield
    i18n.set_language(previous)


@pytest.mark.parametrize(
    "locale, expected",
    [
        ("pt_BR", "pt_BR"),
        ("pt_PT", "pt_BR"),
        ("pt-BR", "pt_BR"),
        ("tr_TR", "tr"),
        ("en_US", "en"),
        ("de_DE", "en"),
        ("", "en"),
    ],
)
def test_language_for_locale(locale, expected):
    assert i18n.language_for_locale(locale) == expected


@pytest.mark.parametrize("selected", ["en", "tr", "pt_BR"])
def test_settings_offer_and_select_portuguese(qt_app, restore_language, selected):
    from scripts.render_preview import PreviewRunner

    from codex_account_manager.gui.settings import SettingsView

    i18n.set_language(selected)
    view = SettingsView(PreviewRunner(), lambda _dark: None)
    try:
        assert view.language.currentData() == selected
        index = view.language.findData("pt_BR")
        assert index >= 0
        assert view.language.itemText(index) == "Português (Brasil)"
    finally:
        view.close()
        view.deleteLater()


def fields(text):
    return sorted(
        (field, spec, conv) for _, field, spec, conv in Formatter().parse(text) if field is not None
    )


def test_catalog_preserves_all_format_fields():
    for source, translation in PT_BR.items():
        assert fields(source) == fields(translation), source


def test_catalog_covers_source_strings_and_state_labels():
    source_dir = Path(i18n.__file__).parent
    for path in source_dir.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "tr"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                assert node.args[0].value in PT_BR, (path.name, node.args[0].value)
    assert set(i18n.TR) <= set(PT_BR)
    for english, _ in i18n._STATE_LABELS.values():
        assert english in PT_BR


def test_portuguese_selection_diagnostics_and_fallback():
    previous = i18n.language()
    try:
        i18n.set_language("pt_BR")
        assert i18n.language() == "pt_BR"
        assert i18n.tr("Weekly remaining") == "Restante na semana"
        assert i18n.tr("Accounts") == "Minhas contas"
        assert i18n.tr("Bind account") == "Verificar conta novamente"
        assert i18n.tr("Saved objectives and their continuity status") == (
            "Notas locais não são objetivos do Codex. Salvar uma nota não inicia trabalho, "
            "recria um objetivo nativo nem redefine seu orçamento."
        )
        assert i18n.tr("{count} accounts", count=3) == "3 contas"
        assert i18n.state_label("completed") == "Concluído"
        assert i18n.diagnostic_name("codex_cli") == "Instalação do Codex CLI"
        assert i18n.diagnostic_name("profile:Example") == "Conta: Example"
        assert i18n.tr("Future unknown message") == "Future unknown message"
        i18n.set_language("invalid")
        assert i18n.language() == "en"
    finally:
        i18n.set_language(previous)
