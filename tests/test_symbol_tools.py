"""Python 语法定位的有界、只读回归；临时目录中的源码不会被执行。"""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from coding_agent.runtime.project_files import ProjectFiles
from coding_agent.runtime.turn import TurnCancelled, TurnContext, turn_context
from coding_agent.tools import symbols
from coding_agent.tools.symbols import SymbolTools


class SymbolToolsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.project_files = ProjectFiles(self.root)
        self.tools = SymbolTools(self.root, self.project_files)

    def fixture(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def find(self, symbol, **kwargs):
        return json.loads(self.tools.find_symbol(symbol, **kwargs))

    def test_definitions_preserve_qualified_names_kinds_and_real_line_ranges(self):
        self.fixture("main.py", (
            "@decorate()\n"
            "class Service:\n"
            "    async def run(self):\n"
            "        def nested():\n"
            "            return helper()\n"
            "        return nested()\n"
            "\n"
            "async def start():\n"
            "    return Service()\n"
        ))
        result = json.loads(self.tools.list_symbols("main.py"))
        definitions = {item["qualified_name"]: item for item in result["definitions"]}
        self.assertEqual(set(definitions), {"Service", "Service.run", "Service.run.nested", "start"})
        self.assertEqual(definitions["Service"]["start_line"], 2)
        self.assertEqual(definitions["Service"]["end_line"], 6)
        self.assertEqual(definitions["Service.run"]["kind"], "async_method")
        self.assertEqual(definitions["Service.run.nested"]["kind"], "function")
        self.assertEqual(definitions["start"]["kind"], "async_function")
        decorator = next(item for item in result["calls"] if item["name"] == "decorate")
        self.assertEqual(decorator["scope"], "")
        helper = next(item for item in result["calls"] if item["name"] == "helper")
        self.assertEqual(helper["scope"], "Service.run.nested")
        self.assertEqual(result["analysis"], "syntax_only")
        self.assertFalse(result["truncated"])

    def test_find_matches_exact_names_and_dotted_syntax_without_resolving_receiver_types(self):
        self.fixture("main.py", (
            "class Service:\n"
            "    def run(self):\n"
            "        self.run()\n"
            "        other.run()\n"
            "        factory().run()\n"
            "def runner():\n"
            "    return 1\n"
        ))
        simple = self.find("run")
        self.assertEqual(len(simple["definitions"]), 1)
        self.assertEqual(len(simple["calls"]), 3)
        self.assertTrue(all(item["name"] == "run" for item in simple["calls"]))
        qualified = self.find("Service.run")
        self.assertEqual(len(qualified["definitions"]), 1)
        self.assertEqual(qualified["calls"], [])
        receiver = self.find("self.run")
        self.assertEqual(receiver["definitions"], [])
        self.assertEqual(len(receiver["calls"]), 1)
        factory_call = next(item for item in simple["calls"] if item["qualified_name"] == "run")
        self.assertTrue(factory_call["receiver_unresolved"])
        self.assertIn("receiver types", simple["notice"])
        self.assertEqual(self.find("ru")["definitions"], [])

    def test_list_query_is_case_sensitive_substring_and_does_not_execute_source(self):
        self.fixture("danger.py", (
            "from pathlib import Path\n"
            "Path('MUST_NOT_EXIST').touch()\n"
            "def RunNow():\n    pass\n"
            "def run_later():\n    pass\n"
        ))
        result = json.loads(self.tools.list_symbols("danger.py", query="Run"))
        self.assertEqual([item["name"] for item in result["definitions"]], ["RunNow"])
        self.assertFalse((self.root / "MUST_NOT_EXIST").exists())

    def test_shared_ignore_rules_and_explicit_file_restrictions_are_preserved(self):
        self.fixture(".gitignore", "hidden.py\ngenerated/\n")
        self.fixture(".agentignore", "private.py\n")
        for path in ("main.py", "hidden.py", "private.py", "generated/deep.py", "node_modules/tool.py"):
            self.fixture(path, "def wanted():\n    pass\n")
        result = self.find("wanted")
        self.assertEqual([item["path"] for item in result["definitions"]], ["main.py"])
        ignored = json.loads(self.tools.list_symbols("hidden.py"))
        self.assertEqual(ignored["definitions"], [])
        self.assertIn("ignored", " ".join(ignored["warnings"]))
        self.assertIs(self.tools.project_files, self.project_files)

    def test_next_query_refreshes_code_and_ignore_rules(self):
        target = self.fixture("main.py", "def old():\n    pass\n")
        self.assertEqual(len(self.find("old")["definitions"]), 1)
        target.write_text("def updated():\n    pass\n", encoding="utf-8")
        self.assertEqual(self.find("old")["definitions"], [])
        self.assertEqual(len(self.find("updated")["definitions"]), 1)
        self.fixture(".agentignore", "main.py\n")
        self.assertEqual(self.find("updated")["definitions"], [])

    def test_other_languages_and_parse_failures_are_explicit(self):
        self.fixture("main.ts", "function wanted() {}")
        self.fixture("broken.py", "def wanted(:\n")
        result = self.find("wanted")
        self.assertEqual(result["definitions"], [])
        self.assertEqual(result["unsupported_files"], 1)
        self.assertTrue(result["truncated"])
        warning = " ".join(result["warnings"])
        self.assertIn("SyntaxError", warning)
        self.assertIn("Non-Python", warning)
        self.assertIn("Python", self.tools.list_symbols("main.ts"))
        self.assertTrue(self.tools.list_symbols(".").startswith("Error:"))

    def test_pep263_encoding_and_type_stub_files_are_supported(self):
        target = self.root / "encoded.py"
        target.write_bytes("# coding: latin-1\n# café\ndef wanted():\n    pass\n".encode("latin-1"))
        self.fixture("types.pyi", "def wanted() -> str: ...\n")
        result = self.find("wanted")
        self.assertEqual({item["path"] for item in result["definitions"]}, {"encoded.py", "types.pyi"})
        self.assertFalse(result["truncated"])

    def test_symbol_result_limit_and_file_budget_mark_partial_scan(self):
        for number in range(4):
            self.fixture(f"{number}.py", "def wanted():\n    pass\n")
        limited = self.find("wanted", max_results=1)
        self.assertEqual(len(limited["definitions"]), 1)
        self.assertTrue(limited["truncated"])
        with patch.object(symbols, "MAX_SYMBOL_FILES", 2):
            scanned = self.find("absent")
        self.assertEqual(scanned["files_scanned"], 2)
        self.assertTrue(scanned["truncated"])
        self.assertIn("scan budget", " ".join(scanned["warnings"]))

    def test_byte_and_ast_budgets_are_bounded_and_reported(self):
        self.fixture("main.py", "def wanted():\n    return sum((1, 2, 3))\n")
        with patch.object(symbols, "MAX_SYMBOL_BYTES", 10):
            result = self.find("wanted")
        self.assertEqual(result["files_parsed"], 0)
        self.assertTrue(result["truncated"])
        with patch.object(symbols, "MAX_AST_NODES", 2):
            result = self.find("wanted")
        self.assertTrue(result["truncated"])
        self.assertIn("AST node budget", " ".join(result["warnings"]))
        with patch.object(symbols, "MAX_FILE_BYTES", 10):
            result = self.find("wanted")
        self.assertTrue(result["truncated"])
        self.assertIn("ValueError", " ".join(result["warnings"]))

    def test_ast_entry_and_json_output_limits_preserve_explicit_incompleteness(self):
        self.fixture("main.py", "def first():\n    first()\ndef second():\n    second()\n")
        with patch.object(symbols, "MAX_AST_ENTRIES", 1):
            result = self.find("second")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["definitions"], [])
        with patch.object(symbols, "MAX_SYMBOL_OUTPUT", 750):
            output = self.tools.list_symbols("main.py")
        self.assertLessEqual(len(output), 750)
        self.assertTrue(json.loads(output)["truncated"])

    def test_unsafe_paths_links_and_scan_time_fifo_swap_cannot_be_read(self):
        target = self.fixture("main.py", "def wanted():\n    pass\n")
        (self.root / "link.py").symlink_to(target)
        (self.root / "dir-link").symlink_to(self.root.parent, target_is_directory=True)
        for path in ("../outside.py", "link.py", "dir-link/outside.py"):
            with self.subTest(path=path):
                self.assertTrue(self.tools.find_symbol("wanted", path=path).startswith("Error:"))
        original = self.tools._read

        def replace_with_fifo(path):
            path.unlink()
            os.mkfifo(path)
            return original(path)

        if hasattr(os, "mkfifo"):
            with patch.object(self.tools, "_read", side_effect=replace_with_fifo):
                result = self.find("wanted", path="main.py")
            self.assertTrue(result["truncated"])
            self.assertEqual(result["files_parsed"], 0)

    def test_source_symlink_swap_after_discovery_is_rejected(self):
        target = self.fixture("main.py", "def wanted():\n    pass\n")
        other = self.fixture("other.txt", "def unwanted():\n    pass\n")
        original = self.tools._read

        def swap(path):
            path.unlink()
            path.symlink_to(other)
            return original(path)

        with patch.object(self.tools, "_read", side_effect=swap):
            result = self.find("wanted", path="main.py")
        self.assertTrue(target.is_symlink())
        self.assertTrue(result["truncated"])
        self.assertEqual(result["files_parsed"], 0)

    def test_invalid_arguments_fail_and_cancellation_propagates(self):
        self.fixture("main.py", "def wanted():\n    pass\n")
        for symbol in ("", "has space", "a..b", "x()", None):
            with self.subTest(symbol=symbol):
                self.assertTrue(self.tools.find_symbol(symbol).startswith("Error:"))
        for limit in (0, 101, True, 1.0):
            self.assertTrue(self.tools.find_symbol("wanted", max_results=limit).startswith("Error:"))
        turn = TurnContext()
        with turn_context(turn):
            turn.cancel()
            with self.assertRaises(TurnCancelled):
                self.tools.find_symbol("wanted")
        with patch.object(symbols, "check_cancelled", side_effect=TurnCancelled("Esc")):
            with self.assertRaises(TurnCancelled):
                self.tools.list_symbols("main.py")


if __name__ == "__main__":
    unittest.main()
