# -*- coding: utf-8 -*-
"""查活多途径优先级链：每一层各自跑满重试，整层失败才换下一层。"""
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from config import live_check as live_cfg
from config import proxy as proxy_cfg
from core import driver_chain
from core import live_check_service as service


SUCCESS = {"ok": True, "status": "live", "access_token": "fixture-token"}
TEMPORARY = {"ok": False, "status": "failed", "retryable": True, "error": "临时连接失败"}
DEACTIVATED = {"ok": False, "status": "deactivated", "retryable": False, "error": "account_deactivated"}


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.setattr(proxy_cfg, "PLAN_CHECK_PROXY", [])
    monkeypatch.setattr(proxy_cfg, "PROXY_POOL", [])
    monkeypatch.setattr(proxy_cfg, "PLAN_CHECK_UPSTREAM_PROXY", "")
    monkeypatch.setattr(proxy_cfg, "LIVE_CHECK_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(proxy_cfg, "LIVE_CHECK_RETRY_DELAY", 0.0)
    monkeypatch.setattr(live_cfg, "LIVE_CHECK_DRIVER", "protocol, cloak")
    monkeypatch.setattr(service, "_QUEUE_SLOTS", SimpleNamespace(release=Mock(), acquire=Mock(return_value=True)))
    monkeypatch.setattr(service.db, "mark_account_live_check_running", Mock(return_value=True))
    monkeypatch.setattr(service.db, "get_account", Mock(return_value={"email_source": "fixture-mail"}))
    monkeypatch.setattr(service.db, "update_account_liveness", Mock())
    monkeypatch.setattr(service, "_append_log", Mock())
    monkeypatch.setattr(service.time, "sleep", Mock())

    def route_for(explicit_proxy=None):
        selected = explicit_proxy or ""
        return {
            "proxy": selected, "network_route": "direct", "proxy_mode": "direct",
            "proxy_used": None, "upstream_proxy": "", "upstream_proxy_used": None,
            "proxy_fallback_reason": None,
        }

    monkeypatch.setattr(service, "resolve_plan_check_route", route_for)
    monkeypatch.setattr(service, "open_plan_check_proxy", Mock(side_effect=lambda route, selected, timeout: (selected, None)))
    check = Mock(return_value=SUCCESS)
    monkeypatch.setattr(service, "check_account_liveness", check)

    def run():
        return service._run_live_check(account_id=99, email="chain@example.invalid", proxy="", trigger="manual")

    return SimpleNamespace(run=run, check=check, log=service._append_log)


def _layers(check):
    return [entry.kwargs.get("driver") for entry in check.call_args_list]


def _log_lines(log):
    return [entry.args[1] for entry in log.call_args_list]


def test_first_layer_success_never_touches_the_second(runner):
    result = runner.run()
    assert _layers(runner.check) == ["protocol"]
    assert result["ok"] is True
    assert result["driver"] == "protocol"
    assert result["driver_layer"] == 1
    assert result["driver_chain"] == ["protocol", "cloak"]


def test_failed_first_layer_switches_to_the_second_with_a_full_budget(runner):
    runner.check.side_effect = [TEMPORARY, TEMPORARY, SUCCESS]
    result = runner.run()
    # 第一层跑满 2 次后换层，第二层重新从第 1 次开始
    assert _layers(runner.check) == ["protocol", "protocol", "cloak"]
    assert result["ok"] is True
    assert result["driver"] == "cloak"
    assert result["driver_layer"] == 2
    assert result["attempts"] == 1
    assert result["max_attempts"] == 2
    lines = _log_lines(runner.log)
    assert any("第 1/2 层途径：protocol" in line for line in lines)
    assert any("第 2/2 层途径：cloak" in line for line in lines)
    assert any("切换第 2 层途径 cloak" in line for line in lines)


def test_both_layers_failing_reports_the_last_layer(runner):
    runner.check.return_value = TEMPORARY
    result = runner.run()
    assert _layers(runner.check) == ["protocol", "protocol", "cloak", "cloak"]
    assert result["ok"] is False
    assert result["driver"] == "cloak"
    assert result["driver_layer"] == 2
    assert result["attempts"] == 2
    assert "临" in result["error"]


def test_deactivated_account_stops_the_chain(runner):
    runner.check.side_effect = [DEACTIVATED, SUCCESS]
    result = runner.run()
    assert _layers(runner.check) == ["protocol"]
    assert result["status"] == "deactivated"
    assert result["driver"] == "protocol"


def test_single_layer_config_keeps_reporting_its_driver(runner, monkeypatch):
    monkeypatch.setattr(live_cfg, "LIVE_CHECK_DRIVER", "cloak")
    result = runner.run()
    assert _layers(runner.check) == ["cloak"]
    assert result["driver"] == "cloak"
    assert result["driver_layer"] == 1
    assert result["driver_chain"] == ["cloak"]
    # 单层时不打链路横幅日志
    assert not any("途径优先级链" in line for line in _log_lines(runner.log))


def test_unknown_layer_fails_the_task_with_the_config_key(runner, monkeypatch):
    monkeypatch.setattr(live_cfg, "LIVE_CHECK_DRIVER", "cloak, typo")
    result = runner.run()
    assert result["ok"] is False
    assert "LIVE_CHECK_DRIVER" in result["error"]
    runner.check.assert_not_called()


def test_aliases_in_the_chain_reach_the_driver_normalized(runner, monkeypatch):
    monkeypatch.setattr(live_cfg, "LIVE_CHECK_DRIVER", "cloakbrowser, api")
    runner.check.side_effect = [TEMPORARY, TEMPORARY, SUCCESS]
    result = runner.run()
    assert _layers(runner.check) == ["cloak", "cloak", "protocol"]
    assert result["driver"] == "protocol"


def test_new_layer_picks_a_fresh_pool_exit(runner, monkeypatch):
    """换层重新从代理池选出口，避免上一层最后的直连兜底带进新的一层。"""
    monkeypatch.setattr(proxy_cfg, "PLAN_CHECK_PROXY", ["socks5://proxy-a.example:1080", "socks5://proxy-b.example:1080"])
    monkeypatch.setattr(proxy_cfg, "LIVE_CHECK_MAX_ATTEMPTS", 1)
    picks = iter(["socks5://proxy-a.example:1080", "socks5://proxy-b.example:1080"])
    monkeypatch.setattr(service.random, "choice", lambda values: next(picks))
    runner.check.side_effect = [TEMPORARY, SUCCESS]
    result = runner.run()
    assert [entry.kwargs["proxy"] for entry in runner.check.call_args_list] == [
        "socks5://proxy-a.example:1080", "socks5://proxy-b.example:1080",
    ]
    assert result["ok"] is True
    assert any("换层重新选择出口" in line for line in _log_lines(runner.log))


def test_manual_verification_bookkeeping_still_runs_between_layers(runner, monkeypatch):
    """换层不应绕过取消检查点：任务开始、每层开始、每次尝试前都会 checkpoint。"""
    checkpoints: list[str] = []
    monkeypatch.setattr(service.task_control, "checkpoint", lambda kind, ident: checkpoints.append(kind))
    runner.check.side_effect = [TEMPORARY, TEMPORARY, SUCCESS]
    runner.run()
    # 1 次任务开始 + 2 次层开始（protocol / cloak） + 3 次登录尝试
    assert checkpoints == [service.KIND] * 6
