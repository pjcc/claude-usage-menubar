# claude-usage-tray (Windows)

The Windows counterpart to the macOS menu-bar plugin. Your Claude session usage,
drawn into the notification-area icon so it is visible without opening settings or
running `/usage`.

```
┌────┐
│ 41 │   session, 41% used
└────┘
```

The figure is tinted green, amber or red by how much is left. Hovering gives the
full breakdown; right-clicking gives reset times, extra-usage credit spend, and the
settings.

## Why it is an icon and not text

The macOS build writes `S:41% (1h52m) W:23% (4d06h)` straight into the menu bar.
Windows 11 has no equivalent: the notification area takes a 16x16 icon and nothing
else, and the deskband API that once allowed text in the taskbar was removed. So the
percentage is rendered *as pixels* into the icon, and the text you would have read in
the menu bar lives in the tooltip and the menu instead.

That is the only significant design difference. The data layer, the throttle, the
backoff and the cache shape are the same as the Mac build.

## Requirements

- **Windows 10 1809 or later.** Built and tested on Windows 11
- **Claude Code, signed in.** It reads the OAuth token Claude Code stores at
  `%USERPROFILE%\.claude\.credentials.json`. There is no separate API key to
  configure, but it will not work if Claude Code has never been signed in here
- **Python 3.** No third-party packages, no `pip install`, nothing to compile.
  The tray icon, its bitmap and the menu go through `ctypes` and Win32 directly

## Install

Copy `claude-usage-tray.pyw` anywhere you like and start it:

```powershell
Start-Process pythonw.exe -ArgumentList '"C:\path\to\claude-usage-tray.pyw"'
```

Double-clicking works too. The `.pyw` extension is what keeps a console window from
appearing.

There is no installer and nothing to uninstall beyond the three items listed at the
bottom. **Open at login is on by default** and is set up on first run, so it starts
with Windows from then on.

## Getting it out of the overflow

New tray icons go into the overflow flyout behind the `^` chevron. Three ways out,
in order of reliability:

1. **Drag it** from the flyout onto the taskbar. Instant, and it stays
2. **Settings > Personalisation > Taskbar > Other system tray icons**, and switch
   *Claude Usage* on
3. **Always show on taskbar** in the right-click menu

Option 3 is a convenience, and it comes with caveats worth knowing.
`Shell_NotifyIcon` has no "always show me" flag, deliberately, so that installers
cannot claim a permanent slot. What Windows 11 does have is one `IsPromoted` DWORD
per icon under `HKCU\Control Panel\NotifyIconSettings`, which is exactly what the
Settings toggle writes, and that is what the menu item sets.

Two things follow from Explorer owning that key:

- the entry does not exist until the shell has filed the icon, which can take a few
  minutes after first run. Before then the menu item reports that and does nothing
- Explorer caches the setting, so it may not take effect until you sign out and back
  in

So the honest answer is that the program *can* do it, but option 1 or 2 gets you
there in one action and this one might not. It is there for scripting a fresh
machine, where signing in again is happening anyway.

## The menu

| Row | |
|---|---|
| Session / Weekly / Extra credits | Percentages, full reset times, credit spend |
| **Refresh now** | Forces a poll, bypassing the local throttle but still respecting a server-imposed backoff |
| Plan, Percentages as of | Provenance of the figures above |

| Toggle | Default | Effect |
|---|---|---|
| Show weekly in icon | off | Adds the tightest weekly limit as a second row. Off keeps the session figure at full height, which is roughly double the glyph size |
| Colour in icon | on | Off means no colour at all, not a different colour: the digits take whichever plain tone contrasts with the taskbar |
| Always show on taskbar | off | See above |
| Open at login | on | An `HKCU\...\Run` value pointing at `pythonw.exe`. No console flash, no shortcut file |

The menu follows the system light/dark setting and re-themes itself if you change it
while it is running.

## How it works

It calls `GET https://api.anthropic.com/api/oauth/usage` with the OAuth token, which
is the same endpoint Claude Code's `/usage` command uses.

**That endpoint is internal and undocumented.** It works today and could change or be
withdrawn without notice. If that happens the icon shows a dim `--` with the reason in
the menu rather than disappearing.

### Refresh: 10s on screen, at most once a minute on the wire

The endpoint rate-limits hard. Around a dozen calls in a few minutes earns an HTTP
429, and it has been observed returning `retry-after: 0` while still refusing, so that
header cannot be trusted as guidance on its own. Rendering and fetching are therefore
separate:

- the icon and tooltip re-render every **10s**. This is display only, costs no
  network, and lets the retry countdown tick in seconds
- the network is touched **at most once a minute**, and only when not already backing
  off. Fetches run on a worker thread so a slow request never freezes the tray
- failures back off **exponentially**: 60s, doubling per consecutive failure, capped
  at one hour. A server `retry-after` is honoured only when it asks for longer
- reset countdowns are recomputed **locally** on every render, so they stay accurate
  between polls
- percentages come from cache, and are marked stale in the tooltip once genuinely old

### Rendering digits into an icon

GDI text drawing does not write an alpha channel, so painting coloured text onto a
transparent bitmap produces fully transparent, invisible glyphs. The way round it is
to draw in white on black, then read the buffer back and use each pixel's brightness
as its alpha while substituting the colour actually wanted. Antialiased edges survive
that, which is what keeps two digits legible at 16px.

Two choices that were measured rather than assumed:

- **Tahoma, not Segoe UI.** Side by side at 16px, Tahoma renders noticeably larger for
  the same fitted height, and its hinted bitmaps stay unambiguous where Segoe UI's
  thin bold strokes turn `8` and `9` to mush
- **Antialiasing off below 20px.** At tray sizes the grey fringe costs more legibility
  than the smooth edge buys. Above that it is switched back on, where chunky pixels
  would be the visible flaw instead

The glyphs are fitted to the digits' *ink* height rather than the font's line box,
which carries ascent, descent and internal leading that no digit occupies. Sizing to
the line box wastes roughly a third of a 16px icon.

### Hardening

- everything reaching the tooltip or a menu label from the API passes through
  `sanitize()`, which strips control characters that would otherwise truncate a
  tooltip or corrupt a label
- anything resembling a token is redacted from cached and displayed error text
- state files are written atomically, with the PID in the temp name, because a forced
  refresh and the scheduled poll can be in flight at once
- a named mutex prevents a second instance, which would add a second icon and double
  the polling into the rate limit the throttle exists to avoid
- no shell is ever invoked

### Notes

- the popup menu is themed via two undocumented `uxtheme.dll` exports, available by
  ordinal only (135 `SetPreferredAppMode`, 136 `FlushMenuThemes`). It is how most apps
  get dark context menus, but it is undocumented all the same, so it is guarded: if a
  future build withdraws them the menu simply comes up light
- limits are read generically from the API's `limits` array, so model-specific caps
  such as a weekly Opus limit appear as extra rows with no code change
- the icon is re-added if Explorer restarts

## Files

| Path | |
|---|---|
| `%LOCALAPPDATA%\claude-usage-tray\config.json` | Settings |
| `%LOCALAPPDATA%\claude-usage-tray\cache.json` | Cached usage, backoff state |
| `%LOCALAPPDATA%\claude-usage-tray\statusline` | One-line sidecar for a Claude Code statusline that wants the credit figure without a network call |

Deleting any of them is safe; they are rebuilt on the next poll.

## Uninstall

Quit from the menu, then:

```powershell
Remove-Item -Recurse "$env:LOCALAPPDATA\claude-usage-tray"
Remove-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" ClaudeUsageTray
```

Then delete `claude-usage-tray.pyw`. Windows tidies the leftover
`NotifyIconSettings` entry itself.
