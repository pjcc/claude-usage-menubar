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

### Refresh: 10s on screen, at most once every 90s on the wire

**The budget is shared with Claude Code**, which polls the same endpoint for its own
limit display. Measured by stopping the tray, sitting completely idle for six minutes
and then making a single call: it was **refused**, `retry-after: 0`. Nothing else could
have spent that budget. Polling at 90s as the only caller under our control still drew a
refusal roughly one time in four.

So being turned away is the ordinary outcome of two consumers, not a fault, and **no
polling interval avoids it**. What matters is that it costs a skipped poll rather than
an escalating penalty - see the `retry-after: 0` handling below.

The endpoint rate-limits hard. Measured on 2026-08-07, twice, with consistent results:

- it serves **5 calls** and refuses the 6th with HTTP 429
- the refusal lasts **300s**, and `Retry-After: 300` was accurate to within 4 seconds
- requests made while refused **do not extend** the block
- the budget does **not** trickle back a call at a time. Probes at +30s, +60s and +91s
  were all still refused, so overshooting costs the remainder of the window outright
- so the sustained ceiling is about **one call per minute**

That last figure is the one that matters, because it is also what this used to poll at.
At 60s the entire budget went on polling, so one extra call from anywhere - a manual
refresh, a restart - was enough to lock it out, and a 429 every few hours was routine.
Polling is now every **90s**, which spends three or four of the five and leaves the rest
for you. Nothing shorter is safe: 60s is the ceiling itself, not a margin beneath it.

`Retry-After` is honoured but never trusted, because it is not always that honest: the
same endpoint has been seen returning `0` while still refusing, and - after several
hours of repeated tripping - asking for a full hour and then serving normally 17 minutes
later. It is treated as a hint with a ceiling.

Its value is also what separates the two kinds of 429. **`retry-after: 0` or absent is
contention** - somebody else got there first - and costs exactly one skipped poll, with
no escalation and no error shown while the figures on screen are still fresh. **A 429
naming a real wait** is a genuine lockout, and gets the full treatment. Rendering and
fetching are therefore separate:

- the display re-renders every **10s**. This costs no network, and lets the retry
  countdown tick in seconds
- the network is touched **at most once every 90 seconds**, and only when not already
  backing off
- failures back off **exponentially** from the poll interval: 90s, doubling per
  consecutive failure, and a `retry-after` is honoured when it asks for longer, in
  either of the two forms RFC 9110 allows for it
- **but never past five minutes, whatever the reason.** That is the one ceiling, and it
  is the number it is because five minutes is the longest this endpoint has ever
  actually stayed shut - measured twice, a refusal lasts 300s. A wait longer than that
  can only be waiting for something that has already ended. The header has been seen
  asking for a full hour and then answering a probe normally within the minute, three
  separate times, so it is treated as advice with a bound rather than an instruction
- what makes bounding it cheap is the other measured fact: **requests made while refused
  do not extend the refusal.** Asking again costs a refusal we can afford; not asking
  costs the entire point of the thing
- **a hand-driven refresh ignores every one of those**, the server's `retry-after`
  included, and has no floor of its own. All of it paces *polling*, and clicking Refresh
  now is overruling exactly that. The only moment the item is withheld is while a
  request is genuinely open, where it reads `Refreshing...` - a statement of fact, not
  a restriction
- **nothing is stored about when to go next.** One function, `next_attempt_at`, is asked
  on every tick and works it out from the state as it stands: how long since the last
  attempt, how many failures in a row, what the server asked for, and whether what is on
  screen is already a `--`. The cache holds that evidence and no decisions, so there is
  no deadline that can outlive the reason for it

This last point is the design, and it is worth saying why. The obvious way to write
this - accumulate a penalty, then forgive it in the cases that deserve forgiveness -
went wrong four times in one day. A failure count survived a power cycle. A wait the
server asked for outlived the figures it was protecting. Every fix was correct, and
each one left the next uncovered case waiting, because a list of exceptions can never
be finished.

So the exceptions were replaced by a property:

```
next_attempt_at(anything, now) - now  <=  MAX_SILENCE_SECONDS
```

It holds for every possible cache, including ones no code path can produce - a clock
that jumped, a hand-edited file, a field of the wrong type entirely - and it is checked
that way, against a few hundred thousand randomised and deliberately hostile states
rather than against a list of remembered incidents. **The worst thing that can happen is
now five minutes of a stale figure, by construction rather than by having thought of
it.** A wake, a cold boot, a rollover and an hour-long `retry-after` all stop being
special cases and become the same bound.

#### The one refusal that waiting cannot fix

A 401 is different in kind from all of the above. The bound is about *when* to ask
again, and it is right that nothing can push an answer more than five minutes away. But
a 401 does not say "ask later". It says this token is not acceptable, and the token is a
file on this machine, so asking again with the same file changes nothing.

That is not academic. The token Claude Code stores lives about eight hours and is only
rewritten when Claude Code runs, so a machine left to itself overnight expires it. On
2026-09-03 the tray then spent nine hours going round this loop:

- three 401s in a row, roughly four minutes apart
- at which point the endpoint stops answering 401 and starts answering **429 with
  `Retry-After: 3600`**
- the ceiling above correctly refuses to sit out an hour, so it polls through the whole
  hour at 90s
- the hour ends, the token is still expired, and it starts again

Around 340 requests, every one of them certain to fail, against a budget shared with
Claude Code itself. The display stayed honest throughout - `--` for the session, the
weekly figure held uncoloured, the reason in the menu - but the reason it gave was
"rate limited", which was true and useless. The thing to do was open Claude Code.

So there is a second gate, and it turns on the credential rather than the clock:

- a 401 records a **fingerprint** of the token that was refused - a truncated SHA-256,
  never the token itself - as evidence in the cache, and no failure count, because
  doubling is guesswork about a server that might recover and there is nothing here to
  guess at
- while the token on disk is still that token there is nothing worth sending, so nothing
  is sent. This is not a longer wait; it is a different question, asked before the wait,
  which is why `next_attempt_at` is untouched and its bound still holds word for word
- **recovery is not polled for.** It *is* the file changing, and the fingerprint of the
  file is read every tick for nothing - so a token Claude Code has just rewritten goes
  out on the next ordinary poll, within 90 seconds rather than up to an hour
- a probe every 15 minutes backstops the case where the refusal was the server's mistake
  rather than the token's, and a lockout met while the credential is already suspect
  paces the next probe the same way, being the same refusal
- a hand-driven refresh ignores the gate, as it ignores everything else
- the menu says `token rejected, open Claude Code to refresh it`, because unlike every
  other failure here there is something to be done about it, and it is not ours to do

The same nine hours, replayed against the same endpoint behaviour: **36 requests instead
of 359**, and the recovery arrives 90 seconds after the token is rewritten instead of
whenever the next hour-long lockout happens to lapse.

- **the same expression drives the countdown you see.** "retrying at 22:37:04" is not a
  stored moment that might disagree with the code; it is that code, asked again
- reset countdowns are recomputed **locally** on every render, so they stay accurate
  between polls
- percentages come from cache, and are flagged once genuinely stale. If a window rolled
  over while we could not reach the API, the figure counts a window nobody is in any
  more, so it is replaced by `--` rather than shown or guessed at as zero
- the same applies to a figure with no window at all. A session nobody is in comes back
  as `0%` with a null reset time, which is the plain truth while it is fresh and
  unknowable once it is not: any session started since opened a window we never saw. So
  a stale `0` becomes `--` too, rather than sitting there looking like a full session

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

What none of that changes: on macOS the token comes from the login keychain, but on
Windows Claude Code keeps it in plaintext at `%USERPROFILE%\.claude\.credentials.json`,
readable by anything running as you. That is Claude Code's storage decision, not this
plugin's, and the hardening above is about not widening the exposure - it cannot narrow
it.

Limits are read generically from the API's `limits` array, so a cap this code has
never heard of appears as an extra row with no change.

## Layout

```
macos/     SwiftBar plugin, and its README
windows/   notification-area app, and its README
```

The two deliberately share no code. Each is meant to be a single self-contained file,
and on macOS a shared module could not sit beside the plugin anyway: SwiftBar treats
every file in its plugin directory as a plugin and would try to execute it.

**What is duplicated is not only formatting.** It includes the whole rate-limit policy -
`retry_after_seconds`, `sane_cache`, `unattended`, `rolled_over`, `unanchored`,
`unreliable`, `unusable`, `next_attempt_at`, `collect_limits`, `write_statusline_sidecar`,
and the block that decides
contention from a genuine lockout - which is precisely the part that keeps changing. **A change to any of
it has to be made twice.** The two builds are kept honest by name: the same functions
take the same arguments in the same order, so a missing edit shows up as a diff of
function bodies rather than having to be reasoned about.

`write_statusline_sidecar` is the one item on that list whose output something outside
this repo parses. Both builds emit the same six-field line, documented in each platform
README, and that line is a published format rather than an internal detail - **changing
it has to be made twice here and then coordinated with whatever reads it**, which is a
different and worse problem than the rest of the list.

The exception, and the one that has actually bitten, is behaviour that exists under
different names on the two sides - Windows forcing a refresh through `maybe_fetch`,
macOS through `force_refresh`. Those have no counterpart to diff against, so they need
checking by hand.
