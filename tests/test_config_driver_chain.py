# -*- coding: utf-8 -*-
"""WebUI 配置层的多途径优先级链：元信息、逐段校验与规范化写回。"""
from pathlib import Path
import runpy

import pytest

from config import codex as codex_cfg
from config import env_loader
from config import live_check as live_cfg
from config import roxybrowser as roxy_cfg
from core import driver_chain
from webui import config_editor


CHAIN_FIELDS = {
    "REGISTRATION_DRIVER": roxy_cfg,
    "LIVE_CHECK_DRIVER": live_cfg,
    "CODEX_OAUTH_DRIVER": codex_cfg,
}
EXPECTED_CHOICES = {
    "REGISTRATION_DRIVER": driver_chain.REGISTRATION_DRIVERS,
    "LIVE_CHECK_DRIVER": driver_chain.LIVE_CHECK_DRIVERS,
    "CODEX_OAUTH_DRIVER": driver_chain.CODEX_OAUTH_DRIVERS,
}
READ_CHAIN = {
    "REGISTRATION_DRIVER": driver_chain.registration_drivers,
    "LIVE_CHECK_DRIVER": driver_chain.live_check_drivers,
    "CODEX_OAUTH_DRIVER": driver_chain.codex_oauth_drivers,
}


def _field(key):
    return {item["key"]: item for item in config_editor.get_config()}[key]


@pytest.mark.parametrize("key", sorted(CHAIN_FIELDS))
def test_chain_fields_are_marked_and_offer_ordered_choices(key, monkeypatch):
    monkeypatch.delenv(key, raising=False)
    field = _field(key)
    assert field["chain"] is True
    assert field["type"] == "str"
    assert [choice["value"] for choice in field["choices"]] == list(EXPECTED_CHOICES[key])


def test_choices_keep_labels_for_the_visual_editor(monkeypatch):
    monkeypatch.delenv("LIVE_CHECK_DRIVER", raising=False)
    field = _field("LIVE_CHECK_DRIVER")
    assert {item["value"] for item in field["choices"]} == {"protocol", "cloak"}
    assert all(item.get("label") for item in field["choices"])


@pytest.mark.parametrize("key, raw, expected", [
    ("REGISTRATION_DRIVER", "roxybrowser > cloak, protocol", "roxy, cloak, protocol"),
    ("REGISTRATION_DRIVER", "ROXY ; CLOAK", "roxy, cloak"),
    ("LIVE_CHECK_DRIVER", "cloakbrowser，api", "cloak, protocol"),
    ("CODEX_OAUTH_DRIVER", "same-as-registration", "same_as_registration"),
    ("CODEX_OAUTH_DRIVER", "roxy, same_as_registration", "roxy, same_as_registration"),
])
def test_update_normalizes_aliases_and_separators(key, raw, expected):
    config_editor.update_config({key: raw})
    assert env_loader.read_env_file()[key] == expected
    assert runpy.run_path(str(Path(CHAIN_FIELDS[key].__file__)))[key] == expected


@pytest.mark.parametrize("key, raw", [
    ("REGISTRATION_DRIVER", "roxy, typo"),
    ("REGISTRATION_DRIVER", "same_as_registration"),
    ("LIVE_CHECK_DRIVER", "same_as_registration"),
    ("LIVE_CHECK_DRIVER", "roxy, protocol"),
    ("CODEX_OAUTH_DRIVER", "protocol, typo"),
])
def test_update_rejects_unknown_or_unsupported_layers(key, raw):
    with pytest.raises(ValueError) as excinfo:
        config_editor.update_config({key: raw})
    assert key in str(excinfo.value)


def test_rejected_update_never_touches_the_env_file(monkeypatch):
    monkeypatch.delenv("LIVE_CHECK_DRIVER", raising=False)
    config_editor.update_config({"LIVE_CHECK_DRIVER": "cloak"})
    with pytest.raises(ValueError):
        config_editor.update_config({"LIVE_CHECK_DRIVER": "cloak, nope"})
    assert env_loader.read_env_file()["LIVE_CHECK_DRIVER"] == "cloak"


@pytest.mark.parametrize("key", sorted(CHAIN_FIELDS))
def test_blank_chain_clears_the_override_and_falls_back_to_the_source_default(key):
    config_editor.update_config({key: "cloak"})
    config_editor.update_config({key: ""})
    assert env_loader.read_env_file()[key] == ""
    # 空值在运行时回退到 config/*.py 默认值（单层）
    assert len(READ_CHAIN[key]()) == 1


def test_chain_choices_stay_writable_for_single_layer_backwards_compatibility():
    config_editor.update_config({"REGISTRATION_DRIVER": "roxy"})
    assert env_loader.read_env_file()["REGISTRATION_DRIVER"] == "roxy"
    assert driver_chain.registration_drivers() == ["roxy"]
