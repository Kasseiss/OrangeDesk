import sys
from pathlib import Path
if len(sys.argv) > 1 and sys.argv[1] in ("--install-path", "--remove-path"):
    from app.install_support import update_user_path
    update_user_path(Path(sys.executable).resolve().parent, add=sys.argv[1] == "--install-path")
    raise SystemExit(0)
from app.cli import cli_main
raise SystemExit(cli_main(sys.argv[1:]))
