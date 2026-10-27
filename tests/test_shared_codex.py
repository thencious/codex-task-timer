from pathlib import Path
from collections import deque
import io
import json
import queue
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from core import Task
from shared_codex import BusyChat, ProxyClient, deliver_to_open_chat, monitor_existing_turn


class TransportTests(unittest.TestCase):
    def client(self):
        client = ProxyClient.__new__(ProxyClient)
        client.timeout = 1
        client.buffer = bytearray()
        client.chunks = queue.Queue()
        client.process = SimpleNamespace(stdin=io.BytesIO())
        return client

    def test_client_frames_are_masked_and_preserve_long_unicode_payloads(self):
        client = self.client()
        payload = ("café 日本語 " * 1000).encode("utf-8")
        client.frame(payload)
        frame = client.process.stdin.getvalue()
        self.assertEqual(frame[0], 0x81)
        self.assertEqual(frame[1], 0x80 | 126)
        length = struct.unpack("!H", frame[2:4])[0]
        mask = frame[4:8]
        decoded = bytes(value ^ mask[i % 4] for i, value in enumerate(frame[8:]))
        self.assertEqual(length, len(payload))
        self.assertEqual(decoded, payload)

    def test_fragmented_server_messages_and_ping_pong(self):
        client = self.client()
        payload = json.dumps({"id": 1, "result": "ok"}).encode()
        data = b'\x89\x02hi' + bytes([0x01, 10]) + payload[:10] + bytes([0x80, len(payload) - 10]) + payload[10:]
        for start in range(0, len(data), 3):
            client.chunks.put(data[start:start + 3])
        self.assertEqual(client.receive(), {"id": 1, "result": "ok"})
        self.assertEqual(client.process.stdin.getvalue()[0], 0x8a)

    def test_events_before_rpc_ack_are_retained(self):
        client = self.client()
        client.counter = 0
        client.pending = deque()
        client.send = Mock()
        event = {"method": "turn/completed", "params": {"threadId": "chat"}}
        client.receive = Mock(side_effect=[event, {"id": 1, "result": {"ok": True}}])
        self.assertEqual(client.request("turn/start", {}), {"ok": True})
        self.assertEqual(client.next_event(), event)


class SharedDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        directory = Path(self.temp.name)
        self.task = Task(chat_id="open-chat", message="Multiline\nUnicode café 日本語", model="test-model",
                         effort="high", cwd=str(directory), delivery_id="unique-attempt",
                         log=str(directory / "log.jsonl"), response=str(directory / "response.txt"))
        self.client = Mock()
        self.factory = Mock(return_value=self.client)

    def tearDown(self):
        self.temp.cleanup()

    def test_uses_existing_idle_writer_and_preserves_per_turn_settings(self):
        self.client.request.side_effect = [
            {"thread": {"status": {"type": "idle"}}},
            {"turn": {"id": "turn-1", "status": "inProgress"}}]
        self.client.next_event.side_effect = [
            {"method": "item/completed", "params": {"threadId": "other-chat", "turnId": "turn-1",
                "item": {"type": "agentMessage", "text": "Ignore other chats"}}},
            {"method": "item/completed", "params": {"threadId": self.task.chat_id, "turnId": "turn-1",
                "item": {"type": "agentMessage", "text": "Correct final answer"}}},
            {"method": "turn/completed", "params": {"threadId": self.task.chat_id,
                "turn": {"id": "unrelated-turn", "status": "completed"}}},
            {"method": "turn/completed", "params": {"threadId": self.task.chat_id,
                "turn": {"id": "turn-1", "status": "completed"}}}]
        self.assertEqual(deliver_to_open_chat("codex", self.task, self.factory), (0, ""))
        method, params = self.client.request.call_args_list[1].args
        self.assertEqual(method, "turn/start")
        self.assertEqual(params["input"][0]["text"], self.task.message)
        self.assertEqual(params["effort"], "high")
        self.assertEqual(params["model"], "test-model")
        self.assertEqual(params["clientUserMessageId"], "unique-attempt")
        self.assertEqual(Path(self.task.response).read_text(encoding="utf-8"), "Correct final answer")
        self.client.close.assert_called_once()

    def test_busy_chat_waits_without_sending_or_interrupting(self):
        self.client.request.return_value = {"thread": {"status": {"type": "active"}}}
        with self.assertRaises(BusyChat):
            deliver_to_open_chat("codex", self.task, self.factory)
        self.assertEqual(self.client.request.call_count, 1)
        self.assertEqual(self.client.request.call_args.args[0], "thread/read")
        self.client.close.assert_called_once()

    def test_recoverable_system_error_accepts_next_message(self):
        self.client.request.side_effect = [
            {"thread": {"status": {"type": "systemError"}, "canAcceptDirectInput": True}},
            {"turn": {"id": "recovered", "status": "completed", "items": []}},
            {"data": []}]
        self.assertEqual(deliver_to_open_chat("codex", self.task, self.factory), (0, ""))
        self.assertEqual(self.client.request.call_args_list[1].args[0], "turn/start")

    def test_idle_chat_rejecting_direct_input_waits(self):
        self.client.request.return_value = {"thread": {
            "status": {"type": "idle"}, "canAcceptDirectInput": False}}
        with self.assertRaises(BusyChat):
            deliver_to_open_chat("codex", self.task, self.factory)
        self.assertEqual(self.client.request.call_count, 1)

    def test_transport_error_after_submission_is_never_retried(self):
        self.client.request.side_effect = [
            {"thread": {"status": {"type": "idle"}}}, ConnectionError("Disconnected")]
        code, error = deliver_to_open_chat("codex", self.task, self.factory)
        self.assertEqual(code, 1)
        self.assertIn("may already have reached", error)
        self.assertEqual(self.client.request.call_count, 2)

    def test_terminal_failure_and_default_effort_are_preserved(self):
        self.task.effort = "Default"
        self.client.request.side_effect = [
            {"thread": {"status": {"type": "idle"}}},
            {"turn": {"id": "turn-1", "status": "failed", "error": {"message": "Quota exceeded"}}}]
        self.assertEqual(deliver_to_open_chat("codex", self.task, self.factory), (1, "Quota exceeded"))
        self.assertNotIn("effort", self.client.request.call_args.args[1])

    def test_reconnect_monitors_existing_turn_without_resubmitting(self):
        self.task.turn_id = "known-turn"
        self.client.pending = deque()
        self.client.request.side_effect = [
            {"data": [{"id": "known-turn", "status": "inProgress"}]},
            {"data": [{"id": "known-turn", "status": "completed",
                       "items": [{"type": "agentMessage", "text": "Finished while monitoring"}]}]}]
        self.assertEqual(monitor_existing_turn("codex", self.task, self.factory, poll_interval=0), (0, ""))
        self.assertEqual([call.args[0] for call in self.client.request.call_args_list],
                         ["thread/turns/list", "thread/turns/list"])
        self.assertEqual(Path(self.task.response).read_text(encoding="utf-8"), "Finished while monitoring")


if __name__ == "__main__":
    unittest.main()
