# claude-usage

Your Claude session and weekly usage limits, always on screen, so you don't have to
open settings or run `/usage` to find out how much you have left.

**Two builds, one for macOS and one for Windows.**

| Platform | Where it lives | Looks like |
|---|---|---|
| **[macOS](macos/)** | the menu bar, via [SwiftBar](https://github.com/swiftbar/SwiftBar) | `S:41% (1h52m) W:23% (4d6h)` |
| **[Windows](windows/)** | the notification area | `41` drawn into the tray icon |

Each is a single file of stdlib Python you copy into place. Both read the OAuth token
Claude Code already stores, tint the figure green, amber or red by how much is left,
and open a menu with full reset times, extra-usage credit spend and their settings.

They differ because their hosts do. macOS gives a plugin arbitrary text in the menu
bar; Windows 11 gives you a 16x16 icon and nothing else, the deskband API that once
allowed text having been removed. So on Windows the percentage is drawn *as pixels*
into the icon, and the text moves to the tooltip and the menu.

## Requirements

- **Claude Code, signed in.** There is no separate API key to configure, but neither
  build works if Claude Code has never been signed in on that machine
- **Python 3**, no third-party packages
- macOS additionally needs SwiftBar. Windows needs nothing else

## Install

See **[macos/README.md](macos/README.md)** or **[windows/README.md](windows/README.md)**.

## How it works

Both call `GET https://api.anthropic.com/api/oauth/usage` with the OAuth token, the
same endpoint Claude Code's `/usage` command uses.

**That endpoint is internal and undocumented.** It works today and could change or be
withdrawn without notice. If that happens, both degrade to a dim placeholder with the
reason in the menu rather than breaking your menu bar or taskbar.

### Refresh: 10s on screen, at most once a minute on the wire

The endpoint rate-limits hard. Around a dozen calls in a few minutes earns an HTTP 429.
Its `retry-after` cannot be trusted in either direction: it has been observed returning
`0` while still refusing, and observed asking for a full hour and then serving the very
next request a minute later. It is treated as a hint with a ceiling, never as an
instruction. Rendering and fetching are therefore separate:

- the display re-renders every **10s**. This costs no network, and lets the retry
  countdown tick in seconds
- the network is touched **at most once a minute**, and only when not already backing
  off
- failures back off **exponentially**: 60s, doubling per consecutive failure. The
  ceiling depends on who failed. A server that answered gets **fifteen minutes**, which
  is also the most that will be taken from a `retry-after`. A failure on this machine -
  no DNS, no route, a timeout - never reached the server, so nobody asked us to stay
  away: those cap at **five minutes**
- **a hand-driven refresh ignores the backoff entirely**, the server's included. The
  backoff paces *polling*, and clicking Refresh now is overruling exactly that. It costs
  one request and is rate-limited only against itself, at once a minute. When it is
  inside that minute the menu item greys out and shows the countdown, rather than being
  offered and then declining
- recovery is **triggered, not just waited out**. Waking from sleep resets the penalty
  outright, and a usage window that ended while we were offline holds it down to the
  ordinary poll interval
- reset countdowns are recomputed **locally** on every render, so they stay accurate
  between polls
- percentages come from cache, and are flagged once genuinely stale. If a window rolled
  over while we could not reach the API, the figure counts a window nobody is in any
  more, so it is replaced by `--` rather than shown or guessed at as zero

### Hardening

- everything reaching the display from the API is filtered first. The sinks differ, so
  the filters differ: SwiftBar parses `text | key=value` per line and reads an
  unescaped `|` as the start of a parameter list, which is how a server-controlled
  string could otherwise forge a `bash=` action
- redirects from the usage endpoint are refused outright, because urllib would carry
  the `Authorization` header to whatever host a `3xx` named
- anything resembling a token is redacted from cached and displayed error text
- state files are written atomically, with the PID in the temp name, because a forced
  refresh and a scheduled poll can be in flight at once
- no shell is ever invoked; `subprocess` is always given an argument list

Limits are read generically from the API's `limits` array, so a cap this code has
never heard of appears as an extra row with no change.

## Layout

```
macos/     SwiftBar plugin, and its README
windows/   notification-area app, and its README
```

The two deliberately share no code. Each is meant to be a single self-contained file,
and on macOS a shared module could not sit beside the plugin anyway: SwiftBar treats
every file in its plugin directory as a plugin and would try to execute it. What is
duplicated is about two hundred lines of pure formatting helpers with no reason to
change.
