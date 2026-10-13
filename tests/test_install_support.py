import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from app import install_support as support

class Key:
    def __init__(self, data): self.data=data
    def __enter__(self): return self
    def __exit__(self,*args): pass

class FakeRegistry:
    HKEY_CURRENT_USER=1; KEY_READ=1; KEY_WRITE=2; REG_SZ=1; REG_EXPAND_SZ=2; REG_DWORD=4
    def __init__(self, initial=None):
        self.values={'Environment': {} if initial is None else {'Path':initial}, support.MARKER:{}}
    def CreateKeyEx(self,root,name,*args): return Key(self.values.setdefault(name,{}))
    def QueryValueEx(self,key,name):
        try: return key.data[name]
        except KeyError: raise FileNotFoundError
    def SetValueEx(self,key,name,reserved,kind,value): key.data[name]=(value,kind)
    def DeleteValue(self,key,name): del key.data[name]

class PathTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.directory=Path(self.temp.name)
        (self.directory/'orange.exe').touch()
        self.addCleanup(self.temp.cleanup)
    def operation(self,registry,add):
        with patch.object(support,'winreg',registry), patch.object(support.ctypes,'WinDLL'):
            support.update_user_path(self.directory,add=add)
    def test_preserves_entries_type_and_new_user_edits(self):
        before=r'%USERPROFILE%\bin;C:\Useful'; registry=FakeRegistry((before,2))
        self.operation(registry,True); self.operation(registry,True)
        path,kind=registry.values['Environment']['Path']
        self.assertEqual(path.count(str(self.directory)),1); self.assertEqual(kind,2)
        registry.values['Environment']['Path']=(path+';C:\\AddedLater',2)
        self.operation(registry,False)
        self.assertEqual(registry.values['Environment']['Path'],(before+';C:\\AddedLater',2))
    def test_does_not_remove_preexisting_entry(self):
        original=(str(self.directory)+';C:\\Other',1); registry=FakeRegistry(original)
        self.operation(registry,True); self.operation(registry,False)
        self.assertEqual(registry.values['Environment']['Path'],original)
    def test_restores_absent_path_value(self):
        registry=FakeRegistry(); self.operation(registry,True); self.operation(registry,True)
        self.operation(registry,False); self.assertNotIn('Path',registry.values['Environment'])
    def test_keeps_trailing_separator(self):
        registry=FakeRegistry(('C:\\Useful;',1)); self.operation(registry,True); self.operation(registry,False)
        self.assertEqual(registry.values['Environment']['Path'],('C:\\Useful;',1))
    def test_missing_cli_leaves_path_unchanged(self):
        registry=FakeRegistry(('C:\\Useful',1)); (self.directory/'orange.exe').unlink()
        with self.assertRaises(RuntimeError): self.operation(registry,True)
        self.assertEqual(registry.values['Environment']['Path'],('C:\\Useful',1))

if __name__=='__main__': unittest.main()
