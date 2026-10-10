# -*- coding: utf-8 -*-
"""公共上传 Key 的分配与管理路由（仅管理员）。

与提链 / 支付 CDK 管理对称：列表只返回掩码，明文只在「新增」或「换新」
时由服务端生成并回显一次。父应用负责登录鉴权中间件。
"""
from __future__ import annotations

from flask import jsonify, request

from core import upload_key_store as store


def _json_object() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("JSON 请求体必须是对象")
    return data


def _error(exc: Exception):
    if isinstance(exc, ValueError):
        status = 400
    elif isinstance(exc, LookupError):
        status = 404
    else:
        status = 500
    return jsonify({"ok": False, "error": str(exc).strip() or "请求失败"}), status


def _no_store(response):
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


def register_upload_key_routes(app):
    """上传 Key 的增删改查；明文永不进入列表响应。"""

    @app.get("/api/upload-keys")
    def upload_keys_list():
        try:
            return _no_store(jsonify({
                "ok": True,
                "items": store.list_keys(),
                "legacy_env_configured": bool(store.legacy_env_key()),
            }))
        except Exception as exc:
            return _error(exc)

    @app.post("/api/upload-keys")
    def upload_keys_create():
        try:
            item = store.create_key(_json_object())
            return jsonify({"ok": True, "item": item}), 201
        except Exception as exc:
            return _error(exc)

    @app.put("/api/upload-keys/<int:key_id>")
    def upload_keys_update(key_id: int):
        try:
            item = store.update_key(key_id, _json_object())
            return jsonify({"ok": True, "item": item})
        except Exception as exc:
            return _error(exc)

    @app.post("/api/upload-keys/<int:key_id>/rotate")
    def upload_keys_rotate(key_id: int):
        try:
            item = store.rotate_key(key_id)
            return _no_store(jsonify({"ok": True, "item": item}))
        except Exception as exc:
            return _error(exc)

    @app.delete("/api/upload-keys/<int:key_id>")
    def upload_keys_delete(key_id: int):
        try:
            store.delete_key(key_id)
            return jsonify({"ok": True})
        except Exception as exc:
            return _error(exc)

    return app
