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
import email.utils
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
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

# A rebranded copy of the interpreter. See build_launcher() for why running
# under pythonw.exe is not good enough.
LAUNCHER_EXE = os.path.join(STATE_DIR, "ClaudeUsage.exe")
LAUNCHER_CFG = os.path.join(STATE_DIR, "pyvenv.cfg")

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
# Nothing to report, and deliberately not one of the alert tones: a figure we
# cannot vouch for must not be able to render as a healthy green.
MUTED = (138, 138, 142)

# Measured 2026-08-07: the endpoint allows five calls, refuses the sixth, and
# stays shut for 300s. Probes at +30s, +60s and +91s were all still refused, so
# the budget does not trickle back a call at a time -- overshooting costs the
# remainder of the window outright, which is why the margin matters more than
# the average rate.
#
# 60s spends the entire budget and was what this used to poll at, so a single
# extra call from anywhere -- a manual refresh, a restart -- locked it out. 90s
# spends three or four of the five and leaves the rest for you. Faster buys
# little anyway: the percentages move slowly, and the countdowns beside them
# are recomputed locally every ten seconds regardless of when we last fetched.
MIN_FETCH_SECONDS = 90
# Two missed polls, which is what this has always meant. It was 240 back when
# a poll was 60s; the interval moved to 90 and this did not follow, which left
# it tripping at 2.7 polls and calling contention a fault sooner than intended.
STALE_AFTER_SECONDS = 270
# The tooltip flags staleness early because you had to hover to read it. The
# icon is glanced at, so it only dims once the age is beyond explaining away
# by a missed poll or two -- at which point the figure is not a live reading.
ICON_STALE_AFTER_SECONDS = 900
# The longest we will ever go without asking, and the only ceiling there is.
#
# It is 300 because that is the longest this endpoint has ever actually stayed
# shut: measured 2026-08-07, twice, a refusal lasts 300s and recovery came at
# 304. So a wait longer than this can only ever be waiting for something that
# has already ended. That single fact is what makes the header safe to bound --
# it has been seen asking for a full hour, and answering 200 to a probe within
# the minute, three separate times.
#
# The other measured fact is what makes bounding it cheap: requests made while
# refused do not extend the refusal. Asking again costs a refusal we can
# afford. Not asking costs the entire point of the thing.
#
# Everything else in this file -- the doubling, the server's retry-after, our
# own throttle -- is advice about *when inside this window*, never permission
# to leave it. See next_attempt_at(), which is the only place that decides.
MAX_SILENCE_SECONDS = 300
# Grace on top of MAX_SILENCE_SECONDS before silence is read as "nothing of
# ours was running". Generous enough that a machine merely running late never
# trips it. See unattended().
RESUME_GAP_SECONDS = 120
# How long a refused credential is taken at its word before we test it again.
# Not measured, unlike the numbers above it: 401 is unambiguous, and the
# recovery that actually matters -- Claude Code rewriting the token -- is seen
# locally on the next tick without asking anyone. This only backstops the case
# where the endpoint was wrong to refuse us. 900s makes that four requests an
# hour rather than forty, and caps a server-side false 401 at a quarter hour of
# `--`. A judgement, and a cheap one whichever way it is wrong.
AUTH_PROBE_SECONDS = 900
# What the menu says while the credential is the thing in the way. Both name
# the fix, because unlike every other failure in this file there is one and it
# is the user's: nothing this program can do turns a 401 into a 200.
#
# Two of them, because the blob answers the question a 401 leaves open. A token
# past its `expiresAt` is the ordinary overnight case and says so; a token
# refused while the file still calls it valid is something else -- revoked,
# rescoped, or the endpoint at fault -- and reads as the anomaly it is.
#
# Both say *in a terminal*, which the one string they replaced did not. On
# 2026-09-16 this tray sat on `--` for 40 hours saying "open Claude Code to
# refresh it" while Claude Code was open all day -- in the browser, which has
# its own session and never touches this file. Only the CLI writes it.
AUTH_ERROR = "token rejected, run Claude Code in a terminal to refresh it"
EXPIRED_ERROR = "token expired, run Claude Code in a terminal to refresh it"

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
# our own name in Windows' lists
# --------------------------------------------------------------------------


def pad4(data):
    return data + b"\x00" * (-len(data) % 4)


def version_node(key, value, kind, children=b""):
    """One VS_VERSIONINFO node. kind 1 is text, 0 is binary.

    The length field counts characters for text and bytes for binary, and
    every node is padded to a 4-byte boundary before its value and before its
    children. Get either wrong and Windows reports no version info at all
    rather than complaining.
    """
    if kind == 1:
        payload = (value + "\x00").encode("utf-16-le") if value else b""
        measure = len(payload) // 2
    else:
        payload = value
        measure = len(payload)
    body = struct.pack("<HHH", 0, measure, kind)
    body += (key + "\x00").encode("utf-16-le")
    body = pad4(body) + payload
    body = pad4(body) + children
    return struct.pack("<H", len(body)) + body[2:]


def version_resource(strings):
    fixed = struct.pack(
        "<LLLLLLLLLLLLL",
        0xFEEF04BD,  # signature
        0x00010000,  # struct version
        0x00010000, 0,  # file version 1.0.0.0
        0x00010000, 0,  # product version
        0x3F, 0,  # flags mask, flags
        0x00000004,  # VOS__WINDOWS32
        0x00000001,  # VFT_APP
        0, 0, 0,
    )
    entries = b"".join(pad4(version_node(k, v, 1)) for k, v in strings.items())
    # 0409 04B0: US English, Unicode.
    table = version_node("040904B0", "", 1, entries)
    strings_block = version_node("StringFileInfo", "", 1, pad4(table))
    translation = version_node("Translation", struct.pack("<HH", 0x0409, 0x04B0), 0)
    var_block = version_node("VarFileInfo", "", 1, pad4(translation))
    return version_node(
        "VS_VERSION_INFO", fixed, 0, pad4(strings_block) + pad4(var_block)
    )


def config_home():
    """The interpreter our launcher copy is pinned to, or None."""
    try:
        with open(LAUNCHER_CFG, encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.partition("=")
                if key.strip() == "home":
                    return value.strip()
    except OSError:
        pass
    return None


def build_launcher():
    """Copy the interpreter under our own name and rebrand it. True on success.

    Windows names a tray icon in Settings > Taskbar after the *executable's*
    FileDescription, and nothing the icon itself supplies changes that -- the
    tooltip is ignored. Run under pythonw.exe and you are listed as "Python",
    indistinguishable from every other Python tray app. The same string names
    us in Task Manager's Startup tab, which matters more, since we put
    ourselves there.

    So the interpreter is copied next to our state and its version resource
    rewritten. A copied CPython cannot find its installation by itself, but a
    two-line pyvenv.cfg is all it needs: the mechanism a virtual environment
    uses to point at its base, without the environment.

    Rebuilt whenever the interpreter it was copied from moves or is upgraded,
    since the copy is then pointing at a directory that may no longer exist.
    """
    source = os.path.join(sys.base_prefix, "pythonw.exe")
    if not os.path.exists(source):
        return False
    if os.path.exists(LAUNCHER_EXE) and config_home() == sys.base_prefix:
        return True
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        # Not written in place: the running copy is locked, and a half-copied
        # launcher is worse than none.
        staging = f"{LAUNCHER_EXE}.{os.getpid()}.tmp"
        shutil.copy2(source, staging)
        if not write_version_info(staging):
            os.remove(staging)
            return False
        with open(LAUNCHER_CFG, "w", encoding="utf-8") as handle:
            handle.write(f"home = {sys.base_prefix}\ninclude-system-site-packages = true\n")
        os.replace(staging, LAUNCHER_EXE)
        return True
    except OSError:
        return False


def write_version_info(path):
    blob = version_resource(
        {
            "CompanyName": "",
            "FileDescription": APP_NAME,
            "FileVersion": "1.0.0.0",
            "InternalName": "ClaudeUsage",
            "OriginalFilename": "ClaudeUsage.exe",
            "ProductName": APP_NAME,
            "ProductVersion": "1.0.0.0",
        }
    )
    handle = kernel32.BeginUpdateResourceW(path, False)
    if not handle:
        return False
    RT_VERSION = 16
    ok = kernel32.UpdateResourceW(
        handle,
        ctypes.cast(RT_VERSION, w.LPCWSTR),
        ctypes.cast(1, w.LPCWSTR),
        0x0409,
        blob,
        len(blob),
    )
    return bool(kernel32.EndUpdateResourceW(handle, not ok) and ok)


def relaunch_branded():
    """Hand over to the rebranded launcher. True if we did, and should exit.

    Deliberately before the single-instance mutex is taken, so the child does
    not find it already held.
    """
    if read_json(CONFIG_PATH).get("brand") is False:
        return False
    if os.path.normcase(sys.executable) == os.path.normcase(LAUNCHER_EXE):
        return False
    if not build_launcher():
        return False
    try:
        subprocess.Popen(
            [LAUNCHER_EXE, SELF],
            close_fds=True,
            creationflags=subprocess.DETACHED_PROCESS,
        )
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------
# launch at login
# --------------------------------------------------------------------------


def pythonw():
    """The executable to register for login.

    Whatever we are actually running under, except that a console
    interpreter is swapped for its windowless twin so a login launch never
    flashes a black box.
    """
    if os.path.basename(sys.executable).lower() != "python.exe":
        return sys.executable
    windowless = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return windowless if os.path.exists(windowless) else sys.executable


def login_command():
    return f'"{pythonw()}" "{SELF}"'


def login_value():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, RUN_VALUE)[0]
    except OSError:
        return None


def login_enabled():
    return login_value() is not None


def enable_login():
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, login_command())


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
    # sys.executable, not the login launcher: this has to match whatever the
    # shell actually saw register the icon, which is the process we are in.
    executable = os.path.normcase(sys.executable)
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


def retry_after_seconds(headers):
    """How long the server asked us to wait, in seconds, or 0 if it did not.

    RFC 9110 allows either a count of seconds or an HTTP-date, and this
    endpoint has only ever sent the first. Reading a date as "no answer" would
    file a genuine lockout under contention and retry it every ninety seconds,
    so both forms are read. What it says is recorded faithfully and bounded
    where the decision is made, in next_attempt_at: this is the header seen
    asking for a full hour and then serving the very next request a minute
    later.
    """
    value = (headers or {}).get("retry-after")
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return 0
    if when is None:
        return 0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, int(when.timestamp() - time.time()))


# A rolling record of every attempt, and nothing else. It exists because three
# separate faults this month were invisible after the fact: the cache holds only
# the current state, so by the time anyone looks, the evidence of how it got
# there has been overwritten. One line per attempt is enough to reconstruct all
# three, and at a poll every 90s it is a few hundred KB a week.
LOG_PATH = os.path.join(STATE_DIR, "log.jsonl")
LOG_MAX_BYTES = 256 * 1024
LOG_KEEP_BYTES = 192 * 1024


def rows_summary(data):
    """{"session": 14, "weekly_all": 6} -- what was on screen at the time."""
    try:
        return {str(r["tag"]): round(r["percent"], 1) for r in collect_limits(data or {})}
    except Exception:  # noqa: BLE001
        return {}


def log_event(fields):
    """Append one JSON line. Never raises, never grows without bound.

    Deliberately not a debug switch: the failures worth diagnosing here are
    rare, days apart, and never reproducible on demand, so a log you have to
    have turned on in advance is a log you will not have.
    """
    try:
        line = json.dumps({"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                           **fields}, default=str)
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
        except OSError:
            pass
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            print(redact(line), file=handle)
            size = handle.tell()
        if size > LOG_MAX_BYTES:
            # Keep the tail, and drop whatever partial line the cut lands in.
            with open(LOG_PATH, "rb") as handle:
                handle.seek(size - LOG_KEEP_BYTES)
                kept = handle.read().partition(bytes([10]))[2]
            write_atomic(LOG_PATH, kept.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - a diagnostic must never be the fault
        pass


def as_time(value, default=0):
    """A timestamp we can do arithmetic with, or `default`.

    Every reader of the cache goes through this, because "it came off disk" and
    "it is a finite number" are different claims. NaN is the dangerous one: it
    compares false against everything, so a NaN deadline is never past.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return value if math.isfinite(value) else default


def sane_cache(cache, now):
    """Bring a cache we did not necessarily write into range.

    Much smaller than it was, because there are no stored deadlines left to
    police: what used to be `backoff_until` and `server_backoff_until` are now
    derived by next_attempt_at() on every tick, and a value that is computed
    cannot be stale. What remains is the evidence those decisions are made
    from, and it is checked because all of it is either compared against the
    clock or used in arithmetic.

    Nothing was fetched or attempted in the future. A fetched_at ahead of now
    is the nastier of the two: it makes `age` negative, which reads as
    permanently fresh, so the figures would never be flagged again.
    """
    for key in ("last_attempt", "fetched_at", "auth_failed_at"):
        cache[key] = min(as_time(cache.get(key)), now)
    fails = cache.get("fails")
    if isinstance(fails, bool) or not isinstance(fails, int) or fails < 0:
        cache["fails"] = 0
    asked = cache.get("retry_after")
    if isinstance(asked, bool) or not isinstance(asked, (int, float)) or asked < 0:
        cache["retry_after"] = 0
    # Which token was refused, if one was. A non-string would compare unequal
    # to every fingerprint anyway, so this buys nothing but keeping the single
    # question rejected_token() asks a question about two strings.
    if not isinstance(cache.get("auth_failed_for"), str):
        cache["auth_failed_for"] = None
    # Deadlines from the design this replaced. Dropped so a cache written by
    # this version cannot be misread by a reader still expecting them.
    for dead in ("backoff_until", "server_backoff_until"):
        cache.pop(dead, None)
    return cache


def unattended(cache, now):
    """True when time has passed that nothing of ours was running for.

    MAX_SILENCE_SECONDS is the longest gap this program can leave between
    attempts, so a longer one cannot have been us waiting -- the machine was
    off, or suspended, or this is the first run since. The failure count on
    disk was then earned by a network nobody can see any more, and starting the
    doubling again is the only honest reading of it.

    Cheap to state now that there is a ceiling to measure against. It used to
    need the stored deadline, and a separate timer-gap check beside it to catch
    a suspend, because a power cycle leaves no gap to see: the process is new
    and the count comes straight back off disk. The clock covers both.
    """
    last = as_time(cache.get("last_attempt"))
    if not last:
        return False
    return now - (last + MAX_SILENCE_SECONDS) > RESUME_GAP_SECONDS


def token_fingerprint(token):
    """A stable name for a token that is not the token.

    This gets cached and logged, so it has to survive being read by anyone who
    can read those -- which the token must not, hence a digest rather than a
    prefix of it. Truncated because the only question ever asked of it is
    whether the credential on disk is still the one that was refused, and
    sixteen hex characters settle that.
    """
    if not isinstance(token, str) or not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def token_refused_before(cache, fingerprint):
    """True when a 401 stands recorded against exactly this credential.

    Separate from rejected_token() because the two are asked at different
    moments and the interval is only half the question. Once the probe falls
    due the gate opens, but the credential is no more trusted than it was a
    second earlier, and what comes back has to be read in that light.
    """
    recorded = cache.get("auth_failed_for")
    return bool(fingerprint) and isinstance(recorded, str) and recorded == fingerprint


def token_expired(expires_at, now):
    """True when the credential on disk says of itself that it has lapsed.

    `expiresAt` is Claude Code's own stamp, in epoch milliseconds, sitting in
    the blob beside the token it describes. It was read past for a year: the
    only question ever asked of the credential was what a 401 said about it,
    and the file had the answer the whole time.

    Read fresh every tick with the token, and cached nowhere. A stored copy
    would be a decision about a file that is opened anyway, which is the shape
    this design exists to avoid -- and a decision that outlives its evidence
    by exactly as long as the file goes unread.

    Fails towards asking, like every gate here. A missing field, a string, a
    bool, a NaN, a negative, a nonsense zero: every one lands on False, and
    False sends the request. Wrong that way costs one call. Wrong the other
    way is a tray that never asks again, which is the only failure in this
    file that cannot recover on its own.
    """
    stamp = as_time(expires_at) / 1000.0
    return stamp > 0 and now > stamp


def rejected_token(cache, fingerprint, now, expires_at):
    """True when the credential on disk is one the endpoint has already refused.

    The single failure here that waiting cannot fix. A 429 clears on its own
    and a dropped connection is worth retrying, but a 401 says this token is
    not acceptable, and the token is a file on this machine: it goes on not
    being acceptable until Claude Code rewrites it. Polling through that is
    worse than wasted -- three refusals in a row and the endpoint starts
    answering with an hour-long 429, which is how a token that expired
    overnight turned into nine hours of `--` on 2026-09-03, some 340 requests
    spent against a budget shared with Claude Code to learn the same thing 340
    times.

    So the gate is the credential rather than the clock, which is why this
    leaves next_attempt_at() alone and its bound still holds word for word.
    Nor is this the ceiling in disguise: recovery is not something that has to
    be polled for, it *is* the file changing, and the fingerprint of the file
    is read every tick for nothing. The interval only backstops the refusal
    having been the server's mistake rather than the token's.

    The expiry is read second, and only ever after a refusal already stands
    against this exact credential. That order is what makes it safe to trust a
    clock: a machine set wrong can say "expired" about a perfectly good token
    all day and still gate nothing, because the server has to have refused it
    first. The file alone never closes this; it only explains a refusal that
    has already happened, which is the one thing it is qualified to do.

    Fails towards asking, deliberately, and the elapsed time is bounded at
    both ends for the same reason next_attempt_at() bounds its own: a refusal
    dated in the future is the one input that could hold the gate shut for
    good, and a jumped clock or a hand-edited file produces one without any
    code here having gone wrong. An unrecognisable fingerprint, a missing or
    nonsensical timestamp, a cache written by another version -- every one of
    them lands on False, and False sends the request.
    """
    if not token_refused_before(cache, fingerprint):
        return False
    if token_expired(expires_at, now):
        # The refusal is accounted for, and by the same file the recovery will
        # arrive in. The interval below exists for a refusal the server got
        # wrong; a lapsed expiry is not that, so a probe cannot learn anything
        # the blob has not already said and there is nothing left to time.
        # This is the whole of the saving: without it the 15 minutes go on
        # spending a request each, for 40 hours, to be told what the file said
        # at the start.
        return True
    since = as_time(cache.get("auth_failed_at"))
    return since > 0 and 0 <= now - since < AUTH_PROBE_SECONDS


def rolled_over(row, fetched_at):
    """True when this row's window ended after our last good fetch.

    Not merely 'the reset time has passed': the server can hand back a window
    that expired moments ago and that reading is still the current truth. It
    is only when the rollover happened while we were blind that the number on
    screen describes a window nobody is in any more.
    """
    when = parse_ts(row.get("resets_at"))
    if when is None or not fetched_at:
        return False
    reset_at = when.timestamp()
    return reset_at <= time.time() and fetched_at < reset_at


def unanchored(row):
    """True when a row carries no window boundary at all.

    A session nobody is in comes back as `percent: 0` with `resets_at: null`:
    there is no window, so there is no clock to compare against and
    rolled_over() cannot see anything. While the reading is fresh that is
    simply the truth, and 0 is the right thing to show. Once it is stale it
    becomes unknowable -- a session started since would have opened a window we
    never saw, and the 0 we are holding describes only the quiet before it.
    """
    return parse_ts(row.get("resets_at")) is None


def unreliable(row, fetched_at, age):
    """The two ways a figure stops describing anything we can stand behind:
    its window ended while we were blind, or it never named a window and has
    since gone stale. Kept in one place because the icon, the tooltip and the
    menu must agree -- a `--` in the icon beside a number in the menu is worse
    than either alone."""
    if rolled_over(row, fetched_at):
        return True
    return unanchored(row) and (age is None or age > ICON_STALE_AFTER_SECONDS)


def unusable(cache, now):
    """True when there is nothing on screen left to protect.

    Once a row is showing `--` there is no figure a backoff can preserve, so
    continuing to sit one out buys nothing and costs the only thing this
    program does. Having no data at all counts the same way.
    """
    data = cache.get("data")
    if not isinstance(data, dict):
        return True
    fetched_at = as_time(cache.get("fetched_at"))
    age = now - fetched_at if fetched_at else None
    rows = collect_limits(data)
    if not rows:
        return True
    return any(unreliable(row, fetched_at, age) for row in rows)


def next_attempt_at(cache, now, forced=False):
    """The one moment we are allowed to ask again. Computed, never stored.

    Everything about pacing lives here. That is the point: the previous design
    accumulated penalties in the cache and then patched, one at a time, every
    path that ought to forgive them -- a failure count surviving a power cycle,
    a server's wait outliving the figures it was protecting, a wake nobody was
    running to notice. Each fix was correct and each left the next uncovered
    case waiting, because a list of exceptions can never be finished.

    So nothing is carried. Each tick asks this function from the state as it
    stands, and the answer is bounded by construction:

        next_attempt_at(anything, now) - now  <=  MAX_SILENCE_SECONDS

    holds for every possible cache, including ones no code path here can
    produce -- a clock that jumped, a hand-edited file, a restored backup, a
    field of the wrong type entirely. That property is what replaces the
    exceptions, and it is the thing worth testing.

    The floor matters as much as the ceiling: barring a person clicking, the
    answer is never sooner than MIN_FETCH_SECONDS after the last attempt, which
    is what keeps us inside a budget shared with Claude Code.
    """
    if forced:
        # Not pacing at all, and the only case that ignores the floor. Someone
        # clicking Refresh now is overruling exactly this function.
        return now
    # Pulled into the window the rest of this reasons about, which is what
    # makes both bounds hold for any input rather than only for a cache that
    # has been through sane_cache. An attempt from the future never happened,
    # and one older than the ceiling is already past due either way, so the two
    # are indistinguishable from here.
    last = min(max(as_time(cache.get("last_attempt")), now - MAX_SILENCE_SECONDS), now)
    if unattended(cache, now) or unusable(cache, now):
        # Nothing was running to earn those failures, or the figures they were
        # protecting are already a `--`. Either way waiting improves nothing,
        # so fall back to the ordinary interval.
        return last + MIN_FETCH_SECONDS
    fails = cache.get("fails", 0)
    if isinstance(fails, bool) or not isinstance(fails, int) or fails <= 0:
        return last + MIN_FETCH_SECONDS
    # Doubling, then the server's own ask if it wants longer. It has been seen
    # returning 0 while still refusing, so it is only ever taken as a floor.
    wait = MIN_FETCH_SECONDS * (2 ** min(fails - 1, 8))
    asked = cache.get("retry_after", 0)
    if isinstance(asked, (int, float)) and not isinstance(asked, bool):
        wait = max(wait, as_time(asked))
    return last + min(wait, MAX_SILENCE_SECONDS)


def credentials():
    """Returns (access_token, plan, expires_at).

    The file is the Windows equivalent of the login keychain item, with the
    same `claudeAiOauth` payload. Read on every poll rather than cached,
    because Claude Code rewrites it when it refreshes the token.

    `expires_at` is handed on exactly as found, milliseconds and all: what it
    means is token_expired()'s business, and that function is shared with the
    Mac build where this one is not. Nothing here validates it, because a
    reader that sanitises loses the difference between "absent" and "absurd",
    and the gate wants both to land in the same place anyway.
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
    return token, oauth.get("subscriptionType") or "", oauth.get("expiresAt")


class RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect from the usage endpoint.

    urllib carries the original headers to the new location, so a 3xx would
    replay the bearer token at whatever host it named, with no same-origin
    check of its own. This endpoint has no reason to redirect, so treat one
    as the anomaly it would be: returning None turns it into an HTTPError,
    which the caller already handles by backing off.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(RefuseRedirect)


def fetch(access_token):
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
        },
    )
    with OPENER.open(request, timeout=TIMEOUT) as response:
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
    # Same reasoning as as_percent(): this goes straight into arithmetic.
    if isinstance(minor, bool) or not isinstance(minor, (int, float)):
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


def as_percent(value):
    """A percentage we can compare and round, or None if it is neither.

    Numeric strings are accepted: the endpoint sends numbers today, but it is
    undocumented and already changed shape once this month, and "14" is a
    change we can still render correctly rather than one worth going blind
    over. Anything non-finite is refused -- NaN compares false against every
    threshold, and round() raises on both it and infinity.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = value
    else:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
    return number if math.isfinite(number) else None


def as_severity(value):
    """The server's own severity, or 'normal' when it did not send a usable
    one. Kept as a function because an unhashable value -- an object where a
    string was expected -- raises on the dict lookups it feeds rather than
    missing them."""
    return value if isinstance(value, str) else "normal"


def collect_limits(data):
    """Prefer the structured `limits` array; fall back to the flat fields.

    A row is kept only when its percentage is arithmetic. Everything
    downstream -- the colour thresholds, the digits drawn into the icon, the
    tooltip, the menu -- assumes that, and this is the last boundary where it
    can still be checked.
    """
    rows = []
    for entry in data.get("limits") or []:
        if not isinstance(entry, dict):
            continue
        percent = as_percent(entry.get("percent"))
        if percent is None:
            continue
        tag, label = meta_for(entry.get("kind", ""))
        rows.append(
            {
                "tag": tag,
                "label": label,
                "percent": percent,
                "severity": as_severity(entry.get("severity")),
                "resets_at": entry.get("resets_at"),
            }
        )
    if rows:
        return rows
    for key, kind in (("five_hour", "session"), ("seven_day", "weekly_all")):
        block = data.get(key)
        if not isinstance(block, dict):
            continue
        percent = as_percent(block.get("utilization"))
        if percent is None:
            continue
        tag, label = meta_for(kind)
        rows.append(
            {
                "tag": tag,
                "label": label,
                "percent": percent,
                "severity": "normal",
                "resets_at": block.get("resets_at"),
            }
        )
    return rows


def alert_code(percent, severity="normal"):
    """Green below the warn threshold, then amber, then red. The server's own
    severity can escalate early -- whichever trips first wins."""
    rank = SEVERITY_RANK.get(as_severity(severity), 0)
    if percent >= COLOR_ALERT_AT or rank >= 2:
        return RED
    if percent >= COLOR_WARN_AT or rank == 1:
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


def status_line(error, retry_at):
    """Recomputed on every render, so the wait counts down instead of showing
    the figure that happened to be true when the request failed. `retry_at`
    comes from next_attempt_at, the same expression the poll itself consults,
    so what this counts down to is the moment we actually go.

    Except for the credential: a tick past a refused token re-reads the file
    and sends nothing, so a countdown to it promised a retry that never went
    out, restarting every 90s for as long as the token stayed dead. Seen on
    2026-09-17. The message already names the fix, and the fix is noticed on
    the next tick without anyone clicking anything."""
    if not error:
        return None
    if error in (AUTH_ERROR, EXPIRED_ERROR):
        return error
    remaining = int(retry_at - time.time())
    if remaining <= 0:
        return f"{error}, retrying shortly"
    at = datetime.fromtimestamp(retry_at)
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
gdi32.CreateBitmap.argtypes = [
    ctypes.c_int, ctypes.c_int, w.UINT, w.UINT, w.LPVOID,
]
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
kernel32.BeginUpdateResourceW.restype = w.HANDLE
kernel32.BeginUpdateResourceW.argtypes = [w.LPCWSTR, w.BOOL]
kernel32.UpdateResourceW.restype = w.BOOL
kernel32.UpdateResourceW.argtypes = [
    w.HANDLE, w.LPCWSTR, w.LPCWSTR, w.WORD, w.LPVOID, w.DWORD,
]
kernel32.EndUpdateResourceW.restype = w.BOOL
kernel32.EndUpdateResourceW.argtypes = [w.HANDLE, w.BOOL]

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
        self.lock = threading.Lock()
        self.fetching = False
        # No startup reconciliation to do: a cold start is just another tick
        # whose cache happens to be old, and next_attempt_at reads that off the
        # clock like any other. What used to be handled here, and separately
        # again on every timer tick, is now one expression consulted at the
        # moment the question is asked.
        self.cache = sane_cache(read_json(CACHE_PATH), time.time())
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
        # Keep the login entry pointing at whatever we now run as. The first
        # start after the branded launcher appears is still under pythonw.exe,
        # and a stale command would keep starting us under the old name.
        if login_enabled() and login_value() != login_command():
            enable_login()

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

    def readd(self):
        """Remove and re-add the icon.

        The shell reads the taskbar-vs-overflow setting when an icon is
        added, not while it is live, so writing IsPromoted on its own changes
        nothing visible until something re-registers -- which otherwise means
        waiting for the next sign-in. Re-adding is instant and costs a single
        frame of flicker.
        """
        self.notify(NIM_DELETE)
        self.notify(NIM_ADD, icon=self.icon, tip=APP_NAME)
        self.refresh_display()

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
            return [("--", MUTED)]
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
        fetched_at = self.fetched_at()
        age = self.age()
        stale = age is None or age > ICON_STALE_AFTER_SECONDS
        out = []
        for r in chosen:
            if unreliable(r, fetched_at, age):
                # The window this figure counted has ended. Zero would be the
                # tempting guess, and the wrong one: it invites you to spend a
                # session you may have spent already. We do not know, so the
                # icon says so.
                out.append(("--", MUTED))
            elif stale:
                # Right shape, unknown vintage. Dropping the colour keeps a
                # figure we cannot vouch for from reading as a healthy green.
                out.append((str(round(r["percent"])), MUTED))
            else:
                out.append(
                    (
                        str(round(r["percent"])),
                        alert_code(r["percent"], r["severity"]) if use_colour else plain,
                    )
                )
        return out

    def tooltip(self):
        """szTip caps at 127 characters, so this is the compact shape rather
        than the menu's fully spelled-out one."""
        data = self.data()
        if not data:
            detail = status_line(*self.status())
            return sanitize(redact(detail or "no data yet"), limit=127)
        lines = []
        fetched_at = self.fetched_at()
        age = self.age()
        for row in collect_limits(data):
            if unreliable(row, fetched_at, age):
                lines.append(f"{row['label']}  --  awaiting refresh")
                continue
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

    def fetched_at(self):
        with self.lock:
            return self.cache.get("fetched_at", 0)

    def age(self):
        fetched_at = self.fetched_at()
        return time.time() - fetched_at if fetched_at else None

    def status(self):
        """The error and the moment we next ask, read together under one lock
        so a menu drawn while a fetch lands cannot pair one with the other."""
        with self.lock:
            return self.cache.get("error"), next_attempt_at(self.cache, time.time())

    def plan(self):
        with self.lock:
            return sanitize(self.cache.get("plan") or "", limit=24)

    def due_at(self):
        """When the next attempt is allowed, and what the menu counts down to.

        The same expression drives both, so the countdown on screen and the
        moment we actually ask can no longer disagree -- they were two stored
        values before, and a menu drawn while a fetch landed could pair one
        with the other."""
        with self.lock:
            return next_attempt_at(self.cache, time.time())

    def maybe_fetch(self, force=False):
        """Starts a fetch if one is due.

        Returns None when it started one, or a short reason when it did not,
        so a hand-driven refresh can say why nothing happened instead of
        looking ignored.
        """
        now = time.time()
        with self.lock:
            # The only bar a forced refresh cannot clear, and not a policy: a
            # second request cannot start while the first is still open
            # without racing it for the cache.
            if self.fetching:
                return "A refresh is already running."
            if now < next_attempt_at(self.cache, now, forced=force):
                # Only reachable unforced -- next_attempt_at returns `now` for
                # a person clicking, which is the whole point of that branch.
                return (
                    "Backing off after a failure."
                    if self.cache.get("fails", 0)
                    else "Polled recently."
                )
            self.fetching = True
            self.cache["last_attempt"] = now
            if force:
                # A person clicking is not a poll, and the failures of one are
                # not the other's to inherit. Without this, five failed clicks
                # during an outage walk the count up and leave the *automatic*
                # poll sitting out a wait it never earned -- the control that
                # exists to escape a wait, buying you a longer one.
                self.cache["fails"] = 0
                self.cache["retry_after"] = 0
        threading.Thread(target=self.fetch_now, args=(force,), daemon=True).start()
        return None

    def refresh_in_flight(self):
        """True while a fetch is open, which is the one moment clicking
        Refresh now cannot do anything. Reported as status rather than
        enforced silently: a balloon is not a sufficient answer on its own,
        since Windows suppresses notifications for an app it has no record of
        the user granting them to, which makes a declined click look
        identical to a broken one."""
        with self.lock:
            return self.fetching

    def fetch_now(self, forced=False):
        """Run one attempt on the worker thread and record what it did.

        `fetching` is cleared here and nowhere else, so the recording is a
        `finally` and even the failure handlers have a handler above them. An
        exception escaping this method would end the thread with the flag
        still set, after which maybe_fetch answers every poll and every click
        with "a refresh is already running" for the life of the process --
        a tray that looks alive and has quietly stopped asking.
        """
        now = time.time()
        update = {}
        try:
            update = self.attempt(now, forced)
        except Exception as err:  # noqa: BLE001 - a handler itself failed
            update = {
                "error": redact(err) or err.__class__.__name__,
                "retry_after": 0,
            }
        finally:
            with self.lock:
                # Stripped before the cache sees it: the cache holds evidence
                # about the service, and whether we bothered it this tick is
                # not that. It belongs in the log, which is about us.
                sent = update.pop("_sent", True)
                self.cache.update(update)
                self.fetching = False
                snapshot = dict(self.cache)
            write_json(CACHE_PATH, snapshot)
            # One line, here, because this is the only place every outcome
            # passes through. `next_in` is the figure that mattered in all
            # three faults: it is what the tray decided to do next, recorded
            # beside the evidence it decided from.
            log_event({
                "event": "fetch",
                "forced": forced,
                # False is the interesting line: a tick that decided the
                # credential on disk was already refused and said nothing.
                "sent": sent,
                "ok": "data" in update,
                "error": snapshot.get("error"),
                "fails": snapshot.get("fails"),
                "asked": snapshot.get("last_retry_after") if update.get("retry_after")
                else None,
                "next_in": round(next_attempt_at(snapshot, time.time()) - time.time()),
                "rows": rows_summary(snapshot.get("data")),
            })
            user32.PostMessageW(self.hwnd, WM_FETCHED, 0, 0)

    def attempt(self, now, forced=False):
        """One request, reduced to the cache fields it changes.

        What it records is evidence, never a decision: how many failures in a
        row, and how long the server said it wanted. When to ask next is read
        off that by next_attempt_at() at the moment the question is asked,
        which is what stops a wait outliving the reason for it.
        """
        try:
            access_token, plan, expires_at = credentials()
            fingerprint = token_fingerprint(access_token)
            expired = token_expired(expires_at, now)
            with self.lock:
                suspect = token_refused_before(self.cache, fingerprint)
                refused = rejected_token(self.cache, fingerprint, now, expires_at)
            if refused and not forced:
                # There is nothing here that could be answered, so nothing is
                # sent. Not a wait either: the tick comes round on the ordinary
                # interval and asks the same question of a file that may have
                # changed by then. A person clicking Refresh now overrules it,
                # because they may know something we do not.
                return {
                    "_sent": False,
                    "error": EXPIRED_ERROR if expired else AUTH_ERROR,
                    "retry_after": 0,
                }
            payload = fetch(access_token)
        except urllib.error.HTTPError as err:
            asked = retry_after_seconds(getattr(err, "headers", None))
            if err.code == 429 and asked <= 0:
                # Contention, not a fault. This endpoint's budget is shared
                # with Claude Code, which polls it too, so being turned away
                # is the ordinary outcome of two consumers rather than a sign
                # anything is wrong -- measured at roughly one refusal in four
                # even with nothing else of ours running. Escalating for it
                # turns a skipped poll into minutes of blindness, and saying
                # "rate limited" over figures fetched ninety seconds ago reads
                # as a fault when it is just a turn missed.
                #
                # A 429 that names a wait is different, and falls through.
                with self.lock:
                    stale = self.cache.get("fetched_at", 0) < now - STALE_AFTER_SECONDS
                return {
                    "error": "rate limited" if stale else None,
                    "retry_after": 0,
                }
            if suspect and asked > 0:
                # A lockout met while this credential is already suspect is,
                # on the evidence, the one our own 401s earned: three in a row
                # and this endpoint starts naming an hour. Polling through it
                # at the ordinary rate is how the saving above gets given back
                # -- it is the same refusal, so it paces the next probe like
                # one. Contention (the branch above) is not read this way: an
                # endpoint too busy to answer says nothing about who is asking.
                return {
                    "error": EXPIRED_ERROR if expired else AUTH_ERROR,
                    "auth_failed_for": fingerprint,
                    "auth_failed_at": now,
                    "retry_after": asked,
                    "last_retry_after": sanitize(
                        str((getattr(err, "headers", None) or {}).get("retry-after")), limit=64
                    ),
                    "last_retry_after_at": now,
                }
            if err.code == 401:
                # Evidence, not a count. `fails` feeds the doubling, and the
                # doubling is guesswork about a server that may yet recover;
                # there is nothing to guess at here, since we know what is
                # wrong and what will change it. Recording which token was
                # refused lets rejected_token() gate on that instead, exactly.
                #
                # 401 alone: 403 has never been seen from this endpoint, and
                # "your scopes are wrong" is not the same claim as "this token
                # is stale", so it keeps the ordinary treatment until it is.
                return {
                    "error": EXPIRED_ERROR if expired else AUTH_ERROR,
                    "auth_failed_for": fingerprint,
                    "auth_failed_at": now,
                    "retry_after": 0,
                }
            with self.lock:
                fails = self.cache.get("fails", 0) + 1
            return {
                "fails": fails,
                "error": (
                    "rate limited" if err.code == 429 else f"HTTP {err.code} from endpoint"
                ),
                # What it asked for, not when we will go. The ceiling is
                # applied where the decision is made, so this stays the honest
                # record of what was said.
                "retry_after": asked,
                # And the header verbatim, so the next argument about it can be
                # settled by reading the cache rather than reasoning about it:
                # a long lockout with the count still at one says the server
                # named a long wait, and nothing recorded whether that arrived
                # as seconds or as an HTTP-date. Never displayed.
                "last_retry_after": sanitize(
                    str((getattr(err, "headers", None) or {}).get("retry-after")), limit=64
                ),
                "last_retry_after_at": now,
            }
        except Exception as err:  # noqa: BLE001 - never let the tray icon break
            with self.lock:
                fails = self.cache.get("fails", 0) + 1
            return {
                # redact: an exception string could conceivably carry the token.
                "error": redact(err) or err.__class__.__name__,
                "fails": fails,
                # Nobody asked us for anything: the request never reached them.
                "retry_after": 0,
            }
        # A convenience for the statusline, and never a reason to call a good
        # fetch a failure: its own formatting can raise on spend data we did
        # not expect, and inside the try above that would discard a healthy
        # 200 and impose a backoff on it.
        try:
            write_statusline_sidecar(payload)
        except Exception:  # noqa: BLE001
            pass
        return {
            "data": payload,
            "plan": plan,
            "fetched_at": now,
            "error": None,
            "fails": 0,
            "retry_after": 0,
            # A 200 settles it: whatever was refused, this was not. The
            # fingerprint must not outlive the refusal it recorded, or the
            # next expiry is gated on a comparison against a dead token.
            "auth_failed_for": None,
            "auth_failed_at": 0,
        }

    # -- menu -------------------------------------------------------------

    def show_menu(self):
        menu = user32.CreatePopupMenu()
        data = self.data()
        rows = collect_limits(data or {})

        # Informational rows first, greyed so they read as status rather than
        # as things to click.
        width = max([len(r["label"]) for r in rows] + [len("Extra credits")])
        fetched_at = self.fetched_at()
        age = self.age()
        for row in rows:
            if rolled_over(row, fetched_at):
                when = parse_ts(row["resets_at"]).astimezone()
                label = (
                    f"{row['label']:<{width}}   {'--':>3}    window ended "
                    f"{when:%H:%M}, awaiting refresh"
                )
            elif unreliable(row, fetched_at, age):
                # No window to name, so nothing to date it against: say what
                # we actually last saw rather than dressing 0 up as current.
                label = (
                    f"{row['label']:<{width}}   {'--':>3}    "
                    "no window open when last seen"
                )
            else:
                label = f"{row['label']:<{width}}   {round(row['percent']):>3}%"
                reset = describe_reset(row["resets_at"])
                if reset:
                    label += f"   {reset}"
            user32.AppendMenuW(menu, MF_STRING | MF_DISABLED | MF_GRAYED, 0, label)

        spend = (data or {}).get("spend") if isinstance((data or {}).get("spend"), dict) else {}
        used, limit = money(spend.get("used")), money(spend.get("limit"))
        if spend.get("enabled") and used and limit:
            percent = as_percent(spend.get("percent"))
            suffix = f"   {round(percent)}% used" if percent is not None else ""
            user32.AppendMenuW(
                menu, MF_STRING | MF_DISABLED | MF_GRAYED, 0,
                f"{'Extra credits':<{width}}   {used} of {limit}{suffix}",
            )
        if not rows:
            user32.AppendMenuW(menu, MF_STRING | MF_DISABLED | MF_GRAYED, 0, "No usage data yet")

        # Provenance stays with the figures it qualifies -- "as of" means
        # nothing away from the number it dates -- and the two things you
        # would do about a figure you distrust, open the real page or fetch
        # again, close the same block. One uninterrupted section: it is all
        # about the current reading. Settings, which change behaviour rather
        # than report it, are the separate concern below the divider.
        if age is None:
            stamp = "No successful fetch yet"
        else:
            when = datetime.fromtimestamp(time.time() - age)
            suffix = f" ({compact_duration(int(age))} ago)" if age > STALE_AFTER_SECONDS else ""
            stamp = f"Percentages as of {when:%H:%M:%S}{suffix}"
        user32.AppendMenuW(menu, MF_STRING | MF_DISABLED | MF_GRAYED, 0, stamp)
        plan = self.plan()
        if plan:
            user32.AppendMenuW(menu, MF_STRING | MF_DISABLED | MF_GRAYED, 0, f"Plan: {plan.capitalize()}")
        detail = status_line(*self.status())
        if detail:
            user32.AppendMenuW(
                menu, MF_STRING | MF_DISABLED | MF_GRAYED, 0,
                sanitize(redact(f"⚠ {detail}"), limit=120),
            )
        user32.AppendMenuW(menu, MF_STRING, ID_SETTINGS, "Open usage settings")
        if self.refresh_in_flight():
            user32.AppendMenuW(menu, MF_STRING | MF_DISABLED | MF_GRAYED, 0, "Refreshing...")
        else:
            user32.AppendMenuW(menu, MF_STRING, ID_REFRESH, "Refresh now")

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
            refused = self.maybe_fetch(force=True)
            if refused:
                log_event({"event": "refresh refused", "why": refused})
                self.balloon(refused, warning=True)
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
            if set_pinned(want):
                # Remembered as well as written: the shell keys the setting to
                # the executable, so it is lost the first time we start under
                # a rebuilt launcher. Our own copy is what restores it.
                self.config["pinned"] = want
                write_json(CONFIG_PATH, self.config)
                self.readd()
            else:
                # No entry means the shell has not filed the icon yet, which
                # it does on its own schedule after a first run. Nothing here
                # can force that, so hand over to the tool that can.
                self.balloon(
                    "Windows has not registered this icon yet. Opening "
                    "taskbar settings -- switch on 'Claude Usage' there, or "
                    "drag the icon out of the overflow.",
                    warning=True,
                )
                os.startfile("ms-settings:taskbar")
        elif ident == ID_QUIT:
            user32.DestroyWindow(self.hwnd)
            return
        self.refresh_display()

    # -- message loop -----------------------------------------------------

    def on_message(self, hwnd, message, wparam, lparam):
        """Callback boundary. Nothing may escape from here.

        An exception raised inside a ctypes callback has nowhere to go: under
        pythonw there is no console to print it to, and the tray would just
        stop responding with no sign of why. Since the API shape is
        undocumented and could change under us, the one guarantee worth
        keeping is the one the rest of the file already makes -- degrade to a
        placeholder rather than die -- and this is the last place it can
        still be kept.
        """
        try:
            return self.dispatch(hwnd, message, wparam, lparam)
        except Exception:  # noqa: BLE001
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def dispatch(self, hwnd, message, wparam, lparam):
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
            # tick, while maybe_fetch enforces the network floor. There is
            # nothing to reconcile ahead of it any more -- the sleep check and
            # the rollover check both existed to lift a stored penalty, and
            # there is no longer one to lift.
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
        # Restore the taskbar pin if the shell has no record of it. Its
        # record is per executable, so the first start under a rebuilt
        # launcher begins in the overflow however deliberately it was pinned.
        if self.option("pinned", False) and not pinned() and set_pinned(True):
            self.readd()
        # What we inherited. The cold-boot fault was invisible precisely
        # because nothing recorded the count the cache handed back.
        now = time.time()
        with self.lock:
            log_event({
                "event": "start",
                "fails": self.cache.get("fails"),
                "last_attempt_age": round(now - self.cache["last_attempt"])
                if self.cache.get("last_attempt") else None,
                "fetched_age": round(now - self.cache["fetched_at"])
                if self.cache.get("fetched_at") else None,
                "unattended": unattended(self.cache, now),
                "next_in": round(next_attempt_at(self.cache, now) - now),
            })
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

    # Restart under our own name before anything else claims resources, so
    # that Windows lists us as "Claude Usage" rather than "Python".
    if relaunch_branded():
        return

    # A second instance would add a second icon and double the polling into
    # the rate limit that the whole throttle exists to avoid.
    kernel32.CreateMutexW(None, False, "ClaudeUsageTray.SingleInstance")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return
    Tray().run()


if __name__ == "__main__":
    main()
