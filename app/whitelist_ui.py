"""Small, opaque whitelist editor. All persistence is requested by signals."""
from __future__ import annotations

from urllib.parse import urlsplit

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor, QPalette, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDialog, QHeaderView,
    QHBoxLayout, QLabel, QLineEdit, QMenu, QPlainTextEdit, QPushButton,
    QScrollArea, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)


def _label(text="", name="Muted"):
    label = QLabel(text)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class WhitelistDialog(QDialog):
    """The owner connects action_requested to its existing background worker."""

    action_requested = Signal(str, object)

    def __init__(self, parent=None, button_factory=None, preview=False):
        super().__init__(parent)
        self.preview = bool(preview)
        self._button_factory = button_factory
        self._busy = False
        self._revision = None
        self._recent_rows = None
        self._recent_buttons = []
        self._theme = None
        self.setObjectName("WhitelistDialog")
        self.setWindowTitle("域名分流白名单")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setAutoFillBackground(True)
        self.resize(690, 650)
        self.setMinimumSize(530, 510)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(_label("域名分流白名单", "SectionTitle"))
        layout.addWidget(_label("粘贴网址或域名即可固定路线，可一次添加多个。只保存域名，不保存网址路径。"))

        self.input = QPlainTextEdit()
        self.input.setObjectName("WhitelistInput")
        self.input.setPlaceholderText("网址用换行或空格分开；纯域名也可用逗号分隔\n例如 https://example.com/path")
        self.input.setMinimumHeight(72)
        self.input.setMaximumHeight(95)
        self.input.installEventFilter(self)
        layout.addWidget(self.input)
        layout.addWidget(_label("Enter 固定直连 · Ctrl+Enter 固定 VPN · Shift+Enter 换行"))

        actions = QHBoxLayout()
        self.include_subdomains = QCheckBox("包含子域名")
        self.include_subdomains.setChecked(True)
        self.include_subdomains.setToolTip("关闭后仅匹配输入域名；修改已保存规则时保留原有范围。")
        actions.addWidget(self.include_subdomains)
        actions.addStretch()
        self.paste_button = self._button("粘贴网址", "Ghost")
        self.paste_button.clicked.connect(self._paste_text)
        actions.addWidget(self.paste_button)
        self.direct_button = self._button("固定直连", "Ghost")
        self.direct_button.clicked.connect(lambda: self._submit_text("direct"))
        actions.addWidget(self.direct_button)
        self.vpn_button = self._button("固定VPN", "Ghost")
        self.vpn_button.clicked.connect(lambda: self._submit_text("vpn"))
        actions.addWidget(self.vpn_button)
        layout.addLayout(actions)

        self.error_label = _label("", "WhitelistError")
        self.error_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.error_label.hide()
        layout.addWidget(self.error_label)
        self.busy_label = _label("正在保存规则…")
        self.busy_label.hide()
        layout.addWidget(self.busy_label)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self._build_saved_tab()
        self._build_recent_tab()

        self.status_label = _label("分流未运行：规则仅保存，下次启动时生效。")
        layout.addWidget(self.status_label)
        footer = QHBoxLayout()
        footer.addWidget(_label("预览模式：不会保存或删除规则。" if self.preview else "现有连接保持原路线，不会因修改规则而中断。"), 1)
        self.close_button = self._button("关闭", "Ghost")
        self.close_button.clicked.connect(self.reject)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)
        self._sync_theme()
        self.set_recent([])
        self._sync_enabled()

    def _button(self, text, role):
        button = self._button_factory(text, role) if self._button_factory else QPushButton(text)
        button.setObjectName(role)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setAutoDefault(False)
        button.setDefault(False)
        return button

    def _build_saved_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 12, 10, 10)
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("筛选已保存域名")
        self.filter_input.setClearButtonEnabled(True)
        self.filter_input.textChanged.connect(self._filter_rules)
        layout.addWidget(self.filter_input)
        self.rules_table = QTableWidget(0, 3)
        self.rules_table.setHorizontalHeaderLabels(["域名", "路线", "匹配范围"])
        self.rules_table.verticalHeader().hide()
        self.rules_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.rules_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.rules_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.rules_table.setAlternatingRowColors(True)
        self.rules_table.setShowGrid(False)
        self.rules_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.rules_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.rules_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.rules_table.itemSelectionChanged.connect(self._sync_enabled)
        self.rules_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.rules_table.customContextMenuRequested.connect(self._show_rules_menu)
        layout.addWidget(self.rules_table, 1)
        self.rules_hint = _label("当前没有已保存的规则。")
        layout.addWidget(self.rules_hint)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.selected_direct_button = self._button("改为直连", "Ghost")
        self.selected_direct_button.clicked.connect(lambda: self._set_selected_mode("direct"))
        buttons.addWidget(self.selected_direct_button)
        self.selected_vpn_button = self._button("改为VPN", "Ghost")
        self.selected_vpn_button.clicked.connect(lambda: self._set_selected_mode("vpn"))
        buttons.addWidget(self.selected_vpn_button)
        self.remove_button = self._button("删除规则", "Danger")
        self.remove_button.clicked.connect(self._remove_selected)
        buttons.addWidget(self.remove_button)
        layout.addLayout(buttons)
        self.tabs.addTab(page, "已保存规则")

    def _build_recent_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 12, 10, 10)
        layout.addWidget(_label("最近 20 个连接域名，点击即可固定路线。匹配范围使用上方“包含子域名”。"))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.recent_widget = QWidget()
        self.recent_layout = QVBoxLayout(self.recent_widget)
        self.recent_layout.setContentsMargins(0, 4, 4, 4)
        self.recent_layout.setSpacing(6)
        scroll.setWidget(self.recent_widget)
        layout.addWidget(scroll, 1)
        self.tabs.addTab(page, "最近连接")

    def fill_text(self, text):
        self.input.setPlainText(text)
        self.input.moveCursor(QTextCursor.MoveOperation.End)
        self.input.setFocus()

    def apply_document(self, doc):
        """Refresh table items only when the saved document revision changes."""
        revision = doc.get("revision", 0)
        if revision != self._revision:
            self._revision = revision
            self._populate_rules(doc.get("rules", []))
            self.show_error("")
        if "recent" in doc:
            self.set_recent(doc["recent"])
        applied = doc.get("applied_revision")
        if not doc.get("active"):
            status = "分流未运行：规则仅保存，下次启动时生效。"
        elif applied == revision:
            status = f"当前规则已应用（版本 {revision}）；用于新连接，现有连接保持原路线。"
        else:
            status = "分流运行中：新规则约 1 秒后用于新连接，现有连接保持原路线。"
        if self.preview:
            status += " 预览不会保存修改。"
        self.status_label.setText(status)

    def _populate_rules(self, rules):
        selected = self._selected_rule()
        table = self.rules_table
        table.setUpdatesEnabled(False)
        table.blockSignals(True)
        try:
            table.clearContents()
            table.setRowCount(len(rules))
            for row, rule in enumerate(rules):
                item = QTableWidgetItem(rule["host"])
                item.setData(Qt.ItemDataRole.UserRole, (rule["host"], bool(rule["subdomains"])))
                table.setItem(row, 0, item)
                table.setItem(row, 1, QTableWidgetItem("VPN" if rule["mode"] == "vpn" else "直连"))
                table.setItem(row, 2, QTableWidgetItem("包含子域名" if rule["subdomains"] else "仅此域名"))
                if selected == (rule["host"], bool(rule["subdomains"])):
                    table.selectRow(row)
        finally:
            table.blockSignals(False)
            table.setUpdatesEnabled(True)
        self.rules_hint.setText(f"已保存 {len(rules)} 条规则；选择后可修改路线或删除，原匹配范围保留。" if rules else "当前没有已保存的规则。")
        self._filter_rules()
        self._sync_enabled()

    def _filter_rules(self, *_):
        text = self.filter_input.text().strip().casefold()
        for row in range(self.rules_table.rowCount()):
            item = self.rules_table.item(row, 0)
            self.rules_table.setRowHidden(row, bool(text and text not in item.text().casefold()))
        self._sync_enabled()

    def _selected_rule(self):
        rows = self.rules_table.selectionModel().selectedRows()
        if not rows or self.rules_table.isRowHidden(rows[0].row()):
            return None
        item = self.rules_table.item(rows[0].row(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _set_selected_mode(self, mode):
        selected = self._selected_rule()
        if selected:
            host, subdomains = selected
            self._emit_action("whitelist-add", {"text": host, "mode": mode, "subdomains": subdomains})

    def _remove_selected(self):
        selected = self._selected_rule()
        if selected:
            host, subdomains = selected
            self._emit_action("whitelist-remove", {"host": host, "subdomains": subdomains})

    def _show_rules_menu(self, position):
        if self._busy or self.preview:
            return
        item = self.rules_table.itemAt(position)
        if item is None:
            return
        self.rules_table.selectRow(item.row())
        menu = QMenu(self)
        menu.addAction("固定直连", lambda: self._set_selected_mode("direct"))
        menu.addAction("固定VPN", lambda: self._set_selected_mode("vpn"))
        menu.addSeparator()
        menu.addAction("删除规则", self._remove_selected)
        menu.exec(self.rules_table.viewport().mapToGlobal(position))

    def set_recent(self, rows):
        recent, seen = [], set()
        for row in rows:
            raw = row.get("host", "")
            if not isinstance(raw, str):
                continue
            try:
                host = urlsplit(raw if "://" in raw else "//" + raw).hostname
            except ValueError:
                continue
            if not host or host in seen:
                continue
            recent.append((host, row.get("mode", "direct"), bool(row.get("whitelist"))))
            seen.add(host)
            if len(recent) >= 20:
                break
        recent = tuple(recent)
        if recent == self._recent_rows:
            return
        self._recent_rows = recent
        self._recent_buttons.clear()
        while self.recent_layout.count():
            item = self.recent_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not recent:
            self.recent_layout.addWidget(_label("暂无最近连接。启动分流后会在这里显示访问的域名。"))
        for host, mode, fixed in recent:
            row = QWidget()
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 2, 0, 2)
            host_label = _label(host, "WhitelistHost")
            host_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            line.addWidget(host_label, 1)
            line.addWidget(_label(("VPN" if mode == "vpn" else "直连") + (" · 已固定" if fixed else "")))
            for route, text in (("direct", "直连"), ("vpn", "VPN")):
                button = self._button(text, "Ghost")
                button.setFixedHeight(32)
                button.setMinimumWidth(64)
                button.setToolTip("将 " + host + " 固定为" + text)
                button.clicked.connect(lambda checked=False, target=host, target_mode=route: self._emit_action(
                    "whitelist-add", {"text": target, "mode": target_mode, "subdomains": self.include_subdomains.isChecked()},
                ))
                self._recent_buttons.append(button)
                line.addWidget(button)
            self.recent_layout.addWidget(row)
        self.recent_layout.addStretch()
        self._sync_enabled()

    def set_busy(self, busy):
        self._busy = bool(busy)
        self.busy_label.setVisible(self._busy)
        self._sync_enabled()

    def _sync_enabled(self):
        writable = not self._busy and not self.preview
        self.input.setEnabled(not self._busy)
        self.include_subdomains.setEnabled(not self._busy)
        self.paste_button.setEnabled(not self._busy)
        for button in (self.direct_button, self.vpn_button, *self._recent_buttons):
            button.setEnabled(writable)
        selected = bool(self._selected_rule())
        for button in (self.selected_direct_button, self.selected_vpn_button, self.remove_button):
            button.setEnabled(writable and selected)

    def show_error(self, message):
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def _paste_text(self):
        text = QApplication.clipboard().text()
        if not text.strip():
            self.show_error("剪贴板里没有网址或域名。")
            return
        self.fill_text(text)
        self.show_error("")

    def _submit_text(self, mode):
        text = self.input.toPlainText()
        if not text.strip():
            self.show_error("请粘贴网址或输入域名；可每行添加一条。")
            self.input.setFocus()
            return
        self._emit_action("whitelist-add", {"text": text, "mode": mode, "subdomains": self.include_subdomains.isChecked()})

    def _emit_action(self, action, payload):
        if self._busy or self.preview:
            return
        self.show_error("")
        self.action_requested.emit(action, payload)

    def eventFilter(self, watched, event):
        if watched is self.input and event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            modifiers = event.modifiers()
            if modifiers & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier):
                return False
            if not event.isAutoRepeat():
                self._submit_text("vpn" if modifiers & Qt.KeyboardModifier.ControlModifier else "direct")
            return True
        return super().eventFilter(watched, event)

    def _sync_theme(self):
        app = QApplication.instance()
        mode = "light" if app and app.property("appearanceTheme") == "light" else "dark"
        if mode == self._theme:
            return
        self._theme = mode
        dark = mode == "dark"
        background, surface, text, muted, border = (
            ("#102038", "#0c192b", "#dceafa", "#9eb7d0", "#496481") if dark else
            ("#e7f0fa", "#f4f8fd", "#244461", "#5f7892", "#a5bfd7")
        )
        palette = self.palette()
        for role, color in ((QPalette.ColorRole.Window, background), (QPalette.ColorRole.Base, surface),
                            (QPalette.ColorRole.AlternateBase, background), (QPalette.ColorRole.Text, text),
                            (QPalette.ColorRole.WindowText, text)):
            palette.setColor(role, QColor(color))
        self.setPalette(palette)
        # Scope only these new controls; global button styling stays inherited.
        self.setStyleSheet(f"""
            QDialog#WhitelistDialog {{ background: {background}; color: {text}; }}
            QDialog#WhitelistDialog QLabel, QDialog#WhitelistDialog QCheckBox {{ color: {text}; }}
            QDialog#WhitelistDialog QLabel#Muted {{ color: {muted}; }}
            QDialog#WhitelistDialog QLabel#WhitelistError {{ color: {'#ffabb0' if dark else '#a42b39'}; }}
            QDialog#WhitelistDialog QPushButton#Ghost {{ color: {text}; }}
            QDialog#WhitelistDialog QPushButton#Ghost:disabled {{ color: {muted}; }}
            QDialog#WhitelistDialog QLineEdit, QDialog#WhitelistDialog QTableWidget {{
                background: {surface}; color: {text}; border: 1px solid {border}; border-radius: 6px;
                alternate-background-color: {background};
                selection-background-color: {'#285480' if dark else '#bfd9f5'};
                selection-color: {text}; padding: 5px;
            }}
            QDialog#WhitelistDialog QHeaderView::section {{
                background: {background}; color: {muted}; border: none; padding: 8px;
            }}
            QDialog#WhitelistDialog QTabWidget::pane {{ border: 1px solid {border}; border-radius: 6px; }}
            QDialog#WhitelistDialog QTabBar::tab {{ color: {muted}; background: {surface}; padding: 9px 16px; }}
            QDialog#WhitelistDialog QTabBar::tab:selected {{ color: {text}; background: {background}; }}
        """)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.StyleChange and hasattr(self, "_theme"):
            self._sync_theme()

    def showEvent(self, event):
        self._sync_theme()
        super().showEvent(event)

