"""Mock-only coordinator/client/recovery integration; no UAC, service or network."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

from app import cli, client, engine, single_file
from app.wechat_direct import WeChatDirectError


class WechatCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orange-wechat-engine-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.resources = self.root / "resources"
        self.manager = Mock()
        self.initial = dict(desired_enabled=True, enabled=False, state="not-installed", message="needs setup")
        self.manager.status.return_value = self.initial
        constructor = patch.object(engine, "WeChatDirect", return_value=self.manager)
        self.constructor = constructor.start()
        self.addCleanup(constructor.stop)
        for name in ("ClaudeProtection", "NetworkDiagnostics"):
            patcher = patch.object(engine, name)
            result = patcher.start()
            result.return_value.status.return_value = dict(active=False, state="stopped", session_path="")
            self.addCleanup(patcher.stop)
        self.coordinator = engine.Coordinator(self.resources, self.root / "data")

    def test_constructor_and_base_status_do_not_install_or_change_preference(self):
        expected_ps = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")
        self.constructor.assert_called_once_with(self.root / "data", self.resources, expected_ps, engine.user_sid)
        self.assertEqual(self.coordinator.status()["wechat_direct"], self.initial)
        self.assertFalse(self.coordinator.status()["wechat_direct"]["enabled"])
        self.manager.change.assert_not_called()
        self.manager.refresh.assert_not_called()

    def test_refresh_updates_actual_state_without_enabling_or_installing(self):
        actual = dict(desired_enabled=True, enabled=True, state="running", message="verified")
        self.manager.status.return_value = actual
        with patch.object(engine, "vpn_running", return_value=False), patch.object(engine, "vendor_shortcut", return_value=None), patch.object(engine, "hidden_spawn") as spawn:
            self.coordinator.refresh()
        self.manager.refresh.assert_called_once_with()
        self.manager.change.assert_not_called()
        self.assertEqual(self.coordinator.status()["wechat_direct"], actual)
        self.assertEqual(self.coordinator.status()["state"], "stopped")
        spawn.assert_not_called()

    def test_rpc_enable_and_disable_work_while_companion_is_stopped(self):
        for action, flag in (("wechat-direct-enable", True), ("wechat-direct-disable", False)):
            with self.subTest(action=action):
                self.manager.reset_mock()
                value = dict(desired_enabled=True, enabled=False, state="pending", message="waiting for UAC")
                self.manager.change.return_value = value
                with patch.object(engine, "hidden_spawn") as spawn:
                    result = self.coordinator.dispatch(action)
                self.manager.change.assert_called_once_with(flag)
                self.assertEqual(result, {"wechat_direct": value, "message": value["message"]})
                self.assertEqual(self.coordinator.status()["wechat_direct"], value)
                self.assertEqual(self.coordinator.status()["state"], "stopped")
                spawn.assert_not_called()

    def test_rpc_manager_failure_is_service_error_without_false_cached_success(self):
        self.manager.change.side_effect = WeChatDirectError("管理员授权取消")
        with self.assertRaisesRegex(engine.ServiceError, "管理员授权取消"):
            self.coordinator.dispatch("wechat-direct-enable")
        self.assertEqual(self.coordinator.status()["wechat_direct"], self.initial)

    def test_client_timeout_is_twenty_seconds_and_mutation_is_not_retried(self):
        endpoint = {"port": 1}
        for action in ("wechat-direct-enable", "wechat-direct-disable"):
            with self.subTest(action=action), patch.object(client, "ensure_service") as ensure, patch.object(client, "_endpoint", return_value=endpoint), patch.object(client, "_send", return_value={"state": "pending"}) as send:
                client.request(action)
                ensure.assert_called_once_with()
                send.assert_called_once_with(endpoint, action, None, 20)
        with patch.object(client, "ensure_service") as ensure, patch.object(client, "_endpoint", return_value=endpoint), patch.object(client, "_send", side_effect=urllib.error.URLError("offline")) as send:
            with self.assertRaises(engine.ServiceError):
                client.request("wechat-direct-enable")
            self.assertEqual(send.call_count, 1)
            self.assertEqual(ensure.call_count, 1)


class WechatRecoveryCliTests(unittest.TestCase):
    def setUp(self):
        self.manager = Mock()
        for target, value in (("WeChatDirect", self.manager),):
            patcher = patch.object(cli, target, return_value=value)
            self.constructor = patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (("service_root", Path("C:/fake/data")), ("resource_root", Path("C:/fake/resources")),
                            ("powershell_path", "fake-powershell.exe"), ("user_sid", "S-1-5-21-1234")):
            patcher = patch.object(cli.engine, name, return_value=value)
            mocked = patcher.start()
            self.addCleanup(patcher.stop)
            if name == "user_sid":
                self.sid = mocked
        for name in ("request", "hidden_spawn"):
            patcher = patch.object(cli, name, side_effect=AssertionError("must not start daemon, proxy or application"))
            mocked = patcher.start()
            self.addCleanup(patcher.stop)
            setattr(self, name, mocked)
        patcher = patch.object(cli.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = cli.cli_main(["wechat-direct", *args])
        self.request.assert_not_called()
        self.hidden_spawn.assert_not_called()
        return result, output.getvalue(), errors.getvalue()

    def test_status_directly_queries_manager_without_requesting_uac(self):
        self.manager.status.return_value = dict(state="running", enabled=True, desired_enabled=True)
        code, output, errors = self.run_cli("status", "--json")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)["enabled"])
        self.assertEqual(errors, "")
        self.constructor.assert_called_once_with(Path("C:/fake/data"), Path("C:/fake/resources"), "fake-powershell.exe", self.sid)
        self.manager._query.assert_called_once_with()
        self.manager.change.assert_not_called()

    def test_status_requires_consistent_actual_evidence(self):
        for value in (dict(state="error", enabled=False), dict(state="checking", enabled=True),
                      dict(state="pending", enabled=False), dict(state="running", enabled=False),
                      dict(state="disabled", enabled=True), dict(state="not-installed", enabled=None)):
            with self.subTest(value=value):
                self.manager.status.return_value = value
                self.assertEqual(self.run_cli("status", "--json")[0], 1)

    def test_confirmed_not_installed_if_present_is_success_without_uac(self):
        for state in ("not-installed", "needs-setup"):
            with self.subTest(state=state):
                self.manager.status.return_value = dict(state=state, enabled=False, desired_enabled=True)
                code, output, _ = self.run_cli("disable", "--if-present", "--json")
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(output)["state"], state)
                self.manager.change.assert_not_called()

    def test_unknown_if_present_still_requests_disable(self):
        disabled = dict(state="disabled", enabled=False, desired_enabled=False)
        for initial in (dict(state="error", enabled=None), dict(state="checking", enabled=False),
                        dict(state="running", enabled=True), dict(state="not-installed", enabled=True)):
            with self.subTest(initial=initial):
                self.manager.reset_mock()
                self.manager.status.side_effect = [initial, disabled]
                self.manager.change.return_value = disabled
                self.assertEqual(self.run_cli("disable", "--if-present", "--json")[0], 0)
                self.manager.change.assert_called_once_with(False)

    def test_disable_waits_in_half_second_intervals_then_rechecks_actual(self):
        disabled = dict(state="disabled", enabled=False, desired_enabled=False)
        self.manager.change.return_value = dict(state="pending", enabled=True)
        self.manager.status.side_effect = [dict(state="pending"), disabled, disabled]
        with patch.object(cli.time, "monotonic", return_value=0):
            code, output, _ = self.run_cli("disable", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["state"], "disabled")
        self.assertEqual(self.sleep.call_count, 2)
        self.assertTrue(all(call.args == (0.5,) for call in self.sleep.call_args_list))
        self.manager.change.assert_called_once_with(False)
        self.manager._query.assert_called_once_with()

    def test_ten_minute_timeout_never_reports_success(self):
        self.manager.change.return_value = dict(state="pending", enabled=True)
        with patch.object(cli.time, "monotonic", side_effect=[0, 601]):
            code, output, _ = self.run_cli("disable", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["state"], "error")
        self.assertIn("十分钟", json.loads(output)["message"])
        self.manager._query.assert_not_called()
        self.sleep.assert_not_called()

    def test_receipt_or_post_query_failure_never_claims_disabled(self):
        for receipt, actual in ((dict(state="error", enabled=False), None),
                                (dict(state="disabled", enabled=True), None),
                                (dict(state="not-installed", enabled=False), None),
                                (dict(state="disabled", enabled=False), dict(state="error", enabled=False))):
            with self.subTest(receipt=receipt, actual=actual):
                self.manager.reset_mock()
                self.manager.change.return_value = receipt
                self.manager.status.return_value = actual
                self.assertEqual(self.run_cli("disable", "--json")[0], 1)

    def test_authorization_error_is_json_failure(self):
        self.manager.change.side_effect = WeChatDirectError("管理员授权已取消")
        code, output, _ = self.run_cli("disable", "--json")
        self.assertEqual(code, 1)
        self.assertIn("已取消", json.loads(output)["message"])

    def test_parent_json_and_human_status_and_no_enable_command(self):
        self.manager.status.return_value = dict(state="disabled", enabled=False)
        self.assertEqual(json.loads(self.run_cli("--json", "status")[1])["state"], "disabled")
        self.assertIn("已核验关闭", self.run_cli("status")[1])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.cli_main(["wechat-direct", "enable"])

    def test_single_file_dispatches_recovery_without_loading_gui(self):
        with patch.object(single_file, "prepare_console") as console, patch.object(cli, "cli_main", return_value=0) as main, patch("app.main.main") as gui:
            self.assertEqual(single_file.single_main(["wechat-direct", "status", "--json"]), 0)
        console.assert_called_once_with()
        main.assert_called_once_with(["wechat-direct", "status", "--json"])
        gui.assert_not_called()


if __name__ == "__main__":
    unittest.main()
