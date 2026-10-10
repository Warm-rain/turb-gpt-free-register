"""批次分组与「组完成后再通知」的契约测试，隔离 SMTP 传输与配置。"""
import json
from contextlib import closing

import pytest

from core import db, task_center_store as tasks


@pytest.fixture
def notifications(monkeypatch):
    from core import mail_notifications as module

    monkeypatch.setattr(module.settings, "SMTP_ENABLED", True)
    monkeypatch.setattr(module.settings, "SMTP_NOTIFY_TASKS", True)
    calls = []
    original = module.enqueue_notification

    def enqueue(conn, **message):
        assert conn.in_transaction
        calls.append(message)
        original(conn, **message)

    monkeypatch.setattr(module, "enqueue_notification", enqueue)
    monkeypatch.setattr(module, "send_notification", lambda *a, **kw: pytest.fail("network send inside task write"))
    db._ensure_sqlite()
    with closing(db._sqlite_conn()) as conn, conn:
        module._initialize(conn)
    return module, calls


def queued():
    with closing(db._sqlite_conn()) as conn:
        return [{"event_key": row["event_key"], **json.loads(row["payload"])}
                for row in conn.execute("SELECT * FROM mail_notifications ORDER BY id")]


def seed(*emails):
    db._save_accounts([
        {"id": index, "email": email, "access_token": f"SECRET-{index}",
         "created_at": "2025-01-01T00:00:00"}
        for index, email in enumerate(emails, 1)
    ])


def start_live_checks(*account_ids):
    for account_id in account_ids:
        assert db.claim_account_live_check(account_id)
        db.mark_account_live_check_running(account_id)


def test_batch_summary_replaces_success_mail_and_alerts_failures(notifications):
    seed("one@example.test", "two@example.test")
    start_live_checks(1, 2)
    batch_id = tasks.record_batch("live_check", [
        {"id": 1, "email": "one@example.test"}, {"id": 2, "email": "two@example.test"}])
    assert batch_id

    db.update_account_liveness(1, {"status": "live"})
    # 组内还有任务未结束：不发汇总，成功的单任务也不再单独发信。
    assert queued() == []

    db.update_account_liveness(2, {"status": "failed", "error": "SECRET-ERROR"})
    messages = queued()
    assert [message["title"] for message in messages] == [
        "账号查活 · 失败", "账号查活 · 批次完成 1/2"]
    assert messages[0]["emails"] == ["two@example.test"]
    summary = messages[1]
    assert summary["event_key"] == f"task:batch:{batch_id}:completed"
    assert summary["emails"] == ["one@example.test", "two@example.test"]
    assert summary["fields"]["成功"] == "1"
    assert summary["fields"]["失败"] == "1"
    assert summary["fields"]["提交数量"] == "2"
    assert "SECRET" not in json.dumps(messages)


def test_success_only_batch_sends_exactly_one_summary(notifications):
    seed("one@example.test", "two@example.test")
    start_live_checks(1, 2)
    tasks.record_batch("live_check", [{"id": 1}, {"id": 2}])
    db.update_account_liveness(1, {"status": "live"})
    db.update_account_liveness(2, {"status": "live"})
    messages = queued()
    assert len(messages) == 1
    assert messages[0]["event_key"].endswith(":completed")
    assert messages[0]["title"].startswith("账号查活 · 批次完成 2/2")


def test_batch_registered_after_completion_still_summarises(notifications):
    seed("one@example.test")
    start_live_checks(1)
    db.update_account_liveness(1, {"status": "live"})
    # 任务先结束、批次后登记（接口收集完再登记的真实顺序）也要能立刻汇总。
    assert tasks.record_batch("live_check", [{"id": 1, "email": "one@example.test"}])
    assert len(queued()) == 2  # 单任务邮件 + 立即产生的汇总


def test_later_single_run_is_not_suppressed_by_finished_batch(notifications):
    seed("one@example.test")
    start_live_checks(1)
    tasks.record_batch("live_check", [{"id": 1, "email": "one@example.test"}])
    db.update_account_liveness(1, {"status": "live"})
    assert len(queued()) == 1
    # 批次已结束，之后再跑一次单账号任务必须恢复逐任务通知。
    db.claim_account_live_check(1)
    db.mark_account_live_check_running(1)
    db.update_account_liveness(1, {"status": "live"})
    messages = queued()
    assert len(messages) == 2
    assert messages[1]["event_key"] != messages[0]["event_key"]


def test_cancelled_batch_member_is_counted_once(notifications):
    seed("one@example.test", "two@example.test")
    start_live_checks(1, 2)
    tasks.record_batch("live_check", [{"id": 1}, {"id": 2}])
    assert tasks.set_task_control_state("live_check", 1, "cancelled")
    assert queued() == []
    db.update_account_liveness(2, {"status": "live"})
    summary = queued()[-1]
    assert summary["fields"]["已取消"] == "1"
    assert summary["fields"]["成功"] == "1"


def test_task_detail_exposes_owning_batch(notifications):
    seed("one@example.test")
    start_live_checks(1)
    batch_id = tasks.record_batch("live_check", [{"id": 1, "email": "one@example.test"}])
    task_id = tasks.list_active_tasks()[0]["id"]
    task = tasks.get_task(task_id)
    assert task["batch"]["id"] == batch_id
    assert task["batch"]["total"] == 1
    assert task["batch"]["status"] == "open"
    db.update_account_liveness(1, {"status": "live"})
    assert tasks.get_task(task_id)["batch"]["status"] == "completed"


def test_registration_batch_uses_job_ids_and_one_summary(notifications):
    jobs = [db.create_job("outlook") for _ in range(2)]
    batch_id = tasks.record_batch("registration", [{"id": job["id"]} for job in jobs])
    assert batch_id
    db.update_job(jobs[0]["id"], status="success")
    assert queued() == []
    db.update_job(jobs[1]["id"], status="success")
    messages = queued()
    assert len(messages) == 1
    assert messages[0]["event_key"] == f"task:batch:{batch_id}:completed"
    assert messages[0]["fields"]["成功"] == "2"
    assert tasks.get_task(f"registration-{jobs[0]['id']}")["batch"]["id"] == batch_id


def test_summary_falls_back_to_account_email(notifications):
    seed("one@example.test", "two@example.test")
    start_live_checks(1, 2)
    # 开通 Plus 等接口只能给出账号编号，汇总邮件仍需回查账号邮箱。
    tasks.record_batch("live_check", [{"id": 1}, {"id": 2}])
    db.update_account_liveness(1, {"status": "live"})
    db.update_account_liveness(2, {"status": "live"})
    assert queued()[-1]["emails"] == ["one@example.test", "two@example.test"]


def test_record_batch_rejects_unknown_type_and_empty_roster(notifications):
    with pytest.raises(ValueError):
        tasks.record_batch("nope", [{"id": 1}])
    assert tasks.record_batch("live_check", []) is None


def test_bulk_endpoint_registers_one_batch(monkeypatch):
    from core import live_check_service
    from webui.app import create_app

    db._save_accounts([{"id": 1, "email": "one@example.test"},
                       {"id": 2, "email": "two@example.test"}])
    monkeypatch.setattr(live_check_service, "enqueue_account_live_check",
                        lambda **kwargs: {"accepted": True, "busy": False})
    client = create_app(auth_code="test-auth").test_client()
    response = client.post("/api/accounts/check-live-bulk", json={"account_ids": [1, 2, 1]},
                           headers={"X-Auth-Code": "test-auth"})
    assert response.status_code == 202
    batch_id = response.get_json()["task_batch_id"]
    assert batch_id
    with closing(db._sqlite_conn()) as conn:
        members = conn.execute("SELECT member_key,email FROM task_batch_members WHERE batch_id=?",
                               (batch_id,)).fetchall()
    assert sorted(row["member_key"] for row in members) == ["1", "2"]
    assert sorted(row["email"] for row in members) == ["one@example.test", "two@example.test"]
