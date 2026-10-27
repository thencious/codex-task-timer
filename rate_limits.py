"""Read account limits through Codex's read-only app-server RPC."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import queue
import subprocess
import threading
import time


@dataclass(frozen=True)
class FiveHourReset:
    resets_at: float
    used_percent: float

    @property
    def send_at(self) -> float:
        return self.resets_at + 60


def five_hour_reset(result: dict, now: float | None = None) -> FiveHourReset | None:
    now = time.time() if now is None else now
    buckets = result.get("rateLimitsByLimitId") or {}
    bucket = buckets.get("codex") or result.get("rateLimits") or {}
    for name in ("primary", "secondary"):
        window = bucket.get(name) or {}
        if window.get("windowDurationMins") != 300:
            continue
        try:
            reset = float(window["resetsAt"])
            used = float(window.get("usedPercent", 0))
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(reset) and reset + 60 > now and math.isfinite(used):
            return FiveHourReset(reset, min(100, max(0, used)))
    return None


def default_delay(deadline: float, now: float) -> str:
    minutes = max(0, math.ceil((deadline - now) / 60))
    return f"{minutes // 60}:{minutes % 60:02}"


def read_five_hour_reset(executable: str, timeout: float = 15) -> FiveHourReset | None:
    """No model requests, key handling, session resumes, or quota-reset actions."""
    process = subprocess.Popen([executable, "app-server", "--listen", "stdio://"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    messages = queue.Queue()

    def reader():
        for line in process.stdout:
            try:
                messages.put(json.loads(line))
            except ValueError:
                continue
        messages.put(None)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    expires = time.monotonic() + timeout

    def send(message):
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def response(request_id):
        while True:
            remaining = expires - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex account-status request timed out.")
            try:
                message = messages.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError("Codex account-status request timed out.") from None
            if message is None:
                raise RuntimeError("Codex account-status connection closed.")
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError("Codex could not read account limits.")
                return message.get("result", {})

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {
            "name": "codex_task_timer", "title": "Codex Task Timer", "version": "1.0.0"}}})
        response(1)
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "account/rateLimits/read", "params": {}})
        return five_hour_reset(response(2))
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        thread.join(timeout=1)
        process.stdout.close()
