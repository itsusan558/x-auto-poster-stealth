"""Playwright-based X posting helper."""

from __future__ import annotations

import asyncio
import os
import tempfile

from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

try:
    from playwright_stealth import stealth_async
except Exception:
    async def stealth_async(_page):
        return None


TMP_DIR = tempfile.gettempdir()


async def _find_first_visible(page, selectors: list[str], timeout_ms: int = 8000):
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            await locator.wait_for(state="visible", timeout=timeout_ms)
            print(f"Found visible element: {selector}")
            return locator
        except PlaywrightTimeout:
            continue
    return None


async def _click_first_visible(page, selectors: list[str], timeout_ms: int = 5000) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            await locator.wait_for(state="visible", timeout=timeout_ms)
            await locator.click()
            print(f"Clicked: {selector}")
            return True
        except PlaywrightTimeout:
            continue
    return False


async def _body_text(page) -> str:
    try:
        return await page.locator("body").inner_text()
    except Exception:
        return ""


async def _is_graduated_access_blocked(page) -> bool:
    body_text = await _body_text(page)
    return (
        "graduated-access" in page.url
        or "Unlock more on X" in body_text
        or "To make X great for everyone" in body_text
    )


async def _open_login_page(page, screenshot) -> bool:
    for attempt in range(3):
        print(f"Opening login page attempt {attempt + 1}")
        await page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(4000)
        body_text = await _body_text(page)
        if "問題が発生しました。再読み込みしてください。" in body_text or "Something went wrong" in body_text:
            await screenshot(f"login_error_{attempt + 1}")
            if attempt < 2:
                await page.reload(wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(3000)
                continue
            return False
        return True
    return False


async def _do_post(
    username: str,
    password: str,
    content: str,
    cookies: list,
    media_path: str | None = None,
) -> tuple[bool, str, list]:
    """Post to X and return (success, message, cookies)."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=os.environ.get("HEADLESS", "true").lower() != "false",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="ja-JP",
        )

        if cookies:
            try:
                await context.add_cookies(cookies)
            except Exception as exc:
                print(f"Cookie load warning: {exc}")

        page = await context.new_page()
        await stealth_async(page)
        new_cookies = cookies

        async def screenshot(name: str) -> None:
            try:
                await page.screenshot(path=os.path.join(TMP_DIR, f"{name}.png"), full_page=True)
            except Exception:
                pass

        try:
            print("Opening https://x.com/home")
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)

            logged_in = await page.locator('[data-testid="tweetTextarea_0"]').count() > 0
            print(f"Logged in: {logged_in}")

            if not logged_in:
                print("Starting login flow")
                opened = await _open_login_page(page, screenshot)
                await screenshot("login_page")
                if not opened:
                    return False, "X のログイン画面で一時エラーが発生しました。少し時間を空けて再試行してください。", []

                username_input = await _find_first_visible(
                    page,
                    [
                        'input[autocomplete="username"]',
                        'input[name="text"]',
                        'input[inputmode="text"]',
                        'input[type="text"]',
                        'input[dir="auto"]',
                        "input",
                    ],
                    timeout_ms=12000,
                )
                if username_input is None:
                    body_text = await _body_text(page)
                    print(f"Login page body: {body_text[:1200]}")
                    await screenshot("no_username_input")
                    return False, "ユーザー名入力欄が見つかりませんでした。", []

                await username_input.click()
                try:
                    await username_input.fill(username)
                except Exception:
                    await page.keyboard.type(username, delay=50)
                await page.wait_for_timeout(500)

                clicked_next = await _click_first_visible(
                    page,
                    [
                        'button:has-text("次へ")',
                        'button:has-text("Next")',
                        '[role="button"]:has-text("次へ")',
                        '[role="button"]:has-text("Next")',
                    ],
                )
                if not clicked_next:
                    await page.keyboard.press("Enter")

                await page.wait_for_timeout(2500)
                await screenshot("after_username")

                page_text = await _body_text(page)
                if "Could not log you in now" in page_text or "現在ログインできません" in page_text:
                    return False, "X 側で一時的にログイン制限がかかっています。時間を空けて再試行してください。", []

                verify_input = await _find_first_visible(
                    page,
                    [
                        'input[data-testid="ocfEnterTextTextInput"]',
                        'input[name="text"]',
                    ],
                    timeout_ms=3000,
                )
                if verify_input is not None:
                    print("Additional verification step detected")
                    await verify_input.fill(username)
                    await page.wait_for_timeout(500)
                    clicked_verify_next = await _click_first_visible(
                        page,
                        [
                            '[data-testid="ocfEnterTextNextButton"]',
                            'button:has-text("次へ")',
                            'button:has-text("Next")',
                        ],
                        timeout_ms=4000,
                    )
                    if not clicked_verify_next:
                        await page.keyboard.press("Enter")
                    await page.wait_for_timeout(2500)

                password_input = await _find_first_visible(
                    page,
                    [
                        'input[name="password"]',
                        'input[type="password"]',
                        '[data-testid="LoginForm_Password_Field"] input',
                        '[data-testid="LoginForm_Password_Field"]',
                    ],
                    timeout_ms=12000,
                )
                if password_input is None:
                    body_text = await _body_text(page)
                    print(f"Password step body: {body_text[:1200]}")
                    await screenshot("no_password_input")
                    if "電話番号" in body_text or "メールアドレス" in body_text or "phone number" in body_text or "email address" in body_text:
                        return False, "X が追加確認を求めています。電話番号またはメールアドレスの確認画面に対応が必要です。", []
                    if "次へ" in body_text and ("ユーザー" in body_text or "電話番号" in body_text or "メールアドレス" in body_text):
                        return False, "ログイン画面から次へ進めませんでした。入力内容の確認か、X 側の一時制限の可能性があります。", []
                    return False, "パスワード入力欄が見つかりませんでした。", []

                await password_input.click()
                await password_input.fill(password)
                await page.wait_for_timeout(500)

                clicked_login = await _click_first_visible(
                    page,
                    [
                        '[data-testid="LoginForm_Login_Button"]',
                        'button:has-text("ログイン")',
                        'button:has-text("Log in")',
                        '[role="button"]:has-text("ログイン")',
                        '[role="button"]:has-text("Log in")',
                    ],
                )
                if not clicked_login:
                    await page.keyboard.press("Enter")

                await page.wait_for_timeout(5000)
                await screenshot("after_login")

                await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(4000)
                if await page.locator('[data-testid="tweetTextarea_0"]').count() == 0:
                    await screenshot("login_failed")
                    return False, f"ログインに失敗しました。URL: {page.url}", []

            compose = page.locator('[data-testid="tweetTextarea_0"]').first
            if await _is_graduated_access_blocked(page):
                await screenshot("graduated_access_blocked")
                return False, "X 側で段階的アクセス制限がかかっています。しばらく通常利用して解除されるまで、このアカウントでは投稿できません。", new_cookies
            await compose.wait_for(state="visible", timeout=15000)
            await compose.click()
            await page.wait_for_timeout(800)
            await page.keyboard.insert_text(content)
            await page.wait_for_timeout(1200)

            if media_path and os.path.exists(media_path):
                print(f"Uploading media: {media_path}")
                try:
                    file_input = page.locator('input[type="file"]').first
                    await file_input.wait_for(state="attached", timeout=8000)
                    await file_input.set_input_files(media_path)
                    await page.wait_for_timeout(5000)
                    await screenshot("after_media")
                except Exception as exc:
                    print(f"Media upload warning: {exc}")

            post_button = await _find_first_visible(
                page,
                [
                    '[data-testid="tweetButtonInline"]',
                    '[data-testid="tweetButton"]',
                    'button:has-text("投稿する")',
                    'button:has-text("Post")',
                ],
                timeout_ms=12000,
            )
            if post_button is None:
                await screenshot("no_post_button")
                return False, "投稿ボタンが見つかりませんでした。", new_cookies

            await post_button.click()
            await page.wait_for_timeout(4000)

            new_cookies = await context.cookies()
            return True, "投稿に成功しました。", new_cookies
        except Exception as exc:
            await screenshot("error")
            return False, f"エラー: {exc}", new_cookies
        finally:
            await browser.close()


def post_tweet(
    username: str,
    password: str,
    content: str,
    cookies: list | None = None,
    media_path: str | None = None,
) -> tuple[bool, str, list]:
    return asyncio.run(_do_post(username, password, content, cookies or [], media_path))
