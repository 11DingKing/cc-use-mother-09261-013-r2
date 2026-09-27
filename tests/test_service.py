import os
import tempfile
import threading
import unittest

from service_09261_013.api import dispatch
from service_09261_013.store import SQLiteStore
from service_09261_013.workflow import (
    Conflict,
    Forbidden,
    Validation,
    Workflow,
)

APPROVERS = ["rev_a", "rev_b", "rev_c"]
ASSOCIATIONS = [("rev_b", "出版社甲")]


def make_flow(store=None):
    return Workflow(approvers=APPROVERS, associations=ASSOCIATIONS,
                    store=store)


def reviewing(flow, case_id="c1", supplier="出版社乙", association=False):
    flow.create(case_id, "alice", "高等数学", supplier,
                applicant_has_association=association)
    flow.submit(case_id, "alice", expected_version=1)


class TestContinuity(unittest.TestCase):
    """退回—重新提交—批准保持同一案件, 审查意见不丢失。"""

    def test_return_and_resubmit_keep_one_case_and_history(self):
        f = make_flow()
        reviewing(f)
        f.return_case("c1", "rev_a", expected_version=2,
                      reason="缺少样章审读记录")
        case = f.submit("c1", "alice", expected_version=3,
                        reason="已补充样章")
        self.assertEqual(case.state, "reviewing")
        self.assertEqual(case.round, 2)
        f.approve("c1", "rev_a", expected_version=4, reason="复核通过")

        self.assertEqual(len(f.snapshot()), 1)  # 仍是一份申请
        actions = [e["action"] for e in f.history("c1")]
        self.assertEqual(actions, ["created", "submitted", "returned",
                                   "resubmitted", "approved"])

    def test_history_restores_every_change_with_reason(self):
        f = make_flow()
        reviewing(f)
        f.return_case("c1", "rev_a", expected_version=2, reason="材料不全")
        f.submit("c1", "alice", expected_version=3)
        f.approve("c1", "rev_c", expected_version=4, reason="同意选用")

        events = f.history("c1")
        self.assertEqual(
            [(e["from_state"], e["to_state"]) for e in events],
            [(None, "draft"), ("draft", "reviewing"),
             ("reviewing", "returned"), ("returned", "reviewing"),
             ("reviewing", "approved")])
        self.assertEqual([e["version"] for e in events], [1, 2, 3, 4, 5])
        returned = events[2]
        self.assertEqual(returned["reason"], "材料不全")
        self.assertEqual(returned["actor"], "rev_a")
        self.assertTrue(all(e["at"] for e in events))


class TestRecusalAndAuthorization(unittest.TestCase):
    """回避与权限: 无权者不能代替审批者作决定。"""

    def setUp(self):
        self.f = make_flow()
        reviewing(self.f, supplier="出版社甲", association=True)

    def test_applicant_association_flags_case(self):
        self.assertTrue(self.f.get("c1")["recusal_required"])
        created = self.f.history("c1")[0]
        self.assertIn("关联", created["reason"])

    def test_non_approver_cannot_decide(self):
        with self.assertRaises(Forbidden):
            self.f.approve("c1", "mallory", expected_version=2)

    def test_applicant_cannot_approve_own_case(self):
        f = Workflow(approvers=["alice"])
        reviewing(f, "c9")
        with self.assertRaises(Forbidden):
            f.approve("c9", "alice", expected_version=2)

    def test_associated_approver_must_recuse(self):
        with self.assertRaises(Forbidden):
            self.f.approve("c1", "rev_b", expected_version=2)
        with self.assertRaises(Forbidden):
            self.f.return_case("c1", "rev_b", expected_version=2, reason="x")

    def test_independent_approver_can_decide(self):
        case = self.f.approve("c1", "rev_a", expected_version=2,
                              reason="回避审查通过")
        self.assertEqual(case.state, "approved")
        self.assertEqual(self.f.history("c1")[-1]["reason"], "回避审查通过")

    def test_only_applicant_submits(self):
        with self.assertRaises(Forbidden):
            self.f.submit("c1", "rev_a", expected_version=2)


class TestIdempotency(unittest.TestCase):
    """重复点击只产生一个结果。"""

    def test_duplicate_click_returns_same_result_once(self):
        f = make_flow()
        f.create("c1", "alice", "书", "出版社乙")
        a = f.submit("c1", "alice", expected_version=1, key="clk-1")
        b = f.submit("c1", "alice", expected_version=1, key="clk-1")
        self.assertEqual(a, b)
        self.assertEqual(len(f.history("c1")), 2)  # created + submitted

    def test_create_replay_and_key_reuse(self):
        f = make_flow()
        f.create("c1", "alice", "书", "出版社乙", key="k1")
        f.create("c1", "alice", "书", "出版社乙", key="k1")
        self.assertEqual(len(f.snapshot()), 1)
        with self.assertRaises(Conflict):
            f.create("c2", "alice", "书", "出版社乙", key="k1")

    def test_duplicate_create_without_key_conflicts(self):
        f = make_flow()
        f.create("c1", "alice", "书", "出版社乙")
        with self.assertRaises(Conflict):
            f.create("c1", "alice", "书", "出版社乙")


class TestConcurrency(unittest.TestCase):
    """两名审批者同时处理同一申请, 只形成一个可解释的结果。"""

    def test_two_approvers_one_result(self):
        f = make_flow()
        reviewing(f)
        results = []

        def decide(actor):
            try:
                results.append(("ok", f.approve("c1", actor,
                                                expected_version=2)))
            except Conflict as exc:
                results.append(("conflict", str(exc)))

        threads = [threading.Thread(target=decide, args=(a,))
                   for a in ("rev_a", "rev_c")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len([r for r in results if r[0] == "ok"]), 1)
        self.assertEqual(len([r for r in results if r[0] == "conflict"]), 1)
        approved = [e for e in f.history("c1") if e["action"] == "approved"]
        self.assertEqual(len(approved), 1)

    def test_stale_version_conflict_is_explainable(self):
        f = make_flow()
        reviewing(f)
        f.approve("c1", "rev_a", expected_version=2)
        with self.assertRaises(Conflict) as ctx:
            f.return_case("c1", "rev_c", expected_version=2, reason="晚一步")
        self.assertIn("approved", str(ctx.exception))


class TestValidation(unittest.TestCase):
    def test_return_requires_reason(self):
        f = make_flow()
        reviewing(f)
        with self.assertRaises(Validation):
            f.return_case("c1", "rev_a", expected_version=2, reason="  ")

    def test_invalid_transition(self):
        f = make_flow()
        f.create("c1", "alice", "书", "出版社乙")
        with self.assertRaises(Conflict):
            f.approve("c1", "rev_a", expected_version=1)

    def test_missing_expected_version(self):
        f = make_flow()
        f.create("c1", "alice", "书", "出版社乙")
        with self.assertRaises(Validation):
            f.submit("c1", "alice", expected_version=None)


class TestApi(unittest.TestCase):
    def setUp(self):
        self.f = make_flow()

    def test_happy_path_and_history_endpoint(self):
        status, body = dispatch(self.f, "POST", "/cases", {
            "id": "c1", "applicant": "alice", "textbook": "高等数学",
            "supplier": "出版社甲", "applicant_has_association": True,
            "idempotency_key": "c-1"})
        self.assertEqual(status, 201)
        self.assertTrue(body["recusal_required"])

        status, _ = dispatch(self.f, "POST", "/cases/c1/submit",
                             {"actor": "alice", "expected_version": 1})
        self.assertEqual(status, 200)
        status, body = dispatch(self.f, "POST", "/cases/c1/return",
                                {"actor": "rev_a", "expected_version": 2,
                                 "reason": "材料不全"})
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "returned")

        status, body = dispatch(self.f, "GET", "/cases/c1/history")
        self.assertEqual(status, 200)
        self.assertEqual([e["action"] for e in body],
                         ["created", "submitted", "returned"])
        status, body = dispatch(self.f, "GET", "/cases")
        self.assertEqual(len(body), 1)
        status, body = dispatch(self.f, "GET", "/cases/c1")
        self.assertEqual(body["round"], 1)

    def test_error_mapping(self):
        status, body = dispatch(self.f, "POST", "/cases/c1/approve",
                                {"actor": "rev_a", "expected_version": 1})
        self.assertEqual((status, body["error"]), (404, "not_found"))

        dispatch(self.f, "POST", "/cases",
                 {"id": "c1", "applicant": "alice", "textbook": "书",
                  "supplier": "出版社乙"})
        status, body = dispatch(self.f, "POST", "/cases/c1/submit",
                                {"actor": "mallory", "expected_version": 1})
        self.assertEqual((status, body["error"]), (403, "forbidden"))

        status, body = dispatch(self.f, "POST", "/cases", {"id": "c2"})
        self.assertEqual((status, body["error"]), (422, "validation"))

        status, body = dispatch(self.f, "DELETE", "/cases/c1")
        self.assertEqual((status, body["error"]), (404, "not_found"))

    def test_idempotent_replay_via_api(self):
        payload = {"id": "c1", "applicant": "alice", "textbook": "书",
                   "supplier": "出版社乙", "idempotency_key": "k"}
        first = dispatch(self.f, "POST", "/cases", payload)
        second = dispatch(self.f, "POST", "/cases", payload)
        self.assertEqual(first, second)
        self.assertEqual(len(self.f.snapshot()), 1)


class TestPersistence(unittest.TestCase):
    def test_state_and_history_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "audit.db")
            f1 = make_flow(store=SQLiteStore(path))
            f1.create("c1", "alice", "书", "出版社乙", key="k1")
            f1.submit("c1", "alice", expected_version=1)
            f1.return_case("c1", "rev_a", expected_version=2,
                           reason="补充材料")

            f2 = make_flow(store=SQLiteStore(path))
            self.assertEqual(f2.get("c1")["state"], "returned")
            self.assertEqual([e["action"] for e in f2.history("c1")],
                             ["created", "submitted", "returned"])
            self.assertEqual(f2.history("c1")[-1]["reason"], "补充材料")

            # 幂等键同样持久化: 重启后重复命令仍返回首次结果
            again = f2.create("c1", "alice", "书", "出版社乙", key="k1")
            self.assertEqual(again.version, 1)
            self.assertEqual(len(f2.snapshot()), 1)

            # 版本连续, 重新提交接着走
            case = f2.submit("c1", "alice", expected_version=3)
            self.assertEqual(case.round, 2)


if __name__ == "__main__":
    unittest.main()
