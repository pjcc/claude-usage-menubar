# claude-usage-menubar

Your Claude session and weekly usage limits, always visible in the macOS menu bar,
so you don't have to open settings or run `/usage` to find out how much you have left.

On Windows, see **[README-windows.md](README-windows.md)** and
`claude-usage-tray.pyw`, which puts the same figures in the notification area.

```
S:41% (1h52m) W:23% (4d06h)
└ session, 41% used, resets in 1h52m
                     └ weekly, 23% used, resets in 4d06h
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
will re-prompt every minute.

If the menu bar item never appears, this prompt is the usual reason. It can hide behind
other windows.

## Dropdown options

| Toggle | Default | Effect |
|---|---|---|
| Show credits in menu bar | off | Appends extra-usage spend, for example `$5/50` |
| Colour in menu bar | on | Off means no colour at all, not a different colour |
| Open at login | on | Manages a LaunchAgent at `~/Library/LaunchAgents/com.ameba.SwiftBar.plist` |

**Refresh now** forces a poll, bypassing the local throttle but still respecting a
server-imposed backoff.

Settings live in `~/.config/swiftbar-claude-usage/config.json`, with cached data
alongside it in `cache.json`. Both are `0600`. Deleting either is safe, as they are
rebuilt on the next poll.

## How it works

It calls `GET https://api.anthropic.com/api/oauth/usage` with the OAuth token from the
keychain, which is the same endpoint Claude Code's `/usage` command uses.

**That endpoint is internal and undocumented.** It works today and could change or be
withdrawn without notice. If that happens, the plugin shows a dim `Claude: ...` with the
reason in the dropdown rather than breaking your menu bar.

### Refresh: 10s on screen, at most once a minute on the wire

The endpoint rate-limits hard. Around a dozen calls in a few minutes earns an HTTP 429,
and it has been observed returning `retry-after: 0` while still refusing, so that header
cannot be trusted as guidance on its own. Rendering and fetching are therefore separate:

- SwiftBar re-renders every **10s**, set by the filename. This is display only, costs no
  network, and lets the retry countdown tick in seconds.
- the network is touched **at most once a minute**, and only when not already backing off.
- failures back off **exponentially**: 60s, doubling per consecutive failure, capped at one
  hour. A server `retry-after` is honoured only when it asks for longer than that.
- reset countdowns are recomputed **locally** on every render, so they stay accurate
  between polls.
- percentages come from cache, and are marked with a trailing character plus an
  "as of" time once genuinely stale.

To change the render rate, rename the file, for example `claude-usage.30s.py`. Only the
display rate changes; the once-a-minute network floor is enforced in code, not by the
filename.

### Hardening

- Everything reaching stdout from the API or keychain passes through `sanitize()`.
  SwiftBar parses `text | key=value` per line, so an unescaped `|`, newline, or ANSI
  escape in server data could otherwise forge menu items or `bash=` parameters.
- Anything resembling a token is redacted from cached and displayed error text.
- State files are written atomically, with the PID in the temp name, because SwiftBar's
  scheduled refresh and a dropdown action can run concurrently.
- No shell is ever invoked. `subprocess` is always given an argument list.

## Uninstall

```sh
rm ~/.swiftbar/claude-usage.10s.py
rm -rf ~/.config/swiftbar-claude-usage
rm -f ~/Library/LaunchAgents/com.ameba.SwiftBar.plist
```

## Notes

- SwiftBar has its own "Launch at Login" preference. Don't use it *and* the dropdown
  toggle, as two mechanisms would race to start it.
- Limits are read generically from the API's `limits` array, so model-specific caps
  such as a weekly Opus limit appear as extra rows with no code change.
