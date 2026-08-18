# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Two independent single-file, stdlib-only Python programs that display Claude Code's session
and weekly usage limits: a SwiftBar plugin for the macOS menu bar (`macos/claude-usage.10s.py`)
and a Win32 notification-area app for Windows (`windows/claude-usage-tray.pyw`). Both read the
OAuth token Claude Code already stores and call the same undocumented endpoint,
`GET https://api.anthropic.com/api/oauth/usage`.

There is no build, no package manifest, no test suite, no CI, and no third-party dependency.
"Install" is copying one file into place.

## Commands

Run the macOS plugin standalone - it prints its SwiftBar output to stdout, so this is the
fastest way to see a change:

```sh
python3 macos/claude-usage.10s.py
```

Its menu actions are the same script re-invoked with a flag; each mutates state and exits:

```sh
python3 macos/claude-usage.10s.py --force-refresh   # also --toggle-credits, --toggle-login, --toggle-color
```

Run the Windows tray app (no console window; use `python` instead of `pythonw` if you want one):

```powershell
Start-Process pythonw.exe -ArgumentList '"windows\claude-usage-tray.pyw"'
```

Read the log first when diagnosing anything - it is always on, one JSON line per attempt, and
`next_in` (what the program decided to do next) is the field that matters:

```powershell
Get-Content "$env:LOCALAPPDATA\claude-usage-tray\log.jsonl" -Tail 20
```

```sh
tail -20 ~/.config/swiftbar-claude-usage/log.jsonl
```

Check parity of the duplicated policy layer after touching either build (see below). Bodies
should differ only in docstring wording:

```sh
extract () { awk -v fn="^def $2" '$0 ~ fn {p=1; print; next} p && /^(def |class |[A-Z_]+ =)/ {exit} p' "$1"; }
for f in retry_after_seconds sane_cache unattended rolled_over unanchored unreliable unusable next_attempt_at collect_limits; do
  echo "== $f"; diff <(extract macos/claude-usage.10s.py $f) <(extract windows/claude-usage-tray.pyw $f)
done
```

## Architecture

Each file is layered top to bottom in the same order: constants, sanitising, state I/O,
platform integration (login item, pinning, branding), rate-limit policy, fetch, formatting,
rendering, entry point. The platform integration and rendering layers are genuinely
platform-specific; everything between `retry_after_seconds` and `collect_limits` is not.

**The two builds deliberately share no code and duplicate that middle layer verbatim.** Each is
meant to be self-contained, and on macOS a shared module could not sit beside the plugin -
SwiftBar executes every file in its plugin directory. The duplicated set is
`retry_after_seconds`, `sane_cache`, `unattended`, `rolled_over`, `unanchored`, `unreliable`,
`unusable`, `next_attempt_at`, `collect_limits`, and the 429 branch in the fetch path. Same
names, same arguments, same order, so a missing edit shows as a body diff.
**A change to any of it has to be made twice.** The exception that has actually bitten is
behaviour with different names on each side - Windows forces a refresh through
`Tray.maybe_fetch`, macOS through `force_refresh` - which has no counterpart to diff and needs
checking by hand.

The two builds differ where the hosts do. SwiftBar re-executes the whole script every tick (the
`10s` in the filename), so the macOS build holds no process state at all: every tick reloads the
cache from disk, and the claim on the network slot is written to disk *before* the request so
concurrent ticks do not each send one. Windows is a single long-lived process with a `Tray`
class, a Win32 message loop, and fetches on a worker thread.

### Rate-limit policy - the core design

The endpoint serves about 5 calls then refuses for 300s, and the budget is shared with Claude
Code polling the same endpoint, so refusals are ordinary rather than faults. What follows from
that:

- **Nothing is stored about when to poll next.** `next_attempt_at(cache, now, forced)` is asked
  on every tick and derives the answer from the evidence in the cache. The cache holds evidence,
  never decisions, so no deadline can outlive the reason for it
- **One invariant replaces every special case**:
  `next_attempt_at(anything, now) - now <= MAX_SILENCE_SECONDS` (300s), for *any* cache
  including impossible ones - a jumped clock, a hand-edited file, fields of the wrong type. This
  replaced an accumulate-and-forgive backoff that failed four different ways in one day. Do not
  reintroduce an exception list; a new case should already be covered by the bound, and changes
  here want checking against randomised hostile caches rather than a list of remembered incidents
- **The floor is `MIN_FETCH_SECONDS` (90s)**, and only a hand-driven refresh (`forced=True`)
  ignores it, along with the server's `Retry-After`
- `Retry-After` is a floor and a hint, never an instruction: it has been observed returning `0`
  while still refusing, and asking for an hour then serving normally minutes later
- **Two kinds of 429.** `retry-after: 0` or absent is contention - one skipped poll, no failure
  count, and no error shown while the figures are still fresh. A 429 naming a real wait is a
  lockout and increments `fails` (doubling from 90s, capped by the 300s ceiling)
- `last_attempt` (gates the network) and `fetched_at` (when data was last good) are separate and
  must stay so: a failed attempt must not make cached figures look fresh, nor infinitely stale

### Never show a figure that cannot be stood behind

Rendering is local and cheap - the display re-renders every 10s, reset countdowns are recomputed
each render, the network is touched at most once per 90s. The staleness rules matter more than
the freshness ones:

- `rolled_over`: a usage window that ended while the API was unreachable counts a window nobody
  is in, so it becomes `--`, never a guessed `0`
- `unanchored`: a `0%` with a null reset time is plainly true while fresh and unknowable once
  stale, so a stale `0` becomes `--` too
- `unreliable` combines the two and is the single place both surfaces ask, so the icon/title and
  the menu can never disagree
- past `UNCOLOURED_AFTER_SECONDS` / `ICON_STALE_AFTER_SECONDS` (900s, same value, two names) the
  figure loses its colour, so a stale reading can never sit there looking healthy
- any failure degrades to a dim placeholder with the reason in the menu; a traceback reaching the
  menu bar or tray is the one outcome worse than no data. macOS renders into a buffer first, so a
  failure part-way through cannot emit half a menu

### Hardening constraints to preserve

- Everything from the API or the credential store passes `sanitize()` before display. SwiftBar
  parses `text | key=value` per line, so an unescaped `|` from server data could forge a `bash=`
  action; the Win32 side strips control characters instead, since a menu item's action is the
  integer it was created with, not something parsed back out of its label
- Redirects from the usage endpoint are refused (`RefuseRedirect`) - urllib would otherwise carry
  the `Authorization` header to whatever host a 3xx named
- Token-shaped strings are redacted from cached and displayed error text
- State files are written atomically with the PID in the temp name, mode `0600`
- No shell is ever invoked; `subprocess` is always given an argument list
- Limits are read generically from the API's `limits` array, so a cap this code has never heard
  of appears as an extra row with no change

Windows-specific mechanics with non-obvious reasons - digits rendered via a white-on-black GDI
buffer to recover an alpha channel, Tahoma below 20px with antialiasing off, the rebranded
`ClaudeUsage.exe` launcher, the `IsPromoted` pin registry dance, the undocumented `uxtheme`
ordinals 135/136 - are documented in `windows/README.md`. Read it before touching them.

## Conventions

- **Stdlib only, one file per platform.** No dependency, no helper module, no packaging
- Comments explain *why*, and the numbers here are measured rather than chosen. When changing a
  constant, change the evidence in its comment with it, or say plainly that it is now a guess
- British spelling in prose and in user-visible strings (`colour`)
- The READMEs are part of the deliverable. Behaviour changes to pacing, staleness or hardening
  belong in the root `README.md` as well as in the code
