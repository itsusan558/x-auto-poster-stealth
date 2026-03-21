"""Open Chrome for manual X login and save Selenium cookies."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
COOKIES_PATH = DATA_DIR / "selenium_cookies_0.json"
STATUS_PATH = DATA_DIR / "manual_login_status.json"
SYSTEM_USER_DATA_DIR = Path(os.environ["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data"


def write_status(status: str, message: str, **extra) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "updated_at": datetime.now().isoformat(),
        "message": message,
    }
    payload.update(extra)
    STATUS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def logged_in(driver: webdriver.Chrome) -> bool:
    selectors = [
        (By.CSS_SELECTOR, '[data-testid="tweetTextarea_0"]'),
        (By.CSS_SELECTOR, '[data-testid="SideNav_NewTweet_Button"]'),
    ]
    for by, value in selectors:
        try:
            WebDriverWait(driver, 2).until(EC.visibility_of_element_located((by, value)))
            return True
        except Exception:
            continue
    return False


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    write_status("starting", "manual selenium login helper started")

    options = webdriver.ChromeOptions()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(f"--user-data-dir={SYSTEM_USER_DATA_DIR}")
    options.add_argument("--profile-directory=Default")

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    try:
        driver.get("https://x.com/i/flow/login")
        write_status("waiting_for_login", "opened Chrome for manual X login")
        print("Chrome を開きました。X に手動ログインしてください。")

        for _ in range(900):
            if logged_in(driver):
                cookies = driver.get_cookies()
                COOKIES_PATH.write_text(
                    json.dumps(cookies, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                write_status(
                    "completed",
                    "cookies saved",
                    cookies_count=len(cookies),
                    cookies_path=str(COOKIES_PATH),
                )
                print(f"Cookie を保存しました: {COOKIES_PATH}")
                return 0
            time.sleep(1)

        write_status("timeout", "timed out waiting for manual login")
        print("手動ログイン待機がタイムアウトしました。")
        return 2
    finally:
        driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
