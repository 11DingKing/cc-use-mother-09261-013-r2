"""教材选用回避审查工作流。

状态机: draft -> reviewing -> returned -> reviewing -> ... -> approved

设计要点:
- 退回(returned)后再次提交仍是同一案件, 轮次(round)递增,
  此前的审查意见保留在事件日志中, 不会随重新提交丢失。
- 审批决定只能由登记的审批者作出; 申请人不得审查自己的申请;
  与候选教材供应方存在关联的审批者必须回避。
- 命令可携带幂等键, 重复提交返回首次结果; 状态变更要求 expected_version
  匹配, 两人同时处理同一申请时只有一人成功, 另一人收到可解释的冲突。
- 每次状态变化追加一条不可变事件(操作人、前后状态、原因、时间、版本),
  供事后查询还原。
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone

# (当前状态, 目标状态) -> 事件动作名
TRANSITIONS = {
    ("draft", "reviewing"): "submitted",
    ("returned", "reviewing"): "resubmitted",
    ("reviewing", "returned"): "returned",
    ("reviewing", "approved"): "approved",
}


class DomainError(Exception):
    """业务错误: 携带 HTTP 语义状态码与机器可读 code。"""

    status = 400
    code = "bad_request"


class NotFound(DomainError):
    status = 404
    code = "not_found"


class Forbidden(DomainError):
    status = 403
    code = "forbidden"


class Conflict(DomainError):
    status = 409
    code = "conflict"


class Validation(DomainError):
    status = 422
    code = "validation"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fingerprint(payload) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Case:
    """一份教材选用申请。退回后重新提交沿用同一 id, round 递增。"""

    id: str
    applicant: str
    textbook: str
    supplier: str
    applicant_has_association: bool = False
    state: str = "draft"
    round: int = 0
    version: int = 1

    def view(self) -> dict:
        data = asdict(self)
        # 申请人已申报与供应方存在关联 -> 该案件需要回避审查
        data["recusal_required"] = self.applicant_has_association
        return data


@dataclass(frozen=True)
class Event:
    """一次状态变化的审计记录。"""

    case_id: str
    action: str
    actor: str
    from_state: str | None
    to_state: str
    round: int
    version: int
    reason: str = ""
    key: str | None = None
    at: str = ""
    seq: int = 0


@dataclass(frozen=True)
class KeyRecord:
    """幂等键记录: 指纹不同说明同一键被另一个命令复用。"""

    key: str
    fingerprint: str
    result: dict


class Workflow:
    def __init__(self, approvers=(), associations=(), store=None):
        self.approvers = set(approvers)
        # associations: {(人员, 供应方)} 关联申报, 命中者对该供应方案件必须回避
        self.associations = {(p, s) for p, s in associations}
        self.store = store
        self.rows: dict[str, Case] = {}
        self.events: dict[str, list[Event]] = {}
        self.keys: dict[str, KeyRecord] = {}
        self._seq = 0
        self._lock = threading.RLock()
        if store is not None:
            for case in store.load_cases():
                self.rows[case.id] = case
            for event in store.load_events():
                self.events.setdefault(event.case_id, []).append(event)
                self._seq = max(self._seq, event.seq)
            for record in store.load_keys():
                self.keys[record.key] = record

    # ---------- 查询 ----------

    def get(self, case_id) -> dict:
        return self._get(case_id).view()

    def snapshot(self) -> list:
        return [self.rows[k].view() for k in sorted(self.rows)]

    def history(self, case_id) -> list:
        """该案件的全部状态变化及原因, 按发生顺序返回。"""
        self._get(case_id)
        return [asdict(e) for e in self.events.get(case_id, [])]

    # ---------- 命令 ----------

    def create(self, id, applicant, textbook, supplier,
               applicant_has_association=False, actor=None, key=None):
        actor = actor or applicant
        fingerprint = _fingerprint(
            ["create", id, applicant, textbook, supplier,
             bool(applicant_has_association), actor])

        def do():
            if id in self.rows:
                raise Conflict(f"案件 {id!r} 已存在")
            if actor != applicant:
                raise Forbidden("只能由申请人本人提交申请")
            case = Case(id=id, applicant=applicant, textbook=textbook,
                        supplier=supplier,
                        applicant_has_association=bool(applicant_has_association))
            reason = ("申请人申报与供应方存在关联, 需要回避审查"
                      if case.applicant_has_association else "")
            return case, self._event(case, "created", actor, None, reason, key)

        return self._execute(key, fingerprint, do)

    def submit(self, id, actor, expected_version, reason="", key=None):
        """提交或退回后重新提交, 只能由申请人本人操作。"""
        fingerprint = _fingerprint(["submit", id, actor, expected_version, reason])

        def do():
            case = self._get(id)
            if actor != case.applicant:
                raise Forbidden("只有申请人本人可以提交或重新提交")
            self._check_version(case, expected_version)
            action = self._transition(case, "reviewing")
            new = replace(case, state="reviewing", round=case.round + 1,
                          version=case.version + 1)
            return new, self._event(new, action, actor, case.state, reason, key)

        return self._execute(key, fingerprint, do)

    def return_case(self, id, actor, expected_version, reason="", key=None):
        """退回申请, 必须填写审查意见。"""
        return self._decide(id, actor, expected_version, "returned",
                            reason, key, require_reason=True)

    def approve(self, id, actor, expected_version, reason="", key=None):
        """批准申请。"""
        return self._decide(id, actor, expected_version, "approved",
                            reason, key, require_reason=False)

    # ---------- 内部 ----------

    def _decide(self, id, actor, expected_version, target, reason, key,
                require_reason):
        fingerprint = _fingerprint(
            ["decide", target, id, actor, expected_version, reason])

        def do():
            case = self._get(id)
            self._check_decider(case, actor)
            if require_reason and not reason.strip():
                raise Validation("退回必须填写审查意见")
            self._check_version(case, expected_version)
            action = self._transition(case, target)
            new = replace(case, state=target, version=case.version + 1)
            return new, self._event(new, action, actor, case.state, reason, key)

        return self._execute(key, fingerprint, do)

    def _execute(self, key, fingerprint, do):
        with self._lock:
            if key is not None:
                cached = self._replay(key, fingerprint)
                if cached is not None:
                    return cached
            case, event = do()
            self.rows[case.id] = case
            self.events.setdefault(case.id, []).append(event)
            record = None
            if key is not None:
                record = KeyRecord(key, fingerprint, asdict(case))
                self.keys[key] = record
            if self.store is not None:
                self.store.commit_command(case, event, record)
            return case

    def _replay(self, key, fingerprint):
        record = self.keys.get(key)
        if record is None:
            return None
        if record.fingerprint != fingerprint:
            raise Conflict(f"幂等键 {key!r} 已被另一个命令使用")
        return Case(**record.result)

    def _event(self, case, action, actor, from_state, reason="", key=None):
        self._seq += 1
        return Event(case_id=case.id, action=action, actor=actor,
                     from_state=from_state, to_state=case.state,
                     round=case.round, version=case.version,
                     reason=reason, key=key, at=_now(), seq=self._seq)

    def _get(self, id):
        try:
            return self.rows[id]
        except KeyError:
            raise NotFound(f"案件 {id!r} 不存在") from None

    def _check_decider(self, case, actor):
        if actor not in self.approvers:
            raise Forbidden(f"{actor!r} 不是审批者, 无权作出审查决定")
        if actor == case.applicant:
            raise Forbidden("申请人不得审查自己的申请")
        if (actor, case.supplier) in self.associations:
            raise Forbidden(
                f"{actor!r} 与供应方 {case.supplier!r} 存在关联, 应当回避")

    @staticmethod
    def _check_version(case, expected_version):
        if expected_version is None:
            raise Validation("缺少 expected_version")
        if expected_version != case.version:
            raise Conflict(
                f"案件已被他人变更: 期望版本 {expected_version}, "
                f"当前版本 {case.version} (状态 {case.state})")

    @staticmethod
    def _transition(case, target):
        action = TRANSITIONS.get((case.state, target))
        if action is None:
            raise Conflict(f"当前状态 {case.state} 不允许变更为 {target}")
        return action
