"""
Flint app indexer — builds cache/apps.json from the Start Menu.

Zero background burn by design: rebuilt only on worker spawn (background
thread), on the "> refresh index" action, or when the cache is older than
TTL at summon time. No filesystem watchers, no polling.

Sources:
  1. .lnk walk of both Start Menu Programs dirs (few hundred entries, <100ms)
  2. one `Get-StartApps` PowerShell call for UWP/Store apps (AUMIDs)
"""

import json
import os
import subprocess
import time
from pathlib import Path

TOOL_DIR = Path(__file__).parent
CACHE = TOOL_DIR / "cache" / "apps.json"
TTL_S = 24 * 3600

START_MENUS = [
    Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
    Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
]
# Noise words — shortcuts nobody launches from a palette.
EXCLUDE = ("uninstall", "readme", "documentation", "release notes", "website",
           "safe mode", "repair")

CREATE_NO_WINDOW = 0x08000000


def _walk_lnk() -> list[dict]:
    apps = []
    for root in START_MENUS:
        if not root.exists():
            continue
        for p in root.rglob("*.lnk"):
            name = p.stem
            if any(w in name.lower() for w in EXCLUDE):
                continue
            apps.append({"name": name, "kind": "lnk", "target": str(p)})
    return apps


def _uwp_apps() -> list[dict]:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-StartApps | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=30,
            creationflags=CREATE_NO_WINDOW,
        ).stdout
        data = json.loads(out or "[]")
        if isinstance(data, dict):
            data = [data]
        return [{"name": d["Name"], "kind": "aumid", "target": d["AppID"]}
                for d in data if d.get("Name") and d.get("AppID")]
    except Exception:
        return []


def build_index() -> list[dict]:
    lnk = _walk_lnk()
    seen = {a["name"].lower() for a in lnk}
    apps = lnk + [a for a in _uwp_apps() if a["name"].lower() not in seen]
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps({"built": time.time(), "apps": apps},
                                ensure_ascii=False), encoding="utf-8")
    return apps


def load_index(max_age_s: float = TTL_S) -> tuple[list[dict], bool]:
    """Return (apps, stale). Missing/corrupt cache → ([], True)."""
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
        stale = time.time() - data.get("built", 0) > max_age_s
        return data.get("apps", []), stale
    except Exception:
        return [], True


def launch(app: dict):
    if app["kind"] == "lnk":
        os.startfile(app["target"])
    else:   # AUMID — works for UWP and most modern win32 Start entries
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app['target']}"],
                         close_fds=True)
