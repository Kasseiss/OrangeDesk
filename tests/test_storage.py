import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.storage import state_root, legacy_roots, migrate_preferences


def writer(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / '.OrangeDesk'
        self.sources = [self.home/'local/OrangeDesk', self.home/'local/Packages/PackageA/LocalCache/Local/OrangeDesk']
        self.secure = lambda path: path.mkdir(parents=True, exist_ok=True)

    def source(self, index, host, mode, revision=9):
        source = self.sources[index]
        source.mkdir(parents=True, exist_ok=True)
        writer(source/'whitelist.json', {'version':1,'revision':revision,'rules':[{'host':host,'mode':mode,'subdomains':True}]})
        return source

    def test_state_location_is_independent_of_appdata_redirection(self):
        with patch.dict(os.environ, {'USERPROFILE':str(self.home),'ORANGEDESK_DATA_HOME':''}):
            with patch.dict(os.environ, {'LOCALAPPDATA':str(self.home/'physical')}):
                first = state_root()
            with patch.dict(os.environ, {'LOCALAPPDATA':str(self.home/'virtual')}):
                self.assertEqual(state_root(), first)
            self.assertEqual(first, self.root)

    def test_explicit_data_home_keeps_packaged_checks_isolated(self):
        self.source(0, 'private.example', 'direct')
        with patch.dict(os.environ, {'ORANGEDESK_DATA_HOME':str(self.home/'isolated')}):
            self.assertEqual(state_root(), self.home/'isolated')
            migrate_preferences(state_root(), writer, self.secure)
        self.assertFalse((self.home/'isolated').exists())

    def test_legacy_discovery_includes_only_our_folder_in_package_cache(self):
        self.source(0, 'one.example', 'direct')
        self.source(1, 'two.example', 'vpn')
        result=legacy_roots(self.home/'local')
        self.assertEqual(result, self.sources)

    def test_migration_merges_whitelists_and_does_not_copy_control_tokens(self):
        self.source(0, 'one.example', 'direct')
        self.source(1, 'two.example', 'vpn')
        writer(self.sources[0]/'service.json', {'token':'private-bearer-token'})
        migrate_preferences(self.root, writer, self.secure, self.sources)
        saved=json.loads((self.root/'whitelist.json').read_text())
        self.assertEqual({r['host'] for r in saved['rules']}, {'one.example','two.example'})
        self.assertFalse((self.root/'service.json').exists())

    def test_newer_legacy_rule_wins_and_current_user_rules_are_not_overwritten(self):
        self.source(0, 'same.example', 'direct')
        self.source(1, 'same.example', 'vpn')
        os.utime(self.sources[0]/'whitelist.json', (100,100))
        os.utime(self.sources[1]/'whitelist.json', (200,200))
        migrate_preferences(self.root, writer, self.secure, self.sources)
        saved=(self.root/'whitelist.json').read_bytes()
        self.assertEqual(json.loads(saved)['rules'][0]['mode'], 'vpn')
        self.source(0, 'changed.example', 'direct')
        migrate_preferences(self.root, writer, self.secure, self.sources)
        self.assertEqual((self.root/'whitelist.json').read_bytes(), saved)

    def test_invalid_settings_are_skipped_and_valid_appearance_is_retained(self):
        self.source(0, 'one.example', 'direct')
        writer(self.sources[0]/'appearance.json', {'theme':'dark','background':'anime-rain-city','ignored':'not copied'})
        self.sources[1].mkdir(parents=True)
        (self.sources[1]/'whitelist.json').write_text('broken')
        writer(self.sources[1]/'appearance.json', {'theme':'unknown','background':'../../outside'})
        migrate_preferences(self.root, writer, self.secure, self.sources)
        self.assertEqual(json.loads((self.root/'appearance.json').read_text()), {'theme':'dark','background':'anime-rain-city'})
