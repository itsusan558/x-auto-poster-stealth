"""Post to X by reusing the local Chrome profile via CDP automation."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import shutil
import time
from pathlib import Path

from PIL import ImageGrab
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DEBUG_DIR = DATA_DIR / "debug"
AUTOMATION_USER_DATA_DIR = DATA_DIR / "chrome-automation-profile"
SYSTEM_USER_DATA_DIR = Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
COMPOSE_URL = "https://x.com/compose/post"
REMOTE_DEBUGGING_HOST = "127.0.0.1"
CHROME_CANDIDATES = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
]
COMPOSE_BOX_SELECTOR = (
    '[data-testid="tweetTextarea_0"] [contenteditable="true"], '
    '[data-testid="tweetTextarea_0"][contenteditable="true"], '
    'div[role="textbox"][contenteditable="true"]'
)
POST_BUTTON_SELECTOR = '[data-testid="tweetButton"], [data-testid="tweetButtonInline"]'
FILE_INPUT_SELECTOR = 'input[data-testid="fileInput"], input[type="file"]'
LOGIN_SELECTOR = 'input[name="text"], input[name="password"]'
UPLOAD_ERROR_TEXT = "Some of your media failed to upload"
CACHE_DIR_NAMES = {
    "Cache",
    "Code Cache",
    "GPUCache",
    "GrShaderCache",
    "DawnCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "ShaderCache",
    "Crashpad",
    "blob_storage",
}
CACHE_FILE_PREFIXES = ("Singleton", "lockfile", ".org.chromium")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--media-path", action="append", help="image or video path")
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


def print_result(success: bool, message: str) -> None:
    print(json.dumps({"success": success, "message": message}, ensure_ascii=False))


def fallback_screenshot(name: str) -> Path:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path = DEBUG_DIR / name
    ImageGrab.grab().save(path)
    return path


def save_page_screenshot(page: Page | None, name: str) -> Path:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path = DEBUG_DIR / name
    if page is not None:
        try:
            page.screenshot(path=str(path), full_page=True)
            return path
        except Exception:
            pass
    return fallback_screenshot(name)


def normalize_text(value: str) -> str:
    return (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((REMOTE_DEBUGGING_HOST, 0))
        return int(sock.getsockname()[1])


def wait_for_port(host: str, port: int, timeout_seconds: float = 30.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError("Chrome のデバッグポートが起動しませんでした。")


def ignore_copy_patterns(_directory: str, names: list[str]) -> set[str]:
    ignored = {name for name in names if name in CACHE_DIR_NAMES}
    ignored.update(name for name in names if name.startswith(CACHE_FILE_PREFIXES))
    return ignored


def prepare_automation_profile(profile_directory: str) -> Path:
    source_profile_dir = SYSTEM_USER_DATA_DIR / profile_directory
    if not source_profile_dir.exists():
        raise RuntimeError(f"Chrome プロフィールが見つかりませんでした: {profile_directory}")

    if AUTOMATION_USER_DATA_DIR.exists():
        shutil.rmtree(AUTOMATION_USER_DATA_DIR, ignore_errors=True)
    AUTOMATION_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    local_state = SYSTEM_USER_DATA_DIR / "Local State"
    if local_state.exists():
        shutil.copy2(local_state, AUTOMATION_USER_DATA_DIR / "Local State")

    shutil.copytree(
        source_profile_dir,
        AUTOMATION_USER_DATA_DIR / profile_directory,
        ignore=ignore_copy_patterns,
        dirs_exist_ok=True,
    )
    return AUTOMATION_USER_DATA_DIR


def relaunch_clean_chrome(profile_directory: str, target_url: str = COMPOSE_URL) -> int:
    debug_port = find_free_port()
    subprocess.run(
        ["taskkill", "/IM", "chrome.exe", "/F"],
        check=False,
        capture_output=True,
        text=True,
    )
    time.sleep(1.0)
    automation_user_data_dir = prepare_automation_profile(profile_directory)
    subprocess.Popen(
        [
            str(resolve_chrome_path()),
            f"--user-data-dir={automation_user_data_dir}",
            f"--profile-directory={profile_directory}",
            f"--remote-debugging-address={REMOTE_DEBUGGING_HOST}",
            f"--remote-debugging-port={debug_port}",
            "--new-window",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-default-apps",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
            "--disable-blink-features=AutomationControlled",
            target_url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    wait_for_port(REMOTE_DEBUGGING_HOST, debug_port, timeout_seconds=30.0)
    time.sleep(1.5)
    return debug_port


def get_or_create_page(context) -> Page:
    for page in reversed(context.pages):
        if "x.com" in page.url or page.url in {"", "about:blank"}:
            return page
    return context.new_page()


def wait_for_compose_box(page: Page, timeout_ms: int = 45000):
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        compose_box = page.locator(COMPOSE_BOX_SELECTOR).first
        try:
            if compose_box.count() and compose_box.is_visible():
                return compose_box
        except Exception:
            pass

        if "graduated-access" in page.url or page.locator("text=Unlock more on X").count():
            raise RuntimeError("X 側の段階的アクセス制限で投稿画面を開けませんでした。")
        if page.locator(LOGIN_SELECTOR).count() or "flow/login" in page.url:
            raise RuntimeError("X にログインした Chrome プロフィールで開いてください。")
        if page.locator("text=Something went wrong").count():
            page.reload(wait_until="domcontentloaded")
        time.sleep(0.4)
    raise RuntimeError("投稿欄が見つかりませんでした。")


def read_compose_text(page: Page) -> str:
    box = wait_for_compose_box(page, timeout_ms=15000)
    return box.evaluate("(node) => (node.innerText || node.textContent || '').trim()")


def set_compose_text(page: Page, text: str) -> None:
    box = wait_for_compose_box(page)
    box.click()
    box.press("Control+A")
    box.press("Backspace")
    page.wait_for_timeout(250)
    if not text:
        return
    page.keyboard.insert_text(text)
    page.wait_for_timeout(600)
    actual = normalize_text(read_compose_text(page))
    if actual == normalize_text(text):
        return
    box.evaluate(
        """
        (node, value) => {
          node.focus();
          const selection = window.getSelection();
          if (selection) {
            const range = document.createRange();
            range.selectNodeContents(node);
            selection.removeAllRanges();
            selection.addRange(range);
          }
          document.execCommand('insertText', false, value);
          node.dispatchEvent(new InputEvent('input', {
            bubbles: true,
            inputType: 'insertText',
            data: value,
          }));
        }
        """,
        text,
    )
    page.wait_for_timeout(400)
    actual = normalize_text(read_compose_text(page))
    if actual != normalize_text(text):
        raise RuntimeError("投稿本文を入力できませんでした。")


def attach_media(page: Page, media_paths: list[Path], wait_seconds: int) -> None:
    input_locator = page.locator(FILE_INPUT_SELECTOR).first
    input_locator.wait_for(state="attached", timeout=15000)
    input_locator.set_input_files([str(path.resolve()) for path in media_paths])
    deadline = time.time() + max(float(wait_seconds), 6.0)
    while time.time() < deadline:
        if page.locator(f"text={UPLOAD_ERROR_TEXT}").count():
            raise RuntimeError("メディアのアップロードに失敗しました。")
        if not page.locator("div[role='progressbar']").count():
            page.wait_for_timeout(800)
            return
        time.sleep(0.5)
    page.wait_for_timeout(1200)


def submit_post(page: Page, text: str) -> None:
    button = page.locator(POST_BUTTON_SELECTOR).first
    button.wait_for(state="visible", timeout=15000)
    if not button.is_enabled():
        raise RuntimeError("投稿ボタンが有効になりませんでした。")
    button.click()
    page.wait_for_timeout(2500)

    expected_text = normalize_text(text)
    deadline = time.time() + 20.0
    while time.time() < deadline:
        if "graduated-access" in page.url or page.locator("text=Unlock more on X").count():
            raise RuntimeError("X 側の段階的アクセス制限で投稿できませんでした。")
        if page.locator(LOGIN_SELECTOR).count() or "flow/login" in page.url:
            raise RuntimeError("投稿の途中で X のログイン画面に戻されました。")

        compose_box = page.locator(COMPOSE_BOX_SELECTOR).first
        current_text = ""
        try:
            if compose_box.count() and compose_box.is_visible():
                current_text = normalize_text(read_compose_text(page))
            else:
                return
        except Exception:
            return

        try:
            still_enabled = button.is_enabled()
        except Exception:
            still_enabled = False

        if expected_text and current_text != expected_text:
            return
        if not expected_text and not still_enabled:
            return
        time.sleep(0.5)

    if expected_text and normalize_text(read_compose_text(page)) == expected_text:
        raise RuntimeError("投稿後も本文が残っており、完了を確認できませんでした。")


def verify_target(page: Page, profile_handle: str, has_media: bool) -> None:
    if not profile_handle:
        return
    suffix = "/media" if has_media else ""
    page.goto(f"https://x.com/{profile_handle}{suffix}", wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)


def main() -> int:
    args = parse_args()
    text = args.text.strip()
    media_paths = [Path(value) for value in (args.media_path or [])]
    profile_handle = args.profile_handle.strip().lstrip("@")

    missing_paths = [str(path) for path in media_paths if not path.exists()]
    if missing_paths:
        print_result(False, f"メディアファイルが見つかりません: {missing_paths[0]}")
        return 1
    if not args.open_only and not media_paths and not text:
        print_result(False, "投稿本文かメディアを指定してください。")
        return 1

    playwright = None
    page: Page | None = None

    try:
        debug_port = relaunch_clean_chrome(args.profile_directory, COMPOSE_URL)
        playwright = sync_playwright().start()
        browser = playwright.chromium.connect_over_cdp(f"http://{REMOTE_DEBUGGING_HOST}:{debug_port}")
        if not browser.contexts:
            raise RuntimeError("Chrome の既存プロフィールに接続できませんでした。")
        context = browser.contexts[0]
        page = get_or_create_page(context)
        page.goto(COMPOSE_URL, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1200)

        if args.open_only:
            save_page_screenshot(page, "existing_profile_open_only.png")
            print_result(True, "既存Chromeプロフィールで投稿画面を開きました。")
            return 0

        wait_for_compose_box(page)
        set_compose_text(page, text)
        if media_paths:
            attach_media(page, media_paths, args.wait_seconds)
            save_page_screenshot(page, "existing_profile_media_ready.png")
        else:
            save_page_screenshot(page, "existing_profile_compose_ready.png")

        if args.draft_only:
            print_result(True, "投稿画面への入力まで完了しました。")
            return 0

        submit_post(page, text)
        save_page_screenshot(page, "existing_profile_after_post.png")
        verify_target(page, profile_handle, bool(media_paths))
        if media_paths:
            save_page_screenshot(page, "existing_profile_media_tab.png")
        elif profile_handle:
            save_page_screenshot(page, "existing_profile_profile_tab.png")
        print_result(True, "既存Chromeプロフィールで投稿しました。")
        return 0
    except PlaywrightTimeoutError:
        save_page_screenshot(page, "existing_profile_media_error.png")
        print_result(False, "X の画面応答がタイムアウトしました。")
        return 1
    except Exception as exc:
        save_page_screenshot(page, "existing_profile_media_error.png")
        print_result(False, str(exc).replace('"', "'"))
        return 1
    finally:
        if playwright is not None:
            playwright.stop()


if __name__ == "__main__":
    raise SystemExit(main())
