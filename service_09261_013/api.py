"""JSON API 适配器: 把 HTTP 语义映射到工作流命令。"""
from .workflow import DomainError

ACTIONS = {"submit": "submit", "return": "return_case", "approve": "approve"}


def dispatch(flow, method, path, body=None):
    body = body or {}
    try:
        return _route(flow, method, _parts(path), body)
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (KeyError, TypeError) as exc:
        return 422, {"error": "validation",
                     "message": f"缺少或非法的参数: {exc}"}


def _parts(path):
    return [p for p in path.strip("/").split("/") if p]


def _route(flow, method, parts, body):
    if parts == ["cases"]:
        if method == "POST":
            case = flow.create(
                id=body["id"],
                applicant=body["applicant"],
                textbook=body["textbook"],
                supplier=body["supplier"],
                applicant_has_association=body.get(
                    "applicant_has_association", False),
                actor=body.get("actor"),
                key=body.get("idempotency_key"))
            return 201, case.view()
        if method == "GET":
            return 200, flow.snapshot()
    if len(parts) == 2 and parts[0] == "cases" and method == "GET":
        return 200, flow.get(parts[1])
    if (len(parts) == 3 and parts[0] == "cases"
            and parts[2] == "history" and method == "GET"):
        return 200, flow.history(parts[1])
    if len(parts) == 3 and parts[0] == "cases" and method == "POST":
        action = ACTIONS.get(parts[2])
        if action is not None:
            handler = getattr(flow, action)
            case = handler(
                parts[1],
                actor=body["actor"],
                expected_version=body.get("expected_version"),
                reason=body.get("reason", ""),
                key=body.get("idempotency_key"))
            return 200, case.view()
    return 404, {"error": "not_found", "message": "未知路径"}
