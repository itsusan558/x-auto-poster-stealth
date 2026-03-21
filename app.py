"""X Auto Poster web application."""

from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template_string, request

import poster

try:
    from google.cloud import scheduler_v1, storage
except ImportError:
    scheduler_v1 = None
    storage = None


app = Flask(__name__)

JST = timezone(timedelta(hours=9))
VERSION = "1.4.0"
MAX_LOGS = 200

PROJECT_ID = os.environ.get("GCP_PROJECT", "local-dev")
GCS_BUCKET = os.environ.get("GCS_BUCKET", "").strip()
SCHEDULER_JOB_NAME = os.environ.get("SCHEDULER_JOB_NAME", "").strip()
SCHEDULER_LOCATION = os.environ.get("SCHEDULER_LOCATION", "asia-northeast1")
SERVICE_URL = os.environ.get("SERVICE_URL", "").strip()
SERVICE_ACCOUNT = os.environ.get(
    "SERVICE_ACCOUNT",
    f"x-auto-poster@{PROJECT_ID}.iam.gserviceaccount.com",
)
DATA_DIR = Path(os.environ.get("APP_DATA_DIR", Path(__file__).with_name("data")))
LOCAL_MODE = os.environ.get("LOCAL_MODE", "").lower() in {"1", "true", "yes"} or not GCS_BUCKET
EXISTING_PROFILE_SCRIPT = Path(__file__).with_name("existing_profile_media_post.py")
DEFAULT_CHROME_PROFILE = os.environ.get("CHROME_PROFILE_DIRECTORY", "Default").strip() or "Default"
DEFAULT_PROFILE_HANDLE = os.environ.get("X_PROFILE_HANDLE", "").strip()

_gcs_client = None

DEFAULT_ACCOUNT = {
    "id": "default",
    "label": "アカウント 1",
    "username": "",
    "password": "",
    "content": "おはようございます。",
    "hour": 7,
    "minute": 0,
    "enabled": True,
    "last_post_date": "",
    "discord_webhook": "",
    "media_path": "",
    "media_filename": "",
}

DEFAULT_CONFIG = {"timezone": "Asia/Tokyo", "accounts": [DEFAULT_ACCOUNT.copy()]}


def ensure_local_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "media").mkdir(parents=True, exist_ok=True)


def now_jst() -> datetime:
    return datetime.now(JST)


def storage_mode_label() -> str:
    return "LOCAL" if LOCAL_MODE else "GCS"


def next_run_text(hour: int, minute: int, enabled: bool) -> str:
    if not enabled:
        return "停止中"
    current = now_jst()
    next_run = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_run <= current:
        next_run += timedelta(days=1)
    return next_run.strftime("%Y-%m-%d %H:%M JST")


def normalize_account(account: dict[str, Any], index: int) -> dict[str, Any]:
    merged = {**DEFAULT_ACCOUNT, **account}
    merged["id"] = merged.get("id") or f"acct{index}"
    merged["label"] = (merged.get("label") or f"アカウント {index + 1}").strip()
    merged["username"] = (merged.get("username") or "").strip()
    merged["content"] = merged.get("content") or ""
    merged["hour"] = max(0, min(23, int(merged.get("hour", 7))))
    merged["minute"] = max(0, min(59, int(merged.get("minute", 0))))
    merged["enabled"] = bool(merged.get("enabled", True))
    merged["discord_webhook"] = (merged.get("discord_webhook") or "").strip()
    merged["media_path"] = merged.get("media_path") or ""
    merged["media_filename"] = merged.get("media_filename") or ""
    merged["password_set"] = bool(merged.get("password"))
    merged["next_run"] = next_run_text(merged["hour"], merged["minute"], merged["enabled"])
    merged["content_length"] = len(merged["content"])
    return merged


def gcs():
    global _gcs_client
    if storage is None:
        raise RuntimeError("google-cloud-storage がインストールされていません")
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
        file_path = local_json_path(path)
        file_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    blob = gcs().bucket(GCS_BUCKET).blob(path)
    blob.upload_from_string(json.dumps(data, ensure_ascii=False, indent=2), content_type="application/json")


def media_local_path(idx: int, filename: str) -> Path:
    ext = Path(filename).suffix.lower() or ".bin"
    ensure_local_dirs()
    return DATA_DIR / "media" / f"acct{idx}_current{ext}"


def save_uploaded_media(idx: int, upload) -> tuple[str, str]:
    filename = upload.filename or "media.bin"
    if LOCAL_MODE:
        destination = media_local_path(idx, filename)
        upload.save(destination)
        return str(destination), filename
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    gcs_path = f"media/acct{idx}_current.{ext}"
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


def fetch_media_to_runtime(idx: int, stored_path: str) -> str | None:
    if not stored_path:
        return None
    if LOCAL_MODE:
        return stored_path if Path(stored_path).exists() else None
    ensure_local_dirs()
    ext = stored_path.rsplit(".", 1)[-1] if "." in stored_path else "bin"
    local_path = DATA_DIR / "media" / f"runtime_media_{idx}.{ext}"
    gcs().bucket(GCS_BUCKET).blob(stored_path).download_to_filename(str(local_path))
    return str(local_path)


def send_discord(webhook_url: str, success: bool, content: str, message: str) -> None:
    if not webhook_url:
        return
    icon = "[OK]" if success else "[NG]"
    status = "Post succeeded" if success else "Post failed"
    payload = json.dumps(
        {
            "content": (
                f"{icon} {status}\n"
                f"Content: {content}\n"
                f"Detail: {message}\n"
                f"Time: {now_jst().strftime('%Y-%m-%d %H:%M:%S')} JST"
            )
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10)


def get_config() -> dict[str, Any]:
    saved = data_read("config.json") or {}
    config = {**DEFAULT_CONFIG, **saved}
    accounts = config.get("accounts") or []
    if not accounts:
        accounts = [DEFAULT_ACCOUNT.copy()]
    config["accounts"] = [normalize_account(account, idx) for idx, account in enumerate(accounts)]
    return config


def save_config(config: dict[str, Any]) -> None:
    raw_config = {"timezone": config.get("timezone", "Asia/Tokyo"), "accounts": []}
    for account in config.get("accounts", []):
        raw_config["accounts"].append({key: account.get(key, DEFAULT_ACCOUNT.get(key)) for key in DEFAULT_ACCOUNT})
    data_write("config.json", raw_config)


def get_logs() -> list[dict[str, Any]]:
    return data_read("post_log.json") or []


def add_log(success: bool, message: str, content: str, account_label: str = "") -> None:
    logs = get_logs()
    logs.insert(
        0,
        {
            "time": now_jst().strftime("%Y-%m-%d %H:%M:%S"),
            "success": success,
            "message": message,
            "content": content,
            "account": account_label,
        },
    )
    data_write("post_log.json", logs[:MAX_LOGS])


def scheduler_job_path() -> str:
    return f"projects/{PROJECT_ID}/locations/{SCHEDULER_LOCATION}/jobs/{SCHEDULER_JOB_NAME}"


def update_scheduler(hour: int, minute: int, tz_name: str, enabled: bool):
    cron = f"{minute} {hour} * * *"
    if LOCAL_MODE:
        return cron, "ローカルモードのため Scheduler 更新はスキップしました。"
    if not SCHEDULER_JOB_NAME or not SERVICE_URL:
        return None, "SCHEDULER_JOB_NAME または SERVICE_URL が未設定です。"
    if scheduler_v1 is None:
        return None, "google-cloud-scheduler がインストールされていません。"
    try:
        client = scheduler_v1.CloudSchedulerClient()
        job = scheduler_v1.Job(
            name=scheduler_job_path(),
            schedule=cron,
            time_zone=tz_name,
            http_target=scheduler_v1.HttpTarget(
                uri=f"{SERVICE_URL}/post",
                http_method=scheduler_v1.HttpMethod.POST,
                oidc_token=scheduler_v1.OidcToken(
                    service_account_email=SERVICE_ACCOUNT,
                    audience=SERVICE_URL,
                ),
            ),
        )
        client.update_job(job=job, update_mask={"paths": ["schedule", "time_zone", "http_target"]})
        if enabled:
            client.resume_job(name=scheduler_job_path())
        else:
            client.pause_job(name=scheduler_job_path())
        return cron, None
    except Exception as exc:
        return None, str(exc)


def clear_cookie_store(idx: int) -> None:
    data_write(f"cookies_{idx}.json", [])


def exported_config() -> dict[str, Any]:
    config = get_config()
    return {
        "version": VERSION,
        "exported_at": now_jst().strftime("%Y-%m-%d %H:%M:%S JST"),
        "config": {
            "timezone": config.get("timezone", "Asia/Tokyo"),
            "accounts": [{key: account.get(key, "") for key in DEFAULT_ACCOUNT} for account in config["accounts"]],
        },
    }


def existing_profile_available() -> bool:
    return LOCAL_MODE and os.name == "nt" and EXISTING_PROFILE_SCRIPT.exists()


def derive_profile_handle(account: dict[str, Any]) -> str:
    configured = (account.get("profile_handle") or "").strip().lstrip("@")
    if configured:
        return configured
    username = (account.get("username") or "").strip().lstrip("@")
    if username:
        return username
    return DEFAULT_PROFILE_HANDLE or "meetyoursxey"


def parse_json_result(text: str) -> dict[str, Any] | None:
    for line in reversed([line.strip() for line in text.splitlines() if line.strip()]):
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except Exception:
                continue
    return None


def run_existing_profile_post(account: dict[str, Any], content: str, media_path: str | None) -> tuple[bool, str]:
    args = [
        sys.executable,
        str(EXISTING_PROFILE_SCRIPT),
        "--profile-directory",
        DEFAULT_CHROME_PROFILE,
        "--profile-handle",
        derive_profile_handle(account),
    ]
    if content:
        args.extend(["--text", content])
    if media_path:
        args.extend(["--media-path", media_path])

    completed = subprocess.run(
        args,
        cwd=str(Path(__file__).resolve().parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=240,
    )
    payload = parse_json_result(completed.stdout) or parse_json_result(completed.stderr) or {}
    message = (
        payload.get("message")
        or completed.stderr.strip()
        or completed.stdout.strip()
        or "既存プロフィール経由の投稿に失敗しました。"
    )
    success = bool(payload.get("success")) if payload else completed.returncode == 0
    return success and completed.returncode == 0, message


HTML = """<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>X Auto Poster</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}body{font-family:"Segoe UI",sans-serif;background:linear-gradient(180deg,#081120 0%,#10192e 100%);color:#e5eefc;min-height:100vh}
.shell{max-width:1080px;margin:0 auto;padding:24px 18px 40px}.card{background:rgba(15,23,42,.92);border:1px solid #23324d;border-radius:18px;box-shadow:0 16px 40px rgba(0,0,0,.24);padding:20px}
.hero{margin-bottom:18px}.top{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.badge{background:#1d9bf0;color:#fff;border-radius:999px;padding:4px 10px;font-size:12px;font-weight:700}.muted{color:#9bb0d1}
.summary,.grid,.form-grid,.side-list,.toolbar,.tabs{display:grid;gap:12px}.summary{grid-template-columns:repeat(auto-fit,minmax(150px,1fr));margin-top:18px}.grid{grid-template-columns:1.5fr 1fr}.form-grid{grid-template-columns:1fr 1fr}.form-full{grid-column:1/-1}
.metric,.side-item,.media-box{background:#0c1627;border:1px solid #20314d;border-radius:14px;padding:14px}.metric .label,.tiny,label{color:#88a0c9;font-size:12px}.metric .value{font-size:22px;font-weight:700;margin-top:6px}
.toolbar,.tabs{grid-auto-flow:column;grid-auto-columns:max-content;overflow:auto;padding-bottom:4px}.tab,.btn,button{border:none;border-radius:12px;padding:10px 14px;font-size:14px;font-weight:700;cursor:pointer}
.tab{background:transparent;color:#9eb2d7;border:1px solid #2a3b59}.tab.active{background:#1d9bf0;color:#fff;border-color:#1d9bf0}.btn-primary{background:#1d9bf0;color:#fff}.btn-secondary{background:#22314b;color:#d7e4fa}.btn-danger{background:#d84f68;color:#fff}
input[type=text],input[type=password],input[type=number],input[type=url],textarea{width:100%;background:#091221;border:1px solid #23324d;color:#e7f0ff;border-radius:12px;padding:11px 12px;font-size:14px}textarea{min-height:120px;resize:vertical}
.inline{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.status{margin-top:16px;border-radius:14px;padding:12px 14px;display:none}.ok{background:#082515;border:1px solid #1f8d4d;color:#97f4b9}.err{background:#2b1014;border:1px solid #d84f68;color:#ffb4c1}.info{background:#0a1d34;border:1px solid #2474c7;color:#a9d1ff}
.media-preview{margin-top:10px;border-radius:12px;overflow:hidden;border:1px solid #22314b;background:#050c17}.media-preview img,.media-preview video{width:100%;max-height:260px;object-fit:contain;display:block}.log-table{width:100%;border-collapse:collapse}.log-table th,.log-table td{text-align:left;padding:10px 8px;border-bottom:1px solid #1a2942;vertical-align:top;font-size:13px}
.pill{display:inline-block;border-radius:999px;padding:4px 10px;font-size:12px;font-weight:700}.pill-ok{background:#0d3620;color:#9af2ba}.pill-ng{background:#40151c;color:#ffb8c4}@media (max-width:860px){.grid,.form-grid{grid-template-columns:1fr}}
</style></head>
<body><div class="shell">
<div class="card hero"><div class="top"><h1>X 自動投稿ツール</h1><span class="badge">v{{ version }}</span><span class="badge">{{ storage_mode }}</span><span class="muted">プロジェクト: {{ project }}</span></div>
<p class="muted" style="margin-top:10px;">アカウント管理、投稿スケジュール、Webhook テスト、Cookie クリア、設定の書き出しと読み込みを1画面で行えます。</p>
<div class="summary"><div class="metric"><div class="label">アカウント数</div><div class="value">{{ config.accounts|length }}</div></div><div class="metric"><div class="label">有効数</div><div class="value">{{ enabled_count }}</div></div><div class="metric"><div class="label">ログ件数</div><div class="value">{{ logs|length }}</div></div><div class="metric"><div class="label">タイムゾーン</div><div class="value" style="font-size:18px;">{{ config.timezone }}</div></div></div>
<div class="toolbar" style="margin-top:18px;"><button class="btn btn-primary" onclick="addAccount()">アカウント追加</button><button class="btn btn-secondary" onclick="exportConfig()">設定を書き出し</button><label class="btn btn-secondary">設定を読み込み<input id="import-file" type="file" accept="application/json" style="display:none" onchange="importConfig(this)"></label></div></div>
<div class="grid"><div class="card"><h2 style="margin-bottom:14px;">アカウント設定</h2><div class="tabs">{% for acct in config.accounts %}<button class="tab {% if loop.index0 == 0 %}active{% endif %}" id="tab-{{ loop.index0 }}" onclick="switchAccount({{ loop.index0 }})">{{ acct.label }}</button>{% endfor %}</div>
{% for acct in config.accounts %}<div class="account-panel" id="panel-{{ loop.index0 }}" style="{% if loop.index0 > 0 %}display:none{% endif %};padding-top:18px;border-top:1px solid #20314d;"><div class="form-grid">
<div><label>表示名</label><input type="text" id="label-{{ loop.index0 }}" value="{{ acct.label }}"></div>
<div><label>ユーザー名</label><input type="text" id="username-{{ loop.index0 }}" value="{{ acct.username }}" placeholder="@ なしで入力"></div>
<div><label>パスワード</label><input type="password" id="password-{{ loop.index0 }}" placeholder="{% if acct.password_set %}保存済みです。変更時のみ入力してください。{% else %}パスワードを入力{% endif %}"></div>
<div><label>Discord Webhook</label><input type="url" id="discord-{{ loop.index0 }}" value="{{ acct.discord_webhook }}" placeholder="https://discord.com/api/webhooks/..."></div>
<div class="form-full"><label>投稿内容</label><textarea id="content-{{ loop.index0 }}" oninput="updateCount({{ loop.index0 }})">{{ acct.content }}</textarea><div class="inline tiny" style="margin-top:6px;"><span id="count-{{ loop.index0 }}">{{ acct.content_length }}</span><span>文字</span><span>次回実行: <strong id="next-run-{{ loop.index0 }}">{{ acct.next_run }}</strong></span><span>最終成功日: <strong>{{ acct.last_post_date or '未実行' }}</strong></span></div></div>
<div><label>時 (JST)</label><input type="number" id="hour-{{ loop.index0 }}" min="0" max="23" value="{{ acct.hour }}" oninput="updateRunPreview({{ loop.index0 }})"></div>
<div><label>分 (JST)</label><input type="number" id="minute-{{ loop.index0 }}" min="0" max="59" value="{{ acct.minute }}" oninput="updateRunPreview({{ loop.index0 }})"></div>
<div class="form-full"><label><input type="checkbox" id="enabled-{{ loop.index0 }}" {% if acct.enabled %}checked{% endif %} onchange="updateRunPreview({{ loop.index0 }})"> 投稿を有効にする</label></div>
<div class="form-full"><label>メディア</label><div class="media-box">{% if acct.media_filename %}<div class="tiny">現在のファイル: {{ acct.media_filename }}</div><div class="media-preview">{% set ext = acct.media_filename.rsplit('.', 1)[-1].lower() if '.' in acct.media_filename else '' %}{% if ext in ['mp4','mov','avi','webm','mkv'] %}<video src="/media/{{ loop.index0 }}" controls></video>{% else %}<img src="/media/{{ loop.index0 }}" alt="preview">{% endif %}</div>{% else %}<div class="tiny">アップロード済みのメディアはありません。</div>{% endif %}<div class="toolbar" style="margin-top:10px;"><label class="btn btn-secondary">メディアをアップロード<input type="file" accept="image/*,video/*" style="display:none" onchange="uploadMedia(this, {{ loop.index0 }})"></label><button class="btn btn-secondary" onclick="clearMedia({{ loop.index0 }})">メディア削除</button><button class="btn btn-secondary" onclick="clearCookies({{ loop.index0 }})">Cookie クリア</button><button class="btn btn-secondary" onclick="testWebhook({{ loop.index0 }})">Webhook テスト</button></div></div></div>
</div><div class="toolbar" style="margin-top:16px;"><button class="btn btn-primary" onclick="saveSettings({{ loop.index0 }}, event)">設定を保存</button><button class="btn btn-primary" onclick="manualPost({{ loop.index0 }}, event)">今すぐ投稿</button>{% if existing_profile_available %}<button class="btn btn-secondary" onclick="existingProfilePost({{ loop.index0 }}, event)">既存Chromeで投稿</button>{% endif %}<button class="btn btn-secondary" onclick="duplicateAccount({{ loop.index0 }})">アカウント複製</button>{% if config.accounts|length > 1 %}<button class="btn btn-danger" onclick="deleteAccount({{ loop.index0 }})">アカウント削除</button>{% endif %}</div>{% if existing_profile_available %}<div class="tiny" style="margin-top:10px;">既存Chrome投稿は Chrome を再起動します。Web アプリを Chrome で開いている場合は画面が閉じることがあります。</div>{% endif %}</div>{% endfor %}
<div id="status" class="status"></div></div>
<div class="card"><h2 style="margin-bottom:14px;">サービス情報</h2><div class="side-list"><div class="side-item"><div class="tiny">サービス URL</div><div style="margin-top:6px;word-break:break-all;">{{ service_url or '未設定' }}</div></div><div class="side-item"><div class="tiny">ストレージバケット</div><div style="margin-top:6px;word-break:break-all;">{{ gcs_bucket or 'ローカルデータフォルダ' }}</div></div><div class="side-item"><div class="tiny">スケジューラ</div><div style="margin-top:6px;">Cloud Scheduler の自動更新はアカウント 1 のみ対象です。</div></div></div>
<h2 style="margin:18px 0 14px;">投稿ログ</h2><div style="overflow:auto;"><table class="log-table"><thead><tr><th>日時</th><th>アカウント</th><th>内容</th><th>結果</th><th>詳細</th></tr></thead><tbody>{% if logs %}{% for log in logs %}<tr><td>{{ log.time }}</td><td>{{ log.account }}</td><td>{{ log.content }}</td><td>{% if log.success %}<span class="pill pill-ok">成功</span>{% else %}<span class="pill pill-ng">失敗</span>{% endif %}</td><td>{{ log.message }}</td></tr>{% endfor %}{% else %}<tr><td colspan="5" class="muted">まだログはありません。</td></tr>{% endif %}</tbody></table></div></div></div></div>
<script>
function showStatus(m,t){const e=document.getElementById('status');e.textContent=m;e.className='status '+t;e.style.display='block';window.scrollTo({top:0,behavior:'smooth'});}
function switchAccount(i){document.querySelectorAll('.account-panel').forEach((p,n)=>p.style.display=n===i?'':'none');document.querySelectorAll('.tab').forEach((t,n)=>t.classList.toggle('active',n===i));}
function collectPayload(i){return{label:document.getElementById('label-'+i).value,username:document.getElementById('username-'+i).value,password:document.getElementById('password-'+i).value,content:document.getElementById('content-'+i).value,hour:parseInt(document.getElementById('hour-'+i).value||'0',10),minute:parseInt(document.getElementById('minute-'+i).value||'0',10),enabled:document.getElementById('enabled-'+i).checked,discord_webhook:document.getElementById('discord-'+i).value};}
function updateCount(i){document.getElementById('count-'+i).textContent=document.getElementById('content-'+i).value.length;}
function updateRunPreview(i){const enabled=document.getElementById('enabled-'+i).checked;const hour=parseInt(document.getElementById('hour-'+i).value||'0',10);const minute=parseInt(document.getElementById('minute-'+i).value||'0',10);if(!enabled){document.getElementById('next-run-'+i).textContent='停止中';return;}const now=new Date();const jst=new Date(now.toLocaleString('en-US',{timeZone:'Asia/Tokyo'}));const next=new Date(jst);next.setHours(hour,minute,0,0);if(next<=jst)next.setDate(next.getDate()+1);document.getElementById('next-run-'+i).textContent=next.getFullYear()+'-'+String(next.getMonth()+1).padStart(2,'0')+'-'+String(next.getDate()).padStart(2,'0')+' '+String(next.getHours()).padStart(2,'0')+':'+String(next.getMinutes()).padStart(2,'0')+' JST';}
async function saveSettings(i,e){const b=e.currentTarget;b.disabled=true;showStatus('設定を保存しています...','info');try{const r=await fetch('/settings/'+i,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(collectPayload(i))});const d=await r.json();if(!r.ok||!d.ok)throw new Error(d.error||'保存に失敗しました');showStatus(d.message,'ok');document.getElementById('tab-'+i).textContent=document.getElementById('label-'+i).value||('アカウント '+(i+1));document.getElementById('password-'+i).value='';}catch(err){showStatus(err.message,'err');}b.disabled=false;}
async function manualPost(i,e){if(!confirm('このアカウントで今すぐ投稿しますか？'))return;const b=e.currentTarget;b.disabled=true;showStatus('投稿を実行しています。少し時間がかかることがあります...','info');try{const r=await fetch('/post/'+i+'?manual=1',{method:'POST'});const d=await r.json();if(!r.ok||!d.ok)throw new Error(d.error||'投稿に失敗しました');showStatus(d.message,'ok');setTimeout(()=>location.reload(),1200);}catch(err){showStatus(err.message,'err');}b.disabled=false;}
async function existingProfilePost(i,e){if(!confirm('既存の Chrome プロフィールで投稿します。Chrome が再起動することがあります。続けますか？'))return;const b=e.currentTarget;b.disabled=true;showStatus('既存Chrome経由で投稿を開始しています。Chrome を使っている場合は画面が切り替わることがあります...','info');try{const r=await fetch('/post-existing/'+i,{method:'POST'});const d=await r.json();if(!r.ok||!d.ok)throw new Error(d.error||'既存Chrome経由の投稿に失敗しました');showStatus(d.message,'ok');setTimeout(()=>location.reload(),1600);}catch(err){showStatus(err.message,'err');}b.disabled=false;}
async function uploadMedia(input,i){const f=input.files[0];if(!f)return;const form=new FormData();form.append('file',f);showStatus('メディアをアップロードしています...','info');try{const r=await fetch('/upload/'+i,{method:'POST',body:form});const d=await r.json();if(!r.ok||!d.ok)throw new Error(d.error||'アップロードに失敗しました');showStatus(d.message,'ok');setTimeout(()=>location.reload(),800);}catch(err){showStatus(err.message,'err');}}
async function clearMedia(i){if(!confirm('現在のメディアを削除しますか？'))return;const r=await fetch('/media/clear/'+i,{method:'POST'});const d=await r.json();if(d.ok)location.reload();else showStatus(d.error||'メディア削除に失敗しました','err');}
async function clearCookies(i){if(!confirm('このアカウントの保存済み Cookie を削除しますか？'))return;const r=await fetch('/cookies/clear/'+i,{method:'POST'});const d=await r.json();if(d.ok)showStatus(d.message,'ok');else showStatus(d.error||'Cookie の削除に失敗しました','err');}
async function testWebhook(i){showStatus('Webhook テストを送信しています...','info');const r=await fetch('/webhook/test/'+i,{method:'POST'});const d=await r.json();if(d.ok)showStatus(d.message,'ok');else showStatus(d.error||'Webhook テストに失敗しました','err');}
async function addAccount(){const r=await fetch('/accounts/add',{method:'POST'});const d=await r.json();if(d.ok)location.reload();else showStatus(d.error||'アカウント追加に失敗しました','err');}
async function duplicateAccount(i){const r=await fetch('/accounts/duplicate/'+i,{method:'POST'});const d=await r.json();if(d.ok)location.reload();else showStatus(d.error||'アカウント複製に失敗しました','err');}
async function deleteAccount(i){if(!confirm('このアカウントを削除しますか？'))return;const r=await fetch('/accounts/'+i,{method:'DELETE'});const d=await r.json();if(d.ok)location.reload();else showStatus(d.error||'アカウント削除に失敗しました','err');}
function exportConfig(){window.location.href='/export';}
async function importConfig(input){const f=input.files[0];if(!f)return;const form=new FormData();form.append('file',f);showStatus('Importing config...','info');try{const r=await fetch('/import',{method:'POST',body:form});const d=await r.json();if(!r.ok||!d.ok)throw new Error(d.error||'Import failed');showStatus(d.message,'ok');setTimeout(()=>location.reload(),900);}catch(err){showStatus(err.message,'err');}finally{input.value='';}}
</script></body></html>"""


@app.route("/")
def index():
    config = get_config()
    logs = get_logs()
    enabled_count = sum(1 for account in config["accounts"] if account.get("enabled"))
    return render_template_string(HTML, config=config, enabled_count=enabled_count, logs=logs, version=VERSION, project=PROJECT_ID, storage_mode=storage_mode_label(), service_url=SERVICE_URL, gcs_bucket=GCS_BUCKET, existing_profile_available=existing_profile_available())


@app.route("/settings/<int:idx>", methods=["POST"])
def update_settings(idx: int):
    data = request.get_json() or {}
    config = get_config()
    if idx >= len(config["accounts"]):
        return jsonify({"ok": False, "error": "アカウントが見つかりません。"}), 404
    account = config["accounts"][idx]
    account["label"] = (data.get("label") or account["label"]).strip() or account["label"]
    account["username"] = (data.get("username") or account["username"]).strip()
    if data.get("password"):
        account["password"] = data["password"]
    account["content"] = (data.get("content") or "").strip()
    account["hour"] = max(0, min(23, int(data.get("hour", account["hour"]))))
    account["minute"] = max(0, min(59, int(data.get("minute", account["minute"]))))
    account["enabled"] = bool(data.get("enabled", account["enabled"]))
    account["discord_webhook"] = (data.get("discord_webhook") or "").strip()
    account["next_run"] = next_run_text(account["hour"], account["minute"], account["enabled"])
    scheduler_note = None
    if idx == 0:
        _, scheduler_note = update_scheduler(account["hour"], account["minute"], config["timezone"], account["enabled"])
    save_config(config)
    message = f"設定を保存しました。次回実行は {account['next_run']} です。"
    if scheduler_note:
        message += f" {scheduler_note}"
    return jsonify({"ok": True, "message": message})


@app.route("/accounts/add", methods=["POST"])
def add_account():
    config = get_config()
    next_index = len(config["accounts"]) + 1
    config["accounts"].append(normalize_account({**DEFAULT_ACCOUNT, "id": f"acct{next_index}", "label": f"アカウント {next_index}"}, next_index - 1))
    save_config(config)
    return jsonify({"ok": True})


@app.route("/accounts/duplicate/<int:idx>", methods=["POST"])
def duplicate_account(idx: int):
    config = get_config()
    if idx >= len(config["accounts"]):
        return jsonify({"ok": False, "error": "アカウントが見つかりません。"}), 404
    base = {key: config["accounts"][idx].get(key, DEFAULT_ACCOUNT.get(key)) for key in DEFAULT_ACCOUNT}
    base["id"] = f"acct{len(config['accounts']) + 1}"
    base["label"] = f"{base['label']} コピー"
    base["last_post_date"] = ""
    config["accounts"].append(normalize_account(base, len(config["accounts"])))
    save_config(config)
    return jsonify({"ok": True})


@app.route("/accounts/<int:idx>", methods=["DELETE"])
def delete_account(idx: int):
    config = get_config()
    if idx >= len(config["accounts"]) or len(config["accounts"]) <= 1:
        return jsonify({"ok": False, "error": "このアカウントは削除できません。"}), 400
    account = config["accounts"].pop(idx)
    delete_media_file(account.get("media_path", ""))
    clear_cookie_store(idx)
    save_config(config)
    return jsonify({"ok": True})


@app.route("/post", methods=["POST"])
@app.route("/post/<int:idx>", methods=["POST"])
def do_post(idx: int = 0):
    config = get_config()
    if idx >= len(config["accounts"]):
        return jsonify({"ok": False, "error": "アカウントが見つかりません。"}), 404
    account = config["accounts"][idx]
    manual = request.args.get("manual") == "1"
    today = now_jst().strftime("%Y-%m-%d")
    if not account.get("enabled", True) and not manual:
        return jsonify({"ok": True, "message": "このアカウントの投稿は停止中です。"})
    if not manual and account.get("last_post_date") == today:
        return jsonify({"ok": True, "message": "今日はすでに投稿済みのためスキップしました。"})
    username = account.get("username") or os.environ.get("X_USERNAME", "")
    password = account.get("password") or os.environ.get("X_PASSWORD", "")
    if not username or not password:
        return jsonify({"ok": False, "error": "ユーザー名またはパスワードが未設定です。"}), 400
    content = (account.get("content") or "").strip()
    if not content:
        return jsonify({"ok": False, "error": "投稿内容が空です。"}), 400
    media_path = None
    stored_media_path = account.get("media_path", "")
    if stored_media_path:
        try:
            media_path = fetch_media_to_runtime(idx, stored_media_path)
        except Exception as exc:
            print(f"メディア読み込み失敗: {exc}")
    cookies = data_read(f"cookies_{idx}.json") or []
    success, message, new_cookies = poster.post_tweet(username, password, content, cookies, media_path)
    if new_cookies:
        data_write(f"cookies_{idx}.json", new_cookies)
    add_log(success, message, content, account.get("label", ""))
    if success:
        account["last_post_date"] = today
        save_config(config)
    try:
        send_discord(account.get("discord_webhook", ""), success, content, message)
    except Exception as exc:
        add_log(False, f"Webhook エラー: {exc}", content, account.get("label", ""))
    if success:
        return jsonify({"ok": True, "message": message})
    return jsonify({"ok": False, "error": message}), 500


@app.route("/post-existing/<int:idx>", methods=["POST"])
def do_post_existing(idx: int):
    if not existing_profile_available():
        return jsonify({"ok": False, "error": "既存Chrome投稿はローカルの Windows 環境でのみ使えます。"}), 400

    config = get_config()
    if idx >= len(config["accounts"]):
        return jsonify({"ok": False, "error": "アカウントが見つかりません。"}), 404

    account = config["accounts"][idx]
    content = (account.get("content") or "").strip()
    stored_media_path = account.get("media_path", "")
    media_path = stored_media_path if stored_media_path and Path(stored_media_path).exists() else None
    if not content and not media_path:
        return jsonify({"ok": False, "error": "投稿本文かメディアを用意してください。"}), 400

    success, message = run_existing_profile_post(account, content, media_path)
    add_log(success, message, content or account.get("media_filename", ""), account.get("label", ""))
    if success:
        account["last_post_date"] = now_jst().strftime("%Y-%m-%d")
        save_config(config)
    try:
        send_discord(account.get("discord_webhook", ""), success, content or account.get("media_filename", ""), message)
    except Exception as exc:
        add_log(False, f"Webhook エラー: {exc}", content or account.get("media_filename", ""), account.get("label", ""))

    if success:
        return jsonify({"ok": True, "message": message})
    return jsonify({"ok": False, "error": message}), 500


@app.route("/upload/<int:idx>", methods=["POST"])
def upload_media(idx: int):
    config = get_config()
    if idx >= len(config["accounts"]):
        return jsonify({"ok": False, "error": "アカウントが見つかりません。"}), 404
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "ファイルがありません。"}), 400
    upload = request.files["file"]
    if not upload.filename:
        return jsonify({"ok": False, "error": "ファイル名が空です。"}), 400
    current_path = config["accounts"][idx].get("media_path", "")
    if current_path:
        delete_media_file(current_path)
    stored_path, filename = save_uploaded_media(idx, upload)
    config["accounts"][idx]["media_path"] = stored_path
    config["accounts"][idx]["media_filename"] = filename
    save_config(config)
    return jsonify({"ok": True, "message": f"メディアを保存しました: {filename}"})


@app.route("/media/<int:idx>")
def serve_media(idx: int):
    config = get_config()
    if idx >= len(config["accounts"]):
        return "", 404
    data, content_type = load_media_bytes(config["accounts"][idx].get("media_path", ""))
    if data is None:
        return "", 404
    return Response(data, content_type=content_type)


@app.route("/media/clear/<int:idx>", methods=["POST"])
def clear_media(idx: int):
    config = get_config()
    if idx >= len(config["accounts"]):
        return jsonify({"ok": False, "error": "アカウントが見つかりません。"}), 404
    delete_media_file(config["accounts"][idx].get("media_path", ""))
    config["accounts"][idx]["media_path"] = ""
    config["accounts"][idx]["media_filename"] = ""
    save_config(config)
    return jsonify({"ok": True})


@app.route("/cookies/clear/<int:idx>", methods=["POST"])
def clear_cookies(idx: int):
    config = get_config()
    if idx >= len(config["accounts"]):
        return jsonify({"ok": False, "error": "アカウントが見つかりません。"}), 404
    clear_cookie_store(idx)
    return jsonify({"ok": True, "message": "保存済み Cookie を削除しました。"})


@app.route("/webhook/test/<int:idx>", methods=["POST"])
def test_webhook(idx: int):
    config = get_config()
    if idx >= len(config["accounts"]):
        return jsonify({"ok": False, "error": "アカウントが見つかりません。"}), 404
    webhook_url = config["accounts"][idx].get("discord_webhook", "")
    if not webhook_url:
        return jsonify({"ok": False, "error": "Discord Webhook が未設定です。"}), 400
    try:
        send_discord(webhook_url, True, "Webhook テスト", "X 自動投稿ツールからのテスト通知です。")
        return jsonify({"ok": True, "message": "Webhook テストを送信しました。"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/export")
def export_config():
    payload = json.dumps(exported_config(), ensure_ascii=False, indent=2)
    return Response(payload, content_type="application/json", headers={"Content-Disposition": 'attachment; filename="x-auto-poster-config.json"'})


@app.route("/import", methods=["POST"])
def import_config():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "ファイルがありません。"}), 400
    upload = request.files["file"]
    if not upload.filename:
        return jsonify({"ok": False, "error": "ファイル名が空です。"}), 400
    try:
        payload = json.loads(upload.stream.read().decode("utf-8"))
    except Exception:
        return jsonify({"ok": False, "error": "JSON ファイルが不正です。"}), 400
    config = payload.get("config", payload)
    accounts = config.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        return jsonify({"ok": False, "error": "設定には最低1つのアカウントが必要です。"}), 400
    normalized = {"timezone": config.get("timezone", "Asia/Tokyo"), "accounts": []}
    for idx, account in enumerate(accounts):
        normalized["accounts"].append(normalize_account(account, idx))
    save_config(normalized)
    return jsonify({"ok": True, "message": "設定を読み込みました。"})


@app.route("/health")
def health():
    return jsonify({"status": "ok", "version": VERSION, "storage_mode": storage_mode_label(), "project": PROJECT_ID})


if __name__ == "__main__":
    ensure_local_dirs()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False)
