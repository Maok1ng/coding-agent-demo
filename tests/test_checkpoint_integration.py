"""代码检查点的真实应用闭环；模型使用脚本响应，只操作临时工作区。"""

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from coding_agent.application import Application
from coding_agent.runtime import environment, tools, workspace
from coding_agent.runtime.model_settings import ModelSettings
from coding_agent.runtime.session import SessionStore
from coding_agent.runtime.turn import TurnCancelled, current_turn
from coding_agent.tools.files import FileTools
from tests.test_application import (
    FakeIO, ScriptedModel, dispatch, report_response, text_response, tool_call, tool_response,
)


class CheckpointIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        for patcher in (
            patch.object(workspace, "WORKDIR", self.root),
            patch("coding_agent.integrations.mcp.get_mcp_config", return_value={}),
            patch.object(environment, "get_environment", return_value=environment.LocalEnvironment(
                environment.ExecutionConfig(timeout=5))),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config = {"model": "fake-checkpoint-model", "max_tokens": 1000,
                       "max_model_calls": 30, "memory_enabled": False, "memory_auto_extract": False}
        self.io = FakeIO()
        self.path = self.root / "app.py"
        self.path.write_text("value = 1\n", encoding="utf-8")

    def application(self, responses=(), *, session=None):
        model = ScriptedModel(responses)
        app = Application(
            self.config, self.root, self.io, session=session, client=Mock(),
            model_call=model, evaluator=Mock(),
            model_settings=ModelSettings(self.config, path=self.root / "fake-model-settings.json"),
        )
        self.addCleanup(app.close)
        return app, model

    @staticmethod
    def edit_turn(*values, path="app.py", exists=True):
        #一个用户回合包含主角色、子角色及多次工具调用；它们必须共用检查点。
        responses = [dispatch()]
        if exists:
            responses.append(tool_response(tool_call("read_file", {"path": path}, "read-before-edit")))
        for number, value in enumerate(values, 1):
            responses.append(tool_response(tool_call("write_file", {
                "path": path, "content": value,
            }, f"write-{number}")))
        responses.extend([report_response(summary="文件已修改；未运行项目测试。"),
                          text_response("已完成本次修改，尚未验证。")])
        return responses

    @staticmethod
    def nodes(app):
        return [item for item in app.checkpoints.list_checkpoints() if item["id"] != "C0"]

    @staticmethod
    def head(app):
        return next(item["id"] for item in app.checkpoints.list_checkpoints() if item["is_head"])

    def test_all_edits_and_child_roles_in_one_turn_share_one_checkpoint(self):
        app, model = self.application(self.edit_turn("value = 2\n", "value = 3\n"))
        app.submit("连续修改两次 app.py")
        self.assertEqual(self.path.read_text(), "value = 3\n")
        self.assertEqual([item["id"] for item in self.nodes(app)], ["C1"])
        calls = len(model.calls)

        app.submit("/undo")

        self.assertEqual(self.path.read_text(), "value = 1\n")
        self.assertEqual(self.head(app), "C0")
        self.assertEqual(len(model.calls), calls)
        self.assertTrue(any(name == "restore_checkpoint" for name, _, _ in self.io.confirmations))

    def test_restore_selects_completed_turn_and_retains_old_branch(self):
        app, model = self.application()
        for value in (2, 3, 4):
            model.responses.extend(self.edit_turn(f"value = {value}\n"))
            app.submit(f"把值改成 {value}")
        self.assertEqual(self.head(app), "C3")
        calls = len(model.calls)

        app.submit("/restore C1")

        self.assertEqual(self.path.read_text(), "value = 2\n")
        self.assertEqual(self.head(app), "C1")
        self.assertEqual(len(model.calls), calls)
        self.assertEqual({item["id"] for item in self.nodes(app)}, {"C1", "C2", "C3"})
        model.responses.extend(self.edit_turn("value = 5\n"))
        app.submit("从恢复后的代码改成 5")
        nodes = {item["id"]: item for item in self.nodes(app)}
        self.assertEqual(self.head(app), "C4")
        self.assertEqual(nodes["C4"]["parent"], "C1")
        self.assertTrue({"C1", "C2", "C3", "C4"}.issubset(nodes))
        calls = len(model.calls)
        app.submit("/restore C3")
        self.assertEqual(self.path.read_text(), "value = 4\n")
        self.assertEqual(self.head(app), "C3")
        self.assertEqual(len(model.calls), calls)

    def test_list_and_plain_read_only_turn_do_not_create_modification_checkpoint(self):
        app, model = self.application([text_response("这是普通解释，没有修改代码。")])
        app.submit("解释 app.py")
        self.assertEqual(self.nodes(app), [])
        calls = len(model.calls)
        app.submit("/checkpoints")
        self.assertEqual(len(model.calls), calls)
        self.assertEqual(self.path.read_text(), "value = 1\n")
        self.assertEqual(self.nodes(app), [])

    def test_restore_equal_file_contents_still_selects_the_requested_checkpoint(self):
        app, model = self.application()
        for value in (2, 1):
            model.responses.extend(self.edit_turn(f"value = {value}\n"))
            app.submit(f"把值改成 {value}")
        self.assertEqual(self.path.read_text(), "value = 1\n")
        self.assertEqual(self.head(app), "C2")
        calls = len(model.calls)

        app.submit("/restore C0")

        self.assertEqual(self.path.read_text(), "value = 1\n")
        self.assertEqual(self.head(app), "C0")
        self.assertEqual(len(model.calls), calls)
        model.responses.extend(self.edit_turn("value = 3\n"))
        app.submit("从已恢复的基线修改")
        nodes = {item["id"]: item for item in self.nodes(app)}
        self.assertEqual(nodes["C3"]["parent"], "C0")

    def test_denied_and_failed_file_tools_do_not_create_checkpoint(self):
        app, model = self.application(self.edit_turn("value = 2\n"))
        app.approval_mode = "ask"
        with patch.object(self.io, "confirm", side_effect=lambda name, *_: name != "write_file"):
            app.submit("尝试修改文件")
        self.assertEqual(self.path.read_text(), "value = 1\n")
        self.assertEqual(self.nodes(app), [])
        model.responses.extend([
            dispatch(), tool_response(tool_call("write_file", {
                "path": "app.py", "content": "value = 3\n",
            }, "write-without-read")), report_response(), text_response("缺少前置读取，修改被拒绝。"),
        ])
        app.submit("再次尝试修改文件")
        self.assertEqual(self.path.read_text(), "value = 1\n")
        self.assertEqual(self.nodes(app), [])

    def test_new_file_can_be_undone_without_touching_existing_user_file(self):
        app, model = self.application(self.edit_turn("new = True\n", path="new.py", exists=False))
        app.submit("新建 new.py")
        self.assertEqual((self.root / "new.py").read_text(), "new = True\n")
        calls = len(model.calls)
        app.submit("/undo")
        self.assertFalse((self.root / "new.py").exists())
        self.assertEqual(self.path.read_text(), "value = 1\n")
        self.assertEqual(len(model.calls), calls)

    def test_restore_preview_precedes_confirmation_and_rejection_preserves_code_and_head(self):
        app, model = self.application(self.edit_turn("value = 2\n"))
        app.submit("修改 app.py")
        self.io.events.clear()
        calls = len(model.calls)

        def reject(name, arguments, reason):
            self.assertEqual(name, "restore_checkpoint")
            self.assertEqual(self.path.read_text(), "value = 2\n")
            previews = [event for event in self.io.events if event["type"] == "change_preview"]
            self.assertTrue(previews)
            self.assertIn("-value = 2", previews[0]["diff"])
            self.assertIn("+value = 1", previews[0]["diff"])
            return False

        with patch.object(self.io, "confirm", side_effect=reject):
            app.submit("/undo")
        self.assertEqual(self.path.read_text(), "value = 2\n")
        self.assertEqual(self.head(app), "C1")
        self.assertEqual(len(model.calls), calls)

    def test_manual_changes_and_changes_during_consent_are_never_overwritten(self):
        app, model = self.application(self.edit_turn("value = 2\n"))
        app.submit("修改 app.py")
        self.path.write_text("manual = True\n", encoding="utf-8")
        calls = len(model.calls)
        confirmations = len(self.io.confirmations)
        app.submit("/undo")
        self.assertEqual(self.path.read_text(), "manual = True\n")
        self.assertEqual(self.head(app), "C1")
        self.assertEqual(len(self.io.confirmations), confirmations)
        self.assertEqual(len(model.calls), calls)
        self.path.write_text("value = 2\n", encoding="utf-8")

        def alter_during_consent(*args):
            self.path.write_text("manual = 'during consent'\n", encoding="utf-8")
            return True

        with patch.object(self.io, "confirm", side_effect=alter_during_consent):
            app.submit("/undo")
        self.assertEqual(self.path.read_text(), "manual = 'during consent'\n")
        self.assertEqual(self.head(app), "C1")
        self.assertEqual(len(model.calls), calls)

    def test_plan_mode_refuses_restore_but_can_list_checkpoints_without_model(self):
        app, model = self.application(self.edit_turn("value = 2\n"))
        app.submit("修改 app.py")
        app.plan.enter("先分析")
        calls = len(model.calls)
        confirmations = len(self.io.confirmations)
        for command in ("/checkpoints", "/undo", "/restore C0"):
            app.submit(command)
        self.assertEqual(self.path.read_text(), "value = 2\n")
        self.assertEqual(self.head(app), "C1")
        self.assertEqual(app.plan.mode, "plan")
        self.assertEqual(len(self.io.confirmations), confirmations)
        self.assertEqual(len(model.calls), calls)

    def test_restore_invalidates_approval_and_pending_question_and_pauses_goal(self):
        app, model = self.application(self.edit_turn("value = 2\n"))
        app.submit("修改 app.py")
        app.goal.set_goal("继续之前的长期任务")
        app.plan.approved_event = "old-approval"
        app.goal.user_input.ask("继续修改哪个文件？", ["app.py", "tests.py"])
        app.pending_goal_execution = True
        history = copy.deepcopy(app.messages)
        calls = len(model.calls)

        app.submit("/undo")

        self.assertEqual(app.goal.active.status, "paused")
        self.assertIsNone(app.plan.approved_event)
        self.assertIsNone(app.goal.user_input.pending)
        self.assertFalse(app.pending_goal_execution)
        self.assertIsNone(app.last_context)
        self.assertEqual(app.messages[:len(history)], history)
        notices = "\n".join(str(item["content"]) for item in app.messages[len(history):])
        self.assertTrue(any(marker in notices.lower() for marker in ("verification", "revalidate", "验证", "重验")))
        self.assertEqual(len(model.calls), calls)

    def test_cancel_after_file_write_preserves_a_restorable_checkpoint(self):
        app, model = self.application(self.edit_turn("value = 2\n"))
        original = tools.trigger_hook

        def interrupt_after_write(kind, block, *args):
            if kind == "PostToolUse" and block.name == "write_file":
                current_turn().cancel("test Esc after write")
                raise TurnCancelled("test Esc after write")
            return original(kind, block, *args)

        with patch.object(tools, "trigger_hook", side_effect=interrupt_after_write):
            with self.assertRaises(TurnCancelled):
                app.submit("修改文件后立即中断")
        self.assertEqual(self.path.read_text(), "value = 2\n")
        self.assertEqual(len(self.nodes(app)), 1)
        calls = len(model.calls)
        app.submit("/undo")
        self.assertEqual(self.path.read_text(), "value = 1\n")
        self.assertEqual(len(model.calls), calls)

    def test_model_failure_after_file_write_preserves_a_restorable_checkpoint(self):
        def fail_model(**kwargs):
            raise RuntimeError("scripted model failure")

        responses = self.edit_turn("value = 2\n")
        #子角色正常提交后让主角色请求失败，避免派发工具把子角色异常转成可处理的工具错误。
        responses[-1:] = [fail_model]
        app, model = self.application(responses)
        with self.assertRaises(RuntimeError):
            app.submit("先修改文件，再遇到模型异常")
        self.assertEqual(self.path.read_text(), "value = 2\n")
        self.assertEqual(len(self.nodes(app)), 1)
        calls = len(model.calls)
        app.submit("/undo")
        self.assertEqual(self.path.read_text(), "value = 1\n")
        self.assertEqual(len(model.calls), calls)

    def test_cancel_between_disk_write_and_checkpoint_commit_is_reconciled(self):
        app, model = self.application(self.edit_turn("value = 2\n"))
        apply_change = FileTools.apply_change

        def interrupt_after_apply(files, change):
            apply_change(files, change)
            current_turn().cancel("test Esc between disk write and checkpoint commit")
            raise TurnCancelled("test Esc between disk write and checkpoint commit")

        with patch.object(FileTools, "apply_change", interrupt_after_apply):
            with self.assertRaises(TurnCancelled):
                app.submit("在代码落盘后立即中断")
        self.assertEqual(self.path.read_text(), "value = 2\n")
        self.assertEqual(len(self.nodes(app)), 1)
        calls = len(model.calls)
        app.submit("/undo")
        self.assertEqual(self.path.read_text(), "value = 1\n")
        self.assertEqual(len(model.calls), calls)

    def test_persisted_session_restores_checkpoint_catalog_without_replaying_model(self):
        session = SessionStore(self.root).open()
        self.addCleanup(session.close)
        app, model = self.application(self.edit_turn("value = 2\n"), session=session)
        app.submit("修改 app.py")
        session_id = session.id
        app.close()
        session.close()
        restored = SessionStore(self.root).open(session_id)
        self.addCleanup(restored.close)
        resumed, fresh_model = self.application(session=restored)
        self.assertEqual(fresh_model.calls, [])
        resumed.submit("/checkpoints")
        self.assertEqual(self.head(resumed), "C1")
        resumed.submit("/undo")
        self.assertEqual(self.path.read_text(), "value = 1\n")
        self.assertEqual(self.head(resumed), "C0")
        self.assertEqual(fresh_model.calls, [])

    def test_model_cannot_invoke_host_restore_command_as_a_tool(self):
        app, model = self.application([tool_response(tool_call("restore_checkpoint", {
            "checkpoint": "C0",
        }, "forged-restore")), text_response("恢复必须由真实用户发起。")])
        app.submit("说明检查点")
        result = model.calls[-1]["messages"][-1]["content"][0]
        self.assertTrue(result["is_error"])
        self.assertEqual(self.path.read_text(), "value = 1\n")
        self.assertFalse(any(name == "restore_checkpoint" for name, _, _ in self.io.confirmations))
        self.assertEqual(self.nodes(app), [])


if __name__ == "__main__":
    unittest.main()
