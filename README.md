# Hermes Day

**One screen to run your whole Hermes day.**

Hermes Day is a full-screen desktop plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that turns scattered per-session prompts into a single triage surface — an inbox for everything your agents need from you, plus a live view of what they're doing, what they finished, and what's scheduled next.

## What it shows

| Section | Contents |
| --- | --- |
| **Needs you** | Every open approval, clarify question, and input prompt across **all** sessions, profiles, bots, and remote connections — oldest first. |
| **Waiting on input** | Live sessions parked on a prompt this surface can't answer (e.g. delivered to another client). |
| **In flight** | Sessions actively working or streaming, newest activity first. |
| **Finished — review** | Turns that completed (or errored) while you weren't looking — persist until you review or dismiss them. |
| **Scheduled** | Every cron job across sources: next run, overdue and last-failure badges. |
| **Sources** | Health of every profile/connection the inbox is scanning — with a per-source mute bell for the noisy ones. |
| **Watching** | Pin any session to keep it on the rail — running or finished — until you unpin it. |

The header carries a live **day arc** (a sun tracking 6 am–6 pm), a stats strip
(waiting / in flight / to review / triaged-today), and the next scheduled
job's countdown. The rail ends with a digest line: today, finished, triaged.

**Focus mode** (eye icon) collapses everything except what's waiting on you and
what's running — the rail, the review queue, and the waiting section fold away.

## Keyboard triage

When anything needs you, the board is fully keyboard-driven — the selected card
shows its own key legend:

| Key | Action |
| --- | --- |
| `j` / `k` or `↓` / `↑` | Move selection |
| `a` | Allow once (approvals) |
| `d` | Deny (approvals) |
| `s` | Snooze 15 min |
| `o` / `Enter` | Open the session |

Clearing the last need triggers a small celebration — inbox zero should feel good.
A filter field above the board narrows every queue by title, preview, or method.

Cards that sit unanswered for 10+ minutes start **breathing** (a slow red ring
pulse) and the oldest open item wears a "waiting longest" flame chip — the board
nags you where your attention is most overdue.

## Stay in flow

- **Quick-task bar** — type what you want done, hit Enter: the plugin creates the session and submits the prompt without leaving the board.
- **Desktop notifications** — new needs announce themselves via the OS (bell toggle in the header; the first scan never fires — no notification storm on launch).
- **Snooze & mute** — snooze a card for 15 minutes, or mute a whole source; hidden items stay out of the counts until they're due.
- **Flood bar** — when one session stacks up multiple approvals, a single "Allow all" bar clears them in one click via `approval.respond { all: true }`.
- **Snooze presets** — 15 min, 1 hour, or tomorrow 9 am from the card's clock menu.
- **Pin to Watching** — keep an eye on any session; pinned rows live in the rail until you release them.
- **Undo dismiss** — dismissing a finished item pops a 6-second undo chip at the bottom of the board.

## Act without opening the session

- **Approvals** — Allow once / Allow session / Always / Deny inline, honoring the backend's choice set (`allow_permanent`, `smart_denied`, custom `choices`).
- **Clarifies** — Choice buttons, free-text answers, and multi-question batches answered per-question via `clarify.lock`.
- **Privileged prompts** (`sudo`, `secret`, `vault`, `mcp_setup`) — jump straight to the owning session; secrets stay typed into the session's masked input.
- **Reply to a session** — running, waiting, and finished rows take an inline follow-up prompt (`prompt.submit`) — no need to open the session to steer it.
- **Stop a runaway** — in-flight rows carry a stop button wired to `session.interrupt`.
- **Heartbeat sparkline** — every in-flight row draws a live sparkline of its event throughput (delta of `latest_seq` between polls), so you can see a stall before you open it.
- Every row deep-links into its session through the owning connection/profile route (`host.openSession` with a stored `route` descriptor).

## Chrome it adds

- **Full-screen `/day` page** — sidebar nav entry ("Day") plus a `ROUTES_AREA` route.
- **Statusbar chip** — live count of items waiting on you; click to open Day.
- **Command palette** — "Open Day" (`hermes-day.open`) and "Day: Toggle notifications" (`hermes-day.notify`).
- **Keybind** — `Mod+Shift+I` (rebindable in Settings → Keyboard).

## How it works

Pure renderer plugin — no agent half, no credentials, no config. It polls the
gateway's own contracts:

- `host.profileRoutes()` → every reachable connection/profile
- `session.active_list` → live sessions per source
- `session.events.since { last_seen: MAX_SAFE_INTEGER }` → each session's `open_requests` snapshot without replaying events
- `request.answer` / `approval.respond` / `clarify.lock` → the same answer paths the in-session cards use
- `cron.manage { action: 'list' }` → scheduled jobs
- `ctx.onEvent('message.complete' | 'error')` → the review list, persisted in `ctx.storage`

Finished items also light the sidebar's own unread dot via `markSessionUnreadFinished`, and reviewing clears it with `ackStoredSessionId` — the plugin borrows core's attention store instead of keeping a parallel one.

## Install

```
hermes://plugin/install?repo=DECRUX9812/hermes-day&enable=1
```

Or clone the `desktop/` half for hot-reload development:

```bash
mkdir -p ~/.hermes/desktop-plugins/hermes-day
cp desktop/plugin.js ~/.hermes/desktop-plugins/hermes-day/plugin.js
# toggle on under Settings → Capabilities → Plugins
```

## Dev loop

Edit `desktop/plugin.js` on disk; the desktop runtime watches
`~/.hermes/desktop-plugins/<id>/plugin.js` and hot-reloads on save. The plugin
is a single self-contained ESM file — plain `jsx()`/`jsxs()` calls, imports
limited to `@hermes/plugin-sdk`, `react`, and `react/jsx-runtime` — matching
the catalog's desktop-surface lint.

## License

MIT
