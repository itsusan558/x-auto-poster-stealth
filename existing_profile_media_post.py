"""Post to X by reusing the local Chrome profile via Selenium."""

from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

import pyperclip
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DEBUG_DIR = DATA_DIR / "debug"
SYSTEM_USER_DATA_DIR = Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
COMPOSE_URL = "https://x.com/compose/post"
REMOTE_DEBUGGING_PORT = 9222
CHROME_CANDIDATES = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--media-path", help="image or video path")
    parser.add_argument("--text", default="", help="optional post text")
    parser.add_argument("--profile-directory", default="Default", help="Chrome profile directory")
    parser.add_argument("--profile-handle", default="", help="X handle used for verification")
    parser.add_argument("--wait-seconds", type=int, default=20, help="wait time after selecting media")
    parser.add_argument("--open-only", action="store_true", help="only open the compose page")
    parser.add_argument("--draft-only", action="store_true", help="fill the compose box but do not post")
    return parser.parse_args()


def resolve_chrome_path() -> Path:
    for path in CHROME_CANDIDATES:
        if path.exists():
            return path
    raise RuntimeError("Google Chrome が見つかりませんでした。")


def screenshot(driver: webdriver.Chrome, name: str) -> Path:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path = DEBUG_DIR / name
    driver.save_screenshot(str(path))
    return path


def relaunch_clean_chrome(profile_directory: str, target_url: str = COMPOSE_URL) -> None:
    subprocess.run(
        ["taskkill", "/IM", "chrome.exe", "/F"],
        check=False,
        capture_output=True,
        text=True,
    )
    time.sleep(1.0)
    subprocess.Popen(
        [
            str(resolve_chrome_path()),
            f"--user-data-dir={SYSTEM_USER_DATA_DIR}",
            f"--profile-directory={profile_directory}",
            f"--remote-debugging-port={REMOTE_DEBUGGING_PORT}",
            "--disable-extensions",
            "--hide-crash-restore-bubble",
            "--disable-session-crashed-bubble",
            target_url,
        ]
    )
    time.sleep(4.0)


def connect_driver() -> webdriver.Chrome:
    options = Options()
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{REMOTE_DEBUGGING_PORT}")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


def wait_for_any(driver: webdriver.Chrome, locators: list[tuple[str, str]], timeout: float = 20):
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        for by, selector in locators:
            try:
                return driver.find_element(by, selector)
            except Exception as exc:
                last_error = exc
        time.sleep(0.4)
    raise TimeoutException(str(last_error) if last_error else "element not found")


def wait_for_compose_ready(driver: webdriver.Chrome) -> None:
    driver.get(COMPOSE_URL)
    wait_for_any(
        driver,
        [
            (By.CSS_SELECTOR, 'div[data-testid="tweetTextarea_0"]'),
            (By.CSS_SELECTOR, 'div[role="textbox"][contenteditable="true"]'),
        ],
        timeout=25,
    )


def find_compose_box(driver: webdriver.Chrome):
    return wait_for_any(
        driver,
        [
            (By.CSS_SELECTOR, 'div[data-testid="tweetTextarea_0"]'),
            (By.CSS_SELECTOR, 'div[role="textbox"][contenteditable="true"]'),
        ],
        timeout=15,
    )


def read_compose_text(driver: webdriver.Chrome) -> str:
    return driver.execute_script(
        """
        const box = document.querySelector('div[data-testid="tweetTextarea_0"], div[role="textbox"][contenteditable="true"]');
        return box ? (box.innerText || box.textContent || '').trim() : '';
        """
    )


def focus_compose_box(driver: webdriver.Chrome) -> None:
    box = find_compose_box(driver)
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", box)
    ActionChains(driver).move_to_element(box).click(box).perform()
    time.sleep(0.8)


def set_compose_text(driver: webdriver.Chrome, text: str) -> None:
    if not text:
        return
    focus_compose_box(driver)
    box = find_compose_box(driver)
    box.send_keys(Keys.CONTROL, "a")
    box.send_keys(Keys.BACKSPACE)
    time.sleep(0.2)
    try:
        driver.execute_cdp_cmd("Input.insertText", {"text": text})
    except Exception:
        pyperclip.copy(text)
        box.send_keys(Keys.CONTROL, "v")
    time.sleep(1.0)

    actual = read_compose_text(driver)
    if text not in actual:
        focus_compose_box(driver)
        pyperclip.copy(text)
        box = find_compose_box(driver)
        box.send_keys(Keys.CONTROL, "a")
        box.send_keys(Keys.BACKSPACE)
        time.sleep(0.2)
        box.send_keys(Keys.CONTROL, "v")
        time.sleep(1.0)
        actual = read_compose_text(driver)
    if text not in actual:
        raise RuntimeError("投稿文を入力できませんでした。")


def attach_media(driver: webdriver.Chrome, media_path: Path, wait_seconds: int) -> None:
    file_input = wait_for_any(
        driver,
        [
            (By.CSS_SELECTOR, 'input[data-testid="fileInput"]'),
            (By.CSS_SELECTOR, 'input[type="file"]'),
        ],
        timeout=15,
    )
    driver.execute_script(
        "arguments[0].style.display='block'; arguments[0].style.visibility='visible';",
        file_input,
    )
    file_input.send_keys(str(media_path.resolve()))
    time.sleep(wait_seconds)


def find_post_button(driver: webdriver.Chrome):
    return wait_for_any(
        driver,
        [
            (By.CSS_SELECTOR, '[data-testid="tweetButton"]'),
            (By.CSS_SELECTOR, '[data-testid="tweetButtonInline"]'),
        ],
        timeout=15,
    )


def click_post(driver: webdriver.Chrome, expected_text: str) -> None:
    button = find_post_button(driver)
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", button)
    try:
        WebDriverWait(driver, 15).until(lambda d: button.get_attribute("aria-disabled") != "true")
    except Exception:
        pass

    try:
        button.click()
    except Exception:
        driver.execute_script("arguments[0].click();", button)
    time.sleep(3.0)

    remaining = read_compose_text(driver)
    if expected_text and expected_text in remaining:
        focus_compose_box(driver)
        ActionChains(driver).key_down(Keys.CONTROL).send_keys(Keys.ENTER).key_up(Keys.CONTROL).perform()
        time.sleep(5.0)


def verify_profile(driver: webdriver.Chrome, profile_handle: str, has_media: bool) -> None:
    if not profile_handle:
        return
    suffix = "media" if has_media else ""
    target = f"https://x.com/{profile_handle}/{suffix}".rstrip("/")
    driver.get(target)
    time.sleep(6.0)


def cleanup_driver(driver: webdriver.Chrome | None) -> None:
    if driver is None:
        return
    try:
        driver.service.stop()
    except Exception:
        pass


def main() -> int:
    args = parse_args()
    text = args.text.strip()
    media_path = Path(args.media_path) if args.media_path else None
    profile_handle = args.profile_handle.strip().lstrip("@")
    driver: webdriver.Chrome | None = None

    if media_path is not None and not media_path.exists():
        print(f'{{"success": false, "message": "メディアファイルが見つかりません: {media_path}"}}')
        return 1
    if not args.open_only and media_path is None and not text:
        print('{"success": false, "message": "投稿文かメディアを指定してください。"}')
        return 1

    try:
        relaunch_clean_chrome(args.profile_directory, COMPOSE_URL)
        driver = connect_driver()
        wait_for_compose_ready(driver)

        if args.open_only:
            screenshot(driver, "existing_profile_open_only.png")
            print('{"success": true, "message": "既存Chromeプロフィールで投稿画面を開きました。"}')
            return 0

        set_compose_text(driver, text)
        if media_path is not None:
            attach_media(driver, media_path, args.wait_seconds)
            screenshot(driver, "existing_profile_media_ready.png")
        else:
            screenshot(driver, "existing_profile_compose_ready.png")

        if args.draft_only:
            print('{"success": true, "message": "投稿画面への入力まで完了しました。"}')
            return 0

        click_post(driver, text)
        screenshot(driver, "existing_profile_after_post.png")
        verify_profile(driver, profile_handle, media_path is not None)
        if media_path is not None:
            screenshot(driver, "existing_profile_media_tab.png")
        elif profile_handle:
            screenshot(driver, "existing_profile_profile_tab.png")
        print('{"success": true, "message": "既存Chromeプロフィールで投稿しました。"}')
        return 0
    except Exception as exc:
        if driver is not None:
            screenshot(driver, "existing_profile_media_error.png")
        message = str(exc).replace('"', "'")
        print(f'{{"success": false, "message": "{message}"}}')
        return 1
    finally:
        cleanup_driver(driver)


if __name__ == "__main__":
    raise SystemExit(main())
