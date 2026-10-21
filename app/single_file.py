"""One executable: windowed Qt UI and console commands, without importing Qt here."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import sys

CLI_COMMANDS = frozenset({"ui", "start", "stop", "shutdown", "status", "pause", "resume",
                          "open-vpn", "install-vpn", "restore", "logs", "whitelist", "diagnostics", "claude-protection", "wechat-direct", "version"})


def prepare_console() -> None:
    """Preserve redirected pipes; otherwise attach to the invoking terminal."""
    if os.name != "nt":
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetFileType.argtypes = [wintypes.HANDLE]
    kernel.GetFileType.restype = wintypes.DWORD
    kernel.AttachConsole.argtypes = [wintypes.DWORD]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                      ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    output = kernel.GetStdHandle(-11 & 0xFFFFFFFF)
    if not output or output == ctypes.c_void_p(-1).value or not kernel.GetFileType(output):
        kernel.AttachConsole(0xFFFFFFFF)
    import msvcrt
    for name, number in (("stdout", -11), ("stderr", -12)):
        if getattr(sys, name) is not None:
            continue
        handle = kernel.GetStdHandle(number & 0xFFFFFFFF)
        duplicate = wintypes.HANDLE()
        process = kernel.GetCurrentProcess()
        if handle and handle != ctypes.c_void_p(-1).value and kernel.GetFileType(handle) and kernel.DuplicateHandle(process, handle, process, ctypes.byref(duplicate), 0, False, 2):
            try:
                descriptor = msvcrt.open_osfhandle(int(duplicate.value), os.O_WRONLY)
            except OSError:
                kernel.CloseHandle(duplicate)
                raise
            stream = os.fdopen(descriptor, "w", encoding="utf-8", errors="replace", buffering=1)
        else:
            stream = open(os.devnull, "w", encoding="utf-8")
        setattr(sys, name, stream)


def single_main(argv=None) -> int:
    sys.orangedesk_single_file = True
    args = list(sys.argv[1:] if argv is None else argv)
    if args and (args[0] in CLI_COMMANDS or args[0] in {"--help", "-h"}):
        prepare_console()
        from .cli import cli_main
        return cli_main(args)
    from .main import main
    return main(args)
