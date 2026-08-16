#!/usr/bin/env python3
# <swiftbar.title>Claude Usage</swiftbar.title>
# <swiftbar.version>2.1</swiftbar.version>
# <swiftbar.desc>Claude session/weekly limit usage in the menu bar.</swiftbar.desc>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
#
# Reads the OAuth token Claude Code stores in the login keychain and asks the
# same usage endpoint that Claude Code's /usage command uses. That endpoint is
# internal and undocumented -- if its shape changes this plugin degrades to a
# dim placeholder rather than breaking the menu bar.
#
# The filename sets how often SwiftBar re-renders (10s). That drives the
# *display* only, so the retry countdown ticks in seconds. The network is
# touched at most once a minute and backs off exponentially on failure --
# see MIN_FETCH_SECONDS and backoff_for().
#
# Self-invoking actions (driven by the dropdown):
#   --toggle-credits   flip the credits chip on/off in the menu bar
#   --toggle-color     flip per-segment ANSI colour in the menu bar
#   --toggle-login     add/remove the launch-at-login agent
#   --force-refresh    clear the local throttle so the next render fetches

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

KEYCHAIN_SERVICE = "Claude Code-credentials"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
SETTINGS_URL = "https://claude.ai/settings/usage"
TIMEOUT = 10

SELF = os.path.abspath(__file__)
# State lives outside the plugin folder on purpose: SwiftBar treats every file
# in there as a plugin, and "claude-usage.config.json" matches its
# name.interval.ext convention -- it would try to execute it.
STATE_DIR = os.path.expanduser("~/.config/swiftbar-claude-usage")
CONFIG_PATH = os.path.join(STATE_DIR, "config.json")
CACHE_PATH = os.path.join(STATE_DIR, "cache.json")
# Sidecar for the Claude Code statusline: one space-separated line it can read
# with the bash `read` builtin. The statusline avoids subprocesses on purpose,
# so it should not have to parse the pretty-printed cache JSON -- and it must
# never hit the network itself, since it re-renders on every message.
STATUSLINE_PATH = os.path.join(STATE_DIR, "statusline")

# Menu-bar colour thresholds, by percent of a limit consumed. Only the "NN%"
# itself is tinted -- the tag and the countdown stay in the system colour so
# they remain readable in both light and dark mode.
#
# Thresholds and colours deliberately mirror the Claude Code statusline, so the
# same figure reads the same in both places.
COLOR_WARN_AT = 50
COLOR_ALERT_AT = 80

# xterm-256 indices. Two things learned the hard way here:
#   1. SwiftBar does NOT support 24-bit truecolor (`38;2;r;g;b`). It drops the
#      sequence silently, so the text renders with no colour at all rather than
#      falling back. 256-index (`38;5;n`) is the only form that works, which is
#      why there is no rgb pair here to fall back to.
#   2. The statusline's green is index 42 = rgb(0,215,135), whose blue channel is
#      135. In a terminal that reads green; in the menu bar it reads teal/blue.
#      Index 40 is the same brightness with no blue in it at all.
GREEN = 40
AMBER = 220
RED = 196

# The usage endpoint rate-limits aggressively (observed: HTTP 429 with
# retry-after ~275s after roughly a dozen calls). So the plugin's refresh
# cadence drives the *display* only -- percentages come from cache and the
# reset countdowns are recomputed locally on every tick, while the network is
# touched at most once a minute and backs off when told to.
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
# Roughly two missed polls, which is what this has always meant.
STALE_AFTER_SECONDS = 240
# The title flags staleness early because a discreet marker costs nothing.
# Dropping the colour is louder, so it waits until the age is beyond
# explaining away by a missed poll or two.
UNCOLOURED_AFTER_SECONDS = 900
# Two ceilings, because two different things are being waited out. The longer
# one is for a server that told us to go away. Name resolution, routing and
# timeouts fail on this machine without the request ever leaving it -- nobody
# asked us to stay away, and capping those the same way is how a laptop that
# slept through a network change sits all afternoon on figures from before it.
#
# The long ceiling is also the most we will take from a retry-after, because
# that header is untrustworthy in both directions: observed returning 0 while
# still refusing, and observed asking for a full hour and then serving the
# very next request a minute later.
MAX_BACKOFF_SECONDS = 900
MAX_LOCAL_BACKOFF_SECONDS = 300

LOGIN_LABEL = "com.ameba.SwiftBar"
LOGIN_PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LOGIN_LABEL}.plist")
SWIFTBAR_BINARY = "/Applications/SwiftBar.app/Contents/MacOS/SwiftBar"

MONO = "font=Menlo size=12"
DIM = "color=#8a8a8e"

# Dropdown row colour, as hex because SwiftBar's own `color=` takes hex directly.
# Same values as the menu-bar palette above. Normal rows are left untinted so
# they keep the system colour and stay readable in both light and dark mode.
SEVERITY_COLOR = {"warning": "#FFD700", "critical": "#FF0000", "severe": "#FF0000"}
SEVERITY_RANK = {"normal": 0, "warning": 1, "critical": 2, "severe": 2}

# SwiftBar parses "text | key=value" per line, so a "|", a newline, or an ANSI
# escape arriving in server data could forge menu params or inject colour.
# Everything that reaches stdout from the API or the keychain goes through
# sanitize() first.
TOKEN_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_-]+")

# Short tag for the menu bar, long label for the dropdown.
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
    """Make an untrusted string safe to emit as SwiftBar output.

    isprintable() drops newlines, control characters and ESC (so no ANSI can be
    smuggled in), and "|" is removed because SwiftBar reads it as the start of
    the parameter list.
    """
    if not isinstance(value, str):
        return ""
    return "".join(c for c in value if c.isprintable() and c != "|")[:limit]


def redact(value):
    """Keep credentials out of anything we cache or display."""
    return TOKEN_PATTERN.sub("sk-ant-***", str(value))


# --------------------------------------------------------------------------
# config + self-modification
# --------------------------------------------------------------------------


def write_json(path, payload):
    write_atomic(path, json.dumps(payload, indent=2))


def write_atomic(path, text):
    """Atomic, owner-only. Atomic so a crash mid-write can't leave a truncated
    file behind; 0600 because this holds usage and spend figures."""
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        os.chmod(STATE_DIR, 0o700)
    except OSError:
        pass
    # PID in the temp name: SwiftBar's scheduled refresh and a dropdown action
    # can run concurrently, and a shared temp path lets them interleave writes
    # into one file -- which produced a corrupt cache during testing.
    temporary = f"{path}.{os.getpid()}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            try:
                os.remove(temporary)
            except OSError:
                pass


def load_config():
    try:
        with open(CONFIG_PATH) as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(config):
    write_json(CONFIG_PATH, config)


def load_cache():
    try:
        with open(CACHE_PATH) as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(cache):
    try:
        write_json(CACHE_PATH, cache)
    except OSError:
        pass


def write_statusline_sidecar(data):
    """used limit currency exponent percent epoch -- removed when the account has
    no extra-usage credits, so a stale chip can't outlive the feature."""
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


def nudge_swiftbar():
    # -g keeps focus where it was instead of pulling it to SwiftBar.
    subprocess.run(
        ["open", "-g", "swiftbar://refreshallplugins"],
        check=False,
        capture_output=True,
    )


def toggle_credits():
    config = load_config()
    config["show_credits"] = not config.get("show_credits", False)
    save_config(config)
    nudge_swiftbar()


def login_enabled():
    return os.path.exists(LOGIN_PLIST)


def swiftbar_running():
    return (
        subprocess.run(
            ["pgrep", "-x", "SwiftBar"], capture_output=True
        ).returncode
        == 0
    )


def enable_login():
    """A LaunchAgent, rather than SwiftBar's own preference, because that one
    isn't reachable from a plugin.

    The plist alone is what makes it start at next login, so bootstrap only
    when SwiftBar isn't already up -- otherwise launchd starts a *second*
    instance and you get duplicate menu bar items."""
    os.makedirs(os.path.dirname(LOGIN_PLIST), exist_ok=True)
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LOGIN_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{SWIFTBAR_BINARY}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>ProcessType</key>
    <string>Interactive</string>
</dict>
</plist>
"""
    with open(LOGIN_PLIST, "w") as handle:
        handle.write(plist)
    if not swiftbar_running():
        subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", LOGIN_PLIST],
            check=False,
            capture_output=True,
        )


def disable_login():
    """Remove the plist only -- deliberately NOT `launchctl bootout`.

    When SwiftBar is running *as* this agent's process, booting the agent out
    kills SwiftBar itself: switching the setting off would quit the app. With
    the plist gone, launchd keeps the current process alive for this session
    and simply finds nothing to start at next login, which is what the toggle
    is meant to mean."""
    try:
        os.remove(LOGIN_PLIST)
    except OSError:
        pass


def toggle_login():
    disable_login() if login_enabled() else enable_login()
    nudge_swiftbar()


def force_refresh():
    """Clear the local throttle so the next render fetches. Clears last_attempt
    rather than fetched_at: fetched_at means 'when we last had good data' and
    zeroing it made a failed forced refresh look infinitely stale forever.

    The backoff goes with it, the server's included, and no floor replaces
    it. All of that paces our *polling*, and the person clicking is overruling
    exactly that. Deferring to the server here is what let an hour-long
    retry-after -- from an endpoint that served the next request a minute
    later -- disable the one control that exists to get past it.
    """
    cache = load_cache()
    cache["last_attempt"] = 0
    cache["backoff_until"] = 0
    cache["server_backoff_until"] = 0
    cache["fails"] = 0
    save_cache(cache)
    nudge_swiftbar()


def toggle_color():
    config = load_config()
    config["color"] = not config.get("color", True)
    save_config(config)
    nudge_swiftbar()


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------


def backoff_for(fails, err=None):
    """Seconds to wait after a failed attempt.

    Doubles per consecutive failure, but the ceiling depends on who failed.
    `err` present means the server answered, so it gets the long ceiling and
    its retry-after is honoured -- though only when it asks for *longer*: it
    has been observed returning `retry-after: 0` while still refusing, and
    obeying that literally means retrying immediately and forever, which is
    what keeps the limit tripped.

    Everything else failed on this machine without the request ever leaving
    it, so those get a ceiling measured in minutes.
    """
    wait = MIN_FETCH_SECONDS * (2 ** min(fails - 1, 8))
    if err is None:
        return min(wait, MAX_LOCAL_BACKOFF_SECONDS)
    try:
        wait = max(wait, int(err.headers.get("retry-after") or 0))
    except (TypeError, ValueError, AttributeError):
        pass
    return min(wait, MAX_BACKOFF_SECONDS)


def rolled_over(row, fetched_at):
    """True when this row's window ended after our last good fetch.

    Not merely 'the reset time has passed': the server can hand back a window
    that expired moments ago and that reading is still the current truth. It
    is only when the rollover happened while we were blind that the number on
    screen counts a window nobody is in any more.
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
    since gone stale. Kept in one place because the title and the dropdown must
    agree -- a `--` in the menu bar beside a number below it is worse than
    either alone."""
    if rolled_over(row, fetched_at):
        return True
    return unanchored(row) and (age is None or age > UNCOLOURED_AFTER_SECONDS)


def credentials():
    """Returns (access_token, plan). Claude Code refreshes the token in place,
    so this is read fresh on every poll rather than cached."""
    out = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    if out.returncode != 0:
        raise RuntimeError("no keychain entry (is Claude Code signed in?)")
    oauth = json.loads(out.stdout)["claudeAiOauth"]
    return oauth["accessToken"], oauth.get("subscriptionType") or ""


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
    sanitized before it can reach stdout."""
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
    """'resets in 3h 39m (15:40)' -- relative first, since that's what you act on."""
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

    compact drops the minor units for whole amounts, to keep the menu bar short.
    """
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


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def ansi_wrap(text, index):
    if index is None:
        return text
    return f"\033[38;5;{index}m{text}\033[0m"


def alert_code(percent, severity="normal"):
    """Green below the warn threshold, then amber, then red. The server's own
    severity can escalate early -- whichever trips first wins."""
    if percent >= COLOR_ALERT_AT or SEVERITY_RANK.get(severity, 0) >= 2:
        return RED
    if percent >= COLOR_WARN_AT or SEVERITY_RANK.get(severity, 0) == 1:
        return AMBER
    return GREEN


def format_wait(seconds):
    """Always keep seconds visible so the countdown is seen to move."""
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds:02d}s"


def status_line(error, backoff_until):
    """Recomputed every render, so the wait counts down instead of showing the
    figure that happened to be true when the request failed.

    The absolute time leads because SwiftBar does not redraw an already-open
    dropdown: the countdown is a snapshot from the last render and goes stale
    while you read it, whereas the clock time stays correct.
    """
    if not error:
        return None
    remaining = int(backoff_until - time.time())
    if remaining <= 0:
        return f"{error}, retrying on next refresh"
    at = datetime.fromtimestamp(backoff_until)
    return f"{error}, retrying at {at:%H:%M:%S} (in {format_wait(remaining)})"


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


def render(data, plan, config, age=None, error=None, backoff_until=0, fetched_at=0):
    rows = collect_limits(data)
    spend = data.get("spend") if isinstance(data.get("spend"), dict) else {}
    show_credits = bool(config.get("show_credits", False))

    # Menu bar: "S:46% (3h39m) W:7% (5d0h)". Each limit's percentage is tinted
    # on its own, so you can see at a glance *which* one is the tight one.
    # Past a certain age the tint comes off: a figure we cannot vouch for must
    # not be able to read as a healthy green.
    use_color = bool(config.get("color", True)) and not (
        age is None or age > UNCOLOURED_AFTER_SECONDS
    )
    chips = []
    for row in rows:
        if unreliable(row, fetched_at, age):
            # The window this figure counted has ended. Zero would be the
            # tempting guess, and the wrong one: it invites you to spend a
            # session you may have spent already. We do not know, so say so.
            chips.append(f"{row['tag']}:--")
            continue
        percent = f"{round(row['percent'])}%"
        if use_color:
            percent = ansi_wrap(percent, alert_code(row["percent"], row["severity"]))
        chip = f"{row['tag']}:{percent}"
        span = compact_duration(seconds_until(row["resets_at"]))
        if span:
            chip = f"{chip} ({span})"
        chips.append(chip)
    if show_credits and spend.get("enabled"):
        chip = credit_chip(spend)
        if chip:
            if use_color:
                chip = ansi_wrap(
                    chip,
                    alert_code(
                        spend.get("percent") or 0, spend.get("severity") or "normal"
                    ),
                )
            chips.append(chip)

    title = " ".join(chips) if chips else "Claude: ..."
    # Countdowns stay accurate regardless of age (they're computed locally), so
    # only the percentages go stale -- flag it discreetly rather than shouting.
    if age is None or age > STALE_AFTER_SECONDS:
        title += " ⋯"

    # Off means off: no ANSI, no line colour, just the system text colour.
    print(f"{title} | ansi=true" if use_color else title)
    print("---")

    width = max([len(r["label"]) for r in rows] + [len("Extra credits")])

    for row in rows:
        if rolled_over(row, fetched_at):
            when = parse_ts(row["resets_at"]).astimezone()
            line = (
                f"{row['label']:<{width}}  {'--':>3}   window ended "
                f"{when:%H:%M}, awaiting refresh"
            )
            print(f"{line} | {MONO}")
            continue
        if unreliable(row, fetched_at, age):
            # No window to name, so nothing to date it against: say what we
            # actually last saw rather than dressing 0 up as current.
            line = (
                f"{row['label']:<{width}}  {'--':>3}   "
                "no window open when last seen"
            )
            print(f"{line} | {MONO}")
            continue
        line = f"{row['label']:<{width}}  {round(row['percent']):>3}%"
        reset = describe_reset(row["resets_at"])
        if reset:
            line += f"   {reset}"
        row_color = SEVERITY_COLOR.get(row["severity"])
        print(f"{line} | {MONO}" + (f" color={row_color}" if row_color else ""))

    used, limit = money(spend.get("used")), money(spend.get("limit"))
    if spend.get("enabled") and used and limit:
        percent = spend.get("percent")
        suffix = f"   {round(percent)}% used" if percent is not None else ""
        row_color = SEVERITY_COLOR.get(spend.get("severity") or "normal")
        print(
            f"{'Extra credits':<{width}}  {used} of {limit}{suffix} | {MONO}"
            + (f" color={row_color}" if row_color else "")
        )

    print("---")
    print_controls(config)
    print("---")
    plan = sanitize(plan, limit=24)
    if plan:
        print(f"Plan: {plan.capitalize()} | {DIM}")
    if age is None:
        print(f"No successful fetch yet, showing cached figures | {DIM}")
    else:
        stamp = datetime.fromtimestamp(time.time() - age)
        suffix = (
            f" ({compact_duration(int(age))} ago)" if age > STALE_AFTER_SECONDS else ""
        )
        print(f"Percentages as of {stamp:%H:%M:%S}{suffix} | {DIM}")
    line = status_line(error, backoff_until)
    if line:
        print(f"⚠ {sanitize(redact(line), limit=120)} | {DIM}")
    print_footer()


def toggle_line(label, enabled, action):
    # ☑/☐ rather than "✓"/spaces: a matched glyph pair keeps the labels aligned
    # in the menu's proportional font.
    mark = "☑" if enabled else "☐"
    print(
        f"{mark} {label} | bash=\"{SELF}\" "
        f"param1={action} terminal=false refresh=true"
    )


def print_footer():
    """Closes both the normal dropdown and the failed one, which is the point:
    when it has failed is exactly when you want to refresh or go and look."""
    print(
        f"Refresh now | bash=\"{SELF}\" param1=--force-refresh "
        f"terminal=false refresh=true"
    )
    print(f"Open usage settings | href={SETTINGS_URL}")


def print_controls(config):
    toggle_line(
        "Show credits in menu bar", config.get("show_credits", False), "--toggle-credits"
    )
    toggle_line("Colour in menu bar", config.get("color", True), "--toggle-color")
    toggle_line("Open at login", login_enabled(), "--toggle-login")


def fail(detail, config, backoff_until=0):
    print(f"Claude: ... | {DIM}")
    print("---")
    detail = status_line(detail, backoff_until) or detail
    print(f"Couldn't read usage: {sanitize(redact(detail), limit=120)} | {MONO}")
    print("---")
    print_controls(config)
    print("---")
    print_footer()


def main():
    args = sys.argv[1:]
    if args:
        if args[0] == "--toggle-credits":
            toggle_credits()
        elif args[0] == "--toggle-login":
            toggle_login()
        elif args[0] == "--toggle-color":
            toggle_color()
        elif args[0] == "--force-refresh":
            force_refresh()
        return

    config = load_config()
    # "Open at login" defaults on, so install the agent the first time we run
    # and record that we did -- after which the toggle is yours to own.
    if not config.get("login_initialised"):
        if not login_enabled():
            enable_login()
        config["login_initialised"] = True
        save_config(config)

    cache = load_cache()
    now = time.time()
    # last_attempt gates the network; fetched_at records when data was last good.
    # Keeping them separate matters: a failed attempt must not make the cached
    # figures look fresh, and must not make them look infinitely stale either.
    # A figure we can no longer stand behind is the one case where sitting out
    # a backoff achieves nothing: it counts a window nobody is in, or none we
    # ever saw, and no amount of waiting improves it. Hold our own penalty --
    # never the server's -- down to the ordinary poll interval until a fetch
    # lands.
    cached = cache.get("data") if isinstance(cache.get("data"), dict) else {}
    cached_at = cache.get("fetched_at", 0)
    cached_age = now - cached_at if cached_at else None
    if now >= cache.get("server_backoff_until", 0) and any(
        unreliable(row, cached_at, cached_age) for row in collect_limits(cached)
    ):
        cache["backoff_until"] = min(
            cache.get("backoff_until", 0),
            cache.get("last_attempt", 0) + MIN_FETCH_SECONDS,
        )
    due = now - cache.get("last_attempt", 0) >= MIN_FETCH_SECONDS
    blocked = now < cache.get("backoff_until", 0)

    if due and not blocked:
        cache["last_attempt"] = now
        try:
            access_token, plan = credentials()
            cache.update(
                {
                    "data": fetch(access_token),
                    "plan": plan,
                    "fetched_at": now,
                    "backoff_until": 0,
                    "server_backoff_until": 0,
                    "error": None,
                    "fails": 0,
                }
            )
            write_statusline_sidecar(cache["data"])
        except urllib.error.HTTPError as err:
            headers = getattr(err, "headers", None)
            try:
                asked = int((headers or {}).get("retry-after") or 0)
            except (TypeError, ValueError):
                asked = 0
            if err.code == 429 and asked <= 0:
                # Contention, not a fault. This endpoint's budget is shared
                # with Claude Code, which polls it too, so being turned away is
                # the ordinary outcome of two consumers rather than a sign that
                # anything is wrong -- measured at roughly one refusal in four
                # even with nothing else of ours running. Escalating for it
                # turns a skipped poll into minutes of blindness, and saying
                # "rate limited" over figures fetched ninety seconds ago reads
                # as a fault when it is just a turn missed.
                #
                # A 429 that names a wait is different, and falls through.
                fresh = cache.get("fetched_at", 0) >= now - STALE_AFTER_SECONDS
                cache["error"] = None if fresh else "rate limited"
                cache["backoff_until"] = now + MIN_FETCH_SECONDS
                cache["server_backoff_until"] = 0
            else:
                fails = cache.get("fails", 0) + 1
                wait = backoff_for(fails, err)
                cache["fails"] = fails
                cache["error"] = (
                    "rate limited" if err.code == 429 else f"HTTP {err.code} from endpoint"
                )
                cache["backoff_until"] = now + wait
                # Only an answer naming a wait is the server asking for room;
                # anything else is a failure we merely recorded.
                cache["server_backoff_until"] = now + wait if asked > 0 else 0
        except Exception as err:  # noqa: BLE001 - never let the menu bar break
            fails = cache.get("fails", 0) + 1
            cache["fails"] = fails
            # redact: an exception string could conceivably carry the token.
            cache["error"] = redact(err) or err.__class__.__name__
            cache["backoff_until"] = now + backoff_for(fails)
            cache["server_backoff_until"] = 0
        save_cache(cache)

    data = cache.get("data")
    if not isinstance(data, dict):
        fail(cache.get("error") or "no data yet", config, cache.get("backoff_until", 0))
        return

    fetched_at = cache.get("fetched_at", 0)
    age = now - fetched_at if fetched_at else None
    render(
        data,
        cache.get("plan") or "",
        config,
        age,
        cache.get("error"),
        cache.get("backoff_until", 0),
        fetched_at,
    )


if __name__ == "__main__":
    main()
