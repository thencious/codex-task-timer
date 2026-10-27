from pathlib import Path
import tempfile
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from tkinter import ttk

from app import App, BG
from core import Chat, Scheduler, Store
from rate_limits import FiveHourReset


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.root.withdraw()
        self.scheduler = Scheduler(Store(Path(self.temp.name)), "codex", [])
        self.chat = Chat("example-chat", "A chat in another directory", str(Path(self.temp.name)), "test-model")
        self.discovery = patch("app.discover_chats", return_value=[self.chat])
        self.discovery.start()
        self.app = App(self.root, self.scheduler, reset_reader=None)
        self.row = self.app.rows[0]

    def tearDown(self):
        for callback in self.root.tk.call("after", "info"):
            self.root.after_cancel(callback)
        self.root.destroy()
        self.discovery.stop()
        self.temp.cleanup()

    def enter_time(self, value, event=None):
        self.row.focus_time()
        self.row.delay.set(value)
        self.row.confirm_time(event)

    def test_dark_two_line_rows_and_plus_under_last_row(self):
        self.app.add_task()
        self.root.update_idletasks()
        self.assertEqual(self.root.cget("background"), BG)
        self.assertEqual(len(self.app.rows), 2)
        for row in self.app.rows:
            self.assertEqual(row.frame.grid_size()[1], 2)
            self.assertLessEqual(row.frame.winfo_reqheight(), 90)
            self.assertFalse(hasattr(row, "clock"))
            self.assertFalse(hasattr(row, "header"))
        self.assertEqual(self.app.rows_frame.pack_slaves()[-1], self.app.add_button)
        self.app.rows[1].remove()
        self.assertEqual(len(self.scheduler.tasks), 1)

    def test_enter_confirms_timer_without_chat_or_message(self):
        self.enter_time("1:00", SimpleNamespace(keysym="Return"))
        self.assertEqual(self.row.task.status, "scheduled")
        self.assertEqual(self.row.task.remaining, 3600)
        self.assertRegex(self.row.delay.get(), r"^\d{2}:\d{2}:\d{2}$")
        self.assertEqual(str(self.row.message.cget("state")), "normal")
        self.assertFalse(self.row.time_editing)

    def test_reset_default_starts_immediately_with_seconds_and_keeps_exact_target(self):
        now = self.scheduler.clock()
        self.app.reset_info = FiveHourReset(now + 149.25, 64)
        self.app.poll_reset_status()
        self.row.update()
        target = self.app.reset_info.send_at
        self.assertEqual(self.row.task.delay, "0:04")
        self.assertEqual(self.row.task.status, "scheduled")
        self.assertEqual(self.row.task.due, target)
        self.assertRegex(self.row.delay.get(), r"^\d{2}:\d{2}:\d{2}$")
        delivery_id = self.row.task.delivery_id
        self.scheduler.clock = lambda: now + 70
        self.row.focus_time()
        self.row.confirm_time(SimpleNamespace(keysym="Return"))
        self.assertEqual(self.row.task.status, "scheduled")
        self.assertEqual(self.row.task.due, target)
        self.assertEqual(self.row.task.delivery_id, delivery_id)

    def test_new_rows_get_running_reset_default(self):
        self.app.reset_info = FiveHourReset(self.scheduler.clock() + 500, 64)
        self.app.poll_reset_status()
        self.row.focus_time()
        self.row.confirm_time()
        self.assertEqual(self.row.task.due, self.app.reset_info.send_at)
        self.app.add_task()
        self.assertEqual(self.app.rows[-1].task.default_due, self.app.reset_info.send_at)
        self.assertEqual(self.app.rows[-1].task.status, "scheduled")
        self.assertEqual(self.app.rows[-1].task.due, self.app.reset_info.send_at)
        self.assertRegex(self.app.rows[-1].delay.get(), r"^\d{2}:\d{2}:\d{2}$")

    def test_expired_empty_default_row_disappears_without_sending(self):
        now = self.scheduler.clock()
        self.app.reset_info = FiveHourReset(now + 60, 64)
        self.app.poll_reset_status()
        self.scheduler.clock = lambda: now + 121
        with patch("core.threading.Thread") as thread:
            self.app.tick()
            thread.assert_not_called()
        self.assertEqual(self.app.rows, [])
        self.assertEqual(self.scheduler.tasks, [])
        self.assertFalse(self.row.frame.winfo_exists())
        self.assertTrue(self.app.add_button.winfo_exists())

    def test_completed_task_is_removed_and_failed_task_remains(self):
        self.app.add_task()
        failed_row = self.app.rows[-1]
        self.row.task.status = "running"
        failed_row.task.status = "running"
        self.scheduler.completions.put((self.row.task.id, 0, ""))
        self.scheduler.completions.put((failed_row.task.id, 1, "Delivery failed"))
        self.app.tick()
        self.assertEqual(self.app.rows, [failed_row])
        self.assertFalse(self.row.frame.winfo_exists())
        self.assertEqual(failed_row.task.status, "failed")
        self.assertEqual([task.id for task in self.scheduler.store.load()], [failed_row.task.id])

    def test_manual_edit_or_clear_is_never_overwritten_by_rate_refresh(self):
        self.app.reset_info = FiveHourReset(self.scheduler.clock() + 500, 64)
        self.app.poll_reset_status()
        self.enter_time("0:10")
        deadline = self.row.task.due
        self.app.reset_info = FiveHourReset(self.scheduler.clock() + 900, 50)
        self.app.poll_reset_status()
        self.assertEqual(self.row.task.due, deadline)
        self.assertFalse(self.row.task.default_timer)
        self.row.clear_time()
        self.app.poll_reset_status()
        self.assertEqual(self.row.task.delay, "")
        self.assertIsNone(self.row.task.default_due)
        saved = self.scheduler.store.load()[0]
        self.assertFalse(saved.default_timer)

    def test_expired_default_cannot_send_immediately(self):
        now = self.scheduler.clock()
        self.app.reset_info = FiveHourReset(now - 500, 64)
        self.app.poll_reset_status()
        with patch("core.threading.Thread") as thread:
            self.scheduler.tick()
            thread.assert_not_called()
        self.assertEqual(self.row.task.status, "draft")
        self.assertIsNone(self.row.task.due)
        self.assertEqual(self.row.task.delay, "")

    def test_clicking_away_confirms_once(self):
        self.enter_time("0:05")
        due = self.row.task.due
        self.assertEqual(self.row.task.status, "scheduled")
        self.row.confirm_time()  # FocusOut following Enter or another commit
        self.assertEqual(self.row.task.due, due)

    def test_shorthand_inputs_arm_the_expected_countdown(self):
        for value, seconds, display in [("3", 10800, "03:00:00"),
                                         ("3.5", 12600, "03:30:00"),
                                         ("330", 12600, "03:30:00")]:
            with self.subTest(value=value):
                self.enter_time(value, SimpleNamespace(keysym="Return"))
                self.assertEqual(self.row.task.status, "scheduled")
                self.assertEqual(self.row.task.remaining, seconds)
                self.assertEqual(self.row.delay.get(), display)
                self.assertFalse(self.row.time_error)

    def test_clicking_blank_background_confirms_time(self):
        self.row.focus_time()
        self.row.delay.set("0:15")
        self.app.click_away(SimpleNamespace(widget=self.app.canvas))
        self.assertEqual(self.row.task.status, "scheduled")
        self.assertFalse(self.row.time_editing)

    def test_escape_then_focus_out_does_not_start(self):
        self.row.focus_time()
        self.row.delay.set("0:05")
        self.row.escape_time()
        self.row.confirm_time()
        self.assertEqual(self.row.task.status, "draft")
        self.assertIsNone(self.row.task.due)
        self.assertEqual(self.row.task.delay, "")
        self.assertEqual(self.row.delay.get(), "h:mm")

    def test_escape_stops_existing_countdown(self):
        self.enter_time("0:10")
        self.row.focus_time()
        self.row.delay.set("9:59")
        self.row.escape_time()
        self.row.confirm_time()
        self.assertIsNone(self.row.task.due)
        self.assertEqual(self.row.task.delay, "")
        self.assertEqual(self.row.task.remaining, 0)
        self.assertEqual(self.row.task.status, "draft")

    def test_blank_enter_does_not_dispatch_even_if_old_deadline_passed(self):
        self.row.chat.set(self.chat.label)
        self.row.select_chat()
        self.row.message.insert("1.0", "Must never be sent")
        self.enter_time("0:05")
        deadline = self.row.task.due
        self.row.focus_time()
        # Exercise the entry widget's delete operation, as with repeated Backspace.
        while self.row.delay_input.get():
            length = len(self.row.delay_input.get())
            self.row.delay_input.delete(length - 1, length)
        self.scheduler.clock = lambda: deadline + 100
        with patch("core.threading.Thread") as thread:
            self.scheduler.tick()  # The editing guard also protects an expired timer.
            self.row.confirm_time(SimpleNamespace(keysym="Return"))
            self.row.confirm_time()  # The resulting FocusOut must not re-arm it.
            self.scheduler.tick()
            thread.assert_not_called()
        self.assertIsNone(self.row.task.due)
        self.assertEqual(self.row.task.status, "draft")
        saved = self.scheduler.store.load()[0]
        self.assertIsNone(saved.due)
        self.assertEqual(saved.delay, "")

    def test_clear_button_and_escape_cancel_without_sending(self):
        self.row.chat.set(self.chat.label)
        self.row.select_chat()
        self.row.message.insert("1.0", "Must never be sent")
        for cancel in [self.row.clear_time, self.row.escape_time]:
            with self.subTest(cancel=cancel.__name__), patch("core.threading.Thread") as thread:
                self.enter_time("0:00")
                self.row.focus_time()
                cancel()
                self.row.confirm_time()
                self.scheduler.tick()
                thread.assert_not_called()
                self.assertEqual(self.row.task.status, "draft")
                self.assertIsNone(self.row.task.due)

    def test_pause_and_clear_are_beside_timer(self):
        self.assertEqual(int(self.row.delay_input.grid_info()["column"]), 0)
        self.assertEqual(int(self.row.pause_button.grid_info()["column"]), 1)
        self.assertEqual(int(self.row.clear_button.grid_info()["column"]), 2)
        self.assertEqual(self.row.pause_button.cget("text"), "")
        self.assertEqual(self.row.pause_icon, "pause")
        self.assertTrue(self.row.pause_button.cget("image"))
        self.assertEqual(self.row.clear_button.cget("text"), "")
        self.assertEqual(self.app.icons["pause"][0].width(), self.app.icons["stop"][0].width())
        self.assertGreater(self.app.icons["remove"][0].width(), self.app.icons["pause"][0].width())
        self.enter_time("0:05")
        self.row.pause_resume()
        self.assertEqual(self.row.pause_icon, "play")

    def test_message_grows_beyond_two_rows_and_shrinks_after_deleting(self):
        self.root.attributes("-alpha", 0)
        self.root.deiconify()
        self.root.update()
        self.row.message.insert("1.0", "A long message that should wrap naturally. " * 8)
        self.root.update()
        self.assertEqual(self.row.message.cget("wrap"), "word")
        self.assertGreater(int(self.row.message.cget("height")), 2)
        lines = self.row.message.count("1.0", "end-1c", "displaylines")[0] + 1
        self.assertEqual(int(self.row.message.cget("height")), lines)
        self.assertEqual(self.row.message.xview()[0], 0)
        self.row.message.delete("1.0", "end")
        self.row.message.insert("1.0", "Short message")
        self.root.update()
        self.assertEqual(int(self.row.message.cget("height")), 1)

    def test_enter_finishes_message_then_next_enter_adds_one_task(self):
        self.row.message.insert("1.0", "Message without a newline")
        self.row.focus_message()
        with patch.object(self.app.add_button, "focus_set") as focus:
            self.assertEqual(self.row.finish_message(), "break")
            focus.assert_called_once()
        self.assertEqual(self.row.task.message, "Message without a newline")
        self.assertNotIn(self.row.task.id, self.scheduler.editing)
        self.assertTrue(self.row.message.bind("<Return>"))
        self.assertTrue(self.app.add_button.bind("<Return>"))
        self.assertEqual(ttk.Style(self.root).lookup("Add.TButton", "background", ["focus"]), "#36534d")
        self.assertEqual(self.app.add_from_keyboard(), "break")
        self.assertEqual(len(self.app.rows), 2)

    def test_default_window_fits_five_tasks_with_two_line_messages(self):
        self.root.attributes("-alpha", 0)
        self.root.deiconify()
        for _ in range(4):
            self.app.add_task()
        for row in self.app.rows:
            row.message.insert("1.0", "First line\nSecond line")
        self.root.update()
        self.assertEqual(len(self.app.rows), 5)
        bottom = self.app.add_button.winfo_y() + self.app.add_button.winfo_height()
        self.assertLessEqual(bottom + 16, self.root.winfo_height())
        self.assertEqual(self.root.winfo_height(), 620)

    def test_scrollbar_is_hidden_when_everything_fits(self):
        self.root.attributes("-alpha", 0)
        self.root.deiconify()
        self.root.update()
        self.assertFalse(self.app.scrollbar.winfo_manager())
        for _ in range(10):
            self.app.add_task()
        self.root.update()
        self.assertTrue(self.app.scrollbar.winfo_manager())
        for row in self.app.rows[1:]:
            row.remove()
        self.root.update()
        self.assertFalse(self.app.scrollbar.winfo_manager())

    def test_hover_remains_active_across_timer_refreshes(self):
        self.enter_time("0:05")
        buttons = [self.row.pause_button, self.row.clear_button, self.row.send_button,
                   self.row.remove_button, self.app.add_button]
        for button in buttons:
            button.state(["active"])
        for _ in range(20):
            self.app.tick()
        for button in buttons:
            with self.subTest(button=str(button)):
                self.assertTrue(button.instate(["active"]))
        self.row.pause_resume()
        self.row.pause_button.state(["active"])
        for _ in range(20):
            self.row.update()
        self.assertTrue(self.row.pause_button.instate(["active"]))

    def test_send_now_requires_ready_task_and_dispatches_once(self):
        self.row.send_now()
        self.assertEqual(self.row.task.status, "draft")
        self.assertIsNone(self.row.task.due)
        self.row.chat.set(self.chat.label)
        self.row.select_chat()
        self.row.message.insert("1.0", "Send this exact message")
        with patch("core.threading.Thread") as thread:
            self.row.send_now()
            self.scheduler.tick()
            self.scheduler.tick()
            thread.return_value.start.assert_called_once()
        self.assertEqual(self.row.task.status, "running")
        self.assertEqual(self.row.task.message, "Send this exact message")

    def test_unedited_focus_does_not_restart_countdown(self):
        self.enter_time("0:10")
        due = self.row.task.due
        self.row.focus_time()
        self.row.confirm_time()
        self.assertEqual(self.row.task.due, due)

    def test_invalid_input_does_not_arm(self):
        self.enter_time("0:99")
        self.assertEqual(self.row.task.status, "draft")
        self.assertTrue(self.row.time_error)
        self.assertIsNone(self.row.task.due)

    def test_clearing_time_cancels(self):
        self.enter_time("0:10")
        self.enter_time("")
        self.assertEqual(self.row.task.status, "draft")
        self.assertIsNone(self.row.task.due)

    def test_chat_directory_is_automatic_and_countdown_keeps_running(self):
        self.enter_time("1:00")
        due = self.row.task.due
        self.row.chat.set(self.chat.label)
        self.row.select_chat()
        self.assertEqual(self.row.task.cwd, str(Path(self.temp.name)))
        self.assertEqual(self.row.task.chat_id, self.chat.id)
        self.row.message.insert("1.0", "Message\nwith another line")
        self.row.changed()
        self.assertEqual(self.row.task.message, "Message\nwith another line")
        self.assertEqual(self.row.task.due, due)

    def test_typing_message_blocks_dispatch_until_focus_leaves(self):
        self.row.chat.set(self.chat.label)
        self.row.select_chat()
        self.row.message.insert("1.0", "Still writing")
        self.enter_time("0:00")
        self.row.focus_message()
        self.scheduler.tick()
        self.assertEqual(self.row.task.status, "scheduled")
        self.row.leave_message()
        with patch("core.threading.Thread") as thread:
            self.scheduler.tick()
            thread.return_value.start.assert_called_once()
            self.assertEqual(self.row.task.status, "running")

    def test_pause_resume_and_saved_rows(self):
        self.enter_time("1:00")
        self.row.pause_resume()
        self.assertEqual(self.row.task.status, "paused")
        self.row.pause_resume()
        self.assertEqual(self.row.task.status, "scheduled")
        self.app.add_task()
        self.app.save()
        tasks = self.scheduler.store.load()
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0].status, "scheduled")
        self.assertEqual(tasks[1].delay, "")


if __name__ == "__main__":
    unittest.main()
