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

Two paths are closed, which is why this exists. **The Graph API is not permitted** where this runs, so `isOnlineMeeting` is not available to mint a join link. **win32com cannot insert one either** — on this account `IsOnlineMeeting` stays False and `OnlineMeeting` is unavailable, so the desktop path can create a meeting but never a Teams link. What is left is the web UI: tick `Microsoft Teams meeting` in the composer and the service generates the link. This drives that UI in a real browser with the user own session — no Graph token, no app registration, no admin consent. Same shape as `slack_web_skill`, applied to Microsoft 365.

## Why a *resident* session, and not a browser per command

Measured 2026-11-12: **every new browser session must pass a FIDO2 passkey at the corporate identity provider.** A persisted `ESTSAUTHPERSISTENT` cookie exists (89 days) but does not skip it — a fresh launch lands on "Pick an account" and then on a passkey prompt.

Consequences, both of which shape everything below:

- **The browser stays open** for the whole working session; commands arrive as JSON lines. A one-browser-per-command design (what the Slack skill does) cannot work here.
- **The passkey is the human step.** The agent never types credentials, and cannot: the prompt wants a PIN, fingerprint or face.

Headless is allowed *inside* an already-authenticated resident session, but it cannot start one — measured again 2026-11-12: `serve --headless` against a stored profile stopped at `login.microsoftonline.com` and exited 3 after the full timeout. Headed runs still need the passkey tap each session; the SSO redirect reaches `login.w3.ibm.com` and waits there for the human.

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

## The time zone trap: solved (it cost four wrong saves)

**Resolved 2026-11-12.** While the account calendar time zone was India (UTC+5:30), every typed time was stored 2 h 30 min later:

| Typed | Composer header showed | Stored (read back via Outlook COM) |
|---|---|---|
| 2026-11-24 09:00–10:00 | 09:00–10:00 | **11:30–12:30** |
| 2026-11-20 14:00–15:00 | 14:00–15:00 | **16:30–17:30** |

+2:30 is the gap between UTC+8 (Beijing) and UTC+5:30 (India), and the composer toolbar carries a control whose text is `India`. So the new-event time zone is India, and every typed time is interpreted there. Established so far:

- the browser reports the expected zone (checked with `Intl`, and again with `timezone_id` forced) — not the browser;
- removing the secondary India time zone from the calendar view did not change it;
- changing the account default time zone did not change it either (2026-11-12);
- clicking the `India` control raises `TimeoutError` — it is not directly clickable.

**What this means for use:** a `meeting-create` run produces a correct title, attendees, body and Teams link, but the time lands 2:30 late. Until the event time zone is fixed in the account, this skill must not be used for a real invitation without a human checking the time afterwards — and the check has to read the *stored* event (Outlook COM on Windows, or the calendar UI), never the composer, because the composer header shows the typed values, not the stored ones.

**The fix was the account calendar time zone**, set to China in Outlook web settings (Settings → Calendar). After that the same probe stored `09:00-10:00` exactly as typed, read back through Outlook COM. No code change was needed: the driver types local wall-clock times and the account tells Outlook which zone they mean.

Two habits this leaves behind:

- **Verify against the stored event, never the composer.** The composer header mirrors what was typed even when the stored value differs, so a read-back of the composer proves nothing. On Windows, `outlook_skill.py list-calendar` is the cheap independent check.
- **A uniform offset is a timezone, not a bug in the typing.** Two saves drifting by the same 2:30 pointed at the zone, not at `fill()`. Measuring the same operation twice and comparing was what identified it.

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
