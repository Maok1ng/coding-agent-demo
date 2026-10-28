"""不调用模型：角色报告必须符合自己的结构，不能冒充宿主状态或越界引用。"""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from jsonschema import Draft202012Validator

from coding_agent.agents.contracts import BRIEF_PROPERTIES, MAX_REPORT_CHARS, report_tool, validate_report


class AgentContractTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.details = {
            "codebase": {"locations": [{"path": "src/app.py", "start_line": 2, "end_line": 4,
                                         "reason": "处理请求入口"}],
                         "relationships": ["main 调用 handle_request"], "uncertainties": []},
            "planning": {"steps": [{"description": "增加参数校验", "files": ["src/app.py"],
                                      "validation": "运行相关单元测试"}],
                         "risks": [], "open_questions": []},
            "implementation": {"changes": [{"path": "src/app.py", "description": "补充参数校验"}],
                               "unverified": ["尚未运行集成测试"], "risks": []},
            "review_research": {"findings": [{"severity": "P2", "description": "空参数未处理",
                                               "basis": "分支直接解引用参数", "path": "src/app.py",
                                               "start_line": 4}],
                                "sources": ["https://example.invalid/reference"],
                                "conclusion": "有一处需要修复", "uncertainties": []},
        }

    def payload(self, role):
        return {"summary": "本次委派的简短报告", "details": deepcopy(self.details[role])}

    def test_all_specialists_accept_their_own_report_and_return_a_copy(self):
        for role in self.details:
            with self.subTest(role=role):
                payload = self.payload(role)
                result = validate_report(role, payload, self.root)
                self.assertEqual(result, payload)
                self.assertIsNot(result, payload)
                self.assertIsNot(result["details"], payload["details"])

    def test_empty_collections_are_valid_and_do_not_imply_checks_passed(self):
        details = {
            "codebase": {"locations": [], "relationships": [], "uncertainties": []},
            "planning": {"steps": [], "risks": [], "open_questions": []},
            "implementation": {"changes": [], "unverified": [], "risks": []},
            "review_research": {"findings": [], "sources": [], "conclusion": "未发现有依据的问题",
                                "uncertainties": []},
        }
        for role, detail in details.items():
            with self.subTest(role=role):
                validate_report(role, {"summary": "无额外发现", "details": detail}, self.root)

    def test_other_roles_details_cannot_be_substituted(self):
        for role in self.details:
            other = next(name for name in self.details if name != role)
            with self.subTest(role=role), self.assertRaises(ValueError):
                validate_report(role, self.payload(other), self.root)

    def test_missing_fields_and_extra_nested_fields_are_rejected(self):
        for role in self.details:
            payload = self.payload(role)
            del payload["summary"]
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, "summary"):
                validate_report(role, payload, self.root)
        payload = self.payload("planning")
        payload["details"]["steps"][0]["command"] = "touch unexpected"
        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            validate_report("planning", payload, self.root)

    def test_host_owned_fields_cannot_be_supplied_in_a_report(self):
        for name, value in (("status", "completed"), ("verification", [{"exit_code": 0}]),
                            ("evidence_refs", ["forged"]), ("role", "implementation")):
            payload = self.payload("implementation")
            payload[name] = value
            with self.subTest(field=name), self.assertRaises(ValueError):
                validate_report("implementation", payload, self.root)
        payload = self.payload("implementation")
        payload["details"]["verification"] = [{"exit_code": 0}]
        with self.assertRaises(ValueError):
            validate_report("implementation", payload, self.root)

    def test_path_references_cannot_escape_or_disclose_bad_paths_in_errors(self):
        (self.root / "outside").symlink_to(self.root.parent, target_is_directory=True)
        setters = {
            "codebase": lambda detail, path: detail["locations"][0].update(path=path),
            "planning": lambda detail, path: detail["steps"][0].update(files=[path]),
            "implementation": lambda detail, path: detail["changes"][0].update(path=path),
            "review_research": lambda detail, path: detail["findings"][0].update(path=path),
        }
        for role, change in setters.items():
            for path in ("../private-secret.py", "/private-secret.py", "outside/private-secret.py", "bad\x00name"):
                payload = self.payload(role)
                change(payload["details"], path)
                with self.subTest(role=role, path=path), self.assertRaises(ValueError) as raised:
                    validate_report(role, payload, self.root)
                self.assertNotIn("private-secret", str(raised.exception))

    def test_relative_missing_paths_and_internal_links_remain_references(self):
        (self.root / "source").mkdir()
        (self.root / "alias").symlink_to(self.root / "source", target_is_directory=True)
        payload = self.payload("implementation")
        payload["details"]["changes"][0]["path"] = "alias/not-created-yet.py"
        validate_report("implementation", payload, self.root)

    def test_lines_are_positive_integers_and_ranges_are_ordered(self):
        for role, field in (("codebase", "locations"), ("review_research", "findings")):
            for start in (0, -1, True, 1.0, 1.5, "2"):
                payload = self.payload(role)
                payload["details"][field][0]["start_line"] = start
                with self.subTest(role=role, start=start), self.assertRaises(ValueError):
                    validate_report(role, payload, self.root)
            payload = self.payload(role)
            payload["details"][field][0].update(start_line=5, end_line=2)
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, "end_line"):
                validate_report(role, payload, self.root)

    def test_review_lines_require_a_path_and_end_requires_start(self):
        payload = self.payload("review_research")
        del payload["details"]["findings"][0]["path"]
        with self.assertRaisesRegex(ValueError, "require a path"):
            validate_report("review_research", payload, self.root)
        payload = self.payload("review_research")
        finding = payload["details"]["findings"][0]
        del finding["start_line"]
        finding["end_line"] = 5
        with self.assertRaisesRegex(ValueError, "requires start_line"):
            validate_report("review_research", payload, self.root)

    def test_budget_counts_the_whole_report_beyond_individual_fields(self):
        payload = self.payload("codebase")
        payload["details"]["relationships"] = ["x" * 900] * 20
        with self.assertRaisesRegex(ValueError, str(MAX_REPORT_CHARS)):
            validate_report("codebase", payload, self.root)
        payload = self.payload("planning")
        payload["summary"] = "x" * 4001
        with self.assertRaises(ValueError):
            validate_report("planning", payload, self.root)

    def test_collection_limits_and_severity_are_enforced(self):
        payload = self.payload("codebase")
        payload["details"]["locations"] *= 13
        with self.assertRaises(ValueError):
            validate_report("codebase", payload, self.root)
        payload = self.payload("review_research")
        payload["details"]["findings"][0]["severity"] = "critical"
        with self.assertRaises(ValueError):
            validate_report("review_research", payload, self.root)

    def test_unknown_roles_and_non_object_reports_are_rejected(self):
        for role in ("orchestrator", "invented", None, {}):
            with self.subTest(role=role), self.assertRaises(ValueError):
                report_tool(role)
        for value in (None, [], "report", 4):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_report("codebase", value, self.root)

    def test_tool_schemas_are_independent_of_external_mutation(self):
        first = report_tool("codebase")
        first["input_schema"]["properties"]["details"]["required"].clear()
        second = report_tool("codebase")
        self.assertIn("locations", second["input_schema"]["properties"]["details"]["required"])

    def test_brief_fields_are_optional_but_have_explicit_limits(self):
        validator = Draft202012Validator({"type": "object", "properties": BRIEF_PROPERTIES,
                                         "additionalProperties": False})
        self.assertTrue(validator.is_valid({}))
        self.assertTrue(validator.is_valid({"constraints": ["保留已有注释"],
                                           "acceptance_criteria": ["相关测试通过"], "known_facts": []}))
        for value in ("free text", [""], ["x"] * 11, ["x" * 1001]):
            self.assertFalse(validator.is_valid({"constraints": value}))


if __name__ == "__main__":
    unittest.main()
