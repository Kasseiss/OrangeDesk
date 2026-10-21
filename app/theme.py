"""Design tokens and static, inexpensive Qt Widgets styling for the desktop companion.

Every painted widget reads colours from ``tokens()`` at paint time, so a theme
switch is one stylesheet assignment plus ordinary repaints.
"""

from string import Template

APP_NAME = "快橙伴航"

FONT_FAMILIES = ("Microsoft YaHei UI", "Segoe UI")
LATIN_FAMILIES = ("Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI")
MONO_FAMILIES = ("Cascadia Mono", "Consolas", "Microsoft YaHei UI")

# Graphite / ink workspace with one controlled warm accent.
PALETTES = {
    "dark": {
        "base": "#111214",
        "sidebar": "#0c0d0f",
        "panel": "#17181b",
        "panel_high": "#1c1d21",
        "raised": "#232428",
        "border": "#28292e",
        "border_strong": "#36373d",
        "hairline": "#202125",
        "ink": "#f3eee7",
        "ink_soft": "#cdc7be",
        "muted": "#928c84",
        "faint": "#5f5b56",
        "accent": "#ff8a3d",
        "accent_hi": "#ffa45f",
        "accent_lo": "#ea6c25",
        "accent_ink": "#1d1008",
        "accent_text": "#ffb07a",
        "success": "#7fd3a9",
        "warn": "#e8b462",
        "danger": "#ef8075",
        "danger_solid": "#c9443a",
        "log_bg": "#0e0f11",
        "selection": "#5b3a22",
        "knob": "#f8f4ee",
        "shadow": "#000000",
    },
    "light": {
        "base": "#f3f1ec",
        "sidebar": "#eae7e1",
        "panel": "#fdfcfa",
        "panel_high": "#f7f5f1",
        "raised": "#ffffff",
        "border": "#e2ded6",
        "border_strong": "#cec8be",
        "hairline": "#ebe7e0",
        "ink": "#1d1b18",
        "ink_soft": "#3d3934",
        "muted": "#716b62",
        "faint": "#a29b91",
        "accent": "#ee7b2f",
        "accent_hi": "#f7954c",
        "accent_lo": "#d9661d",
        "accent_ink": "#22140a",
        "accent_text": "#bb5512",
        "success": "#2b8f62",
        "warn": "#a96d14",
        "danger": "#c4473b",
        "danger_solid": "#c9443a",
        "log_bg": "#fbfaf7",
        "selection": "#f7d6ba",
        "knob": "#ffffff",
        "shadow": "#5a4a36",
    },
}

# Kept for compatibility with callers that read the legacy colour names.
COLORS = {
    "background": PALETTES["dark"]["base"],
    "sidebar": PALETTES["dark"]["sidebar"],
    "surface": PALETTES["dark"]["panel"],
    "surface_high": PALETTES["dark"]["raised"],
    "border": PALETTES["dark"]["border"],
    "text": PALETTES["dark"]["ink"],
    "muted": PALETTES["dark"]["muted"],
    "orange": PALETTES["dark"]["accent"],
    "green": PALETTES["dark"]["success"],
    "blue": "#9fb4d6",
    "red": PALETTES["dark"]["danger"],
}


def current_mode():
    return "dark" if is_dark_theme() else "light"


def tokens(mode=None):
    """Return the palette for ``mode`` or for the application's current theme."""
    if mode not in PALETTES:
        mode = current_mode()
    return PALETTES[mode]


def rgba(hex_color, alpha):
    value = hex_color.lstrip("#")
    red, green, blue = int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    return f"rgba({red},{green},{blue},{int(alpha)})"


_QSS = Template("""
QWidget { color: $ink; font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 12px; }
QMainWindow, QWidget#Root { background: $base; }
QDialog { background: $base; }
QLabel { background: transparent; }
QToolTip { background: $raised; color: $ink; border: 1px solid $border_strong; border-radius: 6px; padding: 6px 8px; font-size: 12px; }

QFrame#Sidebar { background: $sidebar; border: none; border-right: 1px solid $hairline; }
QFrame#Panel, QFrame#Card { background: $panel; border: 1px solid $border; border-radius: 14px; }
QFrame#Segmented { background: $segment_bg; border: 1px solid $border; border-radius: 10px; }
QFrame#Dock { background: $panel; border: 1px solid $border; border-radius: 12px; }
QFrame#AboutHero { background: $panel; border: 1px solid $border; border-radius: 16px; }
QFrame#VendorFrame { background: $panel; border: 1px solid $border; border-radius: 16px; }
QFrame#MetricStrip { background: transparent; border: none; border-top: 1px solid $stage_line; border-radius: 0; }
QFrame#Metric { background: transparent; border: none; border-left: 1px solid $stage_line; border-radius: 0; }
QFrame#Metric[first="true"] { border-left: none; }
QFrame#Divider { background: $hairline; border: none; min-height: 1px; max-height: 1px; }
QFrame#StatusBar { background: transparent; border: none; border-top: 1px solid $hairline; }
QWidget#VendorSlot { background: $panel_high; }

QLabel#WindowBrand { font-size: 14px; font-weight: 600; color: $ink; }
QLabel#BrandSub { font-family: 'Segoe UI'; font-size: 10px; color: $faint; }
QLabel#PageTitle { font-size: 16px; font-weight: 600; color: $ink; }
QLabel#Headline { font-size: 30px; font-weight: 600; color: $ink; }
QLabel#VendorTitle { font-size: 26px; font-weight: 600; color: $ink; }
QLabel#AboutTitle { font-size: 22px; font-weight: 600; color: $ink; }
QLabel#SectionTitle { font-size: 14px; font-weight: 600; color: $ink; }
QLabel#GroupTitle { font-size: 12px; font-weight: 600; color: $muted; }
QLabel#CardTitle { font-size: 13px; font-weight: 600; color: $ink; }
QLabel#SessionState { font-size: 13px; font-weight: 600; color: $ink; }
QLabel#Body { font-size: 12px; color: $ink_soft; }
QLabel#Muted { font-size: 12px; color: $muted; }
QLabel#Caption { font-size: 11px; color: $muted; }
QLabel#Eyebrow { font-family: 'Segoe UI'; font-size: 11px; font-weight: 600; color: $accent_text; }
QLabel#Feedback { font-size: 12px; color: $accent_text; }
QLabel#Version { font-family: 'Segoe UI'; font-size: 11px; color: $faint; }
QLabel#Kbd { font-family: 'Segoe UI'; font-size: 11px; font-weight: 600; color: $ink_soft; background: $raised; border: 1px solid $border_strong; border-radius: 5px; padding: 2px 7px; }
QLabel#MetricValue { font-family: 'Segoe UI'; font-size: 20px; font-weight: 600; color: $ink; }
QLabel#MetricName { font-size: 11px; color: $muted; }
QLabel#MetricHint { font-size: 10px; color: $faint; }
QLabel#WhitelistError { color: $danger; }
QLabel#WhitelistHost { font-family: 'Cascadia Mono', 'Consolas'; font-size: 12px; color: $ink; }

QPlainTextEdit { background: $log_bg; border: 1px solid $border; border-radius: 12px; padding: 10px 12px; color: $ink_soft; font-family: 'Cascadia Mono', 'Consolas', 'Microsoft YaHei UI'; font-size: 12px; selection-background-color: $selection; selection-color: $ink; }
QPlainTextEdit:focus { border-color: $accent_line; }
QLineEdit { background: $panel; border: 1px solid $border_strong; border-radius: 8px; padding: 6px 10px; color: $ink; selection-background-color: $selection; selection-color: $ink; }
QLineEdit:focus { border-color: $accent_line; }

QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 4px 2px 4px 2px; }
QScrollBar::handle:vertical { background: $scroll; border-radius: 3px; min-height: 28px; }
QScrollBar::handle:vertical:hover { background: $scroll_hover; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px 4px 2px 4px; }
QScrollBar::handle:horizontal { background: $scroll; border-radius: 3px; min-width: 28px; }
QScrollBar::handle:horizontal:hover { background: $scroll_hover; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QMenu { background: $raised; border: 1px solid $border_strong; border-radius: 10px; padding: 6px; }
QMenu::item { padding: 7px 22px 7px 14px; border-radius: 6px; color: $ink; }
QMenu::item:selected { background: $accent_wash; color: $ink; }
QMenu::item:disabled { color: $faint; }
QMenu::separator { height: 1px; background: $hairline; margin: 5px 8px; }

QMessageBox { background: $panel; }
QMessageBox QLabel { color: $ink; font-size: 13px; }
QPushButton { background: $raised; color: $ink; border: 1px solid $border_strong; border-radius: 8px; padding: 7px 18px; }
QPushButton:hover { border-color: $accent_line; }
QPushButton:pressed { background: $panel_high; }
QPushButton:focus { border-color: $accent; }
QPushButton:disabled { color: $faint; border-color: $border; }
QCheckBox { spacing: 8px; color: $ink; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 5px; border: 1px solid $border_strong; background: $raised; }
QCheckBox::indicator:checked { background: $accent; border-color: $accent; }
""")


def stylesheet_for(mode="dark"):
    mode = "light" if mode == "light" else "dark"
    palette = dict(PALETTES[mode])
    dark = mode == "dark"
    palette.update(
        accent_line=rgba(palette["accent"], 150),
        accent_wash=rgba(palette["accent"], 46 if dark else 38),
        scroll=rgba(palette["ink"], 46 if dark else 52),
        scroll_hover=rgba(palette["ink"], 80 if dark else 90),
        stage_line=rgba(palette["ink"], 22 if dark else 26),
        segment_bg=rgba(palette["ink"], 10 if dark else 9),
    )
    return _QSS.substitute(palette)


STYLESHEET = stylesheet_for("dark")


def is_dark_theme():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    return app is None or app.property("appearanceTheme") != "light"
