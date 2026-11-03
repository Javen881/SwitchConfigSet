"""Windows portable bootstrap; runtime remains app-local, user data is separate."""
import os
from pathlib import Path
import sys
import traceback

base = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'SwitchBatchCLI'
base.mkdir(parents=True, exist_ok=True)
log = open(base / 'startup.log', 'a', encoding='utf-8', buffering=1)
sys.stdout = log
sys.stderr = log
try:
    if '--data-dir' not in sys.argv:
        sys.argv.extend(['--data-dir', str(base)])
    from app import main
    raise SystemExit(main())
except Exception:
    traceback.print_exc()
    raise SystemExit(1)
