"""Qt-free whitelist tests; all writes stay inside temporary directories."""
import concurrent.futures
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from app.whitelist import MAX_FILE_BYTES, MAX_REVISION, MAX_RULES, WhitelistError, WhitelistStore, normalize_target


class NormalizeTests(unittest.TestCase):
    def test_browser_targets_keep_only_canonical_host(self):
        cases = {
            " HTTPS://User:Private@WWW.Example.COM.:443/path?q=secret#fragment ": "www.example.com",
            "www.Example.com/path?token=private": "www.example.com",
            "例子.测试": "xn--fsqu00a.xn--0zwm56d",
            "https://例子.测试:8443/a": "xn--fsqu00a.xn--0zwm56d",
            "Example.COM:443": "example.com",
            "192.0.2.8:8080": "192.0.2.8",
            "http://192.0.2.8:1/private": "192.0.2.8",
            "example.com.": "example.com",
            "localhost": "localhost",
            "<https://example.com/path>": "example.com",
        }
        for target, canonical in cases.items():
            with self.subTest(target=target):
                self.assertEqual(normalize_target(target), canonical)

    def test_invalid_schemes_paths_ports_and_hosts(self):
        targets = [
            "ftp://example.com", "file:///C:/private", "mailto:user@example.com",
            "https:example.com", "//example.com", "/etc/hosts", "C:/Users/private",
            r"C:\Users\private", "relative/path", "https://", "http://:443",
            "https://example.com:", "example.com:0", "example.com:65536", "example.com:nope",
            "https://[::1]/", "::1", "192.0.2.1/24", "256.1.1.1", "192.000.2.1",
            "*.example.com", "foo_bar.example.com", "-x.example.com", "x-.example.com",
            "example..com", "example.com..", "a" * 64 + ".com", "xn--broken-.com",
            "example.com\x00", "example.com path", "https://example.com\n/private",
        ]
        for target in targets:
            with self.subTest(target=target):
                with self.assertRaises(WhitelistError):
                    normalize_target(target)

    def test_errors_do_not_echo_private_userinfo_or_paths(self):
        with self.assertRaises(WhitelistError) as raised:
            normalize_target("https://user:verysecret@example.com:99999/private?token=sensitive")
        self.assertNotIn("verysecret", str(raised.exception))
        self.assertNotIn("sensitive", str(raised.exception))
        self.assertNotIn("private", str(raised.exception))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orange-whitelist-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "private"
        self.events = []
        self.writes = []

        def secure(root):
            self.events.append(("secure", root))
            root.mkdir(parents=True, exist_ok=True)

        def writer(path, value):
            self.events.append(("write", path))
            self.writes.append(copy.deepcopy(value))
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            temporary.replace(path)

        self.store = WhitelistStore(self.root, writer, secure)

    def write_document(self, value):
        self.root.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def rule(host="example.com", mode="direct", subdomains=True):
        return {"host": host, "mode": mode, "subdomains": subdomains}

    @staticmethod
    def document(rules=None, revision=0):
        return {"version": 1, "revision": revision, "rules": [] if rules is None else rules}

    def test_missing_snapshot_is_read_only(self):
        self.assertEqual(self.store.snapshot(), self.document())
        self.assertFalse(self.root.exists())
        self.assertEqual(self.events, [])

    def test_batch_links_credentials_ipv4_and_exact_scope(self):
        result = self.store.add_many(
            "[Example website](https://user:secret@WWW.Example.COM/private?token=hidden)\n"
            "例子.测试，192.0.2.8:8080; other.example.com/path?a=1, third.example.com",
            "vpn",
        )
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["rules"], [
            self.rule("192.0.2.8", "vpn", False), self.rule("other.example.com", "vpn"),
            self.rule("third.example.com", "vpn"), self.rule("www.example.com", "vpn"),
            self.rule("xn--fsqu00a.xn--0zwm56d", "vpn"),
        ])
        saved = self.store.path.read_text(encoding="utf-8")
        for private in ("secret", "hidden", "private", "token", "8080"):
            self.assertNotIn(private, saved)
        self.assertNotIn(self.rule("example.com", "vpn"), result["rules"])
        self.assertEqual([event[0] for event in self.events], ["secure", "write"])
        self.assertEqual(self.store.snapshot(), result)

    def test_entire_batch_validates_before_mutation(self):
        initial = self.store.add_many("initial.example.com", "direct")
        original = self.store.path.read_bytes()
        with self.assertRaisesRegex(WhitelistError, "第 2 项"):
            self.store.add_many("valid.example.com, bad_domain.com, later.example.com", "vpn")
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertEqual(self.store.snapshot(), initial)
        self.assertEqual(len(self.writes), 1)

    def test_url_punctuation_cannot_become_rules_or_saved_secrets(self):
        result = self.store.add_many(
            "https://example.com/path?values=a,b;verysecret\n"
            "[Markdown link](https://markdown.example.com/a,b;path?token=private,credential) "
            "bare.example.com/path?secret=hidden;anothersecret "
            "fragment.example.com/#credential,secret;value "
            "https://user,password;credential@userinfo.example.com/path "
            "other.example.com/path?a=1, third.example.com "
            "plain.example.com,second.example.com;中文.测试；last.example.com",
            "direct",
        )
        self.assertEqual([rule["host"] for rule in result["rules"]], [
            "bare.example.com", "example.com", "fragment.example.com", "last.example.com",
            "markdown.example.com", "other.example.com", "plain.example.com", "second.example.com",
            "third.example.com", "userinfo.example.com", "xn--fiq228c.xn--0zwm56d",
        ])
        self.assertEqual(result["revision"], 1)
        saved = self.store.path.read_text(encoding="utf-8")
        for private in ("verysecret", "private", "credential", "hidden", "anothersecret", "password", "values", "token"):
            self.assertNotIn(private, saved)

    def test_url_tokens_are_never_split_at_domain_separators(self):
        for text in ("https://example.com,https://other.example.com", "example.com/path,other.example.com/path"):
            with self.subTest(text=text):
                if text.startswith("https://"):
                    with self.assertRaises(WhitelistError):
                        self.store.add_many(text, "direct")
                    self.assertFalse(self.root.exists())
                else:
                    # A path comma is discarded with the path, never expanded
                    # into a second host.
                    result = self.store.add_many(text, "direct")
                    self.assertEqual(result["rules"], [self.rule()])

    def test_duplicates_upsert_without_broadening_or_extra_revisions(self):
        result = self.store.add_many("www.example.com WWW.EXAMPLE.COM. https://www.example.com/path", "direct", False)
        self.assertEqual(result, self.document([self.rule("www.example.com", subdomains=False)], 1))
        self.assertEqual(self.store.add_many("www.example.com", "direct", False), result)
        self.assertEqual(len(self.writes), 1)
        changed = self.store.add_many("www.example.com", "vpn", False)
        self.assertEqual(changed["revision"], 2)
        self.assertEqual(changed["rules"], [self.rule("www.example.com", "vpn", False)])
        separate = self.store.add_many("www.example.com", "direct", True)
        self.assertEqual(separate["revision"], 3)
        self.assertEqual(len(separate["rules"]), 2)
        self.assertEqual(self.store.remove("WWW.EXAMPLE.COM.", False)["rules"], [self.rule("www.example.com")])
        unchanged = self.store.remove("absent.example.com")
        self.assertEqual(unchanged["revision"], 4)
        self.assertEqual(len(self.writes), 4)

    def test_ipv4_defaults_to_exact_and_removes_with_default(self):
        result = self.store.add_many("192.0.2.1", "direct")
        self.assertEqual(result["rules"], [self.rule("192.0.2.1", subdomains=False)])
        self.assertEqual(self.store.remove("http://192.0.2.1/path"), self.document([], 2))

    def test_snapshot_sorted_and_independent(self):
        self.write_document(self.document([self.rule("z.example.com"), self.rule("a.example.com"), self.rule("a.example.com", subdomains=False)], 5))
        result = self.store.snapshot()
        self.assertEqual([(r["host"], r["subdomains"]) for r in result["rules"]], [
            ("a.example.com", False), ("a.example.com", True), ("z.example.com", True),
        ])
        result["rules"][0]["mode"] = "vpn"
        self.assertEqual(self.store.snapshot()["rules"][0]["mode"], "direct")
        self.assertEqual(self.events, [])

    def test_invalid_schema_is_rejected_without_repairing_or_overwriting(self):
        invalid = [
            [], {}, self.document(revision=True), self.document(revision=-1), self.document(revision=1.0), self.document(revision=MAX_REVISION + 1),
            {"version": True, "revision": 0, "rules": []}, {"version": 2, "revision": 0, "rules": []},
            dict(self.document(), private="metadata"), self.document(rules={}),
            self.document([self.rule("EXAMPLE.COM")]), self.document([self.rule("example.com.")]),
            self.document([self.rule("例子.测试")]), self.document([self.rule("http://example.com/private")]),
            self.document([self.rule(mode="proxy")]), self.document([self.rule(mode=[])]),
            self.document([self.rule(subdomains=1)]), self.document([self.rule(host=True)]),
            self.document([self.rule("192.0.2.1")]), self.document([dict(self.rule(), port=443)]),
            self.document([self.rule(), self.rule(mode="vpn")]),
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.write_document(value)
                before = self.store.path.read_bytes()
                with self.assertRaises(WhitelistError):
                    self.store.snapshot()
                with self.assertRaises(WhitelistError):
                    self.store.add_many("valid.example.com", "direct")
                self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.events, [])

    def test_malformed_json_duplicate_fields_utf8_and_size_are_rejected(self):
        self.root.mkdir()
        for raw in (b"{", b"\xff", b'{"version":1,"version":1,"revision":0,"rules":[]}',
                    b'{"version":1,"revision":NaN,"rules":[]}', b" " * (MAX_FILE_BYTES + 1)):
            with self.subTest(raw_prefix=raw[:60]):
                self.store.path.write_bytes(raw)
                with self.assertRaises(WhitelistError):
                    self.store.snapshot()
        self.assertEqual(self.events, [])

    def test_options_and_empty_input_fail_before_writes(self):
        for text, mode, subdomains in (("example.com", "proxy", True), ("example.com", [], True),
                                        ("example.com", None, True),
                                        ("example.com", "direct", 1), (" , ;\n", "direct", True),
                                        (42, "direct", True), ("\ud800", "direct", True)):
            with self.subTest(text=repr(text), mode=mode, subdomains=subdomains):
                with self.assertRaises(WhitelistError):
                    self.store.add_many(text, mode, subdomains)
        with self.assertRaises(WhitelistError):
            self.store.remove("example.com", 1)
        self.assertFalse(self.root.exists())

    def test_revision_limit_allows_unchanged_reads_but_prevents_changed_write(self):
        value = self.document([self.rule()], MAX_REVISION)
        self.write_document(value)
        original = self.store.path.read_bytes()
        self.assertEqual(self.store.add_many("example.com", "direct"), value)
        self.assertEqual(self.store.remove("absent.example.com"), value)
        for mutate in (lambda: self.store.add_many("example.com", "vpn"), lambda: self.store.remove("example.com")):
            with self.assertRaisesRegex(WhitelistError, "上限"):
                mutate()
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertEqual(self.events, [])
        self.assertFalse((self.root / "whitelist.lock").exists())

    def test_rule_and_batch_limits_have_no_partial_writes(self):
        existing = [self.rule(f"host{index}.example.com") for index in range(MAX_RULES)]
        self.write_document(self.document(existing, 4))
        before = self.store.path.read_bytes()
        with self.assertRaises(WhitelistError):
            self.store.add_many("one-more.example.com", "vpn")
        with self.assertRaises(WhitelistError):
            self.store.add_many(" ".join(["example.com"] * (MAX_RULES + 1)), "vpn")
        with self.assertRaises(WhitelistError):
            self.store.add_many("a" * (MAX_FILE_BYTES + 1), "vpn")
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.events, [])

    def test_serialized_size_limit_is_checked_before_secure_or_writer(self):
        label = "a" * 63
        long_suffix = f"{label}.{label}.{label}.{'b' * 35}"
        existing = [self.rule(f"host{index}.{long_suffix}") for index in range(800)]
        self.write_document(self.document(existing, 4))
        before = self.store.path.read_bytes()
        # The existing file fits, while 1000 legal long hosts exceed 256 KiB.
        self.assertLessEqual(len(before), MAX_FILE_BYTES)
        with self.assertRaises(WhitelistError):
            self.store.add_many(" ".join(f"new{index}.{long_suffix}" for index in range(200)), "vpn")
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.events, [])

    def test_secure_failure_prevents_writer_and_atomic_writer_failure_keeps_old_file(self):
        initial = self.store.add_many("example.com", "direct")
        original = self.store.path.read_bytes()

        def failing_secure(root):
            raise PermissionError("not allowed")

        blocked = WhitelistStore(self.root, lambda *_: self.fail("writer ran before secure"), failing_secure)
        with self.assertRaises(WhitelistError):
            blocked.add_many("another.example.com", "vpn")

        def failing_writer(path, value):
            raise OSError("simulated atomic replacement failure")

        failed = WhitelistStore(self.root, failing_writer, lambda _: None)
        with self.assertRaises(WhitelistError):
            failed.add_many("another.example.com", "vpn")
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertEqual(self.store.snapshot(), initial)

    def test_concurrent_mutations_keep_every_rule_and_unique_revision(self):
        writer = self.store._writer
        active = 0
        max_active = 0
        activity_lock = threading.Lock()

        def slow_writer(path, value):
            nonlocal active, max_active
            with activity_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.002)
            writer(path, value)
            with activity_lock:
                active -= 1

        self.store._writer = slow_writer
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda index: self.store.add_many(f"host{index}.example.com", "direct"), range(32)))
        snapshot = self.store.snapshot()
        self.assertEqual(len(snapshot["rules"]), 32)
        self.assertEqual(snapshot["revision"], 32)
        self.assertEqual(sorted(result["revision"] for result in results), list(range(1, 33)))
        self.assertEqual(max_active, 1)
        self.assertEqual(len(self.writes), 32)

    def test_concurrent_processes_merge_from_the_latest_file_revision(self):
        # Both processes finish their initial read before either can lock/write.
        # Without a second read under the configuration lock, one rule is lost.
        barrier = Path(self.temp.name) / "barrier"
        barrier.mkdir()
        script = r'''
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from app.whitelist import WhitelistStore

root, barrier, number = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
def secure(path):
    path.mkdir(parents=True, exist_ok=True)
    (barrier / ("ready-" + number)).write_text("ready", encoding="utf-8")
    deadline = time.monotonic() + 5
    while len(list(barrier.glob("ready-*"))) < 2:
        if time.monotonic() > deadline:
            raise RuntimeError("test barrier timed out")
        time.sleep(0.01)

def writer(path, value):
    time.sleep(0.1)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix="whitelist-test-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":"))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

store = WhitelistStore(root, writer, secure)
result = store.add_many("process" + number + ".example.com", "direct")
print(result["revision"])
'''
        processes = [subprocess.Popen(
            [sys.executable, "-c", script, str(self.root), str(barrier), str(index)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=Path(__file__).resolve().parent.parent,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        ) for index in range(2)]
        try:
            results = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stderr)
                results.append(int(stdout.strip()))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        self.assertEqual(sorted(results), [1, 2])
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["revision"], 2)
        self.assertEqual(snapshot["rules"], [self.rule("process0.example.com"), self.rule("process1.example.com")])
        self.assertTrue((self.root / "whitelist.lock").is_file())

    def test_configuration_lock_times_out_without_changing_rules(self):
        initial = self.store.add_many("initial.example.com", "direct")
        original = self.store.path.read_bytes()
        script = r'''
from pathlib import Path
import sys
import time
from app.whitelist import _configuration_lock
with _configuration_lock(Path(sys.argv[1])):
    print("locked", flush=True)
    time.sleep(5)
'''
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(self.root / "whitelist.lock")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=Path(__file__).resolve().parent.parent,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            started = time.monotonic()
            with self.assertRaisesRegex(WhitelistError, "编辑锁"):
                self.store.add_many("blocked.example.com", "vpn")
            self.assertGreaterEqual(time.monotonic() - started, 1.8)
            self.assertLess(time.monotonic() - started, 4)
            self.assertEqual(self.store.path.read_bytes(), original)
            self.assertEqual(self.store.snapshot(), initial)
            self.assertEqual(len(self.writes), 1)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        result = self.store.add_many("after-release.example.com", "direct")
        self.assertEqual(result["revision"], 2)


if __name__ == "__main__":
    unittest.main()
