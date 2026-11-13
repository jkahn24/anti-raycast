"""
Flint providers — the query pipeline (Phase 2: apps, calc, actions, killproc).

Result contract (also the plugin contract, Phase 4):
    {title, subtitle, icon, run: callable|None, confirm: bool,
     set_query: str|None, copy_text: str|None}
The palette executes rows: set_query rewrites the query box, copy_text goes
to the clipboard, confirm inserts a one-keystroke confirm row, run() fires.

Selftest:  venv\\Scripts\\python.exe providers.py --selftest
"""

import ast
import csv
import ctypes
import ctypes.wintypes as _wt
import io
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import indexer

TOOL_DIR = Path(__file__).parent
ACTIONS_YAML = TOOL_DIR / "actions.yaml"

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010

MAX_ROWS = 12          # ranked pool depth; the palette clips at ~9.5 visible


def _res(title, subtitle="", icon="•", run=None, confirm=False,
         set_query=None, copy_text=None, kind=None, key=None, chip=None,
         alt_run=None, alt_copy_text=None, icon_path=None):
    return {"title": title, "subtitle": subtitle, "icon": icon, "run": run,
            "confirm": confirm, "set_query": set_query, "copy_text": copy_text,
            "kind": kind, "key": key, "chip": chip, "alt_run": alt_run,
            "alt_copy_text": alt_copy_text, "icon_path": icon_path}


# ── Learned ranking (usage.json) ──────────────────────────────────────────────
# The research-verified two-layer model (RESEARCH.md): Firefox-style adaptive
# input history (query→choice association, use_count asymptote 10 via ×0.9+1,
# ×0.975/day idle decay, prune <0.1) + PowerToys-style selection-count boost.

class UsageStore:
    FILE = TOOL_DIR / "cache" / "usage.json"
    DECAY = 0.975
    PRUNE = 0.1

    def __init__(self):
        self._d = None
        self._lock = threading.Lock()

    def _load(self):
        if self._d is not None:
            return
        try:
            d = json.loads(self.FILE.read_text(encoding="utf-8"))
        except Exception:
            d = {}
        d.setdefault("selections", {})
        d.setdefault("queries", {})
        d.setdefault("modes", {})
        today = int(time.time() // 86400)
        days = today - d.get("day", today)
        if days > 0:                                  # idle decay
            f = self.DECAY ** days
            for k in list(d["selections"]):
                d["selections"][k] = round(d["selections"][k] * f, 3)
                if d["selections"][k] < self.PRUNE:
                    del d["selections"][k]
            for k in list(d["modes"]):
                d["modes"][k] = round(d["modes"][k] * f, 3)
                if d["modes"][k] < self.PRUNE:
                    del d["modes"][k]
            for q in list(d["queries"]):
                for k in list(d["queries"][q]):
                    d["queries"][q][k] = round(d["queries"][q][k] * f, 3)
                    if d["queries"][q][k] < self.PRUNE:
                        del d["queries"][q][k]
                if not d["queries"][q]:
                    del d["queries"][q]
        d["day"] = today
        self._d = d

    def _save(self):
        try:
            self.FILE.write_text(json.dumps(self._d), encoding="utf-8")
        except Exception:
            pass

    def record(self, key: str, query: str = ""):
        with self._lock:
            self._load()
            sel = self._d["selections"]
            sel[key] = round(sel.get(key, 0) * 0.9 + 1, 3)   # asymptote 10
            q = (query or "").strip().lower()[:40]
            if q and not q.startswith((">", ":", "?")) \
                    and not q.startswith(("kill ", "mem:", "clip")):
                qd = self._d["queries"].setdefault(q, {})
                qd[key] = round(qd.get(key, 0) * 0.9 + 1, 3)
            self._save()

    def record_mode(self, mode_id: str):
        with self._lock:
            self._load()
            md = self._d["modes"]
            md[mode_id] = round(md.get(mode_id, 0) * 0.9 + 1, 3)
            self._save()

    def mode_counts(self) -> dict:
        self._load()
        return dict(self._d["modes"])

    def boost(self, key: str) -> float:
        self._load()
        return self._d["selections"].get(key, 0)

    def picked_for(self, query: str) -> dict:
        self._load()
        return self._d["queries"].get((query or "").strip().lower()[:40], {})

    def top_keys(self, n: int) -> list[str]:
        self._load()
        return [k for k, _ in sorted(self._d["selections"].items(),
                                     key=lambda kv: -kv[1])[:n]]


usage = UsageStore()


# Command modes — surfaced as clickable pills on the empty palette. Clicking
# inserts `insert` into the query box; ordering adapts to usage (decayed).
MODES = [
    {"id": "ask",    "label": "ask claude", "icon": "🧠", "insert": ". ",
     "hint": "your request for Claude…",
     "tip": "Send a request to Claude — opens a new Claude Code session in Cursor with what you type"},
    {"id": "note",   "label": "note",       "icon": "📥", "insert": "note ",
     "hint": "the thought to capture…",
     "tip": "Jot a quick thought — saved with a timestamp to your capture file (capture_file in config.yaml)"},
    {"id": "g",      "label": "google",     "icon": "🔍", "insert": "g ",
     "hint": "search Google…",
     "tip": "Search Google in your browser for whatever you type"},
    {"id": "snip",   "label": "snippets",   "icon": "✂️", "insert": "snip",
     "hint": "filter snippets…",
     "tip": "Text you copy a lot (emails, paths) — Enter copies it to the clipboard"},
    {"id": "clip",   "label": "clipboard",  "icon": "📋", "insert": "clip",
     "hint": "filter clipboard history…",
     "tip": "Your clipboard history — Enter re-copies, Ctrl+Enter pastes into the window you came from"},
    {"id": "recent", "label": "recent",     "icon": "🕘", "insert": "recent",
     "hint": "filter recent files…",
     "tip": "Files you recently opened anywhere in Windows — Enter reopens them"},
    {"id": "emoji",  "label": "emoji",      "icon": "😀", "insert": ":",
     "hint": "emoji name…",
     "tip": "Emoji picker — type a name like fire or rose, Enter copies the emoji"},
    {"id": "vaultq", "label": "in-notes",   "icon": "🔎", "insert": "?",
     "hint": "terms inside your notes…",
     "tip": "Search INSIDE the text of your notes (every vault in config.yaml) — Enter opens the note in Obsidian"},
    {"id": "win",    "label": "windows",    "icon": "🪟", "insert": "w ",
     "hint": "window title…",
     "tip": "Jump to an open window — like Alt-Tab, but you type its name"},
    {"id": "sys",    "label": "system",     "icon": "⚡", "insert": "sys",
     "hint": "battery · RAM · power scheme",
     "tip": "Battery, memory and power scheme — with one-click scheme switches"},
    {"id": "kill",   "label": "kill",       "icon": "❌", "insert": "kill ",
     "hint": "process name…",
     "tip": "Force-close a misbehaving program — always asks you to confirm first"},
    {"id": "act",    "label": "actions",    "icon": "⚙️", "insert": ">",
     "hint": "filter actions…",
     "tip": "Your custom actions (actions.yaml) + built-ins like refresh and restart"},
]


def mode_effective_query(mode: dict, typed: str) -> str:
    """Combine an active mode chip with the typed text into the pipeline
    query. Bare-word modes need a separating space before arguments."""
    ins = mode["insert"]
    if ins.endswith(" ") or len(ins.strip()) == 1:
        return ins + typed
    return ins + (" " + typed if typed else "")


def mode_of_query(q: str) -> str | None:
    """Which mode a query is using (for usage counting). Precise per insert:
    trailing-space inserts and single-char prefixes match startswith; bare
    words must be exact or followed by a space (so 'granola' isn't 'g')."""
    low = q.lower()
    for m in MODES:
        ins = m["insert"]
        if ins.endswith(" ") or len(ins.strip()) == 1:
            if low.startswith(ins) or low == ins.strip():
                return m["id"]
        elif low == ins or low.startswith(ins + " "):
            return m["id"]
    return None


def ordered_modes() -> list[dict]:
    counts = usage.mode_counts()
    return sorted(MODES, key=lambda m: -counts.get(m["id"], 0))


class PinStore:
    """User-pinned items shown as a tile bar under the search box."""
    FILE = TOOL_DIR / "cache" / "pins.json"
    MAX = 9

    def keys(self) -> list[str]:
        try:
            return json.loads(self.FILE.read_text(encoding="utf-8"))[:self.MAX]
        except Exception:
            return []

    def is_pinned(self, key: str) -> bool:
        return key in self.keys()

    def toggle(self, key: str) -> bool:
        """Pin/unpin; returns True if the key is now pinned."""
        keys = self.keys()
        if key in keys:
            keys.remove(key)
            now = False
        elif len(keys) < self.MAX:
            keys.append(key)
            now = True
        else:
            return False        # bar is full — unpin something first
        try:
            self.FILE.write_text(json.dumps(keys), encoding="utf-8")
        except Exception:
            pass
        return now


pins = PinStore()


# ── Calculator ────────────────────────────────────────────────────────────────

class CalcProvider:
    _ALLOWED = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
                ast.Name, ast.Call, ast.Load,
                ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
                ast.Pow, ast.USub, ast.UAdd)
    NAMES = {"pi": math.pi, "e": math.e, "abs": abs, "round": round,
             "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos,
             "tan": math.tan, "log": math.log, "log2": math.log2,
             "log10": math.log10, "min": min, "max": max}

    def try_eval(self, q: str):
        if not any(c.isdigit() for c in q):
            return None
        try:
            tree = ast.parse(q, mode="eval")
            for node in ast.walk(tree):
                if not isinstance(node, self._ALLOWED):
                    return None
                if isinstance(node, ast.Name) and node.id not in self.NAMES:
                    return None
                if isinstance(node, ast.Call) and (
                        not isinstance(node.func, ast.Name)
                        or not callable(self.NAMES.get(node.func.id))):
                    return None
            val = eval(compile(tree, "<calc>", "eval"),
                       {"__builtins__": {}}, dict(self.NAMES))
        except Exception:
            return None
        if isinstance(val, float) and val.is_integer():
            val = int(val)
        if isinstance(val, float):
            val = round(val, 10)
        return _res(f"= {val}", "Calculator", "🟰",
                    copy_text=str(val), chip="Calc")


# ── Apps ──────────────────────────────────────────────────────────────────────

class AppProvider:
    def __init__(self):
        self.apps: list[dict] = []
        self.aliases: dict[str, str] = {}     # alias -> app-name fragment
        self._lock = threading.Lock()

    def init_async(self):
        """Load the cache immediately; rebuild in the background if stale."""
        apps, stale = indexer.load_index()
        with self._lock:
            self.apps = apps
        if stale:
            threading.Thread(target=self.rebuild, daemon=True).start()

    def rebuild(self):
        apps = indexer.build_index()
        with self._lock:
            self.apps = apps

    def candidates(self):
        with self._lock:
            out = [(a["name"], a) for a in self.apps]
            for alias, frag in self.aliases.items():
                hit = next((a for a in self.apps
                            if frag.lower() in a["name"].lower()), None)
                if hit:
                    out.append((alias, hit))
            return out

    @staticmethod
    def _open_install_folder(lnk_path: str):
        """Ctrl+Enter on a Start Menu app: reveal the real exe in Explorer."""
        try:
            import win32com.client
            target = win32com.client.Dispatch(
                "WScript.Shell").CreateShortCut(lnk_path).TargetPath
            if target and os.path.exists(target):
                subprocess.Popen(["explorer", "/select,", target])
                return
        except Exception:
            pass
        os.startfile(os.path.dirname(lnk_path))

    @staticmethod
    def to_result(app: dict):
        is_lnk = app["kind"] == "lnk"
        # UWP apps have no file path, but the shell resolves their tile icon
        # via the AppsFolder parse name.
        icon_path = (app["target"] if is_lnk
                     else f"shell:AppsFolder\\{app['target']}")
        return _res(app["name"], "App (Start Menu)" if is_lnk else "App", "▶",
                    run=lambda a=app: indexer.launch(a),
                    key=f"app:{app['target']}", chip="App",
                    icon_path=icon_path,
                    alt_run=(lambda p=app["target"]:
                             AppProvider._open_install_folder(p))
                    if is_lnk else None)


# ── Custom actions (actions.yaml) ─────────────────────────────────────────────

class ActionProvider:
    def __init__(self):
        self._mtime = 0.0
        self._actions: list[dict] = []
        self.searches: dict[str, str] = {}     # keyword -> URL with {q}

    def _load(self):
        """mtime-checked hot-reload — called on every palette summon."""
        try:
            mtime = ACTIONS_YAML.stat().st_mtime
        except OSError:
            self._actions = []
            return
        if mtime == self._mtime:
            return
        try:
            import yaml
            with open(ACTIONS_YAML, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            self._actions = data.get("actions", [])
            self.searches = {str(k).lower(): str(v)
                             for k, v in (data.get("searches") or {}).items()}
            self._mtime = mtime
        except Exception:
            pass    # keep the last good load

    def refresh(self):
        self._load()

    @staticmethod
    def _run_cmd(a: dict):
        t, cmd = a.get("type"), str(a.get("command", ""))
        if t == "url":
            webbrowser.open(cmd)
        elif t == "obsidian":
            os.startfile(cmd)
        elif t == "shell":
            subprocess.Popen(cmd, shell=True, creationflags=CREATE_NO_WINDOW)
        elif t == "powershell":
            args = (["-File", cmd] if cmd.lower().endswith(".ps1")
                    else ["-Command", cmd])
            # Visible console — actions like job-status exist to be READ.
            subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy",
                              "Bypass", "-NoExit"] + args,
                             creationflags=CREATE_NEW_CONSOLE)
        elif t == "python":
            subprocess.Popen([sys.executable, cmd],
                             creationflags=CREATE_NO_WINDOW)

    def to_result(self, a: dict):
        if a.get("type") == "builtin":
            if a.get("command") == "killproc":
                return _res(a.get("name", "Kill process…"),
                            "type to filter running processes", "❌",
                            set_query="kill ")
            return None
        return _res(a.get("name", "?"), a.get("subtitle", "Action"),
                    a.get("icon", "⚡"),
                    run=lambda act=a: self._run_cmd(act),
                    confirm=bool(a.get("confirm")),
                    key=f"action:{a.get('name', '?')}", chip="Action")

    def candidates(self):
        self._load()
        out = []
        for a in self._actions:
            r = self.to_result(a)
            if r is None:
                continue
            keys = [a.get("name", "")] + [str(k) for k in a.get("keywords", [])]
            for k in keys:
                if k:
                    out.append((k, r))
        return out


# ── Emoji search (":<query>" — Enter copies the emoji) ───────────────────────

class EmojiProvider:
    """Grid emoji picker behind the ':' prefix. Lazy-loaded on first use so
    it costs nothing at spawn (~5k entries, a few MB once loaded).

    UX rules: empty query = recents (persisted) topped up with a starter set;
    skin-tone variants are filtered out; ranking is prefix > substring >
    fuzzy so short queries behave predictably."""

    MAX = 40                                  # 8 cols x 5 rows in the palette
    RECENT_FILE = TOOL_DIR / "cache" / "emoji_recent.json"
    STARTER = ["👍", "❤️", "😂", "🔥", "🚀", "✅", "❌", "🎉", "👀", "🙏",
               "💪", "😅", "🌹", "🔍", "🧠", "📋", "☕", "🤝", "✨", "😊",
               "😭", "🤔", "👋", "💡", "⚡", "🎯", "🐛", "📈", "🍕", "🎮"]

    def __init__(self):
        self._entries: list[tuple[str, str]] | None = None   # (name, char)
        self._by_char: dict[str, str] = {}

    def _load(self):
        if self._entries is not None:
            return
        entries, by_char = [], {}
        try:
            import emoji as emoji_pkg
            for char, data in emoji_pkg.EMOJI_DATA.items():
                name = data.get("en", "").strip(":").replace("_", " ")
                if not name or "skin tone" in name:
                    continue                     # variants just clutter a grid
                entries.append((name.lower(), char))
                by_char.setdefault(char, name.lower())
                for alias in data.get("alias", []):
                    entries.append((alias.strip(":").replace("_", " ").lower(), char))
        except Exception:
            pass
        self._entries = entries
        self._by_char = by_char

    def _recents(self) -> list[str]:
        try:
            import json
            return json.loads(self.RECENT_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []

    def record_use(self, char: str):
        try:
            import json
            recent = [c for c in self._recents() if c != char]
            self.RECENT_FILE.write_text(
                json.dumps([char] + recent[:59], ensure_ascii=False),
                encoding="utf-8")
        except Exception:
            pass

    def _row(self, char: str):
        return _res(self._by_char.get(char, ""), "", char,
                    copy_text=char, kind="emoji")

    def search(self, frag: str):
        self._load()
        if not self._entries:
            return [_res("emoji package missing", "pip install emoji", "❓")]
        frag = frag.strip().lower()

        if not frag:                             # recents first, then starters
            chars, seen = [], set()
            for c in self._recents() + self.STARTER:
                if c not in seen and c in self._by_char:
                    seen.add(c)
                    chars.append(c)
                if len(chars) >= self.MAX:
                    break
            return [self._row(c) for c in chars]

        starts, contains = [], []
        for name, char in self._entries:
            if name.startswith(frag):
                starts.append((len(name), name, char))
            elif frag in name:
                contains.append((len(name), name, char))
        out, seen = [], set()
        for _, _, char in sorted(starts) + sorted(contains):
            if char not in seen:
                seen.add(char)
                out.append(self._row(char))
            if len(out) >= self.MAX:
                return out
        if len(out) < self.MAX:                  # fuzzy fill for typos
            from rapidfuzz import fuzz, process
            names = [n for n, _ in self._entries]
            for name, _s, idx in process.extract(frag, names, scorer=fuzz.WRatio,
                                                 score_cutoff=60,
                                                 limit=self.MAX * 2):
                char = self._entries[idx][1]
                if char not in seen:
                    seen.add(char)
                    out.append(self._row(char))
                if len(out) >= self.MAX:
                    break
        return out


# ── Unit conversion ("72 f to c", "5 km to mi") ──────────────────────────────

class UnitProvider:
    _TABLES = [  # unit -> factor to the dimension's base
        {"m": 1, "km": 1000, "cm": .01, "mm": .001, "mi": 1609.344,
         "yd": .9144, "ft": .3048, "in": .0254},
        {"kg": 1, "g": .001, "mg": 1e-6, "t": 1000,
         "lb": .45359237, "lbs": .45359237, "oz": .028349523125},
        {"l": 1, "ml": .001, "gal": 3.785411784, "cup": .2365882365},
        {"m/s": 1, "ms": 1, "kmh": 1 / 3.6, "km/h": 1 / 3.6,
         "mph": .44704, "kn": .514444},
        {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12,
         "kib": 2 ** 10, "mib": 2 ** 20, "gib": 2 ** 30, "tib": 2 ** 40},
    ]
    _TEMP = {"c", "f", "k", "°c", "°f"}
    _RE = re.compile(r"^(-?\d+(?:[.,]\d+)?)\s*([a-zA-Z°/]+)\s+(?:to|in|->)\s+"
                     r"([a-zA-Z°/]+)$")

    def try_convert(self, q: str):
        m = self._RE.match(q.strip())
        if not m:
            return None
        val = float(m.group(1).replace(",", "."))
        a, b = m.group(2).lower(), m.group(3).lower()
        if a in self._TEMP and b in self._TEMP:
            a, b = a.lstrip("°"), b.lstrip("°")
            celsius = {"c": val, "f": (val - 32) / 1.8, "k": val - 273.15}[a]
            out = {"c": celsius, "f": celsius * 1.8 + 32, "k": celsius + 273.15}[b]
        else:
            table = next((t for t in self._TABLES if a in t and b in t), None)
            if table is None:
                return None
            out = val * table[a] / table[b]
        out = round(out, 6)
        if float(out).is_integer():
            out = int(out)
        return _res(f"= {out} {b}", f"{m.group(1)} {a} → {b}", "🟰",
                    copy_text=str(out), chip="Calc")


# ── Window switcher ("w <fragment>" — Alt-Tab with fuzzy search) ─────────────

_user32 = ctypes.WinDLL("user32", use_last_error=True)


class WindowProvider:
    _SKIP_TITLES = {"Program Manager", "Windows Input Experience", "Settings"}

    def _enum(self) -> list[tuple[int, str]]:
        wins = []

        @ctypes.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
        def cb(hwnd, _l):
            if _user32.IsWindowVisible(hwnd):
                n = _user32.GetWindowTextLengthW(hwnd)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    _user32.GetWindowTextW(hwnd, buf, n + 1)
                    if buf.value and buf.value not in self._SKIP_TITLES:
                        wins.append((hwnd, buf.value))
            return True

        _user32.EnumWindows(cb, 0)
        return wins

    @staticmethod
    def _focus(hwnd: int):
        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        _user32.SetForegroundWindow(hwnd)

    def search(self, frag: str):
        frag = frag.strip().lower()
        rows = []
        for hwnd, title in self._enum():
            low = title.lower()
            if frag and frag not in low:
                continue
            rows.append((0 if low.startswith(frag) else 1, len(title),
                         _res(title, "switch to window", "🪟", chip="Window",
                              run=lambda h=hwnd: WindowProvider._focus(h))))
        return [r for _, _, r in sorted(rows, key=lambda t: (t[0], t[1]))][:MAX_ROWS] \
            or [_res("No matching windows", frag or "w <fragment>", "🪟", chip="Window")]


# ── Vault notes (filenames in the default pool; "?" = content search) ────────

def _find_rg() -> str:
    """rg isn't on PATH on this machine — fall back to Cursor's bundled one."""
    import shutil
    hit = shutil.which("rg")
    if hit:
        return hit
    bundled = (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/cursor/_"
               / "resources/app/node_modules/@vscode/ripgrep/bin/rg.exe")
    return str(bundled) if bundled.exists() else "rg"


RG = _find_rg()


def _find_obsidian_exe() -> str | None:
    """Note rows show Obsidian's own icon — that's the app they open in."""
    for p in (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Obsidian/Obsidian.exe",
              Path(os.environ.get("LOCALAPPDATA", "")) / "Obsidian/Obsidian.exe"):
        if p.exists():
            return str(p)
    return None


OBSIDIAN_EXE = _find_obsidian_exe()


class VaultProvider:
    EXCLUDE_DIRS = {".obsidian", ".trash", ".git", "node_modules", "venv",
                    ".venv", "__pycache__", "claude-config-backup", "_temp"}

    def __init__(self):
        self.notes: list[dict] = []
        self.vaults: dict[str, str] = {}
        self._lock = threading.Lock()

    def init_async(self, vaults: dict):
        self.vaults = {k: os.path.expanduser(str(v)) for k, v in (vaults or {}).items()}
        threading.Thread(target=self.rebuild, daemon=True).start()

    def rebuild(self):
        notes = []
        for vault, root in self.vaults.items():
            root_p = Path(root)
            if not root_p.exists():
                continue
            for dirpath, dirnames, filenames in os.walk(root_p):
                dirnames[:] = [d for d in dirnames if d not in self.EXCLUDE_DIRS]
                for fn in filenames:
                    if fn.endswith(".md"):
                        rel = os.path.relpath(os.path.join(dirpath, fn), root_p)
                        notes.append({"vault": vault, "rel": rel.replace("\\", "/"),
                                      "stem": fn[:-3]})
        with self._lock:
            self.notes = notes

    def candidates(self):
        with self._lock:
            return [(n["stem"], n) for n in self.notes]

    @staticmethod
    def to_result(note: dict):
        from urllib.parse import quote
        rel_dir = os.path.dirname(note["rel"])
        uri = (f"obsidian://open?vault={quote(note['vault'])}"
               f"&file={quote(note['rel'][:-3])}")
        return _res(note["stem"],
                    f"{note['vault']}{' · ' + rel_dir if rel_dir else ''}",
                    "📝", run=lambda u=uri: os.startfile(u),
                    key=f"note:{note['vault']}/{note['rel']}", chip="Note",
                    alt_copy_text=f"[[{note['stem']}]]",
                    icon_path=OBSIDIAN_EXE)

    _RG_LINE = re.compile(r"^(.*?\.md):(\d+):(.*)$")

    def content_search(self, q: str):
        """'?<terms>' — rg across both vaults with the first matching line as
        the subtitle (so you can see WHY it matched), 2s hard timeout."""
        q = q.strip()
        if not q:
            return [_res("Type terms to search note contents…", "? <terms>", "🔎")]
        roots = [r for r in self.vaults.values() if Path(r).exists()]
        if not roots:
            return []
        try:
            # utf-8 explicitly — vault filenames carry emoji (People/ notes),
            # and text=True would decode as cp1252 and blow up the reader.
            out = subprocess.run(
                [RG, "-in", "--max-count", "1", "--no-heading",
                 "--max-filesize", "2M", "-g", "*.md", q] + roots,
                capture_output=True, timeout=2, encoding="utf-8",
                errors="replace", creationflags=CREATE_NO_WINDOW).stdout or ""
        except FileNotFoundError:
            return [_res("ripgrep (rg) not found", "content search unavailable", "⚠️")]
        except Exception:
            return []
        rows = []
        for line in out.splitlines():
            m = self._RG_LINE.match(line)
            if not m:
                continue
            p, snippet = Path(m.group(1)), m.group(3).strip()
            for vault, root in self.vaults.items():
                try:
                    rel = p.relative_to(root).as_posix()
                except ValueError:
                    continue
                r = self.to_result({"vault": vault, "rel": rel, "stem": p.stem})
                r["subtitle"] = f"{vault} · {snippet[:80]}" if snippet else r["subtitle"]
                rows.append(r)
                break
            if len(rows) >= MAX_ROWS:
                break
        return rows


# ── File search (Windows Search index; scoped-crawl fallback) ────────────────

class FileProvider:
    """Queried ASYNC by the palette (150ms debounce) and merged into results.
    Primary: the OS's own SystemIndex — zero indexing cost to us. Fallback:
    a lazily-built cached listing of the scoped dirs from config.yaml."""

    FALLBACK_EXCLUDE = VaultProvider.EXCLUDE_DIRS | {"AppData", "cache"}
    FALLBACK_CAP = 60_000

    def __init__(self):
        self.scoped: list[str] = []
        self._fallback: list[tuple[str, str]] | None = None   # (name, path)
        self._fb_building = False
        self._lock = threading.Lock()

    @staticmethod
    def _row(path: str):
        name = os.path.basename(path) or path
        icon = "📁" if os.path.isdir(path) else "📄"
        return _res(name, os.path.dirname(path), icon,
                    run=lambda p=path: os.startfile(p),
                    key=f"file:{path}", chip="File", icon_path=path,
                    alt_run=lambda p=path: subprocess.Popen(
                        ["explorer", "/select,", p]))

    def search(self, q: str, limit: int = 8) -> list[dict]:
        q = q.strip()
        if len(q) < 3:                      # too short = noise + index churn
            return []
        rows = self._systemindex(q, limit)
        if rows is None:
            rows = self._fallback_search(q, limit)
        return rows

    def _systemindex(self, q: str, limit: int):
        """None = engine unavailable (triggers fallback); [] = no matches."""
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except Exception:
            return None
        try:
            import adodbapi
            safe = q.replace('"', "").replace("'", "")
            conn = adodbapi.connect(
                "Provider=Search.CollatorDSO;"
                "Extended Properties='Application=Windows';")
            try:
                cur = conn.cursor()
                cur.execute(
                    f"SELECT TOP {int(limit)} System.ItemPathDisplay "
                    f"FROM SystemIndex "
                    f"WHERE SCOPE='file:{Path.home().as_posix()}' "
                    f"AND CONTAINS(System.FileName, '\"{safe}*\"')")
                return [self._row(r[0]) for r in cur.fetchall() if r[0]]
            finally:
                conn.close()
        except Exception:
            return None
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

    def _fallback_search(self, q: str, limit: int):
        with self._lock:
            fb, building = self._fallback, self._fb_building
            if fb is None and not building:
                self._fb_building = True
                threading.Thread(target=self._build_fallback,
                                 daemon=True).start()
        if not fb:
            return []
        from rapidfuzz import fuzz, process
        names = [n for n, _ in fb]
        hits = process.extract(q, names, scorer=fuzz.WRatio,
                               score_cutoff=70, limit=limit)
        return [self._row(fb[idx][1]) for _, _, idx in hits]

    def _build_fallback(self):
        entries = []
        for root in self.scoped:
            if not Path(root).exists():
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames
                               if d not in self.FALLBACK_EXCLUDE
                               and not d.startswith(".")]
                for fn in filenames:
                    entries.append((fn, os.path.join(dirpath, fn)))
                if len(entries) > self.FALLBACK_CAP:
                    break
            if len(entries) > self.FALLBACK_CAP:
                break
        with self._lock:
            self._fallback = entries
            self._fb_building = False


# ── Clipboard history ("clip" — captured while the worker is warm) ───────────

class ClipProvider:
    """Text-only clipboard history, event-driven (WM_CLIPBOARDUPDATE on the
    palette HWND — zero polling). Honest caveat, documented in README: history
    only accumulates while the worker is alive, i.e. from the first summon of
    the session; Win+V covers the gap before that."""

    DB = TOOL_DIR / "cache" / "clipboard.db"
    MAX_ITEMS = 200
    MAX_LEN = 100_000

    def __init__(self):
        self._conn = None
        self._lock = threading.Lock()

    def _db(self):
        import sqlite3
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.DB), check_same_thread=False)
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS clips (text TEXT PRIMARY KEY, ts REAL)")
        return self._conn

    def record(self, text: str):
        if not text or not text.strip() or len(text) > self.MAX_LEN:
            return
        try:
            with self._lock:
                db = self._db()
                db.execute("INSERT OR REPLACE INTO clips VALUES (?, ?)",
                           (text, time.time()))
                db.execute(
                    "DELETE FROM clips WHERE text NOT IN "
                    "(SELECT text FROM clips ORDER BY ts DESC LIMIT ?)",
                    (self.MAX_ITEMS,))
                db.commit()
        except Exception:
            pass

    @staticmethod
    def _age(ts: float) -> str:
        d = max(0, time.time() - ts)
        if d < 60:
            return "just now"
        if d < 3600:
            return f"{int(d // 60)}m ago"
        if d < 86400:
            return f"{int(d // 3600)}h ago"
        return f"{int(d // 86400)}d ago"

    def search(self, frag: str):
        frag = frag.strip().lower()
        try:
            with self._lock:
                rows = self._db().execute(
                    "SELECT text, ts FROM clips ORDER BY ts DESC LIMIT ?",
                    (self.MAX_ITEMS,)).fetchall()
        except Exception:
            return []
        out = []
        for text, ts in rows:
            if frag and frag not in text.lower():
                continue
            first = next((ln for ln in text.splitlines() if ln.strip()), text)
            title = first.strip()[:70] or "(whitespace)"
            extra = f" · {len(text):,} chars" if len(text) > len(first) else ""
            out.append(_res(title, f"{self._age(ts)}{extra}",
                            "📋", copy_text=text, kind="clip", chip="Clip"))
            if len(out) >= MAX_ROWS:
                break
        return out or [_res("Clipboard history is empty",
                            "captured while Anti-Raycast is warm — Win+V has the rest", "📋")]


# ── Claude Code sessions (for the Claude-mode chevron dropdown) ──────────────

class SessionsProvider:
    """Live Claude Code sessions the way Overlord sees them: `claude agents
    --json` (native exe), noise-filtered, with a TL;DR-aware summary from each
    session's transcript tail and a state color."""

    CLAUDE = Path.home() / ".local" / "bin" / "claude.exe"
    PROJECTS = Path.home() / ".claude" / "projects"
    TRIGGER = Path.home() / ".claude" / "overlord-send.trigger"
    # Overlord's exact --ov-* tokens (extension.js:707-710):
    # needs=red, working=amber, done/parked=green, idle=gray.
    STATE_COLORS = {"waiting": "#ff5c6c", "needs": "#ff5c6c",
                    "working": "#f5b14c", "busy": "#f5b14c",
                    "done": "#54d6a0", "idle": "#8b8b8b"}
    _cache: tuple[float, list] = (0.0, [])

    PARK_AFTER_S = 10 * 60      # matches Overlord's doneAfterMinutes default

    @staticmethod
    def _age_label(secs: float) -> str:
        if secs < 60:
            return f"{int(secs)}s"
        if secs < 3600:
            return f"{int(secs // 60)}m"
        return f"{secs / 3600:.1f}h".replace(".0h", "h")

    def cached(self) -> list | None:
        """Fresh cache or None — lets the UI decide skeleton vs instant fill."""
        ts, cached = SessionsProvider._cache
        return cached if time.time() - ts < 10 else None

    def list(self) -> list[dict]:
        ts, cached = SessionsProvider._cache
        if time.time() - ts < 10:
            return cached
        out = []
        try:
            raw = subprocess.run(
                [str(self.CLAUDE), "agents", "--json"],
                capture_output=True, timeout=6, encoding="utf-8",
                errors="replace", creationflags=CREATE_NO_WINDOW).stdout
            for a in json.loads(raw or "[]"):
                cwd = a.get("cwd", "")
                # Overlord's noise rule: the board's own labeler etc.
                if "\\Programs\\cursor" in cwd or a.get("name") == "_":
                    continue
                sid = a.get("sessionId", "")
                raw_status = a.get("status", "idle")
                # Raw status is only busy/idle — derive Overlord's richer
                # states from transcript idle time (their park clock).
                age = self._idle_age(cwd, sid)
                if raw_status in ("busy", "working"):
                    status, label = "working", "working"
                elif raw_status in ("waiting", "needs"):
                    status, label = "waiting", "needs you"
                elif age is not None and age >= self.PARK_AFTER_S:
                    status = "done"
                    label = f"parked {self._age_label(age)}"
                else:
                    status = "idle"
                    label = (f"idle {self._age_label(age)}"
                             if age is not None else "idle")
                from collections import Counter as _C
                title = self._title(cwd, sid) or a.get("name", "?")
                texts = self._tail_texts(cwd, sid)
                toks = self._tokens((title + " ") * 3 + " ".join(texts))
                out.append({
                    "sessionId": sid,
                    "name": title,
                    "auto_name": a.get("name", "?"),
                    "status": status,
                    "status_label": label,
                    "color": self.STATE_COLORS.get(status, "#707070"),
                    "summary": self._summary_from(texts),
                    "_tf": _C(toks),      # contextual-search index (BM25)
                    "_dl": len(toks),
                    "_age": age or 0,
                })
        except Exception:
            pass
        # working > waiting > idle > parked, freshest first inside each group
        rank = {"waiting": 0, "working": 1, "idle": 2, "done": 3}
        out.sort(key=lambda s: rank.get(s["status"], 4))
        SessionsProvider._cache = (time.time(), out)
        return out

    def _transcript(self, cwd: str, sid: str) -> Path:
        return self.PROJECTS / re.sub(r"[\\/:.]", "-", cwd) / f"{sid}.jsonl"

    def _title(self, cwd: str, sid: str) -> str:
        """Human session title — the type:'summary' lines Claude Code writes
        into the transcript (same source as the resume picker). Falls back to
        the first user message; None → caller keeps the auto-name."""
        try:
            fp = self._transcript(cwd, sid)
            size = fp.stat().st_size
            with open(fp, "rb") as f:
                head = f.read(32768).decode("utf-8", errors="replace")
                f.seek(max(0, size - 65536))
                tail = f.read().decode("utf-8", errors="replace")
            title = None
            for chunk in (head, tail):
                for line in chunk.splitlines():
                    if '"type":"summary"' not in line.replace(" ", ""):
                        continue
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    if o.get("type") == "summary" and o.get("summary"):
                        title = o["summary"]        # keep the LAST one seen
            if title:
                return title[:60]
            # Fallback: first real user message from the head.
            for line in head.splitlines():
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") == "user":
                    c = o.get("message", {}).get("content")
                    txt = c if isinstance(c, str) else next(
                        (b.get("text", "") for b in (c or [])
                         if isinstance(b, dict) and b.get("type") == "text"), "")
                    txt = " ".join(str(txt).split()).strip()
                    if txt and not txt.startswith(("<", "Caveat:")):
                        return txt[:60]
        except Exception:
            pass
        return ""

    def _idle_age(self, cwd: str, sid: str) -> float | None:
        """Seconds since the session's transcript last changed — the same
        signal Overlord's park clock keys off."""
        try:
            munged = re.sub(r"[\\/:.]", "-", cwd)
            fp = self.PROJECTS / munged / f"{sid}.jsonl"
            return max(0.0, time.time() - fp.stat().st_mtime)
        except Exception:
            return None

    def _tail_texts(self, cwd: str, sid: str) -> "list[str]":
        """All user+assistant text from the transcript tail (newest last) —
        feeds both the card summary and the contextual BM25 index."""
        texts = []
        try:
            fp = self._transcript(cwd, sid)
            with open(fp, "rb") as f:
                f.seek(max(0, fp.stat().st_size - 65536))
                tail = f.read().decode("utf-8", errors="replace")
            for line in tail.splitlines():
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") not in ("assistant", "user"):
                    continue
                c = o.get("message", {}).get("content")
                if isinstance(c, str):
                    texts.append(c)
                    continue
                for b in (c or []):
                    if isinstance(b, dict) and b.get("type") == "text" \
                            and b.get("text", "").strip():
                        texts.append(b["text"])
        except Exception:
            pass
        return texts

    @staticmethod
    def _summary_from(texts: "list[str]") -> str:
        """Last real message, TL;DR-aware (Overlord's sticky-summary idea)."""
        for t in reversed(texts):
            t = t.strip()
            if not t or t.startswith("<"):
                continue
            if "TL;DR" in t:
                t = t.split("TL;DR", 1)[1].lstrip(":* \n")
            return " ".join(t.split())[:80]
        return ""

    def _summary(self, cwd: str, sid: str) -> str:
        return self._summary_from(self._tail_texts(cwd, sid))

    # ── Contextual ranking (BM25, same shape as ClaudeSearchSessions) ────────

    @staticmethod
    def _tokens(text: str) -> "list[str]":
        return re.findall(r"[a-z0-9]+", text.lower())

    def rank(self, query: str, sess: "list[dict]") -> "list[dict]":
        """BM25 over title(×3)+summary+transcript-tail content, with the last
        query token treated as a prefix (as-you-type) and a recency boost —
        the ClaudeSearchSessions recipe, sized for ~16 live sessions."""
        from collections import Counter as _C
        import math
        q = self._tokens(query)
        if not q:
            return sess
        N = max(len(sess), 1)
        df: dict = _C()
        for s in sess:
            df.update(set(s.get("_tf", ())))
        avg_dl = (sum(s.get("_dl", 0) for s in sess) / N) or 1
        k1, b = 1.5, 0.75
        scored = []
        for s in sess:
            tf_map, dl = s.get("_tf") or {}, s.get("_dl") or 1
            score = 0.0
            for i, tok in enumerate(q):
                if i == len(q) - 1:                    # prefix-match last token
                    tf = sum(v for k, v in tf_map.items() if k.startswith(tok))
                    d = sum(1 for x in sess
                            if any(k.startswith(tok) for k in (x.get("_tf") or ())))
                else:
                    tf = tf_map.get(tok, 0)
                    d = df.get(tok, 0)
                if not tf:
                    continue
                idf = math.log((N - d + 0.5) / (d + 0.5) + 1)
                score += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avg_dl))
            if score > 0:
                # recency: fresher sessions win ties (CSS: +30% decaying)
                age = s.get("_age") or 0
                score *= 1.0 + 0.3 * math.exp(-age / (6 * 3600))
                scored.append((score, s))
        scored.sort(key=lambda t: -t[0])
        return [s for _, s in scored]

    def send(self, session_id: str, text: str):
        """Route a prompt into an EXISTING session via the Overlord
        send-trigger (the extension types it with terminal.sendText)."""
        self.TRIGGER.write_text(
            json.dumps({"sessionId": session_id, "text": text}),
            encoding="utf-8")


# ── Ask Claude (". <prompt>" → Claude Code session in Cursor) ────────────────

class AskClaudeProvider:
    SCRIPT = TOOL_DIR / "scripts" / "ask_claude.ps1"

    def __init__(self):
        self.target: dict | None = None    # set by the chip's session dropdown

    def rows(self, prompt: str):
        prompt = prompt.strip()
        t = self.target
        if not prompt:
            where = (f"→ types into session '{t['name']}'" if t
                     else ". <your request> — opens a Claude Code session in Cursor")
            return [_res("Ask Claude…", where, "🧠", chip="Claude")]

        if t:
            return [_res(f'Ask {t["name"]}: "{prompt[:50]}"',
                         "types into that session's prompt (via Overlord)",
                         "🧠", chip="Claude",
                         run=lambda p=prompt, s=t["sessionId"]:
                         sessions.send(s, p))]

        def _send(p=prompt):
            subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(self.SCRIPT), "-Prompt", p],
                creationflags=CREATE_NO_WINDOW)

        return [_res(f'Ask Claude: "{prompt[:60]}"',
                     "opens a new Claude Code session with this prompt",
                     "🧠", chip="Claude", run=_send)]


# ── Quick capture ("note <text>" → capture file) ──────────────────────────────

class CaptureProvider:
    INBOX = Path.home() / "Documents" / "Inbox.md"   # override: capture_file in config.yaml

    def append(self, text: str):
        line = f"- {time.strftime('%Y-%m-%d %H:%M')} {text.strip()}\n"
        if not self.INBOX.exists():
            self.INBOX.write_text("# 📥 Inbox\n\nQuick captures from Anti-Raycast "
                                  "(`note <text>`).\n\n", encoding="utf-8")
        with open(self.INBOX, "a", encoding="utf-8") as f:
            f.write(line)

    def rows(self, text: str):
        text = text.strip()
        if not text:
            return [_res("Capture a thought…", "note <text> — appends to "
                         f"{self.INBOX.name}", "📥", chip="Capture")]
        return [_res(f'Capture: "{text[:60]}"', f"→ {self.INBOX}",
                     "📥", chip="Capture",
                     run=lambda t=text: self.append(t))]


# ── Snippets ("snip [filter]" → canned strings, Enter copies) ────────────────

class SnippetProvider:
    FILE = TOOL_DIR / "snippets.yaml"

    def __init__(self):
        self._mtime = 0.0
        self._items: list[dict] = []

    def _load(self):
        try:
            mtime = self.FILE.stat().st_mtime
        except OSError:
            self._items = []
            return
        if mtime == self._mtime:
            return
        try:
            import yaml
            with open(self.FILE, "r", encoding="utf-8") as f:
                self._items = (yaml.safe_load(f) or {}).get("snippets", [])
            self._mtime = mtime
        except Exception:
            pass

    def search(self, frag: str):
        self._load()
        frag = frag.strip().lower()
        rows = []
        for s in self._items:
            hay = (s.get("name", "") + " "
                   + " ".join(str(k) for k in s.get("keywords", []))).lower()
            if frag and frag not in hay:
                continue
            rows.append(_res(s.get("name", "?"), str(s.get("text", ""))[:70],
                             "✂️", chip="Snip",
                             copy_text=str(s.get("text", ""))))
        return rows[:MAX_ROWS] or [_res("No matching snippets",
                                        "edit snippets.yaml", "✂️", chip="Snip")]


# ── Recent files ("recent [filter]" — Windows' own Recent items) ─────────────

class RecentProvider:
    DIR = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Recent"

    def search(self, frag: str):
        frag = frag.strip().lower()
        try:
            lnks = sorted(self.DIR.glob("*.lnk"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            return []
        rows = []
        for p in lnks:
            if frag and frag not in p.stem.lower():
                continue
            age = time.time() - p.stat().st_mtime
            ago = (f"{int(age // 60)}m" if age < 3600 else
                   f"{int(age // 3600)}h" if age < 86400 else
                   f"{int(age // 86400)}d")
            rows.append(_res(p.stem, f"{ago} ago", "🕘", chip="Recent",
                             icon_path=str(p),
                             run=lambda q=str(p): os.startfile(q)))
            if len(rows) >= MAX_ROWS:
                break
        return rows


# ── System glance ("sys" — battery, RAM, power scheme + toggles) ─────────────

class SysProvider:
    SCHEMES = {"Balanced": "381b4222-f694-41f0-9685-ff5bb260df2e",
               "Silent": "64a64f24-65b9-4b56-befd-5ec1eaced9b3",
               "Turbo": "6fecc5ae-f350-48a5-b669-b472cb895ccf"}

    def rows(self):
        out = []
        try:
            class SPS(ctypes.Structure):
                _fields_ = [("ACLineStatus", ctypes.c_byte),
                            ("BatteryFlag", ctypes.c_byte),
                            ("BatteryLifePercent", ctypes.c_byte),
                            ("SystemStatusFlag", ctypes.c_byte),
                            ("BatteryLifeTime", ctypes.c_ulong),
                            ("BatteryFullLifeTime", ctypes.c_ulong)]
            sps = SPS()
            ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(sps))
            ac = "plugged in" if sps.ACLineStatus == 1 else "on battery"
            out.append(_res(f"Battery {sps.BatteryLifePercent}%", ac, "🔋",
                            chip="System"))
        except Exception:
            pass
        try:
            class MEM(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MEM(dwLength=ctypes.sizeof(MEM))
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            used = (m.ullTotalPhys - m.ullAvailPhys) / 2 ** 30
            out.append(_res(f"RAM {used:.1f} / {m.ullTotalPhys / 2 ** 30:.0f} GB",
                            f"{m.dwMemoryLoad}% used", "🧮", chip="System"))
        except Exception:
            pass
        try:
            sch = subprocess.run(["powercfg", "/getactivescheme"],
                                 capture_output=True, timeout=2,
                                 encoding="utf-8", errors="replace",
                                 creationflags=CREATE_NO_WINDOW).stdout
            cur = sch.split("(")[-1].rstrip(")\n ") if "(" in sch else "?"
            out.append(_res(f"Power scheme: {cur}", "switch below", "⚡",
                            chip="System"))
        except Exception:
            cur = "?"
        for name, guid in self.SCHEMES.items():
            if name.lower() != cur.lower():
                out.append(_res(f"Switch to {name}", "powercfg /setactive", "⚡",
                                chip="System",
                                run=lambda g=guid: subprocess.Popen(
                                    ["powercfg", "/setactive", g],
                                    creationflags=CREATE_NO_WINDOW)))
        return out


# ── Kill-process builtin ("kill <fragment>") ──────────────────────────────────

class KillProvider:
    DENY = {"system", "system idle process", "csrss.exe", "wininit.exe",
            "winlogon.exe", "services.exe", "lsass.exe", "smss.exe",
            "dwm.exe", "svchost.exe"}

    def search(self, frag: str):
        frag = frag.strip().lower()
        if not frag:
            return [_res("Type a process name fragment…", "kill <name>", "❌")]
        try:
            out = subprocess.run(["tasklist", "/fo", "csv", "/nh"],
                                 capture_output=True, text=True, timeout=5,
                                 creationflags=CREATE_NO_WINDOW).stdout
        except Exception:
            return []
        groups: dict[str, dict] = {}
        for row in csv.reader(io.StringIO(out)):
            if len(row) < 5:
                continue
            name, mem = row[0], row[4]
            if name.lower() in self.DENY or frag not in name.lower():
                continue
            g = groups.setdefault(name, {"n": 0, "kb": 0})
            g["n"] += 1
            try:
                g["kb"] += int(mem.replace(".", "").replace(",", "")
                               .replace("K", "").strip())
            except ValueError:
                pass
        rows = []
        for name, g in sorted(groups.items(), key=lambda kv: -kv[1]["kb"]):
            rows.append(_res(
                f"End {name}",
                f"{g['n']} process(es), {g['kb'] / 1024:.0f} MB",
                "❌", confirm=True, chip="Kill",
                run=lambda n=name: subprocess.Popen(
                    ["taskkill", "/F", "/IM", n],
                    creationflags=CREATE_NO_WINDOW)))
        return rows[:MAX_ROWS]


# ── Pipeline ──────────────────────────────────────────────────────────────────

calc = CalcProvider()
apps = AppProvider()
actions = ActionProvider()
killer = KillProvider()
emojis = EmojiProvider()
vault = VaultProvider()
files = FileProvider()
clip = ClipProvider()
units = UnitProvider()
windows = WindowProvider()
sessions = SessionsProvider()
ask_claude = AskClaudeProvider()
capture = CaptureProvider()
snips = SnippetProvider()
recents = RecentProvider()
sysinfo = SysProvider()

# Plugins: any plugins/*.py exposing query(q) -> list[dict] (see plugins/README).
_plugins: list = []


def _load_plugins():
    import importlib.util
    _plugins.clear()
    for p in sorted((TOOL_DIR / "plugins").glob("*.py")):
        if p.stem.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                f"flint_plugin_{p.stem}", p)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            if callable(getattr(m, "query", None)):
                _plugins.append(m.query)
        except Exception:
            pass    # a broken plugin must never take the palette down


def _plugin_rows(q: str) -> list[dict]:
    out = []
    for fn in _plugins:
        try:
            for d in (fn(q) or [])[:3]:
                out.append(_res(d.get("title", "?"), d.get("subtitle", ""),
                                d.get("icon", "🔌"),
                                run=d.get("on_enter") or d.get("run"),
                                copy_text=d.get("copy_text"), chip="Plugin"))
        except Exception:
            pass
    return out

# Discoverability hints that ride along in the default fuzzy pool.
_HINTS = [
    ("emoji", _res("Emoji search", 'type ":" then a name — Enter copies', "😀",
                   set_query=":")),
    ("clipboard history", _res("Clipboard history", 'type "clip" — Enter copies, Ctrl+Enter pastes', "📋",
                               set_query="clip ")),
]


def _load_cfg() -> dict:
    try:
        import yaml
        with open(TOOL_DIR / "config.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def init():
    """Called once at worker spawn (cheap; index rebuilds are backgrounded)."""
    cfg = _load_cfg()
    apps.aliases = {str(k): str(v) for k, v in (cfg.get("aliases") or {}).items()}
    apps.init_async()
    actions.refresh()
    vault.init_async(cfg.get("vaults") or {})
    files.scoped = [os.path.expanduser(str(d)) for d in (cfg.get("scoped_dirs") or [])]
    if cfg.get("capture_file"):
        CaptureProvider.INBOX = Path(os.path.expandvars(
            os.path.expanduser(str(cfg["capture_file"]))))
    _load_plugins()


def file_search(q: str, limit: int = 8) -> list[dict]:
    """Called by the palette from a WORKER THREAD (debounced) — results are
    merged into the visible list when they arrive."""
    return files.search(q, limit)


def on_summon():
    """Called on every palette show — hot-reload actions.yaml if edited."""
    actions.refresh()


def _rebuild_all():
    apps.rebuild()
    vault.rebuild()


def _builtin_rows(sub: str):
    rows = []
    s = sub.lower()
    if not s or "refresh".startswith(s) or "index" in s:
        rows.append(_res("Refresh index", "rescan apps + vault notes", "🔄",
                         chip="Built-in",
                         run=lambda: threading.Thread(
                             target=_rebuild_all, daemon=True).start()))
    if not s or "restart".startswith(s) or "anti" in s or "raycast" in s:
        r = _res("Restart Anti-Raycast palette", "quits the worker — next Win tap respawns",
                 "♻️", chip="Built-in")
        r["app_quit"] = True
        rows.append(r)
    if not s or "latency".startswith(s):
        try:
            last = (TOOL_DIR / "cache" / "latency.log").read_text(
                encoding="utf-8").splitlines()[-1]
        except Exception:
            last = "no data yet — appears after a session with typing"
        rows.append(_res("Latency", last, "⏱️", chip="Built-in"))
    return rows


def _to_result(payload) -> dict:
    """Discriminate pool payloads: ready-made results carry "title", vault
    notes carry "vault", app index entries carry neither."""
    if "title" in payload:
        return payload
    if "vault" in payload:
        return VaultProvider.to_result(payload)
    return AppProvider.to_result(payload)


_WORD_SPLIT = re.compile(r"[\s\-_./:]+")

# Tier constants (VS Code-style unjumpable thresholds, RESEARCH.md finding 3):
# a query→choice association ALWAYS beats a prefix match, which always beats
# a word-boundary match, then substring, then typo-fuzzy fallback.
_T_PICKED, _T_PREFIX, _T_WORD, _T_SUB, _T_FUZZY = (
    4_000_000, 3_000_000, 2_000_000, 1_000_000, 0)


def _key_of(payload) -> str | None:
    if "title" in payload:
        return payload.get("key")
    if "vault" in payload:
        return f"note:{payload['vault']}/{payload['rel']}"
    return f"app:{payload.get('target', '')}"


def _fuzzy(q: str, limit: int, pool=None):
    """Tiered, usage-aware ranking over the candidate pool."""
    if pool is None:
        pool = (apps.candidates() + actions.candidates()
                + vault.candidates() + _HINTS)
    if not pool:
        return []
    q_low = q.lower()
    picked = usage.picked_for(q_low)          # query→choice layer
    scored, matched_idx = [], set()
    for i, (label, payload) in enumerate(pool):
        low = label.lower()
        if low.startswith(q_low):
            tier = _T_PREFIX
        elif any(w.startswith(q_low) for w in _WORD_SPLIT.split(low) if w):
            tier = _T_WORD
        elif q_low in low:
            tier = _T_SUB
        else:
            continue
        key = _key_of(payload)
        if key and picked.get(key, 0) > 0:
            tier = _T_PICKED
        # Within a tier: picked-count > selection-count boost > shorter label.
        score = (tier
                 + int(min(picked.get(key, 0), 10) * 50_000)
                 + int(min(usage.boost(key) if key else 0, 10) * 10_000)
                 - len(low))
        scored.append((score, i))
        matched_idx.add(i)
    # Typo-fuzzy fallback (transpositions etc.) only fills leftover slots —
    # tiered so it can't destabilize the learnable ordering (VS Code lesson).
    # Strict fuzz.ratio, NOT WRatio: partial-ratio scoring lets gibberish
    # queries "match" short labels, which would defeat the dead-end fallback.
    if len(scored) < limit:
        from rapidfuzz import fuzz, process, utils
        labels = [label for label, _ in pool]
        for _, sc, idx in process.extract(q, labels, scorer=fuzz.ratio,
                                          processor=utils.default_process,
                                          score_cutoff=72, limit=limit * 3):
            if idx not in matched_idx:
                scored.append((_T_FUZZY + int(sc), idx))
                matched_idx.add(idx)
    out, seen = [], set()
    for score, idx in sorted(scored, key=lambda t: -t[0]):
        r = _to_result(pool[idx][1])
        dedupe = (r["title"], r["subtitle"])
        if dedupe in seen:
            continue
        seen.add(dedupe)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def _resolve_keys(keys: list[str], limit: int) -> list[dict]:
    """Turn stored result keys back into live result rows (stale keys skip)."""
    if not keys:
        return []
    by_key = {}
    for label, payload in (apps.candidates() + actions.candidates()
                           + vault.candidates()):
        k = _key_of(payload)
        if k and k not in by_key:
            by_key[k] = payload
    out = []
    for k in keys:
        if k in by_key:
            out.append(_to_result(by_key[k]))
        elif k.startswith("file:") and os.path.exists(k[5:]):
            out.append(files._row(k[5:]))
        if len(out) >= limit:
            break
    return out


def zero_query(limit: int = 8) -> list[dict]:
    """Pre-typing state: frecency-ranked recently-picked items instead of an
    empty box. (Evidence gap acknowledged in RESEARCH.md — judgment call.)"""
    return _resolve_keys(usage.top_keys(limit * 2), limit)


def pinned_rows() -> list[dict]:
    return _resolve_keys(pins.keys(), PinStore.MAX)


def record_selection(row: dict, query: str):
    """Called by the palette after a row is executed."""
    if row.get("key"):
        usage.record(row["key"], query)


def search(q: str) -> list[dict]:
    q = q.strip()
    if not q:
        return zero_query()
    if q.startswith("."):        # ". <prompt>" → new Claude Code session
        return ask_claude.rows(q[1:])
    low = q.lower()
    if low.startswith("note ") or low == "note":
        return capture.rows(q[4:] if len(q) > 4 else "")
    if low == "snip" or low.startswith("snip "):
        return snips.search(q[4:])
    if low == "recent" or low.startswith("recent "):
        return recents.search(q[6:] if len(q) > 6 else "")
    if low == "sys" or low.startswith("sys "):
        return sysinfo.rows()
    if q.startswith(">"):        # actions + built-ins only
        sub = q[1:].strip()
        rows = _builtin_rows(sub)
        if sub:
            rows += _fuzzy(sub, MAX_ROWS, pool=actions.candidates())
        else:
            rows += [r for _, r in
                     {id(r): (None, r) for _, r in actions.candidates()}.values()]
        return rows[:MAX_ROWS]
    if q.startswith(":"):
        return emojis.search(q[1:])
    if q.startswith("?"):
        return vault.content_search(q[1:])
    if q.lower().startswith("kill "):
        return killer.search(q[5:])
    if q.lower() == "clip" or q.lower().startswith("clip "):
        return clip.search(q[4:])
    if q.lower().startswith("w "):     # "w " + fragment = window switcher
        return windows.search(q[2:])
    out = []
    # "<keyword> <terms>" web searches (g/yt/maps/gh/… from actions.yaml)
    parts = q.split(None, 1)
    if len(parts) == 2 and parts[0].lower() in actions.searches:
        from urllib.parse import quote
        url = actions.searches[parts[0].lower()].replace("{q}", quote(parts[1]))
        out.append(_res(f'Search {parts[0].lower()}: "{parts[1][:50]}"',
                        url[:80], "🔍", chip="Web",
                        run=lambda u=url: webbrowser.open(u)))
    c = calc.try_eval(q) or units.try_convert(q)
    if c:
        out.append(c)
    out += _plugin_rows(q)
    out += _fuzzy(q, MAX_ROWS - len(out))
    if not out:
        # Dead ends are a UX failure — always offer somewhere to go.
        from urllib.parse import quote
        out = [
            _res(f'Search the web for "{q}"', "opens your browser", "🔍",
                 chip="Web", run=lambda s=q: webbrowser.open(
                     "https://www.google.com/search?q=" + quote(s))),
        ]
    return out[:MAX_ROWS]


# ── Selftest ──────────────────────────────────────────────────────────────────

def _selftest():
    print("building index ...")
    built = indexer.build_index()
    assert built, "index came back empty"
    names = [a["name"].lower() for a in built]
    assert any("notepad" in n for n in names), "notepad not found in index"
    print(f"  index OK ({len(built)} apps)")

    r = calc.try_eval("2+2*3")
    assert r and r["copy_text"] == "8", r
    assert calc.try_eval("__import__('os')") is None
    assert calc.try_eval("().__class__") is None
    assert calc.try_eval("sqrt(144)")["copy_text"] == "12"
    print("  calc OK")

    actions.refresh()
    cands = actions.candidates()
    assert cands, "actions.yaml produced no candidates"
    print(f"  actions OK ({len(cands)} keywords)")

    rows = killer.search("pythonw")
    assert all("lsass" not in r["title"].lower() for r in killer.search("s"))
    print(f"  killproc OK ({len(rows)} match(es) for 'pythonw')")

    em = emojis.search("fire")
    assert em and any(r["copy_text"] == "\U0001F525" for r in em), \
        [r["title"] for r in em]
    assert emojis.search("rocket")[0]["copy_text"] == "\U0001F680"
    assert emojis.search("rose")[0]["copy_text"] == "\U0001F339"  # prefix beats fuzzy
    grid = emojis.search("")
    assert grid and all(r["kind"] == "emoji" for r in grid), "empty ':' must give the starter grid"
    assert not any("skin tone" in r["title"] for r in emojis.search("thumbs")), "tone variants must be filtered"
    print(f"  emoji OK ('fire' -> {len(em)} hits, empty-grid {len(grid)})")

    cfg = _load_cfg()
    vault.init_async(cfg.get("vaults") or {})
    files.scoped = [os.path.expanduser(str(d)) for d in (cfg.get("scoped_dirs") or [])]
    for _ in range(100):                     # wait for the async vault build
        if vault.notes:
            break
        time.sleep(0.1)
    per_vault = {v: sum(1 for n in vault.notes if n["vault"] == v)
                 for v in vault.vaults}
    assert all(c > 0 for c in per_vault.values()), per_vault
    print(f"  vault OK {per_vault}")

    fr = files.search("flint")
    print(f"  files OK - SystemIndex/fallback returned {len(fr)} for 'flint'"
          + ("" if fr else " (fallback still building - acceptable)"))

    cs = vault.content_search("todo")
    assert isinstance(cs, list), cs
    print(f"  content-search OK - '?todo' -> {len(cs)} file(s)")

    clip.record("flint-selftest-alpha")
    clip.record("flint-selftest-beta")
    clip.record("flint-selftest-alpha")          # dedupe: bumps, not duplicates
    cr = clip.search("flint-selftest")
    assert cr[0]["copy_text"] == "flint-selftest-alpha", [r["title"] for r in cr]
    assert len([r for r in cr if "alpha" in r["title"]]) == 1, "dedupe failed"
    print(f"  clip OK ({len(cr)} rows, dedupe verified)")

    _load_plugins()
    pr = _plugin_rows("uuid")
    assert pr and len(pr[0]["copy_text"]) == 36, pr
    print(f"  plugins OK (uuid -> {pr[0]['title'][:8]}…)")

    apps.init_async()
    time.sleep(0.1)
    hits = search("notepad")
    assert hits and any("notepad" in h["title"].lower() for h in hits), hits
    print(f"  pipeline OK - 'notepad' -> {hits[0]['title']}")
    # Tiered ranking: prefix beats substring beats fuzzy.
    pool = [("Notepad", _res("Notepad", key="t:prefix")),
            ("My Notepad Clone", _res("My Notepad Clone", key="t:word")),
            ("SomeNotepadish", _res("SomeNotepadish", key="t:sub")),
            ("Notpad Typo", _res("Notpad Typo", key="t:fuzzy"))]
    order = [r["title"] for r in _fuzzy("notep", 9, pool=pool)]
    assert order[0] == "Notepad" and order[1] == "My Notepad Clone", order
    print(f"  tiers OK - {order}")

    # Learned layer: query→choice beats every string tier.
    usage.record("t:sub", "notep")
    order2 = [r["title"] for r in _fuzzy("notep", 9, pool=pool)]
    assert order2[0] == "SomeNotepadish", order2
    print(f"  learned OK - after picking, 'notep' -> {order2[0]}")

    # Zero-query surfaces the recorded pick (resolved via a matching pool).
    assert usage.top_keys(5), "usage store must persist selections"
    print(f"  zero-query OK - top keys: {usage.top_keys(3)}")

    u = units.try_convert("72 f to c")
    assert u and u["copy_text"].startswith("22.2"), u
    assert units.try_convert("5 km to mi")["copy_text"].startswith("3.10"), \
        units.try_convert("5 km to mi")
    assert units.try_convert("hello world") is None
    print(f"  units OK (72f -> {u['copy_text']}c)")

    w = windows.search("")
    assert w and any(r["chip"] == "Window" for r in w), w
    print(f"  windows OK ({len(w)} open windows)")

    fb = search("zzqqxxnothingmatchesthis")
    assert fb and fb[0]["chip"] == "Web", [r["title"] for r in fb]
    print("  fallback OK (dead end -> web search row)")

    eng = search("g fastapi docs")
    assert eng[0]["chip"] == "Web" and "fastapi" in eng[0]["title"], eng[0]
    print("  engines OK ('g <q>' -> Google row first)")

    dz = ask_claude.rows("hello")
    assert dz and callable(dz[0]["run"]), dz
    cp = capture.rows("test thought")
    assert cp and callable(cp[0]["run"]), cp
    sn = snips.search("")
    assert isinstance(sn, list), sn
    rc = recents.search("")
    sy = sysinfo.rows()
    assert any("Battery" in r["title"] for r in sy), sy
    assert any("RAM" in r["title"] for r in sy), sy
    print(f"  ask_claude/capture/snips OK · recents {len(rc)} rows · sys {len(sy)} rows")

    assert mode_of_query(". hello") == "ask"
    assert mode_of_query("note buy milk") == "note"
    assert mode_of_query("granola") is None, "bare 'g...' words must not count as google"
    usage.record_mode("sys"); usage.record_mode("sys"); usage.record_mode("sys")
    assert ordered_modes()[0]["id"] == "sys"
    assert len(ordered_modes()) == len(MODES)
    print(f"  modes OK ({len(MODES)} pills, 'sys' promoted after 3 uses)")

    # Latency: full pipeline over the real pool must stay well under budget.
    t0 = time.perf_counter()
    for _ in range(50):
        search("war")
    per = (time.perf_counter() - t0) / 50 * 1000
    assert per < 50, f"search too slow: {per:.1f}ms"
    print(f"  latency OK - {per:.1f}ms/query over {len(apps.candidates()) + len(vault.candidates())} candidates")
    print("SELFTEST PASSED")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
