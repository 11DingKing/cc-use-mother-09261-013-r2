import os
import tempfile
import unittest

from service_09261_013.api import dispatch
from service_09261_013.store import SQLiteStore
from service_09261_013.workflow import (
    ConflictError,
    NotFound,
    PermissionDenied,
    ValidationError,
    Workflow,
)

ROLES = {"alice": "applicant", "bob": "approver", "carol": "approver"}


def make_flow(roles=None):
    return Workflow(SQLiteStore(), ROLES if roles is None else roles)


def submitted_case(flow, case_id="c1", applicant="alice", supplier="pub-1"):
    """建案并提交，进入 reviewing（版本 2，第 1 轮）。"""
    flow.create(case_id, applicant, supplier, "高等数学", key=f"k-{case_id}-create")
    flow.submit(case_id, applicant, 1, key=f"k-{case_id}-submit")
    return flow.get_case(case_id)


class TestContinuity(unittest.TestCase):
    """申请—退回—再次提交—批准的连续关系。"""

    def setUp(self):
        self.f = make_flow()
        submitted_case(self.f)

    def test_cycle_keeps_single_case_with_continuous_versions(self):
        self.f.return_case("c1", "bob", 2, key="k-ret", reason="缺少样章")
        r = self.f.resubmit("c1", "alice", 3, key="k-resub", note="已补样章")
        self.assertEqual(r["case"]["round"], 2)  # 同一案件的第 2 轮提交
        r = self.f.approve("c1", "carol", 4, key="k-appr", reason="通过")
        self.assertEqual(r["case"]["state"], "approved")
        self.assertEqual(r["case"]["version"], 5)
        self.assertEqual(r["case"]["case_id"], "c1")
        self.assertEqual(len(self.f.list_cases()), 1)  # 重新提交没有开新案

    def test_review_comments_survive_resubmission(self):
        self.f.return_case("c1", "bob", 2, key="k-ret", reason="审查意见：补充第3章习题")
        self.f.resubmit("c1", "alice", 3, key="k-resub")
        reasons = [e["reason"] for e in self.f.history("c1") if e["action"] == "returned"]
        self.assertEqual(reasons, ["审查意见：补充第3章习题"])

    def test_history_reconstructs_every_change(self):
        self.f.return_case("c1", "bob", 2, key="k-ret", reason="退回修改")
        self.f.resubmit("c1", "alice", 3, key="k-resub")
        self.f.approve("c1", "carol", 4, key="k-appr")
        events = self.f.history("c1")
        self.assertEqual([e["action"] for e in events],
                         ["created", "submitted", "returned", "resubmitted", "approved"])
        self.assertEqual([e["seq"] for e in events], [1, 2, 3, 4, 5])
        for prev, nxt in zip(events, events[1:]):  # 状态链首尾相接
            self.assertEqual(prev["to_state"], nxt["from_state"])
        for e in events:
            self.assertTrue(e["actor"])
            self.assertTrue(e["at"])
        self.assertEqual(events[2]["actor"], "bob")
        self.assertEqual(events[2]["reason"], "退回修改")


class TestAuthorization(unittest.TestCase):
    """权限与回避：无权者不能代替审批者作决定。"""

    def setUp(self):
        self.f = make_flow()
        submitted_case(self.f)

    def test_non_approver_cannot_decide(self):
        with self.assertRaises(PermissionDenied):
            self.f.approve("c1", "alice", 2, key="k-x1")  # 申请人角色
        with self.assertRaises(PermissionDenied):
            self.f.return_case("c1", "mallory", 2, key="k-x2", reason="越权")  # 无角色

    def test_only_applicant_can_resubmit(self):
        self.f.return_case("c1", "bob", 2, key="k-ret", reason="改")
        with self.assertRaises(PermissionDenied):
            self.f.resubmit("c1", "carol", 3, key="k-x3")

    def test_applicant_must_recuse_even_if_approver(self):
        f = make_flow(roles={"alice": "approver"})
        f.create("c9", "alice", "pub-9", "线性代数", key="k-c9")
        f.submit("c9", "alice", 1, key="k-c9s")
        with self.assertRaises(PermissionDenied):
            f.approve("c9", "alice", 2, key="k-c9a")

    def test_conflicted_approver_must_recuse(self):
        self.f.declare_relationship("bob", "pub-1")
        with self.assertRaises(PermissionDenied):
            self.f.approve("c1", "bob", 2, key="k-b")
        r = self.f.approve("c1", "carol", 2, key="k-c")  # 无关联的审批人可通过
        self.assertEqual(r["case"]["state"], "approved")
        self.assertEqual(r["event"]["detail"]["recusal_check"], "passed")
        self.assertFalse(r["event"]["detail"]["recusal_required"])

    def test_applicant_supplier_relationship_flags_case(self):
        f = make_flow()
        f.declare_relationship("alice", "pub-7")
        f.create("c7", "alice", "pub-7", "概率论", key="k-c7")
        self.assertTrue(f.get_case("c7")["recusal_required"])


class TestIdempotency(unittest.TestCase):
    """重复点击：同一幂等键只产生一次效果。"""

    def setUp(self):
        self.f = make_flow()

    def test_create_replay(self):
        r1 = self.f.create("c1", "alice", "pub-1", "高数", key="k-c")
        r2 = self.f.create("c1", "alice", "pub-1", "高数", key="k-c")
        self.assertFalse(r1["idempotent_replay"])
        self.assertTrue(r2["idempotent_replay"])
        self.assertEqual(len(self.f.list_cases()), 1)

    def test_duplicate_click_single_effect(self):
        submitted_case(self.f)
        r1 = self.f.approve("c1", "bob", 2, key="k-a")
        r2 = self.f.approve("c1", "bob", 2, key="k-a")
        self.assertTrue(r2["idempotent_replay"])
        self.assertEqual(r1["event"], r2["event"])
        self.assertEqual(self.f.get_case("c1")["version"], 3)
        approved = [e for e in self.f.history("c1") if e["action"] == "approved"]
        self.assertEqual(len(approved), 1)

    def test_duplicate_case_id_conflict(self):
        self.f.create("c1", "alice", "pub-1", "高数", key="k-1")
        with self.assertRaises(ConflictError):
            self.f.create("c1", "alice", "pub-1", "高数", key="k-2")


class TestConcurrency(unittest.TestCase):
    """两名审批者同时处理：只形成一个可解释的结果。"""

    def setUp(self):
        self.f = make_flow()
        submitted_case(self.f)

    def test_two_approvers_race_single_result(self):
        r = self.f.approve("c1", "bob", 2, key="k-bob")
        self.assertEqual(r["case"]["state"], "approved")
        with self.assertRaises(ConflictError) as ctx:
            self.f.approve("c1", "carol", 2, key="k-carol")
        self.assertIn("当前版本 3", str(ctx.exception))  # 失败方可解释
        self.assertEqual(self.f.get_case("c1")["version"], 3)
        approved = [e for e in self.f.history("c1") if e["action"] == "approved"]
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["actor"], "bob")

    def test_conflicting_decisions_return_vs_approve(self):
        self.f.return_case("c1", "bob", 2, key="k-r", reason="退回")
        with self.assertRaises(ConflictError):
            self.f.approve("c1", "carol", 2, key="k-a")
        self.assertEqual(self.f.get_case("c1")["state"], "returned")


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.f = make_flow()

    def test_invalid_transition(self):
        self.f.create("c1", "alice", "pub-1", "高数", key="k-c")
        with self.assertRaises(ValidationError):
            self.f.approve("c1", "bob", 1, key="k-a")  # 草稿不可直接批准

    def test_return_requires_reason(self):
        submitted_case(self.f)
        with self.assertRaises(ValidationError):
            self.f.return_case("c1", "bob", 2, key="k-r", reason="")

    def test_expected_version_required(self):
        submitted_case(self.f)
        with self.assertRaises(ValidationError):
            self.f.approve("c1", "bob", None, key="k-a")

    def test_unknown_case(self):
        with self.assertRaises(NotFound):
            self.f.get_case("nope")


class TestPersistence(unittest.TestCase):
    """事后查询：换连接重开后仍能还原每次变化。"""

    def test_events_survive_reopen(self):
        path = os.path.join(tempfile.mkdtemp(), "audit.db")
        f1 = Workflow(SQLiteStore(path), ROLES)
        submitted_case(f1)
        f1.return_case("c1", "bob", 2, key="k-r", reason="补充材料")
        f1.store.close()
        f2 = Workflow(SQLiteStore(path), ROLES)
        events = f2.history("c1")
        self.assertEqual([e["action"] for e in events], ["created", "submitted", "returned"])
        self.assertEqual(events[-1]["reason"], "补充材料")
        f2.resubmit("c1", "alice", 3, key="k-re")
        self.assertEqual(f2.get_case("c1")["round"], 2)
        f2.store.close()


class TestAPI(unittest.TestCase):
    def setUp(self):
        self.f = make_flow()

    def test_full_http_flow(self):
        status, body = dispatch(self.f, "POST", "/cases",
                                {"id": "c1", "applicant": "alice", "supplier": "pub-1",
                                 "title": "高数", "idempotency_key": "k1"})
        self.assertEqual(status, 201)
        self.assertEqual(body["case"]["state"], "draft")
        status, _ = dispatch(self.f, "POST", "/cases/c1/submit",
                             {"actor": "alice", "expected_version": 1, "idempotency_key": "k2"})
        self.assertEqual(status, 200)
        status, _ = dispatch(self.f, "POST", "/cases/c1/return",
                             {"actor": "bob", "expected_version": 2, "idempotency_key": "k3"})
        self.assertEqual(status, 400)  # 退回缺审查意见
        status, _ = dispatch(self.f, "POST", "/cases/c1/approve",
                             {"actor": "alice", "expected_version": 2, "idempotency_key": "k4"})
        self.assertEqual(status, 403)  # 非审批角色不得代决
        status, _ = dispatch(self.f, "POST", "/cases/c1/approve",
                             {"actor": "bob", "expected_version": 9, "idempotency_key": "k5"})
        self.assertEqual(status, 409)  # 版本过期
        status, body = dispatch(self.f, "POST", "/cases/c1/approve",
                                {"actor": "bob", "expected_version": 2, "idempotency_key": "k6"})
        self.assertEqual(status, 200)
        self.assertEqual(body["case"]["state"], "approved")
        status, body = dispatch(self.f, "POST", "/cases/c1/approve",
                                {"actor": "bob", "expected_version": 2, "idempotency_key": "k6"})
        self.assertEqual(status, 200)
        self.assertTrue(body["idempotent_replay"])  # 重复点击返回首次结果
        status, body = dispatch(self.f, "GET", "/cases/c1/history")
        self.assertEqual(status, 200)
        self.assertEqual([e["action"] for e in body["history"]],
                         ["created", "submitted", "approved"])
        status, body = dispatch(self.f, "GET", "/cases/c1")
        self.assertEqual((status, body["state"]), (200, "approved"))
        status, body = dispatch(self.f, "GET", "/cases")
        self.assertEqual((status, len(body["cases"])), (200, 1))

    def test_not_found(self):
        status, _ = dispatch(self.f, "GET", "/cases/nope")
        self.assertEqual(status, 404)
        status, _ = dispatch(self.f, "GET", "/nope")
        self.assertEqual(status, 404)

    def test_relationships_endpoint(self):
        status, body = dispatch(self.f, "POST", "/relationships",
                                {"person": "bob", "supplier": "pub-1"})
        self.assertEqual((status, body["created"]), (201, True))
        status, body = dispatch(self.f, "POST", "/relationships",
                                {"person": "bob", "supplier": "pub-1"})
        self.assertEqual((status, body["created"]), (201, False))
        status, body = dispatch(self.f, "GET", "/relationships")
        self.assertEqual((status, len(body["relationships"])), (200, 1))


if __name__ == "__main__":
    unittest.main()
