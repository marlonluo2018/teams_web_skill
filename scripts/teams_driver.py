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

        # --- date field probe: how is this control actually driven? ---
        probe: dict = {}
        try:
            field = page.get_by_role("combobox", name="Start date")
            if field.count() == 0:
                probe["found"] = False
            else:
                el = field.first
                probe["found"] = True
                probe["tag"] = el.evaluate("e => e.tagName.toLowerCase()")
                for attr in ("type", "readonly", "disabled", "aria-haspopup", "role"):
                    try:
                        probe[attr] = el.get_attribute(attr)
                    except Exception:
                        pass
                probe["value_before"] = el.evaluate("e => e.value")
                el.click()
                page.wait_for_timeout(1000)
                page.keyboard.type("11/20/2026", delay=40)
                page.wait_for_timeout(1000)
                probe["value_after_type"] = el.evaluate("e => e.value")
                page.keyboard.press("Enter")
                page.wait_for_timeout(800)
                probe["value_after_enter"] = el.evaluate("e => e.value")
                try:
                    probe["header_after"] = " ".join(page.get_by_role(
                        "button", name=re.compile(r"[0-9]{1,2}:[0-9]{2}")).first.inner_text().split())
                except Exception:
                    pass
                page.keyboard.press("Escape")
                page.wait_for_timeout(800)
        except Exception as exc:
            probe["error"] = f"{type(exc).__name__}: {str(exc)[:80]}"

        # anything on the page that carries a date in its label is a candidate handle
        try:
            dated = page.locator("[aria-label*='2026'], [title*='2026']")
            samples = []
            for i in range(min(dated.count(), 25)):
                node = dated.nth(i)
                try:
                    label = (node.get_attribute("aria-label") or node.get_attribute("title") or "")
                    role = (node.get_attribute("role") or node.evaluate("e => e.tagName.toLowerCase()"))
                except Exception:
                    continue
                label = " ".join(label.split())[:55]
                if label:
                    samples.append(f"{role}: {label}")
            probe["dated_elements"] = samples[:12]
        except Exception as exc:
            probe["dated_probe_error"] = type(exc).__name__
        report["date_probe"] = probe
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

    # The date field turned out to be an ordinary input[type=text] holding an ISO
    # value: fill() writes it and Tab commits it (measured 2026-11-12 - every
    # earlier attempt failed because focus was not on the input, not because the
    # control was read-only or the format wrong). The clock panel has to be open
    # first, or the field does not exist yet.
    clock = page.get_by_role("button", name=re.compile(r"[0-9]{1,2}:[0-9]{2}"))
    if clock.count() > 0:
        clock.first.click()
        page.wait_for_timeout(3000)

    date_target = start.split()[0]
    date_used = None
    date_samples = []
    try:
        field = page.get_by_role("combobox", name="Start date")
        if field.count() > 0:
            field.first.fill(date_target)
            page.wait_for_timeout(800)
            if field.first.evaluate("e => e.value") == date_target:
                date_used = "fill-iso"
            # Tab commits; the picker reads the field on commit, not on input.
            page.keyboard.press("Tab")
            page.wait_for_timeout(1200)
    except Exception as exc:
        date_samples.append(f"error:{type(exc).__name__}")
    date_format_used = date_used

    time_readback = {}
    for label, value in (("Start time", start.split()[-1]),
                         ("End time", end.split()[-1])):
        try:
            field = page.get_by_role("combobox", name=label)
            if field.count() == 0:
                continue
            field.first.fill(value)
            page.wait_for_timeout(400)
            # Same lesson as the date: the picker reads the field on commit, not
            # on input, so Tab is what makes the value take effect.
            page.keyboard.press("Tab")
            page.wait_for_timeout(900)
            time_readback[label] = field.first.evaluate("e => e.value")
        except Exception as exc:
            time_readback[label] = type(exc).__name__

    # Move focus out instead of pressing Escape: Escape on an edited composer
    # raises the "Discard event?" dialog, which is one Enter away from losing the
    # whole draft.
    try:
        page.locator("input[placeholder=\"Add title\"]").first.click()
        page.wait_for_timeout(1500)
    except Exception:
        pass

    switch_state = _switch_on(page)
    if args.get("teams", True) and switch_state is not True:
        # It was off, or the state could not be read at all. Try the label route
        # too: the input sits inside a label, and clicking the input itself can
        # hit the visually hidden element instead of the switch.
        for attempt in ("input", "role", "label"):
            try:
                if attempt == "input":
                    page.locator("input[role=switch][aria-label=\"Microsoft Teams meeting\"]").first.click(force=True)
                elif attempt == "role":
                    page.get_by_role("switch", name=re.compile("Microsoft Teams meeting", re.I)).first.click(force=True)
                else:
                    page.get_by_text("Microsoft Teams meeting", exact=False).first.click()
                page.wait_for_timeout(1500)
            except Exception:
                continue
            if _switch_on(page) is True:
                break

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
    readback["date_ok"] = (start.split()[0] in readback["time"]) if readback["time"] else False
    readback["date_format_used"] = date_format_used
    readback["date_cell_labels"] = date_samples
    readback["time_fields"] = time_readback
    readback["time_ok"] = (start.split()[-1] in readback["time"] and end.split()[-1] in readback["time"])
    # Counting siblings of the attendees field returned 3 for one chip, so count
    # how many of the requested addresses are actually present as text instead.
    try:
        composer_text = " ".join(page.inner_text("body").split()).replace(" ", "")
        readback["attendee_chips"] = sum(
            1 for a in attendees if a.replace(" ", "").lower() in composer_text.lower())
    except Exception:
        pass

    shot = skill_home() / "meeting-create.png"
    try:
        page.screenshot(path=str(shot))
    except Exception:
        pass

    if args.get("dry_run"):
        _discard(page)
        # A dry run that reports ok:true while the time or the switch is wrong is
        # worse than useless: it is the rehearsal that says the play works.
        warnings = []
        if not readback.get("date_ok"):
            warnings.append(f"date not applied: {readback.get('time')!r}")
        if not readback.get("time_ok"):
            warnings.append(f"times not applied: {readback.get('time')!r}")
        if args.get("teams", True) and readback.get("teams_on") is not True:
            warnings.append("Teams switch did not turn on")
        if readback.get("attendee_chips") != len(attendees):
            warnings.append(f"attendees: {readback.get('attendee_chips')} of {len(attendees)} found")
        return {"ok": not warnings, "dry_run": True, "readback": readback,
                "problems": warnings, "screenshot": str(shot)}

    problems = []
    if not readback.get("date_ok"):
        problems.append(f"date not applied: {readback.get('time')!r} (wanted {start})")
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

def meeting_create_via_slot(page, args: dict) -> dict:
    """Open the composer from the calendar grid instead of editing its date field.

    Kept as an alternative route, not the primary one: the composer date field
    turned out to be an ordinary input and fill() writes it, so the slot route is
    no longer needed. It stays because the grid is the only way to create an
    event visually, and the day column has never been located (the ARIA search
    resolved to the navigation pane twice).

    The composer date control refused typing, fill() and role-based cell clicks
    (measured 2026-11-12), while the calendar itself is driven by ordinary buttons
    labelled like "20, November, 2026". Clicking the target day and then a time
    slot sets both natively, which removes the unreliable field from the path.
    """
    subject = (args.get("subject") or "").strip()
    start = (args.get("start") or "").strip()
    end = (args.get("end") or "").strip()
    attendees = [a.strip() for a in (args.get("attendees") or []) if a.strip()]
    if not subject or not start or not end:
        return {"ok": False, "error": "subject, start and end are required"}
    y, m, d = start.split()[0].split("-")
    hhmm = start.split()[-1]
    months = ["January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December"]
    month_name = months[int(m) - 1]

    page.goto(calendar_url(), wait_until="domcontentloaded")
    if not wait_for_app(page, ready_timeout()):
        return {"ok": False, "error": "calendar did not render"}

    day_button = page.get_by_role(
        "button", name=re.compile(rf"^{int(d)},\s*{month_name},\s*{y}$", re.I))
    nav_clicked = False
    if day_button.count() > 0:
        day_button.first.click()
        nav_clicked = True
        page.wait_for_timeout(4000)

    hour = int(hhmm.split(":")[0])
    candidates = [f"{hhmm}", f"{hour}:00", f"{hour} {chr(48)}0"]
    slot_probe = {"nav_clicked": nav_clicked, "tried": candidates, "samples": []}
    composer_sel = "input[placeholder=\"Add title\"]"

    def composer_is_open() -> bool:
        return page.locator(composer_sel).count() > 0

    # Route 1: a label match. Only existing events carry a time in their label, so
    # a match usually opens an event rather than a new one - the click only counts
    # when the composer really appears, otherwise close it and move on.
    for token in candidates[:2]:
        slot = page.locator(f"[aria-label*='{token}']")
        slot_probe["samples"] += [
            (slot.nth(i).get_attribute("aria-label") or "")[:60]
            for i in range(min(slot.count(), 5))]
        if slot.count() > 0:
            slot.first.click()
            page.wait_for_timeout(4000)
            if composer_is_open():
                break
            page.keyboard.press("Escape")   # the event card it opened
            page.wait_for_timeout(1500)

    # Empty slots carry no aria-label (only existing events do, e.g.
    # "... , 14:00 to 15:00, Tuesday"), so find the column header for the day and
    # the time gutter label, then click where they cross - which is what a person
    # does on this grid.
    if not composer_is_open():
        geo = {}
        try:
            col = page.locator(f"[aria-label*='{month_name} {int(d)}'], [aria-label*='{int(d)}, {month_name}']")
            if col.count() == 0:
                col = page.get_by_role("columnheader", name=re.compile(str(int(d))))
            gut = page.get_by_text(re.compile(rf"^\s*{hhmm.replace(chr(58), chr(58))}\s*$"))
            if gut.count() == 0:
                gut = page.get_by_text(hhmm, exact=False)
            geo["col_count"] = col.count()
            geo["gutter_count"] = gut.count()
            try:
                geo["col_labels"] = [
                    (col.nth(i).get_attribute("aria-label") or "")[:40]
                    for i in range(min(col.count(), 4))]
            except Exception:
                pass
            if col.count() > 0 and gut.count() > 0:
                col_box = col.first.bounding_box()
                gut_box = gut.first.bounding_box()
                geo["col_box"] = col_box
                geo["gut_box"] = gut_box
                if col_box and gut_box:
                    x = col_box["x"] + col_box["width"] / 2
                    y = gut_box["y"] + 6
                    page.mouse.click(x, y)
                    page.wait_for_timeout(5000)
                    clicked_slot = True
                    geo["clicked_at"] = [round(x), round(y)]
        except Exception as exc:
            geo["error"] = f"{type(exc).__name__}: {str(exc)[:60]}"
        slot_probe["geometry"] = geo
    composer = page.locator(composer_sel)
    slot_probe["composer_open"] = composer.count() > 0
    if composer.count() > 0:
        try:
            slot_probe["composer_time"] = " ".join(page.get_by_role(
                "button", name=re.compile(r"[0-9]{1,2}:[0-9]{2}")).first.inner_text().split())
        except Exception:
            pass
    return {"ok": composer.count() > 0, "via_slot": True, "slot_probe": slot_probe}

def grid_probe(page, args: dict) -> dict:
    """Dump the main calendar grid from the inside.

    Searching the whole page by label resolved to the left navigation pane twice,
    so this walks the grid container own descendants via evaluate - which also
    avoids the CSS-quoting that made the previous probe unreadable.
    """
    page.goto(calendar_url(), wait_until="domcontentloaded")
    if not wait_for_app(page, ready_timeout()):
        return {"ok": False, "error": "calendar did not render"}

    collector = """
    (el) => {
      const box = (n) => { const r = n.getBoundingClientRect();
        return {x: Math.round(r.x), y: Math.round(r.y),
                w: Math.round(r.width), h: Math.round(r.height)}; };
      const grab = (sel, limit) => Array.from(el.querySelectorAll(sel)).slice(0, limit)
        .map(n => ({tag: n.tagName.toLowerCase(), role: n.getAttribute("role") || "",
                    label: (n.getAttribute("aria-label") || "").slice(0, 60), box: box(n)}));
      return {
        columnheaders: grab("[role=columnheader]", 10),
        rowheaders: grab("[role=rowheader]", 5),
        gridcells: grab("[role=gridcell]", 8),
        labelled: grab("[aria-label]", 25)
      };
    }
    """

    report: dict = {}
    grids = page.locator("[role=grid]")
    report["grid_count"] = grids.count()
    best, best_area, seen = None, 0, []
    for i in range(min(grids.count(), 8)):
        try:
            node = grids.nth(i)
            box = node.bounding_box()
            label = (node.get_attribute("aria-label") or "")[:60]
        except Exception:
            continue
        seen.append({"i": i, "label": label, "box": box})
        if box and box["width"] * box["height"] > best_area:
            best, best_area = i, box["width"] * box["height"]
    report["grids"] = seen
    if best is None:
        return {"ok": False, "error": "no grid with a box", **report}

    grid = grids.nth(best)
    report["grid_box"] = grid.bounding_box()
    try:
        report["inside"] = grid.evaluate(collector)
    except Exception as exc:
        report["inside_error"] = type(exc).__name__

    gutter = []
    try:
        loc = page.get_by_text("14:00", exact=True)
        for i in range(min(loc.count(), 6)):
            gutter.append(loc.nth(i).bounding_box())
    except Exception as exc:
        gutter.append(str(type(exc).__name__))
    report["gutter_1400"] = gutter
    return {"ok": True, **report}

def date_probe(page, args: dict) -> dict:
    """Is the composer date field writable at all?

    Every earlier attempt either typed without reading the field back, or probed
    after the composer had already been dismissed, so the plain question - readonly
    or not, and does typing change `value` - has never actually been answered.
    """
    page.goto(calendar_url(), wait_until="domcontentloaded")
    if not wait_for_app(page, ready_timeout()):
        return {"ok": False, "error": "calendar did not render"}

    new_ev = page.get_by_role("button", name=re.compile("new event", re.I))
    if new_ev.count() == 0:
        return {"ok": False, "error": "no New event button"}
    new_ev.first.click()
    page.wait_for_timeout(6000)

    report: dict = {}
    clock = page.get_by_role("button", name=re.compile(r"[0-9]{1,2}:[0-9]{2}"))
    report["clock_found"] = clock.count() > 0
    if clock.count() > 0:
        clock.first.click()
        page.wait_for_timeout(3000)

    fields = {}
    for name in ("Start date", "Start time", "End time"):
        entry = {"found": False}
        try:
            loc = page.get_by_role("combobox", name=name)
            entry["count"] = loc.count()
            if loc.count() > 0:
                node = loc.first
                entry["found"] = True
                entry["tag"] = node.evaluate("e => e.tagName.toLowerCase()")
                for attr in ("type", "readonly", "disabled", "aria-readonly"):
                    entry[attr] = node.get_attribute(attr)
                entry["value_before"] = node.evaluate("e => e.value")
        except Exception as exc:
            entry["error"] = type(exc).__name__
        fields[name] = entry
    report["fields"] = fields

    # The field is input[type=text], not readonly, and already holds an ISO value -
    # so the format was never the problem, focus was. Try the three ways to put
    # text into it and report the field value after each, so the winner is known
    # instead of assumed.
    target = args.get("target_date") or time.strftime("%Y-%m-%d")
    attempts: dict = {}
    try:
        node = page.get_by_role("combobox", name="Start date").first

        node.fill(target)
        page.wait_for_timeout(800)
        attempts["fill"] = node.evaluate("e => e.value")

        if attempts["fill"] != target:
            node.evaluate("e => { e.focus(); if (e.select) e.select(); }")
            page.wait_for_timeout(300)
            page.keyboard.type(target, delay=50)
            page.wait_for_timeout(800)
            attempts["focus_type"] = node.evaluate("e => e.value")

        page.keyboard.press("Tab")
        page.wait_for_timeout(1500)
        attempts["after_tab"] = node.evaluate("e => e.value")
    except Exception as exc:
        attempts["error"] = type(exc).__name__ + ": " + str(exc)[:60]
    report["target_date"] = target
    report["set_attempts"] = attempts

    try:
        report["header"] = " ".join(page.get_by_role(
            "button", name=re.compile(r"[0-9]{1,2}:[0-9]{2}")).first.inner_text().split())
    except Exception:
        pass

    shot = skill_home() / "date-probe.png"
    try:
        page.screenshot(path=str(shot))
        report["screenshot"] = str(shot)
    except Exception:
        pass
    return {"ok": True, **report}

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
        takes_value = flag in ("--subject", "--start", "--end", "--attendee", "--body",
                              "--target-date")
        if takes_value and i + 1 < len(argv):
            value = argv[i + 1]
            if flag == "--attendee":
                args["attendees"].append(value)
            else:
                args[flag.lstrip("-").replace("-", "_")] = value
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
    if command not in ("serve", "calibrate", "meeting-create", "meeting-probe-slot", "grid-probe", "date-probe"):
        sys.exit(f"unknown command {command!r}; use serve, calibrate or meeting-create")
    headless = "--headless" in sys.argv[2:]

    # Pre-flight before any browser work: nothing to refuse here any more, the date
    # field turned out to be fillable. The guard that matters now runs inside the
    # composer, against the value the page actually shows.

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
            if command == "date-probe":
                emit({"action": "date-probe", **date_probe(page, _meeting_args(sys.argv[2:]))})
                return 0
            if command == "grid-probe":
                emit({"action": "grid-probe", **grid_probe(page, _meeting_args(sys.argv[2:]))})
                return 0
            if command == "meeting-probe-slot":
                emit({"action": "meeting-probe-slot",
                      **meeting_create_via_slot(page, _meeting_args(sys.argv[2:]))})
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
