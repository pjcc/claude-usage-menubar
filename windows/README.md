# claude-usage-tray (Windows)

Your Claude session usage, drawn into the notification-area icon so it is visible
without opening settings or running `/usage`.

For what it is, how it refreshes and how it is hardened, see the
**[root README](../README.md)**. This file covers the Windows build only. The macOS
counterpart is in [`../macos/`](../macos/).

```
┌────┐
│ 41 │   session, 41% used
└────┘
```

The figure is tinted green, amber or red by how much is left. Hovering gives the
full breakdown; right-clicking gives reset times, extra-usage credit spend, and the
settings.

Only the presentation differs from the macOS build: the data layer, the throttle, the
backoff and the cache shape are the same.

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

New tray icons go into the overflow flyout behind the `^` chevron. Use **Always show
on taskbar** in the right-click menu, which moves it immediately and both ways.
Dragging it out of the flyout by hand, or Settings > Personalisation > Taskbar >
Other system tray icons, do the same thing.

The one case the menu item cannot handle is a first run. `Shell_NotifyIcon` has no
"always show me" flag, deliberately, so that installers cannot claim a permanent
slot. What Windows 11 has instead is one `IsPromoted` DWORD per icon under
`HKCU\Control Panel\NotifyIconSettings`, which is what the Settings toggle writes and
what the menu item sets. That entry does not exist until the shell has filed the icon,
which it does on its own schedule some minutes after the icon first appears. Until
then there is nothing to set, so the menu item says so and opens taskbar settings
instead.

Worth knowing if you are reading the code: the shell reads that setting when an icon
is *added*, not while it is live, so writing it alone changes nothing visible. The
icon is removed and re-added straight afterwards to force the re-read. Without that
the setting sits there until the next sign-in.

The setting is also keyed to the executable, so it would be lost the first time the
launcher below is rebuilt. It is recorded in `config.json` too and reapplied on
startup when the shell has no record of it.

## Why it runs as ClaudeUsage.exe

Windows names a tray icon in **Settings > Taskbar** after the *executable's*
`FileDescription`. Nothing the icon supplies changes that: the tooltip is ignored.
Run under `pythonw.exe` and you are listed as **Python**, indistinguishable from any
other Python tray app, and Task Manager's Startup tab says the same.

So on first run it copies the interpreter to
`%LOCALAPPDATA%\claude-usage-tray\ClaudeUsage.exe`, rewrites its version resource to
say *Claude Usage*, and restarts under it. A copied CPython cannot find its
installation by itself, but the two-line `pyvenv.cfg` beside it is all it needs: the
same mechanism a virtual environment uses to point at its base, without the
environment. It is about 90KB, and it is rebuilt if the interpreter it was copied
from moves or is upgraded.

All of it is stdlib: `BeginUpdateResourceW`/`UpdateResourceW` through `ctypes`, and a
`VS_VERSIONINFO` block assembled by hand. Nothing is compiled and the real
interpreter is never touched.

Set `"brand": false` in `config.json` to skip it and run under `pythonw.exe`.

## The menu

One uninterrupted first section, everything to do with the current reading:

| Row | |
|---|---|
| Session / Weekly / Extra credits | Percentages, full reset times, credit spend |
| Percentages as of, Plan | Provenance of the figures above |
| Open usage settings | The real page on claude.ai |
| Refresh now | Forces a poll, clearing the local throttle and our own backoff. Only a server-imposed wait survives, and a balloon says why if one does |

Then a divider, the settings below, and Quit at the foot.

| Toggle | Default | Effect |
|---|---|---|
| Show weekly in icon | off | Adds the tightest weekly limit as a second row. Off keeps the session figure at full height, which is roughly double the glyph size |
| Colour in icon | on | Off means no colour at all, not a different colour: the digits take whichever plain tone contrasts with the taskbar |
| Always show on taskbar | off | Moves it out of the overflow flyout and back. Reflects the same setting as Windows' own toggle, so changing it there shows up here |
| Open at login | on | An `HKCU\...\Run` value pointing at `pythonw.exe`. No console flash, no shortcut file |

The menu follows the system light/dark setting and re-themes itself if you change it
while it is running.

When the endpoint fails or changes shape the icon shows a dim `--` with the reason in
the menu, rather than disappearing. Fetches run on a worker thread, so a slow request
never freezes the tray.

The icon will not show a figure it cannot stand behind. After fifteen minutes without a
successful fetch the digits lose their colour, so a stale reading can never sit there
looking like a healthy green. If a usage window ended while the API was unreachable,
the number counts a window nobody is in any more and is replaced by a dim `--`; the
menu names the time it ended. Zero is not shown in its place, because a guess of zero
invites you to spend a session you may already have spent.

## Rendering digits into an icon

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

## Notes

Beyond the shared hardening in the [root README](../README.md), two things are
specific to this build:

- a named mutex prevents a second instance, which would add a second icon and double
  the polling into the rate limit the throttle exists to avoid
- server strings reaching a tooltip or menu label are stripped of control characters,
  which would otherwise truncate a tooltip or corrupt a label. There is no `|` to
  escape here: unlike SwiftBar, a Win32 menu item's action is the integer passed when
  it is created, not something parsed back out of its text

Also worth knowing:

- the popup menu is themed via two undocumented `uxtheme.dll` exports, available by
  ordinal only (135 `SetPreferredAppMode`, 136 `FlushMenuThemes`). It is how most apps
  get dark context menus, but it is undocumented all the same, so it is guarded: if a
  future build withdraws them the menu simply comes up light
- the icon is re-added if Explorer restarts

## Files

| Path | |
|---|---|
| `%LOCALAPPDATA%\claude-usage-tray\config.json` | Settings |
| `%LOCALAPPDATA%\claude-usage-tray\cache.json` | Cached usage, backoff state |
| `%LOCALAPPDATA%\claude-usage-tray\statusline` | One-line sidecar for a Claude Code statusline that wants the credit figure without a network call |
| `%LOCALAPPDATA%\claude-usage-tray\ClaudeUsage.exe`, `pyvenv.cfg` | The rebranded interpreter it runs under, see above |

Deleting any of them is safe; they are rebuilt on the next poll or the next start.

## Uninstall

Quit from the menu, then:

```powershell
Remove-Item -Recurse "$env:LOCALAPPDATA\claude-usage-tray"
Remove-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" ClaudeUsageTray
```

Then delete `claude-usage-tray.pyw`. Windows tidies the leftover
`NotifyIconSettings` entry itself.
