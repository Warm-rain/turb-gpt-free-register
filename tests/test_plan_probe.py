# -*- coding: utf-8 -*-
"""Offline checks for the multi-exgress Plus trial eligibility probe.

Implements the observations recorded in wangshen's OSS ``core/promo_check.py``:
the same account can be eligible on most exits and not_eligible on one, and the
same exit can flip between error / zero / non-zero across repeated checks. The
probe therefore never treats a single negative verdict as final.
"""
from __future__ import annotations

from pathlib import Path
import runpy
import unittest
from unittest.mock import patch

import config
from config import proxy as proxy_cfg
from core import db, plan_check_service, plan_probe
from webui import config_editor

_EXIT_A = "socks5h://127.0.0.1:7901"
_EXIT_B = "socks5h://127.0.0.1:7902"
_EXIT_C = "socks5h://127.0.0.1:7903"


def _sample_result(eligible: bool, *, plan: str = "free", checked_at: str = "2026-10-24T10:00:00+00:00",
                   campaign: str | None = None) -> dict:
    result = {
        "ok": True,
        "http_status": 200,
        "current_plan_type": plan,
        "plus_trial_eligible": eligible,
        "checked_at": checked_at,
    }
    if eligible:
        result.update({
            "plus_trial_campaign_id": campaign or "plus-1-month-free",
            "plus_trial_title": "1 month free",
            "plus_trial_discount_percentage": 100,
            "plus_trial_duration_num_periods": 1,
            "plus_trial_duration_period": "month",
        })
    return result


def _error_result(message: str = "HTTP 429") -> dict:
    return {"ok": False, "http_status": 429, "error": message, "checked_at": "2026-10-24T10:00:01+00:00"}


class ProbeExitSelectionTests(unittest.TestCase):
    def test_plan_check_proxy_wins_and_base_exit_goes_last(self):
        with patch.object(proxy_cfg, "PLAN_CHECK_PROXY", [_EXIT_A, _EXIT_B]), \
             patch.object(proxy_cfg, "PROXY_POOL", [_EXIT_C]):
            exits = plan_probe.collect_probe_exits(_EXIT_B)
        self.assertEqual(exits, [_EXIT_A, _EXIT_B])

    def test_falls_back_to_pool_and_dedupes(self):
        with patch.object(proxy_cfg, "PLAN_CHECK_PROXY", []), \
             patch.object(proxy_cfg, "PROXY_POOL", [_EXIT_A, _EXIT_A, _EXIT_B]):
            exits = plan_probe.collect_probe_exits()
        self.assertEqual(exits, [_EXIT_A, _EXIT_B])

    def test_empty_pool_uses_direct_sampling(self):
        with patch.object(proxy_cfg, "PLAN_CHECK_PROXY", []), \
             patch.object(proxy_cfg, "PROXY_POOL", []):
            self.assertEqual(plan_probe.collect_probe_exits(), [""])
            self.assertEqual(plan_probe.collect_probe_exits("  "), [""])

    def test_unknown_base_proxy_is_appended(self):
        with patch.object(proxy_cfg, "PLAN_CHECK_PROXY", [_EXIT_A]), \
             patch.object(proxy_cfg, "PROXY_POOL", []):
            self.assertEqual(plan_probe.collect_probe_exits(_EXIT_C), [_EXIT_A, _EXIT_C])


class ProbeAppliesGateTests(unittest.TestCase):
    def test_only_successful_non_paid_negative_results_are_probed(self):
        self.assertTrue(plan_probe.probe_applies({"ok": True, "current_plan_type": "free"}))
        self.assertTrue(plan_probe.probe_applies({"ok": True, "current_plan_type": ""}))
        self.assertFalse(plan_probe.probe_applies({"ok": True, "plus_trial_eligible": True}))
        self.assertFalse(plan_probe.probe_applies({"ok": False, "error": "timeout"}))
        for plan in ("plus", "pro", "team", "go", "Plus"):
            self.assertFalse(plan_probe.probe_applies({"ok": True, "current_plan_type": plan}), plan)


class ProbeSamplingTests(unittest.TestCase):
    def test_rotates_exits_and_stops_on_first_eligible(self):
        import unittest.mock as mock

        with mock.patch.object(proxy_cfg, "PLAN_CHECK_PROXY", [_EXIT_A, _EXIT_B]), \
             mock.patch.object(proxy_cfg, "PROXY_POOL", []), \
             mock.patch.object(proxy_cfg, "PLAN_CHECK_PROBE_ENABLED", True), \
             mock.patch.object(proxy_cfg, "PLAN_CHECK_PROBE_ATTEMPTS", 5), \
             mock.patch.object(proxy_cfg, "PLAN_CHECK_PROBE_INTERVAL", 0.0):
            results = [_sample_result(False), _sample_result(True, campaign="plus-2-month")]
            with patch.object(plan_probe, "check_account_plan", side_effect=results) as check:
                probe = plan_probe.probe_plus_trial_eligibility("token-1")
        self.assertEqual(check.call_count, 2)
        self.assertEqual([c.kwargs["proxy"] for c in check.call_args_list], [_EXIT_A, _EXIT_B])
        for call in check.call_args_list:
            self.assertEqual(call.kwargs["max_attempts"], 1)
        self.assertEqual(probe["conclusion"], plan_probe.CONCLUSION_ELIGIBLE)
        self.assertTrue(probe["stopped_early"])
        self.assertEqual(probe["attempts"], 2)
        self.assertEqual(probe["eligible_count"], 1)
        self.assertEqual(probe["winner"]["plus_trial_campaign_id"], "plus-2-month")
        self.assertEqual(probe["sampled_exits"], [_EXIT_A, _EXIT_B])

    def test_consistent_negative_across_exits_is_not_eligible(self):
        import unittest.mock as mock

        with mock.patch.object(proxy_cfg, "PLAN_CHECK_PROXY", [_EXIT_A, _EXIT_B, _EXIT_C]), \
             mock.patch.object(proxy_cfg, "PROXY_POOL", []), \
             mock.patch.object(proxy_cfg, "PLAN_CHECK_PROBE_ATTEMPTS", 3), \
             mock.patch.object(proxy_cfg, "PLAN_CHECK_PROBE_INTERVAL", 0.0):
            with patch.object(plan_probe, "check_account_plan",
                              side_effect=[_sample_result(False)] * 3) as check:
                probe = plan_probe.probe_plus_trial_eligibility("token-1")
        self.assertEqual(check.call_count, 3)
        self.assertEqual(probe["conclusion"], plan_probe.CONCLUSION_NOT_ELIGIBLE)
        self.assertFalse(probe["egress_divergence"])
        self.assertFalse(probe["unstable"])
        self.assertEqual(probe["not_eligible_count"], 3)
        self.assertEqual(probe["pool_size"], 3)
        self.assertEqual(sorted(probe["sampled_exits"]), sorted([_EXIT_A, _EXIT_B, _EXIT_C]))

    def test_partial_exit_coverage_stays_inconclusive(self):
        import unittest.mock as mock

        with mock.patch.object(proxy_cfg, "PLAN_CHECK_PROXY", [_EXIT_A, _EXIT_B, _EXIT_C]), \
             mock.patch.object(proxy_cfg, "PROXY_POOL", []), \
             mock.patch.object(proxy_cfg, "PLAN_CHECK_PROBE_ATTEMPTS", 2), \
             mock.patch.object(proxy_cfg, "PLAN_CHECK_PROBE_INTERVAL", 0.0):
            with patch.object(plan_probe, "check_account_plan",
                              side_effect=[_sample_result(False)] * 2):
                probe = plan_probe.probe_plus_trial_eligibility("token-1")
        # 采样次数少于出口数：还有出口没验证，负面结论不足为证。
        self.assertEqual(probe["conclusion"], plan_probe.CONCLUSION_INCONCLUSIVE)
        self.assertEqual(len(probe["sampled_exits"]), 2)
        self.assertEqual(probe["pool_size"], 3)

    def test_single_exit_negative_is_inconclusive(self):
        import unittest.mock as mock

        with mock.patch.object(proxy_cfg, "PLAN_CHECK_PROXY", [_EXIT_A]), \
             mock.patch.object(proxy_cfg, "PROXY_POOL", []), \
             mock.patch.object(proxy_cfg, "PLAN_CHECK_PROBE_ATTEMPTS", 4), \
             mock.patch.object(proxy_cfg, "PLAN_CHECK_PROBE_INTERVAL", 0.0):
            with patch.object(plan_probe, "check_account_plan",
                              side_effect=[_sample_result(False)] * 4):
                probe = plan_probe.probe_plus_trial_eligibility("token-1")
        self.assertEqual(probe["conclusion"], plan_probe.CONCLUSION_INCONCLUSIVE)
        self.assertEqual(probe["sampled_exits"], [_EXIT_A])
        self.assertFalse(probe["egress_divergence"])

    def test_error_samples_keep_conclusion_inconclusive(self):
        import unittest.mock as mock

        with mock.patch.object(proxy_cfg, "PLAN_CHECK_PROXY", [_EXIT_A, _EXIT_B]), \
             mock.patch.object(proxy_cfg, "PROXY_POOL", []), \
             mock.patch.object(proxy_cfg, "PLAN_CHECK_PROBE_ATTEMPTS", 4), \
             mock.patch.object(proxy_cfg, "PLAN_CHECK_PROBE_INTERVAL", 0.0):
            with patch.object(plan_probe, "check_account_plan", side_effect=[
                _sample_result(False), _error_result(), _sample_result(False), _sample_result(False),
            ]):
                probe = plan_probe.probe_plus_trial_eligibility("token-1")
        self.assertEqual(probe["conclusion"], plan_probe.CONCLUSION_INCONCLUSIVE)
        self.assertEqual(probe["error_count"], 1)
        self.assertEqual(probe["not_eligible_count"], 3)

    def test_same_exit_drift_is_flagged_as_unstable_not_divergence(self):
        import unittest.mock as mock

        with mock.patch.object(proxy_cfg, "PLAN_CHECK_PROXY", [_EXIT_A]), \
             mock.patch.object(proxy_cfg, "PROXY_POOL", []), \
             mock.patch.object(proxy_cfg, "PLAN_CHECK_PROBE_ATTEMPTS", 3), \
             mock.patch.object(proxy_cfg, "PLAN_CHECK_PROBE_INTERVAL", 0.0):
            with patch.object(plan_probe, "check_account_plan", side_effect=[
                _sample_result(False), _sample_result(False), _sample_result(True),
            ]):
                probe = plan_probe.probe_plus_trial_eligibility("token-1")
        # 同一出口先后给出否定与肯定：这是观察 2 的抖动，不是出口间分歧。
        self.assertTrue(probe["unstable"])
        self.assertEqual(probe["unstable_exits"], [_EXIT_A])
        self.assertFalse(probe["egress_divergence"])
        self.assertEqual(probe["conclusion"], plan_probe.CONCLUSION_ELIGIBLE)

    def test_cross_exit_disagreement_is_flagged_as_divergence(self):
        import unittest.mock as mock

        with mock.patch.object(proxy_cfg, "PLAN_CHECK_PROXY", [_EXIT_A, _EXIT_B, _EXIT_C]), \
             mock.patch.object(proxy_cfg, "PROXY_POOL", []), \
             mock.patch.object(proxy_cfg, "PLAN_CHECK_PROBE_ATTEMPTS", 3), \
             mock.patch.object(proxy_cfg, "PLAN_CHECK_PROBE_INTERVAL", 0.0):
            with patch.object(plan_probe, "check_account_plan", side_effect=[
                _sample_result(True), _sample_result(False), _sample_result(True),
            ]):
                # 关闭早停才能采完整池：分歧证据需要每条出口都有样本。
                probe = plan_probe.probe_plus_trial_eligibility("token-1", stop_on_eligible=False)
        self.assertEqual(probe["attempts"], 3)
        self.assertTrue(probe["egress_divergence"])
        self.assertEqual(probe["consistent_eligible_exits"], [_EXIT_A, _EXIT_C])
        self.assertEqual(probe["consistent_negative_exits"], [_EXIT_B])
        self.assertFalse(probe["unstable"])
        self.assertEqual(probe["conclusion"], plan_probe.CONCLUSION_ELIGIBLE)

    def test_checkpoint_can_interrupt_probe(self):
        import unittest.mock as mock

        from core import task_control

        with mock.patch.object(proxy_cfg, "PLAN_CHECK_PROXY", [_EXIT_A]), \
             mock.patch.object(proxy_cfg, "PROXY_POOL", []), \
             mock.patch.object(proxy_cfg, "PLAN_CHECK_PROBE_ATTEMPTS", 5), \
             mock.patch.object(proxy_cfg, "PLAN_CHECK_PROBE_INTERVAL", 0.0), \
             patch.object(plan_probe, "check_account_plan", side_effect=[_sample_result(False)]) as check:
            with self.assertRaises(task_control.TaskCancelled):
                plan_probe.probe_plus_trial_eligibility(
                    "token-1", checkpoint=lambda: (_ for _ in ()).throw(task_control.TaskCancelled()),
                )
        check.assert_not_called()


class MergeProbeIntoTests(unittest.TestCase):
    def test_eligible_probe_promotes_winner_fields(self):
        probe = {
            "conclusion": plan_probe.CONCLUSION_ELIGIBLE,
            "attempts": 2,
            "winner": {
                "plus_trial_campaign_id": "plus-2-month",
                "plus_trial_title": "2 months",
                "plus_trial_discount_percentage": 50,
                "plus_trial_duration_num_periods": 2,
                "plus_trial_duration_period": "month",
                "checked_at": "2026-10-24T12:00:00+00:00",
            },
        }
        result = {
            "ok": True,
            "current_plan_type": "free",
            "plus_trial_eligible": False,
            "checked_at": "2026-10-24T11:00:00+00:00",
            "quota_balance": "12.34",
        }
        merged = plan_probe.merge_probe_into_result(result, probe)
        self.assertTrue(merged["plus_trial_eligible"])
        self.assertEqual(merged["plus_trial_campaign_id"], "plus-2-month")
        self.assertEqual(merged["plus_trial_discount_percentage"], 50)
        self.assertEqual(merged["plus_trial_duration_num_periods"], 2)
        self.assertEqual(merged["checked_at"], "2026-10-24T12:00:00+00:00")
        self.assertEqual(merged["quota_balance"], "12.34")
        self.assertEqual(merged["plus_trial_probe_conclusion"], plan_probe.CONCLUSION_ELIGIBLE)
        self.assertEqual(merged["plus_trial_probe_attempts"], 2)
        self.assertIs(merged["plus_trial_probe"], probe)

    def test_inconclusive_probe_keeps_negative_verdict_with_evidence(self):
        probe = {"conclusion": plan_probe.CONCLUSION_INCONCLUSIVE, "attempts": 4, "winner": None}
        merged = plan_probe.merge_probe_into_result(
            {"ok": True, "plus_trial_eligible": False, "checked_at": "2026-10-24T11:00:00+00:00"}, probe,
        )
        self.assertFalse(merged["plus_trial_eligible"])
        self.assertEqual(merged["checked_at"], "2026-10-24T11:00:00+00:00")
        self.assertEqual(merged["plus_trial_probe_conclusion"], plan_probe.CONCLUSION_INCONCLUSIVE)
        self.assertNotIn("plus_trial_campaign_id", merged)


class ServiceIntegrationTests(unittest.TestCase):
    def _account(self):
        db._save_collection("accounts", [{
            "id": 1, "email": "probe@example.test", "access_token": "access-token",
            "status": "registered", "created_at": "2026-10-24T09:00:00+00:00",
            "updated_at": "2026-10-24T09:00:00+00:00",
        }])
        return 1

    def _run(self, *, run_id="run-probe", trigger="manual", proxy=None, session=None):
        # 生产路径由 _claim_plan_slot 先占队列槽，_run_plan_check 的 finally 再归还；
        # 直接调用时先在测试里补上这次 acquire，保持信号量平衡。
        plan_check_service._QUEUE_SLOTS.acquire()
        return plan_check_service._run_plan_check(
            account_id=1, email="probe@example.test", access_token="access-token",
            trigger=trigger, proxy=proxy, timezone_offset_min="-", session=session, run_id=run_id,
        )

    def test_negative_conclusion_is_probed_and_winner_persisted(self):
        import unittest.mock as mock

        account_id = self._account()
        self.assertTrue(db.claim_account_plan_check(acc_id=account_id, trigger="manual", run_id="run-probe"))
        with mock.patch.object(proxy_cfg, "PLAN_CHECK_PROXY", [_EXIT_A, _EXIT_B]), \
             mock.patch.object(proxy_cfg, "PROXY_POOL", []), \
             mock.patch.object(proxy_cfg, "PLAN_CHECK_PROBE_ENABLED", True), \
             mock.patch.object(proxy_cfg, "PLAN_CHECK_PROBE_ATTEMPTS", 5), \
             mock.patch.object(proxy_cfg, "PLAN_CHECK_PROBE_INTERVAL", 0.0), \
             mock.patch.object(plan_check_service, "check_account_plan",
                               return_value=_sample_result(False)), \
             mock.patch.object(plan_check_service, "_wait_for_rate_slot"), \
             mock.patch.object(plan_probe, "check_account_plan",
                               side_effect=[_sample_result(False), _sample_result(True, campaign="plus-2-month")]):
            result = self._run()
        self.assertTrue(result["ok"])
        self.assertTrue(result["plus_trial_eligible"])
        self.assertEqual(result["plus_trial_probe_conclusion"], plan_probe.CONCLUSION_ELIGIBLE)
        row = db.get_account(account_id)
        self.assertTrue(row["plus_trial_eligible"])
        self.assertEqual(row["plus_trial_probe_conclusion"], plan_probe.CONCLUSION_ELIGIBLE)
        self.assertEqual(row["plus_trial_campaign_id"], "plus-2-month")
        snapshot = db.list_account_plan_check_statuses()
        item = next(i for i in snapshot["items"] if i["id"] == account_id)
        self.assertEqual(item["plus_trial_probe_conclusion"], plan_probe.CONCLUSION_ELIGIBLE)

    def test_probe_skipped_when_first_check_is_eligible(self):
        import unittest.mock as mock

        account_id = self._account()
        db.claim_account_plan_check(acc_id=account_id, trigger="manual", run_id="run-probe")
        with mock.patch.object(proxy_cfg, "PLAN_CHECK_PROXY", [_EXIT_A, _EXIT_B]), \
             mock.patch.object(proxy_cfg, "PLAN_CHECK_PROBE_ENABLED", True), \
             mock.patch.object(proxy_cfg, "PLAN_CHECK_PROBE_INTERVAL", 0.0), \
             mock.patch.object(plan_check_service, "check_account_plan",
                               return_value=_sample_result(True)), \
             mock.patch.object(plan_check_service, "_wait_for_rate_slot"), \
             mock.patch.object(plan_probe, "check_account_plan") as probe_check:
            result = self._run()
        probe_check.assert_not_called()
        self.assertTrue(result["plus_trial_eligible"])
        self.assertNotIn("plus_trial_probe", result)
        self.assertIsNone(db.get_account(account_id)["plus_trial_probe_conclusion"])

    def test_probe_skipped_when_disabled(self):
        import unittest.mock as mock

        account_id = self._account()
        db.claim_account_plan_check(acc_id=account_id, trigger="manual", run_id="run-probe")
        with mock.patch.object(proxy_cfg, "PLAN_CHECK_PROXY", [_EXIT_A, _EXIT_B]), \
             mock.patch.object(proxy_cfg, "PLAN_CHECK_PROBE_ENABLED", False), \
             mock.patch.object(proxy_cfg, "PLAN_CHECK_PROBE_INTERVAL", 0.0), \
             mock.patch.object(plan_check_service, "check_account_plan",
                               return_value=_sample_result(False)), \
             mock.patch.object(plan_check_service, "_wait_for_rate_slot"), \
             mock.patch.object(plan_probe, "check_account_plan") as probe_check:
            result = self._run()
        probe_check.assert_not_called()
        self.assertFalse(result["plus_trial_eligible"])
        self.assertNotIn("plus_trial_probe", result)

    def test_probe_skipped_for_borrowed_live_session(self):
        import unittest.mock as mock

        account_id = self._account()
        db.claim_account_plan_check(acc_id=account_id, trigger="live_auto", run_id="run-probe")
        with mock.patch.object(proxy_cfg, "PLAN_CHECK_PROXY", [_EXIT_A, _EXIT_B]), \
             mock.patch.object(proxy_cfg, "PLAN_CHECK_PROBE_ENABLED", True), \
             mock.patch.object(proxy_cfg, "PLAN_CHECK_PROBE_INTERVAL", 0.0), \
             mock.patch.object(plan_check_service, "check_account_plan",
                               return_value=_sample_result(False)), \
             mock.patch.object(plan_check_service, "_wait_for_rate_slot"), \
             mock.patch.object(plan_probe, "check_account_plan") as probe_check:
            result = self._run(trigger="live_auto", session=object())
        probe_check.assert_not_called()
        self.assertFalse(result["plus_trial_eligible"])
        self.assertNotIn("plus_trial_probe", result)


class ProbeSettingsTests(unittest.TestCase):
    def test_defaults_and_env_overrides(self):
        import unittest.mock as mock

        settings = runpy.run_path(str(Path(proxy_cfg.__file__)))
        self.assertTrue(settings["PLAN_CHECK_PROBE_ENABLED"])
        self.assertEqual(settings["PLAN_CHECK_PROBE_ATTEMPTS"], 5)
        self.assertEqual(settings["PLAN_CHECK_PROBE_INTERVAL"], 1.0)
        with mock.patch.dict("os.environ", {
            "PLAN_CHECK_PROBE_ENABLED": "false",
            "PLAN_CHECK_PROBE_ATTEMPTS": "8",
            "PLAN_CHECK_PROBE_INTERVAL": "2.5",
        }):
            settings = runpy.run_path(str(Path(proxy_cfg.__file__)))
        self.assertFalse(settings["PLAN_CHECK_PROBE_ENABLED"])
        self.assertEqual(settings["PLAN_CHECK_PROBE_ATTEMPTS"], 8)
        self.assertEqual(settings["PLAN_CHECK_PROBE_INTERVAL"], 2.5)

    def test_settings_exported_and_editable_in_webui(self):
        for key in ("PLAN_CHECK_PROBE_ENABLED", "PLAN_CHECK_PROBE_ATTEMPTS", "PLAN_CHECK_PROBE_INTERVAL"):
            self.assertIn(key, config.__all__)
            self.assertEqual(getattr(config, key), getattr(proxy_cfg, key))
        fields = {item["key"]: item for item in config_editor.get_config()}
        for key in ("PLAN_CHECK_PROBE_ENABLED", "PLAN_CHECK_PROBE_ATTEMPTS", "PLAN_CHECK_PROBE_INTERVAL"):
            self.assertEqual(fields[key]["file"], "proxy.py")
            self.assertEqual(fields[key]["group"], "代理池")
        self.assertEqual(fields["PLAN_CHECK_PROBE_ENABLED"]["type"], "bool")
        self.assertEqual(fields["PLAN_CHECK_PROBE_ATTEMPTS"]["type"], "int")
        self.assertEqual(fields["PLAN_CHECK_PROBE_INTERVAL"]["type"], "float")


if __name__ == "__main__":
    unittest.main()
