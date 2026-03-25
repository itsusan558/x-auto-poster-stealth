"""起動スクリプト: アプリ + Cloudflare Tunnel x2 を同時起動し、URLをメールで送信する。"""
import subprocess
import sys
import time
import smtplib
import json
import re
import threading
from email.mime.text import MIMEText
from pathlib import Path

NOTIFY_EMAIL = "sixdomonly@gmmail.com"
APP_PORT = 8080
VIDEO_PORT = 7860
DATA_DIR = Path(__file__).parent / "data"
GMAIL_CONFIG = DATA_DIR / "gmail_notify.json"
# 動画編集URLをapp.pyに渡すための一時ファイル
VIDEO_URL_FILE = DATA_DIR / "video_compiler_external_url.txt"


def get_cloudflared_url(proc, timeout=30):
    """cloudflaredのstderrからURLを拾う"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stderr.readline()
        if not line:
            time.sleep(0.2)
            continue
        line = line.decode("utf-8", errors="ignore")
        match = re.search(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com", line)
        if match:
            return match.group(0)
    return None


def start_tunnel(port):
    return subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def send_email(app_url, video_url=None):
    if not GMAIL_CONFIG.exists():
        print(f"[notify] gmail_notify.json が見つかりません。メール送信をスキップします。")
        print(f"[notify] URL: {app_url}")
        return
    cfg = json.loads(GMAIL_CONFIG.read_text(encoding="utf-8"))
    sender = cfg["gmail_address"]
    password = cfg["app_password"]

    body = f"X Post Studio が起動しました。\n\nアクセスURL:\n{app_url}\n"
    if video_url:
        body += f"\n動画編集URL:\n{video_url}\n"
    body += "\nこのURLはトンネルセッション中のみ有効です。"

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"[X Post Studio] 起動しました - {app_url}"
    msg["From"] = sender
    msg["To"] = NOTIFY_EMAIL

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(sender, password)
        s.send_message(msg)
    print(f"[notify] メール送信完了 → {NOTIFY_EMAIL}")


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ── アプリ起動 ──
    print("[start] アプリを起動しています...")
    app_proc = subprocess.Popen(
        [sys.executable, "app.py"],
        cwd=str(Path(__file__).parent),
    )
    time.sleep(2)

    # ── トンネル x2 起動 ──
    print("[start] Cloudflare Tunnel (アプリ) を起動しています...")
    app_tunnel = start_tunnel(APP_PORT)

    print("[start] Cloudflare Tunnel (動画編集) を起動しています...")
    video_tunnel = start_tunnel(VIDEO_PORT)

    # ── URL取得 (並列) ──
    app_url = None
    video_url = None

    def fetch_app_url():
        nonlocal app_url
        app_url = get_cloudflared_url(app_tunnel)

    def fetch_video_url():
        nonlocal video_url
        video_url = get_cloudflared_url(video_tunnel)

    t1 = threading.Thread(target=fetch_app_url)
    t2 = threading.Thread(target=fetch_video_url)
    t1.start(); t2.start()
    t1.join(); t2.join()

    # 動画編集の外部URLをファイルに書き出す (app.pyが参照)
    if video_url:
        VIDEO_URL_FILE.write_text(video_url, encoding="utf-8")
        print(f"[start] 動画編集URL: {video_url}")

    if app_url:
        print(f"[start] アプリURL: {app_url}")
        try:
            send_email(app_url, video_url)
        except Exception as e:
            print(f"[notify] メール送信失敗: {e}")
            print(f"[notify] スマホでこのURLを開いてください: {app_url}")
    else:
        print("[start] URLの取得に失敗しました。cloudflared の動作を確認してください。")

    print("[start] 起動完了。Ctrl+C で終了します。")
    try:
        app_proc.wait()
    except KeyboardInterrupt:
        app_proc.terminate()
        app_tunnel.terminate()
        video_tunnel.terminate()
        if VIDEO_URL_FILE.exists():
            VIDEO_URL_FILE.unlink()


if __name__ == "__main__":
    main()
