"""
Flint Palette — the summoned worker
-----------------------------------
Spawned by launcher.py on the first summon of the session; shows immediately,
then stays warm (hidden on Esc/focus-loss) so every later summon is instant.
The stub's watchdog kills this process if it ever exceeds the resource budget
in config.yaml — recovery is simply the next hotkey press.

Idle cost while warm: one 100ms named-event poll (proven near-zero).
No other timers, no background loops.

Keys: type to search · ↑/↓ select · Enter run · Ctrl+1-9 jump-run · Esc hide
Prefixes: ">" actions/built-ins · "kill <name>" process killer
"""

import os
import sys
import ctypes
import ctypes.wintypes
import time
import traceback
from pathlib import Path

TOOL_DIR  = Path(__file__).parent
CACHE_DIR = TOOL_DIR / "cache"
CRASH_LOG = CACHE_DIR / "crash.log"
PID_FILE  = CACHE_DIR / "palette.pid"

SHOW_EVENT_NAME   = "Flint_ShowPalette"
WORKER_MUTEX_NAME = "Flint_Palette"

CACHE_DIR.mkdir(exist_ok=True)


def _log_exc(label="", exc_info=None):
    try:
        with open(CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n=== {label} ===\n")
            if exc_info:
                f.writelines(traceback.format_exception(*exc_info))
            else:
                traceback.print_exc(file=f)
    except Exception:
        pass


sys.excepthook = lambda t, v, tb: _log_exc(f"uncaught {t.__name__}: {v}", exc_info=(t, v, tb))

# Qt 6.11 + Win11 TSF/IME crash workarounds
os.environ["QT_IM_MODULE"] = "none"
try:
    ctypes.windll.imm32.ImmDisableIME(-1)
    ctypes.windll.imm32.ImmDisableTextFrameworkThreadContext()
except Exception:
    pass
try:
    ctypes.windll.combase.RoInitialize(0)
except Exception:
    pass

# ── Single-instance guard (same pre-clear pattern as the stub) ────────────────
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ctypes.set_last_error(0)
_mutex = kernel32.CreateMutexW(None, True, WORKER_MUTEX_NAME)
if ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS
    sys.exit(0)

PID_FILE.write_text(str(os.getpid()))

import threading  # noqa: E402
from PyQt6.QtCore import (  # noqa: E402
    Qt, QTimer, QEvent, QFileInfo, QRect, QSize, pyqtSignal,
)
from PyQt6.QtGui import (  # noqa: E402
    QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QIcon, QPainter,
    QPen, QPixmap,
)
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QFileIconProvider, QFrame, QGraphicsDropShadowEffect,
    QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem, QMenu,
    QStyle, QStyledItemDelegate, QSystemTrayIcon, QVBoxLayout, QWidget,
)

import providers  # noqa: E402

user32 = ctypes.windll.user32

# ── Visual tokens — cmdk's Raycast replica + real Raycast theme JSONs ────────
# (research 2026-07-13: values verbatim from pacocoursey/cmdk raycast.scss,
# raycast/theme-explorer, MS Learn DWM docs — see RESEARCH.md sources)
BG_SOLID    = "#1C1C1C"                    # gray2 — fallback window fill
BG_ACRYLIC  = "rgba(28,28,28,80%)"         # tint painted over DWM acrylic
HAIRLINE    = "rgba(255,255,255,8%)"       # 1px separators + window border
TXT_PRIMARY   = "#EDEDED"                  # gray12 — titles, input
TXT_SECONDARY = "#A0A0A0"                  # gray11 — subtitles, footer, headings
TXT_PLACEHOLDER = "#707070"                # gray9
SEL_WASH   = "rgba(255,255,255,7%)"        # selection = white wash, NEVER accent
HOVER_WASH = "rgba(255,255,255,4%)"
CHIP_BG, CHIP_BG_SEL = "#2E2E2E", "#3E3E3E"   # kbd-chip grays (gray5/gray7)
ACCENT = "#3b82f6"                         # match highlight + loader only

L2, L4 = CHIP_BG, CHIP_BG_SEL              # legacy aliases (pills, tiles, chip)

ROW_H = 48                                     # two-line rows (title + subtitle)
GRID_COLS, GRID_W, GRID_H = 8, 72, 60          # emoji grid geometry
ROW_DATA = Qt.ItemDataRole.UserRole            # row dict for the delegate

STYLESHEET = f"""
QLineEdit {{
    background: transparent;
    border: none;
    padding: 0 18px;
    min-height: 54px;
    color: {TXT_PRIMARY};
    font-family: 'Segoe UI Variable Text', 'Segoe UI';
    font-size: 17px;
    selection-background-color: {ACCENT};
}}
QFrame#hairline {{
    background: {HAIRLINE};
    border: none;
    min-height: 1px;
    max-height: 1px;
}}
QListWidget {{
    background: transparent;
    border: none;
    outline: none;
    color: {TXT_PRIMARY};
    font-family: 'Segoe UI';
    font-size: 14px;
}}
QScrollBar:vertical {{ width: 6px; background: transparent; }}
QScrollBar::handle:vertical {{ background: {CHIP_BG_SEL}; border-radius: 3px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QLabel#caption {{
    color: {TXT_SECONDARY};
    font-family: 'Segoe UI';
    font-size: 12px;
    padding: 0 18px;
    min-height: 36px;
    max-height: 36px;
}}
"""

# Emoji grid cells (delegate defers to default painting for these).
GRID_ITEM_QSS = f"""
QListWidget::item {{
    border-radius: 8px;
    padding: 0;
    width: {GRID_W - 4}px;
    height: {GRID_H - 4}px;
}}
QListWidget::item:hover {{ background: {HOVER_WASH}; }}
QListWidget::item:selected {{ background: {SEL_WASH}; }}
"""


# ── Windows 11 acrylic (DWM system backdrop) ─────────────────────────────────
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33            # 2 = round (8px, fixed by DWM)
DWMWA_SYSTEMBACKDROP_TYPE = 38                 # 3 = acrylic (transient window)


class _DWM_MARGINS(ctypes.Structure):
    _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int),
                ("t", ctypes.c_int), ("b", ctypes.c_int)]


def apply_acrylic(widget) -> bool:
    """Real Windows 11 glass on a frameless Qt window. Order matters, and the
    client area must keep a non-zero-alpha tint or the backdrop renders black
    (Qt Forum 162168). Returns False below build 22621 → solid fallback."""
    try:
        if sys.getwindowsversion().build < 22621:
            return False
        hwnd = int(widget.winId())
        dwm = ctypes.windll.dwmapi
        one, round_, acrylic = (ctypes.c_int(1), ctypes.c_int(2), ctypes.c_int(3))
        dwm.DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
                                  ctypes.byref(one), ctypes.sizeof(one))
        dwm.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
                                  ctypes.byref(round_), ctypes.sizeof(round_))
        margins = _DWM_MARGINS(-1, -1, -1, -1)   # extend frame = enable backdrop
        dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
        return dwm.DwmSetWindowAttribute(
            hwnd, DWMWA_SYSTEMBACKDROP_TYPE,
            ctypes.byref(acrylic), ctypes.sizeof(acrylic)) == 0
    except Exception:
        return False


# ── Shell icon extraction ─────────────────────────────────────────────────────
# QFileIconProvider returns extension-generic icons (plain doc, bare shortcut
# arrow) — the REAL per-item icons come from the shell. IShellItemImageFactory
# handles both file paths and "shell:AppsFolder\<AUMID>" (UWP tiles);
# SHGetFileInfoW is the fallback.

_shell32 = ctypes.windll.shell32
_gdi32 = ctypes.windll.gdi32
_ole32 = ctypes.windll.ole32


class _GUID(ctypes.Structure):
    _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort),
                ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _SHFILEINFOW(ctypes.Structure):
    _fields_ = [("hIcon", ctypes.c_void_p), ("iIcon", ctypes.c_int),
                ("dwAttributes", ctypes.wintypes.DWORD),
                ("szDisplayName", ctypes.c_wchar * 260),
                ("szTypeName", ctypes.c_wchar * 80)]


_IID_IShellItemImageFactory = _GUID()
_ole32.IIDFromString("{bcc18b79-ba16-442f-80c4-8a59c30c463b}",
                     ctypes.byref(_IID_IShellItemImageFactory))
_SIIGBF_ICONONLY = 0x4


def _hbitmap_to_qimage(hbm) -> "QImage | None":
    """32bpp HBITMAP → QImage via GetDIBits (no reliance on optional
    QImage.fromHBITMAP bindings)."""
    from PyQt6.QtGui import QImage

    class BITMAP(ctypes.Structure):
        _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long),
                    ("bmHeight", ctypes.c_long), ("bmWidthBytes", ctypes.c_long),
                    ("bmPlanes", ctypes.c_ushort), ("bmBitsPixel", ctypes.c_ushort),
                    ("bmBits", ctypes.c_void_p)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_uint16),
                    ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                    ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_uint32),
                    ("biClrImportant", ctypes.c_uint32)]

    bm = BITMAP()
    if not _gdi32.GetObjectW(hbm, ctypes.sizeof(bm), ctypes.byref(bm)):
        return None
    w, h = bm.bmWidth, bm.bmHeight
    bih = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0,
                           0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    hdc = ctypes.windll.user32.GetDC(None)
    try:
        if not _gdi32.GetDIBits(hdc, hbm, 0, h, buf, ctypes.byref(bih), 0):
            return None
    finally:
        ctypes.windll.user32.ReleaseDC(None, hdc)
    img = QImage(bytes(buf), w, h, w * 4, QImage.Format.Format_ARGB32)
    return img.copy()          # detach from the buffer


def _shell_item_icon(parse_name: str, px: int = 48) -> "QIcon | None":
    """Real shell icon for a path OR 'shell:AppsFolder\\AUMID' (UWP)."""
    ppv = ctypes.c_void_p()
    hr = _shell32.SHCreateItemFromParsingName(
        ctypes.c_wchar_p(parse_name), None,
        ctypes.byref(_IID_IShellItemImageFactory), ctypes.byref(ppv))
    if hr != 0 or not ppv.value:
        return None
    try:
        vtbl = ctypes.cast(ppv, ctypes.POINTER(
            ctypes.POINTER(ctypes.c_void_p))).contents
        get_image = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, _SIZE, ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p))(vtbl[3])
        hbm = ctypes.c_void_p()
        if get_image(ppv, _SIZE(px, px), _SIIGBF_ICONONLY,
                     ctypes.byref(hbm)) != 0 or not hbm.value:
            return None
        try:
            img = _hbitmap_to_qimage(hbm)
        finally:
            _gdi32.DeleteObject(hbm)
        if img is None or img.isNull():
            return None
        return QIcon(QPixmap.fromImage(img))
    finally:
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(
            ctypes.cast(ppv, ctypes.POINTER(
                ctypes.POINTER(ctypes.c_void_p))).contents[2])
        release(ppv)


def _shgetfileinfo_icon(path: str) -> "QIcon | None":
    sfi = _SHFILEINFOW()
    if not _shell32.SHGetFileInfoW(ctypes.c_wchar_p(path), 0,
                                   ctypes.byref(sfi), ctypes.sizeof(sfi),
                                   0x100):     # SHGFI_ICON | SHGFI_LARGEICON
        return None
    if not sfi.hIcon:
        return None
    try:
        # Render the HICON into a bitmap we can convert.
        from PyQt6.QtGui import QImage
        px = 32
        hdc_screen = ctypes.windll.user32.GetDC(None)
        hdc = _gdi32.CreateCompatibleDC(hdc_screen)
        hbm = _gdi32.CreateCompatibleBitmap(hdc_screen, px, px)
        old = _gdi32.SelectObject(hdc, hbm)
        ctypes.windll.user32.DrawIconEx(hdc, 0, 0, sfi.hIcon, px, px, 0, None, 3)
        _gdi32.SelectObject(hdc, old)
        img = _hbitmap_to_qimage(hbm)
        _gdi32.DeleteObject(hbm)
        _gdi32.DeleteDC(hdc)
        ctypes.windll.user32.ReleaseDC(None, hdc_screen)
        if img is None or img.isNull():
            return None
        return QIcon(QPixmap.fromImage(img))
    finally:
        ctypes.windll.user32.DestroyIcon(sfi.hIcon)


def shell_icon(parse_name: str) -> "QIcon | None":
    try:
        return (_shell_item_icon(parse_name)
                or _shgetfileinfo_icon(parse_name))
    except Exception:
        return None


def _match_spans(title: str, q: str) -> list[tuple[int, int]]:
    """Character spans of `title` that `q` matched — contiguous hit if there
    is one, else a greedy in-order subsequence (mirrors the fuzzy fallback)."""
    if not q:
        return []
    t, q = title.lower(), q.lower()
    i = t.find(q)
    if i >= 0:
        return [(i, len(q))]
    spans, start, last, ti = [], -1, -1, 0
    for ch in q:
        j = t.find(ch, ti)
        if j < 0:
            return []
        if start == -1:
            start = last = j
        elif j == last + 1:
            last = j
        else:
            spans.append((start, last - start + 1))
            start = last = j
        ti = j + 1
    spans.append((start, last - start + 1))
    return spans


class IconResolver:
    """Shared, cached shell-icon lookup (delegate rows + pin bar)."""
    CAP = 600
    _MISS = object()

    def __init__(self):
        self._provider = None
        self._cache: dict[str, object] = {}

    def get(self, path: str) -> QIcon | None:
        qi = self._cache.get(path)
        if qi is None:
            if len(self._cache) > self.CAP:
                self._cache.clear()
            # Real shell icon first (per-file art, .lnk/.url targets, UWP
            # tiles); QFileIconProvider only as the last resort — it returns
            # extension-generic icons.
            qi = shell_icon(path)
            if qi is None and not path.startswith("shell:"):
                if self._provider is None:
                    self._provider = QFileIconProvider()
                fp = self._provider.icon(QFileInfo(path))
                qi = fp if not fp.isNull() else None
            self._cache[path] = qi if qi is not None else self._MISS
        return None if qi is self._MISS else qi


ICONS = IconResolver()


class RowDelegate(QStyledItemDelegate):
    """Custom row painter: real file icons, match highlighting, two-line
    title/subtitle anatomy, right-aligned type chip. Items without ROW_DATA
    (the emoji grid) fall through to default painting."""

    def __init__(self, view):
        super().__init__(view)
        self.f_title = QFont("Segoe UI");   self.f_title.setPixelSize(14)
        self.f_bold = QFont("Segoe UI");    self.f_bold.setPixelSize(14)
        self.f_bold.setWeight(QFont.Weight.DemiBold)
        self.f_sub = QFont("Segoe UI");     self.f_sub.setPixelSize(11)
        self.f_chip = QFont("Segoe UI");    self.f_chip.setPixelSize(10)
        self.f_chip.setWeight(QFont.Weight.DemiBold)
        self.f_chip.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
        self.f_emoji = QFont("Segoe UI Emoji"); self.f_emoji.setPixelSize(18)

    def _icon_for(self, path: str) -> QIcon | None:
        return ICONS.get(path)

    def paint(self, p, opt, index):
        d = index.data(ROW_DATA)
        if d is None:                       # emoji grid cells
            super().paint(p, opt, index)
            return
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = opt.rect.adjusted(2, 2, -2, -2)
        sel = bool(opt.state & QStyle.StateFlag.State_Selected)
        # Selection = subtle white wash (Raycast/cmdk), never an accent fill.
        if sel:
            p.setBrush(QColor(255, 255, 255, 18)); p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(r, 8, 8)
        elif opt.state & QStyle.StateFlag.State_MouseOver:
            p.setBrush(QColor(255, 255, 255, 10)); p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(r, 8, 8)

        x = r.x() + 12
        icon = self._icon_for(d["icon_path"]) if d.get("icon_path") else None
        if icon:
            icon.paint(p, QRect(x, r.y() + (r.height() - 24) // 2, 24, 24))
        else:
            p.setFont(self.f_emoji)
            p.setPen(QColor(255, 255, 255, 230))
            p.drawText(QRect(x - 2, r.y(), 30, r.height()),
                       Qt.AlignmentFlag.AlignCenter, d.get("icon", "•"))
        x += 36

        right = r.right() - 12
        chip = d.get("chip")
        if chip:
            # kbd-chip styling (cmdk): gray5 bg → gray7 when selected, r4.
            p.setFont(self.f_chip)
            cw = QFontMetrics(self.f_chip).horizontalAdvance(chip.upper()) + 16
            crect = QRect(right - cw, r.y() + (r.height() - 20) // 2, cw, 20)
            p.setBrush(QColor(CHIP_BG_SEL if sel else CHIP_BG))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(crect, 4, 4)
            p.setPen(QColor(0xA0, 0xA0, 0xA0))
            p.drawText(crect, Qt.AlignmentFlag.AlignCenter, chip.upper())
            right = crect.x() - 10

        sub = d.get("subtitle") or ""
        title_y = r.y() + 6 if sub else r.y() + (r.height() - 18) // 2
        self._draw_title(p, d["title"], d.get("spans") or [],
                         x, title_y, right - x, sel)
        if sub:
            p.setFont(self.f_sub)
            p.setPen(QColor(0xA0, 0xA0, 0xA0))     # gray11, selected or not
            elided = QFontMetrics(self.f_sub).elidedText(
                sub, Qt.TextElideMode.ElideRight, right - x)
            p.drawText(QRect(x, r.y() + 26, right - x, 14),
                       Qt.AlignmentFlag.AlignVCenter, elided)
        p.restore()

    def _draw_title(self, p, title, spans, x, y, avail, sel):
        """Draw the title as normal/highlighted segments. Highlighted chars =
        accent + semibold (white + semibold when selected)."""
        hi = set()
        for s, ln in spans:
            hi.update(range(s, s + ln))
        fm_n, fm_b = QFontMetrics(self.f_title), QFontMetrics(self.f_bold)
        rect_h = 20
        # Build (text, bold) segments
        segs, cur, cur_b = [], "", False
        for i, ch in enumerate(title):
            b = i in hi
            if b != cur_b and cur:
                segs.append((cur, cur_b)); cur = ""
            cur += ch; cur_b = b
        if cur:
            segs.append((cur, cur_b))
        for text, bold in segs:
            fm = fm_b if bold else fm_n
            w = fm.horizontalAdvance(text)
            if w > avail:       # elide within this segment and stop
                text = fm.elidedText(text, Qt.TextElideMode.ElideRight, avail)
                w = avail
            p.setFont(self.f_bold if bold else self.f_title)
            if bold:
                p.setPen(QColor(0x3b, 0x82, 0xf6).lighter(135))  # match chars
            else:
                p.setPen(QColor(0xED, 0xED, 0xED))               # gray12
            p.drawText(QRect(x, y, avail, rect_h),
                       Qt.AlignmentFlag.AlignVCenter, text)
            x += w
            avail -= w
            if avail <= 0:
                break

    def sizeHint(self, opt, index):
        if index.data(ROW_DATA) is None:
            return super().sizeHint(opt, index)
        return QSize(0, ROW_H)


class PinBar(QWidget):
    """Horizontal strip of pinned items directly under the search box —
    always visible while anything is pinned. Click or Alt+1-9 launches;
    right-click unpins. Pin/unpin the selected result with Ctrl+P."""
    TILE = 44

    def __init__(self, owner):
        super().__init__()
        self._owner = owner
        self._rows: list[dict] = []
        from PyQt6.QtWidgets import QHBoxLayout
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(8)
        self.setFixedHeight(self.TILE)

    def rows(self) -> list[dict]:
        return self._rows

    def refresh(self):
        while self._lay.count():
            w = self._lay.takeAt(0).widget()
            if w:
                w.deleteLater()
        self._rows = providers.pinned_rows()
        from PyQt6.QtWidgets import QToolButton
        for i, r in enumerate(self._rows):
            btn = QToolButton()
            btn.setFixedSize(self.TILE, self.TILE)
            icon = ICONS.get(r["icon_path"]) if r.get("icon_path") else None
            if icon:
                btn.setIcon(icon)
                btn.setIconSize(QSize(28, 28))
            else:
                btn.setText(r.get("icon", "•"))
                f = QFont("Segoe UI Emoji"); f.setPixelSize(20)
                btn.setFont(f)
            btn.setToolTip(f"{r['title']}  (Alt+{i + 1} · right-click unpins)")
            btn.setStyleSheet(
                f"QToolButton {{ background: {L2}; border: 1px solid {L4};"
                f" border-radius: 10px; color: white; }}"
                f"QToolButton:hover {{ background: {L4}; }}")
            btn.clicked.connect(lambda _c, row=r: self._owner._run_pin(row))
            btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            btn.customContextMenuRequested.connect(
                lambda _p, row=r: self._owner._unpin(row))
            self._lay.addWidget(btn)
        self._lay.addStretch(1)
        self.setVisible(bool(self._rows))


class SessionDelegate(QStyledItemDelegate):
    """Two-line session rows for the Claude picker (research spec: 52px,
    dot + 14px title + right-aligned colored status, 12px summary subline,
    wash selection). Also paints skeleton loading rows."""
    ROW = 52

    def __init__(self, view):
        super().__init__(view)
        self.f_title = QFont("Segoe UI"); self.f_title.setPixelSize(14)
        self.f_meta = QFont("Segoe UI");  self.f_meta.setPixelSize(12)

    def paint(self, p, opt, index):
        d = index.data(ROW_DATA) or {}
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = opt.rect.adjusted(4, 2, -4, -2)
        sel = bool(opt.state & QStyle.StateFlag.State_Selected)
        if sel:
            p.setBrush(QColor(255, 255, 255, 18)); p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(r, 8, 8)
        elif opt.state & QStyle.StateFlag.State_MouseOver:
            p.setBrush(QColor(255, 255, 255, 10)); p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(r, 8, 8)

        if d.get("kind") == "skeleton":       # ghost rows while fetching
            p.setBrush(QColor(255, 255, 255, 20)); p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(QRect(r.x() + 34, r.y() + 10, int(r.width() * .45), 12), 6, 6)
            p.drawRoundedRect(QRect(r.x() + 34, r.y() + 30, int(r.width() * .70), 10), 5, 5)
            p.setBrush(QColor(255, 255, 255, 26))
            p.drawEllipse(r.x() + 14, r.y() + (r.height() - 10) // 2, 10, 10)
            p.restore()
            return

        if d.get("kind") == "new":
            p.setFont(self.f_title)
            p.setPen(QColor(0xED, 0xED, 0xED))
            p.drawText(QRect(r.x() + 14, r.y(), r.width() - 28, r.height()),
                       Qt.AlignmentFlag.AlignVCenter, "➕  New session")
            p.restore()
            return

        s = d.get("sess") or {}
        p.setBrush(QColor(s.get("color", "#8b8b8b"))); p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(r.x() + 14, r.y() + 12, 10, 10)
        # right-aligned status in its state color
        p.setFont(self.f_meta)
        st = s.get("status_label", "")
        stw = QFontMetrics(self.f_meta).horizontalAdvance(st)
        p.setPen(QColor(s.get("color", "#8b8b8b")))
        p.drawText(QRect(r.right() - stw - 12, r.y() + 8, stw, 16),
                   Qt.AlignmentFlag.AlignVCenter, st)
        # title
        p.setFont(self.f_title)
        p.setPen(QColor(0xED, 0xED, 0xED))
        tw = r.width() - 34 - stw - 28
        title = QFontMetrics(self.f_title).elidedText(
            s.get("name", "?"), Qt.TextElideMode.ElideRight, tw)
        p.drawText(QRect(r.x() + 34, r.y() + 6, tw, 18),
                   Qt.AlignmentFlag.AlignVCenter, title)
        # summary subline
        summ = s.get("summary", "")
        if summ:
            p.setPen(QColor(0xA0, 0xA0, 0xA0))
            p.setFont(self.f_meta)
            el = QFontMetrics(self.f_meta).elidedText(
                summ, Qt.TextElideMode.ElideRight, r.width() - 34 - 12)
            p.drawText(QRect(r.x() + 34, r.y() + 27, r.width() - 34 - 12, 16),
                       Qt.AlignmentFlag.AlignVCenter, el)
        p.restore()

    def sizeHint(self, opt, index):
        return QSize(0, self.ROW)


class SessionPopup(QWidget):
    """The Claude session picker: ARIA-combobox keyboard model (type filters,
    ↑/↓ clamp, Home/End, Enter selects, Esc closes without selecting), first
    row pre-selected, skeleton rows on cold fetch (≥400ms once shown),
    below-start-aligned +4px, 150ms fade-in."""
    WIDTH = 440
    MAX_VISIBLE = 6.5

    def __init__(self, owner):
        super().__init__(owner, Qt.WindowType.Popup
                         | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setObjectName("sesspop")
        # Panel bg is painted in paintEvent — QSS backgrounds on translucent
        # Qt.Popup toplevels silently fail to paint (the see-through bug).
        self.setStyleSheet(
            f"QLabel#filterhdr {{ color: {TXT_SECONDARY}; font-family: 'Segoe UI';"
            f" font-size: 12px; padding: 6px 18px 0 18px; }}"
            f"QListWidget {{ background: transparent; border: none; outline: none; }}")
        self._owner = owner
        self._all: list[dict] = []
        self._filter = ""
        self._skeleton_t0 = 0.0
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)   # keys must reach us
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(0)
        self.hdr = QLabel(objectName="filterhdr")
        self.hdr.setFixedHeight(24)
        lay.addWidget(self.hdr)
        self.lst = QListWidget()
        self.lst.setItemDelegate(SessionDelegate(self.lst))
        self.lst.setMouseTracking(True)
        self.lst.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.lst.itemClicked.connect(lambda _i: self._commit())
        lay.addWidget(self.lst)
        self.setFixedWidth(self.WIDTH)

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(0x1C, 0x1C, 0x1C))
        pen = QPen(QColor(255, 255, 255, 36))
        pen.setWidth(1)
        p.setPen(pen)
        p.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 12, 12)

    # ── open / fill ───────────────────────────────────────────────────────────

    def open_at(self, global_pos):
        fresh = providers.sessions.cached()
        if fresh is not None:
            self._all = fresh
            self._rebuild()
        else:
            self._skeleton_t0 = time.monotonic()
            self._show_skeleton()
            threading.Thread(target=lambda: self._owner._sessions_ready.emit(
                providers.sessions.list()), daemon=True).start()
        self.move(global_pos.x(), global_pos.y() + 4)
        self.show()
        self.setFocus()          # plain-widget popups don't grab focus alone
        try:
            from PyQt6.QtCore import QEasingCurve, QPropertyAnimation
            self._anim = QPropertyAnimation(self, b"windowOpacity")
            self._anim.setDuration(150)
            self._anim.setStartValue(0.0); self._anim.setEndValue(1.0)
            self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._anim.start()
        except Exception:
            self.setWindowOpacity(1.0)

    def _add_item(self, data: dict, selectable: bool = True):
        it = QListWidgetItem()
        it.setData(ROW_DATA, data)
        if not selectable:
            it.setFlags(Qt.ItemFlag.NoItemFlags)
        it.setSizeHint(QSize(0, SessionDelegate.ROW))
        self.lst.addItem(it)
        return it

    def _show_skeleton(self):
        self.lst.clear()
        self._add_item({"kind": "new"})
        for _ in range(3):
            self._add_item({"kind": "skeleton"}, selectable=False)
        self.lst.setCurrentRow(0)
        self.hdr.setText("🔎  Type to search sessions by content…")
        self._fit()

    def fill(self, sessions: list):
        """Called via the palette's _sessions_ready signal."""
        if not self.isVisible():
            return
        self._all = sessions
        # Anti-flash: once the skeleton is up, hold it ≥400ms.
        held = time.monotonic() - self._skeleton_t0
        if self._skeleton_t0 and held < 0.4:
            QTimer.singleShot(int((0.4 - held) * 1000), self._rebuild)
        else:
            self._rebuild()

    def _rebuild(self):
        self.lst.clear()
        self._add_item({"kind": "new"})
        # Contextual search (ClaudeSearchSessions recipe): BM25 over each
        # session's title + transcript content, best match first.
        shown = (providers.sessions.rank(self._filter, self._all)
                 if self._filter else self._all)
        for s in shown:
            it = self._add_item({"kind": "sess", "sess": s})
            it.setToolTip(s.get("summary") or "(no recent output)")
        if self._all and not shown:
            self._add_item({"kind": "sess", "sess": {
                "name": "No sessions match",
                "summary": "Backspace to clear the filter",
                "color": "#8b8b8b", "status_label": ""}}, selectable=False)
        self.lst.setCurrentRow(0)          # sticky default: first row
        self.hdr.setText(f"🔎  {self._filter}▏" if self._filter
                         else "🔎  Type to search sessions by content…")
        self._fit()

    def _fit(self):
        n = self.lst.count()
        vis = min(n, self.MAX_VISIBLE)     # half-row clip = scroll affordance
        list_h = int(vis * SessionDelegate.ROW)
        self.setFixedHeight(list_h + 12 + 24)      # + padding + hint header
        self.lst.setFixedHeight(list_h)

    # ── keyboard (ARIA combobox contract) ─────────────────────────────────────

    def keyPressEvent(self, ev):
        key = ev.key()
        n = self.lst.count()
        cur = self.lst.currentRow()
        if key == Qt.Key.Key_Escape:
            self.close()                                   # never commits
            self._owner.query.setFocus()
        elif key == Qt.Key.Key_Down:
            self.lst.setCurrentRow(min(cur + 1, n - 1))    # clamp, no wrap
        elif key == Qt.Key.Key_Up:
            self.lst.setCurrentRow(max(cur - 1, 0))
        elif key == Qt.Key.Key_Home:
            self.lst.setCurrentRow(0)
        elif key == Qt.Key.Key_End:
            self.lst.setCurrentRow(n - 1)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._commit()
        elif key == Qt.Key.Key_Backspace:
            self._filter = self._filter[:-1]
            self._rebuild()
        elif ev.text() and ev.text().isprintable():
            self._filter += ev.text()                      # type-to-filter
            self._rebuild()
        else:
            super().keyPressEvent(ev)

    def _commit(self):
        it = self.lst.currentItem()
        d = it.data(ROW_DATA) if it else None
        if not d or (d.get("kind") == "sess" and not d["sess"].get("sessionId")):
            return
        self.close()
        self._owner._session_chosen(
            None if d.get("kind") == "new" else d["sess"])


class ModePills(QWidget):
    """Clickable command pills on the EMPTY palette — click inserts the
    command prefix into the search box. Order adapts to usage (decayed),
    so your most-used modes drift to the front."""
    PILL_H = 26
    AVAIL_W = 640 - 48              # card inner width

    def __init__(self, owner):
        super().__init__()
        self._owner = owner
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(6)
        self._font = QFont("Segoe UI")
        self._font.setPixelSize(12)

    def refresh(self):
        while self._lay.count():
            item = self._lay.takeAt(0)
            if item.layout():
                while item.layout().count():
                    w = item.layout().takeAt(0).widget()
                    if w:
                        w.deleteLater()
            elif item.widget():
                item.widget().deleteLater()
        from PyQt6.QtWidgets import QHBoxLayout, QPushButton
        fm = QFontMetrics(self._font)
        row, row_w = None, 0
        for m in providers.ordered_modes():
            label = f"{m['icon']} {m['label']}"
            w = fm.horizontalAdvance(label) + 26
            if row is None or row_w + w + 6 > self.AVAIL_W:
                row = QHBoxLayout()
                row.setSpacing(6)
                row.addStretch(1)   # placeholder; re-stretched below
                self._lay.addLayout(row)
                row.takeAt(0)
                row_w = 0
            btn = QPushButton(label)
            btn.setFont(self._font)
            btn.setFixedHeight(self.PILL_H)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            if m.get("tip"):
                btn.setToolTip(m["tip"])
            btn.setStyleSheet(
                f"QPushButton {{ background: {L2}; border: 1px solid {L4};"
                f" border-radius: 13px; color: rgba(255,255,255,0.72);"
                f" padding: 0 12px; }}"
                f"QPushButton:hover {{ background: {L4}; color: white; }}")
            btn.clicked.connect(lambda _c, mode=m: self._owner._use_pill(mode))
            row.addWidget(btn)
            row_w += w + 6
        for i in range(self._lay.count()):
            if self._lay.itemAt(i).layout():
                self._lay.itemAt(i).layout().addStretch(1)


class Palette(QWidget):
    WIDTH = 640
    _files_ready = pyqtSignal(str, list)   # (query, rows) from the worker thread
    _sessions_ready = pyqtSignal(list)     # live sessions for the Claude menu

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("root")
        self.setStyleSheet(STYLESHEET)
        self._acrylic = False       # set by main() after the handle exists

        # Raycast anatomy: boxless input on the glass, hairline, content,
        # hairline, footer. The window IS the card (DWM rounds + shadows it).
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.DEFAULT_PLACEHOLDER = "Search apps, files, notes…"
        self.query = QLineEdit(placeholderText=self.DEFAULT_PLACEHOLDER)
        pal = self.query.palette()
        pal.setColor(pal.ColorRole.PlaceholderText, QColor(TXT_PLACEHOLDER))
        self.query.setPalette(pal)
        self.query.textChanged.connect(self._on_query)
        self.query.installEventFilter(self)
        outer.addWidget(self.query)

        self.hr1 = QFrame(objectName="hairline")
        outer.addWidget(self.hr1)

        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)
        outer.addWidget(content)
        self._card_lay = lay        # _resize_to_fit must invalidate THIS one

        # Active-mode chip: the pill badge rendered INSIDE the search field
        # (Slack-style scoped-search token). Same look as the pills below;
        # the trailing ✕ (clicking the chip) cancels the mode.
        self._active_mode: dict | None = None
        self.mode_chip = QLabel(self.query)
        self.mode_chip.setStyleSheet(
            f"QLabel {{ background: {L2}; border: 1px solid {L4};"
            f" border-radius: 11px; color: rgba(255,255,255,0.72);"
            f" padding: 0 10px; font-family: 'Segoe UI'; font-size: 12px; }}"
            f"QLabel:hover {{ background: {L4}; color: white; }}")
        self.mode_chip.setFixedHeight(22)
        self.mode_chip.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mode_chip.setToolTip("Remove (or Backspace)")
        self.mode_chip.mousePressEvent = self._chip_clicked
        self.mode_chip.hide()

        self.pins = PinBar(self)               # pinned tiles — Ctrl+P to pin
        self.pins.setVisible(False)
        lay.addWidget(self.pins)

        self.mode_pills = ModePills(self)      # command pills (empty state)
        self.mode_pills.setVisible(False)
        lay.addWidget(self.mode_pills)

        self.results = QListWidget()
        self.results.setVisible(False)
        self.results.setFocusPolicy(Qt.FocusPolicy.NoFocus)   # focus stays in the box
        self.results.setMouseTracking(True)                   # hover state
        self.results.setItemDelegate(RowDelegate(self.results))
        self.results.itemActivated.connect(lambda _i: self._execute())
        self.results.itemClicked.connect(lambda _i: self._execute())
        self.results.currentRowChanged.connect(self._update_caption)
        lay.addWidget(self.results)

        # Footer (cmdk: 40px bar behind a 1px hairline, secondary text).
        self.hr2 = QFrame(objectName="hairline")
        self.hr2.setVisible(False)
        outer.addWidget(self.hr2)
        self.caption = QLabel(objectName="caption")           # footer hint bar
        self.caption.setVisible(False)
        self.caption.setFixedHeight(36)
        outer.addWidget(self.caption)
        self._results_h = 0     # the height we last assigned to the list

        self._emoji_font = QFont("Segoe UI Emoji", 22)
        self.setFixedWidth(self.WIDTH)             # DWM shadows the window
        self._rows: list[dict] = []
        self._emoji_mode = False
        self._pending: dict | None = None          # confirm-flow state
        self._prev_hwnd: int = 0                   # focus-restore target
        self._latencies: list[float] = []          # keystroke→render ms

        # File search runs async: 150ms debounce → worker thread hits the
        # Windows Search index → results merge in when they arrive.
        self._files_ready.connect(self._merge_files)
        self._file_timer = QTimer(singleShot=True)
        self._file_timer.timeout.connect(self._fire_file_search)
        self._sessions_ready.connect(self._fill_session_menu)

        # Clipboard history: event-driven capture, zero polling. winId()
        # forces native window creation so the listener has an HWND.
        try:
            user32.AddClipboardFormatListener(int(self.winId()))
        except Exception:
            pass

    # ── Show / hide ───────────────────────────────────────────────────────────

    def summon(self):
        """Toggle: hotkey while visible dismisses (restoring focus); otherwise
        show on the active monitor, focused, with the zero-query state."""
        if self.isVisible():
            self._dismiss(restore=True)
            return
        # Capture the window the user came from — restored on dismissal
        # (W3C ARIA modal requirement; RESEARCH.md finding 10).
        try:
            self._prev_hwnd = user32.GetForegroundWindow()
        except Exception:
            self._prev_hwnd = 0
        providers.on_summon()                      # hot-reload actions.yaml
        self.pins.refresh()
        self.mode_pills.refresh()                  # re-sorts by usage
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        self._pending = None
        self._active_mode = None                   # fresh state, no mode chip
        providers.ask_claude.target = None
        self.mode_chip.hide()
        self.query.setTextMargins(0, 0, 0, 0)
        self.query.setPlaceholderText(self.DEFAULT_PLACEHOLDER)
        self.query.blockSignals(True)              # avoid double _on_query
        self.query.clear()
        self.query.blockSignals(False)
        self._on_query("")                         # zero-query frecency state
        self.move(geo.x() + (geo.width() - self.width()) // 2,
                  geo.y() + int(geo.height() * 0.24))
        self.show()
        self.raise_()
        self.activateWindow()
        self._force_foreground()
        self.query.setFocus()
        self._animate_open()

    def _animate_open(self):
        """cmdk's open feel: 140ms fade + 8px rise, OutCubic."""
        try:
            from PyQt6.QtCore import (QEasingCurve, QParallelAnimationGroup,
                                      QPoint, QPropertyAnimation)
            end = self.pos()
            fade = QPropertyAnimation(self, b"windowOpacity")
            fade.setDuration(140); fade.setStartValue(0.0); fade.setEndValue(1.0)
            fade.setEasingCurve(QEasingCurve.Type.OutCubic)
            rise = QPropertyAnimation(self, b"pos")
            rise.setDuration(140)
            rise.setStartValue(end + QPoint(0, 8)); rise.setEndValue(end)
            rise.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._open_anim = QParallelAnimationGroup(self)
            self._open_anim.addAnimation(fade)
            self._open_anim.addAnimation(rise)
            self._open_anim.start()
        except Exception:
            self.setWindowOpacity(1.0)

    def _dismiss(self, restore: bool):
        """Hide; optionally hand focus back to the pre-summon window. Not used
        for focus-loss hides (the user already focused something else) or app
        launches (the launched app should win focus)."""
        self.hide()
        self._flush_latency()
        if restore and self._prev_hwnd:
            try:
                user32.SetForegroundWindow(self._prev_hwnd)
            except Exception:
                pass

    def _flush_latency(self):
        """p50/p95 keystroke→render, appended per palette session. The 100ms
        budget is a ceiling (RESEARCH.md finding 6) — check with:
        Get-Content cache\\latency.log -Tail 5"""
        if len(self._latencies) < 5:
            self._latencies.clear()
            return
        s = sorted(self._latencies)
        p50, p95 = s[len(s) // 2], s[int(len(s) * 0.95)]
        try:
            with open(CACHE_DIR / "latency.log", "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M')} n={len(s)} "
                        f"p50={p50:.1f}ms p95={p95:.1f}ms max={s[-1]:.1f}ms\n")
        except Exception:
            pass
        self._latencies.clear()

    def _force_foreground(self):
        """SetForegroundWindow with the Alt-tap fallback. The stub already
        called AllowSetForegroundWindow(our pid) on the warm path; the Alt
        tap covers the cold-spawn path where that grant doesn't exist."""
        try:
            hwnd = int(self.winId())
            if user32.GetForegroundWindow() != hwnd:
                user32.keybd_event(0x12, 0, 0, 0)       # Alt down
                user32.keybd_event(0x12, 0, 0x0002, 0)  # Alt up
                user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    # ── Keyboard ──────────────────────────────────────────────────────────────

    def eventFilter(self, obj, ev):
        if obj is self.query and ev.type() == QEvent.Type.KeyPress:
            key, mods = ev.key(), ev.modifiers()
            if key == Qt.Key.Key_Escape:
                self._dismiss(restore=True)
                return True
            if key == Qt.Key.Key_Backspace and self._active_mode \
                    and not self.query.text():
                self._clear_mode()          # pop the mode chip out
                return True
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                step = GRID_COLS if self._emoji_mode else 1
                self._move_selection(step if key == Qt.Key.Key_Down else -step)
                return True
            if self._emoji_mode and key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
                # In the emoji grid, ←/→ walk cells (queries are short —
                # retyping beats losing 2D navigation).
                self._move_selection(1 if key == Qt.Key.Key_Right else -1)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
                idx = self.results.currentRow()
                cur = self._rows[idx] if 0 <= idx < len(self._rows) else None
                if ctrl and cur and not self._pending:
                    if cur.get("kind") == "clip":
                        self._paste_clip(cur)      # paste into previous window
                        return True
                    if not self._emoji_mode and cur.get("alt_copy_text"):
                        QApplication.clipboard().setText(cur["alt_copy_text"])
                        providers.record_selection(cur, self.query.text())
                        self._dismiss(restore=True)
                        return True
                    if not self._emoji_mode and callable(cur.get("alt_run")):
                        providers.record_selection(cur, self.query.text())
                        try:
                            cur["alt_run"]()
                        except Exception:
                            _log_exc(f"alt action failed: {cur['title']}")
                        self._dismiss(restore=False)
                        return True
                # Ctrl+Enter in emoji mode: copy but stay open (multi-pick).
                self._execute(stay=ctrl and self._emoji_mode)
                return True
            if mods & Qt.KeyboardModifier.ControlModifier \
                    and Qt.Key.Key_1 <= key <= Qt.Key.Key_9:
                idx = key - Qt.Key.Key_1
                if idx < len(self._rows):
                    self.results.setCurrentRow(idx)
                    self._execute()
                return True
            if mods & Qt.KeyboardModifier.AltModifier \
                    and Qt.Key.Key_1 <= key <= Qt.Key.Key_9:
                idx = key - Qt.Key.Key_1          # Alt+N launches pin N
                if idx < len(self.pins.rows()):
                    self._run_pin(self.pins.rows()[idx])
                return True
            if mods & Qt.KeyboardModifier.ControlModifier \
                    and key == Qt.Key.Key_P:
                self._toggle_pin()                # Ctrl+P pins the selection
                return True
        return super().eventFilter(obj, ev)

    def _effective_query(self) -> str:
        """What the pipeline sees: active mode chip + typed text."""
        typed = self.query.text()
        if self._active_mode:
            return providers.mode_effective_query(self._active_mode, typed)
        return typed

    def _chip_clicked(self, ev):
        """Chip anatomy: [icon label (▾)] [✕]. In Claude mode the body/chevron
        opens the live-sessions dropdown; the trailing ✕ always cancels."""
        in_x_zone = ev.position().x() > self.mode_chip.width() - 26
        if (self._active_mode and self._active_mode["id"] == "ask"
                and not in_x_zone):
            # Defer past this click — showing a Qt.Popup mid-press lets the
            # matching release land "outside" it and close it instantly.
            QTimer.singleShot(0, self._show_session_menu)
            return
        self._clear_mode()

    def _chip_text(self) -> str:
        m = self._active_mode
        if not m:
            return ""
        if m["id"] == "ask":
            t = providers.ask_claude.target
            core = f"🧠 → {t['name']}" if t else f"{m['icon']} {m['label']}"
            return f"{core} ▾   ✕"
        return f"{m['icon']} {m['label']}   ✕"

    @staticmethod
    def _state_dot(color: str) -> QIcon:
        pm = QPixmap(12, 12)
        pm.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(1, 1, 10, 10)
        painter.end()
        return QIcon(pm)

    def _show_session_menu(self):
        """Chevron → the SessionPopup picker (ARIA-combobox keyboard model,
        type-to-filter, skeleton loading)."""
        self._session_popup = SessionPopup(self)
        self._session_popup.open_at(self.mode_chip.mapToGlobal(
            self.mode_chip.rect().bottomLeft()))

    def _fill_session_menu(self, sess: list):
        pop = getattr(self, "_session_popup", None)
        if pop is not None:
            pop.fill(sess)

    def _session_chosen(self, session: dict | None):
        providers.ask_claude.target = session             # None = new session
        self.mode_chip.setText(self._chip_text())
        self.mode_chip.adjustSize()
        self.query.setTextMargins(self.mode_chip.width() + 10, 0, 0, 0)
        self.query.setFocus()
        self._on_query(self.query.text())             # refresh the Claude row

    def _use_pill(self, mode: dict):
        """Pill click: embed the badge in the field; typing goes after it."""
        providers.usage.record_mode(mode["id"])
        self._active_mode = mode
        providers.ask_claude.target = None
        self.mode_chip.setText(self._chip_text())
        self.mode_chip.adjustSize()
        self.mode_chip.move(12, (self.query.height() - self.mode_chip.height()) // 2)
        self.mode_chip.show()
        self.query.setTextMargins(self.mode_chip.width() + 10, 0, 0, 0)
        self.query.setPlaceholderText(mode.get("hint", ""))
        self.query.blockSignals(True)
        self.query.clear()
        self.query.blockSignals(False)
        self.query.setFocus()
        self._on_query("")                 # render the mode's results now

    def _clear_mode(self):
        self._active_mode = None
        providers.ask_claude.target = None
        self.mode_chip.hide()
        self.query.setTextMargins(0, 0, 0, 0)
        self.query.setPlaceholderText(self.DEFAULT_PLACEHOLDER)
        self._on_query(self.query.text())

    def _run_pin(self, row: dict):
        providers.record_selection(row, "")
        if callable(row.get("run")):
            try:
                row["run"]()
            except Exception:
                _log_exc(f"pin failed: {row['title']}")
        self._dismiss(restore=False)

    def _unpin(self, row: dict):
        if row.get("key"):
            providers.pins.toggle(row["key"])     # was pinned → removes
            self.pins.refresh()
            self._resize_to_fit(self.results.isVisible())

    def _toggle_pin(self):
        idx = self.results.currentRow()
        if not (0 <= idx < len(self._rows)):
            return
        row = self._rows[idx]
        if not row.get("key"):
            self.caption.setText("This row can't be pinned")
            return
        was = providers.pins.is_pinned(row["key"])
        now = providers.pins.toggle(row["key"])
        self.pins.refresh()
        self._resize_to_fit(self.results.isVisible())
        if was:
            self.caption.setText(f"Unpinned: {row['title']}")
        elif now:
            self.caption.setText(f"📌 Pinned: {row['title']}  (Alt+{len(self.pins.rows())})")
        else:
            self.caption.setText("Pin bar is full (9) — right-click a tile to unpin")

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Escape:
            self._dismiss(restore=True)
        else:
            super().keyPressEvent(ev)

    def changeEvent(self, ev):
        # Focus-loss hides the palette (click elsewhere, Alt-Tab away).
        if ev.type() == QEvent.Type.ActivationChange and self.isVisible() \
                and not self.isActiveWindow():
            self.hide()
        super().changeEvent(ev)

    WM_CLIPBOARDUPDATE = 0x031D

    def nativeEvent(self, eventType, message):
        # Event-driven clipboard capture — fires only when something is
        # actually copied anywhere in Windows; costs nothing at idle.
        # NOTE: this override must be bulletproof — ANY exception escaping a
        # native callback (including via super()'s sip wrapper) kills the
        # process with 0xC000041D. Return (False, 0) = "not handled" always.
        try:
            if bytes(eventType) == b"windows_generic_MSG":
                msg = ctypes.wintypes.MSG.from_address(int(message))
                if msg.message == self.WM_CLIPBOARDUPDATE:
                    QTimer.singleShot(0, self._capture_clip)
        except Exception:
            pass
        return False, 0

    def _capture_clip(self):
        try:
            providers.clip.record(QApplication.clipboard().text())
        except Exception:
            pass

    def _move_selection(self, delta: int):
        n = self.results.count()
        if n:
            self.results.setCurrentRow((self.results.currentRow() + delta) % n)

    def _update_caption(self, idx: int):
        """Footer hint bar: what Enter / Ctrl+Enter do for the selected row."""
        if not (0 <= idx < len(self._rows)):
            return
        r = self._rows[idx]
        if self._emoji_mode:
            self.caption.setText(
                f"{r['icon']}  {r['title']}    ·    ↵ Copy · "
                f"Ctrl+↵ Copy & stay open")
            return
        if self._pending:
            self.caption.setText("↵ Confirm    ·    Esc Cancel")
            return
        if r.get("kind") == "clip":
            hint = "↵ Copy · Ctrl+↵ Paste into previous window"
        elif r.get("alt_copy_text"):
            hint = f"↵ Open · Ctrl+↵ Copy {r['alt_copy_text'][:30]}"
        elif r.get("alt_run") and r.get("chip") == "File":
            hint = "↵ Open · Ctrl+↵ Show in folder"
        elif r.get("alt_run"):
            hint = "↵ Launch · Ctrl+↵ Open install folder"
        elif r.get("copy_text"):
            hint = "↵ Copy"
        elif r.get("set_query") is not None:
            hint = "↵ Fill query"
        elif r.get("confirm"):
            hint = "↵ Confirm first, then run"
        else:
            hint = "↵ Run"
        if r.get("key"):
            hint += " · Ctrl+P " + ("unpin" if providers.pins.is_pinned(r["key"]) else "pin")
        self.caption.setText(hint + "    ·    Ctrl+1-9 jump · Esc close")

    # ── Query pipeline ────────────────────────────────────────────────────────

    def _on_query(self, _text: str):
        t0 = time.perf_counter()
        self._pending = None
        eff = self._effective_query()
        # Pills only on the truly empty state (no text, no active mode chip).
        self.mode_pills.setVisible(not eff.strip())
        try:
            rows = providers.search(eff)
        except Exception:
            _log_exc("providers.search failed")
            rows = []
        self._render(rows)
        if eff.strip():
            self._latencies.append((time.perf_counter() - t0) * 1000)
            if len(self._latencies) > 500:
                del self._latencies[:250]
        # Only plain queries get the async file pass — prefixed modes
        # (>, :, ?, ., mem:, kill) are complete already.
        t = eff.strip()
        if len(t) >= 3 and not t.startswith((">", ":", "?", ".")) \
                and not t.lower().startswith(("kill ", "mem:")):
            self._file_timer.start(150)
        else:
            self._file_timer.stop()

    def _fire_file_search(self):
        q = self._effective_query().strip()

        def worker():
            try:
                rows = providers.file_search(q)
            except Exception:
                _log_exc("file_search failed")
                rows = []
            self._files_ready.emit(q, rows)

        threading.Thread(target=worker, daemon=True).start()

    def _merge_files(self, q: str, rows: list):
        if q != self._effective_query().strip() or self._emoji_mode or self._pending:
            return                      # stale result — query moved on
        seen = {(r["title"], r["subtitle"]) for r in self._rows}
        fresh = [r for r in rows if (r["title"], r["subtitle"]) not in seen]
        if not fresh:
            return
        keep = self.results.currentRow()   # don't yank the user's selection
        self._render((self._rows + fresh)[:providers.MAX_ROWS])
        if 0 <= keep < len(self._rows):
            self.results.setCurrentRow(keep)

    def _render(self, rows: list[dict]):
        self._rows = rows
        emoji_mode = bool(rows) and all(r.get("kind") == "emoji" for r in rows)
        if emoji_mode != self._emoji_mode:
            self._emoji_mode = emoji_mode
            if emoji_mode:                          # grid of big glyphs
                self.results.setViewMode(QListView.ViewMode.IconMode)
                self.results.setMovement(QListView.Movement.Static)
                self.results.setResizeMode(QListView.ResizeMode.Adjust)
                self.results.setUniformItemSizes(True)
                self.results.setWordWrap(False)
                self.results.setGridSize(QSize(GRID_W, GRID_H))
                self.results.setStyleSheet(GRID_ITEM_QSS)
            else:                                   # back to the row list
                self.results.setViewMode(QListView.ViewMode.ListMode)
                self.results.setGridSize(QSize())
                self.results.setStyleSheet("")
        # Highlight spans only make sense for plain queries.
        q = self._effective_query().strip()
        low = q.lower()
        plain = q and not q.startswith((">", ":", "?")) \
            and not low.startswith(("kill ", "mem:", "clip", "w "))
        self.results.clear()
        for r in rows:
            if emoji_mode:
                item = QListWidgetItem(r["icon"])
                item.setFont(self._emoji_font)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                item.setToolTip(r["title"])
                item.setSizeHint(QSize(GRID_W - 4, GRID_H - 4))
            else:
                item = QListWidgetItem(r["title"])   # text for a11y; delegate paints
                item.setData(ROW_DATA, {
                    "title": r["title"], "subtitle": r.get("subtitle", ""),
                    "icon": r.get("icon", "•"),
                    "icon_path": r.get("icon_path"),
                    "chip": r.get("chip"),
                    "spans": _match_spans(r["title"], q) if plain else [],
                })
                item.setSizeHint(QSize(0, ROW_H))
            self.results.addItem(item)
        visible = bool(rows)
        self.results.setVisible(visible)
        self.caption.setVisible(visible)
        self.hr2.setVisible(visible)
        if visible:
            if emoji_mode:
                grid_rows = -(-len(rows) // GRID_COLS)      # ceil
                self._results_h = grid_rows * GRID_H + 8
            elif len(rows) > 9:
                # Deliberately clip the last visible row — the half-row
                # signals "more below" (NN/g scroll-affordance research).
                self._results_h = int(9.5 * ROW_H) + 8
            else:
                self._results_h = len(rows) * ROW_H + 8
            self.results.setFixedHeight(self._results_h)
            self.results.setCurrentRow(0)
        self._resize_to_fit(visible)

    def _resize_to_fit(self, _results_visible: bool = False):
        """Deterministic window height. Every child is fixed-height, so the
        layout's minimum size IS the exact required height — but only after a
        forced synchronous recalc (invalidate+activate). Plain adjustSize()
        reads a stale layout right after visibility changes (the giant-empty-
        card bug), and hand-summed constants drift from Qt's real metrics
        (borders, platform padding — the clipped-caption bug)."""
        # Invalidate INNER first — the card layout owns query/pins/results/
        # caption, and dirtying the outer layout alone leaves the inner one
        # serving cached (stale) geometry, which is exactly the overlap bug.
        for lay in (self._card_lay, self.layout()):
            lay.invalidate()
            lay.activate()
        outer = self.layout()
        self.setFixedHeight(max(outer.totalSizeHint().height(),
                                outer.totalMinimumSize().height()))

    def _paste_clip(self, row: dict):
        """Copy the clip, restore focus to the previous window, then send
        Ctrl+V — the closest thing to 'paste into where I was'."""
        QApplication.clipboard().setText(row["copy_text"])
        self._dismiss(restore=True)

        def send_ctrl_v():
            user32.keybd_event(0x11, 0, 0, 0)          # Ctrl down
            user32.keybd_event(0x56, 0, 0, 0)          # V down
            user32.keybd_event(0x56, 0, 0x0002, 0)     # V up
            user32.keybd_event(0x11, 0, 0x0002, 0)     # Ctrl up

        QTimer.singleShot(200, send_ctrl_v)            # let focus settle first

    def _execute(self, stay: bool = False):
        idx = self.results.currentRow()
        if not (0 <= idx < len(self._rows)):
            return
        row = self._pending if self._pending else self._rows[idx]

        if row.get("app_quit"):
            QApplication.quit()                        # stub respawns on next tap
            return
        if row.get("set_query") is not None and not self._pending:
            self.query.setText(row["set_query"])
            return
        if row.get("confirm") and not self._pending:
            self._pending = dict(row, confirm=False)
            self._render([_confirm_row(row)])
            return

        self._pending = None
        eff = self._effective_query()
        providers.record_selection(row, eff)                  # learned ranking
        mode = providers.mode_of_query(eff.strip())
        if mode:
            providers.usage.record_mode(mode)                 # pill ordering
        if row.get("copy_text"):
            QApplication.clipboard().setText(row["copy_text"])
            if row.get("kind") == "emoji":
                providers.emojis.record_use(row["copy_text"])
        launched = False
        if callable(row.get("run")):
            try:
                row["run"]()
                launched = True
            except Exception:
                _log_exc(f"action failed: {row['title']}")
        if not stay:
            # Copies restore focus to where the user was; launches don't —
            # the launched app should win the foreground.
            self._dismiss(restore=not launched)


def _confirm_row(row: dict) -> dict:
    return {"title": f"Press Enter to confirm: {row['title']}",
            "subtitle": "Esc cancels", "icon": "⚠️",
            "run": row.get("run"), "confirm": False,
            "set_query": None, "copy_text": row.get("copy_text")}


def _make_tray_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(ACCENT))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(4, 4, 56, 56, 14, 14)
    p.setPen(QColor(255, 255, 255))
    p.setFont(QFont("Segoe UI Emoji", 28))
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "⚡")
    p.end()
    return QIcon(pm)


def main():
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Flint.App")
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)   # hide ≠ quit; we stay warm
    # Dark tooltips app-wide (Qt default is light yellow — jarring here).
    app.setStyleSheet(
        f"QToolTip {{ background: {BG_SOLID}; color: rgba(255,255,255,0.87);"
        f" border: 1px solid {L4}; border-radius: 6px; padding: 6px 10px;"
        f" font-family: 'Segoe UI'; font-size: 12px; }}"
        f"QMenu {{ background: {BG_SOLID}; color: {TXT_PRIMARY};"
        f" border: 1px solid {HAIRLINE}; border-radius: 8px; padding: 6px;"
        f" font-family: 'Segoe UI'; font-size: 13px; }}"
        f"QMenu::item {{ padding: 7px 14px; border-radius: 6px; }}"
        f"QMenu::item:selected {{ background: {SEL_WASH}; }}"
        f"QMenu::item:disabled {{ color: {TXT_PLACEHOLDER}; }}"
        f"QMenu::separator {{ height: 1px; background: {HAIRLINE};"
        f" margin: 5px 8px; }}")

    providers.init()                       # cache load now, rebuild backgrounded

    palette = Palette()

    # Windows 11 acrylic glass (research: DWM attrs 20/33/38 + extended frame,
    # non-zero-alpha tint mandatory). Falls back to solid #1C1C1C + QSS radius.
    palette.winId()                        # force the native handle
    palette._acrylic = apply_acrylic(palette)
    if palette._acrylic:
        palette.setStyleSheet(STYLESHEET + f"""
            QWidget#root {{ background: {BG_ACRYLIC}; }}""")
    else:
        palette.setStyleSheet(STYLESHEET + f"""
            QWidget#root {{ background: {BG_SOLID}; border-radius: 12px;
                            border: 1px solid {HAIRLINE}; }}""")

    # Tray icon — visibility that the worker is warm, plus escape hatches.
    tray = QSystemTrayIcon(_make_tray_icon(), app)
    tray.setToolTip("Anti-Raycast — tap Win to summon")
    menu = QMenu()
    menu.addAction("Show Anti-Raycast", palette.summon)
    menu.addAction("Refresh index", lambda: __import__("threading").Thread(
        target=providers._rebuild_all, daemon=True).start())
    menu.addSeparator()
    menu.addAction("Quit palette (Win tap respawns)", app.quit)
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: palette.summon()
        if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
    tray.show()

    # Show immediately: the only way we get spawned is a hotkey press.
    QTimer.singleShot(0, palette.summon)

    # 100ms show-event poll. Auto-reset
    # event: each WAIT_OBJECT_0 is exactly one summon request from the stub.
    h_event = kernel32.CreateEventW(None, False, False, SHOW_EVENT_NAME)

    def _poll():
        try:
            if h_event and kernel32.WaitForSingleObject(h_event, 0) == 0:
                palette.summon()
        except Exception:
            pass

    timer = QTimer()
    timer.timeout.connect(_poll)
    timer.start(100)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
