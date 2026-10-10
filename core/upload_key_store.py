# -*- coding: utf-8 -*-
"""分配到不同上传者的公共上传 Key；明文只在服务端可读。

与提链 CDK / 支付 CDK 管理对称：

* 列表只返回掩码与尾号，明文永不进入列表响应；
* ``verify_upload_key`` 是唯一比对明文的地方，供 ``/api/public/accounts/import``
  使用，并用 ``hmac.compare_digest`` 做常量时间比较；
* 兼容旧版 ``PUBLIC_UPLOAD_KEY`` 环境变量：仍按一个可用的隐式 Key 处理，
  管理员可在系统配置里清空，或直接改用本表的多个 Key。

所有访问都走共享数据库助手，因此数据库隔离与账号锁同样适用。
"""
from __future__ import annotations

import hmac
import secrets
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime

from core import db
from core.extract_provider_store import _bool, _id, _object, _text

# 单个 Key 的最大长度；生成值约 35 字符，留出人工粘贴的余量。
KEY_MAX_LENGTH = 512
LABEL_MAX_LENGTH = 120
NOTE_MAX_LENGTH = 2000
_FIELDS = {"label", "note", "enabled", "key"}
_KEY_PREFIX = "uk_"
_ENV_KEY = "PUBLIC_UPLOAD_KEY"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def _connection(*, write=False):
    db._ensure_sqlite()
    with db._LOCK, closing(db._sqlite_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield conn


def generate_key() -> str:
    """生成一个新的上传 Key：URL 安全、无空白字符，便于放进 HTTP 头。"""
    return _KEY_PREFIX + secrets.token_urlsafe(24)


def _key_text(value, *, optional: bool = False) -> str:
    key = _text(value, "key", KEY_MAX_LENGTH, optional=optional)
    if any(char.isspace() for char in key):
        raise ValueError("key 不能包含空白字符")
    return key


def _masked(key: str) -> str:
    if len(key) <= 8:
        return "•" * len(key)
    return f"{key[:4]}…{key[-4:]}"


def _item(row, *, secret: str | None = None) -> dict:
    key = str(row["upload_key"])
    item = {
        "id": int(row["id"]),
        "label": str(row["label"] or ""),
        "note": str(row["note"] or ""),
        "enabled": bool(row["enabled"]),
        "use_count": int(row["use_count"] or 0),
        "last_used_at": str(row["last_used_at"] or ""),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "masked": _masked(key),
        "display_suffix": key[-4:] if len(key) > 4 else "",
    }
    # 只有服务端刚生成明文时才随响应返回一次，方便管理员复制分配。
    if secret is not None:
        item["secret"] = secret
    return item


def _require(conn, key_id: int):
    row = conn.execute("SELECT * FROM upload_keys WHERE id=?", (key_id,)).fetchone()
    if row is None:
        raise LookupError("上传 Key 不存在")
    return row


def legacy_env_key() -> str:
    from config.env_loader import env_str

    return env_str(_ENV_KEY, "")


def list_keys() -> list[dict]:
    with _connection() as conn:
        return [
            _item(row)
            for row in conn.execute("SELECT * FROM upload_keys ORDER BY id DESC")
        ]


def get_key(key_id) -> dict | None:
    """仅供服务端读取的明文行，禁止直接序列化返回。"""
    key_id = _id(key_id, "key_id")
    with _connection() as conn:
        row = conn.execute("SELECT * FROM upload_keys WHERE id=?", (key_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        return item


def create_key(data: dict) -> dict:
    """新增一个 Key；不传 ``key`` 时由服务端生成并只回显一次明文。"""
    data = _object(data, _FIELDS)
    label = _text(data.get("label", ""), "label", LABEL_MAX_LENGTH, optional=True)
    note = _text(data.get("note", ""), "note", NOTE_MAX_LENGTH, optional=True)
    enabled = _bool(data.get("enabled", True), "enabled")
    generated = not str(data.get("key") or "").strip()
    key = generate_key() if generated else _key_text(data["key"])
    now = _now()
    try:
        with _connection(write=True) as conn:
            cursor = conn.execute(
                """
                INSERT INTO upload_keys
                    (label, upload_key, note, enabled, use_count, last_used_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, 0, '', ?, ?)
                """,
                (label, key, note, enabled, now, now),
            )
            row = _require(conn, cursor.lastrowid)
            return _item(row, secret=key if generated else None)
    except sqlite3.IntegrityError:
        raise ValueError("该上传 Key 已存在") from None


def update_key(key_id, data: dict) -> dict:
    """更新备注 / 状态；可选换一个新 Key（服务端不回显管理员自己填的值）。"""
    key_id = _id(key_id, "key_id")
    data = _object(data, _FIELDS)
    with _connection(write=True) as conn:
        previous = _require(conn, key_id)
        values = {
            "label": previous["label"],
            "note": previous["note"],
            "enabled": bool(previous["enabled"]),
        }
        if "label" in data:
            values["label"] = _text(data["label"], "label", LABEL_MAX_LENGTH, optional=True)
        if "note" in data:
            values["note"] = _text(data["note"], "note", NOTE_MAX_LENGTH, optional=True)
        if "enabled" in data:
            values["enabled"] = _bool(data["enabled"], "enabled")
        key = str(previous["upload_key"])
        if "key" in data and str(data["key"] or "").strip():
            key = _key_text(data["key"])
        now = _now()
        try:
            conn.execute(
                """
                UPDATE upload_keys
                SET label=?, upload_key=?, note=?, enabled=?, updated_at=?
                WHERE id=?
                """,
                (values["label"], key, values["note"], int(values["enabled"]), now, key_id),
            )
        except sqlite3.IntegrityError:
            raise ValueError("该上传 Key 已存在") from None
        return _item(_require(conn, key_id))


def rotate_key(key_id) -> dict:
    """重新生成 Key；旧 Key 立即失效。"""
    key_id = _id(key_id, "key_id")
    key = generate_key()
    with _connection(write=True) as conn:
        _require(conn, key_id)
        conn.execute(
            "UPDATE upload_keys SET upload_key=?, updated_at=? WHERE id=?",
            (key, _now(), key_id),
        )
        return _item(_require(conn, key_id), secret=key)


def delete_key(key_id) -> bool:
    key_id = _id(key_id, "key_id")
    with _connection(write=True) as conn:
        if not conn.execute("DELETE FROM upload_keys WHERE id=?", (key_id,)).rowcount:
            raise LookupError("上传 Key 不存在")
        return True


def _enabled_rows() -> list:
    with _connection() as conn:
        return list(conn.execute("SELECT * FROM upload_keys WHERE enabled=1 ORDER BY id"))


def has_active_keys() -> bool:
    """数据库里有启用的 Key，或旧版环境变量 Key 已配置。"""
    if legacy_env_key():
        return True
    return bool(_enabled_rows())


def verify_upload_key(supplied: str) -> dict | None:
    """常量时间比对一个可用的上传 Key。

    命中数据库里的 Key 时返回 ``{"id", "label"}``（``id`` 为 ``None`` 表示
    命中的是旧版环境变量 Key）；否则返回 ``None``。不区分「Key 不存在」与
    「Key 已停用」，避免泄露有效 Key 的指纹。
    """
    supplied = str(supplied or "").strip()
    if not supplied:
        return None
    candidate = supplied.encode("utf-8")
    matched: dict | None = None
    # 不提前 return：尽量让比对次数与已配置 Key 数量一致。
    for row in _enabled_rows():
        if hmac.compare_digest(candidate, str(row["upload_key"]).encode("utf-8")):
            matched = {"id": int(row["id"]), "label": str(row["label"] or "")}
    env_key = legacy_env_key()
    if env_key and hmac.compare_digest(candidate, env_key.encode("utf-8")):
        matched = {"id": None, "label": "环境变量 Key"}
    return matched


def record_use(key_id) -> None:
    """记录一次成功上传；失败不影响导入结果。"""
    if key_id is None:
        return
    try:
        key_id = _id(int(key_id), "key_id")
    except (TypeError, ValueError):
        return
    try:
        with _connection(write=True) as conn:
            conn.execute(
                """
                UPDATE upload_keys
                SET use_count=use_count+1, last_used_at=?, updated_at=?
                WHERE id=?
                """,
                (_now(), _now(), key_id),
            )
    except Exception:  # pragma: no cover - 统计失败不能影响上传
        pass
