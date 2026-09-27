"""JSON API 适配器：把 HTTP 方法+路径映射为工作流命令，并把领域错误翻译为状态码。"""
from .workflow import (
    ConflictError,
    NotFound,
    PermissionDenied,
    ValidationError,
)

_ACTIONS = {"submit", "resubmit", "return", "approve", "cancel"}


def dispatch(flow, method, path, body=None):
    body = body or {}
    parts = [p for p in path.split("/") if p]
    try:
        if method == "POST" and parts == ["cases"]:
            return 201, flow.create(body.get("id"), body.get("applicant"),
                                    body.get("supplier"), body.get("title", ""),
                                    body.get("idempotency_key"))
        if method == "GET" and parts == ["cases"]:
            return 200, {"cases": flow.list_cases()}
        if len(parts) >= 2 and parts[0] == "cases":
            case_id = parts[1]
            if method == "GET" and len(parts) == 2:
                return 200, flow.get_case(case_id)
            if method == "GET" and len(parts) == 3 and parts[2] == "history":
                return 200, {"history": flow.history(case_id)}
            if method == "POST" and len(parts) == 3 and parts[2] in _ACTIONS:
                return 200, flow.act(case_id, parts[2],
                                     actor=body.get("actor"),
                                     expected_version=body.get("expected_version"),
                                     key=body.get("idempotency_key"),
                                     reason=body.get("reason"),
                                     note=body.get("note"))
        if parts == ["relationships"]:
            if method == "POST":
                return 201, flow.declare_relationship(body.get("person"), body.get("supplier"))
            if method == "GET":
                return 200, {"relationships": flow.list_relationships()}
        return 404, {"error": "not_found"}
    except ValidationError as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}
    except PermissionDenied as exc:
        return 403, {"error": "forbidden", "message": str(exc)}
    except NotFound as exc:
        return 404, {"error": "not_found", "message": str(exc)}
    except ConflictError as exc:
        return 409, {"error": "conflict", "message": str(exc)}
