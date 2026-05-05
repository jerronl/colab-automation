"""Standalone utilities — not imported by the main package by default."""
from __future__ import annotations
import asyncio
import glob
import subprocess
import time
import urllib.request
from pathlib import Path


def ensure_browser(cdp_port: int = 9223, profile: Path | None = None) -> subprocess.Popen | None:
    """
    Start Chromium with CDP if not already running on cdp_port.
    Returns the Popen process if launched, None if already running.
    Raises RuntimeError if Chromium cannot be found or doesn't start.
    """
    if profile is None:
        profile = Path.home() / ".playwright-profiles" / "autocolab"
    profile.mkdir(parents=True, exist_ok=True)

    # Already running?
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/version", timeout=2)
        print(f"[ensure_browser] Browser already running on port {cdp_port}")
        return None
    except Exception:
        pass

    chrome = _find_chromium()
    proc = subprocess.Popen(
        [
            "nohup", chrome,
            f"--remote-debugging-port={cdp_port}",
            f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check",
            "--no-sandbox", "--disable-dev-shm-usage", "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/version", timeout=1)
            print(f"[ensure_browser] Browser started PID={proc.pid}")
            return proc
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("Browser did not start in 20s")


def setup_account(cdp_port: int = 9223) -> None:
    """
    Open a Colab tab for the user to log in to a new Google account.
    Starts the browser with the dedicated profile if not already running.
    The login session is persisted in the browser profile automatically.
    """
    ensure_browser(cdp_port=cdp_port)
    asyncio.run(_open_tab(cdp_port, "https://colab.research.google.com/"))
    print("[setup_account] Log in to your Google account in the opened tab.")
    print("[setup_account] Close the tab when done — session is saved automatically.")


async def _open_tab(cdp_port: int, url: str) -> None:
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        await page.goto(url)


def _find_chromium() -> str:
    for pat in [
        str(Path.home() / ".cache/ms-playwright/chromium-*/chrome-linux64/chrome"),
        str(Path.home() / ".cache/ms-playwright/chromium-*/chrome-linux/chrome"),
    ]:
        hits = glob.glob(pat)
        if hits:
            return sorted(hits)[-1]
    raise RuntimeError("Chromium not found — run: playwright install chromium")


def check_rclone_remote(remote_name: str = "gdrive") -> bool:
    """
    Check if rclone remote is configured and accessible.
    If not configured, prompt user to run rclone config.
    Returns True if remote exists and is working, False otherwise.
    """
    try:
        result = subprocess.run(
            ["rclone", "listremotes"],
            capture_output=True,
            text=True,
            timeout=5
        )
        remotes = result.stdout.strip().split('\n')
        configured = any(r.rstrip(':') == remote_name for r in remotes if r)
        if not configured:
            print(f"❌ rclone remote '{remote_name}' not configured.")
            print(f"   Run: rclone config")
            print(f"   Then create a remote named '{remote_name}' pointing to Google Drive.")
            return False
        return True
    except FileNotFoundError:
        print("❌ rclone not found. Install with: curl https://rclone.org/install.sh | sudo bash")
        return False
    except Exception as e:
        print(f"⚠️  Could not check rclone: {e}")
        return False
