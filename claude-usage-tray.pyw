#!/usr/bin/env pythonw
"""Claude session/weekly limit usage in the Windows notification area.

The Windows counterpart to claude-usage.10s.py (macOS/SwiftBar). The data
layer is the same: same endpoint, same throttle, same backoff, same cache
shape. Three things had to change, and they are the only real differences:

  credentials  macOS keeps the OAuth blob in the login keychain; Windows
               Claude Code writes the identical structure in plaintext to
               ~/.claude/.credentials.json. Read fresh on every poll, because
               Claude Code refreshes the token in place.

  the host     There is no SwiftBar, and no text in the Win11 taskbar -- the
               deskband API that used to allow it was removed. So the figures
               are drawn *into* the tray icon as pixels, two rows of digits,
               tinted by severity. The full breakdown lives in the hover
               tooltip and the right-click menu.

  at login     A LaunchAgent becomes an HKCU\\...\\Run value. No file to
               install, no console flash, and trivially inspectable.

Everything is stdlib. The tray icon, its bitmap, and the popup menu go
through ctypes/Win32 because tkinter has no notification-area API.

Run it with pythonw.exe (or just double-click, given the .pyw extension) so
no console window appears.
"""

import ctypes
import ctypes.wintypes as w
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import winreg
from datetime import datetime, timezone

APP_NAME = "Claude Usage"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
SETTINGS_URL = "https://claude.ai/settings/usage"
TIMEOUT = 10

SELF = os.path.abspath(__file__)
CREDENTIALS_PATH = os.path.join(
    os.path.expanduser("~"), ".claude", ".credentials.json"
)

# %LOCALAPPDATA% rather than the Mac build's ~/.config: it is the Windows
# convention for machine-local, non-roaming state, and this cache is
# machine-specific (it tracks one machine's poll cadence and backoff).
STATE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "claude-usage-tray"
)
CONFIG_PATH = os.path.join(STATE_DIR, "config.json")
CACHE_PATH = os.path.join(STATE_DIR, "cache.json")
# Same sidecar contract as the Mac build, for a statusline that wants the
# credit figure without paying for a subprocess or a network call.
STATUSLINE_PATH = os.path.join(STATE_DIR, "statusline")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ClaudeUsageTray"

# Where Windows 11 records, per tray icon, whether it sits on the taskbar or
# in the overflow flyout. See promote_icon().
NOTIFY_ICON_KEY = r"Control Panel\NotifyIconSettings"
ICON_UID = 1

# Percent-of-limit thresholds, mirroring the Mac build and the Claude Code
# statusline so the same figure reads the same everywhere.
COLOR_WARN_AT = 50
COLOR_ALERT_AT = 80

# Straight RGB here, unlike the Mac build's xterm-256 indices -- nothing is
# going through a terminal, we are setting pixels ourselves. These are the
# rgb values behind that build's palette.
GREEN = (0, 215, 0)
AMBER = (255, 215, 0)
RED = (255, 0, 0)

MIN_FETCH_SECONDS = 60
STALE_AFTER_SECONDS = 150
MAX_BACKOFF_SECONDS = 3600

SEVERITY_RANK = {"normal": 0, "warning": 1, "critical": 2, "severe": 2}
TOKEN_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_-]+")

KIND_META = {
    "session": ("S", "Session (5h)"),
    "weekly_all": ("W", "Weekly (all)"),
    "weekly_opus": ("O", "Weekly (Opus)"),
    "weekly_sonnet": ("N", "Weekly (Sonnet)"),
}

CURRENCY_SYMBOL = {"GBP": "£", "USD": "$", "EUR": "€", "JPY": "¥"}


# --------------------------------------------------------------------------
# safety helpers
# --------------------------------------------------------------------------


def sanitize(value, limit=48):
    """Strip control characters and cap length.

    Win32 menus and tooltips are not line-oriented the way SwiftBar's output
    is, so there is no "|" to defend against here. A stray newline or NUL in
    server data would still corrupt a menu label or truncate a tooltip, so
    untrusted strings are still filtered before display.
    """
    if not isinstance(value, str):
        return ""
    return "".join(c for c in value if c.isprintable())[:limit]


def redact(value):
    """Keep credentials out of anything we cache or display."""
    return TOKEN_PATTERN.sub("sk-ant-***", str(value))


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------


def write_atomic(path, text):
    """Atomic so a crash mid-write cannot leave a truncated file behind.

    The Mac build also chmods 0600; on NTFS the inherited ACL already limits
    this to the user's profile, and os.chmod cannot express more than the
    read-only bit, so mode is left alone.
    """
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
    except OSError:
        pass
    # PID in the temp name for the same reason as the Mac build: a forced
    # refresh and the scheduled poll can be in flight at once.
    temporary = f"{path}.{os.getpid()}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            try:
                os.remove(temporary)
            except OSError:
                pass


def read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, payload):
    try:
        write_atomic(path, json.dumps(payload, indent=2))
    except OSError:
        pass


def write_statusline_sidecar(data):
    """used limit currency exponent percent epoch -- removed when the account
    has no extra-usage credits, so a stale chip cannot outlive the feature."""
    spend = data.get("spend") if isinstance(data.get("spend"), dict) else {}
    used, limit = spend.get("used"), spend.get("limit")
    if not (
        spend.get("enabled")
        and isinstance(used, dict)
        and isinstance(limit, dict)
        and used.get("amount_minor") is not None
    ):
        try:
            os.remove(STATUSLINE_PATH)
        except OSError:
            pass
        return
    fields = (
        int(used.get("amount_minor") or 0),
        int(limit.get("amount_minor") or 0),
        sanitize(used.get("currency") or "", limit=4) or "?",
        int(used.get("exponent") or 2),
        int(spend.get("percent") or 0),
        int(time.time()),
    )
    try:
        write_atomic(STATUSLINE_PATH, " ".join(str(f) for f in fields) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# launch at login
# --------------------------------------------------------------------------


def pythonw():
    """Prefer pythonw.exe so a login launch never flashes a console."""
    windowless = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return windowless if os.path.exists(windowless) else sys.executable


def login_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
        return True
    except OSError:
        return False


def enable_login():
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(
            key, RUN_VALUE, 0, winreg.REG_SZ, f'"{pythonw()}" "{SELF}"'
        )


def disable_login():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_WRITE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
    except OSError:
        pass


# --------------------------------------------------------------------------
# taskbar vs overflow
# --------------------------------------------------------------------------


def icon_settings_key():
    """Find the shell's registry entry for our tray icon, or None.

    Shell_NotifyIcon has no "always show me" flag, deliberately -- if it did,
    every installer's icon would claim a permanent slot. What Windows 11 does
    expose is one DWORD per icon under Control Panel\\NotifyIconSettings, and
    that is precisely what the Settings > Taskbar toggle writes. Setting it
    ourselves is the same act performed from here.

    The subkey is named by a hash the shell computes, so ours has to be found
    by what it recorded against it. Two wrinkles:

      - the path is stored against a KNOWNFOLDERID rather than a drive, as
        "{6D809377-...}\\Python313\\pythonw.exe", so it cannot be compared to
        sys.executable directly -- the GUID prefix is stripped and the
        remainder matched as a suffix.
      - the executable is pythonw.exe, which any other Python tray app also
        uses, so the icon's uID is checked too.

    Returns None until the shell has actually seen the icon, which is why
    promotion is an action you invoke rather than something done at startup.
    """
    executable = os.path.normcase(pythonw())
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, NOTIFY_ICON_KEY)
    except OSError:
        return None
    with root:
        for index in range(winreg.QueryInfoKey(root)[0]):
            try:
                name = winreg.EnumKey(root, index)
                with winreg.OpenKey(root, name) as entry:
                    path = winreg.QueryValueEx(entry, "ExecutablePath")[0]
                    uid = winreg.QueryValueEx(entry, "UID")[0]
            except OSError:
                continue
            tail = path.split("}\\", 1)[-1] if path.startswith("{") else path
            if uid == ICON_UID and executable.endswith(os.path.normcase(tail)):
                return name
    return None


def pinned():
    name = icon_settings_key()
    if not name:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{NOTIFY_ICON_KEY}\\{name}") as entry:
            return bool(winreg.QueryValueEx(entry, "IsPromoted")[0])
    except OSError:
        return False


def set_pinned(pin):
    """Returns True if the setting was written."""
    name = icon_settings_key()
    if not name:
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, f"{NOTIFY_ICON_KEY}\\{name}", 0, winreg.KEY_SET_VALUE
        ) as entry:
            winreg.SetValueEx(entry, "IsPromoted", 0, winreg.REG_DWORD, 1 if pin else 0)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------


def backoff_for(fails, err=None):
    """Seconds to wait after a failed attempt.

    Doubles per consecutive failure. A server retry-after is honoured only
    when it asks for *longer*: the endpoint has been observed returning
    `retry-after: 0` while still refusing, and obeying that literally means
    retrying immediately and forever, which keeps the limit tripped.
    """
    wait = MIN_FETCH_SECONDS * (2 ** min(fails - 1, 8))
    if err is not None:
        try:
            wait = max(wait, int(err.headers.get("retry-after") or 0))
        except (TypeError, ValueError, AttributeError):
            pass
    return min(wait, MAX_BACKOFF_SECONDS)


def credentials():
    """Returns (access_token, plan).

    The file is the Windows equivalent of the login keychain item, with the
    same `claudeAiOauth` payload. Read on every poll rather than cached,
    because Claude Code rewrites it when it refreshes the token.
    """
    try:
        with open(CREDENTIALS_PATH, encoding="utf-8") as handle:
            oauth = json.load(handle)["claudeAiOauth"]
    except FileNotFoundError:
        raise RuntimeError("no credentials file (is Claude Code signed in?)")
    except (OSError, ValueError, KeyError):
        raise RuntimeError("credentials file unreadable")
    token = oauth.get("accessToken")
    if not token:
        raise RuntimeError("no access token (try signing in to Claude Code)")
    return token, oauth.get("subscriptionType") or ""


def fetch(access_token):
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.load(response)


def meta_for(kind):
    """Known kinds get a curated tag/label; anything new the server invents is
    sanitized before it can reach a menu label."""
    if kind in KIND_META:
        return KIND_META[kind]
    clean = sanitize(kind, limit=24)
    pretty = clean.replace("_", " ").capitalize() or "Unknown"
    return (clean[:1].upper() or "?", pretty)


def parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def seconds_until(value):
    when = parse_ts(value)
    if when is None:
        return None
    return int((when - datetime.now(timezone.utc)).total_seconds())


def compact_duration(seconds):
    """'3h39m' / '5d0h' / '42m' -- the statusline's shape, no spaces."""
    if seconds is None:
        return ""
    if seconds <= 0:
        return "now"
    minutes = seconds // 60
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


def describe_reset(value):
    """'resets in 3h 39m (15:40)' -- relative first, since that is what you
    act on."""
    when = parse_ts(value)
    if when is None:
        return ""
    local = when.astimezone()
    delta = int((when - datetime.now(timezone.utc)).total_seconds())
    if delta <= 0:
        return "resets now"
    hours, minutes = divmod(delta // 60, 60)
    if hours >= 24:
        return f"resets {local:%a %d %b, %H:%M}"
    span = f"{hours}h {minutes}m" if hours else f"{minutes}m"
    return f"resets in {span} ({local:%H:%M})"


def money(amount, compact=False):
    """Format one of the API's {amount_minor, currency, exponent} objects.

    compact drops the minor units for whole amounts."""
    if not isinstance(amount, dict):
        return None
    minor = amount.get("amount_minor")
    if minor is None:
        return None
    currency = amount.get("currency", "")
    exponent = amount.get("exponent", 2)
    if not isinstance(exponent, int) or not 0 <= exponent <= 4:
        exponent = 2
    symbol = CURRENCY_SYMBOL.get(currency, sanitize(currency, limit=4) + " ")
    scale = 10**exponent
    if compact and minor % scale == 0:
        return f"{symbol}{minor // scale}"
    return f"{symbol}{minor / scale:.{exponent}f}"


def collect_limits(data):
    """Prefer the structured `limits` array; fall back to the flat fields."""
    rows = []
    for entry in data.get("limits") or []:
        if not isinstance(entry, dict) or entry.get("percent") is None:
            continue
        tag, label = meta_for(entry.get("kind", ""))
        rows.append(
            {
                "tag": tag,
                "label": label,
                "percent": entry["percent"],
                "severity": entry.get("severity") or "normal",
                "resets_at": entry.get("resets_at"),
            }
        )
    if rows:
        return rows
    for key, kind in (("five_hour", "session"), ("seven_day", "weekly_all")):
        block = data.get(key)
        if isinstance(block, dict) and block.get("utilization") is not None:
            tag, label = meta_for(kind)
            rows.append(
                {
                    "tag": tag,
                    "label": label,
                    "percent": block["utilization"],
                    "severity": "normal",
                    "resets_at": block.get("resets_at"),
                }
            )
    return rows


def alert_code(percent, severity="normal"):
    """Green below the warn threshold, then amber, then red. The server's own
    severity can escalate early -- whichever trips first wins."""
    if percent >= COLOR_ALERT_AT or SEVERITY_RANK.get(severity, 0) >= 2:
        return RED
    if percent >= COLOR_WARN_AT or SEVERITY_RANK.get(severity, 0) == 1:
        return AMBER
    return GREEN


def credit_chip(spend):
    """'$0/25' -- symbol once, minor units dropped when whole."""
    used = money(spend.get("used"), compact=True)
    limit = money(spend.get("limit"), compact=True)
    if not (used and limit):
        return None
    symbol = CURRENCY_SYMBOL.get((spend.get("used") or {}).get("currency", ""), "")
    if symbol and limit.startswith(symbol):
        limit = limit[len(symbol) :]
    return f"{used}/{limit}"


def format_wait(seconds):
    """Always keep seconds visible so the countdown is seen to move."""
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds:02d}s"


def status_line(error, backoff_until):
    """Recomputed on every render, so the wait counts down instead of showing
    the figure that happened to be true when the request failed."""
    if not error:
        return None
    remaining = int(backoff_until - time.time())
    if remaining <= 0:
        return f"{error}, retrying shortly"
    at = datetime.fromtimestamp(backoff_until)
    return f"{error}, retrying at {at:%H:%M:%S} (in {format_wait(remaining)})"


# --------------------------------------------------------------------------
# Win32
# --------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
uxtheme = ctypes.WinDLL("uxtheme", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

WM_DESTROY = 0x0002
WM_SETTINGCHANGE = 0x001A
WM_COMMAND = 0x0111
WM_TIMER = 0x0113
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAY = WM_APP + 1  # our tray callback
WM_FETCHED = WM_APP + 2  # posted by the fetch thread when it lands

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO, NIIF_WARNING = 0x01, 0x02

MF_STRING, MF_DISABLED, MF_GRAYED = 0x0000, 0x0002, 0x0001
MF_SEPARATOR, MF_CHECKED = 0x0800, 0x0008
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x0002, 0x0080, 0x0100

TRANSPARENT = 1
NONANTIALIASED_QUALITY = 3
ANTIALIASED_QUALITY = 4
FW_BOLD = 700
DEFAULT_CHARSET = 1

# Tahoma, not the Segoe UI you would reach for by default. Compared side by
# side at 16px it renders noticeably larger for the same fitted height and its
# hinted bitmaps stay unambiguous, where Segoe UI's thin bold strokes turn "8"
# and "9" to mush. Antialiasing is dropped at tray sizes for the same reason --
# below about 20px the grey fringe costs more legibility than the smooth edge
# buys -- but kept above that, where chunky pixels would be the visible flaw.
ICON_FACE = "Tahoma"
CRISP_BELOW = 20

ID_REFRESH = 1001
ID_SETTINGS = 1002
ID_COLOR = 1003
ID_LOGIN = 1004
ID_WEEKLY = 1005
ID_QUIT = 1006
ID_PIN = 1007

# uxtheme's PreferredAppMode. A plain Win32 popup menu ignores the system
# theme and renders light forever unless the process asks for otherwise.
APP_MODE_DEFAULT = 0
APP_MODE_FORCE_DARK = 2


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", w.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", w.HINSTANCE),
        ("hIcon", w.HICON),
        ("hCursor", w.HANDLE),
        ("hbrBackground", w.HBRUSH),
        ("lpszMenuName", w.LPCWSTR),
        ("lpszClassName", w.LPCWSTR),
    ]


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", w.DWORD),
        ("hWnd", w.HWND),
        ("uID", w.UINT),
        ("uFlags", w.UINT),
        ("uCallbackMessage", w.UINT),
        ("hIcon", w.HICON),
        ("szTip", w.WCHAR * 128),
        ("dwState", w.DWORD),
        ("dwStateMask", w.DWORD),
        ("szInfo", w.WCHAR * 256),
        ("uVersion", w.UINT),
        ("szInfoTitle", w.WCHAR * 64),
        ("dwInfoFlags", w.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", w.HICON),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", w.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", w.WORD),
        ("biBitCount", w.WORD),
        ("biCompression", w.DWORD),
        ("biSizeImage", w.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", w.DWORD),
        ("biClrImportant", w.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", w.DWORD * 3)]


class TEXTMETRIC(ctypes.Structure):
    _fields_ = [
        ("tmHeight", ctypes.c_long),
        ("tmAscent", ctypes.c_long),
        ("tmDescent", ctypes.c_long),
        ("tmInternalLeading", ctypes.c_long),
        ("tmExternalLeading", ctypes.c_long),
        ("tmAveCharWidth", ctypes.c_long),
        ("tmMaxCharWidth", ctypes.c_long),
        ("tmWeight", ctypes.c_long),
        ("tmOverhang", ctypes.c_long),
        ("tmDigitizedAspectX", ctypes.c_long),
        ("tmDigitizedAspectY", ctypes.c_long),
        ("tmFirstChar", w.WCHAR),
        ("tmLastChar", w.WCHAR),
        ("tmDefaultChar", w.WCHAR),
        ("tmBreakChar", w.WCHAR),
        ("tmItalic", w.BYTE),
        ("tmUnderlined", w.BYTE),
        ("tmStruckOut", w.BYTE),
        ("tmPitchAndFamily", w.BYTE),
        ("tmCharSet", w.BYTE),
    ]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", w.BOOL),
        ("xHotspot", w.DWORD),
        ("yHotspot", w.DWORD),
        ("hbmMask", w.HBITMAP),
        ("hbmColor", w.HBITMAP),
    ]


# Handles are 64-bit; without explicit restypes ctypes assumes int and
# silently truncates them, which fails in ways that look like random bugs.
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.CreateWindowExW.restype = w.HWND
user32.CreateWindowExW.argtypes = [
    w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID,
]
user32.CreateIconIndirect.restype = w.HICON
user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
user32.CreatePopupMenu.restype = w.HMENU
user32.TrackPopupMenu.restype = ctypes.c_int
user32.TrackPopupMenu.argtypes = [
    w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.LPVOID,
]
user32.AppendMenuW.argtypes = [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR]
user32.GetDC.restype = w.HDC
user32.SetTimer.restype = ctypes.c_size_t
user32.SetTimer.argtypes = [w.HWND, ctypes.c_size_t, w.UINT, w.LPVOID]
gdi32.CreateCompatibleDC.restype = w.HDC
gdi32.CreateCompatibleDC.argtypes = [w.HDC]
gdi32.CreateDIBSection.restype = w.HBITMAP
gdi32.CreateDIBSection.argtypes = [
    w.HDC, ctypes.POINTER(BITMAPINFO), w.UINT,
    ctypes.POINTER(ctypes.c_void_p), w.HANDLE, w.DWORD,
]
gdi32.CreateBitmap.restype = w.HBITMAP
gdi32.CreateFontW.restype = w.HFONT
gdi32.CreateFontW.argtypes = [ctypes.c_int] * 5 + [w.DWORD] * 8 + [w.LPCWSTR]
gdi32.SelectObject.restype = w.HGDIOBJ
gdi32.SelectObject.argtypes = [w.HDC, w.HGDIOBJ]
gdi32.DeleteObject.argtypes = [w.HGDIOBJ]
gdi32.TextOutW.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int, w.LPCWSTR, ctypes.c_int]
gdi32.GetTextExtentPoint32W.argtypes = [
    w.HDC, w.LPCWSTR, ctypes.c_int, ctypes.POINTER(w.SIZE),
]
gdi32.GetTextMetricsW.argtypes = [w.HDC, ctypes.POINTER(TEXTMETRIC)]
gdi32.SetBkMode.argtypes = [w.HDC, ctypes.c_int]
gdi32.SetTextColor.argtypes = [w.HDC, w.DWORD]
gdi32.DeleteDC.argtypes = [w.HDC]
shell32.Shell_NotifyIconW.restype = w.BOOL
shell32.Shell_NotifyIconW.argtypes = [w.DWORD, ctypes.POINTER(NOTIFYICONDATA)]

# Same trap for every other handle-taking call: an undeclared argtype defaults
# to c_int, and a 64-bit HWND/HDC/HICON does not survive the trip.
user32.ReleaseDC.argtypes = [w.HWND, w.HDC]
user32.DestroyIcon.argtypes = [w.HICON]
user32.DestroyWindow.argtypes = [w.HWND]
user32.DestroyMenu.argtypes = [w.HMENU]
user32.SetForegroundWindow.argtypes = [w.HWND]
user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
user32.RegisterWindowMessageW.argtypes = [w.LPCWSTR]
user32.RegisterWindowMessageW.restype = w.UINT
kernel32.GetModuleHandleW.restype = w.HMODULE
kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
kernel32.CreateMutexW.restype = w.HANDLE
kernel32.CreateMutexW.argtypes = [w.LPVOID, w.BOOL, w.LPCWSTR]

_FONTS = {}


def font_for(px, quality):
    if (px, quality) not in _FONTS:
        _FONTS[(px, quality)] = gdi32.CreateFontW(
            -px, 0, 0, 0, FW_BOLD, 0, 0, 0, DEFAULT_CHARSET,
            0, 0, quality, 0, ICON_FACE,
        )
    return _FONTS[(px, quality)]


THEME_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"


def theme_is_light(value):
    """Windows tracks two separate light/dark settings and they disagree
    often: 'SystemUsesLightTheme' is the taskbar and tray, 'AppsUseLightTheme'
    is application chrome such as our menu."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, THEME_KEY) as handle:
            return bool(winreg.QueryValueEx(handle, value)[0])
    except OSError:
        return False  # dark is the Windows 11 default


def taskbar_is_light():
    """Only matters with colour switched off, when the digits have to fall
    back to a plain contrasting tone rather than a severity tint."""
    return theme_is_light("SystemUsesLightTheme")


def apply_menu_theme():
    """Make the popup menu follow the system light/dark setting.

    Classic Win32 menus are not theme-aware: on a dark Windows 11 they still
    come up white. The only opt-in short of owner-drawing every item, check
    mark and highlight state ourselves is a pair of undocumented uxtheme
    exports, available by ordinal only -- 135 SetPreferredAppMode and 136
    FlushMenuThemes. Widely relied on (it is how Explorer's own context menus
    and most third-party apps do it), but undocumented all the same, so the
    whole thing is guarded: if a future build withdraws them we fall back to
    the light menu we would have had regardless.

    Forcing the mode rather than passing AllowDark, because AllowDark leaves
    the decision to per-control theming that a bare TrackPopupMenu never
    participates in -- it comes up light anyway.
    """
    try:
        set_preferred_app_mode = uxtheme[135]
        set_preferred_app_mode.restype = ctypes.c_int
        set_preferred_app_mode.argtypes = [ctypes.c_int]
        flush_menu_themes = uxtheme[136]
        flush_menu_themes.restype = None
        flush_menu_themes.argtypes = []
    except (AttributeError, OSError, ValueError):
        return
    try:
        set_preferred_app_mode(
            APP_MODE_DEFAULT if theme_is_light("AppsUseLightTheme") else APP_MODE_FORCE_DARK
        )
        flush_menu_themes()
    except OSError:
        pass


def make_icon(rows, size):
    """Draw rows of [(text, (r,g,b))] into a tray-sized HICON.

    GDI text rendering does not write an alpha channel, so drawing coloured
    text straight onto a transparent bitmap yields fully transparent -- that
    is, invisible -- glyphs. The way round it: draw in white on black, then
    read the buffer back and use each pixel's brightness as its alpha while
    substituting the colour we actually wanted. Antialiased edges survive,
    which is what keeps two-digit numbers legible at 16px.
    """
    screen = user32.GetDC(None)
    hdc = gdi32.CreateCompatibleDC(screen)
    user32.ReleaseDC(None, screen)

    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = size
    info.bmiHeader.biHeight = -size  # top-down, so row 0 is the top row
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = 0  # BI_RGB

    bits = ctypes.c_void_p()
    colour_bitmap = gdi32.CreateDIBSection(hdc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
    if not colour_bitmap:
        gdi32.DeleteDC(hdc)
        return None
    previous = gdi32.SelectObject(hdc, colour_bitmap)
    ctypes.memset(bits, 0, size * size * 4)

    gdi32.SetBkMode(hdc, TRANSPARENT)
    gdi32.SetTextColor(hdc, 0x00FFFFFF)

    band = max(size // max(len(rows), 1), 1)
    # One pixel of air between the two rows, so the digits do not read as a
    # single four-digit number.
    budget = band - 1 if len(rows) > 1 else band
    quality = NONANTIALIASED_QUALITY if size < CRISP_BELOW else ANTIALIASED_QUALITY
    for index, (text, _) in enumerate(rows):
        extent, metrics = w.SIZE(), TEXTMETRIC()
        # Fit to the digits' *ink*, not the line box: the line box carries
        # ascent, descent and internal leading that no digit occupies, and
        # sizing to it wastes roughly a third of a 16px icon. Shrink from
        # generous to fitting, so "100" and "7" both come out as large as
        # they can be rather than clipped or lost.
        ink, px = budget, budget
        for px in range(band * 2, 4, -1):
            gdi32.SelectObject(hdc, font_for(px, quality))
            gdi32.GetTextMetricsW(hdc, ctypes.byref(metrics))
            gdi32.GetTextExtentPoint32W(hdc, text, len(text), ctypes.byref(extent))
            ink = metrics.tmAscent - metrics.tmInternalLeading
            if extent.cx <= size and ink <= budget:
                break
        # TextOut positions the top of the *cell*; the glyph starts one
        # internal-leading further down, so subtract it back off.
        top = index * band + (band - ink) // 2 - metrics.tmInternalLeading
        gdi32.TextOutW(hdc, (size - extent.cx) // 2, top, text, len(text))
    gdi32.GdiFlush()

    pixels = (ctypes.c_ubyte * (size * size * 4)).from_address(bits.value)
    for y in range(size):
        red, green, blue = rows[min(y // band, len(rows) - 1)][1]
        row = y * size * 4
        for x in range(size):
            offset = row + x * 4
            coverage = max(pixels[offset], pixels[offset + 1], pixels[offset + 2])
            if coverage:
                pixels[offset] = blue
                pixels[offset + 1] = green
                pixels[offset + 2] = red
                pixels[offset + 3] = coverage
            else:
                pixels[offset + 3] = 0

    # A 32bpp icon carries its own alpha, so the AND mask is unused -- but
    # CreateIconIndirect still requires one, and an uninitialised bitmap can
    # punch holes in the result, hence the explicit zero fill.
    stride = ((size + 31) // 32) * 4
    blank = (ctypes.c_ubyte * (stride * size))()
    mask_bitmap = gdi32.CreateBitmap(size, size, 1, 1, blank)

    icon_info = ICONINFO()
    icon_info.fIcon = True
    icon_info.hbmMask = mask_bitmap
    icon_info.hbmColor = colour_bitmap
    icon = user32.CreateIconIndirect(ctypes.byref(icon_info))

    gdi32.SelectObject(hdc, previous)
    gdi32.DeleteObject(colour_bitmap)
    gdi32.DeleteObject(mask_bitmap)
    gdi32.DeleteDC(hdc)
    return icon


# --------------------------------------------------------------------------
# the app
# --------------------------------------------------------------------------


class Tray:
    def __init__(self):
        self.config = read_json(CONFIG_PATH)
        self.cache = read_json(CACHE_PATH)
        self.lock = threading.Lock()
        self.fetching = False
        self.icon = None
        self.hwnd = None
        self.size = max(user32.GetSystemMetrics(49), 16)  # SM_CXSMICON
        # Keep a reference: ctypes callbacks are garbage collected like any
        # other object, and Windows holding the only pointer does not count.
        self.wndproc = WNDPROC(self.on_message)
        self.taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")

        # "Open at login" defaults on, so install it the first time we run and
        # record that we did -- after which the toggle is the user's to own.
        if not self.config.get("login_initialised"):
            if not login_enabled():
                enable_login()
            self.config["login_initialised"] = True
            write_json(CONFIG_PATH, self.config)

    # -- config -----------------------------------------------------------

    def option(self, name, default):
        return bool(self.config.get(name, default))

    def toggle(self, name, default):
        self.config[name] = not self.option(name, default)
        write_json(CONFIG_PATH, self.config)

    # -- window -----------------------------------------------------------

    def create_window(self):
        instance = kernel32.GetModuleHandleW(None)
        cls = WNDCLASS()
        cls.lpfnWndProc = self.wndproc
        cls.hInstance = instance
        cls.lpszClassName = "ClaudeUsageTrayWindow"
        user32.RegisterClassW(ctypes.byref(cls))
        # A real (if never shown) window, not HWND_MESSAGE: message-only
        # windows do not receive the TaskbarCreated broadcast, so the icon
        # would vanish for good if Explorer restarted.
        self.hwnd = user32.CreateWindowExW(
            0, cls.lpszClassName, APP_NAME, 0, 0, 0, 0, 0, None, None, instance, None
        )

    def notify(self, action, icon=None, tip=None, info=None, info_icon=NIIF_INFO):
        data = NOTIFYICONDATA()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        data.hWnd = self.hwnd
        data.uID = ICON_UID
        data.uFlags = NIF_MESSAGE
        data.uCallbackMessage = WM_TRAY
        if icon is not None:
            data.uFlags |= NIF_ICON
            data.hIcon = icon
        if tip is not None:
            data.uFlags |= NIF_TIP
            data.szTip = tip[:127]
        if info is not None:
            data.uFlags |= NIF_INFO
            data.szInfoTitle = APP_NAME[:63]
            data.szInfo = info[:255]
            data.dwInfoFlags = info_icon
        return shell32.Shell_NotifyIconW(action, ctypes.byref(data))

    def balloon(self, text, warning=False):
        """The only channel for one-off feedback: the menu is gone by the time
        an action runs, and a message box would steal focus."""
        self.notify(
            NIM_MODIFY, info=text, info_icon=NIIF_WARNING if warning else NIIF_INFO
        )

    # -- rendering --------------------------------------------------------

    def icon_rows(self):
        """The session by default, at full height; the tightest weekly limit
        added as a second row when asked for.

        Session only is the default because it is the figure you act on
        minute to minute, and one row gets roughly double the glyph height of
        two -- which is the difference between readable and squinting in a
        16px tray. Everything else is always in the tooltip and the menu,
        which have room for it.
        """
        rows = collect_limits(self.data() or {})
        if not rows:
            return [("--", (138, 138, 142))]
        session = next((r for r in rows if r["tag"] == "S"), None)
        others = [r for r in rows if r is not session]
        tightest = max(others, key=lambda r: r["percent"], default=None)
        if self.option("weekly", False):
            chosen = [r for r in (session, tightest) if r]
        else:
            # Fall back to the tightest other limit on the odd chance the
            # server stops returning a session row at all.
            chosen = [session or tightest]
        plain = (255, 255, 255) if not taskbar_is_light() else (0, 0, 0)
        use_colour = self.option("color", True)
        return [
            (
                str(round(r["percent"])),
                alert_code(r["percent"], r["severity"]) if use_colour else plain,
            )
            for r in chosen
        ]

    def tooltip(self):
        """szTip caps at 127 characters, so this is the compact shape rather
        than the menu's fully spelled-out one."""
        data = self.data()
        if not data:
            detail = status_line(self.cache.get("error"), self.cache.get("backoff_until", 0))
            return sanitize(redact(detail or "no data yet"), limit=127)
        lines = []
        for row in collect_limits(data):
            span = compact_duration(seconds_until(row["resets_at"]))
            lines.append(
                f"{row['label']}  {round(row['percent'])}%"
                + (f"  {span}" if span else "")
            )
        spend = data.get("spend") if isinstance(data.get("spend"), dict) else {}
        if spend.get("enabled"):
            chip = credit_chip(spend)
            if chip:
                lines.append(f"Credits  {chip}")
        age = self.age()
        if age is None or age > STALE_AFTER_SECONDS:
            lines.append("(figures stale)")
        return "\n".join(lines)[:127]

    def refresh_display(self):
        icon = make_icon(self.icon_rows(), self.size)
        # If GDI refused us a bitmap, keep the icon we already have rather than
        # handing the shell a null and blanking ourselves out of the tray.
        self.notify(NIM_MODIFY, icon=icon or self.icon, tip=self.tooltip())
        if icon:
            if self.icon:
                user32.DestroyIcon(self.icon)
            self.icon = icon

    # -- data -------------------------------------------------------------

    def data(self):
        with self.lock:
            data = self.cache.get("data")
        return data if isinstance(data, dict) else None

    def age(self):
        with self.lock:
            fetched_at = self.cache.get("fetched_at", 0)
        return time.time() - fetched_at if fetched_at else None

    def maybe_fetch(self, force=False):
        now = time.time()
        with self.lock:
            if self.fetching:
                return
            due = force or now - self.cache.get("last_attempt", 0) >= MIN_FETCH_SECONDS
            # Deliberately still honoured on a forced refresh: if the server
            # said wait, we wait.
            blocked = now < self.cache.get("backoff_until", 0)
            if not due or blocked:
                return
            self.fetching = True
            self.cache["last_attempt"] = now
        threading.Thread(target=self.fetch_now, daemon=True).start()

    def fetch_now(self):
        now = time.time()
        try:
            access_token, plan = credentials()
            payload = fetch(access_token)
            update = {
                "data": payload,
                "plan": plan,
                "fetched_at": now,
                "backoff_until": 0,
                "error": None,
                "fails": 0,
            }
            write_statusline_sidecar(payload)
        except urllib.error.HTTPError as err:
            with self.lock:
                fails = self.cache.get("fails", 0) + 1
            update = {
                "fails": fails,
                "error": "rate limited" if err.code == 429 else f"HTTP {err.code} from endpoint",
                "backoff_until": now + backoff_for(fails, err),
            }
        except Exception as err:  # noqa: BLE001 - never let the tray icon break
            with self.lock:
                fails = self.cache.get("fails", 0) + 1
            update = {
                # redact: an exception string could conceivably carry the token.
                "error": redact(err) or err.__class__.__name__,
                "fails": fails,
                "backoff_until": now + backoff_for(fails),
            }
        with self.lock:
            self.cache.update(update)
            self.fetching = False
            snapshot = dict(self.cache)
        write_json(CACHE_PATH, snapshot)
        user32.PostMessageW(self.hwnd, WM_FETCHED, 0, 0)

    # -- menu -------------------------------------------------------------

    def show_menu(self):
        menu = user32.CreatePopupMenu()
        data = self.data()
        rows = collect_limits(data or {})

        # Informational rows first, greyed so they read as status rather than
        # as things to click.
        width = max([len(r["label"]) for r in rows] + [len("Extra credits")])
        for row in rows:
            reset = describe_reset(row["resets_at"])
            label = f"{row['label']:<{width}}   {round(row['percent']):>3}%"
            if reset:
                label += f"   {reset}"
            user32.AppendMenuW(menu, MF_STRING | MF_DISABLED | MF_GRAYED, 0, label)

        spend = (data or {}).get("spend") if isinstance((data or {}).get("spend"), dict) else {}
        used, limit = money(spend.get("used")), money(spend.get("limit"))
        if spend.get("enabled") and used and limit:
            percent = spend.get("percent")
            suffix = f"   {round(percent)}% used" if percent is not None else ""
            user32.AppendMenuW(
                menu, MF_STRING | MF_DISABLED | MF_GRAYED, 0,
                f"{'Extra credits':<{width}}   {used} of {limit}{suffix}",
            )
        if not rows:
            user32.AppendMenuW(menu, MF_STRING | MF_DISABLED | MF_GRAYED, 0, "No usage data yet")

        # Refresh and the provenance of the figures sit directly under the
        # figures they describe: "as of" only means something next to the
        # number it qualifies, and the action you would take about a stale
        # one is right there with it. Settings that change behaviour rather
        # than report it come after.
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_REFRESH, "Refresh now")
        plan = sanitize(self.cache.get("plan") or "", limit=24)
        if plan:
            user32.AppendMenuW(menu, MF_STRING | MF_DISABLED | MF_GRAYED, 0, f"Plan: {plan.capitalize()}")
        age = self.age()
        if age is None:
            stamp = "No successful fetch yet"
        else:
            when = datetime.fromtimestamp(time.time() - age)
            suffix = f" ({compact_duration(int(age))} ago)" if age > STALE_AFTER_SECONDS else ""
            stamp = f"Percentages as of {when:%H:%M:%S}{suffix}"
        user32.AppendMenuW(menu, MF_STRING | MF_DISABLED | MF_GRAYED, 0, stamp)
        detail = status_line(self.cache.get("error"), self.cache.get("backoff_until", 0))
        if detail:
            user32.AppendMenuW(
                menu, MF_STRING | MF_DISABLED | MF_GRAYED, 0,
                sanitize(redact(f"⚠ {detail}"), limit=120),
            )

        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        for label, enabled, ident in (
            ("Show weekly in icon", self.option("weekly", False), ID_WEEKLY),
            ("Colour in icon", self.option("color", True), ID_COLOR),
            ("Always show on taskbar", pinned(), ID_PIN),
            ("Open at login", login_enabled(), ID_LOGIN),
        ):
            user32.AppendMenuW(
                menu, MF_STRING | (MF_CHECKED if enabled else 0), ident, label
            )

        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_SETTINGS, "Open usage settings")
        user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "Quit")

        point = w.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        # Without the foreground/WM_NULL pair the menu refuses to dismiss when
        # you click away from it -- a documented Win32 quirk for tray menus.
        user32.SetForegroundWindow(self.hwnd)
        chosen = user32.TrackPopupMenu(
            menu, TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD,
            point.x, point.y, 0, self.hwnd, None,
        )
        user32.PostMessageW(self.hwnd, 0, 0, 0)
        user32.DestroyMenu(menu)
        if chosen:
            self.command(chosen)

    def command(self, ident):
        if ident == ID_REFRESH:
            self.maybe_fetch(force=True)
        elif ident == ID_SETTINGS:
            os.startfile(SETTINGS_URL)
        elif ident == ID_COLOR:
            self.toggle("color", True)
        elif ident == ID_WEEKLY:
            self.toggle("weekly", False)
        elif ident == ID_LOGIN:
            disable_login() if login_enabled() else enable_login()
        elif ident == ID_PIN:
            want = not pinned()
            if not set_pinned(want):
                self.balloon(
                    "Windows has not filed this icon yet. Try again in a "
                    "moment, or drag it out of the overflow by hand.",
                    warning=True,
                )
            elif want:
                self.balloon(
                    "Pinned to the taskbar. If it stays in the overflow, "
                    "sign out and back in -- Explorer caches the setting."
                )
        elif ident == ID_QUIT:
            user32.DestroyWindow(self.hwnd)
            return
        self.refresh_display()

    # -- message loop -----------------------------------------------------

    def on_message(self, hwnd, message, wparam, lparam):
        if message == self.taskbar_created:
            # Explorer restarted and forgot every tray icon; re-add ours.
            self.notify(NIM_ADD, icon=self.icon, tip=self.tooltip())
        elif message == WM_TRAY:
            if lparam in (WM_RBUTTONUP, WM_LBUTTONUP):
                self.show_menu()
            elif lparam == WM_LBUTTONDBLCLK:
                os.startfile(SETTINGS_URL)
        elif message == WM_TIMER:
            # Display only: the countdowns are recomputed locally on every
            # tick, while maybe_fetch enforces the once-a-minute network floor.
            self.maybe_fetch()
            self.refresh_display()
        elif message == WM_FETCHED:
            self.refresh_display()
        elif message == WM_SETTINGCHANGE:
            # Broadcast when the user flips light/dark. Re-theme the menu, and
            # redraw the icon too: with colour switched off the digits are
            # whichever tone contrasts with the taskbar, and that just moved.
            if lparam and ctypes.wstring_at(lparam) == "ImmersiveColorSet":
                apply_menu_theme()
                self.refresh_display()
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)
        elif message == WM_COMMAND:
            self.command(wparam & 0xFFFF)
        elif message == WM_DESTROY:
            self.notify(NIM_DELETE)
            user32.PostQuitMessage(0)
        else:
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)
        return 0

    def run(self):
        apply_menu_theme()
        self.create_window()
        self.icon = make_icon(self.icon_rows(), self.size)
        # The *first* tooltip is what the shell files the icon under, and it
        # is the name shown in Settings > Taskbar > Other system tray icons.
        # Send the app name, not the live figures: at this point there is no
        # data yet, and "no data yet" is a poor thing to be called forever.
        self.notify(NIM_ADD, icon=self.icon, tip=APP_NAME)
        self.maybe_fetch()
        self.refresh_display()
        # 10s, matching the Mac build's filename-driven cadence: it costs no
        # network and lets the retry countdown tick in seconds.
        user32.SetTimer(self.hwnd, 1, 10000, None)

        message = w.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))


def main():
    # Per-monitor v2, so SM_CXSMICON reports the real pixel size the tray
    # wants. Without it the icon is drawn at 16px then stretched, and the
    # digits blur into illegibility on a scaled display.
    try:
        # The context is a pointer-sized pseudo-handle, so it has to be
        # declared: as a bare int ctypes would pass a 32-bit -4 and Windows
        # would read rubbish in the top half.
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_ssize_t]
        user32.SetProcessDpiAwarenessContext(-4)  # PER_MONITOR_AWARE_V2
    except (AttributeError, OSError):
        try:
            user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass

    # A second instance would add a second icon and double the polling into
    # the rate limit that the whole throttle exists to avoid.
    kernel32.CreateMutexW(None, False, "ClaudeUsageTray.SingleInstance")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return
    Tray().run()


if __name__ == "__main__":
    main()
