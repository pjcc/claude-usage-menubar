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
| Refresh now | Forces a poll, ignoring every backoff including the server's, with no floor of its own. Reads `Refreshing...` and is unclickable only while a request is actually open |

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

A session nobody is in is reported as `0%` with no reset time at all, which is why the
same rule has to cover it separately: with no window there is no clock to compare
against, so nothing looks rolled over however old it gets. Fresh, that `0` is simply
true and is shown. Once it is stale it becomes `--` as well, since a session started in
the meantime would have opened a window this reading knows nothing about. The menu says
`no window open when last seen`.

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
| `%LOCALAPPDATA%\claude-usage-tray\cache.json` | Cached usage, and the evidence pacing is worked out from |
| `%LOCALAPPDATA%\claude-usage-tray\log.jsonl` | One line per attempt, capped at 256KB. See below |
| `%LOCALAPPDATA%\claude-usage-tray\statusline` | One-line sidecar for a Claude Code statusline that wants the credit figure without a network call. Format below |
| `%LOCALAPPDATA%\claude-usage-tray\ClaudeUsage.exe`, `pyvenv.cfg` | The rebranded interpreter it runs under, see above |

Deleting any of them is safe; they are rebuilt on the next poll or the next start. The
one exception is `statusline`, rebuilt only while the account has extra-usage credits,
because its absence is meaningful - see below.

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
  run this, which is what makes the chip safe to add unconditionally
- **It only moves while the tray app is running.** Nothing else refreshes it, so check
  `epoch` before trusting the figure rather than assuming it is current. For reference,
  this build marks its own reading "(figures stale)" at `STALE_AFTER_SECONDS`, 270s,
  and stops trusting the icon digits at `ICON_STALE_AFTER_SECONDS`, 900s; the dotfiles
  statusline flags the sidecar at the latter

The macOS build writes the identical line, at
`~/.config/swiftbar-claude-usage/statusline`. The statusline in
[pjcc/dotfiles](https://github.com/pjcc/dotfiles) reads both paths in turn, so one
script covers either machine. **Changing the field order or units breaks it silently** -
it validates each field but cannot tell a reordered line from a plausible one.

One trap worth naming for anyone reading this path from a shell script: the literal
`${VAR//\\//}` does **not** turn backslashes into forward slashes. It parses as
"delete every forward slash", and on a Windows path that is a harmless no-op, so it
looks correct right up until it mangles a POSIX one. The pattern has to be quoted:
`${var//"$bs"//}`.

## The log

Every attempt writes one JSON line to `log.jsonl`, always on. It is not a debug switch
because the faults worth diagnosing here are days apart and never reproducible on
demand, so a log you have to have enabled in advance is a log you will not have. At a
poll every 90s it is a few hundred KB a week, and it is truncated to its last 192KB
once it passes 256KB.

```
{"at": "2026-08-17 23:04:02", "event": "fetch", "forced": false, "ok": true,
 "error": null, "fails": 0, "asked": null, "next_in": 87, "rows": {"S": 33, "W": 9}}
```

`next_in` is the field that matters: it is what the tray decided to do next, recorded
beside the evidence it decided from. `asked` is the `Retry-After` header verbatim,
which is the thing that was missing when a fifteen-minute lockout appeared with the
failure count still at one and there was no way to tell what the server had actually
sent. A `start` line records what the cache handed back on launch, which is where a
count surviving a power cycle would show up.

To read the last few:

```powershell
Get-Content "$env:LOCALAPPDATA\claude-usage-tray\log.jsonl" -Tail 20
```

## Uninstall

Quit from the menu, then:

```powershell
Remove-Item -Recurse "$env:LOCALAPPDATA\claude-usage-tray"
Remove-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" ClaudeUsageTray
```

Then delete `claude-usage-tray.pyw`. Windows tidies the leftover
`NotifyIconSettings` entry itself.
