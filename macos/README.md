# claude-usage-menubar (macOS)

Your Claude session and weekly usage limits, always visible in the macOS menu bar,
so you don't have to open settings or run `/usage` to find out how much you have left.

For what it is, how it refreshes and how it is hardened, see the
**[root README](../README.md)**. This file covers the macOS build only. The Windows
counterpart is in [`../windows/`](../windows/).

```
S:41% (1h52m) W:23% (4d6h)
└ session, 41% used, resets in 1h52m
                     └ weekly, 23% used, resets in 4d6h
```

The percentage is tinted green, amber or red by how much is left. The tag and countdown
keep the system colour so the whole thing stays readable in light and dark mode.

Clicking it opens a dropdown with full reset times, extra-usage credit spend, and
three toggles.

## Requirements

- **macOS** with [SwiftBar](https://github.com/swiftbar/SwiftBar), a menu-bar plugin host
- **Claude Code, signed in.** The plugin reads its OAuth token from your login keychain.
  There is no separate API key to configure, but it will not work if Claude Code has
  never been signed in on the machine you install it on.
- **Python 3.** The system `python3` from the Command Line Tools is fine; no third-party
  packages are used.

## Install

### 1. SwiftBar

With Homebrew:

```sh
brew install --cask swiftbar
```

Without Homebrew, download it directly. It is signed and notarized by Ameba Labs, and
the `spctl` line below verifies that before you install it:

```sh
cd "$(mktemp -d)"
curl -sL -o SwiftBar.zip \
  "$(curl -sL https://api.github.com/repos/swiftbar/SwiftBar/releases/latest \
     | python3 -c 'import json,sys; print(next(a["browser_download_url"] for a in json.load(sys.stdin)["assets"] if a["name"].endswith(".zip")))')"
ditto -x -k SwiftBar.zip .
spctl -a -vvv -t exec SwiftBar.app     # expect: accepted / Notarized Developer ID
ditto SwiftBar.app /Applications/SwiftBar.app
```

### 2. The plugin

```sh
mkdir -p ~/.swiftbar
cp claude-usage.10s.py ~/.swiftbar/
chmod +x ~/.swiftbar/claude-usage.10s.py
defaults write com.ameba.SwiftBar PluginDirectory -string "$HOME/.swiftbar"
open /Applications/SwiftBar.app
```

### 3. Approve the keychain prompt

The first time it polls, macOS asks whether SwiftBar may read the
`Claude Code-credentials` keychain item. Choose **Always Allow**, since plain *Allow*
will re-prompt on every poll.

If the menu bar item never appears, this prompt is the usual reason. It can hide behind
other windows.

## Dropdown options

| Toggle | Default | Effect |
|---|---|---|
| Show credits in menu bar | off | Appends extra-usage spend, for example `$5/50` |
| Colour in menu bar | on | Off means no colour at all, not a different colour |
| Open at login | on | Manages a LaunchAgent at `~/Library/LaunchAgents/com.ameba.SwiftBar.plist` |

**Refresh now** forces a poll, ignoring every backoff including the server's, with no
floor of its own.

## Files

| Path | |
|---|---|
| `~/.config/swiftbar-claude-usage/config.json` | Settings |
| ` ~/.config/swiftbar-claude-usage/cache.json` | Cached usage, and the evidence pacing is worked out from |
| `~/.config/swiftbar-claude-usage/log.jsonl` | One line per attempt, capped at 256KB. `tail -20` it when something looks stuck: `next_in` is what the plugin decided to do next, `asked` is the `Retry-After` header verbatim |
| `~/.config/swiftbar-claude-usage/statusline` | One-line sidecar for a Claude Code statusline that wants the credit figure without a network call. Format below |

All `0600`. Deleting any of them is safe, as they are rebuilt on the next poll - with
one exception: `statusline` is rebuilt only while the account has extra-usage credits,
because its absence is meaningful (see below). They live outside the plugin folder on
purpose: SwiftBar treats every file in there as a plugin and would try to execute them.

## The statusline sidecar

It exists so a Claude Code statusline can show the credit figure **without calling the
endpoint itself**. A statusline re-renders on every message, and the endpoint
rate-limits hard enough that a second caller would starve this one. So the traffic
stays here and the number goes out through a file: this writes, anything else reads,
and nothing reads back.

One space-separated line, rewritten on every successful poll:

```
2529 4000 GBP 2 63 1787066531
```

| Field | |
|---|---|
| `used_minor` | Credit spent, in minor units - `2529` is £25.29 |
| `limit_minor` | The extra-usage cap, same units |
| `currency` | ISO code, or `?` if the response carried none |
| `exponent` | Minor units per major unit as a power of ten: `2` for GBP/USD/EUR, `0` for JPY |
| `percent` | Percent of the cap used, as the API reports it - **not** recomputed from the two amounts, so do not assume they agree |
| `epoch` | Unix seconds at which the line was written |

The figures come straight off the `spend` block of the usage response. That makes them
money already drawn down, account-wide, and in the account's own currency - not a
per-session estimate and not converted.

Two things a reader has to handle:

- **Absence is meaningful.** The file is removed, not zeroed, when the account has no
  extra-usage credits, so a missing file means the feature is off and the correct
  rendering is nothing at all. It is likewise simply absent on a machine that has never
  run this, so a reader can look for it unconditionally and degrade to showing nothing
- **It only moves while the plugin is running.** Nothing else refreshes it, so check
  `epoch` before trusting the figure rather than assuming it is current. Roughly fifteen
  minutes is a sane bar for a reader; for comparison this build marks its own reading
  "(figures stale)" at `STALE_AFTER_SECONDS`, 270s

The Windows build writes the identical line, at
`%LOCALAPPDATA%\claude-usage-tray\statusline`, so a reader that tries both paths in turn
covers either machine with one script.

**Treat the field order and units as fixed.** A reader can validate each field and still
not tell a reordered line from a plausible one, so a change here breaks it silently
rather than loudly.

## Refresh rate

SwiftBar takes the re-render interval from the filename, so to change it rename the
file, for example `claude-usage.30s.py`. Only the display rate changes; the
90-second network floor is enforced in code, not by the filename.

## Uninstall

```sh
rm ~/.swiftbar/claude-usage.10s.py
rm -rf ~/.config/swiftbar-claude-usage
rm -f ~/Library/LaunchAgents/com.ameba.SwiftBar.plist
```

## Notes

- SwiftBar has its own "Launch at Login" preference. Don't use it *and* the dropdown
  toggle, as two mechanisms would race to start it.
- SwiftBar does not support 24-bit colour. It drops the sequence silently rather than
  falling back, so the palette is xterm-256 indices only.
