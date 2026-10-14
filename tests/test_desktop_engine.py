"""Read-only and mocked tests; never start the real worker or alter a proxy."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

from app import client, cli, engine


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orange-engine-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.resources = self.root / "resources"
        self.resources.mkdir()
        (self.resources / "runtime.ps1").write_text("# mock", encoding="utf-8")
        (self.resources / "快橙一键启动.cmd").write_text("rem mock", encoding="utf-8")
        self.coordinator = engine.Coordinator(self.resources, self.root / "local")

    def _worker(self, result=0, restored=True):
        session = self.root / "session"
        session.mkdir(exist_ok=True)
        engine.atomic_json(session / "worker-status.json", dict(State="Stopped", Result=result, ProxyRestored=restored))
        worker = Mock()
        worker.poll.return_value = result
        worker.wait.return_value = result
        self.coordinator._worker = worker
        self.coordinator._session = session
        return worker, session

    @patch("app.engine.vendor_shortcut", return_value=None)
    @patch("app.engine.vpn_running", return_value=False)
    def test_initial_service_is_read_only(self, *_):
        with patch("app.engine.hidden_spawn") as spawn:
            self.coordinator.refresh()
            self.assertEqual(self.coordinator.status()["state"], "stopped")
            spawn.assert_not_called()

    def test_embedding_reuses_running_vendor_without_clickonce_activation(self):
        with patch("app.engine.vpn_running", return_value=True), patch("app.engine.vendor_shortcut") as shortcut, patch("app.engine.os.startfile", create=True) as activate:
            result = self.coordinator.dispatch("open-vpn", {"embed": True})
        self.assertEqual(result["state"], "stopped")
        shortcut.assert_not_called()
        activate.assert_not_called()

    @patch("app.engine.vendor_shortcut", return_value=None)
    @patch("app.engine.vpn_running", return_value=True)
    def test_stop_only_own_worker_and_confirms_cleanup(self, *_):
        worker, session = self._worker()
        self.coordinator._state = "running"
        result = self.coordinator.stop()
        self.assertTrue((session / "stop").is_file())
        self.assertEqual(result["state"], "stopped")
        self.assertTrue(result["proxy_restored"])
        worker.terminate.assert_not_called()
        worker.kill.assert_not_called()

    def test_stop_without_ownership_never_stops_other_helper(self):
        with patch("app.engine.hidden_spawn") as spawn:
            self.assertEqual(self.coordinator.stop()["state"], "stopped")
            spawn.assert_not_called()

    def test_shutdown_does_not_spawn_daemon_when_absent(self):
        with patch("app.client._endpoint", return_value=None), patch("app.client.ensure_service") as ensure, patch("app.client._send") as send:
            result = client.request("shutdown")
        self.assertEqual(result["state"], "stopped")
        ensure.assert_not_called()
        send.assert_not_called()

    def test_shutdown_waits_for_exact_daemon_identity_exit(self):
        endpoint = {"pid": 42, "ticks": 123, "port": 12345, "token": "test"}
        with patch("app.client._endpoint", return_value=endpoint), patch("app.client.ensure_service") as ensure, patch("app.client._send", return_value={"state": "stopped", "proxy_restored": True}), patch("app.client.process_identity", side_effect=[(123, "daemon.exe"), (999, "other.exe")]), patch("app.client.time.sleep"):
            result = client.request("shutdown")
        ensure.assert_not_called()
        self.assertTrue(result["proxy_restored"])
        self.assertIn("后台已退出", result["message"])

    def test_shutdown_refuses_during_pending_recovery(self):
        self.coordinator._recovery = Mock()
        with patch.object(self.coordinator, "stop") as stop:
            with self.assertRaises(engine.ServiceError):
                self.coordinator.dispatch("shutdown")
            stop.assert_not_called()

    def test_shutdown_refuses_new_start_and_failed_stop_reopens_control(self):
        self.coordinator.dispatch("shutdown")
        with self.assertRaises(engine.ServiceError):
            self.coordinator.dispatch("start")
        self.coordinator._shutdown_requested = False
        with patch.object(self.coordinator, "stop", side_effect=engine.ServiceError("mock timeout")):
            with self.assertRaises(engine.ServiceError):
                self.coordinator.dispatch("shutdown")
        self.assertFalse(self.coordinator._shutdown_requested)

    def test_installer_close_ui_revalidates_identity_and_waits_for_exit(self):
        executable = str(self.root / "OrangeDesk.exe")
        with patch("app.engine.gui_command", return_value=[executable]), patch("app.engine._matching_gui_windows", return_value=[(101, 11, 123)]), patch("app.engine._post_gui_close") as close, patch("app.engine.process_identity", side_effect=[(123, executable), (123, executable), None]), patch("app.engine.time.sleep"):
            engine.close_installed_ui()
        close.assert_called_once_with(101, 11)

    def test_installer_does_not_close_reused_pid_or_other_install(self):
        executable = str(self.root / "OrangeDesk.exe")
        with patch("app.engine.gui_command", return_value=[executable]), patch("app.engine._matching_gui_windows", return_value=[(101, 11, 123), (102, 12, 123)]), patch("app.engine._post_gui_close") as close, patch("app.engine.process_identity", side_effect=[(124, executable), (123, str(self.root / "other/OrangeDesk.exe"))]):
            engine.close_installed_ui()
        close.assert_not_called()

    def test_installer_close_ui_timeout_blocks_shutdown(self):
        executable = str(self.root / "OrangeDesk.exe")
        with patch("app.engine.gui_command", return_value=[executable]), patch("app.engine._matching_gui_windows", return_value=[(101, 11, 123)]), patch("app.engine._post_gui_close"), patch("app.engine.process_identity", return_value=(123, executable)):
            with self.assertRaises(engine.ServiceError):
                engine.close_installed_ui(timeout=0)
        with patch("app.cli.close_installed_ui", side_effect=engine.ServiceError("close failed")), patch("app.cli.request") as request, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.cli_main(["shutdown", "--close-ui"]), 1)
            request.assert_not_called()

    def test_cli_installer_closes_ui_before_shutdown_rpc(self):
        order = []
        with patch("app.cli.close_installed_ui", side_effect=lambda: order.append("close")), patch("app.cli.request", side_effect=lambda *args: order.append("shutdown") or {"state": "stopped"}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cli_main(["shutdown", "--close-ui"]), 0)
        self.assertEqual(order, ["close", "shutdown"])

    def test_stop_timeout_preserves_process_and_recovery_record(self):
        worker, session = self._worker()
        worker.poll.return_value = None
        worker.wait.side_effect = subprocess.TimeoutExpired("mock", 15)
        self.coordinator._state = "running"
        with self.assertRaises(engine.ServiceError):
            self.coordinator.stop()
        result = self.coordinator.status()
        self.assertEqual(result["state"], "error")
        self.assertIs(self.coordinator._worker, worker)
        self.assertTrue((session / "worker-status.json").is_file())
        worker.kill.assert_not_called()

    @patch("app.engine.vendor_shortcut", return_value=None)
    @patch("app.engine.vpn_running", return_value=True)
    def test_failure_is_not_reported_as_success(self, *_):
        _, session = self._worker(result=1, restored=False)
        (session / "monitor.log").write_text("12:00:00 工具停止：已有快橙监测或分流工具在运行，请先关闭旧工具。\n", encoding="utf-8")
        with self.assertRaisesRegex(engine.ServiceError, "旧工具"):
            self.coordinator.stop()
        self.assertEqual(self.coordinator.status()["state"], "error")

    @patch("app.engine.vendor_shortcut", return_value=None)
    @patch("app.engine.vpn_running", return_value=True)
    def test_external_settings_preserved_no_false_restore_claim(self, *_):
        self._worker(restored=False)
        result = self.coordinator.stop()
        self.assertEqual(result["state"], "stopped")
        self.assertFalse(result["proxy_restored"])
        self.assertIn("外部", result["message"])

    def test_pause_only_toggles_own_sentinel(self):
        worker, session = self._worker()
        worker.poll.return_value = None
        self.assertTrue(self.coordinator.set_paused(True)["paused"])
        self.assertTrue((session / "pause").is_file())
        self.assertFalse(self.coordinator.set_paused(False)["paused"])
        self.assertFalse((session / "pause").exists())

    def test_cancel_before_start_does_not_open_vpn_or_spawn(self):
        self.coordinator._stop_requested.set()
        with patch("app.engine.process_identity", return_value=(123, "python.exe")), patch.object(self.coordinator, "open_vpn") as opener, patch("app.engine.hidden_spawn") as spawn:
            self.coordinator._start_job(False)
            opener.assert_not_called()
            spawn.assert_not_called()
        self.assertEqual(self.coordinator.status()["state"], "stopped")

    def test_start_returns_ownership_only_for_newly_accepted_session(self):
        with patch("app.engine.threading.Thread") as thread:
            self.assertTrue(self.coordinator.start()["started"])
            self.assertFalse(self.coordinator.start()["started"])
            self.coordinator._state = "running"
            self.assertFalse(self.coordinator.start()["started"])
            thread.assert_called_once()
            thread.return_value.start.assert_called_once()
        self.assertNotIn("started", self.coordinator.status())

    def test_start_has_controller_identity_and_packaged_bundle(self):
        worker = Mock(pid=123)
        worker.poll.return_value = None
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}), patch("app.engine.process_identity", return_value=(639000000000000000, "python.exe")), patch("app.engine.vpn_running", return_value=True), patch.object(self.coordinator, "open_vpn") as opener, patch("app.engine.powershell_path", return_value="powershell.exe"), patch("app.engine.hidden_spawn", return_value=worker) as spawn:
            self.coordinator._start_job(True)
            opener.assert_not_called()
        args = spawn.call_args.args[0]
        self.assertEqual(args[args.index("-Role") + 1], "Worker")
        self.assertEqual(args[args.index("-ControllerId") + 1], str(os.getpid()))
        self.assertEqual(args[args.index("-ControllerTicks") + 1], "639000000000000000")
        self.assertEqual(args[args.index("-StateRoot") + 1], str(self.coordinator.root))
        self.assertEqual(self.coordinator._session.parent, self.coordinator.root / 'network/sessions')
        session = self.coordinator._session
        self.assertTrue((session / "pause").is_file())
        self.assertEqual(engine.read_json(session / "bundle.json")["Path"], str(self.resources / "快橙一键启动.cmd"))
        self.coordinator._output.close()
        self.coordinator._output = None

    def test_start_opens_vendor_only_when_not_running(self):
        with patch("app.engine.process_identity", return_value=(123, "python.exe")), patch("app.engine.vpn_running", return_value=False), patch.object(self.coordinator, "open_vpn", side_effect=engine.ServiceError("mock activation")) as opener, patch("app.engine.hidden_spawn") as spawn:
            self.coordinator._start_job(False)
        opener.assert_called_once()
        spawn.assert_not_called()
        self.assertEqual(self.coordinator.status()["state"], "error")

    def test_status_does_not_wait_for_operation_lock(self):
        self.coordinator._operations.acquire()
        try:
            begin = time.monotonic()
            self.coordinator.status()
            self.assertLess(time.monotonic() - begin, 0.05)
        finally:
            self.coordinator._operations.release()

    @patch("app.engine.vendor_shortcut", return_value=None)
    @patch("app.engine.vpn_running", return_value=True)
    def test_connected_requires_ui_and_vpn_route(self, *_):
        worker, session = self._worker()
        worker.poll.return_value = None
        engine.atomic_json(session / "worker-status.json", {"State": "Running"})
        engine.atomic_json(session / "ui-response.json", {"Value": {"State": "Connected"}})
        engine.atomic_json(session / "split-status.json", {"VpnReady": False})
        self.coordinator.refresh()
        self.assertFalse(self.coordinator.status()["connected"])
        engine.atomic_json(session / "split-status.json", {"VpnReady": True})
        self.coordinator.refresh()
        self.assertTrue(self.coordinator.status()["connected"])
        self.coordinator._worker = None
        self.coordinator.refresh()
        self.assertFalse(self.coordinator.status()["connected"])

    @patch("app.engine.vendor_shortcut", return_value=None)
    @patch("app.engine.vpn_running", return_value=True)
    def test_startup_timeout_requests_stop_without_killing(self, *_):
        worker, session = self._worker()
        worker.poll.return_value = None
        (session / "worker-status.json").unlink()
        self.coordinator._state = "starting"
        self.coordinator._start_deadline = time.monotonic() - 1
        self.coordinator.refresh()
        self.assertTrue((session / "stop").is_file())
        self.assertEqual(self.coordinator.status()["state"], "error")
        worker.kill.assert_not_called()

    def test_endpoint_rejects_pid_reuse_and_other_executable(self):
        saved = {"pid": 11, "ticks": 123, "port": 5555, "token": "x" * 48, "executable": "expected.exe"}
        with patch("app.client.read_json", return_value=saved), patch("app.client.process_identity", return_value=(124, "expected.exe")):
            self.assertIsNone(client._endpoint())
        with patch("app.client.read_json", return_value=saved), patch("app.client.process_identity", return_value=(123, "expected.exe")), patch("app.client.gui_command", return_value=["other.exe"]):
            self.assertIsNone(client._endpoint())

    def test_rpc_rejects_missing_auth_and_bad_host(self):
        server = engine.RpcServer(self.coordinator, "secret-test-token")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/rpc"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            body = b'{"action":"status","payload":{}}'
            with self.assertRaises(urllib.error.HTTPError) as missing:
                opener.open(urllib.request.Request(url, body), timeout=2)
            self.assertEqual(missing.exception.code, 403)
            with self.assertRaises(urllib.error.HTTPError) as wrong_host:
                opener.open(urllib.request.Request(url, body, {"Authorization": "Bearer secret-test-token", "Host": "evil.example"}), timeout=2)
            self.assertEqual(wrong_host.exception.code, 403)
            with opener.open(urllib.request.Request(url, body, {"Authorization": "Bearer secret-test-token"}), timeout=2) as response:
                self.assertTrue(json.load(response)["ok"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_rpc_shutdown_replies_then_exits(self):
        server = engine.RpcServer(self.coordinator, "test-shutdown-token")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = {"port": server.server_address[1], "token": "test-shutdown-token"}
            result = client._send(endpoint, "shutdown", None, 2)
            self.assertEqual(result["state"], "stopped")
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        finally:
            server.shutdown()
            server.server_close()

    def test_cli_json_does_not_import_qt(self):
        result = self.coordinator.status()
        output = io.StringIO()
        with patch("app.cli.request", return_value=result), contextlib.redirect_stdout(output):
            self.assertEqual(cli.cli_main(["status", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["version"], engine.APP_VERSION)

    @unittest.skipUnless(os.name == "nt", "Windows identity check")
    def test_native_creation_ticks_match_dotnet_exactly(self):
        ticks, image = engine.process_identity(os.getpid())
        command = f"(Get-Process -Id {os.getpid()}).StartTime.ToUniversalTime().Ticks"
        process = engine.hidden_spawn([engine.powershell_path(), "-NoProfile", "-Command", command], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, error = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, error)
        self.assertEqual(ticks, int(output.strip()))
        self.assertTrue(Path(image).is_file())


@unittest.skipUnless(os.name == "nt", "Windows file sharing")
class AtomicJsonTests(unittest.TestCase):
    def test_transient_reader_lock_retries_and_publishes_complete_json(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "lease.json"
            engine.atomic_json(target, {"state": "old"})
            replace = os.replace
            attempts = 0
            def briefly_locked(source, destination):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise PermissionError("brief Windows reader lock")
                return replace(source, destination)
            with patch("app.engine.os.replace", side_effect=briefly_locked), patch("app.engine.time.sleep"):
                engine.atomic_json(target, {"state": "released"})
            self.assertEqual(attempts, 2)
            self.assertEqual(json.loads(target.read_text()), {"state": "released"})
            self.assertEqual(list(Path(temp).glob("*.tmp")), [])

    def test_permanent_lock_is_bounded_and_preserves_previous_record(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "lease.json"
            engine.atomic_json(target, {"state": "old"})
            with patch("app.engine.os.replace", side_effect=PermissionError("locked")) as replace, patch("app.engine.time.sleep"):
                with self.assertRaises(PermissionError):
                    engine.atomic_json(target, {"state": "released"})
            self.assertEqual(replace.call_count, 12)
            self.assertEqual(json.loads(target.read_text()), {"state": "old"})
            self.assertEqual(list(Path(temp).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
