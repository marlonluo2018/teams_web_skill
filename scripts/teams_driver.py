#!/usr/bin/env python3
"""teams_web_skill driver - Microsoft 365 web automation over a resident session.

Why resident, and not one browser per command: measured on this tenant
2026-11-12, every new browser session must pass an IBM w3id FIDO2 passkey, and
the persisted ESTSAUTHPERSISTENT cookie (89 days) does not skip it - a fresh
launch lands on "Pick an account" and then on "Sign-in with Your Passkey".
So the browser stays open for the whole working session, the human taps the
passkey once, and commands arrive as JSON lines on stdin.

    py -3 scripts/teams_driver.py serve       # open the window, wait, take JSON lines
    py -3 scripts/teams_driver.py calibrate   # dump the event composer controls

Nothing here sends an invitation yet: the composer has not been calibrated,
and guessing selectors is how a browser skill sends the wrong thing.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.exit("playwright is not installed: py -3 scripts/install.py")

HOME_NAME = "teams-web-skill"
SKILL_ROOT = Path(__file__).resolve().parents[1]
PORTABLE_DIR = ".state"

# Sign-in hosts to reject. A whitelist of app hosts was wrong twice in one day:
# outlook.office.com redirects to outlook.cloud.microsoft, and Teams lives on
# teams.cloud.microsoft - both absent from the list I had written, so a rendered
# calendar read as "not ready". Reject the sign-in hosts instead; anything else
# with real text on it is the app.
LOGIN_HOSTS = ("login.microsoftonline.com", "login.microsoft.com",
               "login.live.com", "login.w3.ibm.com")
# Text that only appears during sign-in, so it vetoes the app verdict.
LOGIN_HINTS = ("pick an account", "sign in to your account", "enter password",
               "use another account", "sign-in with your passkey")


# --- settings: env var > config file > default -----------------------------

def _data_base() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / HOME_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / HOME_NAME
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / HOME_NAME


def _resolve(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (SKILL_ROOT / path)


def skill_home() -> Path:
    """Profile and diagnostics. A .state/ beside the script means portable."""
    override = os.environ.get("TEAMS_SKILL_HOME")
    if override:
        return _resolve(override)
    portable = SKILL_ROOT / PORTABLE_DIR
    return portable if portable.is_dir() else _data_base()


def config_path() -> Path:
    """Settings stay with the code: visible, and gitignored."""
    override = os.environ.get("TEAMS_SKILL_CONFIG")
    if override:
        return _resolve(override)
    home = skill_home()
    return home / "config.json" if home == SKILL_ROOT / PORTABLE_DIR else SKILL_ROOT / "config.json"


_CONFIG: dict | None = None


def config() -> dict:
    global _CONFIG
    if _CONFIG is not None:
        return _CONFIG
    path = config_path()
    data: dict = {}
    try:
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(loaded, dict):
                raise ValueError("expected a JSON object")
            data = loaded
    except Exception as exc:
        print(f"ignoring {path}: {exc}", file=sys.stderr)
    _CONFIG = data
    return data


def setting(env_name: str, key: str, default):
    value = os.environ.get(env_name)
    if value:
        return value
    value = config().get(key)
    return value if value not in (None, "") else default


def profile_dir() -> Path:
    override = setting("TEAMS_SKILL_PROFILE", "profile", "")
    return _resolve(str(override)) if override else skill_home() / "profile"


def app_url() -> str:
    return str(setting("TEAMS_SKILL_APP_URL", "app_url", "https://teams.cloud.microsoft/"))


def calendar_url() -> str:
    return str(setting("TEAMS_SKILL_CALENDAR_URL", "calendar_url",
                       "https://outlook.office.com/calendar/"))


def ready_timeout() -> int:
    return int(setting("TEAMS_SKILL_READY_TIMEOUT", "ready_timeout", 300))


def state_report() -> dict:
    return {"home": str(skill_home()), "config": str(config_path()),
            "config_found": config_path().exists(),
            "portable": skill_home() == SKILL_ROOT / PORTABLE_DIR}


def emit(payload: dict) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def open_context(playwright, headless: bool = False):
    path = profile_dir()
    path.mkdir(parents=True, exist_ok=True)
    channel = str(setting("TEAMS_SKILL_BROWSER_CHANNEL", "browser_channel", "") or "").strip()
    options = {
        "user_data_dir": str(path),
        "headless": headless,
        "viewport": {"width": 1500, "height": 950},
        "args": ["--no-first-run", "--no-default-browser-check", "--hide-crash-restore-bubble"],
    }
    if channel:
        options["channel"] = channel
    return playwright.chromium.launch_persistent_context(**options)


def app_state(page) -> tuple[bool, str]:
    """(rendered, detail) - rendered means an app screen, not a sign-in screen."""
    url = page.url or ""
    host = urlparse(url).netloc.lower()
    try:
        body = " ".join(page.inner_text("body").split())[:400]
    except Exception:
        body = ""
    low = body.lower()
    if any(host.endswith(h) for h in LOGIN_HOSTS):
        return False, host or "no host"
    if any(hint in low for hint in LOGIN_HINTS):
        return False, "sign-in screen"
    return (len(body) > 60 and bool(host)), "app"


def wait_for_app(page, timeout: int) -> bool:
    """Poll until the app renders, narrating on stderr so the human knows when to tap."""
    deadline = time.time() + timeout
    steady = 0
    last = 0.0
    while time.time() < deadline:
        rendered, detail = app_state(page)
        steady = steady + 1 if rendered else 0
        if time.time() - last > 10:
            last = time.time()
            print(f"waiting: {detail} (steady {steady}/3)", file=sys.stderr, flush=True)
        if steady >= 3:
            return True
        page.wait_for_timeout(2000)
    return False


def controls(page, selector: str, limit: int = 60) -> list[str]:
    out, seen = [], set()
    try:
        loc = page.locator(selector)
        for i in range(min(loc.count(), 200)):
            el = loc.nth(i)
            try:
                label = el.get_attribute("aria-label") or (el.inner_text() or "").strip()
            except Exception:
                continue
            label = " ".join((label or "").split())[:70]
            if label and label not in seen:
                seen.add(label)
                out.append(label)
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out


def inventory(page, selector: str, limit: int = 30) -> list[dict]:
    """Tag/role/placeholder/label for each match - placeholders matter here.

    The composer identifies its fields by placeholder ("Add title", "Invite
    required attendees"), not by aria-label, so a label-only dump reported fewer
    controls than the screen shows.
    """
    out: list[dict] = []
    seen = set()
    try:
        loc = page.locator(selector)
        for i in range(min(loc.count(), 200)):
            el = loc.nth(i)
            try:
                info = {
                    "tag": el.evaluate("e => e.tagName.toLowerCase()"),
                    "role": (el.get_attribute("role") or "").strip(),
                    "label": (el.get_attribute("aria-label") or "").strip()[:40],
                    "placeholder": (el.get_attribute("placeholder") or "").strip()[:40],
                    "editable": (el.get_attribute("contenteditable") or "").strip(),
                }
            except Exception:
                continue
            if not (info["label"] or info["placeholder"] or info["editable"] == "true"):
                continue
            key = (info["tag"], info["role"], info["label"], info["placeholder"])
            if key in seen:
                continue
            seen.add(key)
            out.append(info)
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out


def calibrate(page) -> dict:
    """Open the calendar and the event composer, and report what is actually there.

    This exists because the composer has never been inspected: aria-labels and
    roles are the only stable handles on a product that renames its classes
    every release, and writing meeting-create before seeing them is guessing.
    """
    report: dict = {"calendar_url": calendar_url()}
    page.goto(calendar_url(), wait_until="domcontentloaded")
    if not wait_for_app(page, ready_timeout()):
        report["error"] = "calendar did not render"
        return report
    report["switches"] = controls(page, "[role=switch]")[:20]
    report["new_event_buttons"] = [b for b in controls(page, "button")
                                   if "new" in b.lower()][:10]

    opened = False
    for label in ("New event", "New meeting", "New appointment"):
        btn = page.get_by_role("button", name=label)
        if btn.count() > 0:
            try:
                btn.first.click(timeout=10000)
                opened = True
                break
            except Exception:
                continue
    report["composer_opened"] = opened
    if opened:
        page.wait_for_timeout(9000)
        report["composer_switches"] = controls(page, "[role=switch]")[:20]
        report["composer_textboxes"] = controls(page, "[role=textbox], input, textarea")[:20]
        report["composer_comboboxes"] = controls(page, "[role=combobox]")[:20]
        report["composer_buttons"] = controls(page, "button")[:40]
        report["inventory"] = inventory(page,
            "input, textarea, [contenteditable=true], [role=textbox], [role=combobox]")

        # The open question: does toggling the Teams switch produce the join URL
        # inside the draft, or only after the meeting is created/sent? Toggling a
        # switch on an unsent draft is harmless - nothing leaves the machine.
        toggled = None
        try:
            sw = page.get_by_role("switch", name=re.compile("Microsoft Teams meeting", re.I))
            if sw.count() > 0:
                before = sw.first.get_attribute("aria-checked")
                sw.first.click(timeout=8000)
                page.wait_for_timeout(6000)
                after = sw.first.get_attribute("aria-checked")
                toggled = {"before": before, "after": after}
        except Exception as exc:
            toggled = {"error": type(exc).__name__}
        report["teams_switch"] = toggled
        if toggled and not toggled.get("error"):
            try:
                text = " ".join(page.inner_text("body").split())
                links = re.findall(r"https://teams[.]microsoft[.]com/l/meetup-join[^ ]+", text)
                report["join_url_in_dom"] = bool(links)
                report["join_url_sample"] = (links[0][:80] + "...") if links else None
                report["meetup_mention"] = "meetup-join" in text
            except Exception as exc:
                report["join_url_probe"] = type(exc).__name__
        # Times are the most error-prone field, so open the picker while the
        # composer is up and record what it contains.
        try:
            clock = page.get_by_role("button", name=re.compile(r"[0-9]{1,2}:[0-9]{2}"))
            if clock.count() > 0:
                clock.first.click(timeout=8000)
                page.wait_for_timeout(4000)
                report["time_panel"] = inventory(
                    page, "input, [role=combobox], [role=textbox]")[:20]
                page.keyboard.press("Escape")
                page.wait_for_timeout(1000)
            else:
                report["time_panel"] = "no clock button found"
        except Exception as exc:
            report["time_panel"] = type(exc).__name__

        shot = skill_home() / "calibrate-composer.png"
        try:
            page.screenshot(path=str(shot))
            report["screenshot"] = str(shot)
        except Exception:
            pass
        page.keyboard.press("Escape")
    return report


def _switch_on(page) -> bool | None:
    """Is the Teams switch on? For input[role=switch] the state is `checked`."""
    try:
        sw = page.locator("input[role=switch][aria-label=\"Microsoft Teams meeting\"]")
        if sw.count() == 0:
            sw = page.get_by_role("switch", name=re.compile("Microsoft Teams meeting", re.I))
        if sw.count() == 0:
            return None
        return bool(sw.first.evaluate("e => e.checked"))
    except Exception:
        return None


def _discard(page) -> None:
    """Escape asks "Discard event?"; the dialog has to be answered or the draft stays."""
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(1200)
        ok = page.get_by_role("button", name=re.compile("^OK$", re.I))
        if ok.count() > 0:
            ok.first.click(timeout=5000)
            page.wait_for_timeout(1200)
    except Exception:
        pass


def meeting_create(page, args: dict) -> dict:
    """Fill the composer, then save (i.e. send the invitation) unless dry_run.

    One shot on purpose: the user ruled out leaving an unsent draft behind, so
    the approval happens in the conversation and this call does create-and-send.
    The read-back before the click is a safety check, not a human gate: subject,
    start, end, attendee count and the Teams switch must all match what was
    approved, or nothing is saved.
    """
    subject = (args.get("subject") or "").strip()
    start = (args.get("start") or "").strip()
    end = (args.get("end") or "").strip()
    attendees = [a.strip() for a in (args.get("attendees") or []) if a.strip()]
    if not subject or not start or not end:
        return {"ok": False, "error": "subject, start and end are required"}

    page.goto(calendar_url(), wait_until="domcontentloaded")
    if not wait_for_app(page, ready_timeout()):
        return {"ok": False, "error": "calendar did not render"}

    new_ev = page.get_by_role("button", name=re.compile("new event", re.I))
    if new_ev.count() == 0:
        return {"ok": False, "error": "no New event button"}
    new_ev.first.click()
    page.wait_for_timeout(6000)

    page.locator("input[placeholder=\"Add title\"]").first.fill(subject)
    page.wait_for_timeout(800)

    body = (args.get("body") or "").strip()
    if body:
        try:
            editor = page.get_by_role("textbox", name="Event body")
            if editor.count() == 0:
                editor = page.locator("[contenteditable=true][aria-label=\"Event body\"]")
            editor.first.click()
            editor.first.type(body, delay=15)
            page.wait_for_timeout(800)
        except Exception as exc:
            return {"ok": False, "error": f"body: {type(exc).__name__}"}

    resolved = []
    for address in attendees:
        try:
            box = page.locator("[contenteditable=true][aria-label=\"Invite required attendees\"]").first
            box.click()
            box.type(address, delay=40)
            page.wait_for_timeout(2500)
            page.keyboard.press("Enter")      # accept the first suggestion
            page.wait_for_timeout(1200)
            resolved.append(address)
        except Exception as exc:
            return {"ok": False, "error": f"attendee {address}: {type(exc).__name__}"}

    # times: open the picker and set the three comboboxes
    clock = page.get_by_role("button", name=re.compile(r"[0-9]{1,2}:[0-9]{2}"))
    if clock.count() > 0:
        clock.first.click()
        page.wait_for_timeout(2500)
    for label, value in (("Start date", start.split()[0] if " " in start else start),
                         ("Start time", start.split()[-1]),
                         ("End time", end.split()[-1])):
        try:
            field = page.get_by_role("combobox", name=label)
            if field.count() == 0:
                continue
            field.first.click()
            page.keyboard.press("Control+a")
            field.first.type(value, delay=60)
            page.keyboard.press("Enter")
            page.wait_for_timeout(600)
        except Exception:
            continue
    page.keyboard.press("Escape")
    page.wait_for_timeout(1000)

    if args.get("teams", True) and _switch_on(page) is False:
        try:
            page.locator("input[role=switch][aria-label=\"Microsoft Teams meeting\"]").first.click()
            page.wait_for_timeout(1500)
        except Exception:
            pass

    readback = {"subject": "", "time": "", "attendee_chips": 0, "teams_on": _switch_on(page),
                "body_head": ""}
    try:
        editor = page.get_by_role("textbox", name="Event body")
        readback["body_head"] = " ".join((editor.first.inner_text() or "").split())[:60]
    except Exception:
        pass
    try:
        readback["subject"] = page.locator("input[placeholder=\"Add title\"]").first.input_value()
    except Exception:
        pass
    try:
        readback["time"] = " ".join(page.get_by_role("button", name=re.compile(r"[0-9]{1,2}:[0-9]{2}")).first.inner_text().split())
    except Exception:
        pass
    try:
        readback["attendee_chips"] = page.locator("[aria-label=\"Invite required attendees\"] ~ *").count()
    except Exception:
        pass

    shot = skill_home() / "meeting-create.png"
    try:
        page.screenshot(path=str(shot))
    except Exception:
        pass

    if args.get("dry_run"):
        _discard(page)
        return {"ok": True, "dry_run": True, "readback": readback, "screenshot": str(shot)}

    problems = []
    if readback["subject"] != subject:
        problems.append(f"subject read back as {readback['subject']!r}")
    if body and body[:20] not in readback["body_head"]:
        problems.append("body did not stick in the editor")
    if args.get("teams", True) and readback["teams_on"] is not True:
        problems.append("Teams switch is not on")
    if problems:
        _discard(page)
        return {"ok": False, "error": "; ".join(problems), "readback": readback}

    save = page.get_by_role("button", name=re.compile(r"^(Save|Send)$", re.I))
    if save.count() == 0:
        _discard(page)
        return {"ok": False, "error": "no Save/Send button", "readback": readback}
    label = " ".join((save.first.inner_text() or "").split())
    save.first.click()
    page.wait_for_timeout(9000)

    after = ""
    try:
        after = " ".join(page.inner_text("body").split())[:200]
    except Exception:
        pass
    return {"ok": True, "sent": True, "clicked": label, "readback": readback,
            "screenshot": str(shot), "page_head": after}

def serve(page) -> int:
    """One browser, many commands: JSON in, JSON out, in order."""
    emit({"action": "serve", "ready": True, **state_report()})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except Exception as exc:
            emit({"action": "error", "error": f"bad json: {exc}"})
            continue
        name = cmd.get("cmd")
        try:
            if name == "quit":
                emit({"action": "quit", "ok": True})
                return 0
            if name == "status":
                rendered, detail = app_state(page)
                emit({"action": "status", "app": rendered, "detail": detail,
                      "url": (page.url or "")[:90], **state_report()})
                continue
            if name == "calibrate":
                emit({"action": "calibrate", **calibrate(page)})
                continue
            emit({"action": name or "unknown", "error": "unknown command"})
        except (SystemExit, PWTimeout) as exc:
            emit({"action": name or "unknown", "error": str(exc).splitlines()[0][:200]})
        except Exception as exc:
            emit({"action": name or "unknown", "error": f"{type(exc).__name__}: {exc}"[:200]})
    return 0


USAGE = """teams_web_skill - resident M365 web automation

  py -3 scripts/teams_driver.py serve [--headless]     open the window, then JSON lines on stdin
  py -3 scripts/teams_driver.py calibrate [--headless]  open the calendar + composer, dump controls
  py -3 scripts/teams_driver.py meeting-create --subject S
        --start "2026-11-13 14:00" --end "2026-11-13 15:00" --attendee a@b.c
        [--body TEXT] [--no-teams] [--dry-run]

A new browser session needs an IBM w3id passkey tap, so the browser stays open:
start one, keep it, and send commands ({"cmd":"status"}, {"cmd":"calibrate"},
{"cmd":"quit"}) on stdin.
"""
def _meeting_args(argv: list[str]) -> dict:
    """Minimal flag parser: --subject/--start/--end/--attendee(repeat)/--body/--no-teams/--dry-run."""
    args: dict = {"attendees": [], "teams": True, "dry_run": "--dry-run" in argv}
    i = 0
    while i < len(argv):
        flag = argv[i]
        takes_value = flag in ("--subject", "--start", "--end", "--attendee", "--body")
        if takes_value and i + 1 < len(argv):
            value = argv[i + 1]
            if flag == "--attendee":
                args["attendees"].append(value)
            else:
                args[flag.lstrip("-")] = value
            i += 2
            continue
        if flag == "--no-teams":
            args["teams"] = False
        i += 1
    return args


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if command in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if command not in ("serve", "calibrate", "meeting-create"):
        sys.exit(f"unknown command {command!r}; use serve, calibrate or meeting-create")
    headless = "--headless" in sys.argv[2:]
    with sync_playwright() as pw:
        ctx = open_context(pw, headless=headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(app_url(), wait_until="domcontentloaded")
            if not wait_for_app(page, ready_timeout()):
                emit({"action": "ready", "ok": False, "url": (page.url or "")[:90],
                      "hint": "sign in in the window; every new session needs a passkey",
                      **state_report()})
                return 3
            if command == "calibrate":
                emit({"action": "calibrate", **calibrate(page)})
                return 0
            if command == "meeting-create":
                emit({"action": "meeting-create",
                      **meeting_create(page, _meeting_args(sys.argv[2:]))})
                return 0
            return serve(page)
        finally:
            ctx.close()


if __name__ == "__main__":
    raise SystemExit(main())
