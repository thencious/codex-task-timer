"""Show the real UI with isolated sample data; never read chats or send messages."""
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App
from core import Chat, Scheduler, Store, Task

samples = [
    ("Website polish", "Review the landing page on mobile and fix any spacing issues.", "high", 4523),
    ("Test cleanup", "Run the test suite, investigate failures, and summarize what changed.", "medium", 7510),
    ("Documentation", "Update the setup guide with the new configuration options. Include a short example for each option, and check that all commands work from a clean checkout.", "Default", 11220),
    ("Accessibility review", "Check keyboard navigation and focus states across the settings screen.", "high", 14400),
    ("Release checklist", "Review the release notes and list anything still blocking the next release.", "Default", 18000),
]
compact = "--compact" in sys.argv
selected = samples[:2] if compact else samples
with tempfile.TemporaryDirectory() as directory:
    chats = [Chat(f"sample-{i:08d}", title, str(Path(directory) / ["website", "tests", "docs", "settings", "release"][i]), "sample-model")
             for i, (title, *_) in enumerate(selected)]
    for chat in chats:
        Path(chat.cwd).mkdir()
    tasks = [Task(chat_id=chat.id, chat_title=chat.title, cwd=chat.cwd, message=sample[1],
                  effort=sample[2], status="scheduled", due=time.time() + sample[3],
                  delay="1:15", default_timer=False)
             for chat, sample in zip(chats, selected)]
    tasks[1].status = "paused"
    tasks[1].remaining = selected[1][3]
    root = tk.Tk()
    scheduler = Scheduler(Store(Path(directory)), "disabled-demo", tasks)
    scheduler.tick = lambda: None
    with patch("app.discover_chats", return_value=chats), patch("app.reasoning_levels", return_value={}):
        app = App(root, scheduler, reset_reader=None)
        root.title("Codex Task Timer - Sample " + ("compact" if compact else "queue"))
        if compact:
            root.after(500, lambda: root.geometry("960x240"))
        root.mainloop()
