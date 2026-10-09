"""Dark theme: palette tokens plus the stylesheet built from them."""
from __future__ import annotations

import sys

BG = "#0d0f13"
SURFACE = "#14171d"
RAISED = "#1a1e26"
HOVER = "#1f242e"
BORDER = "#262c36"
BORDER_SOFT = "#1e232b"

TEXT = "#e6e9ef"
MUTED = "#8b93a3"
DIM = "#5d6575"

ACCENT = "#6c8cff"
ACCENT_HOVER = "#8199ff"
ACCENT_PRESSED = "#5a78e6"
SUCCESS = "#3ecf8e"
WARN = "#f0b429"
DANGER = "#f0616d"

MONO = "Cascadia Mono, Consolas, monospace"
SANS = "Segoe UI Variable Display, Segoe UI, Inter, sans-serif"

BADGE_COLORS = {
    "VISION": ("#1b3a5c", "#6cb8ff"),
    "MTP": ("#15402f", "#4fd9a0"),
    "DRAFT": ("#3d3218", "#f0c05a"),
    "SPLIT": ("#2f2740", "#b79cff"),
}


def stylesheet() -> str:
    return f"""
* {{
    font-family: {SANS};
    font-size: 13px;
    color: {TEXT};
}}
QWidget {{ background: transparent; }}
QMainWindow, #Root {{ background: {BG}; }}

#Header {{
    background: {SURFACE};
    border-bottom: 1px solid {BORDER_SOFT};
}}
#HeaderTitle {{ font-size: 15px; font-weight: 600; letter-spacing: 0.2px; }}
#HeaderSub {{ color: {DIM}; font-size: 11px; }}

#Panel {{
    background: {SURFACE};
    border: 1px solid {BORDER_SOFT};
    border-radius: 12px;
}}
#Card {{
    background: {RAISED};
    border: 1px solid {BORDER_SOFT};
    border-radius: 10px;
}}
#CardTitle {{
    font-size: 11px;
    font-weight: 700;
    color: {MUTED};
    letter-spacing: 1.1px;
}}
#SectionLabel {{ color: {MUTED}; font-size: 12px; }}
#SectionLabel[overridden="true"] {{ color: {ACCENT}; }}
#ResetOverride {{ padding: 0px; min-height: 0px; }}
/* Narrow one-glyph buttons: the normal side padding would leave no room for the glyph. */
QPushButton[glyph="true"] {{ padding: 7px 0px; }}
#Hint {{ color: {DIM}; font-size: 11px; }}
#Mono {{ font-family: {MONO}; font-size: 12px; }}

QLabel[role="value"] {{ color: {TEXT}; font-weight: 600; }}
QLabel[role="warn"] {{ color: {WARN}; }}
QLabel[role="danger"] {{ color: {DANGER}; }}
QLabel[role="ok"] {{ color: {SUCCESS}; }}

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 6px 10px;
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {{
    border-color: #323a47;
}}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QLineEdit::placeholder {{ color: {DIM}; }}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{
    color: {DIM};
    background: #101318;
    border-color: {BORDER_SOFT};
}}

/* The chevron is painted by ui.widgets.Combo, so the native arrow is removed here. */
QComboBox {{ padding-right: 28px; }}
QComboBox::drop-down {{ border: none; width: 0; }}
QComboBox::down-arrow {{ image: none; width: 0; height: 0; }}
QComboBox QAbstractItemView {{
    background: {RAISED};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 4px;
    outline: none;
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}

/* Native spin buttons cannot be styled into anything that looks right at this scale.
   Hidden on purpose - typing, arrow keys and the scroll wheel all still work. */
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    width: 0;
    height: 0;
    border: none;
}}

QPushButton {{
    background: {RAISED};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 7px 14px;
    font-weight: 500;
}}
QPushButton:hover {{ background: {HOVER}; border-color: #333c4a; }}
QPushButton:pressed {{ background: #11151b; }}
QPushButton:disabled {{ color: {DIM}; background: #101318; border-color: {BORDER_SOFT}; }}
QPushButton[accent="true"] {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton[accent="true"]:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton[accent="true"]:pressed {{ background: {ACCENT_PRESSED}; }}
QPushButton[danger="true"] {{ color: {DANGER}; border-color: #46262c; }}
QPushButton[danger="true"]:hover {{ background: #2a1a1d; border-color: {DANGER}; }}
QPushButton[flat="true"] {{ background: transparent; border: none; color: {MUTED}; padding: 4px 8px; }}
QPushButton[flat="true"]:hover {{ color: {TEXT}; background: {HOVER}; }}

QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{
    width: 17px; height: 17px;
    border: 1px solid {BORDER};
    border-radius: 5px;
    background: {BG};
}}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
    image: none;
}}
QCheckBox:disabled {{ color: {DIM}; }}

QTabWidget::pane {{ border: none; background: transparent; }}
QTabBar {{ qproperty-drawBase: 0; }}
QTabBar::tab {{
    background: transparent;
    color: {MUTED};
    padding: 8px 16px;
    margin-right: 4px;
    border: none;
    border-radius: 8px;
    font-weight: 500;
}}
QTabBar::tab:hover {{ color: {TEXT}; background: #171b22; }}
QTabBar::tab:selected {{ color: {ACCENT}; background: {RAISED}; font-weight: 600; }}

QListView {{
    background: transparent;
    border: none;
    outline: none;
}}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #2b323d; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #3a4351; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #2b323d; border-radius: 5px; min-width: 30px; }}
QScrollBar::handle:horizontal:hover {{ background: #3a4351; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 8px; }}

QToolTip {{
    background: {RAISED};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 9px;
}}

QStatusBar {{ background: {SURFACE}; border-top: 1px solid {BORDER_SOFT}; color: {MUTED}; }}
QStatusBar::item {{ border: none; }}

QMenu {{ background: {RAISED}; border: 1px solid {BORDER}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {ACCENT}; color: #ffffff; }}

#Console {{
    background: #090b0e;
    border: 1px solid {BORDER_SOFT};
    border-radius: 10px;
    font-family: {MONO};
    font-size: 12px;
    color: #c8d0dc;
}}
#CommandPreview {{
    background: #090b0e;
    border: 1px solid {BORDER_SOFT};
    border-radius: 10px;
    font-family: {MONO};
    font-size: 12px;
    color: #a9d3ff;
}}
"""


def apply_dark_titlebar(widget) -> None:
    """Windows 11 immersive dark title bar - silently ignored elsewhere."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = int(widget.winId())
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(value), ctypes.sizeof(value)
        )
    except Exception:
        pass
