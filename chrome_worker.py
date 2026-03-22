"""Background Chrome worker manager (STEALTH_PLAN Phase 2).

Keeps one automation Chrome process alive between posts.
Provides CLI for managing the worker lifecycle.

Usage:
  py chrome_worker.py status    -- show current worker state
  py chrome_worker.py start     -- start if not running
  py chrome_worker.py stop      -- kill the automation Chrome
  py chrome_worker.py restart   -- kill and restart
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
AUTOMATION_USER_DATA_DIR = DATA_DIR / "chrome-automation-profile"
CHROME_STATE_FILE = DATA_DIR / "chrome-worker-state.json"
REMOTE_DEBUGGING_HOST = "127.0.0.1"
DEFAULT_PROFILE = "Default"
COMPOSE_URL = "https://x.com/home"

CHROME_CANDIDATES = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
]


def resolve_chrome_path() -> Path:
    for path in CHROME_CANDIDATES:
        if path.exists():
            return path
    raise RuntimeError("Google Chrome が見つかりませんでした。")


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((REMOTE_DEBUGGING_HOST, 0))
        return int(sock.getsockname()[1])


def is_port_alive(port: int) -> bool:
    try:
        with socket.create_connection((REMOTE_DEBUGGING_HOST, port), timeout=1.0):
            return True
    except OSError:
        return False


def is_pid_alive(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, check=False,
        )
        return str(pid) in result.stdout
    except Exception:
        return False


def load_state() -> dict:
    if CHROME_STATE_FILE.exists():
        try:
            return json.loads(CHROME_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CHROME_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def worker_status() -> dict:
    state = load_state()
    pid = state.get("pid")
    port = state.get("port")
    if not pid or not port:
        return {"status": "stopped"}
    pid_ok = is_pid_alive(pid)
    port_ok = is_port_alive(port)
    if pid_ok and port_ok:
        return {"status": "running", "pid": pid, "port": port}
    return {"status": "stale", "pid": pid, "port": port, "pid_alive": pid_ok, "port_alive": port_ok}


def start_worker(profile_directory: str = DEFAULT_PROFILE) -> dict:
    s = worker_status()
    if s["status"] == "running":
        return s

    automation_dir = AUTOMATION_USER_DATA_DIR
    if not automation_dir.exists():
        return {"status": "error", "message": "専用プロフィールがまだ作成されていません。existing_profile_media_post.py を一度実行してください。"}

    port = find_free_port()
    proc = subprocess.Popen(
        [
            str(resolve_chrome_path()),
            f"--user-data-dir={automation_dir}",
            f"--profile-directory={profile_directory}",
            f"--remote-debugging-address={REMOTE_DEBUGGING_HOST}",
            f"--remote-debugging-port={port}",
            "--window-position=-32000,-32000",
            "--window-size=1280,800",
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-default-apps",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
            "--lang=ja-JP",
            "--accept-lang=ja-JP,ja,en-US,en",
            COMPOSE_URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for debug port to open
    deadline = time.time() + 30.0
    while time.time() < deadline:
        if is_port_alive(port):
            break
        time.sleep(0.3)
    else:
        return {"status": "error", "message": "Chrome のデバッグポートが起動しませんでした。"}

    time.sleep(1.5)
    state = {"pid": proc.pid, "port": port}
    save_state(state)
    return {"status": "running", "pid": proc.pid, "port": port}


def stop_worker() -> dict:
    state = load_state()
    pid = state.get("pid")
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=False, capture_output=True)
        time.sleep(0.8)
    save_state({})
    return {"status": "stopped"}


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        s = worker_status()
        print(json.dumps(s, indent=2, ensure_ascii=False))
    elif cmd == "start":
        s = start_worker()
        print(json.dumps(s, indent=2, ensure_ascii=False))
    elif cmd == "stop":
        s = stop_worker()
        print(json.dumps(s, indent=2, ensure_ascii=False))
    elif cmd == "restart":
        stop_worker()
        s = start_worker()
        print(json.dumps(s, indent=2, ensure_ascii=False))
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print("Usage: py chrome_worker.py [status|start|stop|restart]", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
