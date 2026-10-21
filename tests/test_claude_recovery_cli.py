"""Recovery CLI tests mock all system actions, UAC, rules, processes and waits."""
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from app import cli, single_file
from app.claude_protection import ProtectionError


class ClaudeRecoveryCliTests(unittest.TestCase):
    def setUp(self):
        self.manager = Mock()
        self.constructor = patch.object(cli, "ClaudeProtection", return_value=self.manager).start()
        self.root = patch.object(cli.engine, "service_root", return_value=Path("C:/fake/data")).start()
        self.resources = patch.object(cli.engine, "resource_root", return_value=Path("C:/fake/resources")).start()
        self.powershell = patch.object(cli.engine, "powershell_path", return_value="fake-powershell.exe").start()
        self.sid = patch.object(cli.engine, "user_sid", return_value="S-1-5-21-1234").start()
        self.rpc = patch.object(cli, "request").start()
        self.spawn = patch.object(cli, "hidden_spawn").start()
        self.sleep = patch.object(cli.time, "sleep").start()
        self.addCleanup(patch.stopall)

    def run_cli(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = cli.cli_main(["claude-protection", *args])
        self.rpc.assert_not_called()
        self.spawn.assert_not_called()
        return result, output.getvalue(), errors.getvalue()

    def test_status_directly_queries_manager_and_returns_json(self):
        self.manager.status.return_value = dict(state="enabled", enabled=True, filter_count=4)
        code, output, errors = self.run_cli("status", "--json")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)["enabled"])
        self.assertEqual(errors, "")
        self.constructor.assert_called_once_with(Path("C:/fake/data"), Path("C:/fake/resources"), "fake-powershell.exe", self.sid)
        self.manager._query.assert_called_once_with()
        self.manager.change.assert_not_called()

    def test_status_unknown_or_failed_returns_one(self):
        for state in ("error", "failed", "pending", "unknown"):
            self.manager.status.return_value = dict(state=state, enabled=None, message="not confirmed")
            self.assertEqual(self.run_cli("status", "--json")[0], 1)

    def test_inconsistent_or_partial_status_is_not_confirmed(self):
        for value in (dict(state="enabled", enabled=False), dict(state="disabled", enabled=None),
                      dict(state="disabled", enabled=False, filter_count=2)):
            self.manager.status.return_value = value
            code, output, _ = self.run_cli("status")
            self.assertEqual(code, 1)
            self.assertIn("状态未确认", output)

    def test_disable_waits_for_receipt_then_queries_actual_state(self):
        self.manager.change.return_value = dict(state="pending", enabled=True)
        disabled = dict(state="disabled", enabled=False, filter_count=0, message="removed")
        self.manager.status.side_effect = [dict(state="pending"), disabled, disabled]
        code, output, _ = self.run_cli("disable", "--json")
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output)["enabled"])
        self.manager.change.assert_called_once_with(False)
        self.manager._query.assert_called_once_with()
        self.assertEqual(self.sleep.call_count, 2)

    def test_disable_cancelled_authorization_returns_clear_failure(self):
        self.manager.change.side_effect = ProtectionError("Windows 管理员授权已取消")
        code, output, _ = self.run_cli("disable", "--json")
        self.assertEqual(code, 1)
        self.assertIn("已取消", json.loads(output)["message"])

    def test_disable_timeout_does_not_claim_rules_removed(self):
        self.manager.change.return_value = dict(state="pending", enabled=True)
        self.manager.status.return_value = dict(state="pending", enabled=True)
        with patch.object(cli.time, "monotonic", side_effect=[0, 121]):
            code, output, _ = self.run_cli("disable", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["state"], "error")
        self.assertFalse(json.loads(output)["verified"])
        self.manager._query.assert_not_called()

    def test_disable_failed_or_partial_receipt_returns_one(self):
        for value in (dict(state="error", enabled=True), dict(state="failed", enabled=False),
                      dict(state="disabled", enabled=False, filter_count=2)):
            self.manager.change.return_value = value
            self.manager.status.return_value = value
            self.assertEqual(self.run_cli("disable", "--json")[0], 1)

    def test_if_present_confirmed_absent_skips_uac(self):
        self.manager.status.return_value = dict(state="disabled", enabled=False, filter_count=0)
        code, _, _ = self.run_cli("disable", "--if-present", "--json")
        self.assertEqual(code, 0)
        self.manager._query.assert_called_once_with()
        self.manager.change.assert_not_called()

    def test_if_present_unknown_or_existing_rules_still_requests_uac(self):
        disabled = dict(state="disabled", enabled=False, filter_count=0)
        for initial in (dict(state="error", enabled=None), dict(state="enabled", enabled=True, filter_count=4),
                        dict(state="disabled", enabled=False, filter_count=2)):
            self.manager.reset_mock()
            self.manager.status.side_effect = [initial, disabled]
            self.manager.change.return_value = disabled
            self.assertEqual(self.run_cli("disable", "--if-present", "--json")[0], 0)
            self.manager.change.assert_called_once_with(False)

    def test_post_receipt_query_failure_is_not_success(self):
        self.manager.change.return_value = dict(state="disabled", enabled=False, filter_count=0)
        self.manager.status.return_value = dict(state="error", enabled=False, message="cannot verify")
        self.assertEqual(self.run_cli("disable", "--json")[0], 1)

    def test_parent_json_option_and_human_output(self):
        self.manager.status.return_value = dict(state="disabled", enabled=False, message="规则已核验移除")
        self.assertEqual(json.loads(self.run_cli("--json", "status")[1])["state"], "disabled")
        self.assertIn("已核验关闭", self.run_cli("status")[1])

    def test_enable_is_not_exposed_by_cli(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.cli_main(["claude-protection", "enable"])
        self.constructor.assert_not_called()
        self.rpc.assert_not_called()

    def test_single_file_dispatches_recovery_command_without_gui(self):
        with patch.object(single_file, "prepare_console") as console, patch.object(cli, "cli_main", return_value=0) as main:
            self.assertEqual(single_file.single_main(["claude-protection", "status", "--json"]), 0)
        console.assert_called_once_with()
        main.assert_called_once_with(["claude-protection", "status", "--json"])


if __name__ == "__main__":
    unittest.main()
