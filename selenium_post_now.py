"""Post a single message to X using Selenium and real Chrome."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pyperclip
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.common.exceptions import WebDriverException
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = DATA_DIR / "config.json"
COOKIES_PATH = DATA_DIR / "selenium_cookies_0.json"
DEBUG_DIR = DATA_DIR / "debug"
SYSTEM_USER_DATA_DIR = Path(os.environ["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", help="post text")
    parser.add_argument("--text-file", help="UTF-8 file containing the post text")
    parser.add_argument("--media-path", help="optional image/video file to attach")
    parser.add_argument("--username", help="temporary X username override")
    parser.add_argument("--password", help="temporary X password override")
    parser.add_argument("--use-system-profile", action="store_true", help="use the normal Chrome profile")
    parser.add_argument("--direct-profile", action="store_true", help="use the real Chrome profile without copying")
    parser.add_argument("--existing-session-only", action="store_true", help="fail instead of logging in when not already signed in")
    parser.add_argument("--debugger-address", help="attach to an already running Chrome debugging endpoint")
    parser.add_argument("--profile-directory", default="Default", help="Chrome profile directory name")
    return parser.parse_args()


def load_account() -> dict:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8", errors="ignore"))
    return config["accounts"][0]


def load_text(args: argparse.Namespace) -> str:
    if args.text_file:
        return Path(args.text_file).read_text(encoding="utf-8").lstrip("\ufeff").strip()
    return (args.text or "").strip()


def save_cookies(driver: webdriver.Chrome) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    COOKIES_PATH.write_text(json.dumps(driver.get_cookies(), ensure_ascii=False, indent=2), encoding="utf-8")


def save_debug(driver: webdriver.Chrome, name: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    (DEBUG_DIR / f"{name}.txt").write_text(driver.find_element(By.TAG_NAME, "body").text, encoding="utf-8")
    driver.save_screenshot(str(DEBUG_DIR / f"{name}.png"))


def is_graduated_access_blocked(driver: webdriver.Chrome) -> bool:
    try:
        body = driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        body = ""
    return (
        "graduated-access" in driver.current_url
        or "Unlock more on X" in body
        or "To make X great for everyone" in body
    )


def prepare_profile_copy(profile_directory: str) -> Path:
    temp_root = Path(tempfile.mkdtemp(prefix="chrome_profile_copy_"))
    source_profile = SYSTEM_USER_DATA_DIR / profile_directory
    target_profile = temp_root / profile_directory

    shutil.copy2(SYSTEM_USER_DATA_DIR / "Local State", temp_root / "Local State")
    shutil.copytree(
        source_profile,
        target_profile,
        ignore=shutil.ignore_patterns(
            "Cache",
            "Code Cache",
            "GPUCache",
            "DawnCache",
            "GrShaderCache",
            "ShaderCache",
            "Crashpad",
            "Sessions",
            "Current Session",
            "Current Tabs",
            "Last Session",
            "Last Tabs",
            "Service Worker\\CacheStorage",
        ),
        dirs_exist_ok=True,
    )
    return temp_root


def build_driver(args: argparse.Namespace, user_data_dir: Path | None = None) -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    if args.debugger_address:
        options.add_experimental_option("debuggerAddress", args.debugger_address)
        return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-first-run")
    options.add_argument("--hide-crash-restore-bubble")
    options.add_argument("--disable-session-crashed-bubble")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    if user_data_dir is not None:
        options.add_argument(f"--user-data-dir={user_data_dir}")
        options.add_argument(f"--profile-directory={args.profile_directory}")

    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)


def restore_cookies(driver: webdriver.Chrome) -> bool:
    if not COOKIES_PATH.exists():
        return False
    driver.get("https://x.com/")
    time.sleep(2)
    cookies = json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
    for cookie in cookies:
        cookie = cookie.copy()
        cookie.pop("sameSite", None)
        try:
            driver.add_cookie(cookie)
        except Exception:
            pass
    driver.get("https://x.com/home")
    time.sleep(4)
    return True


def wait_any(wait: WebDriverWait, selectors: list[tuple[str, str]]):
    last_error = None
    for by, value in selectors:
        try:
            return wait.until(EC.visibility_of_element_located((by, value)))
        except Exception as exc:
            last_error = exc
    if last_error:
        raise last_error
    raise TimeoutException("no selector matched")


def click_any(wait: WebDriverWait, selectors: list[tuple[str, str]]) -> bool:
    for by, value in selectors:
        try:
            elem = wait.until(EC.element_to_be_clickable((by, value)))
            elem.click()
            return True
        except Exception:
            continue
    return False


def find_visible_post_button(driver: webdriver.Chrome):
    for selector in ('[data-testid="tweetButtonInline"]', '[data-testid="tweetButton"]'):
        for button in driver.find_elements(By.CSS_SELECTOR, selector):
            try:
                if button.is_displayed():
                    return button
            except Exception:
                continue
    return None


def wait_for_media_ready(driver: webdriver.Chrome, timeout: int = 120) -> None:
    def preview_present(current_driver: webdriver.Chrome) -> bool:
        preview_selectors = (
            '[data-testid="attachments"]',
            '[data-testid="mediaPreview"]',
            'img[src^="blob:"]',
            'video',
        )
        return any(current_driver.find_elements(By.CSS_SELECTOR, selector) for selector in preview_selectors)

    WebDriverWait(driver, min(timeout, 30)).until(preview_present)

    def upload_finished(current_driver: webdriver.Chrome) -> bool:
        if current_driver.find_elements(By.CSS_SELECTOR, '[role="progressbar"]'):
            return False
        button = find_visible_post_button(current_driver)
        if button is None:
            return False
        disabled = (button.get_attribute("disabled") or "").lower()
        aria_disabled = (button.get_attribute("aria-disabled") or "").lower()
        return disabled not in {"true", "disabled"} and aria_disabled != "true"

    WebDriverWait(driver, timeout).until(upload_finished)


def login(driver: webdriver.Chrome, wait: WebDriverWait, username: str, password: str) -> None:
    driver.get("https://x.com/i/flow/login")
    time.sleep(4)

    body = driver.find_element(By.TAG_NAME, "body").text
    if "問題が発生しました" in body or "Something went wrong" in body:
        driver.refresh()
        time.sleep(4)

    username_input = wait_any(
        wait,
        [
            (By.CSS_SELECTOR, 'input[autocomplete="username"]'),
            (By.NAME, "text"),
            (By.CSS_SELECTOR, 'input[type="text"]'),
        ],
    )
    username_input.click()
    username_input.clear()
    username_input.send_keys(username)
    time.sleep(0.5)

    if not click_any(
        wait,
        [
            (By.XPATH, '//button[normalize-space()="次へ"]'),
            (By.XPATH, '//button[normalize-space()="Next"]'),
            (By.XPATH, '//*[@role="button" and normalize-space()="次へ"]'),
            (By.XPATH, '//*[@role="button" and normalize-space()="Next"]'),
        ],
    ):
        username_input.send_keys(Keys.ENTER)

    time.sleep(3)
    body = driver.find_element(By.TAG_NAME, "body").text
    if "現在ログインできません" in body or "Could not log you in now" in body:
        save_debug(driver, "login_restricted")
        raise RuntimeError("X 側で一時的にログイン制限がかかっています。")

    try:
        verify_input = WebDriverWait(driver, 3).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, 'input[data-testid="ocfEnterTextTextInput"]'))
        )
        verify_input.clear()
        verify_input.send_keys(username)
        time.sleep(0.5)
        click_any(
            wait,
            [
                (By.CSS_SELECTOR, '[data-testid="ocfEnterTextNextButton"]'),
                (By.XPATH, '//button[normalize-space()="次へ"]'),
                (By.XPATH, '//button[normalize-space()="Next"]'),
            ],
        )
        time.sleep(3)
    except Exception:
        pass

    password_input = wait_any(
        wait,
        [
            (By.NAME, "password"),
            (By.CSS_SELECTOR, 'input[type="password"]'),
        ],
    )
    password_input.click()
    password_input.clear()
    password_input.send_keys(password)
    time.sleep(0.5)

    if not click_any(
        wait,
        [
            (By.CSS_SELECTOR, '[data-testid="LoginForm_Login_Button"]'),
            (By.XPATH, '//button[normalize-space()="ログイン"]'),
            (By.XPATH, '//button[normalize-space()="Log in"]'),
        ],
    ):
        password_input.send_keys(Keys.ENTER)

    time.sleep(5)
    driver.get("https://x.com/home")
    time.sleep(4)
    wait_any(
        wait,
        [
            (By.CSS_SELECTOR, '[data-testid="tweetTextarea_0"]'),
            (By.CSS_SELECTOR, '[data-testid="SideNav_NewTweet_Button"]'),
        ],
    )
    save_cookies(driver)


def post_now(driver: webdriver.Chrome, wait: WebDriverWait, content: str, media_path: str | None = None) -> None:
    driver.get("https://x.com/home")
    time.sleep(4)

    if is_graduated_access_blocked(driver):
        save_debug(driver, "graduated_access_blocked")
        raise RuntimeError(
            "X 側で段階的アクセス制限がかかっています。しばらく通常利用して解除されるまで、このアカウントでは投稿できません。"
        )

    compose = wait_any(
        wait,
        [
            (By.CSS_SELECTOR, '[data-testid="tweetTextarea_0"]'),
        ],
    )
    compose.click()
    time.sleep(1)

    if content:
        pyperclip.copy(content)
        ActionChains(driver).key_down(Keys.CONTROL).send_keys("v").key_up(Keys.CONTROL).perform()
        time.sleep(1.5)

    if media_path:
        absolute_media_path = str(Path(media_path).resolve())
        file_inputs = driver.find_elements(By.CSS_SELECTOR, 'input[type="file"]')
        if not file_inputs:
            raise RuntimeError("画像アップロード欄が見つかりませんでした。")
        file_inputs[0].send_keys(absolute_media_path)
        wait_for_media_ready(driver)

    post_button = find_visible_post_button(driver)
    if post_button is not None:
        driver.execute_script("arguments[0].click();", post_button)
        time.sleep(4)
        return

    if not click_any(
        wait,
        [
            (By.CSS_SELECTOR, '[data-testid="tweetButtonInline"]'),
            (By.CSS_SELECTOR, '[data-testid="tweetButton"]'),
            (By.XPATH, '//button[normalize-space()="投稿する"]'),
            (By.XPATH, '//button[normalize-space()="Post"]'),
        ],
    ):
        raise RuntimeError("投稿ボタンが見つかりませんでした。")

    time.sleep(4)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    args = parse_args()
    content = load_text(args)
    if not content and not args.media_path:
        print(json.dumps({"success": False, "message": "投稿本文が空です。"}, ensure_ascii=False))
        return 1

    account = load_account()
    username = args.username or account.get("username", "")
    password = args.password or account.get("password", "")

    driver = None
    temp_profile_dir = None
    try:
        if args.use_system_profile:
            if args.direct_profile:
                driver = build_driver(args, SYSTEM_USER_DATA_DIR)
            else:
                temp_profile_dir = prepare_profile_copy(args.profile_directory)
                driver = build_driver(args, temp_profile_dir)
        else:
            driver = build_driver(args)
        wait = WebDriverWait(driver, 15)
        restored = False if args.use_system_profile else restore_cookies(driver)
        logged_in = False
        if args.use_system_profile or args.debugger_address:
            driver.get("https://x.com/home")
            time.sleep(4)
        if restored:
            try:
                wait_any(wait, [(By.CSS_SELECTOR, '[data-testid="tweetTextarea_0"]')])
                logged_in = True
            except Exception:
                logged_in = False
        else:
            try:
                wait_any(
                    wait,
                    [
                        (By.CSS_SELECTOR, '[data-testid="tweetTextarea_0"]'),
                        (By.CSS_SELECTOR, '[data-testid="SideNav_NewTweet_Button"]'),
                    ],
                )
                logged_in = True
            except Exception:
                logged_in = False

        if not logged_in:
            if args.existing_session_only:
                try:
                    save_debug(driver, "existing_session_not_logged_in")
                except Exception:
                    pass
                raise RuntimeError("既存プロフィールではまだ X にログイン済みではありません。新規ログインは行わず停止しました。")
            login(driver, wait, username, password)

        post_now(driver, wait, content, args.media_path)
        save_cookies(driver)
        print(json.dumps({"success": True, "message": "投稿に成功しました。"}, ensure_ascii=False))
        return 0
    except WebDriverException as exc:
        message = str(exc)
        if "user data directory is already in use" in message:
            print(
                json.dumps(
                    {
                        "success": False,
                        "message": "通常の Chrome プロファイルが使用中です。Chrome を完全終了してから再試行してください。",
                    },
                    ensure_ascii=False,
                )
            )
            return 2
        print(json.dumps({"success": False, "message": message}, ensure_ascii=False))
        return 2
    except Exception as exc:
        try:
            save_debug(driver, "last_error")
        except Exception:
            pass
        print(json.dumps({"success": False, "message": str(exc)}, ensure_ascii=False))
        return 2
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        if temp_profile_dir is not None:
            shutil.rmtree(temp_profile_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
