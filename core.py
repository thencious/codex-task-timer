"""Local Codex session discovery and durable wall-clock task scheduling."""
from __future__ import annotations

from contextlib import closing
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from shared_codex import BusyChat, deliver_to_open_chat, monitor_existing_turn, recorded_turn

CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
BUSY = {"scheduled", "running"}


def parse_delay(value: str) -> int:
    value = value.strip()
    match = re.fullmatch(r"([0-9]{1,5}):([0-5][0-9])", value)
    if match:
        return int(match[1]) * 3600 + int(match[2]) * 60
    if re.fullmatch(r"[0-9]{1,2}", value):
        return int(value) * 3600
    match = re.fullmatch(r"([0-9]{1,5})([0-5][0-9])", value)
    if match:
        return int(match[1]) * 3600 + int(match[2]) * 60
    if re.fullmatch(r"(?:[0-9]{1,5}\.[0-9]+|\.[0-9]+)", value):
        minutes = (Decimal(value) * 60).to_integral_value(rounding=ROUND_HALF_UP)
        return int(minutes) * 60
    raise ValueError("Enter hours (3), decimal hours (3.5), h:mm (3:30), or compact hhmm (330).")


def countdown(seconds: float) -> str:
    seconds = max(0, math.ceil(seconds))
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"


@dataclass
class Chat:
    id: str
    title: str
    cwd: str = ""
    model: str = ""
    updated_at: float = 0

    @property
    def label(self) -> str:
        title = " ".join(self.title.split())[:85] or "Untitled chat"
        directory = Path(normalize_directory(self.cwd)).name if self.cwd else ""
        return f"{title}  ·  {directory + ' · ' if directory else ''}{self.id[-8:]}"


def normalize_directory(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def automatic_directory(chat: Chat, previous: str = "") -> str:
    for value in [chat.cwd, previous]:
        if value and Path(normalize_directory(value)).is_dir():
            return normalize_directory(value)
    return str(Path.home())


def discover_chats(home: Path = CODEX_HOME) -> list[Chat]:
    """Merge every local session source, without filtering by working directory."""
    chats: dict[str, Chat] = {}
    excluded: set[str] = set()
    indexed_ids: set[str] = set()
    databases = sorted(home.glob("state_*.sqlite"),
                       key=lambda p: int(re.search(r"state_(\d+)", p.name)[1]), reverse=True)
    for database in databases:
        try:
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
                model = "model" if "model" in columns else "'' AS model"
                agent = "agent_path" if "agent_path" in columns else "NULL"
                title = "COALESCE(NULLIF(name, ''), title)" if "name" in columns else "title"
                rows = db.execute(f"SELECT id, {title}, cwd, {model}, updated_at, archived, {agent} "
                                  "FROM threads ORDER BY updated_at DESC").fetchall()
                for row in rows:
                    if row[0] in indexed_ids:
                        continue
                    indexed_ids.add(row[0])
                    if row[5] or row[6] not in (None, "", "/root"):
                        excluded.add(row[0])
                    elif row[0] not in chats:
                        chats[row[0]] = Chat(row[0], row[1], normalize_directory(row[2]), row[3] or "", row[4])
        except (OSError, sqlite3.Error):
            continue
    index = home / "session_index.jsonl"
    if index.exists():
        for line in index.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                chat = chats.setdefault(item["id"], Chat(item["id"], "Untitled chat"))
                if item.get("thread_name"):
                    chat.title = item["thread_name"]
                if item.get("cwd") and not chat.cwd:
                    chat.cwd = normalize_directory(item["cwd"])
                if item.get("updated_at"):
                    try:
                        chat.updated_at = max(chat.updated_at, datetime.fromisoformat(item["updated_at"].replace("Z", "+00:00")).timestamp())
                    except ValueError:
                        pass
                # Preserve index ordering even on older versions without timestamps.
                chats.pop(chat.id, None)
                chats[chat.id] = chat
            except (ValueError, KeyError, TypeError):
                continue
    for path in (home / "sessions").rglob("*.jsonl"):
        try:
            with path.open(encoding="utf-8") as handle:
                first = json.loads(handle.readline(1_000_000))
                if first.get("type") != "session_meta":
                    continue
                meta = first["payload"]
                source = meta.get("source", "")
                if isinstance(source, dict) and "subagent" in source:
                    excluded.add(meta["id"])
                    continue
                chat = chats.setdefault(meta["id"], Chat(meta["id"], meta.get("title", "Untitled chat")))
                if not chat.cwd:
                    chat.cwd = normalize_directory(meta.get("cwd", ""))
                if not chat.model:
                    chat.model = meta.get("model", "")
                chat.updated_at = max(chat.updated_at, path.stat().st_mtime)
                if chat.title == "Untitled chat":
                    for _ in range(30):
                        line = handle.readline(1_000_000)
                        if not line:
                            break
                        event = json.loads(line)
                        payload = event.get("payload", {})
                        if event.get("type") == "event_msg" and payload.get("type") == "user_message":
                            chat.title = payload.get("message", "Untitled chat")[:100]
                            break
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted((chat for chat in reversed(list(chats.values())) if chat.id not in excluded),
                  key=lambda chat: chat.updated_at, reverse=True)


def reasoning_levels(home: Path = CODEX_HOME) -> dict[str, list[str]]:
    try:
        cache = json.loads((home / "models_cache.json").read_text(encoding="utf-8"))
        return {model["slug"]: [level["effort"] for level in model.get("supported_reasoning_levels", [])]
                for model in cache.get("models", [])}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def find_codex() -> str:
    """Resolve Windows' wrapper to the real binary; never put prompts in a shell."""
    found = shutil.which("codex.exe") or shutil.which("codex")
    if found and Path(found).suffix.lower() not in {".cmd", ".bat", ".ps1"}:
        return found
    if found:
        try:
            wrapper = Path(found).read_text(encoding="utf-8")
            match = re.search(r'@?"([^"\r\n]+\.exe)"\s+%\*', wrapper, re.I)
            if match and Path(match[1]).is_file():
                return match[1]
        except OSError:
            pass
    candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI/CodexCLI/bin/codex.exe"
    if candidate.is_file():
        return str(candidate)
    raise FileNotFoundError("Codex CLI was not found. Install Codex CLI and sign in, then reopen this app.")


@dataclass
class Task:
    id: str = ""
    delay: str = ""
    chat_id: str = ""
    chat_title: str = ""
    cwd: str = ""
    model: str = ""
    effort: str = "Default"
    message: str = ""
    status: str = "draft"
    due: float | None = None
    remaining: float = 0
    log: str = ""
    response: str = ""
    error: str = ""
    cwd_manual: bool = False
    default_timer: bool = True
    default_due: float | None = None
    delivery_id: str = ""
    retry_at: float = 0
    turn_id: str = ""
    new_chat: bool = False

    @property
    def queue_key(self):
        return self.id if self.new_chat else self.chat_id

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid.uuid4())


class Store:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "tasks.json"

    def load(self) -> list[Task]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        valid = {field.name for field in fields(Task)}
        tasks = [Task(**{k: v for k, v in item.items() if k in valid}) for item in data["tasks"]]
        for task in tasks:
            if task.status == "running":
                task.turn_id = task.turn_id or recorded_turn(task.log)
                if task.turn_id:
                    task.status = "resuming"
                    task.error = "Reconnecting to the existing run."
                else:
                    task.status = "interrupted"
                    task.error = "App closed during this run. Check the chat and log before scheduling again."
            elif task.status == "failed" and task.log:
                try:
                    if "already has an active writer" in Path(task.log).read_text(encoding="utf-8", errors="replace")[-16384:]:
                        task.error = "The chat was already open. Send now can retry through its existing writer."
                except OSError:
                    pass
        return tasks

    def save(self, tasks: list[Task]):
        temp = self.path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            json.dump({"version": 1, "tasks": [asdict(task) for task in tasks]}, handle,
                      ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)


def build_command(executable: str, task: Task) -> list[str]:
    command = [executable, "exec", "-c", 'approval_policy="never"']
    if task.effort != "Default":
        command += ["-c", f'model_reasoning_effort="{task.effort}"']
    if not task.new_chat:
        command += ["resume", "--all"]
    command += ["--skip-git-repo-check", "--json", "-o", task.response]
    if task.model:
        command += ["-m", task.model]
    return command + (["-"] if task.new_chat else [task.chat_id, "-"])


class Scheduler:
    """State belongs to the UI thread; workers report completion through a queue."""
    def __init__(self, store: Store, executable: str, tasks: list[Task] | None = None,
                 clock=time.time, runner=None):
        self.store = store
        self.executable = executable
        self.tasks = store.load() if tasks is None else tasks
        self.clock = clock
        self.runner = runner or self._run
        self.completions: queue.Queue = queue.Queue()
        self.editing: set[str] = set()

    def save(self):
        self.store.save(self.tasks)

    def readiness_error(self, task: Task) -> str:
        if not task.chat_id and not task.new_chat:
            return "Choose a chat."
        if not task.cwd or not Path(task.cwd).is_dir():
            return "Working folder is missing. Right-click to choose a folder."
        if not task.message.strip():
            return "Enter a message."
        return ""

    def start(self, task: Task, validate: bool = True, deadline: float | None = None):
        seconds = parse_delay(task.delay)
        now = self.clock()
        if deadline is not None:
            if not math.isfinite(deadline) or deadline <= now:
                raise ValueError("The reset time has passed. Wait for an updated reset or enter a delay.")
            seconds = deadline - now
        if validate and (error := self.readiness_error(task)):
            raise ValueError(error)
        previous = (task.due, task.remaining, task.error, task.status, task.delivery_id, task.retry_at, task.turn_id)
        task.due = deadline if deadline is not None else now + seconds
        task.remaining = seconds
        task.error = ""
        task.status = "scheduled"
        task.delivery_id = str(uuid.uuid4())
        task.retry_at = 0
        task.turn_id = ""
        try:
            self.save()
        except OSError:
            task.due, task.remaining, task.error, task.status, task.delivery_id, task.retry_at, task.turn_id = previous
            raise

    def pause(self, task: Task):
        if task.status == "scheduled":
            task.remaining = max(0, task.due - self.clock())
            task.due = None
            task.status = "paused"
            self.save()

    def resume(self, task: Task):
        if task.status == "paused":
            task.due = self.clock() + task.remaining
            task.status = "scheduled"
            task.retry_at = 0
            self.save()

    def cancel(self, task: Task):
        if task.status in {"scheduled", "paused"}:
            task.status = "cancelled"
            task.due = None
            task.retry_at = 0
            self.save()

    def tick(self):
        for task in self.tasks:
            if task.status == "resuming":
                task.status = "running"
                task.error = ""
                try:
                    self.save()
                except OSError:
                    task.status = "resuming"
                    raise
                threading.Thread(target=self._monitor_worker, args=(task,), daemon=True).start()
        changed = False
        while True:
            try:
                task_id, code, error = self.completions.get_nowait()
            except queue.Empty:
                break
            task = next((task for task in self.tasks if task.id == task_id), None)
            if task:
                if code == -2:
                    task.status = "scheduled"
                    task.retry_at = self.clock() + 5
                    task.error = error
                else:
                    task.status = "sent" if code == 0 else "failed"
                    task.error = error if code else ""
                    task.due = None
                    task.retry_at = 0
                changed = True
        if changed:
            self.save()
        removable = {task.id for task in self.tasks
                         if task.status == "sent" or (task.status == "scheduled" and task.default_timer and task.default_due is not None
                         and task.due is not None and task.due <= self.clock() and task.id not in self.editing
                         and ((not task.chat_id.strip() and not task.new_chat) or not task.message.strip()))}
        if removable:
            previous_tasks = self.tasks[:]
            self.tasks[:] = [task for task in self.tasks if task.id not in removable]
            try:
                self.save()
            except OSError:
                self.tasks[:] = previous_tasks
                raise
        running = {task.queue_key for task in self.tasks if task.status == "running"}
        first_for_chat = {}
        for pending in sorted((task for task in self.tasks if task.status == "scheduled" and task.due is not None),
                              key=lambda task: task.due):
            first_for_chat.setdefault(pending.queue_key, pending.id)
        ready = sorted((task for task in self.tasks if task.status == "scheduled"
                        and task.due is not None and task.due <= self.clock()
                        and task.retry_at <= self.clock()), key=lambda task: task.due)
        for task in ready:
            if task.id in self.editing or task.queue_key in running or first_for_chat.get(task.queue_key) != task.id:
                continue
            if error := self.readiness_error(task):
                task.error = error
                continue
            task.error = ""
            if not task.retry_at or not task.log:
                stamp = time.time_ns()
                logs = self.store.directory / "logs"
                logs.mkdir(exist_ok=True)
                task.log = str(logs / f"{task.id}-{stamp}.jsonl")
                task.response = str(logs / f"{task.id}-{stamp}.response.txt")
            task.retry_at = 0
            task.status = "running"
            # Persist the claim before launching; never replay an uncertain run after a crash.
            try:
                self.save()
            except OSError:
                task.status = "scheduled"
                raise
            running.add(task.queue_key)
            threading.Thread(target=self._worker, args=(task,), daemon=True).start()

    def _worker(self, task: Task):
        try:
            code, error = self.runner(task)
        except Exception as exc:
            code, error = 1, str(exc)
            try:
                with open(task.log, "a", encoding="utf-8") as handle:
                    handle.write(f"\nLaunch failed: {exc}\n")
            except OSError:
                pass
        self.completions.put((task.id, code, error))

    def _monitor_worker(self, task: Task):
        try:
            code, error = monitor_existing_turn(self.executable, task)
        except Exception as exc:
            code, error = 1, "Could not reconnect to the existing run. Check the chat before retrying. " + str(exc)
        self.completions.put((task.id, code, error))

    def _run(self, task: Task) -> tuple[int, str]:
        command = build_command(self.executable, task)
        with open(task.log, "ab") as log:
            offset = log.tell()
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log, stderr=log,
                                       cwd=task.cwd, shell=False,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            process.communicate(input=task.message.encode("utf-8"))
        if process.returncode:
            with open(task.log, "rb") as log:
                log.seek(0, 2)
                log.seek(max(offset, log.tell() - 16384))
                error_log = log.read().decode("utf-8", errors="replace")
            if re.search(r"(?m)^Error: thread/resume:.*already has an active writer", error_log):
                try:
                    return deliver_to_open_chat(self.executable, task)
                except BusyChat as exc:
                    return -2, str(exc)
        return process.returncode, f"Codex exited with code {process.returncode}. Details: {task.log}"
