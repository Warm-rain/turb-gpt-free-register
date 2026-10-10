# -*- coding: utf-8 -*-
"""多途径优先级链：解析、校验与注册 / Codex 两层回退。

查活的回退行为见 tests/test_live_check_driver_chain.py。
"""
from __future__ import annotations

import pytest

from config import codex as codex_cfg
from config import live_check as live_cfg
from config import roxybrowser as roxy_cfg
from core import codex_oauth, driver_chain


# ============================================================
# 解析与校验
# ============================================================

def test_chain_splits_every_supported_separator_and_keeps_order():
    raw = "roxy, cloak; browser_use| skyvern\n protocol"
    assert driver_chain.parse_driver_chain(
        raw, key="REGISTRATION_DRIVER", allowed=driver_chain.REGISTRATION_DRIVERS, default="protocol",
    ) == ["roxy", "cloak", "browser_use", "skyvern", "protocol"]
    assert driver_chain.parse_driver_chain(
        "roxy -> cloak → protocol", key="REGISTRATION_DRIVER",
        allowed=driver_chain.REGISTRATION_DRIVERS, default="protocol",
    ) == ["roxy", "cloak", "protocol"]
    assert driver_chain.parse_driver_chain(
        "roxy，cloak；skyvern、protocol", key="REGISTRATION_DRIVER",
        allowed=driver_chain.REGISTRATION_DRIVERS, default="protocol",
    ) == ["roxy", "cloak", "skyvern", "protocol"]


def test_chain_normalizes_aliases_and_drops_duplicates():
    chain = driver_chain.parse_driver_chain(
        "roxybrowser, fingerprint, API, cloakbrowser, ROXY", key="REGISTRATION_DRIVER",
        allowed=driver_chain.REGISTRATION_DRIVERS, default="protocol",
    )
    assert chain == ["roxy", "protocol", "cloak"]


@pytest.mark.parametrize("raw", [None, "", "   ", ",", " -> "])
def test_empty_chain_falls_back_to_the_single_default(raw):
    assert driver_chain.parse_driver_chain(
        raw, key="LIVE_CHECK_DRIVER", allowed=driver_chain.LIVE_CHECK_DRIVERS, default="protocol",
    ) == ["protocol"]


@pytest.mark.parametrize("raw, expected", [
    ("typo", "不支持的 LIVE_CHECK_DRIVER='typo'，可选：protocol / cloak"),
    ("protocol, typo", "不支持的 LIVE_CHECK_DRIVER='typo'，可选：protocol / cloak"),
    ("same_as_registration", "不支持的 LIVE_CHECK_DRIVER='same_as_registration'，可选：protocol / cloak"),
])
def test_unknown_or_unsupported_layer_reports_the_config_key(raw, expected):
    with pytest.raises(driver_chain.DriverChainError) as excinfo:
        driver_chain.parse_driver_chain(
            raw, key="LIVE_CHECK_DRIVER", allowed=driver_chain.LIVE_CHECK_DRIVERS, default="protocol",
        )
    assert str(excinfo.value) == expected


def test_chain_length_is_capped():
    raw = ",".join(["roxy", "cloak", "protocol"])
    assert len(driver_chain.parse_driver_chain(
        raw, key="REGISTRATION_DRIVER", allowed=driver_chain.REGISTRATION_DRIVERS, default="protocol",
    )) == 3
    padded = ",".join(["roxy", "cloak", "protocol"] * 5)
    chain = driver_chain.parse_driver_chain(
        padded, key="REGISTRATION_DRIVER", allowed=driver_chain.REGISTRATION_DRIVERS, default="protocol",
    )
    assert chain == ["roxy", "cloak", "protocol"]
    assert driver_chain.MAX_LAYERS >= len(chain)


def test_config_readers_follow_hot_reload(monkeypatch):
    monkeypatch.setattr(roxy_cfg, "REGISTRATION_DRIVER", "cloak > roxy")
    monkeypatch.setattr(live_cfg, "LIVE_CHECK_DRIVER", "cloak, protocol")
    monkeypatch.setattr(codex_cfg, "CODEX_OAUTH_DRIVER", "roxy, protocol")
    assert driver_chain.registration_drivers() == ["cloak", "roxy"]
    assert driver_chain.live_check_drivers() == ["cloak", "protocol"]
    assert driver_chain.codex_oauth_drivers() == ["roxy", "protocol"]


def test_same_as_registration_expands_into_the_registration_chain(monkeypatch):
    monkeypatch.setattr(roxy_cfg, "REGISTRATION_DRIVER", "cloak, protocol")
    monkeypatch.setattr(codex_cfg, "CODEX_OAUTH_DRIVER", "same_as_registration")
    assert driver_chain.codex_oauth_drivers() == ["cloak", "protocol"]
    monkeypatch.setattr(codex_cfg, "CODEX_OAUTH_DRIVER", "roxy, same_as_registration")
    assert driver_chain.codex_oauth_drivers() == ["roxy", "cloak", "protocol"]


def test_same_as_registration_does_not_self_reference(monkeypatch):
    monkeypatch.setattr(roxy_cfg, "REGISTRATION_DRIVER", "protocol")
    monkeypatch.setattr(codex_cfg, "CODEX_OAUTH_DRIVER", "same_as_registration, same_as_registration")
    assert driver_chain.codex_oauth_drivers() == ["protocol"]


def test_chain_helpers_render_labels_and_text():
    assert driver_chain.describe_chain(["roxy", "cloak", "protocol"]) == "roxy → cloak → protocol"
    assert driver_chain.driver_label("browser_use") == "Browser Use"
    assert driver_chain.driver_label("unknown") == "unknown"
    assert driver_chain.format_chain_value(["roxy", "protocol"]) == "roxy, protocol"


# ============================================================
# 注册：第一层整体失败才换第二层
# ============================================================

@pytest.fixture
def registration(monkeypatch):
    from core import task_control
    import main

    monkeypatch.setattr(roxy_cfg, "REGISTRATION_DRIVER", "roxy, cloak, protocol")
    monkeypatch.setattr(driver_chain, "stop_requested", lambda: False)
    calls: list[dict] = []

    def fake_driver(driver_mode, email, name, birthday=None, proxy=None, otp_code=None,
                    batch_dir=None, on_email_acquired=None):
        calls.append({"driver": driver_mode, "email": email, "on_email_acquired": on_email_acquired})
        if on_email_acquired:
            on_email_acquired(f"{driver_mode}@example.test")
        return {"success": True, "email": f"{driver_mode}@example.test", "account_id": 1}

    monkeypatch.setattr(main, "_run_registration_driver", fake_driver)
    return {
        "main": main,
        "calls": calls,
        "task_control": task_control,
        "run": lambda: main.run_registration(email=None, name="测试 用户"),
    }


def test_registration_reports_the_chain_and_uses_the_first_working_layer(registration):
    result = registration["run"]()
    assert [item["driver"] for item in registration["calls"]] == ["roxy"]
    assert result["success"] is True
    assert result["email"] == "roxy@example.test"


def test_registration_falls_back_until_a_layer_produces_an_account(registration):
    main = registration["main"]

    def fake_driver(driver_mode, email, name, birthday=None, proxy=None, otp_code=None,
                    batch_dir=None, on_email_acquired=None):
        registration["calls"].append({"driver": driver_mode, "email": email})
        if driver_mode == "protocol":
            return {"success": True, "email": "done@example.test", "account_id": 9}
        return {"success": False, "email": None, "error": f"{driver_mode} 启动失败"}

    main._run_registration_driver = fake_driver
    result = registration["run"]()
    assert [item["driver"] for item in registration["calls"]] == ["roxy", "cloak", "protocol"]
    assert result["success"] is True and result["account_id"] == 9


def test_registration_keeps_the_created_account_instead_of_retrying(registration):
    """账号已经创建（只剩 Codex 没跑完）不能被当成"该层失败"再换一层重注册。"""
    main = registration["main"]

    def fake_driver(driver_mode, email, name, birthday=None, proxy=None, otp_code=None,
                    batch_dir=None, on_email_acquired=None):
        registration["calls"].append({"driver": driver_mode, "email": email})
        return {"success": False, "email": "half@example.test", "account_id": 5,
                "error": "Codex 未完成: 接码超时"}

    main._run_registration_driver = fake_driver
    result = registration["run"]()
    assert [item["driver"] for item in registration["calls"]] == ["roxy"]
    assert result["account_id"] == 5


def test_registration_exception_in_a_layer_is_treated_as_layer_failure(registration):
    main = registration["main"]

    def fake_driver(driver_mode, email, name, birthday=None, proxy=None, otp_code=None,
                    batch_dir=None, on_email_acquired=None):
        registration["calls"].append({"driver": driver_mode, "email": email})
        if driver_mode == "roxy":
            raise RuntimeError("selenium 未安装")
        return {"success": True, "email": "ok@example.test", "account_id": 3}

    main._run_registration_driver = fake_driver
    result = registration["run"]()
    assert [item["driver"] for item in registration["calls"]] == ["roxy", "cloak"]
    assert result["success"] is True


def test_registration_passes_the_original_email_to_every_layer(registration):
    main = registration["main"]

    def fake_driver(driver_mode, email, name, birthday=None, proxy=None, otp_code=None,
                    batch_dir=None, on_email_acquired=None):
        registration["calls"].append({"driver": driver_mode, "email": email})
        return {"success": False, "email": email, "error": "失败"}

    main._run_registration_driver = fake_driver
    main.run_registration(email="fixed@example.test", name="测试 用户")
    assert [item["email"] for item in registration["calls"]] == ["fixed@example.test"] * 3


def test_registration_stops_switching_when_the_task_is_cancelled(registration, monkeypatch):
    main = registration["main"]
    monkeypatch.setattr(driver_chain, "stop_requested", lambda: True)

    def fake_driver(driver_mode, email, name, birthday=None, proxy=None, otp_code=None,
                    batch_dir=None, on_email_acquired=None):
        registration["calls"].append({"driver": driver_mode, "email": email})
        return {"success": False, "email": email, "error": "用户手动停止"}

    main._run_registration_driver = fake_driver
    result = registration["run"]()
    assert [item["driver"] for item in registration["calls"]] == ["roxy"]
    assert result["success"] is False


def test_registration_cancellation_exceptions_are_never_swallowed(registration):
    main = registration["main"]
    task_control = registration["task_control"]

    def fake_driver(driver_mode, email, name, birthday=None, proxy=None, otp_code=None,
                    batch_dir=None, on_email_acquired=None):
        registration["calls"].append({"driver": driver_mode, "email": email})
        raise task_control.TaskCancelled("用户取消任务")

    main._run_registration_driver = fake_driver
    with pytest.raises(task_control.TaskCancelled):
        registration["run"]()
    assert len(registration["calls"]) == 1


def test_registration_single_layer_short_circuits_the_chain(monkeypatch):
    from core import db
    import main

    monkeypatch.setattr(roxy_cfg, "REGISTRATION_DRIVER", "cloak")
    monkeypatch.setattr(db, "get_account_by_email", lambda email: None)
    calls: list[str] = []
    monkeypatch.setattr(
        main, "_run_registration_driver",
        lambda driver_mode, *args, **kwargs: calls.append(driver_mode) or {"success": True, "account_id": 1},
    )
    result = main.run_registration(email="a@b.test", name="测试 用户")
    assert calls == ["cloak"]
    assert result["success"] is True


def test_registration_rejects_unknown_layers_before_running_anything(monkeypatch):
    import main

    monkeypatch.setattr(roxy_cfg, "REGISTRATION_DRIVER", "roxy, typo")
    with pytest.raises(driver_chain.DriverChainError) as excinfo:
        main.run_registration(email="a@b.test", name="测试 用户")
    assert "REGISTRATION_DRIVER" in str(excinfo.value)


# ============================================================
# Codex 授权：第一层没拿到凭据才换第二层
# ============================================================

@pytest.fixture
def codex(monkeypatch):
    monkeypatch.setattr(codex_cfg, "CODEX_OAUTH_DRIVER", "roxy, protocol")
    monkeypatch.setattr(driver_chain, "stop_requested", lambda: False)
    calls: list[str] = []

    def fake_driver(oauth_driver, email, otp_provider=None, proxy=None, force=False, _cpa_reauth_round=1):
        calls.append(oauth_driver)
        return codex_oauth._codex_result(status="success", ok=True, email=email)

    monkeypatch.setattr(codex_oauth, "_run_codex_oauth_driver", fake_driver)
    return {
        "calls": calls,
        "run": lambda **kwargs: codex_oauth.run_codex_oauth("user@example.test", force=True, **kwargs),
    }


def test_codex_uses_the_first_layer_when_it_succeeds(codex):
    result = codex["run"]()
    assert codex["calls"] == ["roxy"]
    assert result["status"] == "success" and result["ok"] is True


def test_codex_falls_back_to_the_next_layer_after_failure(codex):
    failed = codex_oauth._codex_result(status="failed", email="user@example.test", message="回调超时")
    restored: list[str] = []

    def fake_driver(oauth_driver, email, otp_provider=None, proxy=None, force=False, _cpa_reauth_round=1):
        codex["calls"].append(oauth_driver)
        if oauth_driver == "roxy":
            return failed
        restored.append(oauth_driver)
        return codex_oauth._codex_result(status="success", ok=True, email=email)

    codex_oauth._run_codex_oauth_driver = fake_driver
    result = codex["run"]()
    assert codex["calls"] == ["roxy", "protocol"]
    assert restored == ["protocol"]
    assert result["ok"] is True


@pytest.mark.parametrize("status", ["skipped", "deactivated"])
def test_codex_terminal_layers_do_not_switch(status, monkeypatch):
    monkeypatch.setattr(codex_cfg, "CODEX_OAUTH_DRIVER", "roxy, protocol")
    calls: list[str] = []

    def fake_driver(oauth_driver, email, otp_provider=None, proxy=None, force=False, _cpa_reauth_round=1):
        calls.append(oauth_driver)
        return codex_oauth._codex_result(status=status, email=email, message=status)

    monkeypatch.setattr(codex_oauth, "_run_codex_oauth_driver", fake_driver)
    result = codex_oauth.run_codex_oauth("user@example.test", force=True)
    assert calls == ["roxy"]
    assert result["status"] == status


def test_codex_missing_dependency_skips_that_layer(codex):
    def fake_driver(oauth_driver, email, otp_provider=None, proxy=None, force=False, _cpa_reauth_round=1):
        codex["calls"].append(oauth_driver)
        if oauth_driver == "roxy":
            raise ImportError("No module named 'selenium'")
        return codex_oauth._codex_result(status="success", ok=True, email=email)

    codex_oauth._run_codex_oauth_driver = fake_driver
    result = codex["run"]()
    assert codex["calls"] == ["roxy", "protocol"]
    assert result["ok"] is True


def test_codex_stops_switching_after_a_user_stop(codex, monkeypatch):
    monkeypatch.setattr(driver_chain, "stop_requested", lambda: True)

    def fake_driver(oauth_driver, email, otp_provider=None, proxy=None, force=False, _cpa_reauth_round=1):
        codex["calls"].append(oauth_driver)
        return codex_oauth._codex_result(status="failed", email=email, message="回调超时")

    codex_oauth._run_codex_oauth_driver = fake_driver
    result = codex_oauth.run_codex_oauth("user@example.test", force=True)
    assert codex["calls"] == ["roxy"]
    assert result["status"] == "failed"


def test_codex_single_layer_keeps_the_legacy_import_error_fallback(monkeypatch):
    """单层浏览器驱动缺依赖时，仍按旧行为回退到纯协议。"""
    monkeypatch.setattr(codex_cfg, "CODEX_OAUTH_DRIVER", "roxy")
    calls: list[str] = []

    def fake_driver(oauth_driver, email, otp_provider=None, proxy=None, force=False, _cpa_reauth_round=1):
        calls.append(oauth_driver)
        if oauth_driver == "roxy":
            raise ImportError("No module named 'selenium'")
        return codex_oauth._codex_result(status="success", ok=True, email=email)

    monkeypatch.setattr(codex_oauth, "_run_codex_oauth_driver", fake_driver)
    result = codex_oauth.run_codex_oauth("user@example.test", force=True)
    assert calls == ["roxy", "protocol"]
    assert result["ok"] is True


def test_codex_disabled_auto_skips_before_reading_the_chain(monkeypatch):
    monkeypatch.setattr(codex_cfg, "ENABLE_CODEX_AUTO", False)
    monkeypatch.setattr(codex_cfg, "CODEX_OAUTH_DRIVER", "typo")

    def explode(*args, **kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("未开启自动授权时不应执行任何途径")

    monkeypatch.setattr(codex_oauth, "_run_codex_oauth_driver", explode)
    assert codex_oauth.run_codex_oauth("user@example.test")["status"] == "skipped"
