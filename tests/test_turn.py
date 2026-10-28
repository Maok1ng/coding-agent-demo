"""Turn 取消边界与模型连接清理测试；只使用本地协程和假模型，不请求网络。"""

import asyncio
import copy
import threading
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from coding_agent.runtime import model
from coding_agent.runtime.turn import (
    TurnCancelled, TurnContext, active_turn, cancel_active_turn,
    check_cancelled, current_turn, lead_turn, run_cancellable_async, turn_context,
)


class TurnTests(unittest.TestCase):
    def test_cancel_callbacks_run_lifo_once_and_preserve_first_reason(self):
        turn = TurnContext()
        calls = []
        turn.add_cancel_callback(lambda: calls.append(("first", turn.cancelled.is_set())))
        turn.add_cancel_callback(lambda: calls.append(("second", turn.cancelled.is_set())))
        turn.cancel("first interruption")
        turn.cancel("second interruption")
        self.assertEqual(calls, [("second", True), ("first", True)])
        self.assertEqual(turn.reason, "first interruption")
        self.assertEqual(turn._callbacks, [])

    def test_removed_callback_is_not_called_and_removal_is_idempotent(self):
        turn = TurnContext()
        callback = Mock()
        remove = turn.add_cancel_callback(callback)
        remove()
        remove()
        turn.cancel()
        callback.assert_not_called()

    def test_callback_registered_after_cancellation_runs_immediately(self):
        turn = TurnContext()
        turn.cancel()
        callback = Mock()
        remove = turn.add_cancel_callback(callback)
        callback.assert_called_once_with()
        remove()
        turn.cancel()
        callback.assert_called_once_with()
        self.assertEqual(turn._callbacks, [])

    def test_callback_failure_does_not_skip_cleanup_or_expose_error_text(self):
        turn = TurnContext()
        calls = []
        turn.add_cancel_callback(lambda: calls.append("cleaned"))
        turn.add_cancel_callback(Mock(side_effect=ValueError("test-secret-must-not-be-saved")))
        turn.cancel()
        self.assertEqual(calls, ["cleaned"])
        self.assertEqual(turn.cleanup_errors, ["ValueError"])

    def test_steering_queue_is_fifo_and_injects_each_message_once(self):
        turn = TurnContext()
        turn.enqueue_steering("先看配置")
        turn.enqueue_steering("再跑测试")
        history = []
        self.assertTrue(turn.inject_steering(history))
        self.assertTrue(history[0]["content"].endswith("先看配置"))
        self.assertTrue(history[1]["content"].endswith("再跑测试"))
        self.assertFalse(turn.inject_steering(history))

    def test_context_restores_nested_turn_and_is_not_inherited_by_new_thread(self):
        outer, inner = TurnContext(), TurnContext()
        child_turns = []
        self.assertIsNone(current_turn())
        with turn_context(outer):
            self.assertIs(current_turn(), outer)
            with turn_context(inner):
                self.assertIs(current_turn(), inner)
            self.assertIs(current_turn(), outer)
            child = threading.Thread(target=lambda: child_turns.append(current_turn()))
            child.start()
            child.join(timeout=2)
            self.assertFalse(child.is_alive())
            self.assertEqual(child_turns, [None])
        self.assertIsNone(current_turn())

    def test_cancelled_context_raises_and_restores_previous_context(self):
        outer, cancelled = TurnContext(), TurnContext()
        cancelled.cancel("cancelled before entry")
        with turn_context(outer):
            with self.assertRaisesRegex(TurnCancelled, "cancelled before entry"):
                with turn_context(cancelled):
                    self.fail("cancelled turn must not begin")
            self.assertIs(current_turn(), outer)
        self.assertIsNone(current_turn())

    def test_lead_turn_is_globally_visible_but_context_is_thread_local(self):
        observed = []
        with lead_turn() as turn:
            self.assertIs(current_turn(), turn)
            self.assertIs(active_turn(), turn)
            self.assertFalse(turn.done.is_set())
            child = threading.Thread(target=lambda: observed.append((active_turn(), current_turn())))
            child.start()
            child.join(timeout=2)
            self.assertFalse(child.is_alive())
            self.assertEqual(observed, [(turn, None)])
        self.assertTrue(turn.done.is_set())
        self.assertIsNone(active_turn())
        self.assertIsNone(current_turn())

    def test_lead_turn_marks_done_after_inner_finally_even_when_interrupted(self):
        events = []
        with self.assertRaisesRegex(TurnCancelled, "stop current turn"):
            with lead_turn() as turn:
                try:
                    self.assertIs(cancel_active_turn("stop current turn"), turn)
                    check_cancelled()
                finally:
                    self.assertFalse(turn.done.is_set())
                    events.append("inner cleanup")
        self.assertEqual(events, ["inner cleanup"])
        self.assertTrue(turn.done.is_set())
        self.assertIsNone(active_turn())
        self.assertIsNone(current_turn())
        self.assertIsNone(cancel_active_turn())

    def test_second_lead_turn_cannot_replace_current_lead(self):
        with lead_turn() as first:
            with self.assertRaisesRegex(RuntimeError, "still running"):
                with lead_turn():
                    self.fail("overlapping lead turns must not start")
            self.assertIs(active_turn(), first)
            self.assertIs(current_turn(), first)
            self.assertFalse(first.done.is_set())
        self.assertTrue(first.done.is_set())

    def test_close_failure_records_error_and_stops_loop_before_lead_is_done(self):
        with lead_turn() as turn:
            runtime = turn.async_runtime()

            def fail_close(loop):
                self.assertIs(active_turn(), turn)
                self.assertFalse(turn.done.is_set())
                raise RuntimeError("private close failure")

            runtime.add_close_callback(fail_close)
        self.assertEqual(turn.cleanup_errors, ["RuntimeError"])
        self.assertFalse(runtime._thread.is_alive())
        self.assertTrue(runtime.loop.is_closed())
        self.assertTrue(turn.done.is_set())
        self.assertIsNone(active_turn())
        turn.close()
        with self.assertRaisesRegex(RuntimeError, "already closed"):
            turn.async_runtime()

    def test_close_interrupt_still_stops_loop_and_marks_lead_done(self):
        with self.assertRaises(KeyboardInterrupt):
            with lead_turn() as turn:
                runtime = turn.async_runtime()
                runtime.add_close_callback(Mock(side_effect=KeyboardInterrupt))
        self.assertFalse(runtime._thread.is_alive())
        self.assertTrue(runtime.loop.is_closed())
        self.assertTrue(turn.done.is_set())
        self.assertIsNone(active_turn())
        self.assertIsNone(current_turn())

    def test_close_waits_for_pending_task_cancellation_before_stopping_loop(self):
        cleaned = threading.Event()
        turn = TurnContext()

        async def pending():
            try:
                await asyncio.sleep(30)
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        async def operation():
            asyncio.create_task(pending())
            await asyncio.sleep(0)

        with turn_context(turn):
            run_cancellable_async(operation)
            runtime = turn.async_runtime()
        turn.close()
        self.assertTrue(cleaned.is_set())
        self.assertFalse(runtime._thread.is_alive())
        self.assertTrue(runtime.loop.is_closed())
        self.assertEqual(turn.cleanup_errors, [])

    def test_async_operation_returns_normally_and_unregisters_callback(self):
        turn = TurnContext()

        async def operation():
            await asyncio.sleep(0)
            return "finished"

        with turn_context(turn):
            self.assertEqual(run_cancellable_async(operation), "finished")
        self.assertEqual(turn._callbacks, [])
        turn.cancel()  #已结束的事件循环不应残留需要取消的 Task。
        turn.close()
        self.assertEqual(turn.cleanup_errors, [])

    def test_async_operation_cancellation_finishes_cleanup_before_returning(self):
        turn = TurnContext()
        started = threading.Event()
        cleaned = threading.Event()
        results = []

        async def operation():
            started.set()
            try:
                await asyncio.sleep(30)
            finally:
                #清理也包含 await，确认不是仅设置标记后就提前返回。
                await asyncio.sleep(0)
                cleaned.set()

        def run():
            with turn_context(turn):
                try:
                    run_cancellable_async(operation)
                except TurnCancelled as error:
                    results.append((str(error), cleaned.is_set()))

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(started.wait(timeout=2))
        turn.cancel("cancel async operation")
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [("cancel async operation", True)])
        self.assertEqual(turn._callbacks, [])
        turn.close()
        self.assertEqual(turn.cleanup_errors, [])

    def test_cancelled_turn_does_not_create_async_operation(self):
        turn = TurnContext()
        operation = Mock()
        with turn_context(turn):
            turn.cancel()
            with self.assertRaises(TurnCancelled):
                run_cancellable_async(operation)
        operation.assert_not_called()


class ModelCancellationTests(unittest.TestCase):
    def client(self):
        #仅创建 SDK 配置对象；所有真正请求都由 AsyncAnthropic 假对象拦截。
        client = model.Anthropic(
            api_key="test-key", auth_token="test-auth",
            base_url="https://example.invalid/v1", timeout=2.0, max_retries=1,
            default_headers={"X-Unit-Test": "local-only"}, default_query={"unit": "yes"},
        )
        self.addCleanup(client.close)
        return client

    def asynchronous_client(self):
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.messages.create = AsyncMock()
        client.close = AsyncMock(return_value=None)
        return client

    def test_model_copies_host_configuration_and_request_arguments_then_closes(self):
        client = self.client()
        asynchronous = self.asynchronous_client()
        response = object()
        asynchronous.messages.create.return_value = response
        arguments = {
            "model": "test-model", "max_tokens": 32, "system": "test system",
            "messages": [{"role": "user", "content": "local fixture only"}],
            "tools": [{"name": "example", "input_schema": {"type": "object"}}],
        }
        original = copy.deepcopy(arguments)
        turn = TurnContext()
        with patch.object(model, "AsyncAnthropic", return_value=asynchronous) as constructor:
            with turn_context(turn):
                self.assertIs(model.call_model(client, **arguments), response)
        turn.close()
        constructor.assert_called_once_with(
            api_key=client.api_key, auth_token=client.auth_token,
            base_url=client.base_url, timeout=client.timeout, max_retries=client.max_retries,
            default_headers=client.default_headers, default_query=client.default_query,
        )
        expected = copy.deepcopy(original)
        expected["system"] = [{"type": "text", "text": "test system",
                               "cache_control": {"type": "ephemeral"}}]
        expected["messages"] = [{"role": "user", "content": [{"type": "text", "text": "local fixture only",
                                                              "cache_control": {"type": "ephemeral"}}]}]
        expected["tools"] = [{"name": "example", "input_schema": {"type": "object"},
                              "cache_control": {"type": "ephemeral"}}]
        asynchronous.messages.create.assert_awaited_once_with(**expected)
        #连接池随回合关闭，同一回合内的后续请求不再重建客户端。
        asynchronous.close.assert_awaited_once_with()
        self.assertEqual(arguments, original)

    def test_second_request_in_same_turn_reuses_one_client(self):
        client = self.client()
        asynchronous = self.asynchronous_client()
        asynchronous.messages.create.return_value = object()
        turn = TurnContext()
        with patch.object(model, "AsyncAnthropic", return_value=asynchronous) as constructor:
            with turn_context(turn):
                model.call_model(client, model="test-model", messages=[], max_tokens=16)
                model.call_model(client, model="test-model", messages=[], max_tokens=16)
        turn.close()
        constructor.assert_called_once()
        self.assertEqual(asynchronous.messages.create.await_count, 2)
        asynchronous.close.assert_awaited_once_with()

    def test_cancel_model_request_stops_response_and_client_closes_at_turn_end(self):
        client = self.client()
        asynchronous = self.asynchronous_client()
        turn = TurnContext()
        started = threading.Event()
        closed = threading.Event()
        events = []
        results = []

        async def create(**arguments):
            started.set()
            try:
                await asyncio.sleep(30)
            finally:
                events.append("request stopped")

        async def close():
            await asyncio.sleep(0)
            events.append("connection closed")
            closed.set()

        asynchronous.messages.create.side_effect = create
        asynchronous.close = AsyncMock(side_effect=close)

        def call():
            with turn_context(turn):
                try:
                    model.call_model(client, model="test-model", messages=[], max_tokens=16)
                except TurnCancelled as error:
                    events.append("caller returned")
                    results.append(str(error))

        with patch.object(model, "AsyncAnthropic", return_value=asynchronous):
            worker = threading.Thread(target=call)
            worker.start()
            self.assertTrue(started.wait(timeout=2))
            turn.cancel("cancel model request")
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, ["cancel model request"])
        #响应任务在调用方返回前已停止；连接池本体按回合生命周期在 close 时关闭。
        self.assertEqual(events, ["request stopped", "caller returned"])
        asynchronous.messages.create.assert_awaited_once()
        self.assertEqual(turn._callbacks, [])
        turn.close()
        self.assertEqual(events, ["request stopped", "caller returned", "connection closed"])
        asynchronous.close.assert_awaited_once()

    def test_fake_client_keeps_sync_interface_without_creating_network_client(self):
        response = object()
        client = types.SimpleNamespace(messages=types.SimpleNamespace(create=Mock(return_value=response)))
        with patch.object(model, "AsyncAnthropic", side_effect=AssertionError("must not connect")):
            with turn_context(TurnContext()):
                self.assertIs(model.call_model(client, model="fake", messages=[]), response)
        client.messages.create.assert_called_once_with(model="fake", messages=[])

    def test_cancelled_turn_does_not_create_a_model_connection(self):
        client = self.client()
        turn = TurnContext()
        with patch.object(model, "AsyncAnthropic", side_effect=AssertionError("must not connect")):
            with turn_context(turn):
                turn.cancel()
                with self.assertRaises(TurnCancelled):
                    model.call_model(client, model="test-model", messages=[], max_tokens=16)


if __name__ == "__main__":
    unittest.main()
