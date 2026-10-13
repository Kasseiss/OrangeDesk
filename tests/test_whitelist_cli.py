"""Mocked whitelist commands; never open a VPN client or change a proxy."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from app import cli


def document(*rules, active=False, revision=3, applied_revision=None, **extra):
    return {
        "version": 1, "revision": revision, "rules": list(rules),
        "active": active, "applied_revision": applied_revision, **extra,
    }


def rule(host="example.com", mode="direct", subdomains=True):
    return {"host": host, "mode": mode, "subdomains": subdomains}


class WhitelistCliTests(unittest.TestCase):
    def test_default_ui_and_quick_editor_launch_without_network_calls(self):
        for argv, expected in (([], ()), (["ui", "--whitelist"], ("--whitelist",))):
            with self.subTest(argv=argv), patch("app.cli.gui_command", return_value=["own-gui.exe"]) as command, patch("app.cli.hidden_spawn") as spawn, patch("app.cli.request") as request:
                self.assertEqual(cli.cli_main(argv), 0)
                command.assert_called_once_with(*expected)
                spawn.assert_called_once()
                request.assert_not_called()

    def run_cli(self, argv, result=None, error=None):
        output, errors = io.StringIO(), io.StringIO()
        with patch("app.cli.request", return_value=result, side_effect=error) as request, \
                patch("app.cli._print_status") as status, \
                patch("app.cli.hidden_spawn") as spawn, \
                patch("app.cli.close_installed_ui") as close_ui, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.cli_main(argv)
        status.assert_not_called()
        spawn.assert_not_called()
        close_ui.assert_not_called()
        return code, output.getvalue(), errors.getvalue(), request

    def test_add_multiple_urls_and_hosts_uses_direct_subdomains_by_default(self):
        result = document(rule("example.com"), rule("example.org"), message="已添加 2 条规则。")
        code, output, errors, request = self.run_cli(
            ["whitelist", "add", "https://Example.COM/path?q=1", "example.org"], result,
        )
        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        request.assert_called_once_with("whitelist-add", {
            "text": "https://Example.COM/path?q=1\nexample.org", "mode": "direct", "subdomains": True,
        })
        self.assertIn("已添加 2 条规则", output)
        self.assertIn("example.com · 直连 · 包含子域名", output)
        self.assertIn("example.org · 直连 · 包含子域名", output)
        self.assertIn("下次启动分流时生效", output)

    def test_add_can_force_vpn_for_exact_host(self):
        code, output, _, request = self.run_cli(
            ["whitelist", "add", "example.com", "--mode", "vpn", "--exact"],
            document(rule(mode="vpn", subdomains=False)),
        )
        self.assertEqual(code, 0)
        request.assert_called_once_with("whitelist-add", {
            "text": "example.com", "mode": "vpn", "subdomains": False,
        })
        self.assertIn("example.com · VPN · 仅此域名", output)

    def test_live_add_explains_new_connection_timing(self):
        code, output, _, _ = self.run_cli(
            ["whitelist", "add", "example.com"],
            document(rule(), active=True, applied_revision=2),
        )
        self.assertEqual(code, 0)
        self.assertIn("约 1 秒后用于新连接", output)
        self.assertIn("现有连接保持原路线", output)
        self.assertNotIn("下次启动", output)

    def test_remove_defaults_to_subdomain_scope_and_accepts_url(self):
        code, output, _, request = self.run_cli(
            ["whitelist", "remove", "https://example.com/path"],
            document(rule("other.example", mode="vpn"), message="已删除规则。"),
        )
        self.assertEqual(code, 0)
        request.assert_called_once_with("whitelist-remove", {
            "host": "https://example.com/path", "subdomains": True,
        })
        self.assertIn("已删除规则", output)
        self.assertIn("other.example · VPN · 包含子域名", output)

    def test_remove_exact_keeps_scope_explicit(self):
        code, _, _, request = self.run_cli(
            ["whitelist", "remove", "example.com", "--exact"], document(),
        )
        self.assertEqual(code, 0)
        request.assert_called_once_with("whitelist-remove", {"host": "example.com", "subdomains": False})

    def test_list_json_preserves_full_document_without_status_text(self):
        result = document(
            rule("xn--fiqs8s.example", mode="vpn", subdomains=False),
            active=True, revision=4, applied_revision=4,
        )
        code, output, errors, request = self.run_cli(["whitelist", "list", "--json"], result)
        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        self.assertEqual(json.loads(output), result)
        request.assert_called_once_with("whitelist-list", None)

    def test_list_text_distinguishes_routes_and_subdomain_scope(self):
        code, output, _, request = self.run_cli(
            ["whitelist", "list"],
            document(rule(), rule("api.example.com", mode="vpn", subdomains=False)),
        )
        self.assertEqual(code, 0)
        self.assertIn("2 条规则", output)
        self.assertIn("example.com · 直连 · 包含子域名", output)
        self.assertIn("api.example.com · VPN · 仅此域名", output)
        request.assert_called_once_with("whitelist-list", None)

    def test_empty_list_has_clear_message(self):
        code, output, _, _ = self.run_cli(["whitelist", "list"], document())
        self.assertEqual(code, 0)
        self.assertIn("0 条规则", output)
        self.assertIn("当前没有白名单规则", output)

    def test_invalid_rule_uses_existing_service_error_exit(self):
        code, output, errors, request = self.run_cli(
            ["whitelist", "add", "not/a/host"], error=cli.ServiceError("请输入有效的域名。"),
        )
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("未完成：请输入有效的域名。", errors)
        request.assert_called_once()

    def test_invalid_mode_is_rejected_before_rpc(self):
        with patch("app.cli.request") as request, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                cli.cli_main(["whitelist", "add", "example.com", "--mode", "unknown"])
        self.assertEqual(raised.exception.code, 2)
        request.assert_not_called()

    def test_missing_action_or_target_never_opens_gui_or_sends_rpc(self):
        for argv in (["whitelist"], ["whitelist", "add"], ["whitelist", "remove"]):
            with self.subTest(argv=argv), patch("app.cli.request") as request, \
                    patch("app.cli.hidden_spawn") as spawn, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    cli.cli_main(argv)
                self.assertEqual(raised.exception.code, 2)
                request.assert_not_called()
                spawn.assert_not_called()

    def test_whitelist_help_contains_quick_examples(self):
        output = io.StringIO()
        with patch("app.cli.request") as request, contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                cli.cli_main(["whitelist", "--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("orange whitelist add", output.getvalue())
        self.assertIn("--mode vpn --exact", output.getvalue())
        self.assertIn("orange whitelist list --json", output.getvalue())
        request.assert_not_called()

    def test_fresh_process_import_and_command_do_not_load_qt(self):
        # A fresh process avoids an already-loaded Qt module masking an import.
        source = """
import builtins
import json
original_import = builtins.__import__
def without_qt(name, *args, **kwargs):
    if name.startswith(('PySide', 'PyQt', 'qtpy')):
        raise AssertionError('CLI imported Qt: ' + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = without_qt
from unittest.mock import patch
from app import cli
value = {'version': 1, 'revision': 0, 'rules': [], 'active': False, 'applied_revision': None}
with patch('app.cli.request', return_value=value) as request, patch('app.cli.hidden_spawn') as spawn:
    assert cli.cli_main(['whitelist', 'list', '--json']) == 0
    request.assert_called_once_with('whitelist-list', None)
    spawn.assert_not_called()
"""
        completed = subprocess.run(
            [sys.executable, "-c", source], cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, text=True, encoding="utf-8", timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["rules"], [])


if __name__ == "__main__":
    unittest.main()
