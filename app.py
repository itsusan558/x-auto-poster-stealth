"""Simple local X posting app with scheduling support."""

from __future__ import annotations

import json
import mimetypes
import os
import random
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template_string, request

try:
    from google.cloud import storage
except ImportError:
    storage = None


app = Flask(__name__)

JST = timezone(timedelta(hours=9))
VERSION = "1.10.3"
STATE_FILE = "state.json"
LOG_FILE = "post_log.json"
SCHEDULE_FILE = "schedules.json"
MAX_LOGS = 30
SCHEDULE_POLL_SECONDS = 15
FOLLOW_REVIEW_FILE = "follow_review.json"
MAX_FOLLOW_ACTIONS = 200
FOLLOW_TARGET_MAX_FOLLOWERS = 100
FOLLOW_SAFE_DAILY_LIMIT = 20
FOLLOW_SAFE_WINDOW_LIMIT = 3
FOLLOW_WINDOW_MINUTES = 15
FOLLOW_COOLDOWN_SECONDS = 240

PROJECT_ID = os.environ.get("GCP_PROJECT", "local-dev")
GCS_BUCKET = os.environ.get("GCS_BUCKET", "").strip()
DATA_DIR = Path(os.environ.get("APP_DATA_DIR", Path(__file__).with_name("data")))
LOCAL_MODE = os.environ.get("LOCAL_MODE", "").lower() in {"1", "true", "yes"} or not GCS_BUCKET
EXISTING_PROFILE_SCRIPT = Path(__file__).with_name("existing_profile_media_post.py")
VIDEO_COMPILER_DIR = Path(os.environ.get("HF_VIDEO_COMPILER_DIR", str(Path(__file__).resolve().parent.with_name("hf-video-compiler"))))
VIDEO_COMPILER_ENTRY = VIDEO_COMPILER_DIR / "web_compiler.py"
VIDEO_COMPILER_PORT = int(os.environ.get("HF_VIDEO_COMPILER_PORT", "7860"))
VIDEO_COMPILER_URL = os.environ.get("HF_VIDEO_COMPILER_URL", f"http://127.0.0.1:{VIDEO_COMPILER_PORT}").strip().rstrip("/")
DEFAULT_CHROME_PROFILE = os.environ.get("CHROME_PROFILE_DIRECTORY", "Default").strip() or "Default"
DEFAULT_PROFILE_HANDLE = os.environ.get("X_PROFILE_HANDLE", "").strip().lstrip("@")
VIDEO_EXTENSIONS = {"mp4", "mov", "avi", "webm", "mkv"}

DEFAULT_STATE = {
    "content": "",
    "media_path": "",
    "media_filename": "",
    "media_items": [],
    "last_post_at": "",
    "profile_handle": DEFAULT_PROFILE_HANDLE,
}
DEFAULT_FOLLOW_REVIEW = {
    "current_candidate_id": "",
    "candidates": [],
    "action_log": [],
}

_gcs_client = None
_posting_lock = threading.Lock()
_schedule_lock = threading.Lock()
_video_compiler_lock = threading.Lock()
_scheduler_started = False


def ensure_local_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "media").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "scheduled_media").mkdir(parents=True, exist_ok=True)


def now_jst() -> datetime:
    return datetime.now(JST)


def now_iso() -> str:
    return now_jst().isoformat()


def gcs():
    global _gcs_client
    if storage is None:
        raise RuntimeError("google-cloud-storage is not installed.")
    if _gcs_client is None:
        _gcs_client = storage.Client()
    return _gcs_client


def storage_mode_label() -> str:
    return "LOCAL" if LOCAL_MODE else "GCS"


def local_json_path(name: str) -> Path:
    ensure_local_dirs()
    return DATA_DIR / name


def data_read(path: str, default: Any = None):
    if LOCAL_MODE:
        file_path = local_json_path(path)
        if not file_path.exists():
            return default
        try:
            return json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            return default
    try:
        blob = gcs().bucket(GCS_BUCKET).blob(path)
        return json.loads(blob.download_as_text())
    except Exception:
        return default


def data_write(path: str, data: Any) -> None:
    if LOCAL_MODE:
        local_json_path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    blob = gcs().bucket(GCS_BUCKET).blob(path)
    blob.upload_from_string(json.dumps(data, ensure_ascii=False, indent=2), content_type="application/json")


def load_media_bytes(stored_path: str):
    if not stored_path:
        return None, None
    if LOCAL_MODE:
        file_path = Path(stored_path)
        if not file_path.exists():
            return None, None
        content_type, _ = mimetypes.guess_type(file_path.name)
        return file_path.read_bytes(), content_type or "application/octet-stream"
    try:
        blob = gcs().bucket(GCS_BUCKET).blob(stored_path)
        return blob.download_as_bytes(), blob.content_type or "application/octet-stream"
    except Exception:
        return None, None


def save_binary(path: str, data: bytes, content_type: str | None = None) -> None:
    if LOCAL_MODE:
        file_path = Path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(data)
        return
    blob = gcs().bucket(GCS_BUCKET).blob(path)
    blob.upload_from_string(data, content_type=content_type or "application/octet-stream")


def delete_media_file(stored_path: str) -> None:
    if not stored_path:
        return
    if LOCAL_MODE:
        file_path = Path(stored_path)
        if file_path.exists():
            try:
                file_path.unlink()
            except OSError:
                pass
        return
    try:
        gcs().bucket(GCS_BUCKET).blob(stored_path).delete()
    except Exception:
        pass


def is_video_file(filename: str) -> bool:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in VIDEO_EXTENSIONS


def media_local_path(filename: str) -> Path:
    ensure_local_dirs()
    ext = Path(filename).suffix.lower() or ".bin"
    return DATA_DIR / "media" / f"current_media{ext}"


def scheduled_media_path(schedule_id: str, filename: str) -> str:
    ext = Path(filename).suffix.lower() or ".bin"
    if LOCAL_MODE:
        return str(DATA_DIR / "scheduled_media" / f"{schedule_id}{ext}")
    return f"scheduled_media/{schedule_id}{ext}"


def save_uploaded_media(upload) -> tuple[str, str]:
    filename = upload.filename or "media.bin"
    if LOCAL_MODE:
        destination = media_local_path(filename)
        upload.save(destination)
        return str(destination), filename
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    gcs_path = f"media/current_media.{ext}"
    blob = gcs().bucket(GCS_BUCKET).blob(gcs_path)
    blob.upload_from_file(upload.stream, content_type=upload.content_type or "application/octet-stream")
    return gcs_path, filename


def current_media_storage_path(filename: str) -> str:
    if LOCAL_MODE:
        return str(media_local_path(filename))
    ext = Path(filename).suffix.lower().lstrip(".") or "bin"
    return f"media/current_media.{ext}"


def clear_state_media(state: dict[str, Any]) -> None:
    if state.get("media_path"):
        delete_media_file(state["media_path"])
    state["media_path"] = ""
    state["media_filename"] = ""


def save_media_bytes_to_state(state: dict[str, Any], data: bytes, filename: str, content_type: str | None = None) -> tuple[str, str]:
    safe_name = Path(filename or "media.bin").name or "media.bin"
    destination = current_media_storage_path(safe_name)
    clear_state_media(state)
    save_binary(destination, data, content_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream")
    state["media_path"] = destination
    state["media_filename"] = safe_name
    return destination, safe_name


def import_media_to_state(state: dict[str, Any], media_path: str = "", media_url: str = "", media_filename: str = "") -> tuple[str, str]:
    source_path = (media_path or "").strip()
    source_url = (media_url or "").strip()
    preferred_name = (media_filename or "").strip()

    if source_path:
        file_path = Path(source_path)
        if not file_path.exists() or not file_path.is_file():
            raise ValueError("指定されたメディアファイルが見つかりません。")
        return save_media_bytes_to_state(
            state,
            file_path.read_bytes(),
            preferred_name or file_path.name,
            mimetypes.guess_type(file_path.name)[0],
        )

    if source_url:
        request_obj = urllib.request.Request(source_url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(request_obj, timeout=30) as response:
                data = response.read()
                content_type = response.headers.get_content_type()
                final_url = response.geturl()
        except urllib.error.URLError as exc:
            raise ValueError("メディアURLの取得に失敗しました。") from exc
        parsed_name = Path(urllib.parse.urlparse(final_url).path).name
        return save_media_bytes_to_state(state, data, preferred_name or parsed_name or "media.bin", content_type)

    return "", ""


def copy_media_to_schedule(schedule_id: str, source_path: str, filename: str) -> tuple[str, str]:
    if not source_path or not filename:
        return "", ""
    data, content_type = load_media_bytes(source_path)
    if data is None:
        return "", ""
    destination = scheduled_media_path(schedule_id, filename)
    save_binary(destination, data, content_type)
    return destination, filename


def scheduled_media_path(schedule_id: str, index: int, filename: str) -> str:  # type: ignore[override]
    safe_name = Path(filename or "media.bin").name or "media.bin"
    if LOCAL_MODE:
        return str(DATA_DIR / "scheduled_media" / f"{schedule_id}-{index + 1}-{safe_name}")
    return f"scheduled_media/{schedule_id}-{index + 1}-{safe_name}"


def current_media_storage_path(filename: str) -> str:  # type: ignore[override]
    safe_name = Path(filename or "media.bin").name or "media.bin"
    unique_name = f"{uuid.uuid4().hex[:12]}-{safe_name}"
    if LOCAL_MODE:
        return str(DATA_DIR / "media" / unique_name)
    return f"media/{unique_name}"


def normalize_media_item(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    path = str(item.get("path") or item.get("media_path") or "").strip()
    filename = Path(str(item.get("filename") or item.get("media_filename") or path or "")).name
    if not path or not filename:
        return None
    return {"path": path, "filename": filename, "is_video": is_video_file(filename)}


def normalize_media_items(raw_items: Any, legacy_path: str = "", legacy_filename: str = "") -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(raw_items, list):
        for raw in raw_items:
            normalized = normalize_media_item(raw)
            if normalized:
                items.append(normalized)
    if not items and (legacy_path or legacy_filename):
        normalized = normalize_media_item({"path": legacy_path, "filename": legacy_filename})
        if normalized:
            items.append(normalized)
    return items


def set_state_media_items(state: dict[str, Any], items: list[dict[str, Any]]) -> None:
    normalized = normalize_media_items(items)
    state["media_items"] = normalized
    if normalized:
        state["media_path"] = normalized[0]["path"]
        state["media_filename"] = normalized[0]["filename"]
    else:
        state["media_path"] = ""
        state["media_filename"] = ""


def media_summary(items: list[dict[str, Any]]) -> str:
    normalized = normalize_media_items(items)
    if not normalized:
        return ""
    names = [item["filename"] for item in normalized]
    if len(names) == 1:
        return names[0]
    preview = " / ".join(names[:3])
    if len(names) > 3:
        preview += f" ほか{len(names) - 3}件"
    return preview


def validate_x_media_items(items: list[dict[str, Any]]) -> str | None:
    normalized = normalize_media_items(items)
    if not normalized:
        return None
    video_count = sum(1 for item in normalized if item.get("is_video"))
    total_count = len(normalized)
    if video_count > 1:
        return "X では動画を同時に2本以上投稿できません。動画は1本だけにしてください。"
    if total_count > 4:
        return "X で同時投稿できるメディアは合計4件までです。"
    return None


def save_uploaded_media_item(upload) -> dict[str, Any]:
    filename = Path(upload.filename or "media.bin").name or "media.bin"
    destination = current_media_storage_path(filename)
    if LOCAL_MODE:
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        upload.save(destination)
    else:
        blob = gcs().bucket(GCS_BUCKET).blob(destination)
        blob.upload_from_file(upload.stream, content_type=upload.content_type or "application/octet-stream")
    return {"path": destination, "filename": filename, "is_video": is_video_file(filename)}


def clear_state_media(state: dict[str, Any]) -> None:  # type: ignore[override]
    seen: set[str] = set()
    for item in normalize_media_items(state.get("media_items"), state.get("media_path", ""), state.get("media_filename", "")):
        path = item.get("path", "")
        if path and path not in seen:
            delete_media_file(path)
            seen.add(path)
    set_state_media_items(state, [])


def save_media_bytes_item(data: bytes, filename: str, content_type: str | None = None) -> dict[str, Any]:
    safe_name = Path(filename or "media.bin").name or "media.bin"
    destination = current_media_storage_path(safe_name)
    save_binary(destination, data, content_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream")
    return {"path": destination, "filename": safe_name, "is_video": is_video_file(safe_name)}


def import_media_item(media_path: str = "", media_url: str = "", media_filename: str = "") -> dict[str, Any] | None:
    source_path = (media_path or "").strip()
    source_url = (media_url or "").strip()
    preferred_name = (media_filename or "").strip()

    if source_path:
        file_path = Path(source_path)
        if not file_path.exists() or not file_path.is_file():
            raise ValueError("指定されたメディアファイルが見つかりません。")
        return save_media_bytes_item(
            file_path.read_bytes(),
            preferred_name or file_path.name,
            mimetypes.guess_type(file_path.name)[0],
        )

    if source_url:
        request_obj = urllib.request.Request(source_url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(request_obj, timeout=30) as response:
                data = response.read()
                content_type = response.headers.get_content_type()
                final_url = response.geturl()
        except urllib.error.URLError as exc:
            raise ValueError("メディアURLの取得に失敗しました。") from exc
        parsed_name = Path(urllib.parse.urlparse(final_url).path).name
        return save_media_bytes_item(data, preferred_name or parsed_name or "media.bin", content_type)

    return None


def import_media_items_to_state(state: dict[str, Any], media_specs: list[dict[str, Any]], replace: bool = True) -> list[dict[str, Any]]:
    imported: list[dict[str, Any]] = []
    if replace:
        clear_state_media(state)
    else:
        imported.extend(normalize_media_items(state.get("media_items"), state.get("media_path", ""), state.get("media_filename", "")))
    for spec in media_specs:
        item = import_media_item(
            media_path=str(spec.get("media_path") or spec.get("path") or ""),
            media_url=str(spec.get("media_url") or spec.get("url") or ""),
            media_filename=str(spec.get("media_filename") or spec.get("filename") or ""),
        )
        if item:
            imported.append(item)
    set_state_media_items(state, imported)
    return imported


def copy_media_items_to_schedule(schedule_id: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for index, item in enumerate(normalize_media_items(items)):
        data, content_type = load_media_bytes(item.get("path", ""))
        if data is None:
            continue
        destination = scheduled_media_path(schedule_id, index, item.get("filename", "media.bin"))
        save_binary(destination, data, content_type)
        snapshots.append(
            {
                "path": destination,
                "filename": item.get("filename", ""),
                "is_video": is_video_file(item.get("filename", "")),
            }
        )
    return snapshots


def copy_media_to_schedule(schedule_id: str, source_path: str, filename: str) -> tuple[str, str]:  # type: ignore[override]
    snapshots = copy_media_items_to_schedule(
        schedule_id,
        normalize_media_items(None, source_path, filename),
    )
    if not snapshots:
        return "", ""
    return snapshots[0]["path"], snapshots[0]["filename"]


def get_state() -> dict[str, Any]:
    saved = data_read(STATE_FILE, None) or {}
    state = {**DEFAULT_STATE, **saved}
    state["profile_handle"] = (state.get("profile_handle") or DEFAULT_PROFILE_HANDLE).lstrip("@")
    state["media_items"] = normalize_media_items(state.get("media_items"), state.get("media_path", ""), state.get("media_filename", ""))
    set_state_media_items(state, state["media_items"])
    state["has_media"] = bool(state["media_items"])
    state["is_video"] = bool(state["media_items"] and state["media_items"][0]["is_video"])
    state["media_count"] = len(state["media_items"])
    state["media_summary"] = media_summary(state["media_items"])
    return state


def save_state(state: dict[str, Any]) -> None:
    data_write(
        STATE_FILE,
        {
            "content": state.get("content", ""),
            "media_path": state.get("media_path", ""),
            "media_filename": state.get("media_filename", ""),
            "media_items": normalize_media_items(state.get("media_items"), state.get("media_path", ""), state.get("media_filename", "")),
            "last_post_at": state.get("last_post_at", ""),
            "profile_handle": (state.get("profile_handle") or DEFAULT_PROFILE_HANDLE).lstrip("@"),
        },
    )


def get_logs() -> list[dict[str, Any]]:
    return data_read(LOG_FILE, []) or []


def add_log(success: bool, message: str, content: str, media_filename: str, source: str) -> None:
    logs = get_logs()
    logs.insert(
        0,
        {
            "time": now_jst().strftime("%Y-%m-%d %H:%M:%S"),
            "success": success,
            "message": message,
            "content": content,
            "media_filename": media_filename,
            "source": source,
        },
    )
    data_write(LOG_FILE, logs[:MAX_LOGS])


def existing_profile_available() -> bool:
    return LOCAL_MODE and os.name == "nt" and EXISTING_PROFILE_SCRIPT.exists()


def video_compiler_available() -> bool:
    return LOCAL_MODE and VIDEO_COMPILER_ENTRY.exists()


def video_compiler_running() -> bool:
    if not VIDEO_COMPILER_URL:
        return False
    parsed = urllib.parse.urlparse(VIDEO_COMPILER_URL)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def ensure_video_compiler_started() -> bool:
    if not video_compiler_available():
        return False
    if video_compiler_running():
        return True

    with _video_compiler_lock:
        if video_compiler_running():
            return True
        try:
            env = os.environ.copy()
            env["PORT"] = str(VIDEO_COMPILER_PORT)
            env.setdefault("X_POSTER_URL", os.environ.get("X_POSTER_URL", "http://127.0.0.1:8093"))
            stdout_path = VIDEO_COMPILER_DIR / f"local-{VIDEO_COMPILER_PORT}.log"
            stderr_path = VIDEO_COMPILER_DIR / f"local-{VIDEO_COMPILER_PORT}.err.log"
            with open(stdout_path, "a", encoding="utf-8") as stdout_handle, open(stderr_path, "a", encoding="utf-8") as stderr_handle:
                subprocess.Popen(
                    [sys.executable, str(VIDEO_COMPILER_ENTRY)],
                    cwd=str(VIDEO_COMPILER_DIR),
                    env=env,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                )
            time.sleep(1.0)
        except Exception:
            return False
    return video_compiler_running()


def parse_json_result(text: str) -> dict[str, Any] | None:
    for line in reversed([line.strip() for line in text.splitlines() if line.strip()]):
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except Exception:
                continue
    return None


def run_existing_profile_command(extra_args: list[str]) -> tuple[bool, str]:
    args = [sys.executable, str(EXISTING_PROFILE_SCRIPT), "--profile-directory", DEFAULT_CHROME_PROFILE]
    args.extend(extra_args)
    completed = subprocess.run(
        args,
        cwd=str(Path(__file__).resolve().parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    payload = parse_json_result(completed.stdout) or parse_json_result(completed.stderr) or {}
    message = payload.get("message") or completed.stderr.strip() or completed.stdout.strip() or "既存Chrome経由の操作に失敗しました。"
    success = bool(payload.get("success")) if payload else completed.returncode == 0
    return success and completed.returncode == 0, message


def parse_schedule_datetime(value: str) -> datetime:
    if not (value or "").strip():
        raise ValueError("予約日時を選んでください。")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("予約日時の形式が正しくありません。") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    return parsed.astimezone(JST)


def to_display_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value).astimezone(JST).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return value


def get_schedules() -> list[dict[str, Any]]:
    items = sorted(data_read(SCHEDULE_FILE, []) or [], key=lambda item: item.get("scheduled_at", ""))
    normalized: list[dict[str, Any]] = []
    for item in items:
        copied = dict(item)
        copied["media_items"] = normalize_media_items(copied.get("media_items"), copied.get("media_path", ""), copied.get("media_filename", ""))
        if copied["media_items"]:
            copied["media_path"] = copied["media_items"][0]["path"]
            copied["media_filename"] = media_summary(copied["media_items"])
        normalized.append(copied)
    return normalized


def save_schedules(items: list[dict[str, Any]]) -> None:
    to_save: list[dict[str, Any]] = []
    for item in items:
        copied = dict(item)
        media_items = normalize_media_items(copied.get("media_items"), copied.get("media_path", ""), copied.get("media_filename", ""))
        copied["media_items"] = media_items
        if media_items:
            copied["media_path"] = media_items[0]["path"]
            copied["media_filename"] = media_summary(media_items)
        to_save.append(copied)
    data_write(SCHEDULE_FILE, to_save)


def schedules_for_view(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    labels = {"pending": "待機中", "running": "実行中", "completed": "完了", "failed": "失敗", "canceled": "キャンセル"}
    result = []
    for item in items:
        result.append(
            {
                **item,
                "scheduled_at_display": to_display_time(item.get("scheduled_at", "")),
                "created_at_display": to_display_time(item.get("created_at", "")),
                "status_label": labels.get(item.get("status", "pending"), item.get("status", "pending")),
                "content_preview": ((item.get("content") or "").strip()[:120] or "本文なし"),
            }
        )
    return result


def next_pending_schedule(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in items:
        if item.get("status") == "pending":
            return item
    return None


def execute_post(content: str, media_data: Any, media_filename: str, profile_handle: str, source: str) -> tuple[bool, str]:
    extra_args: list[str] = []
    if content:
        extra_args.extend(["--text", content])
    if isinstance(media_data, list):
        media_items = normalize_media_items(media_data)
    else:
        media_items = normalize_media_items(None, str(media_data or ""), media_filename)
    validation_error = validate_x_media_items(media_items)
    if validation_error:
        add_log(False, validation_error, content, media_summary(media_items), source)
        return False, validation_error
    for item in media_items:
        extra_args.extend(["--media-path", item.get("path", "")])
    if profile_handle:
        extra_args.extend(["--profile-handle", profile_handle])

    with _posting_lock:
        success, message = run_existing_profile_command(extra_args)

    add_log(success, message, content, media_summary(media_items), source)
    if success:
        state = get_state()
        state["last_post_at"] = now_jst().strftime("%Y-%m-%d %H:%M:%S")
        save_state(state)
    return success, message


def claim_due_schedule() -> dict[str, Any] | None:
    with _schedule_lock:
        schedules = get_schedules()
        current_time = now_jst()
        dirty = False
        for item in schedules:
            if item.get("status") != "pending":
                continue
            try:
                scheduled_at = datetime.fromisoformat(item["scheduled_at"]).astimezone(JST)
            except Exception:
                item["status"] = "failed"
                item["last_error"] = "予約日時を読み取れませんでした。"
                dirty = True
                continue
            if scheduled_at <= current_time:
                item["status"] = "running"
                item["started_at"] = now_iso()
                save_schedules(schedules)
                return dict(item)
        if dirty:
            save_schedules(schedules)
    return None


def finish_schedule(schedule_id: str, success: bool, message: str) -> None:
    with _schedule_lock:
        schedules = get_schedules()
        for item in schedules:
            if item.get("id") != schedule_id:
                continue
            item["status"] = "completed" if success else "failed"
            item["result_message"] = message
            item["last_error"] = "" if success else message
            item["posted_at"] = now_iso() if success else ""
            break
        save_schedules(schedules)


def process_due_schedules() -> None:
    if not existing_profile_available():
        return
    job = claim_due_schedule()
    if not job:
        return
    success, message = execute_post(
        job.get("content", ""),
        job.get("media_items") or job.get("media_path", ""),
        job.get("media_filename", ""),
        job.get("profile_handle", DEFAULT_PROFILE_HANDLE),
        "予約投稿",
    )
    finish_schedule(job.get("id", ""), success, message)


def scheduler_loop() -> None:
    while True:
        try:
            process_due_schedules()
        except Exception:
            pass
        time.sleep(SCHEDULE_POLL_SECONDS)


def ensure_scheduler_started() -> None:
    global _scheduler_started
    if _scheduler_started or not LOCAL_MODE:
        return
    worker = threading.Thread(target=scheduler_loop, name="x-auto-poster-scheduler", daemon=True)
    worker.start()
    _scheduler_started = True


def normalize_follow_handle(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw.startswith(("https://", "http://")):
        parsed = urllib.parse.urlparse(raw)
        raw = parsed.path.strip("/").split("/", 1)[0]
    raw = raw.replace("x.com/", "").replace("twitter.com/", "")
    raw = raw.split("?", 1)[0].split("#", 1)[0].strip().lstrip("@")
    if "/" in raw:
        raw = raw.split("/", 1)[0]
    return "".join(ch for ch in raw if ch.isalnum() or ch == "_")


def follow_profile_url(handle: str) -> str:
    return f"https://x.com/{handle}"


def parse_iso_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    return parsed.astimezone(JST)


def get_follow_review() -> dict[str, Any]:
    saved = data_read(FOLLOW_REVIEW_FILE, None) or {}
    review = {
        **DEFAULT_FOLLOW_REVIEW,
        "current_candidate_id": (saved.get("current_candidate_id") or ""),
        "candidates": list(saved.get("candidates") or []),
        "action_log": list(saved.get("action_log") or []),
    }

    normalized_candidates: list[dict[str, Any]] = []
    for item in review["candidates"]:
        handle = normalize_follow_handle(item.get("handle", ""))
        if not handle:
            continue
        normalized_candidates.append(
            {
                "id": item.get("id") or uuid.uuid4().hex,
                "handle": handle,
                "profile_url": item.get("profile_url") or follow_profile_url(handle),
                "follower_count": int(item.get("follower_count") or 0),
                "note": (item.get("note") or "").strip(),
                "source": (item.get("source") or "manual-import").strip() or "manual-import",
                "status": item.get("status") or "pending",
                "created_at": item.get("created_at") or now_iso(),
                "opened_at": item.get("opened_at") or "",
                "reviewed_at": item.get("reviewed_at") or "",
                "updated_at": item.get("updated_at") or item.get("created_at") or now_iso(),
            }
        )

    review["candidates"] = normalized_candidates
    review["action_log"] = list(review.get("action_log") or [])[-MAX_FOLLOW_ACTIONS:]

    current_id = review.get("current_candidate_id") or ""
    if current_id and not any(item.get("id") == current_id and item.get("status") == "reviewing" for item in review["candidates"]):
        review["current_candidate_id"] = ""

    return review


def save_follow_review(review: dict[str, Any]) -> None:
    payload = {
        "current_candidate_id": review.get("current_candidate_id") or "",
        "candidates": list(review.get("candidates") or []),
        "action_log": list(review.get("action_log") or [])[-MAX_FOLLOW_ACTIONS:],
    }
    data_write(FOLLOW_REVIEW_FILE, payload)


def find_follow_candidate(review: dict[str, Any], candidate_id: str) -> dict[str, Any] | None:
    for item in review.get("candidates", []):
        if item.get("id") == candidate_id:
            return item
    return None


def get_current_follow_candidate(review: dict[str, Any]) -> dict[str, Any] | None:
    candidate_id = review.get("current_candidate_id") or ""
    if not candidate_id:
        return None
    candidate = find_follow_candidate(review, candidate_id)
    if not candidate or candidate.get("status") != "reviewing":
        return None
    return candidate


def add_follow_action(review: dict[str, Any], candidate: dict[str, Any], action: str) -> None:
    review.setdefault("action_log", []).append(
        {
            "time": now_iso(),
            "action": action,
            "candidate_id": candidate.get("id", ""),
            "handle": candidate.get("handle", ""),
        }
    )
    review["action_log"] = review["action_log"][-MAX_FOLLOW_ACTIONS:]


def follow_rate_status(review: dict[str, Any]) -> dict[str, Any]:
    now = now_jst()
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    follow_times = []
    for item in review.get("action_log", []):
        if item.get("action") != "followed":
            continue
        action_time = parse_iso_datetime(item.get("time", ""))
        if action_time is not None:
            follow_times.append(action_time)

    daily_times = [item for item in follow_times if item >= start_of_day]
    window_threshold = now - timedelta(minutes=FOLLOW_WINDOW_MINUTES)
    window_times = [item for item in follow_times if item >= window_threshold]
    last_follow_time = max(follow_times) if follow_times else None

    next_available_times: list[datetime] = []
    waiting_reasons: list[str] = []

    if len(daily_times) >= FOLLOW_SAFE_DAILY_LIMIT:
        next_available_times.append(start_of_day + timedelta(days=1))
        waiting_reasons.append("本日の上限")

    if len(window_times) >= FOLLOW_SAFE_WINDOW_LIMIT:
        sorted_window_times = sorted(window_times)
        release_index = len(sorted_window_times) - FOLLOW_SAFE_WINDOW_LIMIT
        next_available_times.append(sorted_window_times[release_index] + timedelta(minutes=FOLLOW_WINDOW_MINUTES))
        waiting_reasons.append(f"{FOLLOW_WINDOW_MINUTES}分上限")

    if last_follow_time is not None:
        cooldown_until = last_follow_time + timedelta(seconds=FOLLOW_COOLDOWN_SECONDS)
        if cooldown_until > now:
            next_available_times.append(cooldown_until)
            waiting_reasons.append("クールダウン")

    next_available_at = max(next_available_times) if next_available_times else None

    return {
        "daily_count": len(daily_times),
        "daily_limit": FOLLOW_SAFE_DAILY_LIMIT,
        "window_count": len(window_times),
        "window_limit": FOLLOW_SAFE_WINDOW_LIMIT,
        "window_minutes": FOLLOW_WINDOW_MINUTES,
        "cooldown_seconds": FOLLOW_COOLDOWN_SECONDS,
        "available": next_available_at is None,
        "next_available_at": next_available_at.isoformat() if next_available_at else "",
        "next_available_display": next_available_at.strftime("%Y-%m-%d %H:%M") if next_available_at else "今すぐ可",
        "waiting_reason": " / ".join(waiting_reasons) if waiting_reasons else "",
    }


def parse_follow_candidate_line(line: str) -> tuple[str, int, str]:
    raw = line.strip()
    if not raw:
        raise ValueError("空行です。")

    parts = [part.strip() for part in (raw.split("\t", 2) if "\t" in raw else raw.split(",", 2))]
    if len(parts) < 2:
        raise ValueError("`@handle, 42, メモ` の形式で入力してください。")

    handle = normalize_follow_handle(parts[0])
    if not handle:
        raise ValueError("ハンドル名を読み取れませんでした。")

    count_text = "".join(ch for ch in parts[1] if ch.isdigit())
    if not count_text:
        raise ValueError("フォロワー数を数字で入れてください。")

    follower_count = int(count_text)
    note = parts[2].strip() if len(parts) > 2 else ""
    return handle, follower_count, note


def import_follow_candidates(text: str) -> tuple[int, list[str]]:
    review = get_follow_review()
    existing_handles = {item.get("handle", "").lower() for item in review.get("candidates", [])}
    added = 0
    skipped: list[str] = []

    for line_number, raw_line in enumerate((text or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            handle, follower_count, note = parse_follow_candidate_line(line)
        except ValueError as exc:
            skipped.append(f"{line_number}行目: {exc}")
            continue

        if follower_count > FOLLOW_TARGET_MAX_FOLLOWERS:
            skipped.append(f"{line_number}行目: フォロワー数が {FOLLOW_TARGET_MAX_FOLLOWERS} を超えています。")
            continue

        if handle.lower() in existing_handles:
            skipped.append(f"{line_number}行目: @{handle} は既に登録済みです。")
            continue

        review.setdefault("candidates", []).append(
            {
                "id": uuid.uuid4().hex,
                "handle": handle,
                "profile_url": follow_profile_url(handle),
                "follower_count": follower_count,
                "note": note,
                "source": "manual-import",
                "status": "pending",
                "created_at": now_iso(),
                "opened_at": "",
                "reviewed_at": "",
                "updated_at": now_iso(),
            }
        )
        existing_handles.add(handle.lower())
        added += 1

    save_follow_review(review)
    return added, skipped


def pick_follow_candidate(review: dict[str, Any]) -> dict[str, Any] | None:
    current = get_current_follow_candidate(review)
    if current is not None:
        return current

    pending = [
        item
        for item in review.get("candidates", [])
        if item.get("status") == "pending" and int(item.get("follower_count") or 0) <= FOLLOW_TARGET_MAX_FOLLOWERS
    ]
    if not pending:
        return None

    candidate = random.choice(pending)
    candidate["status"] = "reviewing"
    candidate["updated_at"] = now_iso()
    review["current_candidate_id"] = candidate.get("id", "")
    save_follow_review(review)
    return candidate


def build_follow_review_view() -> dict[str, Any]:
    review = get_follow_review()
    current_candidate = get_current_follow_candidate(review)
    rate = follow_rate_status(review)

    status_labels = {
        "pending": "待機中",
        "reviewing": "レビュー中",
        "followed": "フォロー済み",
        "skipped": "スキップ",
    }
    action_labels = {
        "opened": "プロフィールを開いた",
        "followed": "フォロー済みに記録",
        "skipped": "スキップ",
    }

    def candidate_for_view(item: dict[str, Any]) -> dict[str, Any]:
        return {
            **item,
            "status_label": status_labels.get(item.get("status", "pending"), item.get("status", "pending")),
            "created_at_display": to_display_time(item.get("created_at", "")),
            "opened_at_display": to_display_time(item.get("opened_at", "")) if item.get("opened_at") else "",
            "reviewed_at_display": to_display_time(item.get("reviewed_at", "")) if item.get("reviewed_at") else "",
        }

    recent_pending = [
        candidate_for_view(item)
        for item in sorted(
            [item for item in review.get("candidates", []) if item.get("status") == "pending"],
            key=lambda item: item.get("created_at", ""),
            reverse=True,
        )[:8]
    ]
    recent_actions = []
    for action in reversed(review.get("action_log", [])):
        if action.get("action") not in action_labels:
            continue
        recent_actions.append(
            {
                **action,
                "action_label": action_labels.get(action.get("action", ""), action.get("action", "")),
                "time_display": to_display_time(action.get("time", "")),
            }
        )
        if len(recent_actions) >= 8:
            break

    return {
        "current_candidate": candidate_for_view(current_candidate) if current_candidate else None,
        "pending_count": sum(1 for item in review.get("candidates", []) if item.get("status") == "pending"),
        "followed_count": sum(1 for item in review.get("candidates", []) if item.get("status") == "followed"),
        "skipped_count": sum(1 for item in review.get("candidates", []) if item.get("status") == "skipped"),
        "pending_candidates": recent_pending,
        "recent_actions": recent_actions,
        "rate": rate,
    }


HTML = """
<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>X 自動投稿</title>
  <style>
    *{box-sizing:border-box}body{margin:0;font-family:"Segoe UI","Yu Gothic UI",sans-serif;background:#f6efe4;color:#1f2a33}
    .shell{max-width:1000px;margin:0 auto;padding:24px 16px 40px}.card{background:#fffaf2;border:1px solid #dfcfb8;border-radius:20px;padding:18px;box-shadow:0 12px 30px rgba(93,72,40,.08)}
    .card+.card{margin-top:16px}.grid{display:grid;grid-template-columns:1.35fr .95fr;gap:16px}.top,.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.row.space{justify-content:space-between}
    h1,h2,p{margin:0}.badge,.pill{display:inline-block;border-radius:999px;padding:4px 10px;font-size:12px;font-weight:700}.badge{background:#dceaf7;color:#235884}
    .pill.ok{background:#e3f6e7;color:#24643a}.pill.ng{background:#fbe6e7;color:#8a2f3b}.pill.wait{background:#e7f0fa;color:#285982}.pill.run{background:#fff1d8;color:#7d5a0d}
    .summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-top:14px}.metric,.box,.item{background:#faf4ea;border:1px solid #e3d4be;border-radius:16px;padding:14px}
    .label,.muted{font-size:12px;color:#746857}.value{margin-top:6px;font-size:18px;font-weight:700;word-break:break-word}.sub{margin-top:8px;color:#5f6f7d;line-height:1.7}
    textarea,input[type="datetime-local"]{width:100%;border:1px solid #d8c6aa;border-radius:14px;background:#fffdf9;padding:14px 16px;font-size:15px;color:#1f2a33}
    textarea{min-height:220px;resize:vertical;line-height:1.7}.field{margin-top:14px}.field label{display:block;margin-bottom:8px;font-size:13px;font-weight:700;color:#6d6255}
    .meta{margin-top:10px;display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;color:#7b6f61;font-size:13px}.btn{border:none;border-radius:14px;padding:12px 16px;font-size:14px;font-weight:700;cursor:pointer}
    .btn:disabled{opacity:.55;cursor:not-allowed}.btn-primary{background:#2f6ea8;color:#fff}.btn-secondary{background:#d7e5f2;color:#244c72}.btn-soft{background:#efe2cf;color:#6b4d27}.btn-danger{background:#ead0d0;color:#8d343d}
    .status{display:none;margin-top:14px;padding:12px 14px;border-radius:14px;line-height:1.6}.status.ok{display:block;background:#e3f6e7;border:1px solid #9fd0a8;color:#24643a}.status.err{display:block;background:#fbe6e7;border:1px solid #e3a2a7;color:#8a2f3b}.status.info{display:block;background:#e7f0fa;border:1px solid #a9c6e6;color:#285982}
    .hint{margin-top:14px;padding:14px;background:#fff7ea;border:1px solid #edd7af;border-radius:16px;color:#715a2f;line-height:1.7}.preview{margin-top:12px;border-radius:14px;overflow:hidden;border:1px solid #e0cfb4;background:#fff}
    .preview img,.preview video{width:100%;max-height:300px;object-fit:contain;display:block}.list{display:grid;gap:10px;margin-top:12px}
    .editor-shell{margin-top:16px}.editor-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap}.editor-actions{display:flex;gap:10px;flex-wrap:wrap}.editor-frame{width:100%;height:920px;border:1px solid #dfcfb8;border-radius:18px;background:#0f0f0f;margin-top:14px}
    @media (max-width:900px){.grid{grid-template-columns:1fr}}@media (max-width:640px){.row,.row.space,.meta{flex-direction:column;align-items:stretch}.btn{width:100%}}
  </style>
</head>
<body>
  <div class="shell">
    <div class="card">
      <div class="top">
        <h1>X 自動投稿</h1>
        <span class="badge">v{{ version }}</span>
        <span class="badge">{{ storage_mode }}</span>
        <span class="badge">{{ "既存Chrome利用可" if existing_profile_available else "既存Chrome利用不可" }}</span>
      </div>
      <p class="sub">既存の Chrome プロフィールで X に投稿します。画像・動画の投稿と、ローカルで動く予約投稿に対応しています。</p>
      <div class="summary">
        <div class="metric"><div class="label">現在のメディア</div><div class="value" id="media-name">{{ state.media_summary or "未選択" }}</div></div>
        <div class="metric"><div class="label">最後の投稿</div><div class="value">{{ state.last_post_at or "まだありません" }}</div></div>
        <div class="metric"><div class="label">次の予約</div><div class="value">{{ next_schedule.scheduled_at_display if next_schedule else "なし" }}</div></div>
        <div class="metric"><div class="label">使用プロフィール</div><div class="value">{{ chrome_profile }}</div></div>
      </div>
    </div>

    <div class="grid" style="margin-top:16px;">
      <div class="card">
        <h2>投稿内容</h2>
        <p class="sub">本文とメディアを作って、今すぐ投稿するか、そのまま予約できます。</p>
        <div class="field"><label for="content">本文</label><textarea id="content" placeholder="ここに投稿文を書いてください。">{{ state.content }}</textarea></div>
        <div class="meta"><div id="autosave">下書きは自動保存されます。</div><div><span id="char-count">0</span> 文字</div></div>
        <div class="box" style="margin-top:16px;">
          <div class="row space">
            <div><div class="label">画像・動画</div><div class="value" style="font-size:16px;" id="media-label">{{ state.media_summary or "なし" }}</div></div>
            <div class="row">
              <label class="btn btn-soft">画像・動画を選ぶ<input id="media-input" type="file" accept="image/*,video/*" multiple style="display:none"></label>
              <button class="btn btn-soft" type="button" onclick="openCompilerPicker(event)" {% if not video_compiler_available %}disabled{% endif %}>Fanzaコンパイラから反映</button>
              <button class="btn btn-secondary" onclick="clearMedia()">メディアを外す</button>
            </div>
          </div>
          <div id="preview-area"></div>
        </div>
        <div class="row" style="margin-top:16px;">
          <button class="btn btn-secondary" onclick="openX(event)" {% if not existing_profile_available %}disabled{% endif %}>既存Chromeで投稿画面を開く</button>
          <button class="btn btn-primary" onclick="postNow(event)" {% if not existing_profile_available %}disabled{% endif %}>今すぐ投稿する</button>
        </div>
        <div class="hint">予約投稿はこのアプリがローカルで動いている間だけ実行されます。予約時刻に PC がスリープしていると実行できません。</div>
        <div id="status" class="status" data-shared-status="1"></div>
      </div>

      <div class="card">
        <h2>予約投稿</h2>
        <p class="sub">今の本文と今のメディアを、その時点の内容で保存して指定時刻に投稿します。</p>
        <div class="box">
          <div class="field" style="margin-top:0;"><label for="schedule-at">予約日時</label><input id="schedule-at" type="datetime-local" value="{{ default_schedule_value }}"></div>
          <button class="btn btn-primary" style="margin-top:12px;" onclick="createSchedule(event)" {% if not existing_profile_available %}disabled{% endif %}>この内容で予約する</button>
        </div>
        <div class="list">
          {% if schedules %}
            {% for item in schedules %}
              <div class="item">
                <div class="row space"><strong>{{ item.scheduled_at_display }}</strong><span class="pill {% if item.status == 'completed' %}ok{% elif item.status == 'failed' or item.status == 'canceled' %}ng{% elif item.status == 'running' %}run{% else %}wait{% endif %}">{{ item.status_label }}</span></div>
                <div class="muted" style="margin-top:8px;">本文</div><div style="white-space:pre-wrap; margin-top:4px;">{{ item.content_preview }}</div>
                <div class="muted" style="margin-top:8px;">メディア</div><div style="margin-top:4px;">{{ item.media_filename or "なし" }}</div>
                {% if item.result_message or item.last_error %}<div class="muted" style="margin-top:8px;">結果</div><div style="white-space:pre-wrap; margin-top:4px;">{{ item.result_message or item.last_error }}</div>{% endif %}
                <div class="row space" style="margin-top:12px;"><div class="muted">作成: {{ item.created_at_display }}</div><button class="btn btn-danger" onclick="deleteSchedule('{{ item.id }}')">削除</button></div>
              </div>
            {% endfor %}
          {% else %}
            <div class="item muted">まだ予約はありません。</div>
          {% endif %}
        </div>
      </div>
    </div>

    <div class="card editor-shell">
      <div class="editor-head">
        <div>
          <h2>動画編集</h2>
          <p class="sub">この画面の中で `hf-video-compiler` を開き、作成した動画や画像をそのまま下書きへ戻せます。</p>
        </div>
        <div class="editor-actions">
          <span class="badge">{{ "動画編集 利用可" if video_compiler_available else "動画編集 未検出" }}</span>
          <span class="badge">{{ "編集中ツール 起動中" if video_compiler_running else "編集中ツール 停止中" }}</span>
          <button class="btn btn-secondary" onclick="startVideoEditor(event)" {% if not video_compiler_available %}disabled{% endif %}>動画編集を起動</button>
          <button class="btn btn-secondary" onclick="openVideoEditor()" {% if not video_compiler_available %}disabled{% endif %}>別タブで開く</button>
          <button class="btn btn-secondary" onclick="reloadVideoEditor()" {% if not video_compiler_available %}disabled{% endif %}>再読み込み</button>
        </div>
      </div>
      {% if video_compiler_available %}
        <div class="hint">動画を作ったら、編集ツール側の `X連携` から `動画をX下書きへ送る` を押してください。上の投稿欄へ自動で戻せます。</div>
        <iframe id="video-editor-frame" class="editor-frame" src="{{ video_compiler_url }}"></iframe>
      {% else %}
        <div class="hint">`hf-video-compiler` フォルダが見つからないため、動画編集を表示できません。</div>
      {% endif %}
    </div>

    <div class="grid" style="margin-top:16px; display:none;" id="follow-review-section">
      <div class="card">
        <h2>レビュー付きフォロー支援</h2>
        <p class="sub">自動フォローはせず、候補の抽選と記録だけを行います。候補は `@handle, 42, メモ` の形式で追加してください。</p>
        <div class="box">
          <div class="field" style="margin-top:0;">
            <label for="follow-import">候補の一括追加</label>
            <textarea id="follow-import" placeholder="@sample_account, 42, AI関連&#10;another_user, 18, video creator" style="min-height:160px;"></textarea>
          </div>
          <div class="row" style="margin-top:12px;">
            <button class="btn btn-secondary" onclick="importFollowCandidates(event)">候補を追加</button>
            <button class="btn btn-primary" onclick="pickFollowCandidate(event)">ランダムに1件選ぶ</button>
          </div>
        </div>
        <div class="hint">この画面をフォローに使う Chrome プロフィールで開いてください。プロフィールは新しいタブで開くだけで、フォロー操作は X 上で手動です。</div>
        {% if follow.current_candidate %}
          <div class="item" style="margin-top:12px;">
            <div class="row space">
              <strong>@{{ follow.current_candidate.handle }}</strong>
              <span class="pill wait">{{ follow.current_candidate.status_label }}</span>
            </div>
            <div class="muted" style="margin-top:8px;">フォロワー数</div>
            <div style="margin-top:4px;">{{ follow.current_candidate.follower_count }}</div>
            <div class="muted" style="margin-top:8px;">メモ</div>
            <div style="white-space:pre-wrap; margin-top:4px;">{{ follow.current_candidate.note or "なし" }}</div>
            <div class="muted" style="margin-top:8px;">追加日時</div>
            <div style="margin-top:4px;">{{ follow.current_candidate.created_at_display }}</div>
            {% if follow.current_candidate.opened_at_display %}
              <div class="muted" style="margin-top:8px;">最後に開いた日時</div>
              <div style="margin-top:4px;">{{ follow.current_candidate.opened_at_display }}</div>
            {% endif %}
            <div class="row" style="margin-top:12px;">
              <button class="btn btn-secondary" onclick="openFollowCandidate(event, '{{ follow.current_candidate.id }}', '{{ follow.current_candidate.profile_url }}')">Xプロフィールを開く</button>
              <button class="btn btn-primary" onclick="markFollowed(event, '{{ follow.current_candidate.id }}')">フォロー済みにする</button>
              <button class="btn btn-danger" onclick="skipFollowCandidate(event, '{{ follow.current_candidate.id }}')">スキップ</button>
            </div>
          </div>
        {% else %}
          <div class="item muted" style="margin-top:12px;">レビュー中の候補はありません。候補を追加してからランダム抽選してください。</div>
        {% endif %}
        <div id="follow-status" class="status" data-shared-status="1"></div>
      </div>

      <div class="card">
        <h2>ペース管理</h2>
        <p class="sub">安全側の固定上限です。日次 {{ follow.rate.daily_limit }} 件、{{ follow.rate.window_minutes }} 分で {{ follow.rate.window_limit }} 件、フォロー記録ごとに {{ follow.rate.cooldown_seconds }} 秒クールダウンします。</p>
        <div class="summary" style="margin-top:12px;">
          <div class="metric"><div class="label">本日</div><div class="value">{{ follow.rate.daily_count }} / {{ follow.rate.daily_limit }}</div></div>
          <div class="metric"><div class="label">{{ follow.rate.window_minutes }}分</div><div class="value">{{ follow.rate.window_count }} / {{ follow.rate.window_limit }}</div></div>
          <div class="metric"><div class="label">次に記録できる時刻</div><div class="value">{{ follow.rate.next_available_display }}</div></div>
          <div class="metric"><div class="label">待機中候補</div><div class="value">{{ follow.pending_count }}</div></div>
        </div>
        <div class="hint" style="margin-top:12px;">{% if follow.rate.available %}今はフォロー済み記録が可能です。{% else %}現在は {{ follow.rate.waiting_reason }} のため待機中です。次に記録できる時刻: {{ follow.rate.next_available_display }}{% endif %}</div>

        <div class="list">
          <div class="item">
            <div class="row space"><strong>待機中の候補</strong><span class="pill wait">{{ follow.pending_count }} 件</span></div>
            {% if follow.pending_candidates %}
              {% for item in follow.pending_candidates %}
                <div class="row space" style="margin-top:10px;">
                  <div>@{{ item.handle }} / {{ item.follower_count }}</div>
                  <div class="muted">{{ item.created_at_display }}</div>
                </div>
              {% endfor %}
            {% else %}
              <div class="muted" style="margin-top:10px;">待機中の候補はありません。</div>
            {% endif %}
          </div>

          <div class="item">
            <div class="row space">
              <strong>最近の判定</strong>
              <span class="pill ok">フォロー済み {{ follow.followed_count }}</span>
            </div>
            <div class="muted" style="margin-top:8px;">スキップ {{ follow.skipped_count }}</div>
            {% if follow.recent_actions %}
              {% for action in follow.recent_actions %}
                <div style="margin-top:10px;">
                  <div><strong>{{ action.action_label }}</strong> / @{{ action.handle }}</div>
                  <div class="muted" style="margin-top:2px;">{{ action.time_display }}</div>
                </div>
              {% endfor %}
            {% else %}
              <div class="muted" style="margin-top:10px;">まだ判定ログはありません。</div>
            {% endif %}
          </div>
        </div>
      </div>
    </div>

    <div class="card" style="margin-top:16px;">
      <h2>最近のログ</h2>
      <div class="list">
        {% if logs %}
          {% for log in logs %}
            <div class="item">
              <div class="row space"><strong>{{ log.time }}</strong><span class="pill {{ 'ok' if log.success else 'ng' }}">{{ '成功' if log.success else '失敗' }}</span></div>
              <div class="muted" style="margin-top:8px;">種類</div><div style="margin-top:4px;">{{ log.source or "手動" }}</div>
              <div class="muted" style="margin-top:8px;">本文</div><div style="white-space:pre-wrap; margin-top:4px;">{{ log.content or "なし" }}</div>
              <div class="muted" style="margin-top:8px;">メディア</div><div style="margin-top:4px;">{{ log.media_filename or "なし" }}</div>
              <div class="muted" style="margin-top:8px;">結果</div><div style="white-space:pre-wrap; margin-top:4px;">{{ log.message }}</div>
            </div>
          {% endfor %}
        {% else %}
          <div class="item muted">まだログはありません。</div>
        {% endif %}
      </div>
    </div>
  </div>
  <script>
    const contentEl = document.getElementById('content');
    const scheduleAtEl = document.getElementById('schedule-at');
    const followImportEl = document.getElementById('follow-import');
    const autosaveEl = document.getElementById('autosave');
    const charCountEl = document.getElementById('char-count');
    const mediaLabelEl = document.getElementById('media-label');
    const mediaNameEl = document.getElementById('media-name');
    const previewAreaEl = document.getElementById('preview-area');
    const mediaInputEl = document.getElementById('media-input');
    const videoEditorFrameEl = document.getElementById('video-editor-frame');
    const initialMediaItems = {{ state.media_items|tojson }};
    let saveTimer = null;
    function updateCharCount(){ charCountEl.textContent = contentEl.value.length; }
    function showStatus(message, type){ document.querySelectorAll('[data-shared-status="1"]').forEach((el)=>{ el.textContent=message; el.className='status '+type; }); }
    async function saveDraft(){ const r = await fetch('/draft',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:contentEl.value})}); const d = await r.json(); if(!r.ok||!d.ok) throw new Error(d.error||'下書きの保存に失敗しました。'); autosaveEl.textContent='下書きは自動保存されます。'; }
    function queueDraftSave(){ autosaveEl.textContent='下書きを保存中...'; clearTimeout(saveTimer); saveTimer=setTimeout(async()=>{ try{ await saveDraft(); } catch(error){ autosaveEl.textContent=error.message; } }, 500); }
    function renderPreview(url,isVideo){ previewAreaEl.innerHTML=''; if(!url)return; const box=document.createElement('div'); box.className='preview'; if(isVideo){ const v=document.createElement('video'); v.src=url; v.controls=true; box.appendChild(v);} else { const i=document.createElement('img'); i.src=url; i.alt='preview'; box.appendChild(i);} previewAreaEl.appendChild(box); }
    async function uploadMedia(input){ const file=input.files[0]; if(!file)return; showStatus('メディアをアップロードしています...','info'); const form=new FormData(); form.append('file',file); try{ const r=await fetch('/upload',{method:'POST',body:form}); const d=await r.json(); if(!r.ok||!d.ok) throw new Error(d.error||'メディアのアップロードに失敗しました。'); mediaLabelEl.textContent=d.filename; mediaNameEl.textContent=d.filename; renderPreview(URL.createObjectURL(file), file.type.startsWith('video/')); showStatus(d.message,'ok'); } catch(error){ showStatus(error.message,'err'); } finally { input.value=''; } }
    async function clearMedia(){ try{ const r=await fetch('/media/clear',{method:'POST'}); const d=await r.json(); if(!r.ok||!d.ok) throw new Error(d.error||'メディアの削除に失敗しました。'); mediaLabelEl.textContent='なし'; mediaNameEl.textContent='未選択'; renderPreview('', false); showStatus(d.message,'ok'); } catch(error){ showStatus(error.message,'err'); } }
    async function withButton(button, fn){ button.disabled=true; try{ await fn(); } finally { button.disabled=false; } }
    async function openX(event){ await withButton(event.currentTarget, async()=>{ showStatus('既存Chromeで投稿画面を開いています...','info'); await saveDraft(); const r=await fetch('/open-x',{method:'POST'}); const d=await r.json(); if(!r.ok||!d.ok) throw new Error(d.error||'投稿画面を開けませんでした。'); showStatus(d.message,'ok'); }); }
    async function postNow(event){ if(!confirm('既存Chromeでそのまま投稿します。続けますか。')) return; await withButton(event.currentTarget, async()=>{ showStatus('今すぐ投稿しています。Chrome が再起動することがあります...','info'); await saveDraft(); const r=await fetch('/post',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:contentEl.value})}); const d=await r.json(); if(!r.ok||!d.ok) throw new Error(d.error||'投稿に失敗しました。'); showStatus(d.message,'ok'); setTimeout(()=>location.reload(),1200); }); }
    async function createSchedule(event){ await withButton(event.currentTarget, async()=>{ showStatus('予約を保存しています...','info'); await saveDraft(); const r=await fetch('/schedule',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:contentEl.value, scheduled_at:scheduleAtEl.value})}); const d=await r.json(); if(!r.ok||!d.ok) throw new Error(d.error||'予約投稿の作成に失敗しました。'); showStatus(d.message,'ok'); setTimeout(()=>location.reload(),800); }); }
    async function deleteSchedule(id){ if(!confirm('この予約を削除しますか。')) return; try{ const r=await fetch(`/schedule/${id}/delete`,{method:'POST'}); const d=await r.json(); if(!r.ok||!d.ok) throw new Error(d.error||'予約の削除に失敗しました。'); showStatus(d.message,'ok'); setTimeout(()=>location.reload(),500); } catch(error){ showStatus(error.message,'err'); } }
    async function startVideoEditor(event){ await withButton(event.currentTarget, async()=>{ showStatus('動画編集ツールを起動しています...','info'); const r=await fetch('/video-editor/start',{method:'POST'}); const d=await r.json(); if(!r.ok||!d.ok) throw new Error(d.error||'動画編集ツールを起動できませんでした。'); showStatus(d.message,'ok'); if(videoEditorFrameEl){ videoEditorFrameEl.src='{{ video_compiler_url }}?ts='+Date.now(); } setTimeout(()=>location.reload(),800); }); }
    function openVideoEditor(){ window.open('{{ video_compiler_url }}','_blank'); }
    function reloadVideoEditor(){ if(videoEditorFrameEl){ videoEditorFrameEl.src='{{ video_compiler_url }}?ts='+Date.now(); } }
    async function importFollowCandidates(event){ await withButton(event.currentTarget, async()=>{ const candidates=(followImportEl?.value||'').trim(); if(!candidates) throw new Error('追加する候補を入力してください。'); showStatus('フォロー候補を取り込んでいます...','info'); const r=await fetch('/follow/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({candidates})}); const d=await r.json(); if(!r.ok||!d.ok) throw new Error(d.error||'候補の追加に失敗しました。'); followImportEl.value=''; showStatus(d.message,'ok'); setTimeout(()=>location.reload(),700); }); }
    async function pickFollowCandidate(event){ await withButton(event.currentTarget, async()=>{ showStatus('ランダム候補を選んでいます...','info'); const r=await fetch('/follow/pick',{method:'POST'}); const d=await r.json(); if(!r.ok||!d.ok) throw new Error(d.error||'候補を選べませんでした。'); showStatus(d.message,'ok'); setTimeout(()=>location.reload(),500); }); }
    async function openFollowCandidate(event, id, url){ await withButton(event.currentTarget, async()=>{ const r=await fetch(`/follow/candidate/${id}/open`,{method:'POST'}); const d=await r.json(); if(!r.ok||!d.ok) throw new Error(d.error||'プロフィールを開けませんでした。'); const tab=window.open(url,'_blank','noopener'); if(!tab){ window.location.href=url; } showStatus(d.message,'ok'); setTimeout(()=>location.reload(),500); }); }
    async function markFollowed(event, id){ if(!confirm('X 上で手動フォローしたあとに記録します。続けますか。')) return; await withButton(event.currentTarget, async()=>{ const r=await fetch(`/follow/candidate/${id}/followed`,{method:'POST'}); const d=await r.json(); if(!r.ok||!d.ok) throw new Error(d.error||'フォロー済みの記録に失敗しました。'); showStatus(d.message,'ok'); setTimeout(()=>location.reload(),500); }); }
    async function skipFollowCandidate(event, id){ if(!confirm('この候補をスキップしますか。')) return; await withButton(event.currentTarget, async()=>{ const r=await fetch(`/follow/candidate/${id}/skip`,{method:'POST'}); const d=await r.json(); if(!r.ok||!d.ok) throw new Error(d.error||'スキップに失敗しました。'); showStatus(d.message,'ok'); setTimeout(()=>location.reload(),500); }); }
    contentEl.addEventListener('input',()=>{ updateCharCount(); queueDraftSave(); }); updateCharCount();
    function mediaRoute(index){ return `/media-items/${index}`; }
    function mediaLabelText(items){ if(!items.length) return 'なし'; const names=items.map((item)=>item.filename); if(names.length===1) return names[0]; const preview=names.slice(0,3).join(' / '); return names.length>3 ? `${preview} ほか${names.length-3}件` : preview; }
    function renderPreviewItems(items){ previewAreaEl.innerHTML=''; if(!items.length) return; const grid=document.createElement('div'); grid.className='list'; grid.style.display='grid'; grid.style.gridTemplateColumns='repeat(auto-fit,minmax(220px,1fr))'; grid.style.gap='10px'; items.forEach((item, index)=>{ const box=document.createElement('div'); box.className='preview'; box.style.position='relative'; box.style.padding='0'; box.style.background='#fff'; const media=item.is_video ? document.createElement('video') : document.createElement('img'); media.src=item.url; media.style.background='#f7f0e5'; media.style.minHeight='180px'; if(item.is_video){ media.controls=true; media.playsInline=true; } else { media.alt=item.filename||'preview'; } box.appendChild(media); const badge=document.createElement('div'); badge.className='badge'; badge.style.position='absolute'; badge.style.top='10px'; badge.style.left='10px'; badge.style.background='rgba(255,250,242,.92)'; badge.style.color='#244c72'; badge.textContent = item.is_video ? `動画 ${index+1}` : `画像 ${index+1}`; box.appendChild(badge); const caption=document.createElement('div'); caption.className='muted'; caption.style.padding='10px 12px'; caption.style.borderTop='1px solid #e8d8bf'; caption.textContent=item.filename||''; box.appendChild(caption); grid.appendChild(box); }); previewAreaEl.appendChild(grid); }
    function applyMediaSummary(items){ const label=mediaLabelText(items); mediaLabelEl.textContent=label; mediaNameEl.textContent=label || '未選択'; }
    async function uploadMediaFiles(input){ const files=[...(input.files||[])]; if(!files.length) return; showStatus('メディアをアップロードしています...','info'); const form=new FormData(); files.forEach((file)=>form.append('files', file)); try{ const r=await fetch('/media-items/upload',{method:'POST',body:form}); const d=await r.json(); if(!r.ok||!d.ok) throw new Error(d.error||'メディアのアップロードに失敗しました。'); applyMediaSummary(files.map((file)=>({filename:file.name}))); renderPreviewItems(files.map((file)=>({filename:file.name,is_video:file.type.startsWith('video/'),url:URL.createObjectURL(file)}))); showStatus(d.message,'ok'); } catch(error){ showStatus(error.message,'err'); } finally { input.value=''; } }
    async function clearMedia(){ try{ const r=await fetch('/media/clear',{method:'POST'}); const d=await r.json(); if(!r.ok||!d.ok) throw new Error(d.error||'メディアの削除に失敗しました。'); applyMediaSummary([]); renderPreviewItems([]); showStatus(d.message,'ok'); } catch(error){ showStatus(error.message,'err'); } }
    function ensureCompilerPicker(){ let picker=document.getElementById('compiler-picker'); if(!picker){ const controls=mediaInputEl?.closest('label')?.parentElement; if(controls){ const button=document.createElement('button'); button.className='btn btn-soft'; button.textContent='Fanzaコンパイラから反映'; button.onclick=(event)=>openCompilerPicker(event); controls.insertBefore(button, controls.lastElementChild); } picker=document.createElement('div'); picker.id='compiler-picker'; picker.className='box'; picker.style.marginTop='12px'; picker.style.display='none'; picker.innerHTML='<div class=\"row space\"><div><div class=\"label\">Fanzaコンパイラ素材</div><div class=\"muted\" id=\"compiler-assets-status\">最新の動画・画像を読み込みます。</div></div><button class=\"btn btn-secondary\" type=\"button\" id=\"compiler-assets-reload\">更新</button></div><div id=\"compiler-assets-list\" class=\"list\" style=\"margin-top:12px;\"></div>'; previewAreaEl.parentElement.appendChild(picker); picker.querySelector('#compiler-assets-reload').addEventListener('click',(event)=>loadCompilerAssets(event)); } return picker; }
    async function downloadSelectedAssets(selected){ for(const asset of selected){ const a=document.createElement('a'); a.href=asset.media_url; a.download=asset.filename || 'media'; a.target='_blank'; document.body.appendChild(a); a.click(); a.remove(); await new Promise((resolve)=>setTimeout(resolve, 120)); } }
    function renderCompilerAssets(items){ const picker=ensureCompilerPicker(); const list=picker.querySelector('#compiler-assets-list'); const status=picker.querySelector('#compiler-assets-status'); list.innerHTML=''; if(!items.length){ status.textContent='反映できる素材がまだありません。'; list.innerHTML='<div class=\"item muted\">先に Fanza コンパイラで動画や画像を作成してください。</div>'; return; } status.textContent='使いたい素材を選んで、ダウンロードするかこのツールへ反映してください。'; items.forEach((job)=>{ const card=document.createElement('div'); card.className='item'; card.innerHTML=`<div class=\"row space\"><strong>${job.timestamp||''}</strong><span class=\"pill wait\">${job.job_id}</span></div><div class=\"muted\" style=\"margin-top:8px;word-break:break-all;\">${job.fanza_url||''}</div><div class=\"list\" style=\"margin-top:10px;display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;\"></div><div class=\"row\" style=\"margin-top:12px;\"><button class=\"btn btn-secondary\" type=\"button\" data-role=\"download\">選択した素材をダウンロード</button><button class=\"btn btn-primary\" type=\"button\" data-role=\"import\">このツールに反映</button></div>`; const assetList=card.querySelector('.list'); const appendAsset=(asset)=>{ if(!asset) return; const row=document.createElement('label'); row.className='preview'; row.style.display='block'; row.style.cursor='pointer'; row.style.padding='0'; row.style.background='#fff'; const preview = asset.media_url && asset.filename && /\\.(mp4|mov|webm|avi|mkv)$/i.test(asset.filename) ? `<video src=\"${asset.media_url}\" muted playsinline preload=\"metadata\" style=\"width:100%;height:220px;object-fit:contain;display:block;background:#f7f0e5\"></video>` : `<img src=\"${asset.media_url}\" alt=\"${asset.filename}\" style=\"width:100%;height:220px;object-fit:contain;display:block;background:#f7f0e5\">`; row.innerHTML=`${preview}<div style=\"padding:10px 12px\"><div class=\"row space\"><strong style=\"font-size:13px\">${asset.label}</strong><input type=\"checkbox\"></div><div class=\"muted\" style=\"margin-top:6px;word-break:break-all;\">${asset.filename}</div></div>`; row.querySelector('input').dataset.asset=JSON.stringify(asset); assetList.appendChild(row); }; appendAsset(job.video); appendAsset(job.jacket); (job.thumbnails||[]).forEach((thumb)=>appendAsset(thumb)); const collectSelected=()=>[...card.querySelectorAll('input[type=\"checkbox\"]:checked')].map((input)=>JSON.parse(input.dataset.asset)); card.querySelector('[data-role=\"download\"]').addEventListener('click', async (event)=>{ const selected=collectSelected(); if(!selected.length){ showStatus('ダウンロードする素材を選んでください。','err'); return; } await withButton(event.currentTarget, async()=>{ showStatus('素材をダウンロードしています...','info'); await downloadSelectedAssets(selected); showStatus(`${selected.length}件の素材をダウンロードしました。`,'ok'); }); }); card.querySelector('[data-role=\"import\"]').addEventListener('click', async (event)=>{ const selected=collectSelected(); if(!selected.length){ showStatus('反映する素材を選んでください。','err'); return; } await withButton(event.currentTarget, async()=>{ showStatus('Fanzaコンパイラの素材を反映しています...','info'); const r=await fetch('/video-editor/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:selected})}); const d=await r.json(); if(!r.ok||!d.ok) throw new Error(d.error||'素材の反映に失敗しました。'); const imported=(d.items||[]).map((item, index)=>({filename:item.filename,is_video:item.is_video,url:mediaRoute(index)})); applyMediaSummary(d.items||[]); renderPreviewItems(imported); showStatus(d.message,'ok'); picker.style.display='none'; }); }); list.appendChild(card); }); }
    async function loadCompilerAssets(event){ const picker=ensureCompilerPicker(); picker.style.display='block'; const reloadButton=event?.currentTarget; if(reloadButton){ reloadButton.disabled=true; } try{ showStatus('Fanzaコンパイラの素材を確認しています...','info'); const r=await fetch('/video-editor/assets'); const d=await r.json(); if(!r.ok||!d.ok) throw new Error(d.error||'素材一覧の取得に失敗しました。'); renderCompilerAssets(d.items||[]); showStatus('Fanzaコンパイラの素材を読み込みました。','ok'); } catch(error){ showStatus(error.message,'err'); } finally { if(reloadButton){ reloadButton.disabled=false; } } }
    async function openCompilerPicker(event){ const picker=ensureCompilerPicker(); picker.style.display = picker.style.display === 'none' ? 'block' : 'none'; if(picker.style.display === 'block'){ await loadCompilerAssets(event); } }
    async function postNow(event){ if(!confirm('既存Chromeでそのまま投稿します。続けますか。')) return; await withButton(event.currentTarget, async()=>{ showStatus('今すぐ投稿しています。Chrome が再起動することがあります...','info'); await saveDraft(); const r=await fetch('/post2',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:contentEl.value})}); const d=await r.json(); if(!r.ok||!d.ok) throw new Error(d.error||'投稿に失敗しました。'); showStatus(d.message,'ok'); setTimeout(()=>location.reload(),1200); }); }
    async function createSchedule(event){ await withButton(event.currentTarget, async()=>{ showStatus('予約を保存しています...','info'); await saveDraft(); const r=await fetch('/schedule2',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:contentEl.value, scheduled_at:scheduleAtEl.value})}); const d=await r.json(); if(!r.ok||!d.ok) throw new Error(d.error||'予約投稿の保存に失敗しました。'); showStatus(d.message,'ok'); setTimeout(()=>location.reload(),800); }); }
    function removeFollowUi(){ const importBox=document.getElementById('follow-import'); if(importBox){ const card=importBox.closest('.card'); if(card){ const grid=card.parentElement; const nextCard=card.nextElementSibling; card.remove(); if(nextCard && nextCard.classList.contains('card')){ nextCard.remove(); } if(grid && !grid.children.length){ grid.remove(); } } } }
    if(mediaInputEl){ mediaInputEl.multiple = true; mediaInputEl.onchange = () => uploadMediaFiles(mediaInputEl); }
    removeFollowUi();
    ensureCompilerPicker();
    applyMediaSummary(initialMediaItems||[]);
    renderPreviewItems((initialMediaItems||[]).map((item, index)=>({filename:item.filename,is_video:item.is_video,url:mediaRoute(index)})));
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    ensure_scheduler_started()
    ensure_video_compiler_started()
    schedules = schedules_for_view(get_schedules())
    next_schedule = next_pending_schedule(schedules)
    follow = build_follow_review_view()
    return render_template_string(
        HTML,
        state=get_state(),
        logs=get_logs(),
        schedules=schedules,
        next_schedule=next_schedule,
        follow=follow,
        version=VERSION,
        storage_mode=storage_mode_label(),
        existing_profile_available=existing_profile_available(),
        video_compiler_available=video_compiler_available(),
        video_compiler_running=video_compiler_running(),
        video_compiler_url=VIDEO_COMPILER_URL,
        chrome_profile=DEFAULT_CHROME_PROFILE,
        default_schedule_value=(now_jst() + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M"),
    )


@app.route("/draft", methods=["POST"])
def save_draft():
    payload = request.get_json() or {}
    state = get_state()
    state["content"] = (payload.get("content") or "").strip()
    save_state(state)
    return jsonify({"ok": True, "message": "下書きを保存しました。"})


@app.route("/upload", methods=["POST"])
def upload_media():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "ファイルがありません。"}), 400
    upload = request.files["file"]
    if not upload.filename:
        return jsonify({"ok": False, "error": "ファイル名が空です。"}), 400
    state = get_state()
    if state.get("media_path"):
        delete_media_file(state["media_path"])
    media_path, media_filename = save_uploaded_media(upload)
    state["media_path"] = media_path
    state["media_filename"] = media_filename
    save_state(state)
    return jsonify({"ok": True, "filename": media_filename, "message": f"メディアを保存しました: {media_filename}"})


@app.route("/media")
def serve_media():
    state = get_state()
    data, content_type = load_media_bytes(state.get("media_path", ""))
    if data is None:
        return "", 404
    return Response(data, content_type=content_type)


@app.route("/media/clear", methods=["POST"])
def clear_media():
    state = get_state()
    clear_state_media(state)
    save_state(state)
    return jsonify({"ok": True, "message": "メディアを外しました。"})


@app.route("/integration/import", methods=["POST"])
def integration_import():
    payload = request.get_json(silent=True) or {}
    state = get_state()

    content = payload.get("content")
    media_path = (payload.get("media_path") or "").strip()
    media_url = (payload.get("media_url") or "").strip()
    media_filename = (payload.get("media_filename") or "").strip()
    clear_media_requested = bool(payload.get("clear_media"))

    if content is not None:
        state["content"] = str(content).strip()

    if media_path or media_url:
        try:
            import_media_to_state(state, media_path=media_path, media_url=media_url, media_filename=media_filename)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception:
            return jsonify({"ok": False, "error": "外部メディアの取り込みに失敗しました。"}), 500
    elif clear_media_requested:
        clear_state_media(state)

    save_state(state)
    return jsonify(
        {
            "ok": True,
            "message": "X投稿ツールに下書きを取り込みました。",
            "content_length": len(state.get("content", "")),
            "media_filename": state.get("media_filename", ""),
        }
    )


def video_compiler_json(path: str) -> dict[str, Any]:
    if not video_compiler_available():
        raise ValueError("動画編集ツールが見つかりません。")
    if not ensure_video_compiler_started():
        raise ValueError("動画編集ツールを起動できませんでした。")
    request_obj = urllib.request.Request(
        VIDEO_COMPILER_URL.rstrip("/") + path,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    try:
        with urllib.request.urlopen(request_obj, timeout=30) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.URLError as exc:
        raise ValueError("動画編集ツールとの通信に失敗しました。") from exc


def latest_video_compiler_assets(limit: int = 3) -> list[dict[str, Any]]:
    history_payload = video_compiler_json("/history")
    items = history_payload.get("items") if isinstance(history_payload, dict) else []
    results: list[dict[str, Any]] = []
    for raw in items or []:
        if not raw.get("available"):
            continue
        job_id = str(raw.get("job_id") or "").strip()
        if not job_id:
            continue
        try:
            video_compiler_json(f"/restore/{job_id}")
        except Exception:
            pass
        thumb_count = int(raw.get("thumb_count") or 0)
        thumbnails = []
        for index in range(1, min(thumb_count, 12) + 1):
            thumbnails.append(
                {
                    "label": f"サムネイル {index}",
                    "filename": f"{job_id}-thumb-{index:02d}.jpg",
                    "media_url": f"{VIDEO_COMPILER_URL}/thumbnail/{job_id}/{index}",
                }
            )
        results.append(
            {
                "job_id": job_id,
                "timestamp": raw.get("timestamp", ""),
                "fanza_url": raw.get("fanza_url", ""),
                "video": {
                    "label": "動画",
                    "filename": f"{job_id}.mp4",
                    "media_url": f"{VIDEO_COMPILER_URL}/output/{job_id}",
                },
                "jacket": {
                    "label": "ジャケット",
                    "filename": f"{job_id}-jacket.jpg",
                    "media_url": f"{VIDEO_COMPILER_URL}/jacket/{job_id}",
                }
                if raw.get("jacket_url")
                else None,
                "thumbnails": thumbnails,
            }
        )
        if len(results) >= limit:
            break
    return results


@app.route("/media-items/upload", methods=["POST"])
def upload_media_items():
    uploads = request.files.getlist("files")
    uploads = [upload for upload in uploads if upload and upload.filename]
    if not uploads:
        return jsonify({"ok": False, "error": "ファイルを選んでください。"}), 400
    state = get_state()
    clear_state_media(state)
    items = [save_uploaded_media_item(upload) for upload in uploads]
    validation_error = validate_x_media_items(items)
    if validation_error:
        for item in items:
            delete_media_file(item.get("path", ""))
        return jsonify({"ok": False, "error": validation_error}), 400
    set_state_media_items(state, items)
    save_state(state)
    return jsonify({"ok": True, "message": f"{len(items)}件のメディアを保存しました。", "items": items})


@app.route("/media-items/<int:index>")
def serve_media_item(index: int):
    state = get_state()
    media_items = state.get("media_items", [])
    if index < 0 or index >= len(media_items):
        return "", 404
    data, content_type = load_media_bytes(media_items[index].get("path", ""))
    if data is None:
        return "", 404
    return Response(data, content_type=content_type)


@app.route("/video-editor/assets")
def video_editor_assets():
    try:
        items = latest_video_compiler_assets()
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "items": items})


@app.route("/video-editor/import", methods=["POST"])
def video_editor_import():
    payload = request.get_json(silent=True) or {}
    media_specs = payload.get("items") or []
    if not media_specs:
        return jsonify({"ok": False, "error": "反映する素材を選んでください。"}), 400
    state = get_state()
    try:
        items = import_media_items_to_state(state, media_specs, replace=not bool(payload.get("append_media")))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    save_state(state)
    return jsonify({"ok": True, "message": f"{len(items)}件の素材を反映しました。", "items": items})


@app.route("/post2", methods=["POST"])
def post_now_v2():
    if not existing_profile_available():
        return jsonify({"ok": False, "error": "既存Chrome投稿はローカルの Windows 環境でのみ使えます。"}), 400
    payload = request.get_json() or {}
    state = get_state()
    content = (payload.get("content") or state.get("content") or "").strip()
    media_items = normalize_media_items(state.get("media_items"), state.get("media_path", ""), state.get("media_filename", ""))
    if LOCAL_MODE:
        media_items = [item for item in media_items if Path(item.get("path", "")).exists()]
    if not content and not media_items:
        return jsonify({"ok": False, "error": "投稿文か画像・動画のどちらかを入れてください。"}), 400
    state["content"] = content
    set_state_media_items(state, media_items)
    save_state(state)
    success, message = execute_post(content, media_items, media_summary(media_items), state.get("profile_handle", DEFAULT_PROFILE_HANDLE), "手動投稿")
    if success:
        return jsonify({"ok": True, "message": message})
    return jsonify({"ok": False, "error": message}), 500


@app.route("/schedule2", methods=["POST"])
def create_schedule_v2():
    if not existing_profile_available():
        return jsonify({"ok": False, "error": "予約投稿はローカルの Windows 環境でのみ使えます。"}), 400
    payload = request.get_json() or {}
    state = get_state()
    content = (payload.get("content") or state.get("content") or "").strip()
    media_items = normalize_media_items(state.get("media_items"), state.get("media_path", ""), state.get("media_filename", ""))
    if LOCAL_MODE:
        media_items = [item for item in media_items if Path(item.get("path", "")).exists()]
    if not content and not media_items:
        return jsonify({"ok": False, "error": "予約する本文か画像・動画を入れてください。"}), 400
    try:
        scheduled_at = parse_schedule_datetime(payload.get("scheduled_at", ""))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if scheduled_at <= now_jst() + timedelta(seconds=30):
        return jsonify({"ok": False, "error": "予約時刻は30秒以上先にしてください。"}), 400
    schedule_id = uuid.uuid4().hex
    snapshot_items = copy_media_items_to_schedule(schedule_id, media_items)
    state["content"] = content
    set_state_media_items(state, media_items)
    save_state(state)
    item = {
        "id": schedule_id,
        "content": content,
        "scheduled_at": scheduled_at.isoformat(),
        "created_at": now_iso(),
        "status": "pending",
        "media_path": snapshot_items[0]["path"] if snapshot_items else "",
        "media_filename": media_summary(snapshot_items),
        "media_items": snapshot_items,
        "profile_handle": state.get("profile_handle", DEFAULT_PROFILE_HANDLE),
        "last_error": "",
        "result_message": "",
        "posted_at": "",
    }
    with _schedule_lock:
        schedules = get_schedules()
        schedules.append(item)
        save_schedules(schedules)
    return jsonify({"ok": True, "message": f"{scheduled_at.strftime('%Y-%m-%d %H:%M')} に予約しました。"})


@app.route("/video-editor/start", methods=["POST"])
def start_video_editor():
    if not video_compiler_available():
        return jsonify({"ok": False, "error": "動画編集ツールが見つかりません。"}), 400
    if not ensure_video_compiler_started():
        return jsonify({"ok": False, "error": "動画編集ツールを起動できませんでした。"}), 500
    return jsonify({"ok": True, "message": "動画編集ツールを起動しました。"})


@app.route("/follow/import", methods=["POST"])
def follow_import():
    payload = request.get_json(silent=True) or {}
    candidates = (payload.get("candidates") or "").strip()
    if not candidates:
        return jsonify({"ok": False, "error": "候補テキストを入力してください。"}), 400

    added, skipped = import_follow_candidates(candidates)
    if added == 0:
        reason = skipped[0] if skipped else "追加できる候補がありませんでした。"
        return jsonify({"ok": False, "error": reason}), 400

    message = f"{added} 件の候補を追加しました。"
    if skipped:
        message += f" スキップ {len(skipped)} 件。"
    return jsonify({"ok": True, "message": message, "skipped": skipped[:5]})


@app.route("/follow/pick", methods=["POST"])
def follow_pick():
    review = get_follow_review()
    current = get_current_follow_candidate(review)
    if current is not None:
        return jsonify({"ok": True, "message": f"@{current['handle']} をレビュー中です。"})

    candidate = pick_follow_candidate(review)
    if candidate is None:
        return jsonify({"ok": False, "error": "待機中の候補がありません。"}), 404
    return jsonify({"ok": True, "message": f"@{candidate['handle']} をレビュー候補にしました。"})


@app.route("/follow/candidate/<candidate_id>/open", methods=["POST"])
def follow_open_candidate(candidate_id: str):
    review = get_follow_review()
    candidate = find_follow_candidate(review, candidate_id)
    if candidate is None:
        return jsonify({"ok": False, "error": "候補が見つかりません。"}), 404
    if candidate.get("status") in {"followed", "skipped"}:
        return jsonify({"ok": False, "error": "この候補は既にレビュー完了です。"}), 400

    candidate["status"] = "reviewing"
    candidate["opened_at"] = now_iso()
    candidate["updated_at"] = now_iso()
    review["current_candidate_id"] = candidate.get("id", "")
    add_follow_action(review, candidate, "opened")
    save_follow_review(review)
    return jsonify({"ok": True, "message": f"@{candidate['handle']} のプロフィールを開きます。"})


@app.route("/follow/candidate/<candidate_id>/followed", methods=["POST"])
def follow_mark_followed(candidate_id: str):
    review = get_follow_review()
    candidate = find_follow_candidate(review, candidate_id)
    if candidate is None:
        return jsonify({"ok": False, "error": "候補が見つかりません。"}), 404
    if candidate.get("status") in {"followed", "skipped"}:
        return jsonify({"ok": False, "error": "この候補は既にレビュー完了です。"}), 400

    rate = follow_rate_status(review)
    if not rate.get("available"):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": f"{rate['waiting_reason']} のため待機中です。次に記録できる時刻: {rate['next_available_display']}",
                }
            ),
            400,
        )

    candidate["status"] = "followed"
    candidate["reviewed_at"] = now_iso()
    candidate["updated_at"] = now_iso()
    if review.get("current_candidate_id") == candidate_id:
        review["current_candidate_id"] = ""
    add_follow_action(review, candidate, "followed")
    save_follow_review(review)
    return jsonify({"ok": True, "message": f"@{candidate['handle']} をフォロー済みに記録しました。"})


@app.route("/follow/candidate/<candidate_id>/skip", methods=["POST"])
def follow_skip_candidate(candidate_id: str):
    review = get_follow_review()
    candidate = find_follow_candidate(review, candidate_id)
    if candidate is None:
        return jsonify({"ok": False, "error": "候補が見つかりません。"}), 404
    if candidate.get("status") in {"followed", "skipped"}:
        return jsonify({"ok": False, "error": "この候補は既にレビュー完了です。"}), 400

    candidate["status"] = "skipped"
    candidate["reviewed_at"] = now_iso()
    candidate["updated_at"] = now_iso()
    if review.get("current_candidate_id") == candidate_id:
        review["current_candidate_id"] = ""
    add_follow_action(review, candidate, "skipped")
    save_follow_review(review)
    return jsonify({"ok": True, "message": f"@{candidate['handle']} をスキップしました。"})


@app.route("/open-x", methods=["POST"])
def open_x():
    if not existing_profile_available():
        return jsonify({"ok": False, "error": "既存Chrome投稿はローカルの Windows 環境でのみ使えます。"}), 400
    state = get_state()
    extra_args = ["--open-only"]
    if state.get("profile_handle"):
        extra_args.extend(["--profile-handle", state["profile_handle"]])
    success, message = run_existing_profile_command(extra_args)
    add_log(success, f"投稿画面を開く: {message}", state.get("content", ""), state.get("media_filename", ""), "画面確認")
    if success:
        return jsonify({"ok": True, "message": message})
    return jsonify({"ok": False, "error": message}), 500


@app.route("/post", methods=["POST"])
def post_now():
    if not existing_profile_available():
        return jsonify({"ok": False, "error": "既存Chrome投稿はローカルの Windows 環境でのみ使えます。"}), 400
    payload = request.get_json() or {}
    state = get_state()
    content = (payload.get("content") or state.get("content") or "").strip()
    media_path = state.get("media_path") or ""
    media_filename = state.get("media_filename") or ""
    if LOCAL_MODE and media_path and not Path(media_path).exists():
        media_path, media_filename = "", ""
    if not content and not media_path:
        return jsonify({"ok": False, "error": "投稿文か画像・動画のどちらかを入れてください。"}), 400
    state["content"] = content
    save_state(state)
    success, message = execute_post(content, media_path, media_filename, state.get("profile_handle", DEFAULT_PROFILE_HANDLE), "手動投稿")
    if success:
        return jsonify({"ok": True, "message": message})
    return jsonify({"ok": False, "error": message}), 500


@app.route("/schedule", methods=["POST"])
def create_schedule():
    if not existing_profile_available():
        return jsonify({"ok": False, "error": "予約投稿はローカルの Windows 環境でのみ使えます。"}), 400
    payload = request.get_json() or {}
    state = get_state()
    content = (payload.get("content") or state.get("content") or "").strip()
    media_path = state.get("media_path") or ""
    media_filename = state.get("media_filename") or ""
    if LOCAL_MODE and media_path and not Path(media_path).exists():
        media_path, media_filename = "", ""
    if not content and not media_path:
        return jsonify({"ok": False, "error": "予約する本文か画像・動画を入れてください。"}), 400
    try:
        scheduled_at = parse_schedule_datetime(payload.get("scheduled_at", ""))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if scheduled_at <= now_jst() + timedelta(seconds=30):
        return jsonify({"ok": False, "error": "予約日時は30秒以上先を指定してください。"}), 400
    schedule_id = uuid.uuid4().hex
    snapshot_path, snapshot_filename = copy_media_to_schedule(schedule_id, media_path, media_filename)
    state["content"] = content
    save_state(state)
    item = {
        "id": schedule_id,
        "content": content,
        "scheduled_at": scheduled_at.isoformat(),
        "created_at": now_iso(),
        "status": "pending",
        "media_path": snapshot_path,
        "media_filename": snapshot_filename,
        "profile_handle": state.get("profile_handle", DEFAULT_PROFILE_HANDLE),
        "last_error": "",
        "result_message": "",
        "posted_at": "",
    }
    with _schedule_lock:
        schedules = get_schedules()
        schedules.append(item)
        save_schedules(schedules)
    return jsonify({"ok": True, "message": f"{scheduled_at.strftime('%Y-%m-%d %H:%M')} に予約しました。"})


@app.route("/schedule/<schedule_id>/delete", methods=["POST"])
def delete_schedule(schedule_id: str):
    with _schedule_lock:
        schedules = get_schedules()
        removed = None
        kept = []
        for item in schedules:
            if item.get("id") == schedule_id and removed is None:
                removed = item
            else:
                kept.append(item)
        if removed is None:
            return jsonify({"ok": False, "error": "予約が見つかりません。"}), 404
        save_schedules(kept)
    for item in normalize_media_items(removed.get("media_items"), removed.get("media_path", ""), removed.get("media_filename", "")):
        delete_media_file(item.get("path", ""))
    return jsonify({"ok": True, "message": "予約を削除しました。"})

@app.route("/health")
def health():
    ensure_scheduler_started()
    ensure_video_compiler_started()
    return jsonify(
        {
            "status": "ok",
            "version": VERSION,
            "storage_mode": storage_mode_label(),
            "project": PROJECT_ID,
            "existing_profile_available": existing_profile_available(),
            "video_compiler_available": video_compiler_available(),
            "video_compiler_running": video_compiler_running(),
            "video_compiler_url": VIDEO_COMPILER_URL,
            "scheduler_poll_seconds": SCHEDULE_POLL_SECONDS,
        }
    )


if __name__ == "__main__":
    ensure_local_dirs()
    ensure_scheduler_started()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False)
