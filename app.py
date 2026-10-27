"""Compact, dark desktop queue for delayed Codex follow-ups."""
from __future__ import annotations

import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core import (Chat, Scheduler, Store, Task, automatic_directory, countdown,
                  discover_chats, find_codex, parse_delay, reasoning_levels)
from rate_limits import default_delay, read_five_hour_reset

ROOT = Path(__file__).resolve().parent
INSTALL_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else ROOT
DATA = INSTALL_DIR / "data"
BG = "#15171b"
SURFACE = "#202329"
FIELD = "#292d35"
INK = "#e2e6ec"
MUTED = "#89929f"
ACCENT = "#74cbbb"
ERROR = "#f19a91"
DEFAULT_HEIGHT = 620


def configure_if_changed(widget, **options):
    changed = {key: value for key, value in options.items() if str(widget.cget(key)) != str(value)}
    if changed:
        widget.configure(**changed)


def control_icons(root):
    """Draw standard media controls with pixels, independently of installed fonts."""
    icons = {}
    for kind in ("pause", "play", "stop", "remove"):
        base_size = 20 if kind == "remove" else 16
        size = max(base_size, round(base_size * float(root.tk.call("tk", "scaling")) / (96 / 72)))
        variants = []
        for color in (MUTED, "#505762", INK):
            icon = tk.PhotoImage(master=root, width=size, height=size)
            scale = size / 16
            if kind == "pause":
                for left, right in ((3, 6), (10, 13)):
                    icon.put(color, to=(round(left * scale), round(2 * scale),
                                        round(right * scale), round(14 * scale)))
            elif kind == "stop":
                icon.put(color, to=(round(3 * scale), round(3 * scale), round(13 * scale), round(13 * scale)))
            elif kind == "remove":
                thickness = max(2, round(1.5 * scale))
                for pos in range(round(3 * scale), round(12 * scale)):
                    other = size - pos - thickness
                    icon.put(color, to=(pos, pos, pos + thickness, pos + thickness))
                    icon.put(color, to=(pos, other, pos + thickness, other + thickness))
            else:
                for y in range(round(2 * scale), round(14 * scale)):
                    half_width = max(1, round((6 - abs((y + .5) / scale - 8)) * scale))
                    left = round(4 * scale)
                    icon.put(color, to=(left, y, left + half_width, y + 1))
            variants.append(icon)
        icons[kind] = (variants[0], "disabled", variants[1], "active", variants[2])
    return icons


class Tooltip:
    def __init__(self, widget, content):
        self.widget, self.content = widget, content
        self.pending = self.window = None
        widget.bind("<Enter>", self.enter, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")
        widget.bind("<Destroy>", self.hide, add="+")

    def enter(self, _event=None):
        self.pending = self.widget.after(450, self.show)

    def show(self):
        self.pending = None
        content = self.content() if callable(self.content) else self.content
        if not content or not self.widget.winfo_exists():
            return
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_geometry(f"+{self.widget.winfo_rootx()}+{self.widget.winfo_rooty() + self.widget.winfo_height() + 5}")
        tk.Label(self.window, text=content, bg=FIELD, fg=INK, font=("Segoe UI", 9),
                 justify="left", wraplength=580, padx=9, pady=6).pack()

    def hide(self, _event=None):
        if self.pending:
            self.widget.after_cancel(self.pending)
            self.pending = None
        if self.window:
            self.window.destroy()
            self.window = None


class TaskRow:
    def __init__(self, app: App, task: Task):
        self.app, self.task = app, task
        self.time_editing = False
        self.time_dirty = False
        self.internal_time_change = False
        self.time_error = ""
        self.invalid_time = None
        self.message_resize = None
        self.frame = ttk.Frame(app.rows_frame, style="Row.TFrame", padding=(8, 7))
        self.frame.pack(fill="x", pady=(0, 5), before=app.add_button)
        self.frame.columnconfigure(3, weight=1)
        self.delay = tk.StringVar()
        self.delay_input = ttk.Entry(self.frame, textvariable=self.delay, width=10,
                                     font=("Consolas", 11), style="Timer.TEntry")
        self.delay_input.grid(row=0, column=0, sticky="ew", padx=(0, 7))
        self.delay_input.bind("<FocusIn>", self.focus_time)
        self.delay_input.bind("<FocusOut>", self.confirm_time)
        self.delay_input.bind("<Return>", self.confirm_time)
        self.delay_input.bind("<KP_Enter>", self.confirm_time)
        self.delay_input.bind("<Escape>", self.escape_time)
        self.delay.trace_add("write", self.time_changed)
        self.chat = tk.StringVar()
        self.chat_input = ttk.Combobox(self.frame, textvariable=self.chat, state="readonly", width=42,
                                      postcommand=app.refresh_chats)
        self.chat_input.grid(row=0, column=3, sticky="ew", padx=(0, 7))
        self.chat_input.bind("<<ComboboxSelected>>", self.select_chat)
        self.effort = tk.StringVar(value=task.effort)
        self.effort_input = ttk.Combobox(self.frame, textvariable=self.effort, state="readonly", width=9)
        self.effort_input.grid(row=0, column=4, padx=(0, 5))
        self.effort.trace_add("write", lambda *_: self.changed())
        self.pause_icon = "pause"
        self.pause_button = ttk.Button(self.frame, image=app.icons[self.pause_icon], width=2, command=self.pause_resume)
        self.pause_button.grid(row=0, column=1, padx=(0, 3))
        self.clear_button = ttk.Button(self.frame, image=app.icons["stop"], width=2, command=self.clear_time)
        self.clear_button.grid(row=0, column=2, padx=(0, 7))
        self.clear_button.bind("<ButtonPress-1>", lambda _event: self.clear_time())
        self.send_button = ttk.Button(self.frame, text="Send now", width=8, command=self.send_now)
        self.send_button.grid(row=0, column=5, padx=(0, 3))
        self.remove_button = ttk.Button(self.frame, image=app.icons["remove"], width=2, command=self.remove)
        self.remove_button.grid(row=0, column=6)
        self.message = tk.Text(self.frame, height=1, wrap="word", font=("Segoe UI", 10),
                               bg=FIELD, fg=INK, insertbackground=INK, selectbackground="#38574f",
                               relief="flat", padx=7, pady=5, highlightthickness=0, undo=True,
                               tabs=("1c",))
        self.message.grid(row=1, column=0, columnspan=7, sticky="ew", pady=(5, 0))
        self.message.insert("1.0", task.message)
        self.message.edit_modified(False)
        self.message.bind("<<Modified>>", self.on_message)
        self.message.bind("<FocusIn>", self.focus_message)
        self.message.bind("<FocusOut>", self.leave_message)
        self.message.bind("<Tab>", self.next_field)
        self.message.bind("<Shift-Tab>", self.previous_field)
        self.message.bind("<Return>", self.finish_message)
        self.message.bind("<KP_Enter>", self.finish_message)
        self.message.bind("<Configure>", lambda _event: self.queue_message_resize())
        self.message_hint = tk.Label(self.message, text="Message to send…", bg=FIELD, fg=MUTED,
                                     font=("Segoe UI", 10), cursor="xterm", borderwidth=0)
        self.message_hint.bind("<Button-1>", lambda _e: self.message.focus_set())
        for widget in (self.frame, self.delay_input, self.chat_input, self.effort_input, self.message):
            widget.bind("<Button-3>", self.context_menu)
        Tooltip(self.delay_input, self.timer_help)
        Tooltip(self.chat_input, lambda: f"{self.task.chat_title}\n{self.task.cwd}" if self.task.chat_id else "Resume chat · all directories")
        Tooltip(self.effort_input, "Thinking level")
        Tooltip(self.pause_button, "Pause / resume")
        Tooltip(self.clear_button, "Stop and clear timer")
        Tooltip(self.send_button, "Send this message immediately")
        Tooltip(self.remove_button, "Remove task and cancel its timer")
        self.refresh_chats()
        self.update()
        self.queue_message_resize()

    def set_time(self, value):
        self.internal_time_change = True
        self.delay.set(value)
        self.internal_time_change = False

    def time_changed(self, *_):
        if self.time_editing and not self.internal_time_change:
            self.time_dirty = True

    def focus_time(self, _event=None):
        if self.task.status == "running" or self.time_editing:
            return
        self.time_editing = True
        self.time_dirty = False
        self.set_time(self.invalid_time if self.invalid_time is not None else self.task.delay)
        self.app.scheduler.editing.add(self.task.id)
        self.delay_input.selection_range(0, "end")

    def confirm_time(self, event=None):
        if not self.time_editing:
            return "break"
        explicit = getattr(event, "keysym", "") in {"Return", "KP_Enter"}
        value = self.delay.get().strip()
        use_default = (not self.time_dirty and self.task.default_timer
                       and self.task.default_due is not None and self.task.status == "draft")
        default_running = (not self.time_dirty and self.task.default_timer
                           and self.task.default_due is not None and self.task.status == "scheduled")
        changed = self.time_dirty or (explicit and not default_running) or use_default
        self.time_editing = False
        self.app.scheduler.editing.discard(self.task.id)
        if changed:
            previous = (self.task.delay, self.task.default_timer, self.task.default_due)
            try:
                if not value:
                    self.clear_time()
                else:
                    parse_delay(value)
                    self.task.delay = value
                    if not use_default:
                        self.task.default_timer = False
                        self.task.default_due = None
                    self.sync_content()
                    self.app.scheduler.start(self.task, validate=False,
                                             deadline=self.task.default_due if use_default else None)
                self.time_error = ""
                self.invalid_time = None
            except (ValueError, OSError) as exc:
                self.task.delay, self.task.default_timer, self.task.default_due = previous
                self.time_error = str(exc)
                self.invalid_time = value
        self.update()
        if explicit:
            self.app.canvas.focus_set()
        return "break"

    def escape_time(self, _event=None):
        self.clear_time()
        self.app.canvas.focus_set()
        return "break"

    def clear_time(self):
        if self.task.status == "running":
            return
        self.time_editing = False
        self.time_dirty = False
        self.time_error = ""
        self.invalid_time = None
        self.task.status = "draft"
        self.task.delay = ""
        self.task.due = None
        self.task.remaining = 0
        self.task.error = ""
        self.task.default_timer = False
        self.task.default_due = None
        self.task.retry_at = 0
        # Cancel in memory before releasing the editing guard or touching disk.
        self.app.scheduler.editing.discard(self.task.id)
        self.app.save()
        self.update()

    def send_now(self):
        if self.task.status == "running":
            return
        self.sync_content()
        error = self.app.scheduler.readiness_error(self.task)
        if error:
            self.task.error = error
            self.update()
            return
        previous = (self.task.delay, self.task.default_timer, self.task.default_due)
        self.task.delay = "0:00"
        self.task.default_timer = False
        self.task.default_due = None
        try:
            self.app.scheduler.start(self.task)
        except (ValueError, OSError) as exc:
            self.task.delay, self.task.default_timer, self.task.default_due = previous
            self.task.error = str(exc)
            self.update()
            return
        self.time_editing = False
        self.time_dirty = False
        self.time_error = ""
        self.invalid_time = None
        self.app.scheduler.editing.discard(self.task.id)
        self.app.canvas.focus_set()
        self.update()

    def sync_content(self):
        if self.task.status != "running":
            self.task.effort = self.effort.get()
            self.task.message = self.message.get("1.0", "end-1c")

    def changed(self):
        self.sync_content()
        self.app.save_later()

    def on_message(self, _event=None):
        if self.message.edit_modified():
            self.message.edit_modified(False)
            self.changed()
            self.update_hint()
            self.queue_message_resize()

    def queue_message_resize(self):
        if self.message_resize is not None:
            self.app.root.after_cancel(self.message_resize)
        self.message_resize = self.app.root.after_idle(self.resize_message)

    def resize_message(self):
        self.message_resize = None
        if not self.message.winfo_exists():
            return
        count = self.message.count("1.0", "end-1c", "displaylines")
        height = max(1, (count[0] if count else 0) + 1)
        if int(self.message.cget("height")) != height:
            self.message.configure(height=height)
            self.app.root.after_idle(self.app.fit_height)

    def finish_message(self, _event=None):
        self.changed()
        self.app.scheduler.editing.discard(self.task.id)
        self.app.add_button.focus_set()
        return "break"

    def focus_message(self, _event=None):
        self.app.scheduler.editing.add(self.task.id)
        self.update_hint()

    def leave_message(self, _event=None):
        self.changed()
        self.app.scheduler.editing.discard(self.task.id)
        self.update_hint()

    def next_field(self, _event=None):
        self.message.tk_focusNext().focus_set()
        return "break"

    def previous_field(self, _event=None):
        self.message.tk_focusPrev().focus_set()
        return "break"

    def update_hint(self):
        if not self.message.get("1.0", "end-1c") and self.app.root.focus_get() != self.message:
            self.message_hint.place(x=6, y=3)
        else:
            self.message_hint.place_forget()

    def refresh_chats(self):
        self.chat_input.configure(values=[chat.label for chat in self.app.chats])
        found = next((chat for chat in self.app.chats if chat.id == self.task.chat_id), None)
        if found:
            self.chat.set(found.label)
            if self.task.status != "running":
                self.task.chat_title = found.title
                self.task.model = found.model or self.task.model
                if not self.task.cwd_manual:
                    self.task.cwd = automatic_directory(found, self.task.cwd)
        elif self.task.chat_id:
            self.chat.set(Chat(self.task.chat_id, self.task.chat_title, self.task.cwd).label)
        else:
            self.chat.set("Resume chat · all directories")
        self.update_efforts()

    def update_efforts(self):
        options = ["Default"] + self.app.levels.get(self.task.model, ["low", "medium", "high", "xhigh", "max"])
        if self.effort.get() not in options:
            options.append(self.effort.get())
        self.effort_input.configure(values=options)

    def select_chat(self, _event=None):
        chat = next((chat for chat in self.app.chats if chat.label == self.chat.get()), None)
        if chat and self.task.status != "running":
            self.task.chat_id, self.task.chat_title = chat.id, chat.title
            self.task.cwd, self.task.model = automatic_directory(chat), chat.model
            self.task.cwd_manual = False
            self.task.error = ""
            self.effort.set("Default")
            self.update_efforts()
            self.app.save_later()

    def pause_resume(self):
        try:
            if self.task.status == "scheduled":
                self.app.scheduler.pause(self.task)
            elif self.task.status == "paused":
                self.app.scheduler.resume(self.task)
            self.update()
        except OSError as exc:
            self.app.report_error(str(exc))

    def cancel(self):
        try:
            self.app.scheduler.cancel(self.task)
            self.update()
        except OSError as exc:
            self.app.report_error(str(exc))

    def choose_folder(self):
        folder = filedialog.askdirectory(initialdir=self.task.cwd or str(Path.home()), parent=self.app.root)
        if folder:
            self.task.cwd = folder
            self.task.cwd_manual = True
            self.task.error = ""
            self.app.save_later()

    def remove(self):
        if self.task.status == "running":
            return
        self.app.scheduler.editing.discard(self.task.id)
        self.app.scheduler.tasks.remove(self.task)
        self.app.rows.remove(self)
        self.frame.destroy()
        self.app.save_later()

    def context_menu(self, event):
        menu = tk.Menu(self.frame, tearoff=False, bg=FIELD, fg=INK,
                       activebackground="#3a454b", activeforeground=INK)
        if self.task.status in {"scheduled", "paused"}:
            menu.add_command(label="Cancel timer", command=self.cancel)
        menu.add_command(label="Working folder…", command=self.choose_folder,
                         state="disabled" if self.task.status == "running" else "normal")
        if self.task.error or self.time_error:
            menu.add_command(label="Show error…", command=lambda: messagebox.showerror(
                "Task error", self.time_error or self.task.error, parent=self.app.root))
        menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def timer_help(self):
        parts = [self.time_error or self.task.error]
        if self.task.default_timer and self.task.default_due is not None:
            parts.append(time.strftime("Default: 5h reset + 1m → %I:%M:%S %p", time.localtime(self.task.default_due)))
        if self.task.due is not None:
            parts.append(time.strftime("Sends %a %H:%M:%S", time.localtime(self.task.due)))
        if self.task.status == "paused":
            parts.append("Paused")
        parts.append("Enter hours (3), decimal hours (3.5), h:mm (3:30), or hhmm (330).\n"
                     "Enter or click away to start. Escape or Stop cancels the timer.")
        return "\n".join(part for part in parts if part)

    def update(self):
        task = self.task
        running = task.status == "running"
        configure_if_changed(self.delay_input, state="disabled" if running else "normal",
                                    style="Error.Timer.TEntry" if self.time_error or (task.error and not task.retry_at) else "Timer.TEntry")
        configure_if_changed(self.chat_input, state="disabled" if running else "readonly")
        configure_if_changed(self.effort_input, state="disabled" if running else "readonly")
        configure_if_changed(self.message, state="disabled" if running else "normal")
        configure_if_changed(self.remove_button, state="disabled" if running else "normal")
        configure_if_changed(self.clear_button, state="disabled" if running else "normal")
        configure_if_changed(self.send_button, state="disabled" if running else "normal")
        icon = "play" if task.status == "paused" else "pause"
        if self.pause_icon != icon:
            self.pause_button.configure(image=self.app.icons[icon])
            self.pause_icon = icon
        configure_if_changed(self.pause_button,
                             state="normal" if task.status in {"scheduled", "paused"} else "disabled")
        if not self.time_editing:
            if task.status in {"scheduled", "paused"}:
                remaining = task.due - time.time() if task.status == "scheduled" else task.remaining
                value = "Waiting…" if task.status == "scheduled" and task.retry_at else countdown(remaining)
            else:
                value = {"running": "Waiting…" if task.retry_at else "Sending…", "sent": "Sent", "failed": "Failed", "interrupted": "Check chat"}.get(
                    task.status, self.invalid_time if self.invalid_time is not None else task.delay or "h:mm")
            if self.delay.get() != value:
                self.set_time(value)
        self.update_hint()


class App:
    def __init__(self, root: tk.Tk, scheduler: Scheduler, reset_reader=read_five_hour_reset):
        self.root, self.scheduler = root, scheduler
        self.rows: list[TaskRow] = []
        self.pending_save = None
        self.reset_reader = reset_reader
        self.reset_updates = queue.Queue()
        self.reset_pending = False
        self.next_reset_read = 0
        self.reset_info = None
        self.chats = discover_chats()
        self.levels = reasoning_levels()
        root.title("Codex Task Timer")
        root.geometry(f"960x{DEFAULT_HEIGHT}")
        root.minsize(700, 145)
        icon = ROOT / "assets" / "codex-task-timer.ico"
        if icon.exists() and os.name == "nt":
            root.iconbitmap(default=str(icon))
        root.configure(bg=BG)
        self.style()
        self.icons = control_icons(root)
        container = ttk.Frame(root, padding=8)
        container.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(container, bg=BG, highlightthickness=0, takefocus=True)
        self.scrollbar = ttk.Scrollbar(container, orient="vertical", command=self.canvas.yview)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=self.update_scrollbar)
        self.rows_frame = ttk.Frame(self.canvas)
        self.window = self.canvas.create_window((0, 0), window=self.rows_frame, anchor="nw")
        self.rows_frame.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.window, width=e.width))
        self.add_button = ttk.Button(self.rows_frame, text="+", width=3, command=self.add_task, style="Add.TButton")
        self.add_button.pack(anchor="w", padx=3)
        self.add_button.bind("<Return>", self.add_from_keyboard)
        self.add_button.bind("<KP_Enter>", self.add_from_keyboard)
        Tooltip(self.add_button, "Add task")
        root.bind_all("<MouseWheel>", self.scroll)
        root.bind("<Button-1>", self.click_away, add="+")
        root.protocol("WM_DELETE_WINDOW", self.close)
        for task in scheduler.tasks:
            self.rows.append(TaskRow(self, task))
        if not self.rows:
            self.add_task()
        root.after_idle(self.canvas.focus_set)
        self.tick()

    def style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10), foreground=INK, background=BG)
        style.configure("TFrame", background=BG)
        style.configure("Row.TFrame", background=SURFACE)
        style.configure("TButton", background=SURFACE, foreground=MUTED, padding=(5, 3), borderwidth=0)
        style.map("TButton", background=[("active", "#343a44")], foreground=[("disabled", "#505762"), ("active", INK)])
        style.configure("Add.TButton", font=("Segoe UI", 14), foreground=ACCENT, background=BG, padding=(5, 0))
        style.map("Add.TButton", background=[("focus", "#36534d"), ("active", "#343a44")],
                   foreground=[("focus", "#a1efdc"), ("active", "#a1efdc")])
        style.configure("TEntry", fieldbackground=FIELD, foreground=INK, insertcolor=INK,
                         bordercolor=FIELD, lightcolor=FIELD, darkcolor=FIELD, padding=(6, 4))
        style.configure("Timer.TEntry", foreground=ACCENT)
        style.configure("Error.Timer.TEntry", foreground=ERROR)
        style.map("TEntry", fieldbackground=[("disabled", SURFACE)], foreground=[("disabled", MUTED)])
        style.configure("TCombobox", fieldbackground=FIELD, foreground=INK, background=SURFACE,
                         arrowcolor=MUTED, bordercolor=FIELD, lightcolor=FIELD, darkcolor=FIELD, padding=(6, 4))
        style.map("TCombobox", fieldbackground=[("readonly", FIELD), ("disabled", SURFACE)],
                   foreground=[("disabled", MUTED), ("readonly", INK)],
                   selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", INK)])
        style.configure("Vertical.TScrollbar", background=FIELD, troughcolor=BG, bordercolor=BG,
                         lightcolor=FIELD, darkcolor=FIELD, arrowcolor=MUTED, width=10)
        self.root.option_add("*TCombobox*Listbox.background", FIELD)
        self.root.option_add("*TCombobox*Listbox.foreground", INK)
        self.root.option_add("*TCombobox*Listbox.selectBackground", "#36534d")
        self.root.option_add("*TCombobox*Listbox.selectForeground", INK)
        if os.name == "nt":
            try:
                import ctypes
                self.root.update_idletasks()
                hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
                enabled = ctypes.c_int(1)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(enabled), ctypes.sizeof(enabled))
            except (AttributeError, OSError):
                pass

    def scroll(self, event):
        if isinstance(event.widget, (tk.Text, ttk.Combobox)):
            return
        if str(event.widget.winfo_toplevel()) == str(self.root):
            self.canvas.yview_scroll(-int(event.delta / 120), "units")

    def update_scrollbar(self, first, last):
        self.scrollbar.set(first, last)
        needed = float(first) > 0.000001 or float(last) < 0.999999
        visible = bool(self.scrollbar.winfo_manager())
        if needed and not visible:
            self.scrollbar.pack(side="right", fill="y", padx=(4, 0), before=self.canvas)
        elif not needed and visible:
            self.scrollbar.pack_forget()

    def click_away(self, event):
        for row in self.rows:
            if row.time_editing and event.widget != row.delay_input:
                row.confirm_time()
                if self.root.focus_get() == row.delay_input:
                    self.canvas.focus_set()
            if self.root.focus_get() == row.message and event.widget not in (row.message, row.message_hint):
                self.canvas.focus_set()

    def add_task(self):
        task = Task()
        self.scheduler.tasks.append(task)
        self.apply_reset_default(task)
        self.rows.append(TaskRow(self, task))
        self.save_later()
        self.root.after_idle(self.show_last_task)
        self.rows[-1].message.focus_set()

    def add_from_keyboard(self, _event=None):
        self.add_task()
        return "break"

    def fit_height(self):
        required = min(DEFAULT_HEIGHT, max(145, self.rows_frame.winfo_reqheight() + 20))
        if self.root.winfo_height() > 1 and self.root.winfo_height() < required:
            self.root.geometry(f"{self.root.winfo_width()}x{required}")

    def show_last_task(self):
        self.fit_height()
        self.canvas.yview_moveto(1)

    def refresh_chats(self):
        self.chats = discover_chats()
        self.levels = reasoning_levels()
        for row in self.rows:
            row.refresh_chats()

    def save_later(self):
        if self.pending_save:
            self.root.after_cancel(self.pending_save)
        self.pending_save = self.root.after(500, self.save)

    def report_error(self, text):
        self.root.title(f"Codex Task Timer · {text}")

    def save(self):
        self.pending_save = None
        try:
            self.scheduler.save()
            return True
        except OSError as exc:
            self.report_error(f"Could not save: {exc}")
            return False

    def apply_reset_default(self, task):
        if task.status != "draft" or not task.default_timer or (task.delay and task.default_due is None):
            return False
        now = self.scheduler.clock()
        target = self.reset_info.send_at if self.reset_info is not None else task.default_due
        if target is None:
            return False
        if target <= now:
            target = None
        delay = default_delay(target, now) if target is not None else ""
        changed = task.delay != delay or task.default_due != target
        previous = (task.delay, task.default_due)
        task.delay, task.default_due = delay, target
        if target is not None:
            try:
                self.scheduler.start(task, validate=False, deadline=target)
            except (ValueError, OSError):
                task.delay, task.default_due = previous
                raise
            return True
        return changed

    def poll_reset_status(self):
        while True:
            try:
                info = self.reset_updates.get_nowait()
            except queue.Empty:
                break
            self.reset_pending = False
            if info is not None:
                self.reset_info = info
        changed = False
        for row in self.rows:
            if not row.time_editing:
                changed = self.apply_reset_default(row.task) or changed
        if changed:
            self.save_later()
        if self.reset_reader is not None and not self.reset_pending and time.monotonic() >= self.next_reset_read:
            self.reset_pending = True
            self.next_reset_read = time.monotonic() + 60

            def read():
                try:
                    info = self.reset_reader(self.scheduler.executable)
                except Exception:
                    info = None
                self.reset_updates.put(info)

            threading.Thread(target=read, daemon=True).start()

    def tick(self):
        try:
            self.poll_reset_status()
            self.scheduler.tick()
            active_ids = {task.id for task in self.scheduler.tasks}
            for row in self.rows[:]:
                if row.task.id not in active_ids:
                    self.rows.remove(row)
                    row.frame.destroy()
                    continue
                row.update()
        except OSError as exc:
            self.report_error(str(exc))
        finally:
            self.root.after(200, self.tick)

    def close(self):
        if any(task.status == "running" for task in self.scheduler.tasks):
            self.root.iconify()
            return
        for row in self.rows:
            row.confirm_time()
            row.sync_content()
        if self.save():
            self.root.destroy()


def instance_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / ".instance.lock").open("a+b")
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        if handle.read(1) == b"":
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def main():
    if os.name == "nt":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TheNi.CodexTaskTimer")
    root = tk.Tk()
    root.withdraw()
    try:
        lock = instance_lock(DATA)
    except OSError:
        messagebox.showinfo("Codex Task Timer", "Codex Task Timer is already open. Check your taskbar.", parent=root)
        root.destroy()
        return
    try:
        app = App(root, Scheduler(Store(DATA), find_codex()))
    except Exception as exc:
        messagebox.showerror("Codex Task Timer", f"Could not open the app:\n\n{exc}", parent=root)
        root.destroy()
        lock.close()
        return
    root.deiconify()
    root.mainloop()
    lock.close()


if __name__ == "__main__":
    main()
