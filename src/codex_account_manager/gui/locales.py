"""Supported interface languages and their Windows packaging metadata."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Locale:
    name: str
    store_tag: str
    qt_catalog: str | None
    date_formats: tuple[str, str, str]
    timestamp_dates: tuple[str, str] | None = None


# Dates for account badges: current year, another year, and the full tooltip date.
_LEGACY_TIMESTAMP_DATES = ("%d.%m", "%d.%m.%Y")
LOCALES = {
    "en": Locale(
        "English", "en-US", None, ("%m/%d", "%m/%d/%y", "%m/%d/%Y"), _LEGACY_TIMESTAMP_DATES
    ),
    "tr": Locale("Türkçe", "tr-TR", "tr", ("%d.%m", "%d.%m.%y", "%d.%m.%Y")),
    "pt_BR": Locale(
        "Português (Brasil)",
        "pt-BR",
        "pt_BR",
        ("%d/%m", "%d/%m/%y", "%d/%m/%Y"),
        _LEGACY_TIMESTAMP_DATES,
    ),
    "es": Locale("Español", "es", "es", ("%d/%m", "%d/%m/%y", "%d/%m/%Y")),
    "ja": Locale("日本語", "ja-JP", "ja", ("%m/%d", "%Y/%m/%d", "%Y/%m/%d")),
    "de": Locale("Deutsch", "de-DE", "de", ("%d.%m.", "%d.%m.%y", "%d.%m.%Y")),
}


def language_for_locale(locale_name: str) -> str:
    """Map a Windows or Qt locale to a supported interface language."""
    prefix = locale_name.replace("-", "_").split("_", 1)[0].lower()
    if prefix == "pt":
        return "pt_BR"
    return prefix if prefix in LOCALES else "en"
