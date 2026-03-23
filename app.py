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
TEMPLATES_FILE = "templates.json"
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


def get_templates() -> list[dict]:
    return data_read(TEMPLATES_FILE) or []


def save_templates(templates: list[dict]) -> None:
    data_write(TEMPLATES_FILE, templates)


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


def execute_post(content: str, media_data: Any, media_filename: str, profile_handle: str, source: str, reply_to_url: str = "") -> tuple[bool, str]:
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
    if reply_to_url:
        extra_args.extend(["--reply-to-url", reply_to_url])

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
    :root{
      --bg:#000;--surface:#0d1117;--surface2:#161b22;--surface3:#1c2128;
      --border:#2f3336;--accent:#1d9bf0;--accent-hover:#1a8cd8;
      --text:#e7e9ea;--text2:#71767b;--text3:#536471;
      --green:#00ba7c;--red:#f4212e;--orange:#ffd400;
      --r:16px;
    }
    *{box-sizing:border-box;margin:0;padding:0}
    html{scroll-behavior:smooth}
    body{font-family:-apple-system,"Segoe UI","Yu Gothic UI",sans-serif;background:var(--bg);color:var(--text);min-height:100vh;line-height:1.5}

    /* ── Layout ── */
    .app{display:grid;grid-template-columns:240px 1fr;min-height:100vh;max-width:1280px;margin:0 auto}

    /* ── Sidebar ── */
    .sidebar{position:sticky;top:0;height:100vh;padding:12px;border-right:1px solid var(--border);display:flex;flex-direction:column;gap:4px;overflow-y:auto}
    .sidebar-logo{display:flex;align-items:center;gap:12px;padding:12px;margin-bottom:8px}
    .sidebar-logo svg{fill:var(--text);width:28px;height:28px;flex-shrink:0}
    .sidebar-logo span{font-size:20px;font-weight:700}
    .nav-item{display:flex;align-items:center;gap:16px;padding:12px 16px;border-radius:999px;cursor:pointer;font-size:16px;font-weight:400;color:var(--text);border:none;background:none;width:100%;text-align:left;transition:background .15s}
    .nav-item:hover{background:rgba(255,255,255,.08)}
    .nav-item.active{font-weight:700}
    .nav-item svg{width:22px;height:22px;flex-shrink:0}
    .sidebar-status{margin-top:auto;padding:12px;border-radius:12px;background:var(--surface);border:1px solid var(--border)}
    .status-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--text3);margin-right:6px}
    .status-dot.on{background:var(--green)}
    .status-row{display:flex;align-items:center;font-size:12px;color:var(--text2);padding:3px 0}

    /* ── Main ── */
    .main{min-height:100vh;border-right:1px solid var(--border)}
    .main-header{position:sticky;top:0;z-index:10;backdrop-filter:blur(12px);background:rgba(0,0,0,.85);padding:14px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
    .main-header h1{font-size:20px;font-weight:700}

    /* ── Tabs ── */
    .tabs{display:flex;border-bottom:1px solid var(--border)}
    .tab{flex:1;padding:14px;font-size:15px;font-weight:500;color:var(--text2);border:none;background:none;cursor:pointer;border-bottom:2px solid transparent;transition:color .15s,border-color .15s;position:relative}
    .tab:hover{background:rgba(255,255,255,.04);color:var(--text)}
    .tab.active{color:var(--text);font-weight:700;border-bottom-color:var(--accent)}
    .tab-badge{position:absolute;top:10px;right:calc(50% - 24px);background:var(--accent);color:#fff;border-radius:999px;padding:1px 6px;font-size:10px;font-weight:700}

    /* ── Tab panels ── */
    .panel{display:none;padding:0 0 60px}
    .panel.active{display:block}

    /* ── Compose box ── */
    .compose-area{padding:16px 20px;border-bottom:1px solid var(--border)}
    .compose-inner{display:flex;gap:12px}
    .avatar{width:40px;height:40px;border-radius:50%;background:linear-gradient(135deg,var(--accent),#7856ff);flex-shrink:0;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:16px;color:#fff}
    .compose-right{flex:1;min-width:0}
    .compose-textarea{width:100%;background:none;border:none;outline:none;color:var(--text);font-size:18px;font-family:inherit;resize:none;line-height:1.6;min-height:120px;padding:8px 0}
    .compose-textarea::placeholder{color:var(--text3)}
    .compose-media-preview{display:grid;gap:4px;border-radius:16px;overflow:hidden;margin-top:8px;border:1px solid var(--border)}
    .compose-media-preview.count-1{grid-template-columns:1fr}
    .compose-media-preview.count-2{grid-template-columns:1fr 1fr}
    .compose-media-preview.count-3{grid-template-columns:1fr 1fr;grid-template-rows:auto auto}
    .compose-media-preview.count-4{grid-template-columns:1fr 1fr}
    .compose-media-preview img,.compose-media-preview video{width:100%;height:200px;object-fit:cover;display:block;background:#111}
    .compose-media-preview.count-1 img,.compose-media-preview.count-1 video{height:300px}
    .compose-actions{display:flex;align-items:center;padding:10px 20px;border-bottom:1px solid var(--border);gap:4px}
    .compose-btn{width:36px;height:36px;border-radius:50%;border:none;background:none;cursor:pointer;display:flex;align-items:center;justify-content:center;color:var(--accent);transition:background .15s}
    .compose-btn:hover{background:rgba(29,155,240,.12)}
    .compose-btn:disabled{opacity:.3;cursor:not-allowed}
    .compose-btn svg{width:18px;height:18px}
    .compose-footer{display:flex;align-items:center;justify-content:space-between;padding:10px 20px;border-bottom:1px solid var(--border)}
    .char-ring{position:relative;width:30px;height:30px;flex-shrink:0}
    .char-ring svg{transform:rotate(-90deg)}
    .char-ring .ring-bg{fill:none;stroke:var(--surface3);stroke-width:3}
    .char-ring .ring-fill{fill:none;stroke:var(--accent);stroke-width:3;stroke-linecap:round;transition:stroke-dashoffset .1s,stroke .1s}
    .char-ring .ring-num{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;color:var(--text2)}
    .post-btn{background:var(--text);color:#000;border:none;border-radius:999px;padding:8px 20px;font-size:15px;font-weight:700;cursor:pointer;transition:opacity .15s;display:flex;align-items:center;gap:6px}
    .post-btn:disabled{opacity:.4;cursor:not-allowed}
    .post-btn:not(:disabled):hover{opacity:.85}
    .post-btn-group{display:flex;align-items:stretch;gap:1px}
    .post-btn-group .post-btn{border-radius:999px 0 0 999px}
    .sched-toggle-btn{border-radius:0 999px 999px 0!important;padding:8px 12px!important;border-left:1px solid rgba(0,0,0,.15)!important;}
    .open-btn{background:none;border:1px solid var(--border);color:var(--text);border-radius:999px;padding:8px 16px;font-size:14px;font-weight:600;cursor:pointer;transition:background .15s;margin-right:8px}
    .open-btn:hover{background:rgba(255,255,255,.06)}
    .open-btn:disabled{opacity:.4;cursor:not-allowed}

    /* ── Drop zone ── */
    .drop-hint{margin:12px 20px;padding:20px;border:2px dashed var(--border);border-radius:12px;text-align:center;color:var(--text2);font-size:13px;cursor:pointer;transition:border-color .15s,background .15s}
    .drop-hint:hover,.drop-hint.dragover{border-color:var(--accent);background:rgba(29,155,240,.06);color:var(--accent)}

    /* ── Autosave ── */
    .autosave{font-size:12px;color:var(--text3)}

    /* ── Schedule panel ── */
    .section{padding:20px}
    .section-title{font-size:17px;font-weight:700;margin-bottom:16px}
    .schedule-form{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:16px;margin-bottom:20px}
    .label-text{font-size:12px;font-weight:600;color:var(--text2);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px;display:block}
    input[type="datetime-local"]{width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:10px 14px;font-size:14px;color:var(--text);outline:none}
    input[type="datetime-local"]:focus{border-color:var(--accent)}
    input[type="datetime-local"]::-webkit-calendar-picker-indicator{filter:invert(1) opacity(.5)}
    .sched-btn{width:100%;margin-top:12px;background:var(--accent);color:#fff;border:none;border-radius:999px;padding:10px;font-size:15px;font-weight:700;cursor:pointer;transition:background .15s}
    .sched-btn:hover{background:var(--accent-hover)}
    .sched-btn:disabled{opacity:.4;cursor:not-allowed}

    /* ── Schedule items ── */
    .sched-item{display:flex;flex-direction:column;gap:6px;padding:14px 16px;border-bottom:1px solid var(--border);transition:background .1s}
    .sched-item:hover{background:rgba(255,255,255,.03)}
    .sched-top{display:flex;justify-content:space-between;align-items:center}
    .sched-time{font-size:14px;font-weight:600}
    .sched-content{font-size:14px;color:var(--text2);white-space:pre-wrap;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
    .sched-meta{display:flex;align-items:center;gap:8px}
    .pill{display:inline-flex;align-items:center;border-radius:999px;padding:2px 10px;font-size:12px;font-weight:600;gap:4px}
    .pill.ok{background:rgba(0,186,124,.15);color:var(--green)}
    .pill.ng{background:rgba(244,33,46,.15);color:var(--red)}
    .pill.wait{background:rgba(29,155,240,.15);color:var(--accent)}
    .pill.run{background:rgba(255,212,0,.15);color:var(--orange)}
    .del-btn{background:none;border:none;color:var(--text3);cursor:pointer;padding:4px 8px;border-radius:6px;font-size:12px;transition:color .15s,background .15s}
    .del-btn:hover{color:var(--red);background:rgba(244,33,46,.1)}
    .empty-state{padding:60px 20px;text-align:center;color:var(--text3)}
    .empty-state svg{width:40px;height:40px;opacity:.3;margin-bottom:12px}
    .empty-state p{font-size:14px}

    /* ── Log items ── */
    .log-item{padding:14px 20px;border-bottom:1px solid var(--border);transition:background .1s}
    .log-item:hover{background:rgba(255,255,255,.03)}
    .log-top{display:flex;align-items:center;gap:8px;margin-bottom:4px}
    .log-content{font-size:14px;color:var(--text2);white-space:pre-wrap;overflow:hidden;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical}
    .log-meta{font-size:12px;color:var(--text3);margin-top:4px}

    /* ── Video panel ── */
    .video-panel-head{display:flex;justify-content:space-between;align-items:center;padding:16px 20px;border-bottom:1px solid var(--border)}
    .video-actions{display:flex;gap:8px}
    .v-btn{background:var(--surface2);border:1px solid var(--border);color:var(--text);border-radius:999px;padding:6px 16px;font-size:13px;font-weight:600;cursor:pointer;transition:background .15s}
    .v-btn:hover{background:var(--surface3)}
    .v-btn:disabled{opacity:.4;cursor:not-allowed}
    .editor-frame{width:100%;height:calc(100vh - 110px);border:none;background:#111;display:block}

    /* ── Stats bar ── */
    .stats-bar{display:grid;grid-template-columns:repeat(4,1fr);border-bottom:1px solid var(--border)}
    .stat{padding:16px 20px;border-right:1px solid var(--border)}
    .stat:last-child{border-right:none}
    .stat-label{font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;font-weight:600}
    .stat-value{font-size:18px;font-weight:700;margin-top:4px;word-break:break-word}
    .stat-value#media-name{font-size:14px}

    /* ── Toast ── */
    .toast-wrap{position:fixed;bottom:24px;right:24px;display:flex;flex-direction:column;gap:8px;z-index:9999;pointer-events:none}
    .toast{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:12px 16px;font-size:14px;font-weight:500;min-width:240px;max-width:340px;box-shadow:0 8px 32px rgba(0,0,0,.6);display:flex;align-items:center;gap:10px;pointer-events:auto;animation:slideIn .2s ease}
    .toast.ok{border-left:3px solid var(--green);color:var(--text)}
    .toast.err{border-left:3px solid var(--red);color:var(--text)}
    .toast.info{border-left:3px solid var(--accent);color:var(--text)}
    @keyframes slideIn{from{transform:translateX(20px);opacity:0}to{transform:translateX(0);opacity:1}}
    @keyframes slideOut{to{transform:translateX(20px);opacity:0}}
    .toast.out{animation:slideOut .2s ease forwards}

    /* ── Spinner ── */
    @keyframes spin{to{transform:rotate(360deg)}}
    .spinner{width:16px;height:16px;border:2px solid rgba(255,255,255,.3);border-top-color:#fff;border-radius:50%;animation:spin .6s linear infinite;flex-shrink:0}
    .spinner.dark{border-color:rgba(0,0,0,.2);border-top-color:#000}

    /* ── Compiler picker ── */
    #compiler-picker{padding:16px 20px;border-bottom:1px solid var(--border);display:none}

    /* ── Thread composer ── */
    #thread-composer{padding:0}
    .thread-item{display:flex;gap:0;padding:16px 20px 0;position:relative}
    .thread-left{display:flex;flex-direction:column;align-items:center;width:48px;flex-shrink:0}
    .thread-line{width:2px;background:var(--border);flex:1;min-height:20px;margin-top:8px;border-radius:1px}
    .thread-item:last-child .thread-line{display:none}
    .thread-right{flex:1;min-width:0;padding-bottom:16px}
    .thread-item-meta{display:flex;justify-content:space-between;align-items:center;margin-top:6px}
    .thread-label{font-size:11px;color:var(--text3);font-weight:600;text-transform:uppercase;letter-spacing:.4px}
    .thread-char-count{font-size:12px;color:var(--text3)}
    .thread-char-count.warn{color:var(--orange)}
    .thread-char-count.over{color:var(--red)}
    .thread-del-btn{background:none;border:none;color:var(--text3);cursor:pointer;padding:4px;border-radius:6px;display:flex;align-items:center;justify-content:center;transition:color .15s,background .15s}
    .thread-del-btn:hover{color:var(--red);background:rgba(244,33,46,.1)}
    /* ── Templates ── */
    .tpl-item{padding:14px 20px;border-bottom:1px solid var(--border);display:flex;align-items:flex-start;gap:12px;cursor:pointer;transition:background .1s}
    .tpl-item:hover{background:rgba(29,155,240,.06)}
    .tpl-item:hover .tpl-name{color:var(--accent)}
    .tpl-body{flex:1;min-width:0}
    .tpl-name{font-size:14px;font-weight:600;margin-bottom:3px;transition:color .1s}
    .tpl-preview{font-size:13px;color:var(--text2);overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
    .tpl-actions{display:flex;gap:6px;flex-shrink:0;align-self:center}
    .tpl-use-btn{background:var(--accent);color:#fff;border:none;border-radius:999px;padding:5px 14px;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap}
    .tpl-del-btn{background:none;border:none;color:var(--text3);cursor:pointer;padding:4px 8px;border-radius:6px;font-size:12px}
    .tpl-del-btn:hover{color:var(--red);background:rgba(244,33,46,.1)}
    /* ── Reply bar ── */
    #reply-bar{display:none}
    /* ── Misc ── */
    .hint-bar{padding:12px 20px;background:rgba(255,212,0,.06);border-bottom:1px solid rgba(255,212,0,.15);color:var(--orange);font-size:13px}

    /* ── Responsive ── */
    @media(max-width:900px){
      .app{grid-template-columns:1fr}
      .sidebar{display:none}
      .stats-bar{grid-template-columns:repeat(2,1fr)}
    }
    @media(max-width:540px){
      .stats-bar{grid-template-columns:1fr}
      .stat{border-right:none}
      .compose-footer{flex-wrap:wrap;gap:8px}
    }
  </style>
</head>
<body>

<!-- Toast container -->
<div class="toast-wrap" id="toast-wrap"></div>

<div class="app">

  <!-- ── Sidebar ── -->
  <aside class="sidebar">
    <div class="sidebar-logo">
      <svg viewBox="0 0 24 24"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-4.714-6.231-5.401 6.231H2.744l7.73-8.835L1.254 2.25H8.08l4.253 5.622zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg>
    </div>
    <button class="nav-item active" onclick="switchTab('compose')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12h18M3 6h18M3 18h18"/></svg>
      投稿
    </button>
    <button class="nav-item" onclick="switchTab('schedule')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
      予約管理
    </button>
    <button class="nav-item" onclick="switchTab('video')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="23,7 16,12 23,17"/><rect x="1" y="5" width="15" height="14" rx="2"/></svg>
      動画編集
    </button>
    <button class="nav-item" onclick="switchTab('templates')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 6h16M4 10h16M4 14h8"/><rect x="14" y="12" width="8" height="8" rx="1"/></svg>
      テンプレート
    </button>
    <button class="nav-item" onclick="switchTab('log')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14,2 14,8 20,8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
      ログ
    </button>

    <div class="sidebar-status">
      <div class="status-row">
        <span class="status-dot {{ 'on' if existing_profile_available else '' }}"></span>
        {{ "Chrome 接続済" if existing_profile_available else "Chrome 未接続" }}
      </div>
      <div class="status-row">
        <span class="status-dot {{ 'on' if video_compiler_running else '' }}"></span>
        {{ "動画ツール 起動中" if video_compiler_running else "動画ツール 停止中" }}
      </div>
      <div class="status-row" style="margin-top:6px;color:var(--text3);font-size:11px;">v{{ version }} · {{ chrome_profile }}</div>
    </div>
  </aside>

  <!-- ── Main ── -->
  <main class="main">
    <div class="main-header">
      <h1 id="page-title">投稿</h1>
    </div>

    <!-- Stats bar -->
    <div class="stats-bar">
      <div class="stat"><div class="stat-label">メディア</div><div class="stat-value" id="media-name">{{ state.media_summary or "未選択" }}</div></div>
      <div class="stat"><div class="stat-label">最後の投稿</div><div class="stat-value" style="font-size:14px;">{{ state.last_post_at or "—" }}</div></div>
      <div class="stat"><div class="stat-label">次の予約</div><div class="stat-value" style="font-size:14px;">{{ next_schedule.scheduled_at_display if next_schedule else "なし" }}</div></div>
      <div class="stat"><div class="stat-label">予約数</div><div class="stat-value">{{ schedules|length }}</div></div>
    </div>

    <!-- ── Compose panel ── -->
    <div class="panel active" id="panel-compose">

      <!-- 返信先バー -->
      <div id="reply-bar" style="display:none;padding:10px 20px;background:rgba(29,155,240,.06);border-bottom:1px solid rgba(29,155,240,.2);align-items:center;gap:10px;">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><polyline points="9,17 4,12 9,7"/><path d="M20 18v-2a4 4 0 0 0-4-4H4"/></svg>
        <span style="font-size:13px;color:var(--accent);font-weight:600;">返信先URL</span>
        <input id="reply-to-url" type="url" placeholder="https://x.com/user/status/..."
          style="flex:1;background:transparent;border:none;outline:none;font-size:13px;color:var(--text);min-width:0;">
        <button onclick="clearReply()" style="background:none;border:none;color:var(--text2);cursor:pointer;font-size:18px;line-height:1;padding:0 4px;">×</button>
      </div>

      <!-- スレッドコンポーザー -->
      <div id="thread-composer">
        <!-- 本投稿 -->
        <div class="thread-item" data-index="0">
          <div class="thread-left">
            <div class="avatar">X</div>
            <div class="thread-line"></div>
          </div>
          <div class="thread-right">
            <textarea class="compose-textarea" id="content" placeholder="いまどうしてる？" rows="4">{{ state.content }}</textarea>
            <div id="preview-area"></div>
            <div class="thread-item-meta">
              <span class="thread-label">本投稿</span>
              <span class="thread-char-count" id="char-count-0">0 / 280</span>
            </div>
          </div>
        </div>
        <!-- 返信欄はJSで追加 -->
      </div>

      <div style="padding:10px 20px;border-bottom:1px solid var(--border);">
        <button onclick="addThreadItem()" style="background:none;border:1px solid var(--border);color:var(--accent);border-radius:999px;padding:7px 16px;font-size:13px;font-weight:600;cursor:pointer;display:flex;align-items:center;gap:6px;">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          返信を追加
        </button>
      </div>

      <!-- Actions toolbar -->
      <div class="compose-actions">
        <label class="compose-btn" title="画像・動画を追加">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21,15 16,10 5,21"/></svg>
          <input id="media-input" type="file" accept="image/*,video/*" multiple style="display:none">
        </label>
        <button class="compose-btn" title="動画コンパイラから選ぶ" onclick="openCompilerPicker(event)" {% if not video_compiler_available %}disabled{% endif %}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="23,7 16,12 23,17"/><rect x="1" y="5" width="15" height="14" rx="2"/></svg>
        </button>
        <button class="compose-btn" title="メディアを外す" onclick="clearMedia()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3,6 5,6 21,6"/><path d="M19,6l-1,14H6L5,6"/><path d="M10,11v6M14,11v6"/><path d="M9,6V4h6v2"/></svg>
        </button>
        <button class="compose-btn" title="返信先を設定" onclick="toggleReplyBar()" id="reply-btn">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9,17 4,12 9,7"/><path d="M20 18v-2a4 4 0 0 0-4-4H4"/></svg>
        </button>
        <div style="margin-left:auto;font-size:12px;color:var(--text3);" id="media-label">{{ state.media_summary or "" }}</div>
      </div>

      <!-- Drop zone -->
      <div class="drop-hint" id="drop-zone">
        ここにファイルをドロップ、またはクリックして選択
      </div>
      <div id="compiler-picker"></div>

      <!-- Footer: char count + post button -->
      <div class="compose-footer">
        <div style="display:flex;align-items:center;gap:10px;">
          <span class="autosave" id="autosave">下書き自動保存</span>
        </div>
        <div style="display:flex;align-items:center;gap:12px;">
          <div class="char-ring" id="char-ring" title="">
            <svg width="30" height="30" viewBox="0 0 30 30">
              <circle class="ring-bg" cx="15" cy="15" r="12"/>
              <circle class="ring-fill" id="ring-fill" cx="15" cy="15" r="12" stroke-dasharray="75.4" stroke-dashoffset="75.4"/>
            </svg>
            <div class="ring-num" id="ring-num"></div>
          </div>
          <button class="open-btn" onclick="openX(event)" {% if not existing_profile_available %}disabled{% endif %}>下書き</button>
          <div class="post-btn-group">
            <button class="post-btn" id="post-btn" onclick="postNow(event)" {% if not existing_profile_available %}disabled{% endif %}>
              投稿する
            </button>
            <button class="post-btn sched-toggle-btn" id="sched-toggle-btn" onclick="toggleSchedulePicker()" {% if not existing_profile_available %}disabled{% endif %} title="予約投稿">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6,9 12,15 18,9"/></svg>
            </button>
          </div>
        </div>
      </div>

      <!-- インライン予約ピッカー -->
      <div id="inline-schedule-picker" style="display:none;padding:14px 20px;border-bottom:1px solid var(--border);background:rgba(29,155,240,.04);">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
          <input id="inline-schedule-at" type="datetime-local" value="{{ default_schedule_value }}"
            style="background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:7px 12px;font-size:13px;color:var(--text);outline:none;flex:1;min-width:180px;">
          <button class="post-btn" style="font-size:13px;padding:7px 16px;" onclick="createScheduleInline(event)" {% if not existing_profile_available %}disabled{% endif %}>
            予約する
          </button>
          <button onclick="toggleSchedulePicker()" style="background:none;border:none;color:var(--text2);cursor:pointer;font-size:18px;line-height:1;padding:0 4px;">×</button>
        </div>
      </div>

      <div class="hint-bar">⌨️ Ctrl+Enter で投稿 — 予約投稿はこのアプリが起動中のみ実行されます。</div>
    </div>

    <!-- ── Schedule panel ── -->
    <div class="panel" id="panel-schedule">
      <div class="section">
        <div class="section-title">新しい予約</div>
        <div class="schedule-form">
          <label class="label-text" for="schedule-at">予約日時</label>
          <input id="schedule-at" type="datetime-local" value="{{ default_schedule_value }}">
          <div style="font-size:12px;color:var(--text2);margin-top:8px;">現在の本文・メディアをこの時刻に投稿します。</div>
          <button class="sched-btn" onclick="createSchedule(event)" {% if not existing_profile_available %}disabled{% endif %}>予約する</button>
        </div>

        <div class="section-title">予約一覧</div>
      </div>
      {% if schedules %}
        {% for item in schedules %}
          <div class="sched-item">
            <div class="sched-top">
              <span class="sched-time">{{ item.scheduled_at_display }}</span>
              <div style="display:flex;align-items:center;gap:8px;">
                <span class="pill {% if item.status == 'completed' %}ok{% elif item.status == 'failed' or item.status == 'canceled' %}ng{% elif item.status == 'running' %}run{% else %}wait{% endif %}">
                  {% if item.status == 'running' %}<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:currentColor;animation:spin .8s linear infinite;margin-right:2px;"></span>{% endif %}
                  {{ item.status_label }}
                </span>
                <button class="del-btn" onclick="deleteSchedule('{{ item.id }}')">削除</button>
              </div>
            </div>
            <div class="sched-content">{{ item.content_preview or "（本文なし）" }}</div>
            <div class="sched-meta">
              {% if item.media_filename %}<span style="font-size:12px;color:var(--text3);">📎 {{ item.media_filename }}</span>{% endif %}
              <span style="font-size:12px;color:var(--text3);">作成: {{ item.created_at_display }}</span>
              {% if item.result_message or item.last_error %}<span style="font-size:12px;color:var(--text3);">{{ item.result_message or item.last_error }}</span>{% endif %}
            </div>
          </div>
        {% endfor %}
      {% else %}
        <div class="empty-state">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
          <p>予約はまだありません</p>
        </div>
      {% endif %}
    </div>

    <!-- ── Video panel ── -->
    <div class="panel" id="panel-video">
      <div class="video-panel-head">
        <div>
          <div style="font-size:16px;font-weight:700;">動画編集ツール</div>
          <div style="font-size:13px;color:var(--text2);margin-top:2px;">hf-video-compiler と連携 · 「X連携」→「動画をX下書きへ送る」で自動反映</div>
        </div>
        <div class="video-actions">
          <button class="v-btn" onclick="startVideoEditor(event)" {% if not video_compiler_available %}disabled{% endif %}>起動</button>
          <button class="v-btn" onclick="openVideoEditor()">別タブ</button>
          <button class="v-btn" onclick="reloadVideoEditor()">再読込</button>
        </div>
      </div>
      {% if video_compiler_available %}
        <iframe id="video-editor-frame" class="editor-frame" src="{{ video_compiler_url }}"></iframe>
      {% else %}
        <div class="empty-state" style="padding-top:80px;">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><polygon points="23,7 16,12 23,17"/><rect x="1" y="5" width="15" height="14" rx="2"/></svg>
          <p>hf-video-compiler フォルダが見つかりません</p>
        </div>
      {% endif %}
    </div>

    <!-- ── Templates panel ── -->
    <div class="panel" id="panel-templates">
      <div class="section">
        <div class="section-title">新しいテンプレート</div>
        <div class="schedule-form">
          <label class="label-text" for="tpl-name">テンプレート名</label>
          <input id="tpl-name" type="text" placeholder="例: 定期告知文" style="width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:10px 14px;font-size:14px;color:var(--text);outline:none;margin-bottom:10px;">
          <label class="label-text" for="tpl-content">本文</label>
          <textarea id="tpl-content" placeholder="テンプレートの本文を入力..." style="width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:12px 14px;font-size:14px;color:var(--text);min-height:120px;resize:vertical;font-family:inherit;outline:none;line-height:1.6;"></textarea>
          <button class="sched-btn" onclick="saveTemplate(event)" style="margin-top:12px;">テンプレートを保存</button>
        </div>
        <div class="section-title">保存済みテンプレート</div>
      </div>
      <div id="template-list">
        <div class="empty-state">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M4 6h16M4 10h16M4 14h8"/></svg>
          <p>テンプレートはまだありません</p>
        </div>
      </div>
    </div>

    <!-- ── Log panel ── -->
    <div class="panel" id="panel-log">
      {% if logs %}
        {% for log in logs %}
          <div class="log-item">
            <div class="log-top">
              <span class="pill {{ 'ok' if log.success else 'ng' }}">{{ '✓ 成功' if log.success else '✕ 失敗' }}</span>
              <span style="font-size:13px;font-weight:600;margin-left:4px;">{{ log.time }}</span>
              <span style="font-size:12px;color:var(--text3);margin-left:6px;">{{ log.source or "手動" }}</span>
            </div>
            <div class="log-content">{{ log.content or "（本文なし）" }}</div>
            <div class="log-meta">
              {% if log.media_filename %}📎 {{ log.media_filename }} · {% endif %}{{ log.message }}
            </div>
          </div>
        {% endfor %}
      {% else %}
        <div class="empty-state">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14,2 14,8 20,8"/></svg>
          <p>まだログはありません</p>
        </div>
      {% endif %}
    </div>

    <!-- hidden follow section preserved -->
    <div style="display:none;" id="follow-review-section">
      <div id="follow-import-wrap">
        <textarea id="follow-import"></textarea>
      </div>
      <div id="follow-status" class="status" data-shared-status="1"></div>
    </div>
    <div id="status" class="status" data-shared-status="1" style="display:none;"></div>

  </main>
</div>
  <script>
    // ── Elements ──
    const contentEl = document.getElementById('content');
    const scheduleAtEl = document.getElementById('schedule-at');
    const followImportEl = document.getElementById('follow-import');
    const autosaveEl = document.getElementById('autosave');
    const mediaLabelEl = document.getElementById('media-label');
    const mediaNameEl = document.getElementById('media-name');
    const previewAreaEl = document.getElementById('preview-area');
    const mediaInputEl = document.getElementById('media-input');
    const videoEditorFrameEl = document.getElementById('video-editor-frame');
    const ringFill = document.getElementById('ring-fill');
    const ringNum = document.getElementById('ring-num');
    const dropZone = document.getElementById('drop-zone');
    const compilerPickerEl = document.getElementById('compiler-picker');
    const replyBarEl = document.getElementById('reply-bar');
    const replyUrlEl = document.getElementById('reply-to-url');
    const replyBtnEl = document.getElementById('reply-btn');
    const initialMediaItems = {{ state.media_items|tojson }};
    const MAX_CHARS = 280;
    const CIRC = 75.4;
    let saveTimer = null;

    // ── Toast ──
    function toast(message, type='info', duration=4000){
      const wrap = document.getElementById('toast-wrap');
      const el = document.createElement('div');
      el.className = `toast ${type}`;
      const icons = {ok:'✓',err:'✕',info:'ℹ'};
      el.innerHTML = `<span style="font-size:16px;">${icons[type]||'ℹ'}</span><span>${message}</span>`;
      wrap.appendChild(el);
      setTimeout(()=>{ el.classList.add('out'); setTimeout(()=>el.remove(), 200); }, duration);
    }
    function showStatus(message, type){ toast(message, type); }

    // ── Tab switching ──
    const TAB_TITLES = {compose:'投稿', schedule:'予約管理', video:'動画編集', templates:'テンプレート', log:'ログ'};
    function switchTab(name){
      document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
      document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
      const panel = document.getElementById('panel-'+name);
      if(panel) panel.classList.add('active');
      document.querySelectorAll('.nav-item').forEach(n=>{
        if(n.getAttribute('onclick')?.includes(`'${name}'`)) n.classList.add('active');
      });
      const titleEl = document.getElementById('page-title');
      if(titleEl) titleEl.textContent = TAB_TITLES[name]||name;
    }

    // ── Char counter ──
    function updateCharCount(){
      const len = contentEl.value.length;
      const ratio = Math.min(len / MAX_CHARS, 1);
      const offset = CIRC - ratio * CIRC;
      if(ringFill){ ringFill.style.strokeDashoffset = offset; }
      const remaining = MAX_CHARS - len;
      if(ringNum){
        if(len === 0){ ringNum.textContent=''; }
        else if(remaining <= 20){ ringNum.textContent = remaining; ringNum.style.color = remaining < 0 ? 'var(--red)' : 'var(--orange)'; }
        else { ringNum.textContent=''; }
      }
      if(ringFill){
        if(remaining < 0){ ringFill.style.stroke='var(--red)'; }
        else if(remaining <= 20){ ringFill.style.stroke='var(--orange)'; }
        else { ringFill.style.stroke='var(--accent)'; }
      }
    }

    // ── Draft save ──
    async function saveDraft(){
      const r = await fetch('/draft',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:contentEl.value})});
      const d = await r.json();
      if(!r.ok||!d.ok) throw new Error(d.error||'下書きの保存に失敗しました。');
      if(autosaveEl) autosaveEl.textContent = '保存済み ✓';
    }
    function queueDraftSave(){
      if(autosaveEl) autosaveEl.textContent = '保存中...';
      clearTimeout(saveTimer);
      saveTimer = setTimeout(async()=>{
        try{ await saveDraft(); } catch(e){ if(autosaveEl) autosaveEl.textContent = '保存失敗'; }
      }, 600);
    }

    // ── Button with spinner ──
    async function withButton(button, fn){
      const orig = button.innerHTML;
      button.disabled = true;
      button.innerHTML = '<span class="spinner'+(button.classList.contains('post-btn')?' dark':'')+'"></span>';
      try{ await fn(); } finally { button.disabled=false; button.innerHTML=orig; }
    }

    // ── Media ──
    function mediaRoute(i){ return `/media-items/${i}`; }
    function mediaLabelText(items){
      if(!items.length) return '';
      const names = items.map(i=>i.filename);
      if(names.length===1) return names[0];
      return names.length>3 ? `${names.slice(0,3).join(' / ')} ほか${names.length-3}件` : names.join(' / ');
    }
    function renderPreviewItems(items){
      previewAreaEl.innerHTML='';
      if(!items.length) return;
      const grid = document.createElement('div');
      grid.className = `compose-media-preview count-${Math.min(items.length,4)}`;
      items.slice(0,4).forEach((item,i)=>{
        const media = item.is_video ? document.createElement('video') : document.createElement('img');
        media.src = item.url;
        if(item.is_video){ media.controls=true; media.playsInline=true; }
        else { media.alt=item.filename||''; }
        grid.appendChild(media);
      });
      previewAreaEl.appendChild(grid);
    }
    function applyMediaSummary(items){
      const label = mediaLabelText(items);
      if(mediaLabelEl) mediaLabelEl.textContent = label;
      if(mediaNameEl) mediaNameEl.textContent = label || '未選択';
    }
    async function uploadFiles(files){
      if(!files.length) return;
      toast('メディアをアップロードしています...','info',8000);
      const form = new FormData();
      files.forEach(f=>form.append('files',f));
      try{
        const r = await fetch('/media-items/upload',{method:'POST',body:form});
        const d = await r.json();
        if(!r.ok||!d.ok) throw new Error(d.error||'アップロードに失敗しました。');
        applyMediaSummary(files.map(f=>({filename:f.name})));
        renderPreviewItems(files.map(f=>({filename:f.name,is_video:f.type.startsWith('video/'),url:URL.createObjectURL(f)})));
        toast(d.message,'ok');
      } catch(e){ toast(e.message,'err'); }
    }
    async function clearMedia(){
      try{
        const r = await fetch('/media/clear',{method:'POST'});
        const d = await r.json();
        if(!r.ok||!d.ok) throw new Error(d.error||'メディアの削除に失敗しました。');
        applyMediaSummary([]); renderPreviewItems([]);
        toast(d.message,'ok');
      } catch(e){ toast(e.message,'err'); }
    }

    // ── Drop zone ──
    if(dropZone){
      dropZone.addEventListener('click',()=>mediaInputEl?.click());
      dropZone.addEventListener('dragover',e=>{ e.preventDefault(); dropZone.classList.add('dragover'); });
      dropZone.addEventListener('dragleave',()=>dropZone.classList.remove('dragover'));
      dropZone.addEventListener('drop',e=>{ e.preventDefault(); dropZone.classList.remove('dragover'); uploadFiles([...e.dataTransfer.files]); });
    }
    if(mediaInputEl){ mediaInputEl.multiple=true; mediaInputEl.onchange=()=>uploadFiles([...mediaInputEl.files]); }

    // ── Keyboard shortcut ──
    document.addEventListener('keydown',e=>{
      if((e.ctrlKey||e.metaKey)&&e.key==='Enter'){ e.preventDefault(); document.getElementById('post-btn')?.click(); }
    });

    // ── Actions ──
    async function openX(event){
      await withButton(event.currentTarget, async()=>{
        toast('投稿画面を開いています...','info');
        await saveDraft();
        const r=await fetch('/open-x',{method:'POST'});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.error||'投稿画面を開けませんでした。');
        toast(d.message,'ok');
      });
    }
    // ── Thread composer ──
    let threadCount = 1;
    const MAX_THREAD = 25;

    function getThreadTexts(){
      return [...document.querySelectorAll('#thread-composer textarea')].map(el=>el.value);
    }

    function updateThreadCharCount(textarea){
      const idx = textarea.closest('.thread-item')?.dataset.index;
      const countEl = document.getElementById(`char-count-${idx}`);
      if(!countEl) return;
      const len = textarea.value.length;
      const rem = 280 - len;
      countEl.textContent = rem <= 20 ? `残り ${rem}` : `${len} / 280`;
      countEl.className = 'thread-char-count' + (rem < 0 ? ' over' : rem <= 20 ? ' warn' : '');
    }

    function addThreadItem(){
      if(threadCount >= MAX_THREAD){ toast(`スレッドは最大${MAX_THREAD}件です。`,'err'); return; }
      const idx = threadCount++;
      const composer = document.getElementById('thread-composer');
      const item = document.createElement('div');
      item.className = 'thread-item';
      item.dataset.index = idx;
      item.innerHTML = `
        <div class="thread-left">
          <div class="avatar" style="font-size:13px;">${idx+1}</div>
          <div class="thread-line"></div>
        </div>
        <div class="thread-right">
          <textarea class="compose-textarea" id="content-${idx}" placeholder="続きを書く..." rows="3" style="min-height:80px;"></textarea>
          <div class="thread-item-meta">
            <span class="thread-label">返信 ${idx}</span>
            <div style="display:flex;align-items:center;gap:8px;">
              <span class="thread-char-count" id="char-count-${idx}">0 / 280</span>
              <button class="thread-del-btn" onclick="removeThreadItem(this)" title="削除">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
              </button>
            </div>
          </div>
        </div>`;
      item.querySelector('textarea').addEventListener('input', e=>updateThreadCharCount(e.target));
      composer.appendChild(item);
      item.querySelector('textarea').focus();
      // 前の item の thread-line を表示
      composer.querySelectorAll('.thread-line').forEach((l,i,arr)=>{ l.style.display = i < arr.length-1 ? 'block' : 'none'; });
      // 最初のアイテムのlineを表示
      updateThreadLines();
    }

    function removeThreadItem(btn){
      const item = btn.closest('.thread-item');
      item.remove();
      threadCount--;
      updateThreadLines();
    }

    function updateThreadLines(){
      const items = document.querySelectorAll('#thread-composer .thread-item');
      items.forEach((item, i)=>{
        const line = item.querySelector('.thread-line');
        if(line) line.style.display = i < items.length-1 ? 'block' : 'none';
      });
    }

    // 本投稿のchar count
    contentEl.addEventListener('input', e=>{ updateCharCount(); updateThreadCharCount(e.target); queueDraftSave(); });

    // ── Reply ──
    function toggleReplyBar(){
      const showing = replyBarEl.style.display !== 'none' && replyBarEl.style.display !== '';
      replyBarEl.style.display = showing ? 'none' : 'flex';
      if(!showing){ replyUrlEl?.focus(); }
      if(replyBtnEl) replyBtnEl.style.color = showing ? '' : 'var(--accent)';
    }
    function clearReply(){
      if(replyUrlEl) replyUrlEl.value='';
      if(replyBarEl) replyBarEl.style.display='none';
      if(replyBtnEl) replyBtnEl.style.color='';
    }

    // ── Templates ──
    async function loadTemplates(){
      try{
        const r=await fetch('/templates');
        const d=await r.json();
        renderTemplateList(d.templates||[]);
      } catch(e){ console.error(e); }
    }
    function renderTemplateList(templates){
      const listEl=document.getElementById('template-list');
      if(!listEl) return;
      if(!templates.length){
        listEl.innerHTML='<div class="empty-state"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M4 6h16M4 10h16M4 14h8"/></svg><p>テンプレートはまだありません</p></div>';
        return;
      }
      listEl.innerHTML='';
      templates.forEach(tpl=>{
        const item=document.createElement('div');
        item.className='tpl-item';
        item.innerHTML=`<div class="tpl-body"><div class="tpl-name">${escHtml(tpl.name)}</div><div class="tpl-preview">${escHtml(tpl.content)}</div></div><div class="tpl-actions"><button class="tpl-use-btn">使う</button><button class="tpl-del-btn">削除</button></div>`;
        item.querySelector('.tpl-use-btn').onclick=e=>{ e.stopPropagation(); applyTemplate(tpl); };
        item.querySelector('.tpl-del-btn').onclick=e=>{ e.stopPropagation(); deleteTemplate(tpl.id); };
        item.onclick=()=>applyTemplate(tpl);
        listEl.appendChild(item);
      });
    }
    function escHtml(s){ const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
    function applyTemplate(tpl){
      contentEl.value=tpl.content;
      updateCharCount(); queueDraftSave();
      switchTab('compose');
      toast(`テンプレート「${tpl.name}」を適用しました。`,'ok');
    }
    async function saveTemplate(event){
      await withButton(event.currentTarget, async()=>{
        const name=(document.getElementById('tpl-name')?.value||'').trim();
        const content=(document.getElementById('tpl-content')?.value||'').trim();
        if(!name||!content) throw new Error('名前と本文を入力してください。');
        const r=await fetch('/templates',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,content})});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.error||'保存に失敗しました。');
        document.getElementById('tpl-name').value='';
        document.getElementById('tpl-content').value='';
        toast(d.message,'ok');
        await loadTemplates();
      });
    }
    async function deleteTemplate(id){
      if(!confirm('このテンプレートを削除しますか？'))return;
      const r=await fetch(`/templates/${id}/delete`,{method:'POST'});
      const d=await r.json();
      if(!r.ok||!d.ok){ toast(d.error||'削除に失敗しました。','err'); return; }
      toast(d.message,'ok');
      await loadTemplates();
    }

    async function postNow(event){
      const texts = getThreadTexts();
      const hasThread = texts.length > 1;
      const label = hasThread ? `スレッド ${texts.length} 件を投稿` : '今すぐXに投稿';
      if(!confirm(`${label}します。よろしいですか？`)) return;
      await withButton(event.currentTarget, async()=>{
        toast('投稿しています...','info',15000);
        await saveDraft();
        const replyTo = replyUrlEl?.value?.trim()||'';

        if(hasThread){
          const tweets = texts.map(text=>({text}));
          const r=await fetch('/post-thread',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tweets})});
          const d=await r.json();
          if(!r.ok||!d.ok) throw new Error(d.error||'スレッド投稿に失敗しました。');
          toast(d.message,'ok');
        } else {
          const r=await fetch('/post2',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:contentEl.value, reply_to_url:replyTo})});
          const d=await r.json();
          if(!r.ok||!d.ok) throw new Error(d.error||'投稿に失敗しました。');
          toast(d.message,'ok');
        }
        clearReply();
        setTimeout(()=>location.reload(),1500);
      });
    }
    function toggleSchedulePicker(){
      const picker=document.getElementById('inline-schedule-picker');
      if(!picker) return;
      picker.style.display = picker.style.display==='none'||!picker.style.display ? 'block' : 'none';
    }
    async function createScheduleInline(event){
      await withButton(event.currentTarget, async()=>{
        toast('予約を保存しています...','info');
        await saveDraft();
        const at=document.getElementById('inline-schedule-at')?.value||'';
        const r=await fetch('/schedule2',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:contentEl.value, scheduled_at:at})});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.error||'予約の保存に失敗しました。');
        toast(d.message,'ok');
        document.getElementById('inline-schedule-picker').style.display='none';
        setTimeout(()=>location.reload(),800);
      });
    }
    async function createSchedule(event){
      await withButton(event.currentTarget, async()=>{
        toast('予約を保存しています...','info');
        await saveDraft();
        const r=await fetch('/schedule2',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:contentEl.value, scheduled_at:scheduleAtEl.value})});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.error||'予約の保存に失敗しました。');
        toast(d.message,'ok');
        setTimeout(()=>location.reload(),800);
      });
    }
    async function deleteSchedule(id){
      if(!confirm('この予約を削除しますか？')) return;
      try{
        const r=await fetch(`/schedule/${id}/delete`,{method:'POST'});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.error||'削除に失敗しました。');
        toast(d.message,'ok');
        setTimeout(()=>location.reload(),500);
      } catch(e){ toast(e.message,'err'); }
    }
    async function startVideoEditor(event){
      await withButton(event.currentTarget, async()=>{
        toast('動画編集ツールを起動しています...','info');
        const r=await fetch('/video-editor/start',{method:'POST'});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.error||'起動できませんでした。');
        toast(d.message,'ok');
        if(videoEditorFrameEl) videoEditorFrameEl.src='{{ video_compiler_url }}?ts='+Date.now();
        setTimeout(()=>location.reload(),1000);
      });
    }
    function openVideoEditor(){ window.open('{{ video_compiler_url }}','_blank'); }
    function reloadVideoEditor(){ if(videoEditorFrameEl) videoEditorFrameEl.src='{{ video_compiler_url }}?ts='+Date.now(); }

    // ── Compiler picker ──
    function ensureCompilerPicker(){
      const p = compilerPickerEl;
      if(p && !p.dataset.ready){
        p.dataset.ready='1';
        p.innerHTML=`<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;"><span style="font-size:14px;font-weight:600;">コンパイラ素材</span><button class="v-btn" id="compiler-assets-reload" type="button">更新</button></div><div style="font-size:12px;color:var(--text2);margin-bottom:10px;" id="compiler-assets-status">読み込み中...</div><div id="compiler-assets-list" style="display:grid;gap:10px;"></div>`;
        p.querySelector('#compiler-assets-reload').addEventListener('click',e=>loadCompilerAssets(e));
      }
      return p;
    }
    async function downloadSelectedAssets(selected){
      for(const a of selected){ const el=document.createElement('a'); el.href=a.media_url; el.download=a.filename||'media'; document.body.appendChild(el); el.click(); el.remove(); await new Promise(r=>setTimeout(r,120)); }
    }
    function renderCompilerAssets(items){
      const picker=ensureCompilerPicker();
      const list=picker.querySelector('#compiler-assets-list');
      const status=picker.querySelector('#compiler-assets-status');
      list.innerHTML='';
      if(!items.length){ status.textContent='素材がまだありません。'; return; }
      status.textContent='使いたい素材を選んでください。';
      items.forEach(job=>{
        const card=document.createElement('div');
        card.style.cssText='background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:12px;';
        card.innerHTML=`<div style="display:flex;justify-content:space-between;margin-bottom:8px;"><strong style="font-size:13px;">${job.timestamp||''}</strong><span class="pill wait">${job.job_id}</span></div><div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:8px;" id="assets-${job.job_id}"></div><div style="display:flex;gap:8px;margin-top:10px;"><button class="v-btn" data-role="download">ダウンロード</button><button class="v-btn" style="background:var(--accent);color:#fff;border:none;" data-role="import">反映</button></div>`;
        const assetList=card.querySelector(`#assets-${job.job_id}`);
        const appendAsset=asset=>{ if(!asset)return; const row=document.createElement('label'); row.style.cssText='cursor:pointer;border-radius:8px;overflow:hidden;border:1px solid var(--border);display:block;'; const isVid=/\.(mp4|mov|webm|avi|mkv)$/i.test(asset.filename||''); row.innerHTML=`${isVid?`<video src="${asset.media_url}" muted preload="metadata" style="width:100%;height:120px;object-fit:cover;display:block;background:#111;"></video>`:`<img src="${asset.media_url}" style="width:100%;height:120px;object-fit:cover;display:block;background:#111;">`}<div style="padding:6px 8px;display:flex;justify-content:space-between;align-items:center;"><span style="font-size:11px;color:var(--text2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:80%;">${asset.label}</span><input type="checkbox"></div>`; row.querySelector('input').dataset.asset=JSON.stringify(asset); assetList.appendChild(row); };
        appendAsset(job.video); appendAsset(job.jacket); (job.thumbnails||[]).forEach(t=>appendAsset(t));
        const collectSelected=()=>[...card.querySelectorAll('input:checked')].map(i=>JSON.parse(i.dataset.asset));
        card.querySelector('[data-role="download"]').onclick=async e=>{ const sel=collectSelected(); if(!sel.length){toast('素材を選んでください。','err');return;} await downloadSelectedAssets(sel); toast(`${sel.length}件をダウンロードしました。`,'ok'); };
        card.querySelector('[data-role="import"]').onclick=async e=>{ const sel=collectSelected(); if(!sel.length){toast('素材を選んでください。','err');return;} toast('反映しています...','info'); const r=await fetch('/video-editor/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:sel})}); const d=await r.json(); if(!r.ok||!d.ok){toast(d.error||'失敗しました。','err');return;} const imported=(d.items||[]).map((item,i)=>({filename:item.filename,is_video:item.is_video,url:mediaRoute(i)})); applyMediaSummary(d.items||[]); renderPreviewItems(imported); toast(d.message,'ok'); if(compilerPickerEl) compilerPickerEl.style.display='none'; };
        list.appendChild(card);
      });
    }
    async function loadCompilerAssets(event){
      const picker=ensureCompilerPicker();
      if(picker) picker.style.display='block';
      const btn=event?.currentTarget;
      if(btn) btn.disabled=true;
      try{
        const r=await fetch('/video-editor/assets');
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.error||'取得に失敗しました。');
        renderCompilerAssets(d.items||[]);
      } catch(e){ toast(e.message,'err'); }
      finally { if(btn) btn.disabled=false; }
    }
    async function openCompilerPicker(event){
      const picker=ensureCompilerPicker();
      if(!picker) return;
      if(picker.style.display==='none'||!picker.style.display){ await loadCompilerAssets(event); }
      else { picker.style.display='none'; }
    }

    // ── Follow (hidden, preserved for API compat) ──
    async function importFollowCandidates(event){ await withButton(event.currentTarget, async()=>{ const candidates=(followImportEl?.value||'').trim(); if(!candidates){toast('候補を入力してください。','err');return;} const r=await fetch('/follow/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({candidates})}); const d=await r.json(); if(!r.ok||!d.ok) throw new Error(d.error||'失敗しました。'); followImportEl.value=''; toast(d.message,'ok'); setTimeout(()=>location.reload(),700); }); }
    async function pickFollowCandidate(event){ await withButton(event.currentTarget, async()=>{ const r=await fetch('/follow/pick',{method:'POST'}); const d=await r.json(); if(!r.ok||!d.ok) throw new Error(d.error||'失敗しました。'); toast(d.message,'ok'); setTimeout(()=>location.reload(),500); }); }
    async function openFollowCandidate(event,id,url){ await withButton(event.currentTarget, async()=>{ const r=await fetch(`/follow/candidate/${id}/open`,{method:'POST'}); const d=await r.json(); if(!r.ok||!d.ok) throw new Error(d.error||'失敗しました。'); window.open(url,'_blank','noopener'); toast(d.message,'ok'); setTimeout(()=>location.reload(),500); }); }
    async function markFollowed(event,id){ if(!confirm('フォロー済みとして記録しますか？'))return; await withButton(event.currentTarget, async()=>{ const r=await fetch(`/follow/candidate/${id}/followed`,{method:'POST'}); const d=await r.json(); if(!r.ok||!d.ok) throw new Error(d.error||'失敗しました。'); toast(d.message,'ok'); setTimeout(()=>location.reload(),500); }); }
    async function skipFollowCandidate(event,id){ if(!confirm('この候補をスキップしますか？'))return; await withButton(event.currentTarget, async()=>{ const r=await fetch(`/follow/candidate/${id}/skip`,{method:'POST'}); const d=await r.json(); if(!r.ok||!d.ok) throw new Error(d.error||'失敗しました。'); toast(d.message,'ok'); setTimeout(()=>location.reload(),500); }); }

    // ── Init ──
    updateCharCount();
    updateThreadLines();
    applyMediaSummary(initialMediaItems||[]);
    renderPreviewItems((initialMediaItems||[]).map((item,i)=>({filename:item.filename,is_video:item.is_video,url:mediaRoute(i)})));
    loadTemplates();
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
    reply_to_url = (payload.get("reply_to_url") or "").strip()
    media_items = normalize_media_items(state.get("media_items"), state.get("media_path", ""), state.get("media_filename", ""))
    if LOCAL_MODE:
        media_items = [item for item in media_items if Path(item.get("path", "")).exists()]
    if not content and not media_items:
        return jsonify({"ok": False, "error": "投稿文か画像・動画のどちらかを入れてください。"}), 400
    state["content"] = content
    set_state_media_items(state, media_items)
    save_state(state)
    success, message = execute_post(content, media_items, media_summary(media_items), state.get("profile_handle", DEFAULT_PROFILE_HANDLE), "手動投稿", reply_to_url=reply_to_url)
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

@app.route("/post-thread", methods=["POST"])
def post_thread():
    if not existing_profile_available():
        return jsonify({"ok": False, "error": "既存Chrome投稿はローカルの Windows 環境でのみ使えます。"}), 400
    payload = request.get_json() or {}
    tweets = payload.get("tweets", [])
    if not tweets:
        return jsonify({"ok": False, "error": "ツイートが空です。"}), 400
    if len(tweets) > 25:
        return jsonify({"ok": False, "error": "スレッドは25件以内にしてください。"}), 400

    import tempfile
    thread_data = []
    for tweet in tweets:
        text = (tweet.get("text") or "").strip()
        thread_data.append({"text": text, "media_paths": []})

    if not any(t["text"] for t in thread_data):
        return jsonify({"ok": False, "error": "本文が全て空です。"}), 400

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(thread_data, f, ensure_ascii=False)
        thread_file = f.name

    try:
        with _posting_lock:
            success, message = run_existing_profile_command(["--thread-json", thread_file])
    finally:
        try:
            Path(thread_file).unlink()
        except Exception:
            pass

    add_log(success, message, tweets[0].get("text", "")[:50] + f" (スレッド{len(tweets)}件)", "", "スレッド投稿")
    if success:
        state = get_state()
        state["last_post_at"] = now_jst().strftime("%Y-%m-%d %H:%M:%S")
        save_state(state)
        return jsonify({"ok": True, "message": message})
    return jsonify({"ok": False, "error": message}), 500


@app.route("/templates", methods=["GET"])
def list_templates():
    return jsonify({"ok": True, "templates": get_templates()})


@app.route("/templates", methods=["POST"])
def create_template():
    payload = request.get_json() or {}
    name = (payload.get("name") or "").strip()
    content = (payload.get("content") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "テンプレート名を入力してください。"}), 400
    if not content:
        return jsonify({"ok": False, "error": "テンプレート本文を入力してください。"}), 400
    templates = get_templates()
    template = {"id": uuid.uuid4().hex, "name": name, "content": content, "created_at": now_iso()}
    templates.append(template)
    save_templates(templates)
    return jsonify({"ok": True, "message": f"テンプレート「{name}」を保存しました。", "template": template})


@app.route("/templates/<template_id>/delete", methods=["POST"])
def delete_template(template_id: str):
    templates = get_templates()
    updated = [t for t in templates if t.get("id") != template_id]
    if len(updated) == len(templates):
        return jsonify({"ok": False, "error": "テンプレートが見つかりません。"}), 404
    save_templates(updated)
    return jsonify({"ok": True, "message": "テンプレートを削除しました。"})


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
    ensure_video_compiler_started()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False)


# ── Stealth: Chrome worker management endpoints ────────────────────────────

@app.route("/chrome-worker/status")
def chrome_worker_status():
    """Return the current automation Chrome worker state."""
    try:
        import chrome_worker as _cw
        return jsonify(_cw.worker_status())
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/chrome-worker/start", methods=["POST"])
def chrome_worker_start():
    """Start the background automation Chrome worker (if not already running)."""
    try:
        import chrome_worker as _cw
        profile = request.json.get("profile_directory", DEFAULT_CHROME_PROFILE) if request.is_json else DEFAULT_CHROME_PROFILE
        return jsonify(_cw.start_worker(profile))
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/chrome-worker/stop", methods=["POST"])
def chrome_worker_stop():
    """Stop the background automation Chrome worker."""
    try:
        import chrome_worker as _cw
        return jsonify(_cw.stop_worker())
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/chrome-worker/restart", methods=["POST"])
def chrome_worker_restart():
    """Restart the background automation Chrome worker."""
    try:
        import chrome_worker as _cw
        profile = request.json.get("profile_directory", DEFAULT_CHROME_PROFILE) if request.is_json else DEFAULT_CHROME_PROFILE
        _cw.stop_worker()
        return jsonify(_cw.start_worker(profile))
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500
