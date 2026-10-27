"""Connect to the existing Codex daemon through its local WebSocket proxy."""
from __future__ import annotations

import base64
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import queue
import struct
import subprocess
import threading
import time


class BusyChat(Exception):
    pass


def recorded_turn(log_path):
    if not log_path:
        return ""
    try:
        with open(log_path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("delivery") == "existing Codex writer":
                    return event.get("turnId", "")
    except OSError:
        pass
    return ""


def read_turn(client, thread_id, turn_id):
    cursor = None
    while True:
        result = client.request("thread/turns/list", {"threadId": thread_id, "limit": 50, "cursor": cursor})
        for turn in result.get("data", []):
            if turn.get("id") == turn_id:
                return turn
        cursor = result.get("nextCursor")
        if not cursor:
            raise RuntimeError("The submitted turn could not be found. Check the chat before retrying.")


def save_final_response(client, task, turn):
    items = turn.get("items", [])
    answers = [item.get("text", "") for item in items if item.get("type") == "agentMessage"]
    if not answers:
        try:
            result = client.request("thread/items/list", {"threadId": task.chat_id,
                "turnId": turn["id"], "limit": 100})
            answers = [item.get("text", "") for item in result.get("data", []) if item.get("type") == "agentMessage"]
        except Exception:
            return
    if answers:
        Path(task.response).write_text("\n\n".join(answers), encoding="utf-8")


def monitor_existing_turn(executable, task, client_factory=None, poll_interval=2):
    client = (client_factory or ProxyClient)(executable)
    try:
        while True:
            turn = read_turn(client, task.chat_id, task.turn_id)
            client.pending.clear()
            status = turn.get("status")
            if status == "completed":
                save_final_response(client, task, turn)
                return 0, ""
            if status not in {"inProgress", "active"}:
                return 1, (turn.get("error") or {}).get("message") or f"Codex turn {status}. Check the chat."
            time.sleep(poll_interval)
    except Exception as exc:
        return 1, "Could not monitor the existing run. Check the chat before retrying. " + str(exc)
    finally:
        client.close()


class ProxyClient:
    def __init__(self, executable, timeout=10):
        self.timeout = timeout
        self.pending = deque()
        self.buffer = bytearray()
        self.chunks = queue.Queue()
        self.counter = 0
        self.process = subprocess.Popen([executable, "app-server", "proxy"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

        def read():
            try:
                while chunk := self.process.stdout.read(65536):
                    self.chunks.put(chunk)
            finally:
                self.chunks.put(None)

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        try:
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            self.write_all((f"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
            header = bytearray()
            deadline = time.monotonic() + timeout
            while not header.endswith(b"\r\n\r\n"):
                header.extend(self.read_exact(1, deadline))
                if len(header) > 16384:
                    raise RuntimeError("Invalid Codex proxy response.")
            expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
            if b" 101 " not in header.split(b"\r\n", 1)[0] or expected not in header:
                raise RuntimeError("Could not connect to the existing Codex server.")
            self.request("initialize", {"clientInfo": {"name": "codex_task_timer",
                "title": "Codex Task Timer", "version": "1.1.0"}, "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized", "params": {}})
        except Exception:
            self.close()
            raise

    def read_exact(self, length, deadline):
        while len(self.buffer) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex server did not respond in time.")
            try:
                chunk = self.chunks.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError("Codex server did not respond in time.") from None
            if chunk is None:
                raise ConnectionError("Codex server connection closed.")
            self.buffer.extend(chunk)
        result = bytes(self.buffer[:length])
        del self.buffer[:length]
        return result

    def write_all(self, payload):
        remaining = memoryview(payload)
        while remaining:
            written = self.process.stdin.write(remaining)
            if not written:
                raise ConnectionError("Codex server connection closed while writing.")
            remaining = remaining[written:]

    def frame(self, payload, opcode=1):
        length = len(payload)
        header = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", length)
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.write_all(header + mask + masked)

    def send(self, message):
        self.frame(json.dumps(message, ensure_ascii=False).encode("utf-8"))

    def receive(self, timeout=None):
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        message = bytearray()
        while True:
            first, second = self.read_exact(2, deadline)
            length = second & 127
            if length == 126:
                length = struct.unpack("!H", self.read_exact(2, deadline))[0]
            elif length == 127:
                length = struct.unpack("!Q", self.read_exact(8, deadline))[0]
            if length > 16_777_216 or len(message) + length > 16_777_216:
                raise RuntimeError("Codex server message exceeded the transport limit.")
            mask = self.read_exact(4, deadline) if second & 0x80 else None
            payload = self.read_exact(length, deadline)
            if mask:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            opcode = first & 15
            if opcode == 8:
                raise ConnectionError("Codex server connection closed.")
            if opcode == 9:
                self.frame(payload, opcode=10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1):
                raise RuntimeError("Unexpected Codex server frame.")
            message.extend(payload)
            if first & 0x80:
                return json.loads(message.decode("utf-8"))

    def request(self, method, params=None):
        self.counter += 1
        request_id = self.counter
        self.send({"id": request_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex server request timed out.")
            message = self.receive(timeout=remaining)
            if message.get("id") == request_id and ("result" in message or "error" in message):
                if "error" in message:
                    raise RuntimeError(message["error"].get("message", "Codex server request failed."))
                return message.get("result", {})
            self.pending.append(message)

    def next_event(self, timeout=60):
        return self.pending.popleft() if self.pending else self.receive(timeout)

    def close(self):
        try:
            self.frame(b"", opcode=8)
        except (OSError, ValueError):
            pass
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
        self.reader.join(timeout=1)
        self.process.stdout.close()


def deliver_to_open_chat(executable, task, client_factory=ProxyClient):
    """Use the existing writer, waiting locally if its current turn is active."""
    client = client_factory(executable)
    submitted = False
    try:
        thread = client.request("thread/read", {"threadId": task.chat_id, "includeTurns": False})["thread"]
        status = thread.get("status", {}).get("type")
        if status == "active":
            raise BusyChat("This chat is working. Waiting for its current turn to finish.")
        if status not in {"idle", "systemError"} or thread.get("canAcceptDirectInput") is False:
            raise BusyChat("This chat is held by another Codex window. Waiting for it to become available.")
        params = {"threadId": task.chat_id, "clientUserMessageId": task.delivery_id,
                  "input": [{"type": "text", "text": task.message}], "cwd": task.cwd,
                  "approvalPolicy": "never"}
        if task.model:
            params["model"] = task.model
        if task.effort != "Default":
            params["effort"] = task.effort
        # Once this request is attempted, transport errors cannot safely be retried automatically.
        submitted = True
        turn = client.request("turn/start", params)["turn"]
        turn_id = turn["id"]
        with open(task.log, "a", encoding="utf-8") as log:
            log.write(json.dumps({"delivery": "existing Codex writer", "turnId": turn_id}) + "\n")
            log.flush()
            while turn.get("status") in {"inProgress", "active"}:
                try:
                    event = client.next_event(timeout=2)
                except TimeoutError:
                    # Some existing clients don't subscribe this connection to turn events.
                    turn = read_turn(client, task.chat_id, turn_id)
                    continue
                payload = event.get("params", {})
                if payload.get("threadId") != task.chat_id:
                    continue
                if payload.get("turnId") not in (None, turn_id):
                    continue
                log.write(json.dumps(event, ensure_ascii=False) + "\n")
                log.flush()
                item = payload.get("item", {})
                if event.get("method") == "item/completed" and item.get("type") == "agentMessage":
                    Path(task.response).write_text(item.get("text", ""), encoding="utf-8")
                if event.get("method") == "turn/completed" and payload.get("turn", {}).get("id") == turn_id:
                    turn = payload["turn"]
        if turn.get("status") == "completed":
            save_final_response(client, task, turn)
            return 0, ""
        error = turn.get("error") or {}
        return 1, error.get("message") or f"Codex turn {turn.get('status', 'failed')}. Check the chat."
    except BusyChat:
        raise
    except Exception as exc:
        prefix = "Delivery may already have reached the chat. Check it before retrying. " if submitted else "Could not connect to the chat's existing writer. "
        return 1, prefix + str(exc)
    finally:
        client.close()
