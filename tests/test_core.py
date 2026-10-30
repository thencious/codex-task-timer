import json
from contextlib import closing
from pathlib import Path
import queue
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from core import (Chat, Scheduler, Store, Task, automatic_directory, build_command,
                  countdown, discover_chats, normalize_directory, parse_delay)


class TimerTests(unittest.TestCase):
    def test_new_chat_builds_fresh_exec_command_with_same_settings(self):
        task = Task(new_chat=True, cwd="example", effort="high", model="test-model", response="answer.txt")
        command = build_command("codex", task)
        self.assertNotIn("resume", command)
        self.assertNotIn("--all", command)
        self.assertEqual(command[-1], "-")
        self.assertNotIn("", command)
        self.assertIn('model_reasoning_effort="high"', command)
        self.assertIn("test-model", command)

    def test_hour_minute_input_and_seconds_display(self):
        self.assertEqual(parse_delay("2:30"), 9000)
        self.assertEqual(parse_delay("0:00"), 0)
        self.assertEqual(countdown(3661), "01:01:01")
        self.assertEqual(countdown(.1), "00:00:01")
        self.assertEqual(countdown(-20), "00:00:00")
        for value in ["1:60", "1:5", "1:05:00", "-1:00", "a:01", "360", "199",
                      "3.5.0", "-3", "-3.5", "nan", "inf", "", "3,5"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_delay(value)

    def test_flexible_hour_and_compact_inputs(self):
        for value, seconds in {"3": 10800, "3.5": 12600, "330": 12600,
                               "12": 43200, "1230": 45000, "005": 300,
                               "0": 0, ".5": 1800, "3.25": 11700,
                               "3.1": 11160, " 330 ": 12600}.items():
            with self.subTest(value=value):
                self.assertEqual(parse_delay(value), seconds)

    def test_decimal_hours_round_to_nearest_minute(self):
        self.assertEqual(parse_delay("3.333"), 12000)
        self.assertEqual(parse_delay("0.025"), 120)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = Store(self.path)
        self.now = 1000.0
        self.calls = queue.Queue()
        self.scheduler = Scheduler(self.store, "codex", [], clock=lambda: self.now,
                                   runner=lambda task: (self.calls.put(task.id) or (0, "")))

    def tearDown(self):
        self.temp.cleanup()

    def task(self, **kwargs):
        values = dict(chat_id="chat-a", cwd=str(self.path), message="Do the next task", delay="0:01")
        values.update(kwargs)
        task = Task(**values)
        self.scheduler.tasks.append(task)
        return task

    def complete(self, scheduler=None):
        scheduler = scheduler or self.scheduler
        deadline = time.monotonic() + 5
        while scheduler.completions.empty():
            if time.monotonic() >= deadline:
                self.fail("Worker did not finish")
            time.sleep(.005)
        scheduler.tick()

    def test_real_clock_jump_dispatches_once(self):
        task = self.task()
        self.scheduler.start(task)
        self.now += 59
        self.scheduler.tick()
        self.assertEqual(task.status, "scheduled")
        self.now += 3600  # sleep or a delayed timer callback
        self.scheduler.tick()
        self.assertEqual(self.calls.get(timeout=5), task.id)
        self.complete()
        for _ in range(5):
            self.scheduler.tick()
        self.assertTrue(self.calls.empty())
        self.assertEqual(task.status, "sent")
        self.assertNotIn(task, self.scheduler.tasks)
        self.assertEqual(self.store.load(), [])

    def test_new_chat_default_timer_dispatches_and_is_persisted(self):
        task = self.task(chat_id="", new_chat=True, default_due=self.now + 60)
        self.scheduler.start(task, deadline=task.default_due)
        self.assertTrue(self.store.load()[0].new_chat)
        self.now += 61
        self.scheduler.tick()
        self.assertEqual(self.calls.get(timeout=5), task.id)
        self.complete()
        self.assertNotIn(task, self.scheduler.tasks)

    def test_pause_resume_uses_remaining_time(self):
        task = self.task()
        self.scheduler.start(task)
        self.now += 20
        self.scheduler.pause(task)
        self.assertEqual(task.remaining, 40)
        self.now += 500
        self.scheduler.tick()
        self.assertTrue(self.calls.empty())
        self.scheduler.resume(task)
        self.assertEqual(task.due, self.now + 40)
        self.now += 39
        self.scheduler.tick()
        self.assertEqual(task.status, "scheduled")

    def test_cancel_does_not_send(self):
        task = self.task()
        self.scheduler.start(task)
        self.scheduler.cancel(task)
        self.now += 100
        self.scheduler.tick()
        self.assertTrue(self.calls.empty())
        self.assertEqual(task.status, "cancelled")

    def test_empty_default_tasks_expire_without_sending_and_stay_removed_on_reload(self):
        for missing in [{"message": ""}, {"message": " \n\t"}, {"chat_id": ""}, {"chat_id": "", "message": ""}]:
            task = self.task(**missing)
            task.default_due = self.now + 60
            self.scheduler.start(task, validate=False, deadline=task.default_due)
        self.now += 61
        self.scheduler.tick()
        self.assertEqual(self.scheduler.tasks, [])
        self.assertEqual(self.store.load(), [])
        self.assertTrue(self.calls.empty())

    def test_editing_default_task_waits_before_removing_and_manual_task_is_kept(self):
        task = self.task(message="", default_due=self.now + 60)
        self.scheduler.start(task, validate=False, deadline=task.default_due)
        manual = self.task(message="", default_timer=False)
        self.scheduler.start(manual, validate=False)
        self.scheduler.editing.add(task.id)
        self.now += 61
        self.scheduler.tick()
        self.assertIn(task, self.scheduler.tasks)
        self.scheduler.editing.discard(task.id)
        self.scheduler.tick()
        self.assertNotIn(task, self.scheduler.tasks)
        self.assertIn(manual, self.scheduler.tasks)
        self.assertTrue(self.calls.empty())

    def test_same_chat_waits_and_other_chat_runs(self):
        # A worker that has finished but whose completion has not been collected
        # remains "running" until the next tick.
        first = self.task(delay="0:00")
        second = self.task(delay="0:00")
        other = self.task(chat_id="chat-b", delay="0:00")
        for task in [first, second, other]:
            self.scheduler.start(task)
        self.scheduler.tick()
        launched = {self.calls.get(timeout=5), self.calls.get(timeout=5)}
        self.assertEqual(launched, {first.id, other.id})
        self.assertEqual(second.status, "scheduled")
        self.complete()
        self.assertEqual(self.calls.get(timeout=5), second.id)
        self.complete()
        self.assertEqual(second.status, "sent")

    def test_deadlines_survive_restart_and_interrupted_runs_do_not_replay(self):
        due = self.task()
        self.scheduler.start(due)
        interrupted = self.task(status="running")
        self.scheduler.save()
        self.now += 300
        loaded = Scheduler(self.store, "codex", clock=lambda: self.now, runner=self.scheduler.runner)
        self.assertEqual(loaded.tasks[0].due, 1060)
        self.assertEqual(loaded.tasks[1].status, "interrupted")
        loaded.tick()
        self.assertEqual(self.calls.get(timeout=5), due.id)
        self.complete(loaded)
        self.assertTrue(self.calls.empty())
        self.assertEqual([(task.id, task.status) for task in loaded.tasks], [(interrupted.id, "interrupted")])

    def test_invalid_task_never_becomes_scheduled(self):
        for changes in [{"message": " "}, {"chat_id": ""}, {"cwd": ""},
                        {"cwd": str(self.path / "missing")}, {"delay": "0:99"}]:
            task = self.task(**changes)
            with self.assertRaises(ValueError):
                self.scheduler.start(task)
            self.assertEqual(task.status, "draft")

    def test_save_failure_does_not_arm_timer(self):
        task = self.task(delay="0:00")
        with patch.object(self.store, "save", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError):
                self.scheduler.start(task)
        self.scheduler.tick()
        self.assertEqual(task.status, "draft")
        self.assertTrue(self.calls.empty())

    def test_process_failure_is_visible_without_retry(self):
        self.scheduler.runner = lambda task: (12, "CLI failed")
        task = self.task(delay="0:00")
        self.scheduler.start(task)
        self.scheduler.tick()
        self.complete()
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.error, "CLI failed")
        self.scheduler.tick()
        self.assertEqual(task.status, "failed")

    def test_busy_chat_retries_without_overtaking_or_losing_its_delivery_id(self):
        first = self.task(delay="0:00")
        second = self.task(delay="0:00")
        for task in [first, second]:
            self.scheduler.start(task)
        delivery_id = first.delivery_id
        results = iter([(-2, "Waiting for chat"), (0, ""), (0, "")])
        self.scheduler.runner = lambda task: (self.calls.put(task.id) or next(results))
        self.scheduler.tick()
        self.assertEqual(self.calls.get(timeout=5), first.id)
        self.complete()
        self.assertEqual(first.status, "scheduled")
        self.assertEqual(first.retry_at, self.now + 5)
        self.scheduler.tick()
        self.assertTrue(self.calls.empty())
        self.assertEqual(second.status, "scheduled")
        self.now += 5
        self.scheduler.tick()
        self.assertEqual(self.calls.get(timeout=5), first.id)
        self.assertEqual(first.delivery_id, delivery_id)
        self.complete()
        self.assertEqual(self.calls.get(timeout=5), second.id)
        self.complete()
        self.assertEqual((first.status, second.status), ("sent", "sent"))

    def test_writer_conflict_uses_existing_writer_instead_of_failing(self):
        stub = self.path / "locked_codex.py"
        stub.write_text("import sys\nprint('Error: thread/resume: thread already has an active writer')\nsys.exit(1)\n", encoding="utf-8")
        task = self.task(log=str(self.path / "run.jsonl"), response=str(self.path / "response.txt"))
        with patch("core.build_command", return_value=[sys.executable, str(stub)]), patch(
                "core.deliver_to_open_chat", return_value=(0, "")) as shared:
            self.assertEqual(self.scheduler._run(task), (0, ""))
            shared.assert_called_once_with("codex", task)

    def test_restart_reconnects_to_acknowledged_turn_without_sending_again(self):
        log = self.path / "known-turn.jsonl"
        log.write_text(json.dumps({"delivery": "existing Codex writer", "turnId": "known-turn"}) + "\n", encoding="utf-8")
        task = self.task(status="running", log=str(log))
        self.scheduler.save()
        loaded = Scheduler(self.store, "codex", clock=lambda: self.now, runner=self.scheduler.runner)
        self.assertEqual(loaded.tasks[0].status, "resuming")
        self.assertEqual(loaded.tasks[0].turn_id, "known-turn")
        with patch("core.monitor_existing_turn", return_value=(0, "")) as monitor:
            loaded.tick()
            if loaded.tasks and loaded.tasks[0].status == "running":
                self.complete(loaded)
            monitor.assert_called_once()
        self.assertTrue(self.calls.empty())
        self.assertEqual(loaded.tasks, [])

    def test_old_writer_conflict_does_not_hide_a_new_unrelated_failure(self):
        stub = self.path / "failed_codex.py"
        stub.write_text("import sys\nprint('Error: invalid configuration')\nsys.exit(1)\n", encoding="utf-8")
        log = self.path / "run.jsonl"
        log.write_text("Error: thread/resume: thread already has an active writer\n", encoding="utf-8")
        task = self.task(log=str(log), response=str(self.path / "response.txt"))
        with patch("core.build_command", return_value=[sys.executable, str(stub)]), patch("core.deliver_to_open_chat") as shared:
            self.assertEqual(self.scheduler._run(task)[0], 1)
            shared.assert_not_called()

    def test_real_subprocess_gets_exact_multiline_unicode_prompt_and_arguments(self):
        stub = self.path / "fake_codex.py"
        received = self.path / "received.json"
        stub.write_text("import sys, json, pathlib\n"
                        "args=sys.argv[1:]\n"
                        f"pathlib.Path({str(received)!r}).write_text(json.dumps({{'args':args,'prompt':sys.stdin.buffer.read().decode('utf-8'),'cwd':str(pathlib.Path.cwd())}}),encoding='utf-8')\n"
                        "pathlib.Path(args[args.index('-o')+1]).write_text('Done', encoding='utf-8')\n"
                        "print('{\"type\":\"turn.completed\"}')\n", encoding="utf-8")
        self.scheduler.runner = self.scheduler._run
        prompt = 'Follow up\nUnicode: café 日本語\n$HOME; echo "not shell code" & < >'
        task = self.task(delay="0:00", message=prompt, effort="high", model="example-model")
        self.scheduler.start(task)
        real_builder = build_command
        with patch("core.build_command", side_effect=lambda executable, task:
                   [sys.executable, str(stub)] + real_builder(executable, task)[1:]):
            self.scheduler.tick()
            self.complete()
        result = json.loads(received.read_text(encoding="utf-8"))
        self.assertEqual(result["prompt"], prompt)
        self.assertEqual(Path(result["cwd"]), self.path)
        self.assertEqual(result["args"][-2:], [task.chat_id, "-"])
        self.assertIn('model_reasoning_effort="high"', result["args"])
        self.assertIn("--all", result["args"])
        self.assertEqual(Path(task.response).read_text(encoding="utf-8"), "Done")
        self.assertEqual(task.status, "sent")
        self.assertIn("turn.completed", Path(task.log).read_text(encoding="utf-8"))


class DiscoveryTests(unittest.TestCase):
    def test_merges_database_index_and_rollouts_from_all_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            with closing(sqlite3.connect(home / "state_5.sqlite")) as db:
                db.execute("CREATE TABLE threads (id, title, cwd, model, archived, updated_at, agent_path)")
                db.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)",
                           ("db", "Initial title", str(home / "project-a"), "test-model", 0, 1, None))
                db.commit()
            (home / "session_index.jsonl").write_text(
                '{"id":"db","thread_name":"Renamed chat"}\n'
                '{"id":"index","thread_name":"Only in index"}\n', encoding="utf-8")
            sessions = home / "sessions/2026/10/23"
            sessions.mkdir(parents=True)
            for chat_id, directory in [("index", "project-b"), ("rollout", "project-c")]:
                (sessions / f"{chat_id}.jsonl").write_text(json.dumps({"type": "session_meta", "payload": {
                    "id": chat_id, "cwd": str(home / directory), "title": "Only in rollout", "source": "cli"}}) + "\n", encoding="utf-8")
            chats = {chat.id: chat for chat in discover_chats(home)}
            self.assertEqual(set(chats), {"db", "index", "rollout"})
            self.assertEqual(chats["db"].title, "Renamed chat")
            self.assertEqual(chats["index"].cwd, str(home / "project-b"))
            self.assertEqual(chats["rollout"].cwd, str(home / "project-c"))

    def test_directory_defaults_and_windows_path_normalization(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(automatic_directory(Chat("a", "Title", temp)), temp)
            self.assertEqual(automatic_directory(Chat("a", "Title")), str(Path.home()))
        self.assertEqual(normalize_directory("\\\\?\\C:\\Users\\test"), "C:\\Users\\test")
        self.assertEqual(normalize_directory("\\\\?\\UNC\\server\\share"), "\\\\server\\share")

    def test_sqlite_read_only_discovery_filters_archived_and_subagents(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            with closing(sqlite3.connect(home / "state_5.sqlite")) as db:
                db.execute("CREATE TABLE threads (id, title, cwd, model, archived, updated_at, agent_path)")
                db.executemany("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)", [
                    ("root", "My chat", temp, "test-model", 0, 2, None),
                    ("old", "Archived", temp, "test-model", 1, 1, None),
                    ("child", "Child", temp, "test-model", 0, 3, "/root/child")])
                db.commit()
            chats = discover_chats(home)
            self.assertEqual([chat.id for chat in chats], ["root"])
            self.assertEqual(chats[0].model, "test-model")

    def test_session_index_fallback_tolerates_partial_line(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / "session_index.jsonl").write_text(
                '{"id":"a","thread_name":"First"}\n'
                '{"id":"a","thread_name":"Renamed"}\n'
                '{"id":"b","thread_name":"Second"}\n{partial', encoding="utf-8")
            chats = discover_chats(home)
            self.assertEqual([chat.id for chat in chats], ["b", "a"])
            self.assertEqual(chats[1].title, "Renamed")


if __name__ == "__main__":
    unittest.main()
