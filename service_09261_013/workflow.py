"""教材选用回避审查的领域工作流。

- 连续关系：申请、退回、再次提交、批准共用同一 case_id，版本号与提交轮次连续递增；
  退回时的审查意见作为不可变事件永久保留，重新提交不开新案、不丢历史。
- 权限与回避：批准/退回仅审批角色可执行；申请人本人及与候选教材供应方存在
  申报关联的人员一律回避，无权者不得代替审批者作决定。
- 幂等与并发：变更命令必须携带 idempotency_key 与 expected_version。重复点击
  返回首次结果；两名审批者同时处理时只有第一个命令生效，其余得到 409 及当前
  状态说明——同一版本只会形成一个可解释的结果。
- 审计：每次状态变化追加一条事件（操作者、前后状态、原因、轮次、时间），
  history 可完整还原案件的每一次变化及其原因。
"""
import sqlite3
from datetime import datetime, timezone

# 状态机：draft -> reviewing -> returned -> reviewing -> approved
TRANSITIONS = {
    "submit": {"draft": "reviewing"},
    "resubmit": {"returned": "reviewing"},
    "return": {"reviewing": "returned"},
    "approve": {"reviewing": "approved"},
    "cancel": {"draft": "cancelled", "returned": "cancelled"},
}
EVENT_NAMES = {
    "create": "created", "submit": "submitted", "resubmit": "resubmitted",
    "return": "returned", "approve": "approved", "cancel": "cancelled",
}
DECISION_ACTIONS = ("approve", "return")
APPLICANT_ACTIONS = ("submit", "resubmit", "cancel")


class NotFound(Exception):
    """案件不存在。"""


class ConflictError(Exception):
    """版本/幂等冲突：已有其他命令先一步生效。"""


class PermissionDenied(Exception):
    """无权限或应当回避。"""


class ValidationError(Exception):
    """命令参数或状态不合法。"""


def _require(value, message):
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValidationError(message)
    return value


class Workflow:
    def __init__(self, store, roles=None):
        self.store = store
        self.roles = dict(roles or {})  # 姓名 -> "applicant" / "approver"

    # ---------- 查询 ----------
    def get_case(self, case_id):
        case = self.store.get_case(case_id)
        if case is None:
            raise NotFound(f"案件不存在: {case_id}")
        return self._case_view(case)

    def list_cases(self):
        return [self._case_view(c) for c in self.store.list_cases()]

    def history(self, case_id):
        """完整审计轨迹：每次状态变化及其原因、操作者、时间。"""
        if self.store.get_case(case_id) is None:
            raise NotFound(f"案件不存在: {case_id}")
        return self.store.list_events(case_id)

    def list_relationships(self):
        return self.store.list_relationships()

    # ---------- 命令 ----------
    def create(self, case_id, applicant, supplier, title="", key=None):
        _require(case_id, "id 必填")
        _require(applicant, "applicant 必填")
        _require(supplier, "supplier 必填")
        _require(key, "idempotency_key 必填")
        try:
            with self.store.transaction():
                cached = self.store.find_idempotency(key)
                if cached is not None:
                    return {**cached, "idempotent_replay": True}
                if self.store.get_case(case_id) is not None:
                    raise ConflictError(f"案件已存在: {case_id}")
                case = self.store.insert_case(case_id, applicant, supplier, title or "")
                event = self.store.append_event(
                    case_id=case_id, seq=1, action=EVENT_NAMES["create"], actor=applicant,
                    from_state=None, to_state="draft", round_=0, reason=None,
                    detail={"supplier": supplier, "title": title or ""}, key=key)
                response = {"case": self._case_view(case), "event": event}
                self.store.record_idempotency(key, case_id, "create", response)
                return {**response, "idempotent_replay": False}
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"并发写入冲突: {exc}") from exc

    def act(self, case_id, action, *, actor, expected_version, key, reason=None, note=None):
        if action not in TRANSITIONS:
            raise ValidationError(f"未知操作: {action}")
        _require(actor, "actor 必填")
        _require(key, "idempotency_key 必填")
        if expected_version is None:
            raise ValidationError("expected_version 必填")
        try:
            expected_version = int(expected_version)
        except (TypeError, ValueError):
            raise ValidationError("expected_version 必须为整数")
        if action == "return":
            _require(reason, "退回必须填写审查意见 reason")
        try:
            with self.store.transaction():
                cached = self.store.find_idempotency(key)
                if cached is not None:
                    return {**cached, "idempotent_replay": True}
                case = self.store.get_case(case_id)
                if case is None:
                    raise NotFound(f"案件不存在: {case_id}")
                self._check_actor(case, action, actor)
                # 先查版本再查状态机：版本不符说明调用方看到的是过期状态，
                # 属于并发冲突（409），应告知当前真实版本与状态。
                if case["version"] != expected_version:
                    raise ConflictError(
                        f"版本冲突：期望 {expected_version}，当前版本 {case['version']}"
                        f"（状态 {case['state']}），已有其他命令先生效")
                target = TRANSITIONS[action].get(case["state"])
                if target is None:
                    raise ValidationError(f"状态 {case['state']} 不允许执行 {action}")
                new_version = case["version"] + 1
                new_round = case["round"] + 1 if action in ("submit", "resubmit") else case["round"]
                if not self.store.update_case(case_id, expected_version, state=target,
                                              version=new_version, round_=new_round):
                    raise ConflictError(f"版本冲突：案件 {case_id} 已被并发修改")
                detail = {}
                if note:
                    detail["note"] = note
                if action in DECISION_ACTIONS:
                    detail["recusal_check"] = "passed"
                    detail["recusal_required"] = self.store.has_relationship(
                        case["applicant"], case["supplier"])
                event = self.store.append_event(
                    case_id=case_id, seq=new_version, action=EVENT_NAMES[action], actor=actor,
                    from_state=case["state"], to_state=target, round_=new_round,
                    reason=reason, detail=detail, key=key)
                response = {"case": self._case_view(self.store.get_case(case_id)), "event": event}
                self.store.record_idempotency(key, case_id, action, response)
                return {**response, "idempotent_replay": False}
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"并发写入冲突: {exc}") from exc

    def submit(self, case_id, actor, expected_version, key):
        return self.act(case_id, "submit", actor=actor,
                        expected_version=expected_version, key=key)

    def resubmit(self, case_id, actor, expected_version, key, note=None):
        return self.act(case_id, "resubmit", actor=actor,
                        expected_version=expected_version, key=key, note=note)

    def return_case(self, case_id, actor, expected_version, key, reason):
        return self.act(case_id, "return", actor=actor,
                        expected_version=expected_version, key=key, reason=reason)

    def approve(self, case_id, actor, expected_version, key, reason=None):
        return self.act(case_id, "approve", actor=actor,
                        expected_version=expected_version, key=key, reason=reason)

    def cancel(self, case_id, actor, expected_version, key):
        return self.act(case_id, "cancel", actor=actor,
                        expected_version=expected_version, key=key)

    def declare_relationship(self, person, supplier):
        """申报人员与供应方的关联关系，作为回避审查依据（天然幂等）。"""
        _require(person, "person 必填")
        _require(supplier, "supplier 必填")
        with self.store.transaction():
            created = self.store.declare_relationship(person, supplier)
        return {"person": person, "supplier": supplier, "created": created}

    # ---------- 内部 ----------
    def _check_actor(self, case, action, actor):
        if action in APPLICANT_ACTIONS and actor != case["applicant"]:
            raise PermissionDenied("仅申请人本人可提交/撤回该案件")
        if action in DECISION_ACTIONS:
            if self.roles.get(actor) != "approver":
                raise PermissionDenied("仅审批角色可作出审查决定，禁止他人代决")
            if actor == case["applicant"]:
                raise PermissionDenied("申请人须回避本人案件的审批")
            if self.store.has_relationship(actor, case["supplier"]):
                raise PermissionDenied("审批人与候选教材供应方存在申报关联，须回避")

    def _case_view(self, case):
        view = dict(case)
        view["recusal_required"] = self.store.has_relationship(
            case["applicant"], case["supplier"])
        return view
