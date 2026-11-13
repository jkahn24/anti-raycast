"""
Flint Launcher — the resident stub
----------------------------------
The ONLY Flint process that stays resident (~10-15 MB, 0% CPU idle — blocked
in GetMessageW). Registers Ctrl+Alt+Space system-wide, spawns/signals the
palette worker, and runs the watchdog that enforces the resource budget on it.

Why the watchdog lives HERE and not in the worker: Flint replaces Raycast,
whose failure mode was the resident process itself running away. Enforcement
must sit outside the process being policed.

Patterns cloned from WisperWin/launcher.py (mutex + hotkey + spawn) and
an earlier tool of mine (AllowSetForegroundWindow handshake).
"""

import ctypes
import ctypes.wintypes
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from hotkey_util import parse_hotkey_safe

# ── Windows API constants ─────────────────────────────────────────────────────
MOD_NOREPEAT = 0x4000   # prevent repeated WM_HOTKEY while held
HOTKEY_ID    = 1
WM_HOTKEY    = 0x0312
SYNCHRONIZE  = 0x00100000
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WAIT_OBJECT_0 = 0

EVENT_NAME        = "Flint_ShowPalette"
MUTEX_NAME        = "Flint_Launcher"
WORKER_MUTEX_NAME = "Flint_Palette"

TOOL_DIR     = Path(__file__).parent
PID_FILE     = TOOL_DIR / "cache" / "palette.pid"
WATCHDOG_LOG = Path(os.environ.get("TEMP", str(TOOL_DIR))) / "flint_watchdog.log"

user32   = ctypes.windll.user32
# use_last_error=True atomically captures GetLastError() so get_last_error()
# reliably returns 183 (ERROR_ALREADY_EXISTS) from CreateMutexW.
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Watchdog caps — defaults; overridden by config.yaml `watchdog:` if readable.
CAPS = {"cpu_pct_max": 20.0, "cpu_sustain_s": 60, "rss_mb_max": 300}


def _load_config() -> dict:
    """Best-effort read of the flat subset of config.yaml the stub needs
    (hotkey + watchdog scalars). Hand-rolled instead of PyYAML so the stub
    runs on SYSTEM pythonw — running it from the venv costs a second process
    (venv\\Scripts\\pythonw.exe is a shim that spawns the base interpreter).
    Must never die on a bad config."""
    cfg: dict = {}
    try:
        section = None
        for raw in (TOOL_DIR / "config.yaml").read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].rstrip()
            if not line.strip() or ":" not in line:
                continue
            key, _, val = line.partition(":")
            val = val.strip()
            if not raw.startswith((" ", "\t")):          # top-level key
                section = key.strip()
                if val:
                    cfg[section] = val
                    section = None
                else:
                    cfg[section] = {}
            elif section and isinstance(cfg.get(section), dict) and val:
                try:
                    cfg[section][key.strip()] = float(val)
                except ValueError:
                    cfg[section][key.strip()] = val
    except Exception:
        pass
    return cfg


def _get_worker_pythonw() -> Path:
    """The palette always runs from Flint's venv (PyQt6 lives there)."""
    venv = TOOL_DIR / "venv" / "Scripts" / "pythonw.exe"
    return venv if venv.exists() else Path(sys.executable)


def _worker_running() -> bool:
    h = kernel32.OpenMutexW(SYNCHRONIZE, False, WORKER_MUTEX_NAME)
    if h:
        kernel32.CloseHandle(h)
        return True
    return False


_last_launch_t: float = 0.0
_LAUNCH_DEBOUNCE_S: float = 20.0   # also the crash-loop guard after a watchdog kill


def _launch_palette():
    global _last_launch_t
    now = time.monotonic()
    if now - _last_launch_t < _LAUNCH_DEBOUNCE_S:
        return
    _last_launch_t = now
    subprocess.Popen(
        [str(_get_worker_pythonw()), str(TOOL_DIR / "palette.py")],
        creationflags=0x00000008,   # DETACHED_PROCESS
        close_fds=True,
        cwd=str(TOOL_DIR),
    )


def _read_worker_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:
        return None


def _allow_palette_foreground():
    """Transfer foreground permission to the palette before signaling it —
    the ONLY reliable way for a background process to take focus on Win11."""
    pid = _read_worker_pid()
    if pid:
        try:
            user32.AllowSetForegroundWindow(pid)
        except Exception:
            pass


# ── Watchdog ──────────────────────────────────────────────────────────────────

class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.wintypes.DWORD),
        ("PageFaultCount", ctypes.wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _proc_cpu_seconds(h) -> float | None:
    """Total kernel+user CPU seconds consumed by the process (GetProcessTimes)."""
    ft = ctypes.wintypes.FILETIME
    created, exited, kern, user = ft(), ft(), ft(), ft()
    if not kernel32.GetProcessTimes(h, ctypes.byref(created), ctypes.byref(exited),
                                    ctypes.byref(kern), ctypes.byref(user)):
        return None
    def _secs(t):  # FILETIME is 100ns units split across two DWORDs
        return ((t.dwHighDateTime << 32) | t.dwLowDateTime) / 1e7
    return _secs(kern) + _secs(user)


def _proc_rss_mb(h) -> float | None:
    pmc = _PROCESS_MEMORY_COUNTERS()
    pmc.cb = ctypes.sizeof(pmc)
    if not kernel32.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
        return None
    return pmc.WorkingSetSize / (1024 * 1024)


def _wd_log(msg: str):
    try:
        with open(WATCHDOG_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _watchdog_loop():
    """Sample the worker every 10s via WaitForSingleObject (parks costlessly
    while waiting; doubles as the death detector). Kill on sustained CPU or
    RSS breach. NO auto-respawn — the next hotkey press respawns, and the
    20s launch debounce prevents crash loops."""
    ncores = os.cpu_count() or 8
    hot_needed = max(1, int(CAPS["cpu_sustain_s"] / 10))
    h = None
    last_cpu = last_t = None
    hot = 0
    while True:
        if h is None:
            pid = _read_worker_pid()
            if pid and _worker_running():
                h = kernel32.OpenProcess(
                    PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE,
                    False, pid) or None
                last_cpu = last_t = None
                hot = 0
            if h is None:
                time.sleep(10)
                continue
        if kernel32.WaitForSingleObject(h, 10_000) == WAIT_OBJECT_0:
            kernel32.CloseHandle(h)   # worker exited on its own
            h = None
            continue
        cpu, rss, now = _proc_cpu_seconds(h), _proc_rss_mb(h), time.monotonic()
        kill_reason = None
        if rss is not None and rss > CAPS["rss_mb_max"]:
            kill_reason = f"RSS {rss:.0f} MB > {CAPS['rss_mb_max']} MB"
        if cpu is not None and last_cpu is not None:
            pct = (cpu - last_cpu) / (now - last_t) * 100 / ncores
            hot = hot + 1 if pct > CAPS["cpu_pct_max"] else 0
            if hot >= hot_needed:
                kill_reason = (f"CPU {pct:.1f}% of total > {CAPS['cpu_pct_max']}% "
                               f"sustained {CAPS['cpu_sustain_s']}s")
        last_cpu, last_t = cpu, now
        if kill_reason:
            _wd_log(f"KILLED palette (pid {_read_worker_pid()}): {kill_reason}")
            kernel32.TerminateProcess(h, 1)
            kernel32.CloseHandle(h)
            h = None
            hot = 0


# ── Bare-Win-key mode (low-level keyboard hook) ──────────────────────────────
# RegisterHotKey can't bind a lone modifier, so `hotkey: win` installs a
# WH_KEYBOARD_LL hook: a Win press+release with no other key in between
# summons Flint, and the real Win keyup is swallowed and replayed behind a
# dummy keystroke so the Start Menu never opens. Win+<anything> combos pass
# through untouched. Same trick AutoHotkey/PowerToys use.

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
VK_LWIN, VK_RWIN = 0x5B, 0x5C
VK_DUMMY = 0xFF                     # unassigned VK — masks the bare-Win release
KEYEVENTF_KEYUP = 0x0002


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.wintypes.DWORD),
        ("scanCode", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.wintypes.ULONG)),
    ]


_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int,
                               ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM)
_hook_ref = None    # keep the callback alive — GC'd callbacks crash the hook
LLKHF_INJECTED = 0x10
WM_FLINT_SUMMON = 0x8001    # WM_APP+1 — posted by the hook, handled in the loop

user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.CallNextHookEx.restype = ctypes.c_longlong
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                  ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]


def _install_win_hook(main_thread_id: int):
    """The hook callback must stay FAST — Windows silently unhooks slow LL
    hooks (LowLevelHooksTimeout). So it only masks the Start Menu and posts
    WM_FLINT_SUMMON back to the message loop, which does the real work."""
    state = {"down": False, "used": False, "replaying": False}

    def on_trigger():
        user32.PostThreadMessageW(main_thread_id, WM_FLINT_SUMMON, 0, 0)

    def proc(nCode, wParam, lParam):
        if nCode == 0:
            kb = ctypes.cast(lParam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
            vk = kb.vkCode
            injected = bool(kb.flags & LLKHF_INJECTED)
            if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                if vk in (VK_LWIN, VK_RWIN):
                    if not state["replaying"]:
                        state["down"], state["used"] = True, False
                elif state["down"] and not injected:
                    # Only PHYSICAL keys make this a Win+combo. Injected keys
                    # while Win is held are other tools' Start-menu mask keys
                    # (observed: something on this machine injects F13 on Win
                    # keydown) — counting them would kill bare-Win detection.
                    state["used"] = True
            elif wParam in (WM_KEYUP, WM_SYSKEYUP) and vk in (VK_LWIN, VK_RWIN):
                if state["replaying"]:
                    state["replaying"] = False   # our replayed keyup — pass through
                elif state["down"] and not state["used"]:
                    # Bare Win release: swallow it, replay dummy+keyup so the
                    # shell sees Win+dummy (no Start Menu), then summon Flint.
                    state["down"] = False
                    state["replaying"] = True
                    user32.keybd_event(VK_DUMMY, 0, 0, 0)
                    user32.keybd_event(VK_DUMMY, 0, KEYEVENTF_KEYUP, 0)
                    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
                    on_trigger()
                    return 1                     # eat the real keyup
                else:
                    state["down"] = False
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    global _hook_ref
    _hook_ref = _HOOKPROC(proc)
    return user32.SetWindowsHookExW(WH_KEYBOARD_LL, _hook_ref, None, 0)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Flint.App")

    # Single-instance guard. Clear last-error first: a stale 183 survives a
    # SUCCESSFUL CreateMutexW (2026-06-10 bug, hit across all house tools).
    ctypes.set_last_error(0)
    _mutex = kernel32.CreateMutexW(None, True, MUTEX_NAME)
    if ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS
        sys.exit(0)

    cfg = _load_config()
    try:
        CAPS.update({k: float(v) for k, v in (cfg.get("watchdog") or {}).items()
                     if k in CAPS})
    except Exception:
        pass

    h_event = kernel32.CreateEventW(None, False, False, EVENT_NAME)
    if not h_event:
        sys.exit(1)

    threading.Thread(target=_watchdog_loop, daemon=True).start()

    def _summon():
        if _worker_running():
            _allow_palette_foreground()
            kernel32.SetEvent(h_event)       # palette toggles show/hide
        else:
            _launch_palette()                # cold path: palette shows on start

    spec = str(cfg.get("hotkey") or "").strip().lower()
    use_win_hook = spec in ("win", "lwin", "windows")
    if use_win_hook:
        if not _install_win_hook(kernel32.GetCurrentThreadId()):
            _wd_log("SetWindowsHookExW failed — cannot bind the Win key.")
            sys.exit(1)
    else:
        hk = parse_hotkey_safe(spec)
        if not user32.RegisterHotKey(None, HOTKEY_ID, hk["mod_flags"] | MOD_NOREPEAT, hk["vk"]):
            time.sleep(1)
            if not user32.RegisterHotKey(None, HOTKEY_ID, hk["mod_flags"] | MOD_NOREPEAT, hk["vk"]):
                _wd_log(f"RegisterHotKey failed — {hk['label']} may be claimed by another app.")
                sys.exit(1)

    # NO startup launch of the worker (deliberate — the anti-Raycast choice):
    # the palette spawns on the FIRST summon of the session only.
    msg = ctypes.wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if (msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID) \
                or msg.message == WM_FLINT_SUMMON:
            _summon()
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))

    if not use_win_hook:
        user32.UnregisterHotKey(None, HOTKEY_ID)


if __name__ == "__main__":
    main()
