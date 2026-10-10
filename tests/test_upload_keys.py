# -*- coding: utf-8 -*-
"""公共上传 Key 的分配、停用与旧版单 Key 兼容。"""
from __future__ import annotations

import json

import pytest

from core import db, upload_key_store as store
from webui.app import create_app

IMPORT_URL = "/api/public/accounts/import"
LINE = "multi@example.test--password--JBSWY3DPEHPK3PXP--test-token"
AUTH = {"X-Auth-Code": "admin-test-key"}


@pytest.fixture
def client(monkeypatch):
    # 默认关掉旧版单 Key，确保测试覆盖的是数据库里分配的 Key。
    monkeypatch.delenv("PUBLIC_UPLOAD_KEY", raising=False)
    monkeypatch.setattr("core.account_import.fetch_account_user_name", lambda *a, **kw: {"ok": True, "user_name": "Multi"})
    return create_app(auth_code="admin-test-key").test_client()


def test_create_generates_key_and_list_masks_plaintext(client):
    created = client.post("/api/upload-keys", json={"label": "alice", "note": "运营一"}, headers=AUTH)
    assert created.status_code == 201
    item = created.get_json()["item"]
    secret = item["secret"]
    assert secret.startswith("uk_") and len(secret) > 20
    assert item["label"] == "alice"
    assert item["enabled"] is True
    assert item["use_count"] == 0

    listing = client.get("/api/upload-keys", headers=AUTH).get_json()
    assert listing["ok"] is True
    assert listing["legacy_env_configured"] is False
    assert len(listing["items"]) == 1
    listed = listing["items"][0]
    assert "secret" not in listed
    assert secret not in json.dumps(listing)
    assert listed["masked"].endswith(secret[-4:])
    assert listed["display_suffix"] == secret[-4:]


def test_admin_routes_require_authentication(client):
    assert client.get("/api/upload-keys").status_code == 401
    assert client.post("/api/upload-keys", json={"label": "x"}).status_code == 401
    assert client.put("/api/upload-keys/1", json={"enabled": False}).status_code == 401
    assert client.delete("/api/upload-keys/1").status_code == 401


def test_admin_can_supply_label_only_key_or_custom_key(client):
    custom = client.post("/api/upload-keys", json={"label": "bob", "key": "custom-upload-key"}, headers=AUTH)
    assert custom.status_code == 201
    item = custom.get_json()["item"]
    # 管理员自己填写的 Key 不回显明文，只给掩码尾号。
    assert "secret" not in item
    assert item["display_suffix"] == "-key"

    duplicate = client.post("/api/upload-keys", json={"label": "bob2", "key": "custom-upload-key"}, headers=AUTH)
    assert duplicate.status_code == 400
    assert "已存在" in duplicate.get_json()["error"]

    blank = client.post("/api/upload-keys", json={"label": "bad", "key": "has space"}, headers=AUTH)
    assert blank.status_code == 400


def test_update_rotate_and_delete(client):
    key_id = client.post("/api/upload-keys", json={"label": "carol"}, headers=AUTH).get_json()["item"]["id"]
    first = store.verify_upload_key(store.get_key(key_id)["upload_key"])
    assert first == {"id": key_id, "label": "carol"}

    updated = client.put(f"/api/upload-keys/{key_id}", json={"label": "carol-2", "enabled": False}, headers=AUTH)
    assert updated.status_code == 200
    assert updated.get_json()["item"]["enabled"] is False
    assert store.verify_upload_key(store.get_key(key_id)["upload_key"]) is None

    rotated = client.post(f"/api/upload-keys/{key_id}/rotate", headers=AUTH).get_json()["item"]
    assert "secret" in rotated and rotated["enabled"] is False
    assert store.verify_upload_key(rotated["secret"]) is None  # 仍处于停用状态
    client.put(f"/api/upload-keys/{key_id}", json={"enabled": True}, headers=AUTH)
    assert store.verify_upload_key(rotated["secret"]) == {"id": key_id, "label": "carol-2"}

    assert client.delete(f"/api/upload-keys/{key_id}", headers=AUTH).status_code == 200
    assert client.delete(f"/api/upload-keys/{key_id}", headers=AUTH).status_code == 404
    assert store.list_keys() == []


def test_public_import_accepts_assigned_key_and_tags_notification(client, monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(
        "core.mail_notifications.notify_account_upload",
        lambda **kwargs: sent.append(kwargs),
    )
    item = client.post("/api/upload-keys", json={"label": "dave"}, headers=AUTH).get_json()["item"]

    denied = client.post(IMPORT_URL, json={"text": LINE}, headers={"X-Upload-Key": "wrong"})
    assert denied.status_code == 401
    assert db.get_account_by_email("multi@example.test") is None

    ok = client.post(IMPORT_URL, json={"text": LINE}, headers={"X-Upload-Key": item["secret"]})
    assert ok.status_code == 200 and ok.get_json()["inserted"] == 1
    assert sent and sent[-1]["source"] == "公共上传（dave）"
    assert store.get_key(item["id"])["use_count"] == 1
    assert store.get_key(item["id"])["last_used_at"]


def test_disabled_or_missing_keys_close_public_import(client):
    assert client.post(IMPORT_URL, json={"text": LINE}, headers={"X-Upload-Key": "x"}).status_code == 503

    key_id = client.post("/api/upload-keys", json={"label": "erin"}, headers=AUTH).get_json()["item"]["id"]
    secret = store.get_key(key_id)["upload_key"]
    client.put(f"/api/upload-keys/{key_id}", json={"enabled": False}, headers=AUTH)
    # 停用了唯一一个 Key 后，公开上传回到「未配置」状态，而不是提示 Key 无效。
    assert client.post(IMPORT_URL, json={"text": LINE}, headers={"X-Upload-Key": secret}).status_code == 503


def test_legacy_env_key_still_works_alongside_assigned_keys(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_UPLOAD_KEY", "legacy-key")
    assigned = client.post("/api/upload-keys", json={"label": "frank"}, headers=AUTH).get_json()["item"]

    listing = client.get("/api/upload-keys", headers=AUTH).get_json()
    assert listing["legacy_env_configured"] is True

    assert client.post(IMPORT_URL, json={"text": LINE}, headers={"X-Upload-Key": "legacy-key"}).status_code == 200
    second = LINE.replace("multi@", "second@")
    assert client.post(IMPORT_URL, json={"text": second}, headers={"X-Upload-Key": assigned["secret"]}).status_code == 200

    # 旧版 Key 不写 use_count，数据库里的 Key 正常计数。
    assert store.get_key(assigned["id"])["use_count"] == 1
