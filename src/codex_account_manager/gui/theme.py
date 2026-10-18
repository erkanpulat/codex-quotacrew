"""Qt stylesheet generated from the design palette (dark/light)."""

from __future__ import annotations

from codex_account_manager.gui.design import DARK, LIGHT, RADIUS, Palette


def _sheet(p: Palette) -> str:
    success_bg = "#e0f6e9" if p is LIGHT else "#153a2c"
    warning_bg = "#fff0da" if p is LIGHT else "#49351a"
    primary_bg = "#e5f0ff" if p is LIGHT else "#1b3454"
    content_bg = p.bg
    sidebar_bg = p.surface
    card_bg = p.surface
    selected_bg = "#19385f" if p is DARK else p.surface_alt
    plan_bg = "#1d58b6" if p is DARK else p.surface_alt
    plan_text = "#ffffff" if p is DARK else p.muted
    status_halo = "#174b42" if p is DARK else "#dff5e9"
    return f"""
* {{
    font-family: "Segoe UI", sans-serif;
    font-size: 14px;
    color: {p.text};
}}
QMainWindow, QDialog, QMessageBox {{ background: {p.bg}; }}
QWidget#Root {{ background: {p.bg}; }}
QWidget#Sidebar {{
    background: {sidebar_bg};
    border-right: 1px solid {p.border};
}}
QWidget#Content {{ background: {content_bg}; border: none; }}
QWidget#GridHost {{ background: transparent; border: none; }}
QWidget#SetupSidebar {{ background: {sidebar_bg}; border-right: 1px solid {p.border}; }}
QFrame#SetupConnector {{ background: {p.border}; }}
QDialog#SetupDialog QLabel#Caption {{ font-size: 14px; }}
QDialog#SetupDialog QLabel#SetupStepLabel {{ font-size: 17px; }}
QDialog#SetupDialog QFrame#SettingRow QLabel#FieldTitle {{ font-size: 16px; }}
QDialog#SetupDialog QFrame#SettingRow {{ border-bottom: 1px solid {p.border}; }}
QDialog#SetupDialog QPushButton#Primary {{ min-width: 96px; min-height: 28px; }}
QLabel#SetupMark {{ color: {p.primary}; font-size: 48px; font-weight: 800; }}
QLabel#SetupBrand {{ font-size: 23px; font-weight: 700; }}
QLabel#SetupTitle {{ font-size: 32px; font-weight: 700; }}
QLabel#SetupStepNumber {{ border: 1px solid {p.border}; border-radius: 18px; color: {p.muted}; font-size: 17px; font-weight: 600; }}
QLabel#SetupStepNumber[state="current"] {{ background: {p.primary}; color: {p.on_primary}; border-color: {p.primary}; }}
QLabel#SetupStepNumber[state="done"] {{ color: {p.success}; border-color: {p.success}; }}
QLabel#SetupStepLabel {{ font-size: 16px; color: {p.muted}; }}
QLabel#SetupStepLabel[state="current"] {{ color: {p.text}; font-weight: 600; }}
QFrame#SetupCard {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 12px; }}
QLabel#SetupCardTitle {{ font-size: 24px; font-weight: 600; }}
QLabel#CliIcon {{ background: {p.bg}; border-radius: 12px; font-size: 30px; font-weight: 600; }}
QLabel#SetupPhase {{ border-top: 1px solid {p.border}; padding-top: 14px; color: {p.muted}; }}
QLabel#SetupPhase[state="current"] {{ color: {p.primary}; }}
QLabel#SetupPhase[state="done"] {{ color: {p.success}; }}
QPushButton#SetupLink {{ background: transparent; border: none; color: {p.primary}; padding: 0; }}
QProgressBar#CliProgress::chunk {{ background: {p.primary}; border-radius: 4px; }}
QLabel#CliStatus {{ color: {p.primary}; font-size: 16px; }}
QFrame#SetupPhaseRow {{ border-top: 1px solid {p.border}; }}
QLabel#SetupPhaseMark {{ color: {p.muted}; font-size: 32px; border-radius: 19px; }}
QLabel#SetupPhaseMark[state="done"] {{ background: {p.success}; color: {p.bg}; font-size: 24px; }}
QLabel#SetupPhaseMark[state="current"] {{ color: {p.primary}; }}

QFrame#Topbar {{
    background: {card_bg};
    border: 1px solid {p.border}; border-radius: 10px;
}}
QLabel#StatusDot {{ font-size: 24px; color: {p.muted}; background: {status_halo}; border-radius: 21px; }}
QLabel#StatusDot[active="true"] {{ color: {p.success}; }}
QFrame#Topbar QLabel#FieldTitle {{ font-size: 15px; }}
QFrame#Topbar QLabel#Caption {{ font-size: 13px; }}
QPushButton#MonitorButton {{ background: transparent; font-size: 14px; }}
QFrame#DetailRow {{ border-bottom: 1px solid {p.border}; }}
QSplitter::handle {{ background: transparent; width: 16px; }}
QPushButton#SidebarAction {{ text-align: left; background: transparent; border: none; padding: 12px 12px; min-height: 32px; font-size: 14px; }}
QPushButton#SidebarAction:hover {{ background: {p.surface_alt}; }}
QPushButton#SidebarAction:checked {{ background: {selected_bg}; color: {p.primary}; border-left: 3px solid {p.primary}; }}
QLabel#HeroTitle {{ font-size: 22px; font-weight: 600; }}
QLabel#H1 {{ font-size: 26px; font-weight: 600; }}
QLabel#H2 {{ font-size: 18px; font-weight: 600; }}
QFrame#PowerControls {{ background: {p.bg}; }}
QPushButton#PowerChoice {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 12px; padding: 0; }}
QPushButton#PowerChoice:checked {{ background: {primary_bg}; border: 2px solid {p.primary}; }}
QPushButton#PowerChoice:hover {{ border-color: {p.primary}; }}
QPushButton#PowerChoice QLabel {{ background: transparent; }}
QPushButton#PowerChoice QLabel#FieldTitle {{ font-size: 16px; }}
QPushButton#PowerPreset:checked {{ background: {primary_bg}; color: {p.primary}; border-color: {p.primary}; }}
QLabel#PowerChoiceMark {{ color: {p.primary}; font-size: 22px; }}
QLabel#PowerClock {{ color: {p.primary}; font-size: 32px; font-weight: 600; }}
QLabel#Step {{ font-size: 24px; font-weight: 700; color: {p.primary}; letter-spacing: -0.5px; }}

QLabel#Eyebrow {{ color: {p.muted}; font-size: 12px; font-weight: 700; letter-spacing: 0.3px; }}
QLabel#Section {{ color: {p.muted}; font-size: 12px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; }}
QLabel#Caption {{ color: {p.muted}; font-size: 13px; letter-spacing: 0.2px; }}
QLabel#PlanBadge {{ background: {plan_bg}; color: {plan_text}; border: none; border-radius: 9px; padding: 2px 6px; font-size: 12px; font-weight: 600; }}
QLabel#Pill {{ border: 1px solid {p.border}; border-radius: 7px; padding: 4px 8px; font-size: 12px; font-weight: 600; letter-spacing: 0px; }}
QLabel#Pill[tone="success"] {{ color: {p.success}; background: {success_bg}; border: none; }}
QLabel#Pill[tone="warning"] {{ color: {p.warning}; background: {warning_bg}; border: none; }}
QLabel#Pill[tone="danger"] {{ color: {p.danger}; border-color: {p.danger}; }}
QLabel#Pill[tone="primary"] {{ color: {p.primary}; background: {primary_bg}; border: none; }}
QLabel#Pill[tone="muted"] {{ color: {p.muted}; }}

QLabel#Body {{ font-size: 14px; color: {p.muted}; letter-spacing: 0px; line-height: 1.5; }}
QLabel#FieldTitle {{ font-size: 14px; font-weight: 600; letter-spacing: 0.2px; }}
QMenu {{ background: {p.surface}; border: 1px solid {p.border}; padding: 6px; }}
QMenu::item {{ padding: 8px 24px; }}
QMenu::item:selected {{ background: {p.surface_alt}; }}
QLabel#Brand {{ font-size: 17px; font-weight: 700; letter-spacing: -0.3px; }}

QPushButton {{
    background: {p.surface_alt};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 8px 14px;
    min-height: 22px;
    color: {p.text};
}}
QPushButton:focus {{ border-color: {p.primary}; }}
QPushButton:hover {{ border-color: {p.primary}; }}
QPushButton:disabled {{ color: {p.muted}; border-color: {p.border}; }}
QPushButton#Primary {{ background: {p.primary}; border: none; color: {p.on_primary}; font-weight: 600; font-size: 14px; }}
QPushButton#Primary:hover {{ background: {p.primary_hover}; }}
QPushButton#Primary:disabled {{ background: {p.surface_alt}; color: {p.muted}; }}
QPushButton#Danger {{ color: {p.danger}; border-color: {p.border}; }}
QPushButton#Danger:hover {{ border-color: {p.danger}; }}
QPushButton#Ghost {{ background: transparent; border: none; color: {p.muted}; padding: 6px; }}
QPushButton#Ghost:hover {{ color: {p.text}; }}

QListWidget#Nav {{ background: transparent; border: none; outline: 0; padding: 0; }}
QListWidget#Nav::item {{
    padding: 10px 12px; border-radius: 8px; color: {p.muted}; margin: 3px 0; font-size: 15px;
}}
QListWidget#Nav::item:selected {{ background: {selected_bg}; color: {p.primary}; border-left: 3px solid {p.primary}; }}
QListWidget#Nav::item:hover {{ color: {p.text}; }}

QWidget#AccountTable {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 10px; }}
QScrollArea#AccountTableScroll {{ background: {p.surface}; border: none; }}
QWidget#AccountHeader {{ background: {p.surface_alt}; border: none; border-bottom: 1px solid {p.border}; min-height: 43px; }}
QFrame#AccountRow {{ background: {p.surface}; border: none; border-bottom: 1px solid {p.border}; }}
QFrame#AccountsPanel {{ background: {card_bg}; border: 1px solid {p.border}; border-radius: 10px; }}
QFrame#MetricCard {{ background: transparent; border: none; }}
QLabel#MetricValue {{ color: {p.text}; font-size: 14px; font-weight: 700; }}
QLabel#MetricCaption {{ color: {p.muted}; font-size: 13px; }}
QFrame#PagePanel, QWidget#PagePanel {{
    background: {card_bg}; border: 1px solid {p.border}; border-radius: 10px;
}}
QLabel#WorkIcon {{ background: {p.surface_alt}; border-radius: 10px; }}
QLabel#AccountAvatar {{ background: #c9f5e3; color: #108b60; border-radius: 22px; font-size: 20px; font-weight: 700; }}
QLabel#AccountAvatar[variant="1"] {{ background: #ede0ff; color: #7729b9; }}
QLabel#AccountAvatar[variant="2"] {{ background: #dcf4e9; color: #04925a; }}
QToolButton#RowMenu {{ border: none; background: transparent; font-size: 23px; color: {p.muted}; padding: 0; }}
QToolButton#RowMenu:hover {{ color: {p.primary}; }}
QToolButton#RowMenu::menu-indicator {{ image: none; width: 0px; }}
QToolButton#RowMore {{ background: transparent; border: none; border-radius: 8px; padding: 6px; }}
QToolButton#RowMore:hover {{ background: {p.surface_alt}; }}
QToolButton#RowMenu:focus, QToolButton#RowMore:focus {{ border: 1px solid {p.primary}; border-radius: 6px; }}
QToolButton#RowMore::menu-indicator {{ image: none; width: 0px; }}
QPushButton#TableHeading {{ text-align: left; background: transparent; border: none; color: {p.text}; padding: 8px 0; font-size: 13px; font-weight: 600; }}
QPushButton#TableHeading:hover {{ color: {p.primary}; }}
QLabel#TableColumn {{ color: {p.text}; padding: 8px 0; font-size: 13px; font-weight: 600; }}
QPushButton#InlineAction {{ background: transparent; border: none; text-align: left; color: {p.muted}; padding: 0; font-size: 13px; }}
QPushButton#CreditLink {{ background: transparent; border: none; text-align: left; color: {p.primary}; padding: 0; font-size: 13px; }}
QPushButton#CreditLink:hover {{ color: {p.primary_hover}; }}
QLabel#Muted {{ color: {p.muted}; }}
QLabel#AccountStatus {{ font-size: 13px; }}
QLabel#AccountStatus[tone="warning"] {{ color: {p.warning}; }}
QLabel#AccountStatus[tone="primary"] {{ color: {p.primary}; }}
QLabel#InlineWarning {{ background: {p.surface_alt}; border: 1px solid {p.warning}; border-radius: 8px; padding: 12px; }}
QLabel#Toast {{ background: {p.surface}; color: {p.text}; border: 1px solid {p.primary}; border-radius: 8px; padding: 14px; }}
QFrame#NoticeBanner {{ background: {p.surface_alt}; border-bottom: 1px solid {p.warning}; }}
QFrame#PowerBanner {{ background: {p.surface_alt}; border: 1px solid {p.warning}; border-radius: 8px; }}
QFrame#Card {{ background: {card_bg}; border: 1px solid {p.border}; border-radius: {RADIUS}px; }}
QFrame#Card[active="true"] {{ border: 1px solid {p.primary}; }}
QFrame#Panel {{ background: {card_bg}; border: 1px solid {p.border}; border-radius: {RADIUS}px; }}

QProgressBar {{
    background: {p.track}; border: none; border-radius: 5px; height: 8px;
    text-align: center; color: transparent;
}}
QProgressBar::chunk {{ border-radius: 5px; }}
QProgressBar#LoadingProgress {{ border-radius: 2px; }}
QProgressBar#LoadingProgress::chunk {{ background: {p.primary}; border-radius: 2px; }}

QTableWidget {{
    background: {p.surface}; gridline-color: {p.border};
    border: 1px solid {p.border}; border-radius: {RADIUS}px;
    selection-background-color: {selected_bg}; selection-color: {p.text}; outline: 0;
}}
QHeaderView {{ background: {p.surface}; }}
QHeaderView::section {{
    background: {p.surface_alt}; color: {p.text}; border: none;
    border-bottom: 1px solid {p.border}; padding: 10px 12px; font-size: 13px; font-weight: 600;
}}
QTableWidget::item {{ padding: 8px 12px; border-bottom: 1px solid {p.border}; }}
QTableWidget::item:selected {{ background: {selected_bg}; border-bottom: 1px solid {p.border}; }}
QTableCornerButton::section {{ background: {p.surface}; border: none; }}

QComboBox {{ background: {p.surface_alt}; border: 1px solid {p.border}; border-radius: 8px; padding: 8px 24px 8px 10px; min-height: 22px; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ image: none; }}
QComboBox:hover {{ border-color: {p.primary}; }}
QComboBox QAbstractItemView {{
    background: {p.surface}; border: 1px solid {p.border};
    selection-background-color: {p.surface_alt}; outline: 0;
}}
QCheckBox {{
    spacing: 12px;
}}
QCheckBox::indicator {{
    width: 44px;
    height: 24px;
    border-radius: 12px;
    border: 2px solid transparent;
    background: {p.border};
}}
QCheckBox::indicator:checked {{
    background: {p.primary};
}}
QPlainTextEdit {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 8px; padding: 8px; }}
QSpinBox {{ background: {p.surface_alt}; border: 1px solid {p.border}; border-radius: 8px; padding: 8px; }}
QLineEdit {{ background: {p.surface_alt}; border: 1px solid {p.border}; border-radius: 8px; padding: 8px 10px; min-height: 22px; }}
QPlainTextEdit:focus, QSpinBox:focus, QComboBox:focus, QLineEdit:focus {{ border-color: {p.primary}; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {p.border}; border-radius: 5px; min-width: 30px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {p.border}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {p.muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QStatusBar {{ background: {p.surface}; border-top: 1px solid {p.border}; color: {p.muted}; }}
QToolTip {{ background: {p.surface_alt}; color: {p.text}; border: 1px solid {p.border}; padding: 6px; }}
QFrame#SettingRow {{ border-bottom: 1px solid {p.border}; }}

QTabWidget::pane {{
    border: none;
    background: transparent;
}}
QTabBar::tab {{
    background: transparent;
    color: {p.muted};
    padding: 10px 24px;
    font-size: 15px;
    font-weight: 600;
    border-bottom: 2px solid transparent;
    margin-right: 16px;
}}
QTabBar::tab:selected {{
    color: {p.text};
    border-bottom: 2px solid {p.primary};
}}
QTabBar::tab:hover:!selected {{
    color: {p.text};
}}
"""


def stylesheet(dark: bool = True) -> str:
    return _sheet(DARK if dark else LIGHT)


def application_palette(dark: bool = True):
    from PySide6.QtGui import QColor, QPalette

    colors = DARK if dark else LIGHT
    palette = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, colors.bg),
        (QPalette.ColorRole.WindowText, colors.text),
        (QPalette.ColorRole.Base, colors.surface),
        (QPalette.ColorRole.AlternateBase, colors.surface_alt),
        (QPalette.ColorRole.Text, colors.text),
        (QPalette.ColorRole.Button, colors.surface_alt),
        (QPalette.ColorRole.ButtonText, colors.text),
        (QPalette.ColorRole.Highlight, colors.primary),
        (QPalette.ColorRole.HighlightedText, colors.on_primary),
        (QPalette.ColorRole.ToolTipBase, colors.surface_alt),
        (QPalette.ColorRole.ToolTipText, colors.text),
        (QPalette.ColorRole.Mid, colors.border),
    ):
        palette.setColor(role, QColor(color))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(colors.muted))
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(colors.muted)
    )
    return palette
