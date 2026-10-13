"""Offline dialog tests; no service, vendor UI, proxy, or persisted user data."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from app.whitelist_ui import WhitelistDialog


def rule(host="example.com", mode="direct", subdomains=True):
    return {"host": host, "mode": mode, "subdomains": subdomains}


def document(*rules, revision=1, active=False, applied_revision=None, **extra):
    return {
        "version": 1, "revision": revision, "rules": list(rules),
        "active": active, "applied_revision": applied_revision, **extra,
    }


class WhitelistUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.factory_calls = []
        def factory(text, role):
            self.factory_calls.append((text, role))
            return QPushButton(text)
        self.dialog = WhitelistDialog(button_factory=factory)
        self.actions = []
        self.dialog.action_requested.connect(lambda action, payload: self.actions.append((action, payload)))

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()

    def test_opaque_dialog_uses_injected_buttons(self):
        self.assertFalse(self.dialog.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        self.assertTrue(self.dialog.autoFillBackground())
        self.assertLessEqual(self.dialog.input.maximumHeight(), 95)
        self.assertIn(("固定直连", "Ghost"), self.factory_calls)
        self.assertIn(("固定VPN", "Ghost"), self.factory_calls)

    def test_batch_input_buttons_emit_raw_text_and_selected_scope(self):
        text = "https://example.com/path\nother.example:8443/path"
        self.dialog.fill_text(text)
        self.dialog.direct_button.click()
        self.dialog.include_subdomains.setChecked(False)
        self.dialog.vpn_button.click()
        self.assertEqual(self.actions, [
            ("whitelist-add", {"text": text, "mode": "direct", "subdomains": True}),
            ("whitelist-add", {"text": text, "mode": "vpn", "subdomains": False}),
        ])

    def test_enter_control_enter_and_shift_enter_are_local_to_input(self):
        self.dialog.fill_text("example.com")
        QTest.keyClick(self.dialog.input, Qt.Key.Key_Return)
        QTest.keyClick(self.dialog.input, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(self.dialog.input, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual([payload["mode"] for _, payload in self.actions], ["direct", "vpn"])
        self.assertEqual(self.dialog.input.toPlainText(), "example.com\n")

    def test_recent_connections_have_one_click_routes_and_only_hosts(self):
        self.dialog.set_recent([
            {"host": "https://EXAMPLE.com/private/path?q=1", "mode": "vpn", "whitelist": False},
            {"host": "example.com", "mode": "vpn", "whitelist": False},
            *[{"host": f"host{i}.example", "mode": "direct", "whitelist": True} for i in range(30)],
        ])
        self.assertEqual(len(self.dialog._recent_buttons), 40)
        self.dialog._recent_buttons[0].click()
        self.dialog.include_subdomains.setChecked(False)
        self.dialog._recent_buttons[1].click()
        self.assertEqual(self.actions, [
            ("whitelist-add", {"text": "example.com", "mode": "direct", "subdomains": True}),
            ("whitelist-add", {"text": "example.com", "mode": "vpn", "subdomains": False}),
        ])
        self.assertEqual(self.dialog._recent_rows[0][0], "example.com")
        self.assertIn(("直连", "Ghost"), self.factory_calls)
        self.assertIn(("VPN", "Ghost"), self.factory_calls)

    def test_saved_route_change_and_remove_keep_original_scope(self):
        self.dialog.apply_document(document(rule(subdomains=False)))
        self.dialog.rules_table.selectRow(0)
        self.dialog.selected_vpn_button.click()
        self.dialog.remove_button.click()
        self.assertEqual(self.actions, [
            ("whitelist-add", {"text": "example.com", "mode": "vpn", "subdomains": False}),
            ("whitelist-remove", {"host": "example.com", "subdomains": False}),
        ])
        self.dialog.apply_document(document(rule("other.example"), revision=2))
        self.dialog.rules_table.selectRow(0)
        self.dialog.include_subdomains.setChecked(False)
        self.dialog.selected_direct_button.click()
        self.assertEqual(self.actions[-1], ("whitelist-add", {
            "text": "other.example", "mode": "direct", "subdomains": True,
        }))

    def test_busy_blocks_all_mutations_and_restores_selected_controls(self):
        self.dialog.fill_text("example.com")
        self.dialog.apply_document(document(rule(), recent=[{"host": "recent.example", "mode": "direct"}]))
        self.dialog.rules_table.selectRow(0)
        self.dialog.set_busy(True)
        for button in (self.dialog.direct_button, self.dialog.vpn_button, self.dialog.remove_button,
                       self.dialog.selected_vpn_button, *self.dialog._recent_buttons):
            self.assertFalse(button.isEnabled())
            button.click()
        QTest.keyClick(self.dialog.input, Qt.Key.Key_Return)
        self.dialog._remove_selected()
        self.assertEqual(self.actions, [])
        self.assertTrue(self.dialog.close_button.isEnabled())
        self.dialog.set_busy(False)
        self.assertTrue(self.dialog.remove_button.isEnabled())
        self.dialog.direct_button.click()
        self.assertEqual(len(self.actions), 1)

    def test_revision_controls_table_refresh_and_keeps_selection(self):
        self.dialog.apply_document(document(rule(), revision=5))
        self.dialog.rules_table.selectRow(0)
        with patch.object(self.dialog, "_populate_rules", wraps=self.dialog._populate_rules) as populate:
            self.dialog.apply_document(document(rule(mode="vpn"), revision=5, active=True, applied_revision=5))
            populate.assert_not_called()
            self.assertEqual(self.dialog.rules_table.item(0, 1).text(), "直连")
            self.dialog.apply_document(document(rule(mode="vpn"), revision=6))
            populate.assert_called_once()
        self.assertEqual(self.dialog.rules_table.item(0, 1).text(), "VPN")
        self.assertEqual(self.dialog._selected_rule(), ("example.com", True))
        self.assertIsNone(self.dialog.rules_table.cellWidget(0, 0))
        self.assertIsNone(self.dialog.rules_table.cellWidget(0, 1))

    def test_filter_hides_nonmatching_rows_and_disables_hidden_selection(self):
        self.dialog.apply_document(document(rule(), rule("api.other.example", mode="vpn")))
        self.dialog.rules_table.selectRow(0)
        self.dialog.filter_input.setText("API.OTHER")
        self.assertTrue(self.dialog.rules_table.isRowHidden(0))
        self.assertFalse(self.dialog.rules_table.isRowHidden(1))
        self.assertFalse(self.dialog.remove_button.isEnabled())
        self.dialog.rules_table.selectRow(1)
        self.assertTrue(self.dialog.remove_button.isEnabled())

    def test_saved_pending_and_applied_status_are_explicit(self):
        self.dialog.apply_document(document(rule(), revision=8))
        self.assertIn("规则仅保存", self.dialog.status_label.text())
        self.dialog.apply_document(document(rule(), revision=8, active=True, applied_revision=7))
        self.assertIn("约 1 秒后用于新连接", self.dialog.status_label.text())
        self.dialog.apply_document(document(rule(), revision=8, active=True, applied_revision=8))
        self.assertIn("已应用（版本 8）", self.dialog.status_label.text())
        self.assertIn("现有连接保持原路线", self.dialog.status_label.text())

    def test_empty_input_and_service_format_error_are_visible_plain_text(self):
        self.dialog.direct_button.click()
        self.assertEqual(self.actions, [])
        self.assertIn("网址或输入域名", self.dialog.error_label.text())
        self.assertFalse(self.dialog.error_label.isHidden())
        self.dialog.apply_document(document())
        self.dialog.show_error("第 2 行格式错误：请使用有效域名；<bad> 不能添加。")
        self.dialog.apply_document(document(active=True, applied_revision=1))
        self.assertIn("第 2 行格式错误", self.dialog.error_label.text())
        self.assertEqual(self.dialog.error_label.textFormat(), Qt.TextFormat.PlainText)
        self.dialog.show_error("")
        self.assertTrue(self.dialog.error_label.isHidden())

    def test_preview_has_no_mutating_signal_even_through_methods(self):
        preview = WhitelistDialog(preview=True)
        self.addCleanup(preview.deleteLater)
        actions = []
        preview.action_requested.connect(lambda *args: actions.append(args))
        preview.fill_text("example.com")
        preview.apply_document(document(rule(), recent=[{"host": "recent.example", "mode": "vpn"}]))
        preview.rules_table.selectRow(0)
        preview.direct_button.click()
        preview._recent_buttons[0].click()
        QTest.keyClick(preview.input, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        preview._set_selected_mode("vpn")
        preview._remove_selected()
        self.assertEqual(actions, [])
        self.assertIn("预览不会保存", preview.status_label.text())


if __name__ == "__main__":
    unittest.main()
