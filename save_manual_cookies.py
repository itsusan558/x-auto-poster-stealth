"""Open a visible browser, wait for manual X login, and save cookies."""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
COOKIES_PATH = DATA_DIR / "cookies_0.json"
STATUS_PATH = DATA_DIR / "manual_login_status.json"
SCREENSHOT_PATH = Path(tempfile.gettempdir()) / "manual_login_timeout.png"


async def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    status = {
        "status": "starting",
        "updated_at": datetime.now().isoformat(),
        "message": "manual login helper started",
    }
    STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=False,
            slow_mo=50,
            channel="chrome",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        context = await browser.new_context(
            locale="ja-JP",
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = await context.new_page()
        await page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded", timeout=60000)

        status = {
            "status": "waiting_for_login",
            "updated_at": datetime.now().isoformat(),
            "message": "please log in manually in the opened browser",
        }
        STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Browser opened. Please log in to X manually.")

        success = False
        for _ in range(600):
            try:
                if await page.locator('[data-testid="tweetTextarea_0"]').count() > 0:
                    success = True
                    break
                if "home" in page.url and await page.locator('[data-testid="SideNav_NewTweet_Button"]').count() > 0:
                    success = True
                    break
            except Exception:
                pass
            await page.wait_for_timeout(1000)

        if success:
            cookies = await context.cookies()
            COOKIES_PATH.write_text(
                json.dumps(cookies, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            status = {
                "status": "completed",
                "updated_at": datetime.now().isoformat(),
                "message": "cookies saved",
                "cookies_count": len(cookies),
                "cookies_path": str(COOKIES_PATH),
            }
            STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Saved {len(cookies)} cookies to {COOKIES_PATH}")
            await browser.close()
            return 0

        await page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)
        status = {
            "status": "timeout",
            "updated_at": datetime.now().isoformat(),
            "message": "timed out waiting for manual login",
            "screenshot": str(SCREENSHOT_PATH),
        }
        STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Timed out waiting for login. Screenshot saved to {SCREENSHOT_PATH}")
        await browser.close()
        return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
