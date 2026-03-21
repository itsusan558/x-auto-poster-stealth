"""Simple local X posting app that reuses the existing Chrome profile."""

from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import sys
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
VERSION = "1.6.0"
MAX_LOGS = 20
STATE_FILE = "state.json"
LOG_FILE = "post_log.json"

PROJECT_ID = os.environ.get("GCP_PROJECT", "local-dev")
GCS_BUCKET = os.environ.get("GCS_BUCKET", "").strip()
DATA_DIR = Path(os.environ.get("APP_DATA_DIR", Path(__file__).with_name("data")))
LOCAL_MODE = os.environ.get("LOCAL_MODE", "").lower() in {"1", "true", "yes"} or not GCS_BUCKET

EXISTING_PROFILE_SCRIPT = Path(__file__).with_name("existing_profile_media_post.py")
DEFAULT_CHROME_PROFILE = os.environ.get("CHROME_PROFILE_DIRECTORY", "Default").strip() or "Default"
DEFAULT_PROFILE_HANDLE = os.environ.get("X_PROFILE_HANDLE", "").strip().lstrip("@")
VIDEO_EXTENSIONS = {"mp4", "mov", "avi", "webm", "mkv"}

DEFAULT_STATE = {
    "content": "",
    "media_path": "",
    "media_filename": "",
    "last_post_at": "",
    "profile_handle": DEFAULT_PROFILE_HANDLE,
}

_gcs_client = None


def ensure_local_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "media").mkdir(parents=True, exist_ok=True)


def now_jst() -> datetime:
    return datetime.now(JST)


def storage_mode_label() -> str:
    return "LOCAL" if LOCAL_MODE else "GCS"


def gcs():
    global _gcs_client
    if storage is None:
        raise RuntimeError("google-cloud-storage is not installed.")
    if _gcs_client is None:
        _gcs_client = storage.Client()
    return _gcs_client


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
        local_json_path(path).write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return
    blob = gcs().bucket(GCS_BUCKET).blob(path)
    blob.upload_from_string(
        json.dumps(data, ensure_ascii=False, indent=2),
        content_type="application/json",
    )


def media_local_path(filename: str) -> Path:
    ensure_local_dirs()
    ext = Path(filename).suffix.lower() or ".bin"
    return DATA_DIR / "media" / f"current_media{ext}"


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


def delete_media_file(stored_path: str) -> None:
    if not stored_path:
        return
    if LOCAL_MODE:
        file_path = Path(stored_path)
        if file_path.exists():
            file_path.unlink()
        return
    try:
        gcs().bucket(GCS_BUCKET).blob(stored_path).delete()
    except Exception:
        pass


def is_video_file(filename: str) -> bool:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in VIDEO_EXTENSIONS


def legacy_state() -> dict[str, Any] | None:
    config = data_read("config.json", None)
    if not config:
        return None
    accounts = config.get("accounts") or []
    if not accounts:
        return None
    account = accounts[0]
    return {
        "content": account.get("content") or "",
        "media_path": account.get("media_path") or "",
        "media_filename": account.get("media_filename") or "",
        "last_post_at": account.get("last_post_date") or "",
        "profile_handle": (account.get("profile_handle") or DEFAULT_PROFILE_HANDLE).lstrip("@"),
    }


def get_state() -> dict[str, Any]:
    saved = data_read(STATE_FILE, None)
    if saved is None:
        saved = legacy_state() or {}
    state = {**DEFAULT_STATE, **saved}
    state["content"] = state.get("content") or ""
    state["media_path"] = state.get("media_path") or ""
    state["media_filename"] = state.get("media_filename") or ""
    state["last_post_at"] = state.get("last_post_at") or ""
    state["profile_handle"] = (state.get("profile_handle") or DEFAULT_PROFILE_HANDLE).lstrip("@")
    state["has_media"] = bool(state["media_path"] and state["media_filename"])
    state["is_video"] = is_video_file(state["media_filename"])
    return state


def save_state(state: dict[str, Any]) -> None:
    payload = {
        "content": state.get("content", ""),
        "media_path": state.get("media_path", ""),
        "media_filename": state.get("media_filename", ""),
        "last_post_at": state.get("last_post_at", ""),
        "profile_handle": (state.get("profile_handle") or DEFAULT_PROFILE_HANDLE).lstrip("@"),
    }
    data_write(STATE_FILE, payload)


def get_logs() -> list[dict[str, Any]]:
    return data_read(LOG_FILE, []) or []


def add_log(success: bool, message: str, content: str, media_filename: str) -> None:
    logs = get_logs()
    logs.insert(
        0,
        {
            "time": now_jst().strftime("%Y-%m-%d %H:%M:%S"),
            "success": success,
            "message": message,
            "content": content,
            "media_filename": media_filename,
        },
    )
    data_write(LOG_FILE, logs[:MAX_LOGS])


def existing_profile_available() -> bool:
    return LOCAL_MODE and os.name == "nt" and EXISTING_PROFILE_SCRIPT.exists()


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
    message = (
        payload.get("message")
        or completed.stderr.strip()
        or completed.stdout.strip()
        or "既存Chrome経由の操作に失敗しました。"
    )
    success = bool(payload.get("success")) if payload else completed.returncode == 0
    return success and completed.returncode == 0, message


HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>X 自動投稿</title>
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", "Yu Gothic UI", sans-serif;
      background: linear-gradient(180deg, #f7f3ec 0%, #efe7db 100%);
      color: #1f2a33;
      min-height: 100vh;
    }
    .shell {
      max-width: 860px;
      margin: 0 auto;
      padding: 28px 18px 40px;
    }
    .card {
      background: rgba(255, 252, 247, 0.96);
      border: 1px solid #dccdb7;
      border-radius: 22px;
      box-shadow: 0 20px 40px rgba(109, 87, 55, 0.12);
      padding: 20px;
    }
    .card + .card { margin-top: 16px; }
    .top {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }
    h1, h2, h3, p { margin: 0; }
    h1 { font-size: 30px; }
    h2 { font-size: 18px; }
    .subtext {
      margin-top: 10px;
      color: #5f6f7d;
      line-height: 1.7;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 5px 10px;
      background: #ddebf8;
      color: #22598a;
      font-size: 12px;
      font-weight: 700;
    }
    .summary {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
      margin-top: 18px;
    }
    .metric {
      background: #f8f2e8;
      border: 1px solid #e2d2bb;
      border-radius: 16px;
      padding: 14px;
    }
    .label {
      font-size: 12px;
      color: #7b6f61;
    }
    .value {
      margin-top: 6px;
      font-weight: 700;
      font-size: 18px;
      word-break: break-word;
    }
    textarea {
      width: 100%;
      min-height: 220px;
      resize: vertical;
      background: #fffdf9;
      border: 1px solid #d9c6a8;
      border-radius: 18px;
      padding: 16px;
      font-size: 15px;
      line-height: 1.7;
      color: #1f2a33;
    }
    textarea:focus {
      outline: 2px solid #85b3dd;
      border-color: #85b3dd;
    }
    .row {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }
    .row.space {
      justify-content: space-between;
    }
    .meta-line {
      margin-top: 10px;
      display: flex;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
      color: #7b6f61;
      font-size: 13px;
    }
    .status {
      display: none;
      margin-top: 14px;
      padding: 12px 14px;
      border-radius: 16px;
      line-height: 1.6;
    }
    .status.ok {
      display: block;
      background: #e3f6e7;
      border: 1px solid #9fd0a8;
      color: #24643a;
    }
    .status.err {
      display: block;
      background: #fbe6e7;
      border: 1px solid #e3a2a7;
      color: #8a2f3b;
    }
    .status.info {
      display: block;
      background: #e7f0fa;
      border: 1px solid #a9c6e6;
      color: #285982;
    }
    .media-box {
      margin-top: 16px;
      padding: 16px;
      background: #fbf7f0;
      border: 1px solid #e2d2bb;
      border-radius: 18px;
    }
    .preview {
      margin-top: 12px;
      border-radius: 16px;
      overflow: hidden;
      border: 1px solid #e0cfb4;
      background: #fff;
    }
    .preview img,
    .preview video {
      width: 100%;
      max-height: 360px;
      object-fit: contain;
      display: block;
      background: #fff;
    }
    .btn {
      border: none;
      border-radius: 14px;
      padding: 12px 16px;
      font-size: 14px;
      font-weight: 700;
      cursor: pointer;
      transition: transform .12s ease, opacity .12s ease;
    }
    .btn:hover { transform: translateY(-1px); }
    .btn:disabled { opacity: .55; cursor: not-allowed; transform: none; }
    .btn-primary { background: #2f6ea8; color: #fff; }
    .btn-secondary { background: #d7e5f2; color: #244c72; }
    .btn-soft { background: #efe2cf; color: #6b4d27; }
    .hint {
      margin-top: 14px;
      padding: 14px;
      background: #fff7ea;
      border: 1px solid #edd7af;
      border-radius: 16px;
      color: #715a2f;
      line-height: 1.7;
    }
    .log-list {
      display: grid;
      gap: 10px;
      margin-top: 12px;
    }
    .log-item {
      background: #faf5ed;
      border: 1px solid #e1d3c0;
      border-radius: 16px;
      padding: 14px;
    }
    .pill {
      display: inline-block;
      border-radius: 999px;
      padding: 4px 9px;
      font-size: 12px;
      font-weight: 700;
    }
    .pill.ok {
      background: #e3f6e7;
      color: #24643a;
    }
    .pill.ng {
      background: #fbe6e7;
      color: #8a2f3b;
    }
    .muted {
      color: #7b6f61;
      font-size: 13px;
    }
    @media (max-width: 640px) {
      .row,
      .row.space {
        flex-direction: column;
        align-items: stretch;
      }
      .btn {
        width: 100%;
      }
      h1 {
        font-size: 25px;
      }
    }
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
      <p class="subtext">
        ユーザー名やパスワードは使わず、既存の Chrome プロフィールで X を開いてそのまま投稿します。
        画像と動画も添付できます。
      </p>
      <div class="summary">
        <div class="metric">
          <div class="label">現在のメディア</div>
          <div class="value" id="media-name">{{ state.media_filename or "未選択" }}</div>
        </div>
        <div class="metric">
          <div class="label">最後の投稿</div>
          <div class="value">{{ state.last_post_at or "まだありません" }}</div>
        </div>
        <div class="metric">
          <div class="label">使用プロフィール</div>
          <div class="value">{{ chrome_profile }}</div>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>投稿内容</h2>
      <p class="subtext">本文を書いて、必要なら画像か動画を添付して、そのまま投稿します。</p>
      <textarea id="content" placeholder="ここに投稿文を書いてください。">{{ state.content }}</textarea>
      <div class="meta-line">
        <div id="autosave">下書きは自動保存されます。</div>
        <div><span id="char-count">0</span> 文字</div>
      </div>

      <div class="media-box">
        <div class="row space">
          <div>
            <div class="label">画像・動画</div>
            <div class="value" style="font-size:16px;" id="media-label">{{ state.media_filename or "なし" }}</div>
          </div>
          <div class="row">
            <label class="btn btn-soft">
              画像・動画を選ぶ
              <input id="media-input" type="file" accept="image/*,video/*" style="display:none" onchange="uploadMedia(this)">
            </label>
            <button class="btn btn-secondary" onclick="clearMedia()">メディアを外す</button>
          </div>
        </div>
        <div id="preview-area">
          {% if state.has_media %}
            <div class="preview">
              {% if state.is_video %}
                <video src="/media" controls></video>
              {% else %}
                <img src="/media" alt="preview">
              {% endif %}
            </div>
          {% endif %}
        </div>
      </div>

      <div class="row" style="margin-top:16px;">
        <button class="btn btn-secondary" id="open-x-btn" onclick="openX(event)" {% if not existing_profile_available %}disabled{% endif %}>既存Chromeで投稿画面を開く</button>
        <button class="btn btn-primary" id="post-btn" onclick="postNow(event)" {% if not existing_profile_available %}disabled{% endif %}>既存Chromeで投稿する</button>
      </div>

      <div class="hint">
        この操作では Chrome を再起動することがあります。Web アプリ自体は Edge など別ブラウザで開いておくと安定します。
      </div>

      <div id="status" class="status"></div>
    </div>

    <div class="card">
      <h2>最近のログ</h2>
      <div class="log-list">
        {% if logs %}
          {% for log in logs %}
            <div class="log-item">
              <div class="row space">
                <strong>{{ log.time }}</strong>
                <span class="pill {{ 'ok' if log.success else 'ng' }}">{{ '成功' if log.success else '失敗' }}</span>
              </div>
              <div class="muted" style="margin-top:8px;">本文</div>
              <div style="white-space:pre-wrap; margin-top:4px;">{{ log.content or 'なし' }}</div>
              <div class="muted" style="margin-top:8px;">メディア</div>
              <div style="margin-top:4px;">{{ log.media_filename or 'なし' }}</div>
              <div class="muted" style="margin-top:8px;">結果</div>
              <div style="white-space:pre-wrap; margin-top:4px;">{{ log.message }}</div>
            </div>
          {% endfor %}
        {% else %}
          <div class="log-item muted">まだログはありません。</div>
        {% endif %}
      </div>
    </div>
  </div>

  <script>
    const contentEl = document.getElementById('content');
    const autosaveEl = document.getElementById('autosave');
    const charCountEl = document.getElementById('char-count');
    const mediaLabelEl = document.getElementById('media-label');
    const mediaNameEl = document.getElementById('media-name');
    const previewAreaEl = document.getElementById('preview-area');
    let saveTimer = null;

    function updateCharCount() {
      charCountEl.textContent = contentEl.value.length;
    }

    function showStatus(message, type) {
      const el = document.getElementById('status');
      el.textContent = message;
      el.className = 'status ' + type;
    }

    async function saveDraft(showSaved = false) {
      const response = await fetch('/draft', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({content: contentEl.value})
      });
      const data = await response.json();
      if (!response.ok || !data.ok) {
        throw new Error(data.error || '下書きの保存に失敗しました。');
      }
      autosaveEl.textContent = showSaved ? '下書きを保存しました。' : '下書きは自動保存されます。';
      return data;
    }

    function queueDraftSave() {
      autosaveEl.textContent = '下書きを保存中...';
      clearTimeout(saveTimer);
      saveTimer = setTimeout(async () => {
        try {
          await saveDraft(false);
        } catch (error) {
          autosaveEl.textContent = error.message;
        }
      }, 500);
    }

    function renderPreview(url, isVideo) {
      previewAreaEl.innerHTML = '';
      if (!url) return;
      const wrapper = document.createElement('div');
      wrapper.className = 'preview';
      if (isVideo) {
        const video = document.createElement('video');
        video.src = url;
        video.controls = true;
        wrapper.appendChild(video);
      } else {
        const img = document.createElement('img');
        img.src = url;
        img.alt = 'preview';
        wrapper.appendChild(img);
      }
      previewAreaEl.appendChild(wrapper);
    }

    async function uploadMedia(input) {
      const file = input.files[0];
      if (!file) return;
      showStatus('メディアをアップロードしています...', 'info');
      const form = new FormData();
      form.append('file', file);
      try {
        const response = await fetch('/upload', { method: 'POST', body: form });
        const data = await response.json();
        if (!response.ok || !data.ok) {
          throw new Error(data.error || 'メディアのアップロードに失敗しました。');
        }
        mediaLabelEl.textContent = data.filename;
        mediaNameEl.textContent = data.filename;
        renderPreview(URL.createObjectURL(file), file.type.startsWith('video/'));
        showStatus(data.message, 'ok');
      } catch (error) {
        showStatus(error.message, 'err');
      } finally {
        input.value = '';
      }
    }

    async function clearMedia() {
      try {
        const response = await fetch('/media/clear', { method: 'POST' });
        const data = await response.json();
        if (!response.ok || !data.ok) {
          throw new Error(data.error || 'メディアの削除に失敗しました。');
        }
        mediaLabelEl.textContent = 'なし';
        mediaNameEl.textContent = '未選択';
        renderPreview('', false);
        showStatus(data.message, 'ok');
      } catch (error) {
        showStatus(error.message, 'err');
      }
    }

    async function withButton(button, action) {
      button.disabled = true;
      try {
        await action();
      } finally {
        button.disabled = false;
      }
    }

    async function openX(event) {
      await withButton(event.currentTarget, async () => {
        showStatus('既存Chromeで投稿画面を開いています...', 'info');
        await saveDraft(false);
        const response = await fetch('/open-x', { method: 'POST' });
        const data = await response.json();
        if (!response.ok || !data.ok) {
          throw new Error(data.error || '投稿画面を開けませんでした。');
        }
        showStatus(data.message, 'ok');
      });
    }

    async function postNow(event) {
      if (!confirm('既存Chromeでそのまま投稿します。続けますか。')) return;
      await withButton(event.currentTarget, async () => {
        showStatus('既存Chromeで投稿しています。Chrome が再起動することがあります...', 'info');
        await saveDraft(false);
        const response = await fetch('/post', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({content: contentEl.value})
        });
        const data = await response.json();
        if (!response.ok || !data.ok) {
          throw new Error(data.error || '投稿に失敗しました。');
        }
        showStatus(data.message, 'ok');
        setTimeout(() => location.reload(), 1200);
      });
    }

    contentEl.addEventListener('input', () => {
      updateCharCount();
      queueDraftSave();
    });
    updateCharCount();
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(
        HTML,
        state=get_state(),
        logs=get_logs(),
        version=VERSION,
        storage_mode=storage_mode_label(),
        existing_profile_available=existing_profile_available(),
        chrome_profile=DEFAULT_CHROME_PROFILE,
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
    return jsonify(
        {
            "ok": True,
            "filename": media_filename,
            "message": f"メディアを保存しました: {media_filename}",
        }
    )


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
    if state.get("media_path"):
        delete_media_file(state["media_path"])
    state["media_path"] = ""
    state["media_filename"] = ""
    save_state(state)
    return jsonify({"ok": True, "message": "メディアを外しました。"})


@app.route("/open-x", methods=["POST"])
def open_x():
    if not existing_profile_available():
        return jsonify({"ok": False, "error": "既存Chrome投稿はローカルの Windows 環境でのみ使えます。"}), 400
    state = get_state()
    extra_args = ["--open-only"]
    if state.get("profile_handle"):
        extra_args.extend(["--profile-handle", state["profile_handle"]])
    success, message = run_existing_profile_command(extra_args)
    add_log(success, f"投稿画面を開く: {message}", state.get("content", ""), state.get("media_filename", ""))
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
    if LOCAL_MODE and media_path and not Path(media_path).exists():
        media_path = ""

    if not content and not media_path:
        return jsonify({"ok": False, "error": "投稿文か画像・動画のどちらかを入れてください。"}), 400

    state["content"] = content
    save_state(state)

    extra_args: list[str] = []
    if content:
        extra_args.extend(["--text", content])
    if media_path:
        extra_args.extend(["--media-path", media_path])
    if state.get("profile_handle"):
        extra_args.extend(["--profile-handle", state["profile_handle"]])

    success, message = run_existing_profile_command(extra_args)
    add_log(success, message, content, state.get("media_filename", ""))
    if success:
        state["last_post_at"] = now_jst().strftime("%Y-%m-%d %H:%M:%S")
        save_state(state)
        return jsonify({"ok": True, "message": message})
    return jsonify({"ok": False, "error": message}), 500


@app.route("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "version": VERSION,
            "storage_mode": storage_mode_label(),
            "project": PROJECT_ID,
            "existing_profile_available": existing_profile_available(),
        }
    )


if __name__ == "__main__":
    ensure_local_dirs()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False)
