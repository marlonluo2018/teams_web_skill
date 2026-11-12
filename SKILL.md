---
name: teams_web_skill
description: |
  Microsoft 365 web automation over a resident, human-authenticated browser session —
  for the things the desktop COM skill cannot do: creating calendar meetings that need a
  Teams join link, and working on macOS. Use when the user asks for a Teams meeting link
  or a meeting invite on a non-Windows machine. This drives the web UI in a real browser;
  it is NOT the Microsoft Graph API and needs no app registration or token.
triggers: [
  "teams meeting link", "create a meeting invite", "teams 会议链接", "建会议邀请",
  "schedule a teams meeting", "约个 teams 会议", "meeting invite on mac"
]
operations: ["serve", "calibrate"]
---

# Microsoft 365 (web) — resident session

This drives Microsoft 365 in a real browser with the user own session. No Graph token, no app registration, no admin consent — the same shape as `slack_browser_skill`, applied to Microsoft 365.

## Why a *resident* session, and not a browser per command

Measured on this tenant 2026-11-12: **every new browser session must pass an IBM w3id FIDO2 passkey.** A persisted `ESTSAUTHPERSISTENT` cookie exists (89 days) but does not skip it — a fresh launch lands on "Pick an account" and then on "Sign-in with Your Passkey".

Consequences, both of which shape everything below:

- **The browser stays open** for the whole working session; commands arrive as JSON lines. A one-browser-per-command design (what the Slack skill does) cannot work here.
- **The passkey is the human step.** The agent never types credentials, and cannot: the prompt wants a PIN, fingerprint or face.

Headless is allowed *inside* an already-authenticated resident session, but it cannot start one.

## Commands

```text
py -3 scripts/teams_driver.py serve       # open the window, wait for the app, then JSON lines on stdin
py -3 scripts/teams_driver.py calibrate   # open the calendar + composer and dump the controls
```

In `serve`, one JSON object per line, one reply per line:

```text
{"cmd":"status"}      -> {"app": true, "detail": "app", "url": ..., "home": ..., "config": ...}
{"cmd":"calibrate"}   -> the composer control names (see below)
{"cmd":"quit"}
```

## Known limitation: the meeting date cannot be set (yet)

Everything else in the composer is driven successfully — title, attendees (resolves to a chip), start/end **times**, body, and the `Microsoft Teams meeting` switch (verified `checked: true`). **The date cannot.** Whatever date is asked for, the composer stays on today.

What was tried, and what it did (all measured 2026-11-12):

| Attempt | Result |
|---|---|
| `type()` four formats into `Start date` (ISO, US, EU, 2-digit year) | value never changed |
| `fill()` on the same combobox | value never changed |
| `get_by_role("gridcell", name="November 20, 2026")` in the picker | no match |
| clicking the mini-calendar day button, then a time slot labelled `14:00` | matched an *existing event*, opened it instead of a new composer |
| geometric click at the column-header / time-gutter intersection | both handles resolved to the **left navigation pane** (`x=58`), not the day column |

So `meeting-create` **refuses any date other than today** rather than creating it on the wrong day. That refusal is deliberate: a meeting invite with a silently wrong date reaches other people and cannot be recalled.

The remaining unknown is narrow and testable: the main calendar grid column for a given day. The next attempt should dump the grid container's own descendants (roles, labels, boxes) instead of searching the whole page by label — that is exactly what pointed at the navigation pane instead of the grid.

## Current state — read this before promising anything

**`meeting-create` is not implemented yet.** The scaffolding (session, state, settings, installer, `calibrate`) is in place; the calendar composer has not been inspected, and writing a create-and-send flow from guesses is how a browser skill sends the wrong invitation to the wrong people.

Order of work, deliberately:

1. `calibrate` against a live session — collects the real `aria-label`/role names of the event composer and its Teams control;
2. only then implement `meeting-create`;
3. verify on a real invite addressed to the user own mailbox before anyone else is involved.

## Selector policy

Locate controls by **`aria-label` and role only**. Class names in Microsoft 365 web are generated and change between releases; a class-based selector works the day it is written and silently breaks later. When a control cannot be found by role or label, that is a `calibrate` job, not an excuse for a CSS guess.

## Approval — an invitation is an outgoing message

Same rule as email and Slack: **never create-and-send an invitation without an approved draft.** The draft must show subject, time, attendees, body, and whether a Teams link is requested.

One thing the approval cannot show: **the join link does not exist until the meeting is created**, so it is generated during the approved action. Say so explicitly in the approval block — an approval that hides that fact is a blind approval.

Failure has one dangerous shape here, because create-and-send happens in one go: a half-completed run can leave the meeting created but unsent, or sent but unreported. The driver must report **which** of those happened, and a retry must first look for a meeting already on the calendar with the same subject and time. Two invitations to the same meeting is a visible mistake to everyone invited.

## Cross-platform

Playwright only — no COM, no `pywin32`. Windows, macOS and Linux take the same code path, which is the reason this skill exists next to `outlook_com_skill`: COM is Windows-only, and on macOS there is no way to get a Teams join link at all.

State lives outside the checkout (`%LOCALAPPDATA%\teams-web-skill\` on Windows, `~/Library/Application Support/teams-web-skill/` on macOS, `$XDG_DATA_HOME/teams-web-skill/` elsewhere); settings live with the code in `config.json`, which is gitignored. A `.state/` directory beside `scripts/` switches to portable mode.

## Relationship to the other skills

- **Invitations (create + Teams link)** → this skill.
- **Accept/decline incoming invitations, mail, folders, search** → `outlook_com_skill` (Windows, COM). Inbox triage must not depend on a browser session: if the session is down, invitations would pile up.
- **Chat messages** → `slack_browser_skill` for Slack. This skill does not send Teams chat messages yet.
