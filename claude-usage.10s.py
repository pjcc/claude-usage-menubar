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

# Menu-bar colour thresholds, by percent of a limit consumed. Only the "NN%"
# itself is tinted -- the tag and the countdown stay in the system colour so
# they remain readable in both light and dark mode.
#
# Thresholds and colours deliberately mirror the Claude Code statusline, so the
# same figure reads the same in both places.
COLOR_WARN_AT = 50
COLOR_ALERT_AT = 80

# (xterm-256 index, rgb).
#
# Two things learned the hard way here:
#   1. SwiftBar does NOT support 24-bit truecolor (`38;2;r;g;b`). It drops the
#      sequence silently, so the text renders with no colour at all rather than
#      falling back. 256-index (`38;5;n`) is the only form that works.
#   2. The statusline's green is index 42 = rgb(0,215,135), whose blue channel is
#      135. In a terminal that reads green; in the menu bar it reads teal/blue.
#      Index 40 is the same brightness with no blue in it at all.
GREEN = (40, (0, 215, 0))
AMBER = (220, (255, 215, 0))
RED = (196, (255, 0, 0))

# The usage endpoint rate-limits aggressively (observed: HTTP 429 with
# retry-after ~275s after roughly a dozen calls). So the plugin's refresh
# cadence drives the *display* only -- percentages come from cache and the
# reset countdowns are recomputed locally on every tick, while the network is
# touched at most once a minute and backs off when told to.
MIN_FETCH_SECONDS = 60
STALE_AFTER_SECONDS = 150
DEFAULT_BACKOFF_SECONDS = 300
MAX_BACKOFF_SECONDS = 3600

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
    """Atomic, owner-only. Atomic so a crash mid-write can't leave corrupt JSON
    behind; 0600 because the cache holds usage and spend figures."""
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
            json.dump(payload, handle, indent=2)
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
    Deliberately leaves backoff_until alone -- if the server said wait, we wait."""
    cache = load_cache()
    cache["last_attempt"] = 0
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

    Doubles per consecutive failure. The server's retry-after is honoured only
    when it asks for *longer*: it has been observed returning `retry-after: 0`
    while still refusing, and obeying that literally means retrying immediately
    and forever, which is what keeps the limit tripped.
    """
    wait = MIN_FETCH_SECONDS * (2 ** min(fails - 1, 8))
    if err is not None:
        try:
            wait = max(wait, int(err.headers.get("retry-after") or 0))
        except (TypeError, ValueError, AttributeError):
            pass
    return min(wait, MAX_BACKOFF_SECONDS)


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


def ansi_wrap(text, colour, mode="256"):
    if colour is None:
        return text
    index, (red, green, blue) = colour
    if mode == "256":
        return f"\033[38;5;{index}m{text}\033[0m"
    return f"\033[38;2;{red};{green};{blue}m{text}\033[0m"


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


def render(data, plan, config, age=None, error=None, backoff_until=0):
    rows = collect_limits(data)
    spend = data.get("spend") if isinstance(data.get("spend"), dict) else {}
    show_credits = bool(config.get("show_credits", False))

    # Menu bar: "S:46% (3h39m) W:7% (5d0h)". Each limit's percentage is tinted
    # on its own, so you can see at a glance *which* one is the tight one.
    use_color = bool(config.get("color", True))
    # 256-index only: SwiftBar silently drops truecolor sequences.
    mode = config.get("color_mode", "256")
    chips = []
    for row in rows:
        percent = f"{round(row['percent'])}%"
        if use_color:
            percent = ansi_wrap(
                percent, alert_code(row["percent"], row["severity"]), mode
            )
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
                    mode,
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
    print(
        f"Refresh now | bash=\"{SELF}\" param1=--force-refresh "
        f"terminal=false refresh=true"
    )
    print(f"Open usage settings | href={SETTINGS_URL}")


def toggle_line(label, enabled, action):
    # ☑/☐ rather than "✓"/spaces: a matched glyph pair keeps the labels aligned
    # in the menu's proportional font.
    mark = "☑" if enabled else "☐"
    print(
        f"{mark} {label} | bash=\"{SELF}\" "
        f"param1={action} terminal=false refresh=true"
    )


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
    print(
        f"Refresh now | bash=\"{SELF}\" param1=--force-refresh "
        f"terminal=false refresh=true"
    )
    print(f"Open usage settings | href={SETTINGS_URL}")


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
                    "error": None,
                    "fails": 0,
                }
            )
        except urllib.error.HTTPError as err:
            fails = cache.get("fails", 0) + 1
            cache["fails"] = fails
            cache["error"] = (
                "rate limited" if err.code == 429 else f"HTTP {err.code} from endpoint"
            )
            cache["backoff_until"] = now + backoff_for(fails, err)
        except Exception as err:  # noqa: BLE001 - never let the menu bar break
            fails = cache.get("fails", 0) + 1
            cache["fails"] = fails
            # redact: an exception string could conceivably carry the token.
            cache["error"] = redact(err) or err.__class__.__name__
            cache["backoff_until"] = now + backoff_for(fails)
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
    )


if __name__ == "__main__":
    main()
