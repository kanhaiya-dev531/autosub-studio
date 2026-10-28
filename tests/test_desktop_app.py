"""Drive the desktop app's real code (StudioApp) against the real engine, with a stand-in for tkinter.

tkinter cannot be installed in every environment (CI, minimal Linux images), so these tests put a small fake
`tkinter` in place. The fake keeps widget state in memory (trees, variables, packing), which is enough to check
what the app does when somebody clicks: which service calls are made, what the lists show, what gets saved.
It does not draw anything, so look at the real window once by hand (python run_desktop.py).
"""

from __future__ import annotations

import importlib
import sys
import types
from unittest.mock import MagicMock

import pytest
from conftest import make_config, needs_ffmpeg, wait_until

from autosub.accounts import Accounts
from autosub.service import JobService

pytestmark = needs_ffmpeg


# ---- the stand-in toolkit ---------------------------------------------------------------
class FakeVar:
    def __init__(self, value="", **_kw):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


class FakeWidget:
    """Accepts any option or call; remembers only what the app later asks about."""

    def __init__(self, *_args, **kw):
        self.options = dict(kw)
        self._manager = ""
        self.disabled = False

    def __getattr__(self, name):  # any other Tk method is a harmless no-op
        if name.startswith("__"):
            raise AttributeError(name)
        return MagicMock(name=name)

    def pack(self, **_kw):
        self._manager = "pack"

    def grid(self, **_kw):
        self._manager = "grid"

    def pack_forget(self):
        self._manager = ""

    def pack_propagate(self, _flag):
        return None

    def winfo_manager(self):
        return self._manager

    def configure(self, **kw):
        self.options.update(kw)

    config = configure

    def state(self, flags):
        for flag in flags:
            if flag in ("disabled", "!disabled"):
                self.disabled = flag == "disabled"

    def current(self, index=None):
        if index is not None:
            self._index = index
        return getattr(self, "_index", 0)


class FakeTree(FakeWidget):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.items: dict[str, dict] = {}
        self.selected: tuple[str, ...] = ()

    def insert(self, _parent, _index, iid=None, values=(), **_kw):
        self.items[iid] = {"values": list(values)}
        return iid

    def get_children(self, _item=""):
        return tuple(self.items)

    def delete(self, *ids):
        for iid in ids:
            self.items.pop(iid, None)
        self.selected = tuple(i for i in self.selected if i in self.items)

    def selection(self):
        return self.selected

    def selection_set(self, *ids):
        self.selected = tuple(ids[0:1]) if len(ids) == 1 and isinstance(ids[0], str) else tuple(ids)

    def set(self, iid, column, value):
        order = {"status": 1}
        self.items[iid]["values"][order[column]] = value

    def values_of(self, iid):
        return self.items[iid]["values"]


class FakeRoot(FakeWidget):
    def __init__(self):
        super().__init__()
        self.scheduled: list = []
        self.destroyed = False

    def after(self, _ms, func=None, *args):
        self.scheduled.append((func, args))
        return f"after#{len(self.scheduled)}"

    def destroy(self):
        self.destroyed = True


def _install_fake_tk(monkeypatch):
    tk = types.ModuleType("tkinter")
    ttk = types.ModuleType("tkinter.ttk")
    for name, cls in {"Tk": FakeRoot, "Toplevel": FakeWidget, "Menu": FakeWidget, "Text": FakeWidget,
                      "StringVar": FakeVar, "Misc": FakeWidget}.items():
        setattr(tk, name, cls)
    for name in ("Frame", "Button", "Label", "Entry", "Combobox", "Scrollbar", "Progressbar", "Radiobutton",
                 "LabelFrame", "PanedWindow", "Style"):
        setattr(ttk, name, FakeWidget)
    ttk.Treeview = FakeTree
    ttk.Style = lambda *_a, **_k: MagicMock(theme_names=lambda: ["clam"])
    sub = {"filedialog": types.ModuleType("tkinter.filedialog"), "messagebox": types.ModuleType("tkinter.messagebox"),
           "simpledialog": types.ModuleType("tkinter.simpledialog")}
    tk.ttk = ttk
    for key, module in sub.items():
        setattr(tk, key, module)
        monkeypatch.setitem(sys.modules, f"tkinter.{key}", module)
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setitem(sys.modules, "tkinter.ttk", ttk)
    return sub


@pytest.fixture
def gui(monkeypatch, tmp_path):
    """The app module imported against the fake toolkit, with every dialog replaced by a recorder."""
    dialogs = _install_fake_tk(monkeypatch)
    for name in [m for m in sys.modules if m == "desktop.app" or m == "PIL.ImageTk"]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "PIL.ImageTk", types.SimpleNamespace(PhotoImage=lambda image: ("photo", image.size)))
    import PIL

    monkeypatch.setattr(PIL, "ImageTk", sys.modules["PIL.ImageTk"], raising=False)
    app_module = importlib.import_module("desktop.app")

    fd, mb, sd = dialogs["filedialog"], dialogs["messagebox"], dialogs["simpledialog"]
    fd.askopenfilename = MagicMock(return_value="")
    fd.asksaveasfilename = MagicMock(return_value="")
    mb.showerror, mb.showinfo = MagicMock(), MagicMock()
    mb.askyesno = MagicMock(return_value=True)
    sd.askfloat = MagicMock(return_value=None)
    yield types.SimpleNamespace(module=app_module, fd=fd, mb=mb, sd=sd, tmp=tmp_path)
    monkeypatch.delitem(sys.modules, "desktop.app", raising=False)


@pytest.fixture
def app(gui):
    cfg = make_config(gui.tmp)
    service = JobService(cfg)
    owner = Accounts(service.repo).local_user(cfg.data_dir)["id"]
    instance = gui.module.StudioApp(cfg, service, owner, FakeRoot())
    yield instance
    service.shutdown()


def settle(app, condition, timeout=60):
    """Let the worker threads finish, picking their results up the way the window's timers would."""

    def step():
        app._drain()
        app._poll()
        return condition()

    return wait_until(step, timeout=timeout)


@pytest.fixture
def open_project(app, gui, sample_video):
    gui.fd.askopenfilename.return_value = str(sample_video)
    app.open_video()
    settle(app, lambda: app.job_id is not None)
    settle(app, lambda: app.job_status == "ready" and app.rows)
    return app


# ---- tests ----------------------------------------------------------------------------------
def test_starts_empty_with_editor_disabled(app):
    assert app.title_var.get() == "No project open"
    assert app.cue_tree.get_children() == ()
    assert all(w.disabled for w in app.editor_buttons)
    assert app.render_btn.options["text"] == "Render video with Pop…"


def test_opening_a_video_creates_a_project_and_shows_its_lines(open_project, gui):
    app = open_project
    assert app.title_var.get() == "landscape.mp4"
    assert "subtitle lines" in app.status_var.get()
    assert len(app.rows) >= 3 and len(app.cue_tree.get_children()) == len(app.rows)
    first = app.cue_tree.values_of("0")
    assert first[0] == 1 and first[1].startswith("0:00:00") and first[3] == app.rows[0]["text"]
    assert not any(w.disabled for w in app.editor_buttons)
    assert app.projects.get_children() == (app.job_id,) and app.projects.values_of(app.job_id)[1] == "Ready"
    gui.mb.showerror.assert_not_called()


def test_a_file_that_is_not_a_video_shows_an_error_and_adds_nothing(app, gui, tmp_path):
    fake = tmp_path / "fake.mp4"
    fake.write_text("not a video")
    gui.fd.askopenfilename.return_value = str(fake)
    app.open_video()
    settle(app, lambda: gui.mb.showerror.called)
    assert "could not be read" in gui.mb.showerror.call_args.args[1]
    assert app.job_id is None and app.projects.get_children() == ()
    assert not list(app.cfg.uploads_dir.glob("*"))  # the copy was cleaned up
    assert app._importing is False


def test_cancelling_the_file_chooser_does_nothing(app, gui):
    app.open_video()
    assert app.job_id is None and app._importing is False


def test_previews_arrive_for_every_style(open_project):
    app = open_project
    settle(app, lambda: len(app._photos) == 3)
    assert set(app._photos) == {"classic", "pop", "neon"}
    assert all(photo[0] == "photo" and photo[1][0] <= 224 for photo in app._photos.values())


def test_editing_a_line_marks_it_dirty_and_saving_stores_it(open_project, gui):
    app = open_project
    old = dict(app.rows[0])
    edited = {**old, "text": "Typed in the desktop app"}
    gui.module.CueDialog = lambda *_a, **_k: types.SimpleNamespace(show=lambda _parent: ("save", edited))
    app.cue_tree.selection_set("0")
    app.edit_selected()
    assert app.dirty and app.save_var.get() == "Unsaved changes"
    assert app.cue_tree.values_of("0")[3] == "Typed in the desktop app"
    assert app.service.get_cues(app.job_id)[0].text != "Typed in the desktop app"  # not stored yet

    assert app.save() is True
    assert not app.dirty and app.save_var.get() == "All changes saved"
    assert app.service.get_cues(app.job_id)[0].text == "Typed in the desktop app"


def test_split_merge_add_and_delete(open_project, gui):
    app = open_project
    count = len(app.rows)
    app.cue_tree.selection_set("0")
    app.split_selected()
    assert len(app.rows) == count + 1 and app.rows[0]["end"] == app.rows[1]["start"]

    app.cue_tree.selection_set("0")
    app.merge_selected()
    assert len(app.rows) == count

    app.cue_tree.selection_set(str(count - 1))
    app.merge_selected()  # nothing after the last line
    assert len(app.rows) == count and "no line after" in app.notice_var.get()

    app.cue_tree.selection_set("1")
    app.delete_selected()
    assert len(app.rows) == count - 1
    assert app.save() is True and len(app.service.get_cues(app.job_id)) == count - 1


def test_invalid_edits_are_reported_and_not_lost(open_project, gui):
    app = open_project
    app.rows[0]["end"] = app.rows[0]["start"]  # an end before the start cannot be stored
    app.mark_dirty()
    assert app.save() is False
    assert app.dirty and app.save_var.get() == "Not saved"
    assert "after the start" in gui.mb.showerror.call_args.args[1]


def test_actions_without_a_selection_explain_themselves(open_project):
    app = open_project
    app.edit_selected()
    assert "Select a subtitle line" in app.notice_var.get()


def test_shift_stretch_and_auto_sync(open_project, gui):
    app = open_project
    before = app.rows[0]["start"]
    gui.sd.askfloat.return_value = 0.5
    app.sync_shift()
    assert app.rows[0]["start"] == before + 500 and "0.50" in app.notice_var.get()
    gui.sd.askfloat.return_value = None  # cancelled
    app.sync_shift()
    assert app.rows[0]["start"] == before + 500
    gui.sd.askfloat.return_value = 1.1
    app.sync_stretch()
    assert app.rows[0]["start"] == round((before + 500) * 1.1)


def test_unsaved_edits_are_saved_before_a_sync(open_project, gui):
    app = open_project
    app.rows[0]["text"] = "Changed before syncing"
    app.mark_dirty()
    gui.sd.askfloat.return_value = 0.2
    app.sync_shift()
    assert not app.dirty and app.service.get_cues(app.job_id)[0].text == "Changed before syncing"


def test_import_srt_replaces_the_lines(open_project, gui, tmp_path):
    app = open_project
    srt = tmp_path / "subs.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello from a file\n", encoding="utf-8")
    gui.fd.askopenfilename.return_value = str(srt)
    gui.mb.askyesno.return_value = False  # do not match to the speech
    app.import_srt()
    assert [r["text"] for r in app.rows] == ["Hello from a file"]
    assert len(app.cue_tree.get_children()) == 1

    srt.write_bytes(b"\xff\xfe\x00" * 3 + b"\x81")  # unreadable as text in the tried encodings -> cp1252 fallback, no cues
    app.import_srt()
    assert gui.mb.showerror.called


@pytest.mark.parametrize(("kind", "marker"), [("srt", "-->"), ("vtt", "WEBVTT"), ("txt", ""), ("ass", "[Script Info]")])
def test_exports_write_files(open_project, gui, tmp_path, kind, marker):
    app = open_project
    target = tmp_path / f"out.{kind}"
    gui.fd.asksaveasfilename.return_value = str(target)
    app.export(kind)
    text = target.read_text(encoding="utf-8")
    assert marker in text and text.strip()
    if kind == "ass":
        assert gui.fd.asksaveasfilename.call_args.kwargs["initialfile"] == "landscape-pop.ass"


def test_export_cancelled_writes_nothing(open_project, gui, tmp_path):
    gui.fd.asksaveasfilename.return_value = ""
    open_project.export("srt")
    assert not list(tmp_path.glob("*.srt"))


def test_render_then_save_the_video(open_project, gui, tmp_path):
    app = open_project
    app.template.set("neon")
    app._on_template()
    assert app.render_btn.options["text"] == "Render video with Neon…"
    target = tmp_path / "result.mp4"
    gui.fd.asksaveasfilename.return_value = str(target)
    app.render_video()
    assert app.render_id and app.render_btn.disabled
    settle(app, lambda: app.render_id is None, timeout=120)
    assert target.exists() and target.read_bytes()[4:8] == b"ftyp"
    assert not app.render_btn.disabled and app.render_var.get() == "Video ready"
    assert gui.fd.asksaveasfilename.call_args.kwargs["initialfile"] == "landscape-neon.mp4"


def test_saving_the_last_render_later(open_project, gui, tmp_path):
    app = open_project
    app.save_last_render()
    assert "no rendered video" in app.notice_var.get()
    gui.fd.asksaveasfilename.return_value = ""  # cancel the first prompt
    app.render_video()
    settle(app, lambda: app.render_id is None, timeout=120)
    assert "Video kept" in app.notice_var.get()
    target = tmp_path / "later.mp4"
    gui.fd.asksaveasfilename.return_value = str(target)
    app.save_last_render()
    assert target.exists()


def test_deleting_a_project_clears_the_window(open_project, gui):
    app = open_project
    job_id = app.job_id
    app.delete_project()
    assert app.service.get_job(job_id) is None
    assert app.job_id is None and app.title_var.get() == "No project open" and app.cue_tree.get_children() == ()
    assert app.projects.get_children() == () and all(w.disabled for w in app.editor_buttons)


def test_declining_the_delete_prompt_keeps_the_project(open_project, gui):
    gui.mb.askyesno.return_value = False
    open_project.delete_project()
    assert open_project.service.get_job(open_project.job_id) is not None


def test_closing_saves_pending_edits_and_shuts_the_engine_down(open_project):
    app = open_project
    app.rows[0]["text"] = "Last minute change"
    app.mark_dirty()
    app.close()
    assert app.root.destroyed
    assert app.service.get_cues(app.job_id)[0].text == "Last minute change"


def test_project_that_disappears_clears_the_window(open_project):
    app = open_project
    app.service.delete_job(app.job_id)  # removed behind the window's back (for example by the cleanup job)
    app._poll()
    assert app.job_id is None and app.projects.get_children() == ()


def test_switching_projects_saves_the_first_one(open_project, gui, sample_video, silent_video):
    app = open_project
    first = app.job_id
    app.rows[0]["text"] = "Edited in the first project"
    app.mark_dirty()
    gui.fd.askopenfilename.return_value = str(silent_video)
    app.open_video()
    settle(app, lambda: app.job_id not in (None, first))
    second = app.job_id
    assert second != first
    assert app.service.get_cues(first)[0].text == "Edited in the first project"  # saved when the window moved on
    app.projects.selection_set(first)
    app._on_select_project()
    assert app.job_id == first and app.rows[0]["text"] == "Edited in the first project"
