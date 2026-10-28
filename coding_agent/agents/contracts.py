"""角色交接合同：模型提交工作报告，宿主校验结构，不把报告当作执行证据。"""

from copy import deepcopy
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry


MAX_REPORT_CHARS = 16000


def _text(limit=1000):
    return {"type": "string", "minLength": 1, "maxLength": limit}


def _array(items, maximum):
    return {"type": "array", "items": items, "maxItems": maximum}


def _object(properties, required=None):
    #所有对象都关闭额外字段，避免模型把权限、运行状态等宿主字段夹进报告。
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


BRIEF_PROPERTIES = {
    "constraints": _array(_text(), 10),
    "acceptance_criteria": _array(_text(), 10),
    "known_facts": _array(_text(), 10),
}

_PATH = _text()
_LINE = {"type": "integer", "minimum": 1}
_DETAILS = {
    "codebase": _object({
        "locations": _array(_object({
            "path": _PATH, "start_line": _LINE, "end_line": _LINE, "reason": _text(),
        }, ["path", "start_line", "reason"]), 12),
        "relationships": _array(_text(), 20),
        "uncertainties": _array(_text(), 10),
    }),
    "planning": _object({
        "steps": _array(_object({
            "description": _text(), "files": _array(_PATH, 20), "validation": _text(),
        }), 20),
        "risks": _array(_text(), 10),
        "open_questions": _array(_text(), 10),
    }),
    "implementation": _object({
        "changes": _array(_object({"path": _PATH, "description": _text()}), 30),
        "unverified": _array(_text(), 10),
        "risks": _array(_text(), 10),
    }),
    "review_research": _object({
        "findings": _array(_object({
            "severity": {"type": "string", "maxLength": 2, "enum": ["P0", "P1", "P2", "P3"]},
            "description": _text(), "basis": _text(), "path": _PATH,
            "start_line": _LINE, "end_line": _LINE,
        }, ["severity", "description", "basis"]), 20),
        "sources": _array(_text(2000), 20),
        "conclusion": _text(2000),
        "uncertainties": _array(_text(), 10),
    }),
}


def report_tool(role: str) -> dict:
    """角色由宿主选择；工具参数不允许模型另选角色或修改输出合同。"""
    if not isinstance(role, str) or role not in _DETAILS:
        raise ValueError("only a specialist role can submit a report")
    return {
        "name": "report_result",
        "description": (
            "Submit the final structured report for this delegated task. A valid report "
            "ends this delegation, not the user's Goal. Do not combine it with other tools. "
            "Report uncertainty honestly; schema validation does not prove correctness. "
            "Execution status, verification records and evidence references are host-owned."
        ),
        "input_schema": _object({"summary": _text(4000), "details": deepcopy(_DETAILS[role])}),
    }


def _schema_error(error):
    #ValidationError.message 可能回显错误值或绝对路径；只展示合同中的位置及规则。
    location = ".".join(str(part) for part in error.absolute_path) or "report"
    if error.validator == "required":
        missing = [name for name in error.validator_value if name not in error.instance]
        reason = "missing required fields: " + ", ".join(missing)
    elif error.validator == "additionalProperties":
        reason = "unexpected fields are not allowed"
    elif error.validator == "type":
        reason = "must have type " + str(error.validator_value)
    elif error.validator == "enum":
        reason = "must use one of the allowed values"
    else:
        reason = "violates " + str(error.validator)
    return f"{location}: {reason}"[:300]


def _check_path(value, workspace, location):
    """路径仅作代码引用；不读取文件，也不保证文件存在或报告内容真实。"""
    try:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("not a relative path")
        if not (workspace / path).resolve().is_relative_to(workspace):
            raise ValueError("link escapes workspace")
    except (OSError, RuntimeError, ValueError):
        #不把用户提交的越界路径放进错误文本，防止回显工作区外信息。
        raise ValueError(f"{location}: path must stay relative to the workspace") from None


def _check_location(item, workspace, location, *, optional=False):
    #JSON Schema 的 integer 接受 1.0；行号沿用本项目的严格 int 约定。
    for name in ("start_line", "end_line"):
        if name in item and type(item[name]) is not int:
            raise ValueError(f"{location}.{name}: line number must be an integer")
    if "path" in item:
        _check_path(item["path"], workspace, location + ".path")
    if optional and ("start_line" in item or "end_line" in item) and "path" not in item:
        raise ValueError(f"{location}: line numbers require a path")
    if "end_line" in item:
        if "start_line" not in item:
            raise ValueError(f"{location}: end_line requires start_line")
        if item["end_line"] < item["start_line"]:
            raise ValueError(f"{location}: end_line must not precede start_line")


def validate_report(role: str, payload: dict, workspace: Path) -> dict:
    """验证固定角色的报告后返回独立副本；不接受模型填写宿主执行事实。"""
    definition = report_tool(role)
    #空 Registry 禁止未来的外部 schema 引用偷偷发出网络请求。
    validator = Draft202012Validator(definition["input_schema"], registry=Registry())
    error = next(validator.iter_errors(payload), None)
    if error is not None:
        raise ValueError(_schema_error(error))
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("report must contain JSON values") from None
    if len(encoded) > MAX_REPORT_CHARS:
        raise ValueError(f"report exceeds {MAX_REPORT_CHARS} characters; shorten the report")

    root = Path(workspace).resolve()
    details = payload["details"]
    if role == "codebase":
        for index, item in enumerate(details["locations"]):
            _check_location(item, root, f"details.locations.{index}")
    elif role == "planning":
        for index, step in enumerate(details["steps"]):
            for number, path in enumerate(step["files"]):
                _check_path(path, root, f"details.steps.{index}.files.{number}")
    elif role == "implementation":
        for index, change in enumerate(details["changes"]):
            _check_path(change["path"], root, f"details.changes.{index}.path")
    else:
        for index, finding in enumerate(details["findings"]):
            _check_location(finding, root, f"details.findings.{index}", optional=True)
    return deepcopy(payload)
