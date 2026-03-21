"""Post a single message to X using the local project config.

Usage:
  py -3 post_now.py --text-file post.txt
  py -3 post_now.py --text "hello"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import poster


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = DATA_DIR / "config.json"
COOKIES_PATH = DATA_DIR / "cookies_0.json"


def load_account() -> dict:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8", errors="ignore"))
    return config["accounts"][0]


def load_cookies() -> list:
    if not COOKIES_PATH.exists():
        return []
    return json.loads(COOKIES_PATH.read_text(encoding="utf-8"))


def save_cookies(cookies: list) -> None:
    COOKIES_PATH.write_text(
        json.dumps(cookies, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", help="Post text")
    parser.add_argument("--text-file", help="UTF-8 text file containing the post body")
    return parser.parse_args()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    args = parse_args()
    if args.text_file:
        content = Path(args.text_file).read_text(encoding="utf-8").lstrip("\ufeff").strip()
    else:
        content = (args.text or "").strip()

    if not content:
        print(json.dumps({"success": False, "message": "投稿本文が空です。"}, ensure_ascii=False))
        return 1

    account = load_account()
    username = account.get("username", "")
    password = account.get("password", "")
    cookies = load_cookies()

    print(json.dumps({"starting": True, "length": len(content)}, ensure_ascii=False))
    success, message, new_cookies = poster.post_tweet(username, password, content, cookies, None)
    if new_cookies:
        save_cookies(new_cookies)
    print(json.dumps({"success": success, "message": message, "cookies_count": len(new_cookies)}, ensure_ascii=False))
    return 0 if success else 2


if __name__ == "__main__":
    raise SystemExit(main())
