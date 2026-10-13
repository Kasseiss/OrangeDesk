"""Add/remove only this installation's user PATH entry; preserve other entries."""
from __future__ import annotations
import ctypes
import os
from pathlib import Path
import winreg

MARKER = r"Software\OrangeDesk\Installer"


def _normal(value: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.expandvars(value.strip().strip('"'))))


def update_user_path(directory: Path, *, add: bool) -> None:
    directory = directory.resolve()
    if add and not (directory / "orange.exe").is_file():
        raise RuntimeError("安装目录缺少命令行程序。")
    entry = str(directory)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
        try:
            value, kind = winreg.QueryValueEx(key, "Path")
            original_exists = True
        except FileNotFoundError:
            value, kind = "", winreg.REG_EXPAND_SZ
            original_exists = False
        if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(value, str):
            raise RuntimeError("用户 PATH 格式异常，未修改。")
        parts = value.split(";") if value else []
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, MARKER, 0, winreg.KEY_READ | winreg.KEY_WRITE) as marker:
            try:
                prior_owned = winreg.QueryValueEx(marker, "PathAdded")[0] == 1
                old_dir = winreg.QueryValueEx(marker, "Directory")[0]
                owned = prior_owned and _normal(old_dir) == _normal(entry)
            except FileNotFoundError:
                prior_owned = False
                owned = False
            if add and prior_owned and _normal(old_dir) != _normal(entry):
                parts = [x for x in parts if _normal(x) != _normal(old_dir)]
                value = ";".join(parts)
            exists = any(_normal(x) == _normal(entry) for x in parts)
            if add:
                if not exists:
                    new_value = value + ("" if not value or value.endswith(";") else ";") + entry
                    winreg.SetValueEx(key, "Path", 0, kind, new_value)
                winreg.SetValueEx(marker, "PathAdded", 0, winreg.REG_DWORD, int(owned or not exists))
                winreg.SetValueEx(marker, "Directory", 0, winreg.REG_SZ, entry)
                if not owned:
                    winreg.SetValueEx(marker, "OriginalPathExists", 0, winreg.REG_DWORD, int(original_exists))
                    winreg.SetValueEx(marker, "TrailingSeparator", 0, winreg.REG_DWORD, int(value.endswith(";")))
            elif owned:
                remaining = [x for x in parts if _normal(x) != _normal(entry)]
                try:
                    existed_before = winreg.QueryValueEx(marker, "OriginalPathExists")[0] == 1
                except FileNotFoundError:
                    existed_before = True
                if not remaining and not existed_before:
                    winreg.DeleteValue(key, "Path")
                else:
                    restored = ";".join(remaining)
                    try:
                        trailing = winreg.QueryValueEx(marker, "TrailingSeparator")[0] == 1
                    except FileNotFoundError:
                        trailing = False
                    if trailing and parts and _normal(parts[-1]) == _normal(entry):
                        restored += ";"
                    winreg.SetValueEx(key, "Path", 0, kind, restored)
                winreg.SetValueEx(marker, "PathAdded", 0, winreg.REG_DWORD, 0)
    if os.name == "nt":
        result = ctypes.c_size_t()
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.SendMessageTimeoutW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_size_t)]
        user.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 2, 2000, ctypes.byref(result))
