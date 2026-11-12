# teams_web_skill

**The solution for Teams meeting links in a company whose IT policy does not permit the Graph API —
and where win32com cannot insert a link either.**

The Graph API is banned by company IT policy. win32com cannot insert one either: `IsOnlineMeeting`
stays False and `OnlineMeeting` is unavailable, so the desktop path can create a meeting but never a
Teams link.

What is left is the web UI, which is what this drives: the Outlook calendar in Chromium with the
user's own SSO session. It fills the composer, switches on `Microsoft Teams meeting`, and sends. The
join link is minted by the service at creation — nothing pasted in, nothing invented.

## Who does what

**The agent runs everything except the sign-in, and the sign-in is needed only when the stored
session has expired.**

1. While the session is valid, nothing is asked: a freshly launched browser goes straight to the
   calendar (measured 2026-11-12: `landed_without_help: true`).
2. When it has expired, the agent opens a window on the sign-in page and **you** approve it with
   your passkey — PIN, fingerprint or face. The agent never sees the credential. Then the agent
   does the work: title, attendees, date, times, body, the Teams switch, and the send.
3. **A window opens for each run**, because headless cannot complete the corporate sign-in: a
   headless launch with a valid stored session still lands on the login page and exits 3
   (measured). That is why `serve` exists — one session, many commands, one boot.

**What that means in practice:** be reachable for the occasional sign-in, and expect a Chromium
window on each run. It is not a scheduled job.

## What it does

| Command | Purpose |
|---|---|
| `meeting-create` | fill the composer and send the invitation (title, attendees, date, times, body, Teams link on/off) |
| `serve` | keep one session open and take JSON commands on stdin — for a batch, to boot once |
| `calibrate` | re-discover the composer controls after a Microsoft UI change |

`meeting-create --dry-run` fills everything and discards it without sending: use it to check a new
environment, or after any UI change.

## Install

Python 3.10+ is the only prerequisite. Everything else is one command, and it is safe to re-run:

```bash
py -3 scripts/install.py        # Windows    (python3 scripts/install.py on macOS/Linux)
```

It installs Playwright, downloads its Chromium build (~130 MB, once), and creates `config.json` from
the template if there is none. It never asks for credentials.

## Quick start

What the **agent** runs — the window it opens is where you tap the passkey:

```bash
D=scripts/teams_driver.py

py -3 $D meeting-create --subject 'Sync' --start '2026-11-25 10:00' --end '2026-11-25 11:00' \
     --attendee someone@example.com --body-file body.txt --dry-run    # rehearse, sends nothing
py -3 $D meeting-create --subject 'Sync' --start '2026-11-25 10:00' --end '2026-11-25 11:00' \
     --attendee someone@example.com --body-file body.txt              # approve, then send
```

## Configuration

Defaults live in `config.json` beside `scripts/` (gitignored), and every key has an env-var twin,
with **env var > config file > default**:

| key | env var | default |
|---|---|---|
| `app_url` | `TEAMS_SKILL_APP_URL` | the Teams web app |
| `calendar_url` | `TEAMS_SKILL_CALENDAR_URL` | the Outlook calendar |
| `browser_channel` | `TEAMS_SKILL_BROWSER_CHANNEL` | bundled Chromium; set `msedge`/`chrome` if the passkey prompt does not appear |
| `ready_timeout` | `TEAMS_SKILL_READY_TIMEOUT` | 300 s to reach the app after sign-in |
| `idp_hosts` | `TEAMS_SKILL_IDP_HOSTS` | empty — add your corporate sign-in host(s), comma separated, so its page is recognised as a sign-in page rather than as the app |
| `profile` | `TEAMS_SKILL_PROFILE` | the browser profile under the per-user data directory |

The browser profile holds the session and lives outside the checkout: `%LOCALAPPDATA%\teams-web-skill\`
on Windows, `~/Library/Application Support/teams-web-skill/` on macOS, `$XDG_DATA_HOME/teams-web-skill/`
on Linux. A `.state/` directory beside `scripts/` switches to portable mode.

## Limits worth knowing

- **Headless cannot start a session.** The passkey needs a window, so `--headless` stops at the
  sign-in page and exits 3. Headless is only useful inside a session that is already open.
- **The composer is not evidence.** Its header shows what was typed even when the stored event
  differs; read the event back from the calendar (`list-calendar`) to know what landed.
- **Check `Sent Items` before retrying a failure.** A run can report `sent: false` while the
  invitation has already gone out, because the "creating a Teams meeting" dialog keeps the
  composer on screen after the send completes. Two invitations to one meeting is a visible mistake.
- **Times follow the account calendar time zone.** If every time comes back shifted by the same
  amount, fix the calendar time zone in the account settings — not the code.

`SKILL.md` is the operating manual: field selectors, the dry-run recipe, and the measured quirks.
