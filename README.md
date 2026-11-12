# teams_web_skill

Microsoft 365 through a real browser session — for creating meetings that need a Teams join link,
and for working on machines where the desktop COM skill cannot run (macOS).

No Graph token, no app registration, no admin consent. It drives the web UI with the user own
signed-in session, which is the one thing the tenant does allow.

## Install

```bash
py -3 scripts/install.py        # Windows    (python3 on macOS/Linux)
```

Installs Playwright, downloads its Chromium (~130 MB, once), creates `config.json` from the template.

## Use

```bash
py -3 scripts/teams_driver.py serve      # window opens — tap your passkey there
```

Then send JSON lines on stdin: `{"cmd":"status"}`, `{"cmd":"calibrate"}`, `{"cmd":"quit"}`.

**A session is per browser launch.** Measured 2026-11-12: a persisted auth cookie does not skip
the passkey, so every new launch needs the human to tap it. That is why the browser stays open
instead of starting one per command.

## Status

| Part | State |
|---|---|
| Resident session, `status`, `calibrate` | working |
| `meeting-create`: title, attendees, **times**, body, `Microsoft Teams meeting` switch | working, read back and verified |
| `meeting-create`: **the date** | **not working** — refuses anything but today |

The date refusal is deliberate. Requesting a future date fails in ~0.5 s with exit 2 and never opens
the browser, because creating the meeting on the wrong day would put a wrong time in front of other
people and cannot be recalled. `SKILL.md` lists the five approaches already tried, so the next
attempt starts from evidence rather than from a guess.
