"""Static, inexpensive Qt Widgets styling for the desktop companion."""

APP_NAME = "快橙伴航"

COLORS = {
    "background": "#0b0d12",
    "sidebar": "#0c0d11",
    "surface": "#151c29",
    "surface_high": "#1b2433",
    "border": "#293347",
    "text": "#eef2fb",
    "muted": "#8996ad",
    "orange": "#ff9147",
    "green": "#70dbb2",
    "blue": "#83adff",
    "red": "#ff8b92",
}

STYLESHEET = """
QMainWindow, QWidget#Root { background: #0b0d12; color: #eef2fb; }
QWidget { color: #eef2fb; font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 12px; }
QLabel { background: transparent; }
QFrame#Sidebar { background: #0c0d11; border-right: 1px solid #282019; }
QLabel#Brand { font-size: 20px; font-weight: 700; color: #f9faff; }
QLabel#BrandSub { font-size: 9px; letter-spacing: 2px; color: #708099; }
QLabel#PageTitle { font-size: 24px; font-weight: 700; letter-spacing: 1px; }
QLabel#PageSubtitle { color: #8e9bb2; font-size: 12px; }
QLabel#Eyebrow { color: #ffab72; font-size: 10px; font-weight: 600; letter-spacing: 2px; }
QLabel#HeroTitle { font-size: 14px; font-weight: 600; color: #ffbd80; }
QLabel#LauncherHeadline { font-size: 40px; font-weight: 800; color: #fff4e7; letter-spacing: 2px; }
QLabel#HeroDescription { color: #beafa4; font-size: 12px; }
QLabel#Muted, QLabel#Footer { color: #7d8ca4; font-size: 11px; }
QLabel#SectionTitle { font-size: 15px; font-weight: 600; }
QLabel#CardTitle { font-size: 14px; font-weight: 600; }
QLabel#MetricValue { font-family: 'Segoe UI'; font-size: 31px; font-weight: 600; color: #ffe1c3; }
QLabel#MetricName { color: #a0adc1; font-size: 11px; }
QLabel#MetricHint { color: #61718c; font-size: 9px; }
QLabel#Chip { background: #222c3e; border: 1px solid #34415a; border-radius: 11px; padding: 4px 10px; color: #b9c8df; font-size: 10px; }
QLabel#SuccessChip { background: #17382f; border: 1px solid #245543; border-radius: 11px; padding: 4px 10px; color: #80e7bd; font-size: 10px; }
QLabel#WarningChip { background: #3b2b21; border: 1px solid #624128; border-radius: 11px; padding: 4px 10px; color: #ffb478; font-size: 10px; }
QFrame#Card { background: #14171e; border: 1px solid #2c2b2c; border-radius: 14px; }
QFrame#Metric { background: transparent; border: none; border-left: 1px solid #302b25; border-radius: 0; }
QFrame#MetricStrip { background: #101217; border-top: 1px solid #362920; border-bottom: 1px solid #272729; border-radius: 0; }
QFrame#RouteBand { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #1c1916,stop:1 #11161e); border: 1px solid #39312a; border-radius: 14px; }
QFrame#Hero { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #202738,stop:0.58 #1c2433,stop:1 #30281f); border: 1px solid #3b3c46; border-radius: 18px; }
QFrame#InfoCard { background: #13161d; border: 1px solid #302b26; border-radius: 12px; }
QFrame#Divider { background: #273145; max-height: 1px; border: 0; }
QPushButton { background: #202b3e; border: 1px solid #33415a; border-radius: 9px; padding: 10px 16px; font-weight: 600; }
QPushButton:hover { background: #29364b; border-color: #516483; }
QPushButton:pressed { background: #182234; }
QPushButton:focus { border-color: #e89b64; }
QPushButton:disabled { background: #1b2330; border-color: #273145; color: #64728a; }
QPushButton#Primary { background: transparent; color: #21170e; border: 1px solid transparent; border-radius: 10px; font-size: 14px; font-weight: 700; padding: 13px 22px; }
QPushButton#Primary:hover { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #ffb86b,stop:1 #ff884b); }
QPushButton#Primary:disabled { background: #5d4434; border-color: #72523c; color: #c9ac99; }
QPushButton#Danger { background: #38232a; color: #ffb2b7; border-color: #64333e; }
QPushButton#Danger:hover { background: #492c34; }
QPushButton#Ghost { background: transparent; border: 1px solid #344057; color: #9eafc8; font-size: 11px; padding: 7px 12px; }
QPushButton#Nav { text-align: left; background: transparent; border: 1px solid transparent; border-radius: 9px; color: #8795ad; padding: 12px 14px; font-size: 12px; font-weight: 500; }
QPushButton#Nav:hover { background: #192231; color: #d9e4f6; }
QPushButton#Nav:checked { background: #2b251f; border-color: #59402d; color: #ffab70; font-weight: 600; }
QPushButton#TextButton { background: transparent; color: #ffb17a; border: none; text-align: left; padding: 5px 14px; }
QPlainTextEdit { background: #0e1521; border: 1px solid #263449; border-radius: 12px; padding: 13px; color: #adbed7; font-family: 'Cascadia Mono', 'Consolas'; font-size: 11px; selection-background-color: #735137; }
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 7px; margin: 5px 0; }
QScrollBar::handle:vertical { background: #35435b; border-radius: 3px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QMenu { background: #172131; border: 1px solid #37455e; border-radius: 8px; padding: 5px; }
QMenu::item { padding: 8px 22px; border-radius: 4px; }
QMenu::item:selected { background: #3c3025; color: #ffbd8b; }
QMenu::separator { height: 1px; background: #344158; margin: 4px 8px; }
QToolTip { background: #243046; color: #e9f0ff; border: 1px solid #465a79; padding: 6px; }
/* Scenic launcher: frosted foreground over an original painted night sky. */
QMainWindow, QWidget#Root { background: transparent; }
QLabel#WindowBrand { font-size: 15px; font-weight: 600; color: #e5f0ff; }
QLabel#HeaderContext { color: #adc7e3; font-size: 11px; }
QLabel#SkyEyebrow { color: #d6e8f6; font-family: 'Segoe UI'; font-size: 10px; letter-spacing: 2px; }
QLabel#ConnectionHeadline { color: #ffffff; font-size: 48px; font-weight: 700; letter-spacing: 1px; }
QLabel#SkyDescription { color: #d8e7f4; font-size: 12px; }
QLabel#SkyHint { color: #a9bfd4; font-size: 10px; }
QLabel#Footer { color: #b9cde0; font-size: 10px; }
QLabel#BottomVersion { color: #64809d; font-family: 'Segoe UI'; font-size: 10px; }
QLabel#Chip { background: rgba(226,241,255,30); border: 1px solid rgba(208,233,255,60); color: #d9ebfa; }
QFrame#BottomBar { background: rgba(220,235,249,225); border: 1px solid rgba(255,255,255,210); border-radius: 15px; }
QFrame#FrostInfo { background: rgba(228,240,251,217); border: 1px solid rgba(255,255,255,205); border-radius: 13px; }
QFrame#Metric { background: transparent; border: none; border-left: 1px solid rgba(135,161,189,85); }
QFrame#Metric QLabel#MetricValue { font-size: 26px; color: #234464; }
QFrame#Metric QLabel#MetricName { color: #49627a; }
QFrame#Metric QLabel#MetricHint { color: #7890a7; }
QFrame#Card { background: rgba(231,241,252,229); border: 1px solid rgba(255,255,255,210); border-radius: 14px; }
QFrame#Card QLabel { color: #244461; }
QFrame#Card QLabel#Muted { color: #5f7892; }
QFrame#Hero { background: rgba(220,237,251,225); border: 1px solid rgba(255,255,255,190); }
QFrame#Hero QLabel#HeroTitle { color: #244868; font-size: 27px; }
QFrame#Hero QLabel#HeroDescription, QFrame#Hero QLabel#Muted { color: #5b7793; }
QPlainTextEdit { background: rgba(233,243,253,230); color: #325372; border: 1px solid rgba(255,255,255,210); selection-background-color: #94bae4; }
QPushButton#Nav { padding: 8px 14px; }
QPushButton#WindowControl { padding: 0; border: none; }
QMenu { background: #e4effa; border: 1px solid #a5bfd7; }
QMenu::item { color: #284864; }
QMenu::item:selected { background: #c3d9f1; color: #174579; }
"""

DARK_OVERRIDES = """
QFrame#BottomBar { background: rgba(12,23,38,242); border: 1px solid rgba(117,151,189,110); }
QFrame#FrostInfo { background: rgba(17,31,49,237); border: 1px solid rgba(113,151,191,125); }
QFrame#Metric { border-left: 1px solid rgba(112,147,184,65); }
QFrame#Metric QLabel#MetricValue { color: #d1e8ff; }
QFrame#Metric QLabel#MetricName { color: #b1c7dd; }
QFrame#Metric QLabel#MetricHint { color: #7895b0; }
QFrame#Card { background: rgba(16,29,47,239); border: 1px solid rgba(109,145,184,125); }
QFrame#Card QLabel { color: #dfedfb; }
QFrame#Card QLabel#Muted { color: #9eb7d0; }
QFrame#Hero { background: rgba(17,32,50,238); border: 1px solid rgba(119,158,197,135); }
QFrame#Hero QLabel#HeroTitle { color: #e2f0fe; }
QFrame#Hero QLabel#HeroDescription, QFrame#Hero QLabel#Muted { color: #a3bed8; }
QLabel#BottomVersion { color: #83a5c7; }
QLabel#Chip { background: rgba(22,42,64,180); border: 1px solid rgba(131,175,212,100); color: #d2e9fd; }
QPlainTextEdit { background: rgba(10,22,37,243); color: #c6dcf2; border: 1px solid rgba(104,144,184,120); selection-background-color: #285480; }
QMenu { background: #112238; border: 1px solid #4a6888; }
QMenu::item { color: #d1e4f7; }
QMenu::item:selected { background: #24496b; color: #ecf8ff; }
QMenu::separator { background: #38516c; }
QToolTip { background: #142a42; color: #deefff; border: 1px solid #4b6a8c; }
QScrollBar::handle:vertical { background: #486784; }
"""


def stylesheet_for(mode="dark"):
    return STYLESHEET + (DARK_OVERRIDES if mode != "light" else "")


def is_dark_theme():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    return app is None or app.property("appearanceTheme") != "light"
