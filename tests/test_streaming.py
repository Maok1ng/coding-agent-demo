"""SSE 增量、完整工具协议、连接取消和 CLI 去重；不发送真实模型请求。"""

import asyncio
from contextlib import redirect_stdout
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from coding_agent.agents import runner as runner_module
from coding_agent.agents.roles import AgentTask
from coding_agent.agents.runner import AgentRunner
from coding_agent.cli import TerminalInteraction
from coding_agent.application import Application
from coding_agent.runtime import model
from coding_agent.runtime.context import RunContext
from coding_agent.runtime.session import SessionStore
from coding_agent.runtime.turn import TurnCancelled, TurnContext, check_cancelled, turn_context
from tests.test_application import FakeIO, text_response, tool_call, tool_response


class StreamingModelTests(unittest.TestCase):
    def test_multiple_text_blocks_preserve_boundaries_and_ignore_other_content(self):
        pieces, blocks = [], [0]
        for text in ("first", "second"):
            model._stream_event(SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(
                type="text", text="")), pieces.append, blocks)
            model._stream_event(SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(
                type="text_delta", text=text)), pieces.append, blocks)
        self.assertEqual("".join(pieces), "first\nsecond")

    def test_sync_stream_emits_only_text_and_returns_complete_tool_blocks(self):
        final = tool_response(tool_call("write_file", {"path": "a.py", "content": "complete"}))
        stream = MagicMock()
        stream.__enter__.return_value = stream
        stream.__iter__.return_value = iter([
            SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="读取")),
            SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="input_json_delta", partial_json='{"path":')),
            SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="thinking_delta", thinking="private")),
            SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="完成")),
            SimpleNamespace(type="message_stop"),
        ])
        stream.get_final_message.return_value = final
        client = SimpleNamespace(messages=SimpleNamespace(stream=Mock(return_value=stream), create=Mock()))
        pieces = []
        self.assertIs(model.call_model(client, on_text=pieces.append, model="fake", messages=[]), final)
        self.assertEqual(pieces, ["读取", "完成"])
        client.messages.create.assert_not_called()
        client.messages.stream.assert_called_once_with(model="fake", messages=[])
        stream.get_final_message.assert_called_once()
        stream.__exit__.assert_called_once_with(None, None, None)

    def test_prompt_cache_breakpoints_cover_tools_system_and_last_message_without_mutating_history(self):
        history = [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "data"}]},
        ]
        tools = [{"name": "bash", "input_schema": {}}]
        client = SimpleNamespace(messages=SimpleNamespace(create=Mock(return_value=SimpleNamespace(content=[]))))
        model.call_model(client, model="fake", system="system prompt", messages=history, tools=tools)
        request = client.messages.create.call_args.kwargs
        self.assertEqual(request["tools"][-1]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(request["system"], [{"type": "text", "text": "system prompt",
                                              "cache_control": {"type": "ephemeral"}}])
        self.assertEqual(request["messages"][-1]["content"][-1]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(request["messages"][0]["content"], [{"type": "text", "text": "task"}])
        self.assertEqual(sum("cache_control" in block for message in request["messages"]
                             if isinstance(message.get("content"), list)
                             for block in message["content"]), 1)
        self.assertEqual(history[0]["content"], "task")
        self.assertNotIn("cache_control", str(history))

    def test_usage_metrics_accumulate_across_calls_on_one_turn(self):
        turn = TurnContext()
        response = SimpleNamespace(content=[], usage=SimpleNamespace(
            input_tokens=100, cache_read_input_tokens=800,
            cache_creation_input_tokens=100, output_tokens=50))
        client = SimpleNamespace(messages=SimpleNamespace(create=Mock(return_value=response)))
        with turn_context(turn):
            model.call_model(client, model="fake", messages=[])
            model.call_model(client, model="fake", messages=[])
        self.assertEqual(turn.metrics, {"model_calls": 2, "input_tokens": 200,
                                        "cache_read_tokens": 1600, "cache_creation_tokens": 200,
                                        "output_tokens": 100})

    def test_usage_metrics_skip_responses_without_usage_data(self):
        turn = TurnContext()
        client = SimpleNamespace(messages=SimpleNamespace(create=Mock(
            return_value=SimpleNamespace(content=[]))))
        with turn_context(turn):
            model.call_model(client, model="fake", messages=[])
        self.assertEqual(turn.metrics, {})

    def test_cache_false_skips_breakpoints_for_one_shot_calls(self):
        history = [{"role": "user", "content": "summarize this"}]
        client = SimpleNamespace(messages=SimpleNamespace(create=Mock(
            return_value=SimpleNamespace(content=[]))))
        model.call_model(client, model="fake", system="judge prompt",
                         messages=history, cache=False)
        request = client.messages.create.call_args.kwargs
        self.assertNotIn("cache_control", str(request))

    def client(self):
        client = model.Anthropic(api_key="fake-test-key", base_url="https://example.invalid", max_retries=0)
        self.addCleanup(client.close)
        return client

    def asynchronous(self, text_stream, final, *, stopped=True):
        stream = MagicMock()
        stream.__aenter__ = AsyncMock(return_value=stream)
        stream.__aexit__ = AsyncMock(return_value=False)
        async def events():
            async for text in text_stream:
                yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=text))
            if stopped:
                yield SimpleNamespace(type="message_stop")

        stream.__aiter__.side_effect = events
        stream.get_final_message = AsyncMock(return_value=final)
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.messages.stream.return_value = stream
        client.messages.create = AsyncMock()
        client.close = AsyncMock(return_value=None)
        return client, stream

    def test_async_stream_emits_before_final_response_and_closes_connections(self):
        client = self.client()
        final = text_response("你好")
        timeline = []
        turn = TurnContext()

        async def texts():
            yield "你"
            await asyncio.sleep(0)
            yield "好"

        asynchronous, stream = self.asynchronous(texts(), final)
        stream.get_final_message.side_effect = lambda: timeline.append("final") or final
        with patch.object(model, "AsyncAnthropic", return_value=asynchronous), turn_context(turn):
            response = model.call_model(client, on_text=timeline.append, model="fake", messages=[], max_tokens=20)
        turn.close()
        self.assertIs(response, final)
        self.assertEqual(timeline, ["你", "好", "final"])
        asynchronous.messages.create.assert_not_awaited()
        stream.__aexit__.assert_awaited_once()
        asynchronous.close.assert_awaited_once()

    def test_one_turn_keeps_different_host_credentials_and_endpoints_in_separate_pools(self):
        first_host = self.client()
        second_host = model.Anthropic(api_key="second-fake-key", base_url="https://second.invalid", max_retries=0)
        self.addCleanup(second_host.close)
        turn = TurnContext()

        async def texts(value):
            yield value

        first, _ = self.asynchronous(texts("first endpoint"), text_response("first result"))
        second, _ = self.asynchronous(texts("second endpoint"), text_response("second result"))
        first.messages.create.return_value = text_response("first reused")
        pieces = []
        with patch.object(model, "AsyncAnthropic", side_effect=[first, second]) as factory, turn_context(turn):
            model.call_model(first_host, on_text=pieces.append, model="one", messages=[], max_tokens=20)
            model.call_model(second_host, on_text=pieces.append, model="two", messages=[], max_tokens=20)
            reused = model.call_model(first_host, model="one-again", messages=[], max_tokens=20)
        self.assertEqual(pieces, ["first endpoint", "second endpoint"])
        self.assertEqual(factory.call_count, 2)
        calls = factory.call_args_list
        self.assertEqual(calls[0].kwargs["api_key"], "fake-test-key")
        self.assertEqual(calls[1].kwargs["api_key"], "second-fake-key")
        self.assertEqual(str(calls[0].kwargs["base_url"]), "https://example.invalid")
        self.assertEqual(str(calls[1].kwargs["base_url"]), "https://second.invalid")
        self.assertEqual(reused.content[0]["text"], "first reused")
        first.messages.create.assert_awaited_once()
        second.messages.create.assert_not_awaited()
        turn.close()
        first.close.assert_awaited_once()
        second.close.assert_awaited_once()
        self.assertNotIn(turn, model._turn_clients)

    def test_cancel_mid_stream_closes_sse_and_defers_client_close_to_turn_end(self):
        client = self.client()
        turn, started = TurnContext(), threading.Event()
        pieces, outcomes = [], []

        async def texts():
            yield "已经展示的部分"
            started.set()
            await asyncio.sleep(30)
            yield "不应该出现"

        asynchronous, stream = self.asynchronous(texts(), text_response("not complete"))

        def worker():
            with turn_context(turn):
                try:
                    model.call_model(client, on_text=pieces.append, model="fake", messages=[], max_tokens=20)
                except TurnCancelled:
                    outcomes.append(stream.__aexit__.await_count)

        with patch.object(model, "AsyncAnthropic", return_value=asynchronous):
            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(started.wait(timeout=2))
            turn.cancel("stop streaming")
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(pieces, ["已经展示的部分"])
        #取消立即收尾 SSE；连接池本体保留到回合 close 时统一关闭。
        self.assertEqual(outcomes, [1])
        stream.get_final_message.assert_not_awaited()
        self.assertEqual(turn._callbacks, [])
        turn.close()
        asynchronous.close.assert_awaited_once()

    def test_eof_without_message_stop_is_not_a_complete_response(self):
        client = self.client()
        turn = TurnContext()

        async def texts():
            yield "partial"

        partial = tool_response(tool_call("write_file", {"path": "a.py", "content": "bad"}))
        asynchronous, stream = self.asynchronous(texts(), partial, stopped=False)
        with patch.object(model, "AsyncAnthropic", return_value=asynchronous), \
                turn_context(turn), self.assertRaises(ConnectionError):
            model.call_model(client, on_text=Mock(), model="fake", messages=[], max_tokens=20)
        turn.close()
        stream.get_final_message.assert_not_awaited()
        stream.__aexit__.assert_awaited_once()
        asynchronous.close.assert_awaited_once()


class StreamingRunnerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        patcher = patch("coding_agent.integrations.mcp.get_mcp_config", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.context = RunContext(self.root, TurnContext(), role="implementation", memory_home=self.root / "host")
        self.io = FakeIO()
        self.config = {"model": "fake", "max_tokens": 100}

    def run_model(self, function, history):
        with patch.object(runner_module, "call_model", side_effect=function):
            runner = AgentRunner(Mock(), self.config, self.io)
        with turn_context(self.context.turn):
            return runner.run(AgentTask("implementation", "implement a small change"), self.context, messages=history)

    def test_steering_enters_history_before_next_model_request(self):
        self.context.turn.enqueue_steering("改用方案 B")
        seen = []

        def request(client, *, on_text, **arguments):
            seen.append(list(arguments["messages"]))
            return text_response("done")

        result = self.run_model(request, [{"role": "user", "content": "create new.py"}])

        self.assertEqual(result.status, "completed")
        self.assertTrue(seen[0][-1]["content"].endswith("改用方案 B"))
        self.assertIn("USER_PROMPT", seen[0][-1]["tags"])
        self.assertEqual(self.context.turn.finish_inputs(), ([], []))

    def test_tools_wait_for_complete_message_and_text_not_tool_json_is_streamed(self):
        calls = []
        path = self.root / "new.py"

        def request(client, *, on_text, **arguments):
            calls.append(arguments)
            if len(calls) == 1:
                on_text("准备修改")
                self.assertFalse(path.exists())
                self.assertEqual(len(history), 1)
                return tool_response(tool_call("write_file", {"path": "new.py", "content": "value = 1"}))
            self.assertEqual(path.read_text(), "value = 1")
            on_text("已完成")
            return text_response("已完成")

        history = [{"role": "user", "content": "create new.py"}]
        result = self.run_model(request, history)
        self.assertEqual(result.status, "completed")
        self.assertEqual([event["text"] for event in self.io.events if event["type"] == "text_delta"],
                         ["准备修改", "已完成"])
        self.assertEqual(history[1]["content"][0]["type"], "tool_use")
        self.assertEqual(history[2]["content"][0]["type"], "tool_result")
        self.assertTrue(all(event["status"] == "completed" for event in self.io.events if event["type"] == "text_end"))

    def test_interrupted_partial_text_is_saved_without_tool_execution(self):
        def request(client, *, on_text, **arguments):
            on_text("目前的分析")
            self.context.turn.cancel("cancel halfway through response")
            check_cancelled()
            return tool_response(tool_call("write_file", {"path": "forbidden.py", "content": "bad"}))

        history = [{"role": "user", "content": "modify"}]
        with self.assertRaises(TurnCancelled):
            self.run_model(request, history)
        self.assertFalse((self.root / "forbidden.py").exists())
        self.assertEqual(history[-1]["content"][0]["type"], "text")
        self.assertIn("目前的分析", history[-1]["content"][0]["text"])
        self.assertIn("响应未完整接收", history[-1]["content"][0]["text"])
        self.assertNotIn("tool_use", str(history))
        self.assertEqual(next(event for event in self.io.events if event["type"] == "text_end")["status"], "interrupted")

    def test_connection_failure_keeps_partial_text_but_does_not_retry_or_call_tools(self):
        count = []

        def request(client, *, on_text, **arguments):
            count.append(1)
            on_text("partial answer")
            raise ConnectionError("fake connection failed")

        history = [{"role": "user", "content": "modify"}]
        with self.assertRaises(ConnectionError):
            self.run_model(request, history)
        self.assertEqual(len(count), 1)
        self.assertIn("partial answer", history[-1]["content"][0]["text"])
        self.assertEqual(self.context.turn.started_tools, set())

    def test_stream_can_be_disabled_without_changing_model_arguments(self):
        def request(client, **arguments):
            self.assertNotIn("on_text", arguments)
            return text_response("normal answer")

        self.config["stream"] = False
        self.run_model(request, [{"role": "user", "content": "explain"}])
        self.assertFalse(any(event["type"] in {"text_delta", "text_end"} for event in self.io.events))

    def test_max_tokens_response_cannot_execute_even_valid_tool_json(self):
        def request(client, *, on_text, **arguments):
            on_text("准备写入")
            response = tool_response(tool_call("write_file", {"path": "forbidden.py", "content": "bad"}))
            response.stop_reason = "max_tokens"
            return response

        history = [{"role": "user", "content": "modify"}]
        result = self.run_model(request, history)
        self.assertEqual(result.status, "limit")
        self.assertFalse((self.root / "forbidden.py").exists())
        self.assertNotIn("tool_use", str(history))
        self.assertIn("工具调用未执行", result.summary)

    def test_application_saves_partial_text_and_pauses_goal_on_stream_interrupt(self):
        session = SessionStore(self.root).open()
        self.addCleanup(session.close)

        def request(client, *, on_text, **arguments):
            on_text("用户已经看见的内容")
            raise TurnCancelled("cancel stream")

        with patch.object(runner_module, "call_model", side_effect=request):
            application = Application({**self.config, "memory_enabled": False}, self.root, self.io,
                                      session, client=Mock(), evaluator=Mock())
        self.addCleanup(application.close)
        with self.assertRaises(TurnCancelled):
            application.submit("/goal 完成修改")
        self.assertEqual(application.goal.active.status, "paused")
        self.assertTrue(application.last_context.turn.done.is_set())
        saved = session.store.load(session.id)
        self.assertIn("用户已经看见的内容", str(saved["messages"]))
        self.assertIn("Turn interrupted", saved["messages"][-1]["content"])
        self.assertEqual(saved["state"]["goal"]["status"], "paused")


class StreamingCLITests(unittest.TestCase):
    def delta(self, interface, text, request="request-1", role="orchestrator"):
        interface.emit({"type": "text_delta", "request_id": request, "role": role, "text": text})

    def end(self, interface, request="request-1", status="completed"):
        interface.emit({"type": "text_end", "request_id": request, "role": "orchestrator", "status": status})

    def test_deltas_display_immediately_and_final_answer_is_not_duplicated(self):
        interface, output = TerminalInteraction(), io.StringIO()
        with redirect_stdout(output):
            self.delta(interface, "hello")
            self.assertIn("hello", output.getvalue())
            self.delta(interface, " world")
            self.end(interface)
            interface.emit({"type": "answer", "text": "hello world"})
        self.assertEqual(output.getvalue().count("hello world"), 1)

    def test_cleanup_warning_after_stream_is_not_hidden(self):
        interface, output = TerminalInteraction(), io.StringIO()
        with redirect_stdout(output):
            self.delta(interface, "done")
            self.end(interface)
            interface.emit({"type": "answer", "text": "done\n检查未完成"})
        self.assertEqual(output.getvalue().count("done"), 1)
        self.assertIn("检查未完成", output.getvalue())

    def test_child_summary_does_not_suppress_parent_or_next_turn(self):
        interface, output = TerminalInteraction(), io.StringIO()
        with redirect_stdout(output):
            self.delta(interface, "child result", role="implementation")
            self.end(interface)
            interface.emit({"type": "answer", "text": "child result"})
            self.delta(interface, "parent result", request="request-2")
            self.end(interface, request="request-2")
            interface.emit({"type": "agent_started", "role": "orchestrator", "turn_id": "next"})
            interface.emit({"type": "answer", "text": "parent result"})
        self.assertEqual(output.getvalue().count("child result"), 2)
        self.assertEqual(output.getvalue().count("parent result"), 2)

    def test_interrupted_stream_and_terminal_controls_do_not_corrupt_prompt(self):
        interface, output = TerminalInteraction(), io.StringIO()
        with redirect_stdout(output):
            self.delta(interface, "partial\x1b[2J\rtext")
            self.end(interface, status="interrupted")
            interface.emit({"type": "notice", "text": "等待下一条消息"})
        self.assertNotIn("\x1b", output.getvalue())
        self.assertNotIn("\r", output.getvalue())
        self.assertIn("text\n等待下一条消息", output.getvalue())


if __name__ == "__main__":
    unittest.main()
