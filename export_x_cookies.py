"""Export X/Twitter cookies from the existing Chrome profile into Selenium format."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import shutil
import sqlite3
import tempfile
from ctypes import wintypes
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_PATH = DATA_DIR / "selenium_cookies_0.json"
USER_DATA_DIR = Path(os.environ["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data"
LOCAL_STATE_PATH = USER_DATA_DIR / "Local State"
COOKIES_DB_PATH = USER_DATA_DIR / "Profile 1" / "Network" / "Cookies"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi_decrypt(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    blob_out = DATA_BLOB()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    ):
        raise ctypes.WinError()

    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def get_master_key() -> bytes:
    local_state = json.loads(LOCAL_STATE_PATH.read_text(encoding="utf-8"))
    encrypted_key = base64.b64decode(local_state["os_crypt"]["encrypted_key"])
    if encrypted_key.startswith(b"DPAPI"):
        encrypted_key = encrypted_key[5:]
    return _dpapi_decrypt(encrypted_key)


def decrypt_cookie_value(encrypted_value: bytes, master_key: bytes) -> str | None:
    if encrypted_value.startswith(b"v10") or encrypted_value.startswith(b"v11"):
        nonce = encrypted_value[3:15]
        ciphertext = encrypted_value[15:-16]
        tag = encrypted_value[-16:]
        return AESGCM(master_key).decrypt(nonce, ciphertext + tag, None).decode("utf-8")
    if encrypted_value.startswith(b"v20"):
        # Chrome 127+ App-Bound Encryption — 対応不可
        return None
    try:
        return _dpapi_decrypt(encrypted_value).decode("utf-8")
    except Exception:
        return None


def export_x_cookies() -> list[dict]:
    temp_db = Path(tempfile.gettempdir()) / "chrome_x_cookies_export.db"
    shutil.copy2(COOKIES_DB_PATH, temp_db)
    master_key = get_master_key()
    conn = sqlite3.connect(temp_db)
    conn.text_factory = bytes  # BLOBをUTF-8デコードしない
    cur = conn.cursor()
    cur.execute(
        """
        select host_key, path, is_secure, expires_utc, name, encrypted_value
        from cookies
        where host_key like '%x.com' or host_key like '%twitter.com'
        """
    )
    rows = cur.fetchall()
    conn.close()

    cookies: list[dict] = []
    for host_key, path, is_secure, expires_utc, name, encrypted_value in rows:
        if isinstance(host_key, bytes):
            host_key = host_key.decode("utf-8")
        if isinstance(path, bytes):
            path = path.decode("utf-8")
        if isinstance(name, bytes):
            name = name.decode("utf-8")
        value = decrypt_cookie_value(encrypted_value, master_key)
        if value is None:
            continue
        cookie = {
            "domain": host_key,
            "path": path,
            "secure": bool(is_secure),
            "name": name,
            "value": value,
        }
        # Chrome stores timestamps as microseconds since 1601-01-01 UTC.
        if expires_utc and expires_utc > 0:
            unix_seconds = int(expires_utc / 1_000_000 - 11644473600)
            if unix_seconds > 0:
                cookie["expiry"] = unix_seconds
        cookies.append(cookie)
    return cookies


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cookies = export_x_cookies()
    OUTPUT_PATH.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"success": True, "count": len(cookies), "output": str(OUTPUT_PATH)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
