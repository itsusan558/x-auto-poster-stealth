"""Post to X by reusing the local Chrome profile via UI automation."""

from __future__ import annotations

import argparse
import ctypes
import json
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
COMPOSE_URL = "https://x.com/compose/post"
CHROME_CANDIDATES = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
]

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

VK_MENU = 0x12
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_BACK = 0x08
VK_L = 0x4C
VK_RETURN = 0x0D

WM_PASTE = 0x0302
EM_SETSEL = 0x00B1
BM_CLICK = 0x00F5
SW_RESTORE = 9
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2

EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
EnumChildProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


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


def get_window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def get_window_thread(hwnd: int) -> int:
    pid = wintypes.DWORD()
    return user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))


def screenshot(name: str) -> Path:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path = DEBUG_DIR / name
    ImageGrab.grab().save(path)
    return path


def enumerate_windows(include_untitled: bool = False) -> list[int]:
    windows: list[int] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        title = get_window_text(hwnd)
        if include_untitled or title:
            windows.append(hwnd)
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return windows


def find_x_window(chrome_pid: int, timeout_seconds: float = 30.0) -> int:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        for hwnd in enumerate_windows(include_untitled=True):
            class_name = get_class_name(hwnd)
            title = get_window_text(hwnd)
            if get_window_pid(hwnd) == chrome_pid and class_name == "Chrome_WidgetWin_1" and title != "Default IME":
                return hwnd
        for hwnd in enumerate_windows(include_untitled=True):
            class_name = get_class_name(hwnd)
            title = get_window_text(hwnd)
            if class_name == "Chrome_WidgetWin_1" and "Google Chrome" in title and ("x.com" in title or " / X" in title):
                return hwnd
        for hwnd in enumerate_windows(include_untitled=True):
            class_name = get_class_name(hwnd)
            title = get_window_text(hwnd)
            if class_name == "Chrome_WidgetWin_1" and title and title != "Default IME":
                return hwnd
        time.sleep(0.5)
    raise RuntimeError("X を開いた Chrome ウィンドウが見つかりませんでした。")


def focus_window(hwnd: int) -> None:
    foreground = user32.GetForegroundWindow()
    current_thread = kernel32.GetCurrentThreadId()
    foreground_thread = get_window_thread(foreground) if foreground else 0
    target_thread = get_window_thread(hwnd)

    attached_foreground = False
    attached_target = False
    if foreground_thread and foreground_thread != current_thread:
        user32.AttachThreadInput(foreground_thread, current_thread, True)
        attached_foreground = True
    if target_thread and target_thread != current_thread:
        user32.AttachThreadInput(target_thread, current_thread, True)
        attached_target = True

    try:
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.BringWindowToTop(hwnd)
        key(VK_MENU)
        key(VK_MENU, True)
        user32.SetForegroundWindow(hwnd)
        user32.SetFocus(hwnd)
        user32.SetActiveWindow(hwnd)
        time.sleep(0.4)
        if user32.GetForegroundWindow() != hwnd:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.4)
    finally:
        if attached_foreground:
            user32.AttachThreadInput(foreground_thread, current_thread, False)
        if attached_target:
            user32.AttachThreadInput(target_thread, current_thread, False)

    time.sleep(0.4)


def relaunch_clean_chrome(profile_directory: str, target_url: str = COMPOSE_URL) -> int:
    subprocess.run(
        ["taskkill", "/IM", "chrome.exe", "/F"],
        check=False,
        capture_output=True,
        text=True,
    )
    time.sleep(1.0)
    proc = subprocess.Popen(
        [
            str(resolve_chrome_path()),
            f"--user-data-dir={SYSTEM_USER_DATA_DIR}",
            f"--profile-directory={profile_directory}",
            "--disable-extensions",
            "--hide-crash-restore-bubble",
            "--disable-session-crashed-bubble",
            target_url,
        ]
    )
    time.sleep(4.5)
    return proc.pid


def run_javascript(hwnd: int, code: str, wait_seconds: float = 1.2) -> None:
    focus_window(hwnd)
    chord(VK_CONTROL, VK_L)
    type_text("javascript:")
    pyperclip.copy(code)
    chord(VK_CONTROL, 0x56)
    tap(VK_RETURN)
    time.sleep(wait_seconds)


def focus_compose_box(hwnd: int) -> None:
    run_javascript(
        hwnd,
        (
            "(()=>{"
            "const box=document.querySelector('[data-testid=\"tweetTextarea_0\"] [contenteditable=\"true\"],"
            "[data-testid=\"tweetTextarea_0\"][contenteditable=\"true\"],"
            "div[role=\"textbox\"][contenteditable=\"true\"]');"
            "if(!box){return;}"
            "box.focus();"
            "box.click();"
            "const selection=window.getSelection();"
            "if(selection){"
            "const range=document.createRange();"
            "range.selectNodeContents(box);"
            "range.collapse(false);"
            "selection.removeAllRanges();"
            "selection.addRange(range);"
            "}"
            "})()"
        ),
        wait_seconds=1.0,
    )


def get_compose_text_length(hwnd: int) -> int:
    marker = "__codexlen__"
    run_javascript(
        hwnd,
        (
            "(()=>{"
            "const box=document.querySelector('[data-testid=\"tweetTextarea_0\"] [contenteditable=\"true\"],"
            "[data-testid=\"tweetTextarea_0\"][contenteditable=\"true\"],"
            "div[role=\"textbox\"][contenteditable=\"true\"]');"
            "const text=box ? ((box.innerText||box.textContent||'').trim()) : '';"
            f"document.title='{marker}'+text.length;"
            "})()"
        ),
        wait_seconds=0.8,
    )
    title = get_window_text(hwnd)
    if marker not in title:
        return -1
    try:
        return int(title.split(marker, 1)[1].split(" - Google Chrome", 1)[0])
    except Exception:
        return -1


def set_compose_text(hwnd: int, text: str) -> None:
    if not text:
        return
    focus_compose_box(hwnd)
    chord(VK_CONTROL, 0x41)
    tap(VK_BACK)
    pyperclip.copy(text)
    chord(VK_CONTROL, 0x56)
    time.sleep(1.0)
    length = get_compose_text_length(hwnd)
    if length < len(text):
        focus_compose_box(hwnd)
        pyperclip.copy(text)
        chord(VK_CONTROL, 0x56)
        time.sleep(1.0)
        length = get_compose_text_length(hwnd)
    if length < len(text):
        raise RuntimeError("投稿文を入力できませんでした。")


def open_media_dialog(hwnd: int) -> int:
    run_javascript(
        hwnd,
        (
            "(()=>{"
            "const input=document.querySelector('input[data-testid=\"fileInput\"],input[type=\"file\"]');"
            "if(input){input.click();}"
            "})()"
        ),
        wait_seconds=1.2,
    )
    for dialog_hwnd in enumerate_windows():
        if get_class_name(dialog_hwnd) == "#32770":
            return dialog_hwnd
    raise RuntimeError("メディア選択ダイアログが開きませんでした。")


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
        title_lower = title.lower()
        if class_name == "Edit" and not edit_hwnd:
            edit_hwnd = hwnd
        if class_name == "Button" and not button_hwnd:
            if "開く" in title or "open" in title_lower or "(&o)" in title_lower:
                button_hwnd = hwnd

    if not edit_hwnd or not button_hwnd:
        raise RuntimeError("ファイル選択ダイアログの入力欄を見つけられませんでした。")
    return edit_hwnd, button_hwnd


def attach_media(hwnd: int, media_paths: list[Path], wait_seconds: int) -> None:
    for index, media_path in enumerate(media_paths):
        dialog_hwnd = open_media_dialog(hwnd)
        edit_hwnd, button_hwnd = find_dialog_controls(dialog_hwnd)
        pyperclip.copy(str(media_path.resolve()))
        user32.SendMessageW(edit_hwnd, EM_SETSEL, 0, -1)
        user32.SendMessageW(edit_hwnd, WM_PASTE, 0, 0)
        time.sleep(0.4)
        user32.SendMessageW(button_hwnd, BM_CLICK, 0, 0)
        time.sleep(wait_seconds if index == 0 else max(2, wait_seconds // 2))


def submit_post(hwnd: int, text: str) -> None:
    focus_compose_box(hwnd)
    chord(VK_CONTROL, VK_RETURN)
    time.sleep(4.0)
    if text:
        length = get_compose_text_length(hwnd)
        if length >= len(text):
            run_javascript(
                hwnd,
                (
                    "(()=>{"
                    "const btn=document.querySelector('[data-testid=\"tweetButton\"], [data-testid=\"tweetButtonInline\"]');"
                    "if(btn){btn.click();}"
                    "})()"
                ),
                wait_seconds=5.0,
            )


def verify_target(hwnd: int, profile_handle: str, has_media: bool) -> None:
    if not profile_handle:
        return
    suffix = "/media" if has_media else ""
    focus_window(hwnd)
    chord(VK_CONTROL, VK_L)
    pyperclip.copy(f"https://x.com/{profile_handle}{suffix}")
    chord(VK_CONTROL, 0x56)
    tap(VK_RETURN)
    time.sleep(8.0)


def print_result(success: bool, message: str) -> None:
    print(json.dumps({"success": success, "message": message}, ensure_ascii=False))


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
        print_result(False, "投稿文かメディアを指定してください。")
        return 1

    try:
        chrome_pid = relaunch_clean_chrome(args.profile_directory, COMPOSE_URL)
        x_hwnd = find_x_window(chrome_pid)
        if args.open_only:
            screenshot("existing_profile_open_only.png")
            print_result(True, "既存Chromeプロフィールで投稿画面を開きました。")
            return 0

        set_compose_text(x_hwnd, text)
        if media_paths:
            attach_media(x_hwnd, media_paths, args.wait_seconds)
            screenshot("existing_profile_media_ready.png")
        else:
            screenshot("existing_profile_compose_ready.png")

        if args.draft_only:
            print_result(True, "投稿画面への入力まで完了しました。")
            return 0

        submit_post(x_hwnd, text)
        screenshot("existing_profile_after_post.png")
        verify_target(x_hwnd, profile_handle, bool(media_paths))
        if media_paths:
            screenshot("existing_profile_media_tab.png")
        elif profile_handle:
            screenshot("existing_profile_profile_tab.png")
        print_result(True, "既存Chromeプロフィールで投稿しました。")
        return 0
    except Exception as exc:
        screenshot("existing_profile_media_error.png")
        print_result(False, str(exc).replace('"', "'"))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
