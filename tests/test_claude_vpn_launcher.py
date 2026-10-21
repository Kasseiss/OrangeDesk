"""Offline regression checks for the fail-closed Claude/VPN launcher.

Only a random-port loopback HTTP server and a temporary fake Claude are used.
No installed Claude, production proxy, VPN, registry or routing API is touched.
"""
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


LAUNCHER = Path(__file__).resolve().parents[1] / "tools" / "Start-ClaudeWithVpn.ps1"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
TOKEN = "a" * 32
STATUS_PATH = f"/fastorange/{TOKEN}/vpn-status.json"
PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(os.name == "nt" and POWERSHELL.is_file(), "requires Windows PowerShell 5.1")
class ClaudeVpnLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp_parent = Path(tempfile.gettempdir()).resolve()
        self.root = Path(tempfile.mkdtemp(prefix="orange-claude-launcher-test-", dir=self.temp_parent)).resolve()
        self.addCleanup(self.cleanup_temp)
        self.requests = []
        self.proof = {
            "vpnBindingEnforced": True,
            "pacDirectFallback": False,
            "vpnReady": True,
            "claudeRouteIsVpn": True,
            "vpnInterfaceIndex": 17,
            "vpnAddress": "192.168.255.34",
        }
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append((self.client_address[0], self.path))
                body = json.dumps(owner.proof).encode("utf-8")
                self.send_response(200 if self.path == STATUS_PATH else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self.stop_server)
        self.proxy = f"http://127.0.0.1:{self.server.server_port}"
        self.data = self.root / "data"
        self.session = self.data / "network" / "sessions" / TOKEN
        self.session.mkdir(parents=True)
        self.status_file = self.session / "split-status.json"
        self.write_status(StrictVpnBinding=True, PacDirectFallback=False)
        self.write_record(f"{self.proxy}/fastorange/{TOKEN}/proxy.pac")
        self.env = dict(os.environ)
        self.env.update({"HTTP_PROXY": "http://original.invalid:9999", "HTTPS_PROXY": "http://original.invalid:9998",
                         "ALL_PROXY": "socks5://original.invalid:9997", "NO_PROXY": "*", "TEMP": str(self.root), "TMP": str(self.root)})
        self.env.pop("ANTHROPIC_BASE_URL", None)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)

    def cleanup_temp(self):
        # Validate the resolved absolute target before any recursive removal.
        target = self.root.resolve()
        if target.parent != self.temp_parent or not target.name.startswith("orange-claude-launcher-test-"):
            raise RuntimeError(f"refusing unsafe cleanup target: {target}")
        shutil.rmtree(target)

    def write_status(self, **status):
        self.status_file.write_text(json.dumps(status), encoding="utf-8")

    def write_record(self, pac_url):
        (self.session / "proxy-before.json").write_text(json.dumps({"PacUrl": pac_url}), encoding="utf-8")

    def run_launcher(self, *args, check_only=True, script=None):
        command = [str(POWERSHELL), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script or LAUNCHER)]
        if script is None:
            command += ["-DataRoot", str(self.data)]
            if check_only:
                command += ["-CheckOnly"]
        command += list(args)
        return subprocess.run(command, env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, encoding="utf-8", errors="replace", timeout=20)

    def assert_rejected(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('"Ready":true', result.stdout)
        self.assertFalse((self.root / "capture.json").exists(), "fake Claude unexpectedly started")

    def assert_only_local_health_requests(self):
        self.assertTrue(self.requests, "launcher did not request local VPN proof")
        self.assertEqual(set(self.requests), {("127.0.0.1", STATUS_PATH)})

    def test_legacy_status_without_strict_binding_is_rejected(self):
        self.write_status(Running=True)
        self.assert_rejected(self.run_launcher())
        self.assertEqual(self.requests, [])

    def test_no_ready_vpn_is_rejected(self):
        self.proof["vpnReady"] = False
        self.assert_rejected(self.run_launcher())
        self.assert_only_local_health_requests()

    def test_claude_route_outside_vpn_is_rejected(self):
        self.proof["claudeRouteIsVpn"] = False
        self.assert_rejected(self.run_launcher())
        self.assert_only_local_health_requests()

    def test_direct_fallback_in_status_is_rejected_without_request(self):
        self.write_status(StrictVpnBinding=True, PacDirectFallback=True)
        self.assert_rejected(self.run_launcher())
        self.assertEqual(self.requests, [])

    def test_direct_fallback_in_live_proof_is_rejected(self):
        self.proof["pacDirectFallback"] = True
        self.assert_rejected(self.run_launcher())
        self.assert_only_local_health_requests()

    def test_live_proof_without_binding_is_rejected(self):
        self.proof["vpnBindingEnforced"] = False
        self.assert_rejected(self.run_launcher())

    def test_stale_status_is_rejected_without_request(self):
        stale = time.time() - 120
        os.utime(self.status_file, (stale, stale))
        self.assert_rejected(self.run_launcher())
        self.assertEqual(self.requests, [])

    def test_non_session_directory_is_rejected_without_request(self):
        invalid = self.session.with_name("not-a-session")
        self.session.rename(invalid)
        self.assert_rejected(self.run_launcher())
        self.assertEqual(self.requests, [])

    def test_non_loopback_pac_is_rejected_without_request(self):
        self.write_record(f"http://example.invalid:{self.server.server_port}/fastorange/{TOKEN}/proxy.pac")
        self.assert_rejected(self.run_launcher())
        self.assertEqual(self.requests, [])

    def test_pac_token_must_match_session_directory(self):
        self.write_record(f"{self.proxy}/fastorange/{'c' * 32}/proxy.pac")
        self.assert_rejected(self.run_launcher())
        self.assertEqual(self.requests, [])

    def test_invalid_vpn_interface_or_address_is_rejected(self):
        for field, value in (("vpnInterfaceIndex", 0), ("vpnAddress", "not-an-address"), ("vpnAddress", "::1")):
            with self.subTest(field=field, value=value):
                original = self.proof[field]
                self.proof[field] = value
                try:
                    self.assert_rejected(self.run_launcher())
                finally:
                    self.proof[field] = original
        self.assert_only_local_health_requests()

    def test_custom_api_url_is_rejected_before_fake_claude(self):
        fake = self.make_fake_claude()
        self.env["ANTHROPIC_BASE_URL"] = "https://custom-api.invalid"
        result = self.run_launcher("-ClaudePath", str(fake), "-SingaporeNodeConfirmed", check_only=False)
        self.assert_rejected(result)
        self.assert_only_local_health_requests()

    def test_launch_requires_explicit_singapore_confirmation(self):
        fake = self.make_fake_claude()
        result = self.run_launcher("-ClaudePath", str(fake), check_only=False)
        self.assert_rejected(result)
        self.assert_only_local_health_requests()

    def test_valid_check_only_never_invokes_fake_claude(self):
        fake = self.make_fake_claude()
        result = self.run_launcher("-ClaudePath", str(fake))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout.strip())
        self.assertEqual(report, {"Ready": True, "Proxy": self.proxy, "VpnInterfaceIndex": 17,
                                 "VpnAddress": "192.168.255.34", "RegionVerified": False,
                                 "ExternalRequestsSent": False})
        self.assert_only_local_health_requests()
        self.assertFalse((self.root / "capture.json").exists())

    def test_valid_explicit_companion_session_is_used(self):
        result = self.run_launcher("-SessionPath", str(self.session))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["Ready"])
        self.assert_only_local_health_requests()

    def test_selected_unprotected_session_cannot_fall_back_to_another_ready_one(self):
        selected = self.session.with_name("b" * 32)
        selected.mkdir()
        (selected / "split-status.json").write_text(json.dumps({"StrictVpnBinding": False}), encoding="utf-8")
        self.assert_rejected(self.run_launcher("-SessionPath", str(selected)))
        self.assertEqual(self.requests, [])

    def test_explicit_session_outside_root_is_rejected_without_health_request(self):
        outside = self.root / ("b" * 32)
        outside.mkdir()
        self.assert_rejected(self.run_launcher("-SessionPath", str(outside)))
        self.assertEqual(self.requests, [])

    def test_bundled_launcher_matches_the_standalone_source(self):
        bundled = LAUNCHER.parents[1] / "resources" / LAUNCHER.name
        self.assertEqual(hashlib.sha256(LAUNCHER.read_bytes()).digest(), hashlib.sha256(bundled.read_bytes()).digest())

    def make_fake_claude(self):
        capture = self.root / "capture.py"
        capture.write_text(
            "import json, os, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "settings_path = pathlib.Path(args[args.index('--settings') + 1])\n"
            "payload = {'args': args, 'settingsPath': str(settings_path), "
            "'settings': json.loads(settings_path.read_text(encoding='utf-8-sig')), "
            "'env': {k: os.environ.get(k) for k in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY')}}\n"
            "pathlib.Path(__file__).with_name('capture.json').write_text(json.dumps(payload), encoding='utf-8')\n"
            "sys.exit(int(os.environ.get('FAKE_CLAUDE_EXIT', '0')))\n", encoding="utf-8")
        fake = self.root / "fake-claude.cmd"
        fake.write_text(f'@echo off\r\n"{sys.executable}" "{capture}" %*\r\nexit /b %errorlevel%\r\n', encoding="utf-8")
        return fake

    def run_fake_in_wrapper(self, exit_code=0):
        fake = self.make_fake_claude()
        self.env["FAKE_CLAUDE_EXIT"] = str(exit_code)
        wrapper = self.root / "wrapper.ps1"
        restore_record = self.root / "restored.json"
        # Catch the launcher error so its finally cleanup can be observed in the same process.
        wrapper.write_text(
            "$ErrorActionPreference='Stop'\n"
            "$failed=$false\n"
            f"try {{ & {ps_quote(LAUNCHER)} -DataRoot {ps_quote(self.data)} "
            f"-ClaudePath {ps_quote(fake)} -SingaporeNodeConfirmed -ClaudeArguments @('test-argument') }} "
            "catch { $failed=$true }\n"
            "$environment=@{}\n"
            "foreach($name in @('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY')) "
            "{$environment[$name]=[Environment]::GetEnvironmentVariable($name,'Process')}\n"
            f"[IO.File]::WriteAllText({ps_quote(restore_record)}, "
            "(@{failed=$failed;env=$environment}|ConvertTo-Json -Depth 4),[Text.UTF8Encoding]::new($false))\n",
            encoding="utf-8-sig")
        result = self.run_launcher(script=wrapper)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        restored = json.loads(restore_record.read_text(encoding="utf-8"))
        self.assertEqual(restored["failed"], exit_code != 0)
        self.assertEqual(restored["env"], {key: self.env[key] for key in PROXY_NAMES})
        payload = json.loads((self.root / "capture.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["args"].count("--settings"), 1)
        self.assertEqual(payload["args"][-1], "test-argument")
        self.assertEqual(payload["env"], {"HTTP_PROXY": self.proxy, "HTTPS_PROXY": self.proxy,
                                          "ALL_PROXY": None, "NO_PROXY": "localhost,127.0.0.1,::1"})
        self.assertEqual(payload["settings"], {"env": {"HTTP_PROXY": self.proxy, "HTTPS_PROXY": self.proxy,
                                                       "ALL_PROXY": "", "NO_PROXY": "localhost,127.0.0.1,::1"}})
        settings = Path(payload["settingsPath"]).resolve()
        self.assertEqual(settings.parent, self.root)
        self.assertFalse(settings.exists(), "session settings were left on disk")
        self.assertEqual(list(self.root.glob("OrangeClaudeProxy-*.json")), [])
        self.assert_only_local_health_requests()

    def test_fake_claude_gets_protected_environment_and_temporary_settings(self):
        self.run_fake_in_wrapper()

    def test_fake_claude_failure_restores_environment_and_deletes_settings(self):
        self.run_fake_in_wrapper(exit_code=37)

    def test_settings_override_is_rejected_before_health_request(self):
        for option in ("--settings=alternate.json", "--settings"):
            with self.subTest(option=option):
                result = self.run_launcher("-ClaudeArguments", option)
                self.assert_rejected(result)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
