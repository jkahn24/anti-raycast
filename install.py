"""
Flint installer — one-shot setup.
Creates the venv, installs the 4 pinned deps, and writes Flint.lnk into the
Startup folder (ScreenshotTool.lnk pattern: pythonw.exe + launcher.py).
Idempotent — safe to re-run.

Run:  py -3.12 install.py        (or any Python 3.11+)
"""

import os
import subprocess
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).parent.resolve()
VENV     = TOOL_DIR / "venv"
PYTHONW  = VENV / "Scripts" / "pythonw.exe"
PYTHON   = VENV / "Scripts" / "python.exe"
DEPS     = ["PyQt6", "rapidfuzz", "pywin32", "PyYAML", "emoji"]

STARTUP = (Path(os.environ["APPDATA"])
           / "Microsoft/Windows/Start Menu/Programs/Startup")
LNK = STARTUP / "Flint.lnk"


def main():
    if not VENV.exists():
        print(f"Creating venv at {VENV} ...")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV)])

    print(f"Installing deps: {', '.join(DEPS)}")
    subprocess.check_call([str(PYTHON), "-m", "pip", "install", "--quiet",
                           "--upgrade"] + DEPS)

    (TOOL_DIR / "cache").mkdir(exist_ok=True)
    (TOOL_DIR / "plugins").mkdir(exist_ok=True)

    # Startup shortcut via WScript.Shell (no pywin32 needed in THIS python).
    # Stub runs on SYSTEM pythonw, NOT the venv's: venv\Scripts\pythonw.exe is
    # a shim that spawns the base interpreter as a second resident process
    # (~+14 MB for nothing). The stub is stdlib-only, so system python is safe.
    sys_pythonw = Path(sys.executable).parent / "pythonw.exe"
    if not sys_pythonw.exists():
        sys_pythonw = Path(sys.executable)
    ps = f'''
    $s = (New-Object -ComObject WScript.Shell).CreateShortcut("{LNK}")
    $s.TargetPath = "{sys_pythonw}"
    $s.Arguments = '"{TOOL_DIR / "launcher.py"}"'
    $s.WorkingDirectory = "{TOOL_DIR}"
    $s.Description = "Flint launcher stub (Ctrl+Alt+Space)"
    $s.Save()
    '''
    subprocess.check_call(["powershell", "-NoProfile", "-Command", ps])
    print(f"Startup shortcut written: {LNK}")
    print("\nDone. Start now with:")
    print(f'  & "{sys_pythonw}" "{TOOL_DIR / "launcher.py"}"')
    print("Then press Ctrl+Alt+Space.")


if __name__ == "__main__":
    main()
