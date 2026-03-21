"""Post to X by reusing the local Chrome profile via UI automation."""

from __future__ import annotations

import argparse
import ctypes
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

import pyperclip
from PIL import ImageGrab


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DEBUG_DIR = DATA_DIR / "debug"
SYSTEM_USER_DATA_DIR = Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
CHROME_PATH = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")

user32 = ctypes.windll.user32

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_TAB = 0x09
VK_SPACE = 0x20
VK_L = 0x4C
VK_RETURN = 0x0D

WM_PASTE = 0x0302
EM_SETSEL = 0x00B1
BM_CLICK = 0x00F5

EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
EnumChildProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--media-path", help="image or video path")
    parser.add_argument("--text", default="", help="optional post text")
    parser.add_argument("--profile-directory", default="Default", help="Chrome profile directory")
    parser.add_argument("--profile-handle", default="meetyoursxey", help="X handle used for verification")
    parser.add_argument("--wait-seconds", type=int, default=20, help="wait time after selecting media")
    return parser.parse_args()


def key(vk: int, up: bool = False) -> None:
    user32.keybd_event(vk, 0, 2 if up else 0, 0)


def tap(vk: int, delay: float = 0.05) -> None:
    key(vk)
    time.sleep(delay)
    key(vk, True)
    time.sleep(delay)


def chord(mod: int, vk: int) -> None:
    key(mod)
    time.sleep(0.05)
    key(vk)
    time.sleep(0.05)
    key(vk, True)
    key(mod, True)
    time.sleep(0.15)


def type_text(text: str) -> None:
    for ch in text:
        code = user32.VkKeyScanW(ord(ch))
        if code == -1:
            continue
        vk = code & 0xFF
        shift_state = (code >> 8) & 0xFF
        if shift_state & 1:
            key(VK_SHIFT)
            time.sleep(0.01)
        key(vk)
        time.sleep(0.02)
        key(vk, True)
        if shift_state & 1:
            key(VK_SHIFT, True)
        time.sleep(0.03)


def get_window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def get_class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def screenshot(name: str) -> Path:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path = DEBUG_DIR / name
    ImageGrab.grab().save(path)
    return path


def enumerate_windows() -> list[int]:
    windows: list[int] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        if user32.IsWindowVisible(hwnd) and get_window_text(hwnd):
            windows.append(hwnd)
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return windows


def find_x_window() -> int:
    for hwnd in enumerate_windows():
        if "X - Google Chrome" in get_window_text(hwnd):
            return hwnd
    raise RuntimeError("X の Chrome ウィンドウが見つかりませんでした。")


def focus_window(hwnd: int) -> None:
    user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.8)


def relaunch_clean_chrome(profile_directory: str) -> None:
    subprocess.run(
        ["taskkill", "/IM", "chrome.exe", "/F"],
        check=False,
        capture_output=True,
        text=True,
    )
    time.sleep(1.0)
    subprocess.Popen(
        [
            str(CHROME_PATH),
            f"--user-data-dir={SYSTEM_USER_DATA_DIR}",
            f"--profile-directory={profile_directory}",
            "--disable-extensions",
            "--hide-crash-restore-bubble",
            "--disable-session-crashed-bubble",
            "https://x.com/home",
        ]
    )
    time.sleep(6.0)


def open_compose(hwnd: int, text: str) -> None:
    focus_window(hwnd)
    tap(ord("N"), 0.04)
    time.sleep(1.8)
    if text:
        pyperclip.copy(text)
        chord(VK_CONTROL, 0x56)
        time.sleep(0.5)


def open_media_dialog() -> int:
    for _ in range(2):
        tap(VK_TAB, 0.06)
    tap(VK_SPACE, 0.08)
    time.sleep(1.4)
    for hwnd in enumerate_windows():
        if get_class_name(hwnd) == "#32770":
            return hwnd
    raise RuntimeError("ファイル選択ダイアログが開きませんでした。")


def find_dialog_controls(dialog_hwnd: int) -> tuple[int, int]:
    children: list[int] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        children.append(hwnd)
        return True

    user32.EnumChildWindows(dialog_hwnd, EnumChildProc(callback), 0)

    edit_hwnd = 0
    button_hwnd = 0
    for hwnd in children:
        class_name = get_class_name(hwnd)
        title = get_window_text(hwnd)
        if class_name == "Edit" and not edit_hwnd:
            edit_hwnd = hwnd
        if class_name == "Button" and ("開く" in title or "(&O)" in title or "ŠJ" in title):
            button_hwnd = hwnd

    if not edit_hwnd or not button_hwnd:
        raise RuntimeError("ファイル選択ダイアログの入力欄を見つけられませんでした。")
    return edit_hwnd, button_hwnd


def attach_media(media_path: Path, wait_seconds: int) -> None:
    dialog_hwnd = open_media_dialog()
    edit_hwnd, button_hwnd = find_dialog_controls(dialog_hwnd)
    pyperclip.copy(str(media_path.resolve()))
    user32.SendMessageW(edit_hwnd, EM_SETSEL, 0, -1)
    user32.SendMessageW(edit_hwnd, WM_PASTE, 0, 0)
    time.sleep(0.4)
    user32.SendMessageW(button_hwnd, BM_CLICK, 0, 0)
    time.sleep(wait_seconds)


def click_post_via_javascript(hwnd: int) -> None:
    focus_window(hwnd)
    chord(VK_CONTROL, VK_L)
    type_text("javascript:")
    pyperclip.copy("document.querySelector('[data-testid=tweetButton]')?.click()")
    chord(VK_CONTROL, 0x56)
    tap(VK_RETURN)
    time.sleep(8.0)


def verify_media_tab(hwnd: int, profile_handle: str) -> None:
    focus_window(hwnd)
    chord(VK_CONTROL, VK_L)
    pyperclip.copy(f"https://x.com/{profile_handle}/media")
    chord(VK_CONTROL, 0x56)
    tap(VK_RETURN)
    time.sleep(8.0)


def verify_profile(hwnd: int, profile_handle: str) -> None:
    focus_window(hwnd)
    chord(VK_CONTROL, VK_L)
    pyperclip.copy(f"https://x.com/{profile_handle}")
    chord(VK_CONTROL, 0x56)
    tap(VK_RETURN)
    time.sleep(8.0)


def main() -> int:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

    args = parse_args()
    text = args.text.strip()
    media_path = Path(args.media_path) if args.media_path else None
    if media_path is not None and not media_path.exists():
        print(f'{{"success": false, "message": "メディアファイルが見つかりません: {media_path}"}}')
        return 1
    if media_path is None and not text:
        print('{"success": false, "message": "投稿本文またはメディアを指定してください。"}')
        return 1

    try:
        relaunch_clean_chrome(args.profile_directory)
        x_hwnd = find_x_window()
        open_compose(x_hwnd, text)
        if media_path is not None:
            attach_media(media_path, args.wait_seconds)
            screenshot("existing_profile_media_ready.png")
        else:
            screenshot("existing_profile_compose_ready.png")
        click_post_via_javascript(x_hwnd)
        screenshot("existing_profile_after_post.png")
        if media_path is not None:
            verify_media_tab(x_hwnd, args.profile_handle)
            screenshot("existing_profile_media_tab.png")
        else:
            verify_profile(x_hwnd, args.profile_handle)
            screenshot("existing_profile_profile_tab.png")
        print('{"success": true, "message": "既存プロフィール経由で投稿を実行しました。"}')
        return 0
    except Exception as exc:
        screenshot("existing_profile_media_error.png")
        message = str(exc).replace('"', "'")
        print(f'{{"success": false, "message": "{message}"}}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
