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

# Hosts that mean "the app rendered" rather than "the sign-in flow is running".
APP_HOSTS = ("teams.cloud.microsoft", "teams.microsoft.com",
             "outlook.office.com", "m365.cloud.microsoft")
# Text that only appears during sign-in, so it vetoes the app verdict.
LOGIN_HINTS = ("pick an account", "sign in to your account", "enter password",
               "use another account", "sign-in with your passkey", "verifying")


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
    if any(hint in low for hint in LOGIN_HINTS):
        return False, "sign-in screen"
    if not any(host.endswith(h) for h in APP_HOSTS):
        return False, host or "no host"
    return (len(body) > 40), "app"


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
        report["composer_checkboxes"] = controls(page, "[role=checkbox]")[:20]
        report["composer_teams_controls"] = [c for c in controls(page, "button, [role=switch], [role=checkbox]")
                                             if "team" in c.lower() or "online" in c.lower()][:15]
        report["composer_buttons"] = controls(page, "button")[:40]
        shot = skill_home() / "calibrate-composer.png"
        try:
            page.screenshot(path=str(shot))
            report["screenshot"] = str(shot)
        except Exception:
            pass
        page.keyboard.press("Escape")
    return report


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

A new browser session needs an IBM w3id passkey tap, so the browser stays open:
start one, keep it, and send commands ({"cmd":"status"}, {"cmd":"calibrate"},
{"cmd":"quit"}) on stdin.
"""
def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if command in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if command not in ("serve", "calibrate"):
        sys.exit(f"unknown command {command!r}; use serve or calibrate")
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
            return serve(page)
        finally:
            ctx.close()


if __name__ == "__main__":
    raise SystemExit(main())
