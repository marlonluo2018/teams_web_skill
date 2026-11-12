#!/usr/bin/env python3
"""One-step setup: Playwright, its Chromium build, and a config file if missing.

    py -3 scripts/install.py          (Windows)
    python3 scripts/install.py        (macOS / Linux)

Idempotent. It never signs in: a new session needs an IBM w3id passkey tap,
which only the human at the window can do.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

MIN_PYTHON = (3, 10)
HERE = Path(__file__).resolve().parent


def say(text: str) -> None:
    print(f"- {text}")


def main() -> int:
    if sys.version_info < MIN_PYTHON:
        say(f"python {sys.version.split()[0]} is too old; need {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+")
        return 1
    say(f"python {sys.version.split()[0]}")

    try:
        import playwright  # noqa: F401
    except ImportError:
        say("installing playwright")
        done = subprocess.run([sys.executable, "-m", "pip", "install", "playwright"])
        if done.returncode:
            say("pip install failed - fix that, then re-run")
            return done.returncode
    else:
        say("playwright already installed")

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            chromium = Path(pw.chromium.executable_path)
    except Exception as exc:
        say(f"playwright is installed but not usable: {exc}")
        return 1

    if chromium.exists():
        say(f"chromium present: {chromium}")
    else:
        say("downloading the Chromium build (~130 MB, one time)")
        done = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"])
        if done.returncode or not chromium.exists():
            say("chromium download failed - re-run, or install it by hand:")
            say(f"  {sys.executable} -m playwright install chromium")
            return done.returncode or 1
        say(f"chromium ready: {chromium}")

    sys.path.insert(0, str(HERE))
    import teams_driver as driver

    cfg = driver.config_path()
    if cfg.exists():
        say(f"config present: {cfg}")
    else:
        template = driver.SKILL_ROOT / "config.example.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        say(f"created {cfg} from config.example.json")

    state = driver.state_report()
    say(f"state  home={state['home']} portable={state['portable']}")

    print()
    print("Next: start a resident session (a window opens; tap your passkey there) -")
    print(f"    {sys.executable} {HERE / 'teams_driver.py'} serve")
    print("Then, in that same session, run calibrate before trusting meeting-create:")
    print("    {\"cmd\":\"calibrate\"}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
