"""Temporary files and mocked children only; no VPN, UI, elevation or network."""
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import zipfile

from app.diagnostics import KIND, NetworkDiagnostics


class NetworkDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orange-diagnostics-tests-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "diagnostics"
        self.resources = self.base / "resources"
        self.resources.mkdir()
        for name in ("Collect-NetworkDiagnostics.ps1", "Trace-NetworkDiagnostics.ps1"):
            (self.resources / name).write_text("# test resource", encoding="utf-8")
        self.companion = self.base / "network" / "sessions" / "companion"
        self.companion.mkdir(parents=True)
        self.child = Mock(pid=777)
        self.child.poll.return_value = None
        self.child.wait.return_value = 0
        self.spawn = Mock(return_value=self.child)
        self.diagnostics = NetworkDiagnostics(self.root, self.resources, "mock-powershell.exe", self.spawn)
        self.roaming = self.base / "roaming"
        self.environment = patch.dict(os.environ, {"APPDATA": str(self.roaming)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def start(self):
        result = self.diagnostics.start(self.companion)
        self.assertTrue(result["ok"], result)
        return Path(result["session_path"])

    def write_status(self, session, **data):
        (session / "status.json").write_text(json.dumps({"State": "Running", **data}), encoding="utf-8")

    def test_construction_and_status_are_read_only(self):
        self.assertFalse(self.root.exists())
        self.assertFalse(self.diagnostics.status()["active"])
        self.spawn.assert_not_called()

    def test_start_launches_one_owned_continuous_child(self):
        session = self.start()
        command = self.spawn.call_args.args[0]
        self.assertIn("-Continuous", command)
        self.assertEqual(command[command.index("-DurationMinutes") + 1], "1440")
        self.assertEqual(command[command.index("-CompanionSessionPath") + 1], str(self.companion))
        self.assertEqual(json.loads((session / "manifest.json").read_text(encoding="utf-8"))["kind"], KIND)
        self.assertTrue(self.diagnostics.start(self.companion)["active"])
        self.assertEqual(self.spawn.call_count, 1)
        self.assertNotIn("shell", self.spawn.call_args.kwargs)

    def test_missing_script_and_spawn_failure_are_visible(self):
        (self.resources / "Collect-NetworkDiagnostics.ps1").unlink()
        result = self.diagnostics.start(self.companion)
        self.assertFalse(result["ok"])
        self.spawn.assert_not_called()
        (self.resources / "Collect-NetworkDiagnostics.ps1").write_text("# test")
        self.spawn.side_effect = OSError("cannot launch child")
        result = self.diagnostics.start(self.companion)
        self.assertFalse(result["ok"])
        self.assertIn("cannot launch child", result["message"])

    def test_status_reports_real_child_exit_and_script_error(self):
        session = self.start()
        self.write_status(session, LastError="adapter query failed", IncidentAt="2026-10-20T12:30:00Z")
        self.assertIn("adapter query failed", self.diagnostics.status()["message"])
        self.child.poll.return_value = 9
        result = self.diagnostics.status()
        self.assertEqual(result["state"], "failed")
        self.assertFalse(result["active"])
        self.assertEqual(result["incident_at"], "2026-10-20T12:30:00Z")

    def test_stop_requests_only_owned_child_and_never_kills(self):
        session = self.start()
        self.child.wait.side_effect = subprocess.TimeoutExpired("collector", 5)
        result = self.diagnostics.stop()
        self.assertFalse(result["ok"])
        self.assertTrue((session / "stop.request").is_file())
        self.child.terminate.assert_not_called()
        self.child.kill.assert_not_called()

    def test_deep_requires_explicit_click_and_uses_mocked_elevation(self):
        with patch.object(self.diagnostics, "_elevate", return_value=33) as elevate:
            self.assertFalse(self.diagnostics.start_deep()["ok"])
            elevate.assert_not_called()
            session = self.start()
            elevate.assert_not_called()
            result = self.diagnostics.start_deep()
            self.assertTrue(result["ok"])
            self.assertEqual(result["deep_state"], "starting")
            command = elevate.call_args.args[0]
            self.assertIn("Trace-NetworkDiagnostics.ps1", " ".join(command))
            self.assertEqual(command[command.index("-SessionDir") + 1], str(session))
            self.diagnostics.start_deep()
            self.assertEqual(elevate.call_count, 1)

    def test_cancelled_elevation_does_not_claim_deep_running(self):
        self.start()
        with patch.object(self.diagnostics, "_elevate", return_value=5):
            result = self.diagnostics.start_deep()
        self.assertFalse(result["ok"])
        self.assertEqual(result["deep_state"], "stopped")

    def test_export_redacts_and_excludes_credentials_and_payload(self):
        session = self.start()
        self.write_status(session)
        (session / "baseline.json").write_text(json.dumps({"password": "hidden-password", "api_key": "hidden-key", "note": "Bearer hidden-bearer"}))
        (session / "observations.jsonl").write_text(json.dumps({"url": "https://name:pw@example.test/a?token=hidden-query", "Authorization": "Bearer hidden-auth"}) + "\n")
        (session / "credentials.json").write_text("must-never-read")
        (session / "wifi-old.etl").write_bytes(b"local-provider-metadata")
        (self.companion / "monitor.log").write_text('password=hidden-companion\nAuthorization: Bearer hidden-header\nAuthorization: Basic hidden-basic\n')
        (self.companion / "config.json").write_text("must-never-read-config")
        log = self.roaming / "FastOrange" / "Logs" / ("openvpn_" + datetime.now().strftime("%Y%m%d") + ".log")
        log.parent.mkdir(parents=True)
        log.write_text("[DEBUG] payload must-never-export\n[INFO] openvpn STATE: Connected\n[ERROR] password=hidden-vpn\n")
        output = self.base / "export.zip"
        result = self.diagnostics.export(output, self.companion)
        self.assertTrue(result["ok"], result)
        with zipfile.ZipFile(output) as archive:
            names = archive.namelist()
            text = "\n".join(archive.read(name).decode("utf-8") for name in names if not name.endswith(".etl"))
            for secret in ("hidden-password", "hidden-key", "hidden-bearer", "hidden-query", "hidden-auth", "hidden-companion", "hidden-header", "hidden-basic", "hidden-vpn", "must-never-export", "name:pw"):
                self.assertNotIn(secret, text)
            self.assertFalse(any("credentials" in name or "config.json" in name for name in names))
            self.assertTrue(any(name.endswith(".etl") for name in names))
            self.assertIn("STATE: Connected", text)
            self.assertIn("[REDACTED]", text)
            self.assertTrue(all("文件不存在" in error for error in json.loads(archive.read("export-manifest.json"))["errors"]))
        self.child.terminate.assert_not_called()

    def test_export_selects_current_and_three_owned_history_without_deleting(self):
        session = self.start()
        for number in range(5):
            history = self.root / ("session-2020010" + str(number))
            history.mkdir()
            (history / "manifest.json").write_text(json.dumps({"kind": KIND}))
            (history / "status.json").write_text('{}')
        foreign = self.root / "session-foreign"
        foreign.mkdir()
        (foreign / "private.txt").write_text("not-owned")
        output = self.base / "history.zip"
        result = self.diagnostics.export(output)
        self.assertTrue(result["ok"])
        with zipfile.ZipFile(output) as archive:
            manifest = json.loads(archive.read("export-manifest.json"))
            self.assertEqual(len(manifest["sessions"]), 4)
            self.assertIn(session.name, manifest["sessions"])
            self.assertNotIn("not-owned", " ".join(archive.namelist()))
        self.assertTrue((foreign / "private.txt").is_file())
        self.assertEqual(len(list(self.root.glob("session-*"))), 7)

    def test_deep_export_timeout_omits_unfinished_etl_but_keeps_collector(self):
        session = self.start()
        (session / "trace-status.json").write_text('{"State":"Running"}')
        (session / "wifi-live.etl").write_bytes(b"unfinished")
        with patch("app.diagnostics.DEEP_STOP_TIMEOUT_SECONDS", 0):
            result = self.diagnostics.export(self.base / "unfinished.zip")
        self.assertTrue(result["ok"])
        self.assertTrue((session / "trace-stop.request").is_file())
        self.assertFalse((session / "stop.request").exists())
        self.assertTrue(any("ETL" in error for error in result["errors"]))
        with zipfile.ZipFile(result["path"]) as archive:
            self.assertFalse(any(name.endswith(".etl") for name in archive.namelist()))

    def test_capacity_limit_is_reported_and_does_not_delete_sources(self):
        session = self.start()
        source = session / "large.txt"
        source.write_text("x" * 2000)
        with patch("app.diagnostics.MAX_EXPORT_BYTES", 100):
            result = self.diagnostics.export(self.base / "small.zip")
        self.assertTrue(result["ok"])
        self.assertTrue(any("large.txt" in error for error in result["errors"]))
        self.assertTrue(source.is_file())

    def test_invalid_destination_and_atomic_failure_preserve_old_export(self):
        self.start()
        self.assertFalse(self.diagnostics.export(self.base / "bad.txt")["ok"])
        output = self.base / "old.zip"
        output.write_bytes(b"previous export")
        with patch("app.diagnostics.os.replace", side_effect=OSError("cannot replace")):
            result = self.diagnostics.export(output)
        self.assertFalse(result["ok"])
        self.assertEqual(output.read_bytes(), b"previous export")
        self.assertFalse(list(self.base.glob(".old.zip.*.tmp")))

    def test_duration_expiry_and_live_but_stale_status_are_visible(self):
        session = self.start()
        self.write_status(session)
        past = time.time() - 40
        os.utime(session / "status.json", (past, past))
        result = self.diagnostics.status()
        self.assertEqual(result["state"], "stale")
        self.assertFalse(result["active"])
        self.assertFalse(self.diagnostics.start(self.companion)["ok"])
        self.assertEqual(self.spawn.call_count, 1)
        self.write_status(session, State="DurationComplete")
        result = self.diagnostics.status()
        self.assertEqual(result["state"], "stopped")
        self.assertIn("24", result["message"])

    def test_deep_timestamp_compares_timezones_and_timeout_allows_retry(self):
        session = self.start()
        with patch.object(self.diagnostics, "_elevate", return_value=33) as elevate:
            self.assertTrue(self.diagnostics.start_deep()["ok"])
            requested = self.diagnostics._deep_requested_at
            from datetime import timedelta, timezone
            requested_at = datetime.fromisoformat(requested)
            old_local = (requested_at - timedelta(seconds=1)).astimezone(timezone(timedelta(hours=8)))
            (session / "trace-status.json").write_text(json.dumps({"State": "Stopped", "UpdatedAt": old_local.isoformat()}))
            self.assertEqual(self.diagnostics.status()["deep_state"], "starting")
            self.diagnostics._deep_requested_at = (requested_at - timedelta(seconds=40)).isoformat()
            (session / "trace-status.json").unlink()
            self.assertEqual(self.diagnostics.status()["deep_state"], "error")
            self.assertTrue(self.diagnostics.start_deep()["ok"])
            self.assertEqual(elevate.call_count, 2)

    def test_running_deep_is_not_duplicated_and_export_waits_for_save(self):
        session = self.start()
        trace = session / "trace-status.json"
        trace.write_text('{"State":"Running"}')
        (session / "wifi-complete.etl").write_bytes(b"saved-metadata")
        with patch.object(self.diagnostics, "_elevate") as elevate:
            self.assertTrue(self.diagnostics.start_deep()["ok"])
            elevate.assert_not_called()
        def saved(_seconds):
            trace.write_text('{"State":"Stopped"}')
        with patch("app.diagnostics.time.sleep", side_effect=saved):
            result = self.diagnostics.export(self.base / "saved.zip")
        self.assertTrue(result["ok"])
        with zipfile.ZipFile(result["path"]) as archive:
            self.assertTrue(any(name.endswith("wifi-complete.etl") for name in archive.namelist()))
        self.assertFalse((session / "stop.request").exists())

    def test_all_text_previous_rotations_are_exported_and_redacted(self):
        session = self.start()
        for name in ("observations.jsonl.previous", "wifi-details.jsonl.previous", "notes.txt.previous", "baseline.json.previous"):
            (session / name).write_text('{"password":"rotation-secret"}\n', encoding="utf-8")
        result = self.diagnostics.export(self.base / "rotations.zip")
        self.assertTrue(result["ok"])
        with zipfile.ZipFile(result["path"]) as archive:
            rotations = [name for name in archive.namelist() if name.endswith(".previous")]
            self.assertEqual(len(rotations), 4)
            for name in rotations:
                self.assertNotIn("rotation-secret", archive.read(name).decode("utf-8"))

    def test_new_companion_stops_old_owned_collector_without_killing(self):
        old = self.start()
        new_companion = self.companion.with_name("new-companion")
        new_companion.mkdir()
        result = self.diagnostics.start(new_companion)
        self.assertTrue(result["ok"])
        self.assertNotEqual(result["session_path"], str(old))
        self.assertTrue((old / "stop.request").is_file())
        self.assertEqual(self.spawn.call_count, 2)
        command = self.spawn.call_args.args[0]
        self.assertEqual(command[command.index("-CompanionSessionPath") + 1], str(new_companion))
        self.child.kill.assert_not_called()

    def test_history_companion_logs_are_included_once_with_omissions(self):
        self.start()
        (self.companion / "monitor.log").write_text("current-monitor")
        historical_companion = self.companion.with_name("historic-companion")
        historical_companion.mkdir()
        (historical_companion / "monitor.log").write_text("historic-monitor")
        for number in range(2):
            history = self.root / ("session-2020010" + str(number))
            history.mkdir()
            (history / "manifest.json").write_text(json.dumps({"kind": KIND, "companion_session": str(historical_companion)}))
        result = self.diagnostics.export(self.base / "companion-history.zip")
        self.assertTrue(result["ok"])
        self.assertTrue(any("split-status.json" in item for item in result["omissions"]))
        with zipfile.ZipFile(result["path"]) as archive:
            historical = [name for name in archive.namelist() if "historic-companion/monitor.log" in name]
            self.assertEqual(len(historical), 1)
            self.assertEqual(archive.read(historical[0]), b"historic-monitor")

    def test_export_inside_session_does_not_package_itself(self):
        session = self.start()
        result = self.diagnostics.export(session / "inside.zip")
        self.assertTrue(result["ok"])
        with zipfile.ZipFile(result["path"]) as archive:
            self.assertFalse(any(name.endswith((".zip", ".tmp")) for name in archive.namelist()))


if __name__ == "__main__":
    unittest.main()
