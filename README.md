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

## Stay in flow

- **Quick-task bar** — type what you want done, hit Enter: the plugin creates the session and submits the prompt without leaving the board.
- **Desktop notifications** — new needs announce themselves via the OS (bell toggle in the header; the first scan never fires — no notification storm on launch).
- **Snooze & mute** — snooze a card for 15 minutes, or mute a whole source; hidden items stay out of the counts until they're due.
- **Flood bar** — when one session stacks up multiple approvals, a single "Allow all" bar clears them in one click via `approval.respond { all: true }`.

## Act without opening the session

- **Approvals** — Allow once / Allow session / Always / Deny inline, honoring the backend's choice set (`allow_permanent`, `smart_denied`, custom `choices`).
- **Clarifies** — Choice buttons, free-text answers, and multi-question batches answered per-question via `clarify.lock`.
- **Privileged prompts** (`sudo`, `secret`, `vault`, `mcp_setup`) — jump straight to the owning session; secrets stay typed into the session's masked input.
- Every row deep-links into its session through the owning connection/profile route (`host.openSession` with a stored `route` descriptor).

## Chrome it adds

- **Full-screen `/day` page** — sidebar nav entry ("Day") plus a `ROUTES_AREA` route.
- **Statusbar chip** — live count of items waiting on you; click to open Day.
- **Command palette** — "Open Day" (`id: hermes-day.open`).
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
