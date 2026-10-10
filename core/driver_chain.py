# -*- coding: utf-8 -*-
"""注册 / 查活 / CodeX 授权的多途径（驱动）优先级链。

三个流程各自有一个配置项，取值既可以是单个途径，也可以是按优先级排列的途径列表：

    REGISTRATION_DRIVER = "roxy, cloak, protocol"
    LIVE_CHECK_DRIVER   = "cloak > protocol"
    CODEX_OAUTH_DRIVER  = "roxy, protocol"

分隔符支持英文/中文逗号、分号、竖线、箭头（``>`` ``->`` ``→``）以及空白。
执行时按顺序尝试：当前层整体失败（没有产出账号 / 没判定废号 / 没拿到授权凭据）
才切到下一层，任一层成功立即结束。单个取值 = 只有一层，与旧配置完全兼容。

路径别名与各流程的历史取值保持一致，例如 ``roxybrowser``/``fingerprint`` 都归一为
``roxy``，``cloakbrowser`` 归一为 ``cloak``。配置项里写别名同样生效。
"""
from __future__ import annotations

import re

# 链路长度上限：只用于防御异常配置，正常三到五层足够。
MAX_LAYERS = 8

# 各流程支持的规范化途径（顺序仅用于错误提示与前端排序参考）。
REGISTRATION_DRIVERS: tuple[str, ...] = ("protocol", "roxy", "cloak", "browser_use", "skyvern")
LIVE_CHECK_DRIVERS: tuple[str, ...] = ("protocol", "cloak")
CODEX_OAUTH_DRIVERS: tuple[str, ...] = (
    "protocol", "roxy", "cloak", "browser_use", "skyvern", "same_as_registration",
)

# 规范化途径 -> 可接受的写法（历史别名，保持与各驱动分发处的判断一致）。
DRIVER_ALIASES: dict[str, tuple[str, ...]] = {
    "protocol": ("protocol", "api", "http", "纯协议"),
    "roxy": ("roxy", "roxybrowser", "fingerprint", "browser", "指纹浏览器"),
    "cloak": ("cloak", "cloakbrowser", "本地指纹浏览器"),
    "browser_use": ("browser_use", "browseruse", "browser-use", "bu", "browser use"),
    "skyvern": ("skyvern", "sv"),
    "same_as_registration": (
        "same_as_registration", "same-as-registration", "sameasregistration",
        "follow_registration", "跟随注册",
    ),
}

# 规范化途径 -> 中文短标签（日志 / 前端展示）。
DRIVER_LABELS: dict[str, str] = {
    "protocol": "纯协议",
    "roxy": "RoxyBrowser",
    "cloak": "CloakBrowser",
    "browser_use": "Browser Use",
    "skyvern": "Skyvern",
    "same_as_registration": "跟随注册途径",
}

_ALIAS_TO_DRIVER: dict[str, str] = {
    alias.lower(): driver
    for driver, aliases in DRIVER_ALIASES.items()
    for alias in aliases
}

_SEPARATOR_RE = re.compile(r"[,;|，；、\s]+")


class DriverChainError(ValueError):
    """配置里的途径链无法解析（未知途径 / 该流程不支持）。"""


def split_driver_tokens(raw: object) -> list[str]:
    """把配置值切成原始 token 列表（保持顺序、去掉空项）。"""
    text = str(raw or "")
    for arrow in ("->", "=>", "→", "⇒", ">"):
        text = text.replace(arrow, ",")
    return [token.strip() for token in _SEPARATOR_RE.split(text) if token.strip()]


def normalize_driver(name: object) -> str | None:
    """把单个写法归一为规范化途径名；无法识别时返回 None。"""
    token = str(name or "").strip().lower()
    if not token:
        return None
    return _ALIAS_TO_DRIVER.get(token)


def parse_driver_chain(
    raw: object,
    *,
    key: str,
    allowed: tuple[str, ...],
    default: str,
) -> list[str]:
    """解析优先级链，返回规范化途径列表（去重、限制长度）。

    - 空配置（None / 空串 / 只有分隔符）回退到 ``default``；
    - 未知途径或该流程不支持的途径抛 :class:`DriverChainError`，
      消息里带上配置项名与可选值，便于任务日志直接定位。
    """
    tokens = split_driver_tokens(raw)
    if not tokens:
        return [default]

    chain: list[str] = []
    for token in tokens:
        driver = normalize_driver(token)
        if driver is None or driver not in allowed:
            raise DriverChainError(
                f"不支持的 {key}={token!r}，可选：{' / '.join(allowed)}"
            )
        if driver not in chain:
            chain.append(driver)
    if not chain:
        return [default]
    return chain[:MAX_LAYERS]


def describe_chain(chain: list[str] | tuple[str, ...]) -> str:
    """日志用：把优先级链渲染成 ``roxy → cloak → protocol``。"""
    return " → ".join(str(item) for item in chain)


def driver_label(name: object) -> str:
    """日志 / 前端用：途径的中文标签，未知取值原样返回。"""
    token = normalize_driver(name)
    if token is None:
        return str(name or "")
    return DRIVER_LABELS.get(token, token)


def format_chain_value(chain: list[str] | tuple[str, ...]) -> str:
    """把规范化途径列表写回配置项时使用的写法（逗号分隔）。"""
    return ", ".join(str(item) for item in chain)


def stop_requested() -> bool:
    """WebUI 任务是否已被用户停止（线程上下文里取 job_id）。

    途径回退意味着"再花一次成本"，用户主动停止后不应该继续换层重跑。
    """
    try:
        from core import registration_service
        return bool(registration_service.is_stop_requested())
    except Exception:
        return False


def is_cancellation(exc: BaseException) -> bool:
    """任务取消类异常：途径链不能吞掉它去换下一层。"""
    if stop_requested():
        return True
    try:
        from core import task_control
        if isinstance(exc, task_control.TaskCancelled):
            return True
    except Exception:
        pass
    try:
        from core.registration_service import StopRequested
        if isinstance(exc, StopRequested):
            return True
    except Exception:
        pass
    return False


def registration_drivers() -> list[str]:
    """当前注册优先级链（hot reload 安全：每次读取 config 模块属性）。"""
    from config import roxybrowser as cfg
    return parse_driver_chain(
        getattr(cfg, "REGISTRATION_DRIVER", ""),
        key="REGISTRATION_DRIVER",
        allowed=REGISTRATION_DRIVERS,
        default="protocol",
    )


def live_check_drivers() -> list[str]:
    """当前查活优先级链。"""
    from config import live_check as cfg
    return parse_driver_chain(
        getattr(cfg, "LIVE_CHECK_DRIVER", ""),
        key="LIVE_CHECK_DRIVER",
        allowed=LIVE_CHECK_DRIVERS,
        default="protocol",
    )


def codex_oauth_drivers() -> list[str]:
    """当前 Codex 授权优先级链；``same_as_registration`` 就地展开为注册链。"""
    from config import codex as cfg
    chain = parse_driver_chain(
        getattr(cfg, "CODEX_OAUTH_DRIVER", ""),
        key="CODEX_OAUTH_DRIVER",
        allowed=CODEX_OAUTH_DRIVERS,
        default="roxy",
    )
    if "same_as_registration" not in chain:
        return chain

    expanded: list[str] = []
    for driver in chain:
        if driver != "same_as_registration":
            if driver not in expanded:
                expanded.append(driver)
            continue
        for inherited in registration_drivers():
            if inherited not in expanded:
                expanded.append(inherited)
    return expanded[:MAX_LAYERS] or ["protocol"]
