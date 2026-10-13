from __future__ import annotations
import sys
import traceback
from pathlib import Path


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--vendor-guard":
        if len(args) != 2:
            return 2
        from .vendor_guard import run_guard
        return run_guard(args[1])
    if "--daemon" in args:
        from .engine import serve, service_root
        try:
            return serve()
        except Exception:
            root = service_root()
            root.mkdir(parents=True, exist_ok=True)
            with (root / "service-error.log").open("a", encoding="utf-8") as out:
                traceback.print_exc(file=out)
            return 1
    if args and args[0] in ("--install-path", "--remove-path"):
        from .install_support import update_user_path
        update_user_path(Path(sys.executable).resolve().parent, add=args[0] == "--install-path")
        return 0
    if "--check-runtime" in args:
        import json
        index = args.index("--check-runtime")
        if index + 1 >= len(args):
            return 2
        target = Path(args[index + 1])
        try:
            from PySide6 import QtCore, QtGui, QtWidgets
            value = {"ok": True, "qt": QtCore.qVersion()}
            result = 0
        except Exception:
            value = {"ok": False, "error": traceback.format_exc()}
            result = 1
        target.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    from .ui import run_gui
    screenshot = None
    if "--screenshot" in args:
        index = args.index("--screenshot")
        if index + 1 >= len(args):
            return 2
        screenshot = args[index + 1]
    return run_gui(argv=args, screenshot_path=screenshot, preview="--preview-ui" in args)


if __name__ == "__main__":
    raise SystemExit(main())
