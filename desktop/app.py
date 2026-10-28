"""AutoSub Studio for the desktop (tkinter). Same engine, database and styles as the website.

    python run_desktop.py

The window never does slow work itself: probing, copying, speech recognition, previews and
rendering all run on worker threads inside `autosub.service.JobService`, and this module only
polls the database for progress. Editing rules live in `desktop/logic.py`.
"""

from __future__ import annotations

import functools
import logging
import os
import queue
import shutil
import sys
import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any

from PIL import Image, ImageTk

from autosub import styles
from autosub.accounts import Accounts
from autosub.config import Config
from autosub.db import DatabaseError
from autosub.languages import LANGUAGES
from autosub.media import MediaError
from autosub.service import JobError, JobService

from . import logic

log = logging.getLogger("autosub.desktop")

APP_NAME = "AutoSub Studio"
VIDEO_TYPES = [
    ("Video files", "*.mp4 *.mov *.mkv *.webm *.avi *.m4v *.mpg *.mpeg *.3gp *.wmv *.flv"),
    ("All files", "*.*"),
]
THUMB_SIZE = (224, 126)
PLACEHOLDER = "Preview appears\nwhen the subtitles are ready"
ACTIVE = ("uploaded", "transcribing")
Handler = Callable[..., Any]


def guarded(method: Handler) -> Handler:
    """Show a friendly message instead of a traceback when something the person did cannot work."""

    @functools.wraps(method)
    def wrapper(self: StudioApp, *args: Any, **kwargs: Any) -> Any:
        try:
            return method(self, *args, **kwargs)
        except (JobError, MediaError, ValueError, OSError) as exc:
            self.error(str(exc))
            return None

    return wrapper


# ---------------------------------------------------------------------------
# the small window for editing one subtitle line
# ---------------------------------------------------------------------------
class CueDialog:
    """Edit the times and text of a line. `result` is None, ("save", row) or ("split", row, position)."""

    def __init__(self, parent: tk.Tk, row: logic.Row, title: str = "Edit line") -> None:
        self.result: tuple[Any, ...] | None = None
        self.row = row
        self.win = tk.Toplevel(parent)
        self.win.title(title)
        self.win.transient(parent)
        self.win.resizable(False, False)

        body = ttk.Frame(self.win, padding=14)
        body.pack(fill="both", expand=True)
        times = ttk.Frame(body)
        times.pack(fill="x")
        self.start = tk.StringVar(value=logic.format_time(row["start"]))
        self.end = tk.StringVar(value=logic.format_time(row["end"]))
        ttk.Label(times, text="Starts").grid(row=0, column=0, sticky="w")
        ttk.Label(times, text="Ends").grid(row=0, column=2, sticky="w", padx=(16, 0))
        ttk.Entry(times, textvariable=self.start, width=13, font=("Consolas", 10)).grid(row=1, column=0)
        ttk.Label(times, text="→").grid(row=1, column=1, padx=8)
        ttk.Entry(times, textvariable=self.end, width=13, font=("Consolas", 10)).grid(row=1, column=2, padx=(16, 0))

        ttk.Label(body, text="Text").pack(anchor="w", pady=(12, 2))
        self.text = tk.Text(body, width=54, height=4, wrap="word", undo=True, font=("Segoe UI", 11))
        self.text.pack(fill="x")
        self.text.insert("1.0", row["text"])
        self.text.focus_set()

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="Split at cursor", command=self._split).pack(side="left")
        ttk.Button(buttons, text="Save", style="Accent.TButton", command=self._save).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self.win.destroy).pack(side="right", padx=8)
        self.win.bind("<Escape>", lambda _e: self.win.destroy())
        self.win.bind("<Control-Return>", lambda _e: self._save())

    def _read(self) -> logic.Row | None:
        try:
            row = logic.validate_row(self.start.get(), self.end.get(), self.text.get("1.0", "end"))
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.win)
            return None
        row["orig"] = self.row["orig"]
        return row

    def _save(self) -> None:
        row = self._read()
        if row:
            self.result = ("save", row)
            self.win.destroy()

    def _split(self) -> None:
        row = self._read()
        if not row:
            return
        # the position is counted in the tidied text (single spaces), which is what the row holds
        position = len(" ".join(self.text.get("1.0", "insert").split()))
        if position == 0 or position >= len(row["text"]):
            messagebox.showinfo(APP_NAME, "Put the cursor inside the text where the line should split.", parent=self.win)
            return
        self.result = ("split", row, position)
        self.win.destroy()

    def show(self, parent: tk.Tk) -> tuple[Any, ...] | None:
        self.win.grab_set()
        parent.wait_window(self.win)
        return self.result


# ---------------------------------------------------------------------------
# the main window
# ---------------------------------------------------------------------------
class StudioApp:
    POLL_MS = 700       # how often progress is read from the database
    QUEUE_MS = 120      # how often results from worker threads are picked up
    AUTOSAVE_MS = 1500  # pause after the last edit before it is saved

    def __init__(self, cfg: Config, service: JobService, owner_id: str, root: tk.Tk | None = None) -> None:
        self.cfg, self.service, self.owner_id = cfg, service, owner_id
        self.root = root or tk.Tk()
        self.events: queue.Queue[tuple[Any, ...]] = queue.Queue()

        self.job_id: str | None = None
        self.job_status = ""
        self.rows: list[logic.Row] = []
        self.dirty = False
        self._autosave: str | None = None
        self._project_order: list[tuple[str, str]] = []
        self._preview_gen = 0
        self._photos: dict[str, Any] = {}
        self.render_id: str | None = None
        self.last_render: dict | None = None
        self._importing = False

        self._build_style()
        self._build_menu()
        self._build_layout()
        self._set_editor_enabled(False)
        self._on_template()
        self.refresh_projects()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(self.QUEUE_MS, self._drain)
        self.root.after(self.POLL_MS, self._poll)

    # ---- building the window -----------------------------------------------
    def _build_style(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"), padding=(12, 6))
        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("Hint.TLabel", foreground="#5a5f78")
        style.configure("Treeview", rowheight=26)

    def _build_menu(self) -> None:
        bar = tk.Menu(self.root)
        file_menu = tk.Menu(bar, tearoff=False)
        file_menu.add_command(label="Open video…", accelerator="Ctrl+O", command=self.open_video)
        file_menu.add_command(label="Save changes", accelerator="Ctrl+S", command=self.save)
        file_menu.add_separator()
        for kind in ("srt", "vtt", "txt", "ass"):
            file_menu.add_command(label=f"Export .{kind}…", command=functools.partial(self.export, kind))
        file_menu.add_command(label="Save last rendered video…", command=self.save_last_render)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self.close)
        bar.add_cascade(label="File", menu=file_menu)

        timing = tk.Menu(bar, tearoff=False)
        timing.add_command(label="Shift all lines…", command=self.sync_shift)
        timing.add_command(label="Fix drift (stretch)…", command=self.sync_stretch)
        timing.add_command(label="Match to the speech automatically", command=self.sync_auto)
        timing.add_separator()
        timing.add_command(label="Import an .srt file…", command=self.import_srt)
        bar.add_cascade(label="Timing", menu=timing)

        help_menu = tk.Menu(bar, tearoff=False)
        help_menu.add_command(label="About", command=self.about)
        bar.add_cascade(label="Help", menu=help_menu)
        self.root.configure(menu=bar)
        self.root.bind("<Control-o>", lambda _e: self.open_video())
        self.root.bind("<Control-s>", lambda _e: self.save())

    def _build_layout(self) -> None:
        self.root.title(APP_NAME)
        self.root.geometry("1200x780")
        self.root.minsize(1000, 660)
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        top = ttk.Frame(outer)
        top.pack(fill="x")
        ttk.Button(top, text="Open video…", style="Accent.TButton", command=self.open_video).pack(side="left")
        ttk.Label(top, text="Spoken language").pack(side="left", padx=(16, 4))
        self.language = ttk.Combobox(top, state="readonly", width=22, values=[name for _, name in LANGUAGES])
        self.language.current(0)
        self.language.pack(side="left")
        self.notice_var = tk.StringVar(value="Open a video to create subtitles for it.")
        ttk.Label(top, textvariable=self.notice_var, style="Hint.TLabel").pack(side="right")

        panes = ttk.PanedWindow(outer, orient="horizontal")
        panes.pack(fill="both", expand=True, pady=(10, 0))

        # -- projects --
        left = ttk.Frame(panes, padding=(0, 0, 8, 0))
        panes.add(left, weight=1)
        ttk.Label(left, text="Projects", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 4))
        self.projects = ttk.Treeview(left, columns=("name", "status"), show="headings", selectmode="browse", height=12)
        self.projects.heading("name", text="Video")
        self.projects.heading("status", text="Status")
        self.projects.column("name", width=170, anchor="w")
        self.projects.column("status", width=90, anchor="w")
        self.projects.pack(fill="both", expand=True)
        self.projects.bind("<<TreeviewSelect>>", self._on_select_project)
        self.delete_btn = ttk.Button(left, text="Delete project", command=self.delete_project)
        self.delete_btn.pack(fill="x", pady=(8, 0))

        # -- the open project --
        right = ttk.Frame(panes)
        panes.add(right, weight=4)
        self.title_var = tk.StringVar(value="No project open")
        ttk.Label(right, textvariable=self.title_var, style="Title.TLabel").pack(anchor="w")
        self.status_var = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.status_var, style="Hint.TLabel").pack(anchor="w")
        self.bar_slot = ttk.Frame(right)  # holds the progress bar only while a video is being transcribed
        self.bar_slot.pack(fill="x")
        self.job_bar = ttk.Progressbar(self.bar_slot, maximum=100, mode="determinate")

        cues = ttk.Frame(right)
        cues.pack(fill="both", expand=True, pady=(8, 0))
        self.cue_tree = ttk.Treeview(cues, columns=("n", "start", "end", "text"), show="headings", selectmode="browse")
        for name, label, width in (("n", "#", 44), ("start", "Starts", 100), ("end", "Ends", 100), ("text", "Text", 560)):
            self.cue_tree.heading(name, text=label)
            self.cue_tree.column(name, width=width, anchor="w", stretch=name == "text")
        scroll = ttk.Scrollbar(cues, orient="vertical", command=self.cue_tree.yview)
        self.cue_tree.configure(yscrollcommand=scroll.set)
        self.cue_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.cue_tree.bind("<Double-1>", lambda _e: self.edit_selected())
        self.cue_tree.bind("<Return>", lambda _e: self.edit_selected())
        self.cue_tree.bind("<Delete>", lambda _e: self.delete_selected())

        row_buttons = ttk.Frame(right)
        row_buttons.pack(fill="x", pady=(6, 0))
        self.editor_buttons: list[ttk.Button] = []
        for text, command in (
            ("Edit…", self.edit_selected), ("Split", self.split_selected), ("Merge with next", self.merge_selected),
            ("Add below", self.add_below), ("Delete", self.delete_selected), ("Save", self.save),
        ):
            button = ttk.Button(row_buttons, text=text, command=command)
            button.pack(side="left", padx=(0, 6))
            self.editor_buttons.append(button)
        self.save_var = tk.StringVar(value="")
        ttk.Label(row_buttons, textvariable=self.save_var, style="Hint.TLabel").pack(side="right")

        # -- caption styles --
        self.template = tk.StringVar(value="pop")
        frame = ttk.LabelFrame(right, text="Caption style", padding=8)
        frame.pack(fill="x", pady=(10, 0))
        self.thumbs: dict[str, ttk.Label] = {}
        self.style_radios: list[ttk.Radiobutton] = []
        for column, template in enumerate(styles.TEMPLATES.values()):
            cell = ttk.Frame(frame, padding=(0, 0, 12, 0))
            cell.grid(row=0, column=column, sticky="n")
            box = ttk.Frame(cell, width=THUMB_SIZE[0], height=THUMB_SIZE[1])
            box.pack_propagate(False)  # keep the box the size of a thumbnail whatever it holds
            box.pack()
            thumb = ttk.Label(box, text=PLACEHOLDER, anchor="center", justify="center")
            thumb.pack(fill="both", expand=True)
            radio = ttk.Radiobutton(cell, text=template.name, value=template.key, variable=self.template,
                                    command=self._on_template)
            radio.pack(anchor="w", pady=(4, 0))
            ttk.Label(cell, text=template.tagline, style="Hint.TLabel", wraplength=220).pack(anchor="w")
            self.thumbs[template.key] = thumb
            self.style_radios.append(radio)

        # -- exporting --
        actions = ttk.Frame(right)
        actions.pack(fill="x", pady=(10, 0))
        self.export_buttons: list[ttk.Button] = []
        for kind in ("srt", "vtt", "txt", "ass"):
            button = ttk.Button(actions, text=f".{kind}", command=functools.partial(self.export, kind))
            button.pack(side="left", padx=(0, 6))
            self.export_buttons.append(button)
        self.render_btn = ttk.Button(actions, text="Render video…", style="Accent.TButton", command=self.render_video)
        self.render_btn.pack(side="left", padx=(14, 8))
        self.render_bar = ttk.Progressbar(actions, maximum=100, mode="determinate", length=160)
        self.render_var = tk.StringVar(value="")
        ttk.Label(actions, textvariable=self.render_var, style="Hint.TLabel").pack(side="left", padx=(8, 0))

        self.editor_buttons.append(self.render_btn)
        self.editor_buttons.extend(self.export_buttons)

    # ---- small helpers ---------------------------------------------------------
    def notice(self, message: str) -> None:
        self.notice_var.set(message)

    def error(self, message: str) -> None:
        self.notice(message)
        messagebox.showerror(APP_NAME, message, parent=self.root)

    def _set_editor_enabled(self, enabled: bool) -> None:
        flag = "!disabled" if enabled else "disabled"
        for widget in (*self.editor_buttons, *self.style_radios):
            widget.state([flag])

    def about(self) -> None:
        messagebox.showinfo(
            APP_NAME,
            f"{APP_NAME}\nVideo to subtitles with styled caption templates.\n\n"
            f"Speech engine: {self.cfg.transcriber}\nDatabase: {'MySQL' if self.cfg.uses_mysql else 'SQLite'}\n"
            f"Data folder: {self.cfg.data_dir}",
            parent=self.root,
        )

    # ---- projects ------------------------------------------------------------
    def refresh_projects(self, select: str | None = None) -> None:
        jobs = self.service.list_jobs(self.owner_id, 50)
        self._fill_projects(jobs, select or self.job_id)

    def _fill_projects(self, jobs: list[dict], keep: str | None) -> None:
        order = [(j["id"], j["original_name"]) for j in jobs]
        if order != self._project_order:  # a project was added, removed or renamed
            self._project_order = order
            self.projects.delete(*self.projects.get_children())
            for job in jobs:
                self.projects.insert("", "end", iid=job["id"], values=(job["original_name"], ""))
            if keep and any(j["id"] == keep for j in jobs):
                self.projects.selection_set(keep)
        for job in jobs:  # progress changes often, so only the status cell is touched
            short = {"ready": "Ready", "failed": "Failed"}.get(job["status"], f"{job.get('progress') or 0}%")
            self.projects.set(job["id"], "status", short)

    def _on_select_project(self, _event: object = None) -> None:
        chosen = self.projects.selection()
        if not chosen or chosen[0] == self.job_id:
            return
        if not self.save(quiet=True):
            if self.job_id:  # stay on the project whose edits could not be saved
                self.projects.selection_set(self.job_id)
            return
        self.open_project(chosen[0])

    @guarded
    def open_project(self, job_id: str) -> None:
        if self.job_id and self.job_id != job_id and not self.save(quiet=True):
            return  # keep showing the project whose edits could not be stored, so nothing is lost
        job = self.service.get_job(job_id)
        if job is None:
            self.error("That project no longer exists.")
            self.refresh_projects()
            return
        self.job_id, self.job_status = job_id, ""
        self.rows, self.dirty = [], False
        self.save_var.set("")
        self._render_rows()
        self._photos.clear()
        for thumb in self.thumbs.values():
            thumb.configure(image="", text=PLACEHOLDER)
        self._show_job(job)

    def _show_job(self, job: dict) -> None:
        """Bring the right-hand side in line with the job row. Called when opening and when status changes."""
        self.title_var.set(job["original_name"])
        self.status_var.set(logic.status_line(job))
        was = self.job_status
        self.job_status = job["status"]
        if job["status"] in ACTIVE:
            self.job_bar.configure(value=job.get("progress") or 0)
            if not self.job_bar.winfo_manager():
                self.job_bar.pack(fill="x", pady=(4, 0))
            self._set_editor_enabled(False)
            return
        self.job_bar.pack_forget()
        if job["status"] == "ready":
            self._set_editor_enabled(True)
            if was != "ready":
                self.rows = logic.rows_from_cues(self.service.get_cues(job["id"]))
                self._render_rows()
                self._load_previews()
        else:
            self._set_editor_enabled(False)

    @guarded
    def open_video(self) -> None:
        if self._importing:
            self.notice("Still adding the previous video…")
            return
        chosen = filedialog.askopenfilename(parent=self.root, title="Choose a video", filetypes=VIDEO_TYPES)
        if not chosen:
            return
        language = LANGUAGES[max(0, self.language.current())][0]
        self._importing = True
        self.notice(f"Adding {Path(chosen).name}…")
        threading.Thread(target=self._import_worker, args=(Path(chosen), language), daemon=True).start()

    def _import_worker(self, source: Path, language: str) -> None:
        job_id, target = self.service.new_upload_path(source.name)
        try:
            shutil.copy2(source, target)
            job = self.service.create_job(self.owner_id, target, source.name, language, job_id)
        except (JobError, MediaError, OSError) as exc:
            Path(target).unlink(missing_ok=True)
            self.events.put(("import-failed", str(exc)))
        except Exception as exc:  # never lose the error silently in a worker thread
            log.exception("Adding a video failed")
            Path(target).unlink(missing_ok=True)
            self.events.put(("import-failed", f"Unexpected problem: {exc}"))
        else:
            self.events.put(("imported", job))

    @guarded
    def delete_project(self) -> None:
        if not self.job_id:
            return
        name = self.title_var.get()
        if not messagebox.askyesno(APP_NAME, f'Delete "{name}"?\nIts subtitles and the stored copy of the video are removed.',
                                   parent=self.root):
            return
        self.service.delete_job(self.job_id)
        self._clear_project()
        self.refresh_projects()
        self.notice("Project deleted.")

    def _clear_project(self) -> None:
        self.job_id, self.job_status, self.rows, self.dirty = None, "", [], False
        self._preview_gen += 1
        self.title_var.set("No project open")
        self.status_var.set("")
        self.job_bar.pack_forget()
        self._render_rows()
        self._set_editor_enabled(False)

    # ---- subtitle lines ----------------------------------------------------------------
    def _render_rows(self, select: int | None = None) -> None:
        self.cue_tree.delete(*self.cue_tree.get_children())
        for i, row in enumerate(self.rows):
            self.cue_tree.insert("", "end", iid=str(i), values=(
                i + 1, logic.format_time(row["start"]), logic.format_time(row["end"]), row["text"]))
        if select is not None and 0 <= select < len(self.rows):
            self.cue_tree.selection_set(str(select))
            self.cue_tree.see(str(select))
            self.cue_tree.focus(str(select))

    def _selected(self) -> int | None:
        chosen = self.cue_tree.selection()
        return int(chosen[0]) if chosen else None

    def _need_selection(self) -> int | None:
        index = self._selected()
        if index is None:
            self.notice("Select a subtitle line first.")
        return index

    def mark_dirty(self) -> None:
        self.dirty = True
        self.save_var.set("Unsaved changes")
        if self._autosave:
            self.root.after_cancel(self._autosave)
        self._autosave = self.root.after(self.AUTOSAVE_MS, lambda: self.save(quiet=True))

    @guarded
    def edit_selected(self) -> None:
        index = self._need_selection()
        if index is None:
            return
        outcome = CueDialog(self.root, self.rows[index]).show(self.root)
        if not outcome:
            return
        if outcome[0] == "save":
            self.rows[index] = outcome[1]
            select = index
        else:
            _, row, position = outcome
            left, right = logic.split_row(row, position)
            self.rows[index:index + 1] = [left, right]
            select = index
        self._render_rows(select)
        self.mark_dirty()

    @guarded
    def split_selected(self) -> None:
        index = self._need_selection()
        if index is None:
            return
        left, right = logic.split_row(self.rows[index])
        self.rows[index:index + 1] = [left, right]
        self._render_rows(index)
        self.mark_dirty()

    @guarded
    def merge_selected(self) -> None:
        index = self._need_selection()
        if index is None:
            return
        if index + 1 >= len(self.rows):
            self.notice("There is no line after this one to merge with.")
            return
        self.rows[index:index + 2] = [logic.merge_rows(self.rows[index], self.rows[index + 1])]
        self._render_rows(index)
        self.mark_dirty()

    @guarded
    def add_below(self) -> None:
        index = self._selected()
        new = logic.new_row_after(self.rows, index)
        position = len(self.rows) if index is None else index + 1
        self.rows.insert(position, new)
        self._render_rows(position)
        self.edit_selected()
        if not self.rows[position]["text"].strip():  # the person cancelled the empty line
            del self.rows[position]
            self._render_rows(max(0, position - 1) if self.rows else None)
        else:
            self.mark_dirty()

    @guarded
    def delete_selected(self) -> None:
        index = self._need_selection()
        if index is None:
            return
        del self.rows[index]
        self._render_rows(min(index, len(self.rows) - 1) if self.rows else None)
        self.mark_dirty()

    def save(self, quiet: bool = False) -> bool:
        """Store the edited lines. Returns False (after telling the person) when they could not be stored."""
        if self._autosave:
            self.root.after_cancel(self._autosave)
            self._autosave = None
        if not self.job_id or not self.dirty:
            return True
        try:
            cues = self.service.save_cues(self.job_id, logic.to_items(self.rows))
        except (JobError, ValueError) as exc:
            self.save_var.set("Not saved")
            self.error(str(exc))
            return False
        self.rows = logic.rows_from_cues(cues)
        self.dirty = False
        self.save_var.set("All changes saved")
        self._render_rows(self._selected())
        self._load_previews()
        if not quiet:
            self.notice("Subtitles saved.")
        return True

    # ---- timing --------------------------------------------------------------------------
    def _apply_sync(self, mode: str, **params: Any) -> None:
        if not self.job_id or not self.save():
            return
        cues, message = self.service.apply_sync(self.job_id, mode, **params)
        self.rows = logic.rows_from_cues(cues)
        self._render_rows()
        self._load_previews()
        self.notice(message)

    @guarded
    def sync_shift(self) -> None:
        if not self.job_id:
            return
        seconds = simpledialog.askfloat(
            "Shift all lines", "Seconds to move every line (positive = later, negative = earlier):",
            parent=self.root, minvalue=-3600.0, maxvalue=3600.0, initialvalue=0.0)
        if seconds is not None:
            self._apply_sync("offset", delta_ms=round(seconds * 1000))

    @guarded
    def sync_stretch(self) -> None:
        if not self.job_id:
            return
        factor = simpledialog.askfloat(
            "Fix drift", "Stretch factor (for example 1.0427 turns 23.976 fps timing into 25 fps timing):",
            parent=self.root, minvalue=0.5, maxvalue=2.0, initialvalue=1.0)
        if factor is not None:
            self._apply_sync("stretch", factor=factor)

    @guarded
    def sync_auto(self) -> None:
        if self.job_id:
            self._apply_sync("auto")

    @guarded
    def import_srt(self) -> None:
        if not self.job_id or not self.save():
            return
        chosen = filedialog.askopenfilename(parent=self.root, title="Choose an .srt file",
                                            filetypes=[("Subtitle files", "*.srt"), ("All files", "*.*")])
        if not chosen:
            return
        raw = Path(chosen).read_bytes()
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("That subtitle file is too large.")
        for encoding in ("utf-8-sig", "utf-16", "cp1252"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeError:
                continue
        else:
            raise ValueError("The file could not be read as text.")
        auto = messagebox.askyesno(APP_NAME, "Match the timing of this file to the speech in the video?\n\n"
                                   "Choose Yes if the file came from a different version of the video.", parent=self.root)
        cues, message = self.service.import_srt(self.job_id, text, auto_sync=auto)
        self.rows = logic.rows_from_cues(cues)
        self._render_rows()
        self._load_previews()
        self.notice(message)

    # ---- previews --------------------------------------------------------------------------
    def _on_template(self) -> None:
        self.render_btn.configure(text=f"Render video with {styles.get_template(self.template.get()).name}…")

    def _load_previews(self) -> None:
        job_id = self.job_id
        if not job_id or self.job_status != "ready":
            return
        self._preview_gen += 1
        generation = self._preview_gen
        service = self.service

        def work() -> None:
            for key in styles.TEMPLATES:
                if generation != self._preview_gen:
                    return
                try:
                    path = service.ensure_preview(job_id, key)
                    with Image.open(path) as shot:
                        image = shot.convert("RGB")
                    image.thumbnail(THUMB_SIZE)
                    self.events.put(("preview", generation, key, image))
                except (JobError, MediaError, OSError) as exc:
                    self.events.put(("preview-failed", generation, key, str(exc)))

        threading.Thread(target=work, daemon=True).start()

    # ---- exporting and rendering ---------------------------------------------------------------
    @guarded
    def export(self, kind: str) -> None:
        if not self.job_id or not self.save():
            return
        job = self.service.get_job(self.job_id)
        if job is None:
            return
        if kind == "ass":
            template = self.template.get()
            text = self.service.export_ass(self.job_id, template)
        else:
            template = None
            text = getattr(self.service, f"export_{kind}")(self.job_id)
        target = filedialog.asksaveasfilename(
            parent=self.root, title=f"Save .{kind}", defaultextension=f".{kind}",
            initialfile=logic.export_name(job, kind, template), filetypes=[(f".{kind} file", f"*.{kind}")])
        if target:
            Path(target).write_text(text, encoding="utf-8")
            self.notice(f"Saved {Path(target).name}")

    @guarded
    def render_video(self) -> None:
        if not self.job_id or not self.save():
            return
        if self.render_id:
            self.notice("A video is already being rendered.")
            return
        render = self.service.start_render(self.job_id, self.template.get())
        self.render_id = render["id"]
        self.render_btn.state(["disabled"])
        if not self.render_bar.winfo_manager():
            self.render_bar.pack(side="left")
        self.render_bar.configure(value=0)
        self.render_var.set("Rendering…")

    def _poll_render(self) -> None:
        if not self.render_id:
            return
        render = self.service.get_render(self.render_id)
        if render is None:
            self.render_id = None
            return
        if render["status"] in ("queued", "rendering"):
            self.render_bar.configure(value=render.get("progress") or 0)
            self.render_var.set(f"Rendering… {render.get('progress') or 0}%")
            return
        self.render_id = None
        self.render_bar.pack_forget()
        if self.job_id:
            self.render_btn.state(["!disabled"])
        if render["status"] == "done":
            self.render_var.set("Video ready")
            self.last_render = render
            self.save_last_render()
        else:
            self.render_var.set("")
            self.error(render.get("message") or "The video could not be rendered.")

    @guarded
    def save_last_render(self) -> None:
        render = self.last_render
        if not render or not render.get("output_path") or not Path(render["output_path"]).is_file():
            self.notice("There is no rendered video to save yet.")
            return
        job = self.service.get_job(render["job_id"]) or {"original_name": "video.mp4"}
        target = filedialog.asksaveasfilename(
            parent=self.root, title="Save the video with subtitles", defaultextension=".mp4",
            initialfile=logic.export_name(job, "mp4", render["template"]), filetypes=[("MP4 video", "*.mp4")])
        if target:
            shutil.copyfile(render["output_path"], target)
            self.notice(f"Saved {Path(target).name}")
        else:
            self.notice('Video kept. Use File → "Save last rendered video…" to save it later.')

    # ---- background plumbing ----------------------------------------------------------------------
    def _drain(self) -> None:
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self.root.after(self.QUEUE_MS, self._drain)

    def _handle(self, event: tuple[Any, ...]) -> None:
        try:
            kind = event[0]
            if kind == "preview":
                _, generation, key, image = event
                if generation == self._preview_gen:
                    photo = ImageTk.PhotoImage(image)
                    self._photos[key] = photo  # keep a reference or Tk drops the picture
                    self.thumbs[key].configure(image=photo, text="")
            elif kind == "preview-failed":
                _, generation, key, message = event
                if generation == self._preview_gen:
                    self.thumbs[key].configure(image="", text="Preview not available")
                    log.warning("Preview failed: %s", message)
            elif kind == "imported":
                self._importing = False
                job = event[1]
                self.notice(f"Added {job['original_name']}")
                self.refresh_projects(select=job["id"])
                self.open_project(job["id"])
            elif kind == "import-failed":
                self._importing = False
                self.error(event[1])
        except Exception:  # a broken event must not stop the loop that handles the next ones
            log.exception("Could not handle %r", event[:1])

    def _poll(self) -> None:
        try:
            jobs = self.service.list_jobs(self.owner_id, 50)
            self._fill_projects(jobs, self.job_id)
            current = next((j for j in jobs if j["id"] == self.job_id), None) if self.job_id else None
            if self.job_id and current is None and not self.dirty:
                self._clear_project()
            elif current is not None and (current["status"] != self.job_status or current["status"] in ACTIVE):
                self._show_job(current)
            self._poll_render()
        except Exception:
            log.exception("Polling failed")
        self.root.after(self.POLL_MS, self._poll)

    def close(self) -> None:
        unsaved = self.dirty and not self.save(quiet=True)
        if unsaved and not messagebox.askyesno(APP_NAME, "Your latest edits could not be saved. Quit anyway?",
                                               parent=self.root):
            return
        busy = self.job_status in ACTIVE or self.render_id is not None
        if busy and not messagebox.askyesno(
                APP_NAME, "A video is still being processed. Quit anyway?\nThe unfinished work will be marked as failed.",
                parent=self.root):
            return
        self._preview_gen += 1
        self.service.shutdown()
        self.root.destroy()
        if busy:
            os._exit(0)  # worker threads would otherwise keep the process alive until they finish


# ---------------------------------------------------------------------------
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:  # sharper text on high-DPI Windows screens
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except (AttributeError, OSError, ImportError):
        pass

    root = tk.Tk()
    try:
        cfg = Config.from_env()
        service = JobService(cfg)
        owner = Accounts(service.repo).local_user(cfg.data_dir)["id"]
    except DatabaseError as exc:
        messagebox.showerror(APP_NAME, f"Could not open the database.\n\n{exc}\n\nCheck the DB_* values in your .env file.")
        return 1
    except Exception as exc:
        log.exception("Start-up failed")
        messagebox.showerror(APP_NAME, f"Could not start:\n\n{exc}")
        return 1
    StudioApp(cfg, service, owner, root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
