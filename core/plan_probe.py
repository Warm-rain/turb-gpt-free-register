# -*- coding: utf-8 -*-
"""Plus 试用资格多出口交叉验证（probe）。

依据 wangshen OSS 版 ``core/promo_check.py`` 注释记录的作者观察（本次未独立复现）：

1. 同一个账号通过八条出口查询，七条返回 eligible、一条返回 not_eligible；
   作者估计全池 100 条里约 10 条出口会把好号判成 not_eligible。
2. 同一个账号、同一条出口多次检查，结账结果在 error / 零元 / 非零元之间变化。

两条合起来只支持「查询环境和时点都可能影响结果」，不足以认定完全由 IP 决定。
因此单次单点的「not eligible」不作为定论：

- 负面结论必须换出口重新采样（观察 1）；
- 同一出口也允许重复采样，用分歧暴露抖动（观察 2）；
- 采样记录、所用出口、分歧与抖动证据全部写回结果，供事后审计。

业务口径：任一采样 eligible 即判 eligible（宁可多查也不错杀）；
全部采样一致负面且覆盖多条出口、无抖动才算 not_eligible；
其余情况（单出口、有错误、同出口结论漂移）标 inconclusive，交由使用者复查。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Callable, Optional

from core.chatgpt_plan import _mask_proxy, check_account_plan

logger = logging.getLogger(__name__)

CONCLUSION_ELIGIBLE = "eligible"
CONCLUSION_NOT_ELIGIBLE = "not_eligible"
CONCLUSION_INCONCLUSIVE = "inconclusive"

# probe 命中 eligible 时，需要从获胜采样回填到账号行的优惠字段。
_PROMO_FIELDS = (
    "plus_trial_campaign_id",
    "plus_trial_title",
    "plus_trial_discount_percentage",
    "plus_trial_duration_num_periods",
    "plus_trial_duration_period",
)

# 与账号列表页一致的套餐口径：命中这些关键字的账号不可能是 Free 试用资格对象。
_NON_FREE_PLAN_MARKERS = ("plus", "pro", "team", "go")


def _proxy_cfg():
    from config import proxy as proxy_cfg

    return proxy_cfg


def _bool_setting(name: str, default: bool) -> bool:
    try:
        return bool(getattr(_proxy_cfg(), name, default))
    except Exception:
        return default


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(getattr(_proxy_cfg(), name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _float_setting(name: str, default: float, lower: float, upper: float) -> float:
    try:
        value = float(getattr(_proxy_cfg(), name, default) or 0.0)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def probe_settings() -> dict:
    """返回 probe 的当前配置快照（WebUI 设置页可热改）。"""
    return {
        "enabled": _bool_setting("PLAN_CHECK_PROBE_ENABLED", True),
        "attempts": _int_setting("PLAN_CHECK_PROBE_ATTEMPTS", 5, 1, 10),
        "interval": _float_setting("PLAN_CHECK_PROBE_INTERVAL", 1.0, 0.0, 30.0),
    }


def probe_applies(result: dict) -> bool:
    """只有「查询成功、非付费套餐、未发现试用资格」的结论才需要交叉验证。"""
    if not isinstance(result, dict) or not result.get("ok"):
        return False
    if bool(result.get("plus_trial_eligible")):
        return False
    plan = str(result.get("current_plan_type") or result.get("plan_type") or "").lower()
    return not any(marker in plan for marker in _NON_FREE_PLAN_MARKERS)


def _proxy_lines(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [line.strip() for line in str(value).splitlines() if line.strip()]


def collect_probe_exits(base_proxy: Optional[str] = None) -> list[str]:
    """probe 使用的出口候选：优先套餐专用代理，再退回通用代理池。

    ``base_proxy``（主查询刚用过的出口）排到最后：先验证其他出口，再回到原出口
    重复采样。池为空时返回 ``[""]``（直连），让单出口配置也能做时点抖动采样。
    """
    cfg = _proxy_cfg()
    candidates = _proxy_lines(getattr(cfg, "PLAN_CHECK_PROXY", "")) or _proxy_lines(
        getattr(cfg, "PROXY_POOL", "")
    )
    ordered: list[str] = []
    for item in candidates:
        if item not in ordered:
            ordered.append(item)
    base = str(base_proxy or "").strip()
    if base:
        if base in ordered:
            ordered.remove(base)
        ordered.append(base)
    if not ordered:
        ordered = [""]
    return ordered


def _outcome(sample: dict) -> str:
    if not sample.get("ok"):
        return "error"
    return "eligible" if sample.get("plus_trial_eligible") else "not_eligible"


def _probe_once(token: str, exit_proxy: str, attempt: int, *, timeout: float | None = None) -> dict:
    """单次采样：固定一条出口、单次 HTTP 尝试，重复与轮换由 probe 编排。"""
    sample = {
        "attempt": attempt,
        "exit": _mask_proxy(exit_proxy) or "direct",
        "ok": False,
        "http_status": None,
        "error": None,
        "plus_trial_eligible": False,
        "current_plan_type": "",
        "checked_at": None,
    }
    try:
        result = check_account_plan(token, proxy=exit_proxy, max_attempts=1, timeout=timeout)
    except Exception as exc:
        sample["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        return sample
    sample["ok"] = bool(result.get("ok"))
    sample["http_status"] = result.get("http_status")
    sample["error"] = result.get("error")
    sample["plus_trial_eligible"] = bool(result.get("plus_trial_eligible"))
    sample["current_plan_type"] = result.get("current_plan_type") or ""
    sample["checked_at"] = result.get("checked_at")
    for key in _PROMO_FIELDS:
        if result.get(key) is not None:
            sample[key] = result.get(key)
    return sample


def _unique_exits(items: list[dict]) -> list[str]:
    out: list[str] = []
    for item in items:
        exit_name = item.get("exit")
        if exit_name and exit_name not in out:
            out.append(exit_name)
    return out


def summarize_probe(
    samples: list[dict],
    *,
    exits: list[str],
    base_proxy: Optional[str] = None,
    started_at: str | None = None,
    stopped_early: bool = False,
) -> dict:
    """按统计口径汇总采样：eligible 优先，全池一致负面才算 not_eligible。"""
    samples = [item for item in (samples or []) if isinstance(item, dict)]
    eligible = [s for s in samples if s.get("ok") and s.get("plus_trial_eligible")]
    negative = [s for s in samples if s.get("ok") and not s.get("plus_trial_eligible")]
    errors = [s for s in samples if not s.get("ok")]

    eligible_exits = _unique_exits(eligible)
    negative_exits = _unique_exits(negative)
    sampled_exits = _unique_exits(samples)

    # 观察 2：同一出口重复采样结论漂移（含 error 抖动）。
    per_exit: dict[str, set[str]] = {}
    for sample in samples:
        per_exit.setdefault(sample.get("exit") or "direct", set()).add(_outcome(sample))
    unstable_exits = [name for name, outcomes in per_exit.items() if len(outcomes) > 1]
    # 观察 1：出口之间结论相反。只统计“始终一致”的出口：同一出口自相矛盾属于
    # 抖动（观察 2），不是出口间分歧。
    consistent_eligible_exits = [name for name, outs in per_exit.items() if outs == {"eligible"}]
    consistent_negative_exits = [name for name, outs in per_exit.items() if outs == {"not_eligible"}]
    egress_divergence = bool(consistent_eligible_exits and consistent_negative_exits)

    if eligible:
        conclusion = CONCLUSION_ELIGIBLE
    elif (
        negative
        and not errors
        and len(exits) > 1
        and len(sampled_exits) >= len(exits)
        and not unstable_exits
    ):
        # 只有「池内每条出口都采到、且全部一致负面、无抖动」才敢下 not_eligible。
        conclusion = CONCLUSION_NOT_ELIGIBLE
    else:
        # 单出口、只覆盖部分出口、出现错误或同出口结论漂移：按作者观察，
        # 这些情况下负面结论都不足以定论。
        conclusion = CONCLUSION_INCONCLUSIVE

    winner = eligible[0] if eligible else None
    return {
        "conclusion": conclusion,
        "attempts": len(samples),
        "samples": samples,
        "eligible_count": len(eligible),
        "not_eligible_count": len(negative),
        "error_count": len(errors),
        "eligible_exits": eligible_exits,
        "not_eligible_exits": negative_exits,
        "consistent_eligible_exits": consistent_eligible_exits,
        "consistent_negative_exits": consistent_negative_exits,
        "sampled_exits": sampled_exits,
        "pool_size": len(exits),
        "base_exit": _mask_proxy(base_proxy) or None,
        "egress_divergence": egress_divergence,
        "unstable": bool(unstable_exits),
        "unstable_exits": unstable_exits,
        "stopped_early": bool(stopped_early),
        "started_at": started_at,
        "completed_at": datetime.now().isoformat(timespec="seconds"),
        "winner": winner,
    }


def probe_plus_trial_eligibility(
    token: str,
    *,
    base_proxy: Optional[str] = None,
    attempts: Optional[int] = None,
    stop_on_eligible: Optional[bool] = None,
    interval: Optional[float] = None,
    timeout: float | None = None,
    checkpoint: Optional[Callable[[], None]] = None,
    rate_limiter: Optional[Callable[[], None]] = None,
) -> dict:
    """对同一账号换出口重复采样 Plus 试用资格，返回汇总后的 probe 结果。"""
    settings = probe_settings()
    total = settings["attempts"] if attempts is None else max(1, min(10, int(attempts or 1)))
    stop_early = True if stop_on_eligible is None else bool(stop_on_eligible)
    gap = settings["interval"] if interval is None else max(0.0, min(30.0, float(interval or 0.0)))
    exits = collect_probe_exits(base_proxy)
    started_at = datetime.now().isoformat(timespec="seconds")

    samples: list[dict] = []
    for index in range(total):
        if checkpoint is not None:
            # 用户取消/暂停时由调用方的 control 抛出 TaskCancelled。
            checkpoint()
        exit_proxy = exits[index % len(exits)]
        if rate_limiter is not None:
            rate_limiter()
        sample = _probe_once(token, exit_proxy, index + 1, timeout=timeout)
        samples.append(sample)
        if stop_early and sample.get("ok") and sample.get("plus_trial_eligible"):
            break
        if index + 1 < total and gap > 0:
            time.sleep(gap)

    stopped_early = stop_early and bool(samples) and len(samples) < total
    summary = summarize_probe(
        samples, exits=exits, base_proxy=base_proxy, started_at=started_at, stopped_early=stopped_early,
    )
    logger.info(
        "[Probe] 资格交叉验证 conclusion=%s eligible=%s/%s exits=%s/%s divergence=%s unstable=%s",
        summary["conclusion"],
        summary["eligible_count"],
        summary["attempts"],
        len(summary["sampled_exits"]),
        summary["pool_size"],
        summary["egress_divergence"],
        summary["unstable"],
    )
    return summary


def merge_probe_into_result(result: dict, probe: dict) -> dict:
    """把 probe 汇总合并回套餐查询结果；eligible 时回填优惠字段。"""
    merged = dict(result or {})
    conclusion = str((probe or {}).get("conclusion") or "")
    winner = (probe or {}).get("winner") or {}
    if conclusion == CONCLUSION_ELIGIBLE and winner:
        merged["plus_trial_eligible"] = True
        for key in _PROMO_FIELDS:
            if winner.get(key) is not None:
                merged[key] = winner.get(key)
        if winner.get("checked_at"):
            # 资格实际观测时间以获胜采样为准，便于审计「哪一次出的资格」。
            merged["checked_at"] = winner.get("checked_at")
    merged["plus_trial_probe"] = probe
    merged["plus_trial_probe_conclusion"] = conclusion
    merged["plus_trial_probe_attempts"] = int((probe or {}).get("attempts") or 0)
    return merged
