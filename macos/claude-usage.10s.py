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
# touched at most once every 90 seconds and backs off exponentially on
# failure -- see MIN_FETCH_SECONDS and next_attempt_at().
#
# Self-invoking actions (driven by the dropdown):
#   --toggle-credits   flip the credits chip on/off in the menu bar
#   --toggle-color     flip per-segment ANSI colour in the menu bar
#   --toggle-login     add/remove the launch-at-login agent
#   --force-refresh    clear the local throttle so the next render fetches

import contextlib
import email.utils
import io
import json
import math
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

# The usage endpoint rate-limits aggressively, so the plugin's refresh cadence
# drives the *display* only -- percentages come from cache and the reset
# countdowns are recomputed locally on every tick, while the network is touched
# at most once every 90 seconds and backs off when told to.
#
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
# The title flags staleness early because a discreet marker costs nothing.
# Dropping the colour is louder, so it waits until the age is beyond
# explaining away by a missed poll or two.
UNCOLOURED_AFTER_SECONDS = 900
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
    cache["fails"] = 0
    cache["retry_after"] = 0
    save_cache(cache)
    log_event({"event": "forced refresh"})
    nudge_swiftbar()


def toggle_color():
    config = load_config()
    config["color"] = not config.get("color", True)
    save_config(config)
    nudge_swiftbar()


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
    for key in ("last_attempt", "fetched_at"):
        cache[key] = min(as_time(cache.get(key)), now)
    fails = cache.get("fails")
    if isinstance(fails, bool) or not isinstance(fails, int) or fails < 0:
        cache["fails"] = 0
    asked = cache.get("retry_after")
    if isinstance(asked, bool) or not isinstance(asked, (int, float)) or asked < 0:
        cache["retry_after"] = 0
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


def unusable(cache, now):
    """True when there is nothing on screen left to protect.

    Once a chip is showing `--` there is no figure a backoff can preserve, so
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
    downstream -- the colour thresholds, the chips, the dropdown -- assumes
    that, and this is the last boundary where it can still be checked.
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
    rank = SEVERITY_RANK.get(as_severity(severity), 0)
    if percent >= COLOR_ALERT_AT or rank >= 2:
        return RED
    if percent >= COLOR_WARN_AT or rank == 1:
        return AMBER
    return GREEN


def format_wait(seconds):
    """Always keep seconds visible so the countdown is seen to move."""
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds:02d}s"


def status_line(error, retry_at):
    """Recomputed every render, so the wait counts down instead of showing the
    figure that happened to be true when the request failed.

    The absolute time leads because SwiftBar does not redraw an already-open
    dropdown: the countdown is a snapshot from the last render and goes stale
    while you read it, whereas the clock time stays correct.

    `retry_at` comes from next_attempt_at, the same expression the poll
    itself consults, so what this counts down to is when we actually go.
    """
    if not error:
        return None
    remaining = int(retry_at - time.time())
    if remaining <= 0:
        return f"{error}, retrying on next refresh"
    at = datetime.fromtimestamp(retry_at)
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


def render(data, plan, config, age=None, error=None, retry_at=0, fetched_at=0):
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
                        as_percent(spend.get("percent")) or 0,
                        as_severity(spend.get("severity")),
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
        percent = as_percent(spend.get("percent"))
        suffix = f"   {round(percent)}% used" if percent is not None else ""
        row_color = SEVERITY_COLOR.get(as_severity(spend.get("severity")))
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
    line = status_line(error, retry_at)
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


def fail(detail, config, retry_at=0):
    print(f"Claude: ... | {DIM}")
    print("---")
    detail = status_line(detail, retry_at) or detail
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

    now = time.time()
    cache = sane_cache(load_cache(), now)
    # last_attempt gates the network; fetched_at records when data was last good.
    # Keeping them separate matters: a failed attempt must not make the cached
    # figures look fresh, and must not make them look infinitely stale either.
    #
    # One question, asked once, from the state as it stands. There is nothing to
    # reconcile first -- no penalty on disk to forgive after a wake, no stored
    # deadline to pull back down once the figures it was protecting have already
    # become a `--`. Both of those used to be separate passes over the cache
    # here, and both were places a case could go uncovered.
    retry_at = next_attempt_at(cache, now)

    if now >= retry_at:
        cache["last_attempt"] = now
        # Claim the slot on disk *before* the request, not after it. SwiftBar
        # starts a fresh process every tick and every one of them reads this
        # file: for as long as the claim lives only in our own memory, each
        # tick landing mid-request sees the old timestamp, agrees a poll is
        # due, and sends one of its own. The window is the keychain read plus
        # the request, both of which can run to TIMEOUT, against a tick every
        # ten seconds -- so a slow endpoint drew *more* traffic from us rather
        # than less, which is the opposite of what the whole throttle is for.
        save_cache(cache)
        try:
            access_token, plan = credentials()
            payload = fetch(access_token)
            cache.update(
                {
                    "data": payload,
                    "plan": plan,
                    "fetched_at": now,
                    "error": None,
                    "fails": 0,
                    "retry_after": 0,
                }
            )
            # A convenience for the statusline, and never a reason to call a
            # good fetch a failure: its own formatting can raise on spend data
            # we did not expect, and that would otherwise land in the handler
            # below -- discarding a healthy 200 and imposing a backoff on it.
            try:
                write_statusline_sidecar(payload)
            except Exception:  # noqa: BLE001
                pass
        except urllib.error.HTTPError as err:
            asked = retry_after_seconds(getattr(err, "headers", None))
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
                cache["retry_after"] = 0
            else:
                cache["fails"] = cache.get("fails", 0) + 1
                cache["error"] = (
                    "rate limited" if err.code == 429 else f"HTTP {err.code} from endpoint"
                )
                # What it asked for, not when we will go. The ceiling is
                # applied where the decision is made, so this stays the honest
                # record of what was said.
                cache["retry_after"] = asked
                # And the header verbatim, so the next argument about it can be
                # settled by reading the cache rather than reasoning about it:
                # a long lockout with the count still at one says the server
                # named a long wait, and nothing recorded whether that arrived
                # as seconds or as an HTTP-date. Never displayed.
                cache["last_retry_after"] = sanitize(
                    str((getattr(err, "headers", None) or {}).get("retry-after")),
                    limit=64,
                )
                cache["last_retry_after_at"] = now
        except Exception as err:  # noqa: BLE001 - never let the menu bar break
            cache["fails"] = cache.get("fails", 0) + 1
            # redact: an exception string could conceivably carry the token.
            cache["error"] = redact(err) or err.__class__.__name__
            # Nobody asked us for anything: the request never reached them.
            cache["retry_after"] = 0
        save_cache(cache)
        # The attempt changed the evidence, so the countdown has to be asked
        # again rather than reused from before it.
        retry_at = next_attempt_at(cache, now)
        # One line, here, because every outcome passes through it. `next_in` is
        # the figure that mattered in all three faults: what the plugin decided
        # to do next, recorded beside the evidence it decided from.
        log_event({
            "event": "fetch",
            "ok": cache.get("error") is None and cache.get("fetched_at") == now,
            "error": cache.get("error"),
            "fails": cache.get("fails"),
            "asked": cache.get("last_retry_after") if cache.get("retry_after") else None,
            "next_in": round(retry_at - now),
            "rows": rows_summary(cache.get("data")),
        })

    data = cache.get("data")
    if not isinstance(data, dict):
        fail(cache.get("error") or "no data yet", config, retry_at)
        return

    fetched_at = cache.get("fetched_at", 0)
    age = now - fetched_at if fetched_at else None
    # The same guarantee the Windows build makes at its callback boundary, and
    # for the same reason: the endpoint is undocumented and can change shape
    # under us, and a traceback in the menu bar is the one outcome worse than a
    # placeholder. Rendered into a buffer first so a failure part-way through
    # cannot leave half a menu on screen -- SwiftBar reads everything above the
    # first separator as the title, so partial output is not merely untidy.
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            render(
                data,
                cache.get("plan") or "",
                config,
                age,
                cache.get("error"),
                retry_at,
                fetched_at,
            )
    except Exception as err:  # noqa: BLE001 - never let the menu bar break
        fail(redact(err) or err.__class__.__name__, config, retry_at)
    else:
        sys.stdout.write(buffer.getvalue())


if __name__ == "__main__":
    main()
