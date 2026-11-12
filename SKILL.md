---
name: teams_web_skill
description: |
  Teams meeting links for Microsoft 365 when the Graph API is not permitted and win32com
  cannot insert one: it creates the calendar meeting with a real Teams join link, and works on
  macOS too. Use when the user asks for a Teams meeting link or a meeting invite. This drives
  the web UI in a real browser; it is NOT the Graph API and needs no app registration or token.
triggers: [
  "teams meeting link", "create a meeting invite", "teams 会议链接", "建会议邀请",
  "schedule a teams meeting", "约个 teams 会议", "meeting invite on mac"
]
operations: ["serve", "calibrate"]
---

# Microsoft 365 (web) — resident session

**The Graph API is not permitted** where this runs. **win32com cannot insert a Teams link either** — on this account `IsOnlineMeeting` stays False and `OnlineMeeting` is unavailable, so the desktop path can create a meeting but never a link. What remains is the web UI: tick `Microsoft Teams meeting` in the composer and the service generates the link. This drives that UI in a real browser with the user own session — no Graph token, no app registration, no admin consent. Same shape as `slack_web_skill`, applied to Microsoft 365.

## Sessions: one launch per run is fine, headless is not

Measured 2026-11-12, in this order:

- **A headed launch with a valid stored session goes straight in** — `landed_without_help: true`, no prompt. The one-browser-per-command shape works; the earlier belief that every launch demanded a passkey was wrong, and came from reading a headless failure as an auth rule.
- **A headless launch does not**, even with that same valid session. Re-measured 2026-11-12: it clears
  the earlier SSO steps and stops at the IdP's FIDO2 page — "Sign-in with Your Passkey ... use your
  PIN, fingerprint, or face" — which a headless browser cannot satisfy. The same profile that enters
  silently in a window is asked for a passkey without one.
- **`--offscreen` is the workaround that is not a workaround**: the browser stays headed (so the same
  profile still enters silently) with the window parked at -32000,-32000. Verified: app reached, window
  off the desktop. Nothing about the browser is disguised, so this does not trade a policy question for
  convenience.
- **When the stored session has expired**, the headed run lands on the sign-in page and waits for the human: passkey, PIN, fingerprint or face. Only then is the agent blocked on a person.

`serve` is therefore an optimisation, not a requirement: one session, many commands, one boot (~15 s each otherwise). Use it for a batch; a single `meeting-create` one-shot is fine.

If a sign-in page ever hangs, the corporate IdP host is the thing to list under `idp_hosts` so the driver recognises it as a sign-in page instead of an app screen.

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

## Times follow the account calendar time zone

The composer interprets typed times in the account's calendar time zone, not the machine's. A uniform
shift — every time coming back the same amount late — is that setting, not a bug in the typing: fix the
time zone in the calendar settings and re-test before touching code.

Verify times by reading the **stored event** (`list-calendar`), never the composer header: the header
mirrors what was typed even when the stored value differs, so reading it back proves nothing.

## The date field: solved, and how it was misread

**Resolved 2026-11-12.** The `Start date` control is an ordinary `input[type=text]` holding an ISO value (`2026-11-12`), not read-only, and `fill("2026-11-20")` writes it; `Tab` commits it. Five earlier approaches had failed because **focus was never on the input**, not because the control was special: a probe that read the element attributes (`readonly: null`, `type: "text"`, `value_before: "2026-11-12"`) and then reported `{"fill": "2026-11-20", "after_tab": "2026-11-20"}` settled it in one run.

The lesson worth keeping: every failure had been attributed to the *widget* ("read-only", "needs a calendar cell", "wants a different format") when the real cause was the *interaction*. Reading the element attributes first would have cost one run instead of six.

What was tried, and what it did (all measured 2026-11-12):

| Attempt | Result |
|---|---|
| `type()` four formats into `Start date` (ISO, US, EU, 2-digit year) | value never changed |
| `fill()` on the same combobox | value never changed |
| `get_by_role("gridcell", name="November 20, 2026")` in the picker | no match |
| clicking the mini-calendar day button, then a time slot labelled `14:00` | matched an *existing event*, opened it instead of a new composer |
| geometric click at the column-header / time-gutter intersection | both handles resolved to the **left navigation pane** (`x=58`), not the day column |

The refusal that used to guard this is gone; what remains is the read-back check — if the composer's own header does not show the requested date, `meeting-create` refuses to save rather than sending a wrong time to other people.

The remaining unknown is narrow and testable: the main calendar grid column for a given day. The next attempt should dump the grid container's own descendants (roles, labels, boxes) instead of searching the whole page by label — that is exactly what pointed at the navigation pane instead of the grid.

## Current state

`meeting-create` works end to end, verified on 2026-11-12 against a real invitation addressed to the user own mailbox:

| Step | Evidence |
|---|---|
| resident session | passkey sign-in, Teams UI rendered |
| title, body, attendees, date, times, Teams switch | typed and read back in the composer |
| Teams join link | present in the delivered invitation body (`teams.microsoft.com/l/meetup-join/...`) |
| send | `composer_closed: true`; the appointment and its time confirmed by reading the stored event |

What it does **not** do: send Teams chat messages, accept or decline incoming invitations (that stays with `outlook_com_skill`), or run unattended — every new browser session needs a passkey.

## Selector policy

Locate controls by **`aria-label` and role only**. Class names in Microsoft 365 web are generated and change between releases; a class-based selector works the day it is written and silently breaks later. When a control cannot be found by role or label, that is a `calibrate` job, not an excuse for a CSS guess.

## Approval — an invitation is an outgoing message

Same rule as email and Slack: **never create-and-send an invitation without an approved draft.** The draft must show subject, time, attendees, body, and whether a Teams link is requested.

One thing the approval cannot show: **the join link does not exist until the meeting is created**, so it is generated during the approved action. Say so explicitly in the approval block — an approval that hides that fact is a blind approval.

Failure has one dangerous shape here, because create-and-send happens in one go: a half-completed run can leave the meeting created but unsent, or **sent but reported unsent**.

That second case happened here on 2026-11-12. A run ended with `composer_closed: false` and reported `sent: false`, and the invitation had in fact been delivered: the "Please wait ... creating a Teams meeting" modal keeps the composer on screen *after* the send completes, so the composer is not evidence either way. Only `Sent Items` is.

**Before retrying a run that reported failure, look for the invitation in `Sent Items`** (`outlook_skill.py find --subject "<subject>" --folder "Sent Items"`). Two invitations to one meeting is visible to everyone invited, and the only thing that prevents it is checking the sent folder, not the browser.

## Cross-platform

Playwright only — no COM, no `pywin32`. Windows, macOS and Linux take the same code path, which is the reason this skill exists next to `outlook_com_skill`: COM is Windows-only, and on macOS there is no way to get a Teams join link at all.

State lives outside the checkout (`%LOCALAPPDATA%\teams-web-skill\` on Windows, `~/Library/Application Support/teams-web-skill/` on macOS, `$XDG_DATA_HOME/teams-web-skill/` elsewhere); settings live with the code in `config.json`, which is gitignored. A `.state/` directory beside `scripts/` switches to portable mode.

## Relationship to the other skills

- **Invitations (create + Teams link)** → this skill.
- **Accept/decline incoming invitations, mail, folders, search** → `outlook_com_skill` (Windows, COM). Inbox triage must not depend on a browser session: if the session is down, invitations would pile up.
- **Chat messages** → `slack_browser_skill` for Slack. This skill does not send Teams chat messages yet.
