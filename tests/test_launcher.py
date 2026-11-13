"""Desktop launcher helpers and Claude Code discovery (no window is opened)."""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

import agent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from agenticcad import app as launcher  # noqa: E402


def test_repo_root_finds_server_py():
    root = launcher._repo_root()
    assert (root / "server.py").exists() and (root / "static" / "index.html").exists()


def test_free_port_prefers_requested_then_falls_back():
    assert launcher._free_port(0) > 0
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); s.listen(1)
        taken = s.getsockname()[1]
        port = launcher._free_port(taken)
        assert port != taken and port > 0



def test_designs_from_the_tabs_test_build_are_copied_once(tmp_path):
    tabs, data = tmp_path / "AgenticCAD Tabs", tmp_path / "AgenticCAD"
    (tabs / "designs").mkdir(parents=True); (tabs / "imports" / "logos").mkdir(parents=True); (data / "designs").mkdir(parents=True)
    (tabs / "designs" / "sheet_tiled.py").write_text("tiled")
    (tabs / "designs" / "sheet_tiled.cam.py").write_text("cam")
    (tabs / "designs" / "bracket.py").write_text("old copy")
    (tabs / "imports" / "logos" / "logo.step").write_text("step")
    (data / "designs" / "bracket.py").write_text("main's own")
    got = launcher._import_tabs_test_build(tabs, data)
    assert got == ["designs/sheet_tiled.cam.py", "designs/sheet_tiled.py", "imports/logos/logo.step"]
    assert (data / "designs" / "bracket.py").read_text() == "main's own"                 # never overwritten
    assert (data / "imports" / "logos" / "logo.step").read_text() == "step"
    (tabs / "designs" / "later.py").write_text("x")
    assert launcher._import_tabs_test_build(tabs, data) == [] and not (data / "designs" / "later.py").exists()   # once
    assert launcher._import_tabs_test_build(tmp_path / "none", tmp_path / "fresh") == []

class _FakeNative:
    """Stands in for pywebview's BrowserView, its NSWindow and its WKWebView in the page watch; each look is 1.5 s on its
    clock and runs when the test says so."""
    def __init__(self):
        self.webview = self.window = self
        self.on_screen, self.on_space, self.has_screen, self.page = True, True, True, "visible"
        self.replaced, self.gave_up, self.looks, self.next = [], [], 0, None
        self.mb, self.layout, self.now = 400, ["internal"], 1000.0

    def isVisible(self):
        return self.on_screen

    def occlusionState(self):
        return 8194 if self.on_screen else 8192

    def screen(self):
        return object() if self.has_screen else None

    def isOnActiveSpace(self):
        return self.on_space

    def evaluateJavaScript_completionHandler_(self, _js, done):
        self.looks += 1
        done(self.page, None)

    def later(self, _seconds, fn):
        self.next = fn

    def replace(self, why):
        self.replaced.append(why)
        self.page, self.mb = "visible", 400

    def watch(self, alive=lambda: True):
        launcher._watch_page(self, alive, self.later, self.replace, self.gave_up.append, lambda _v: self.mb << 20,
                             lambda: list(self.layout), lambda: self.now)
        return self

    def ticks(self, n, grow=0):
        for _ in range(n):
            self.now += 1.5
            self.mb += grow
            self.next()

    def settle(self):
        self.ticks(8)                               # past SCREENS_SETTLE from the start of the watch


def test_page_hidden_in_a_visible_window_is_replaced_after_four_looks_never_sooner():
    n = _FakeNative().watch()
    n.settle()
    assert not n.replaced                           # a healthy page is left alone
    n.page = "hidden"
    n.ticks(3)
    assert not n.replaced                           # a slow update lands well inside this
    n.ticks(1)
    assert n.replaced == ["hidden in a visible window"] and n.page == "visible"
    n.ticks(3)
    assert len(n.replaced) == 1


@pytest.mark.parametrize("away", ["on_screen", "on_space", "has_screen"])
def test_a_window_off_screen_may_sleep_and_is_not_touched(away):
    n = _FakeNative().watch()
    n.settle()
    n.page = "hidden"
    n.ticks(3)                                      # three hidden looks, then the window goes away
    setattr(n, away, False)
    looks = n.looks
    n.ticks(5)
    assert not n.replaced and n.looks == looks      # asking would wake a suspended page
    setattr(n, away, True)
    n.ticks(3)
    assert not n.replaced                           # the count started again
    n.ticks(1)
    assert len(n.replaced) == 1


def test_a_monitor_change_holds_the_watch_until_the_displays_settle():
    n = _FakeNative().watch()
    n.settle()
    n.page = "hidden"
    n.ticks(3)
    n.layout = ["internal", "external"]
    n.ticks(6)                                      # 9 s since the change: nothing yet, and no look at a page in the middle of it
    n.layout = ["external"]                         # the unplug comes in steps: the wait starts again
    looks = n.looks
    n.ticks(6)
    assert not n.replaced and n.looks == looks
    n.ticks(1 + 4)
    assert len(n.replaced) == 1


def test_a_runaway_footprint_is_stopped_wherever_the_window_is():
    n = _FakeNative().watch()
    n.on_space = False                              # the real one ran on with the window off screen too
    n.ticks(8, grow=400)                            # 270 MB/s, as the real ones ran
    assert n.replaced and n.replaced[0].startswith("footprint running away") and n.mb < 3000


def test_a_big_model_loading_is_not_a_runaway():
    n = _FakeNative().watch()
    n.ticks(2, grow=2000)                           # a large jump that stops
    n.ticks(6)
    n.ticks(3, grow=300)                            # and steps too short to be one
    n.ticks(1)
    n.ticks(3, grow=300)
    assert not n.replaced
    m = _FakeNative().watch()
    m.ticks(10, grow=150)                           # a steady climb below the rate of the real ones
    assert not m.replaced
    k = _FakeNative().watch()
    k.ticks(4, grow=250)                            # at the rate but well under the floor
    assert not k.replaced


def test_a_new_page_gets_time_to_load_before_it_is_looked_at():
    n = _FakeNative().watch()
    n.settle()
    n.page = "hidden"
    n.ticks(4)
    n.page = "hidden"
    looks = n.looks
    n.ticks(19)                                     # 28.5 s after the swap
    assert n.looks == looks and len(n.replaced) == 1
    n.ticks(1 + 4)
    assert len(n.replaced) == 2


def test_a_third_recovery_in_ten_minutes_gets_the_plain_page_and_the_watch_ends():
    n = _FakeNative().watch()
    n.settle()
    for count in (1, 2):
        n.page = "hidden"
        n.ticks(24)
        assert len(n.replaced) == count
    n.page = "hidden"
    n.ticks(24)
    assert len(n.replaced) == 2 and n.gave_up == ["hidden in a visible window"]
    looks = n.looks
    n.next, tick = None, n.next
    tick()
    assert n.next is None and n.looks == looks


def test_recoveries_hours_apart_each_get_a_new_page():
    n = _FakeNative().watch()
    n.settle()
    for count in (1, 2, 3, 4):
        n.page = "hidden"
        n.ticks(24)
        assert len(n.replaced) == count
        n.now += 3600
    assert not n.gave_up


def test_page_watch_stops_with_the_window():
    alive = [True]
    n = _FakeNative().watch(lambda: alive[0])
    alive[0] = False
    n.next, tick = None, n.next
    tick()
    assert n.next is None


def test_find_claude_cli_env_override_and_candidates(tmp_path, monkeypatch):
    fake = tmp_path / "claude"; fake.write_text("#!/bin/sh\n"); fake.chmod(0o755)
    monkeypatch.setenv("AGENTICCAD_CLAUDE", str(fake))
    assert agent.find_claude_cli() == str(fake)
    monkeypatch.setenv("AGENTICCAD_CLAUDE", str(tmp_path / "missing"))
    monkeypatch.setenv("PATH", str(tmp_path / "emptybin"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert agent.find_claude_cli() is None
    (tmp_path / ".local" / "bin").mkdir(parents=True)
    loc = tmp_path / ".local" / "bin" / "claude"; loc.write_text("#!/bin/sh\n"); loc.chmod(0o755)
    assert agent.find_claude_cli() == str(loc)
    cands = agent.claude_cli_candidates()
    assert str(loc) in cands and all(isinstance(c, str) for c in cands)


def test_windows_candidates_and_cmd_shim_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cands = agent.claude_cli_candidates()
    assert any(c.endswith("claude.exe") for c in cands) and any("Roaming" in c for c in cands)
    monkeypatch.delenv("AGENTICCAD_CLAUDE", raising=False)
    monkeypatch.setattr(agent.shutil, "which", lambda name: str(tmp_path / "claude.cmd"))
    assert agent.find_claude_cli() is None          # npm's .cmd shim cannot be spawned by the SDK


def test_auth_status_and_error_detection(tmp_path, monkeypatch):
    # an API key (settings or env) counts as signed in without calling the CLI
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert agent.claude_auth_status(None)["auth_method"] == "api_key"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert agent.claude_auth_status(None, api_key="sk-x")["logged_in"] is True
    # no CLI at all
    monkeypatch.setenv("PATH", str(tmp_path)); monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("AGENTICCAD_CLAUDE", raising=False)
    st = agent.claude_auth_status(None)
    assert st["logged_in"] is False and st["error"]
    # a fake CLI answering like the real one
    fake = tmp_path / "claude"; fake.write_text('#!/bin/sh\necho \'{"loggedIn": false, "authMethod": "none"}\'\n'); fake.chmod(0o755)
    st = agent.claude_auth_status(str(fake))
    assert st == {"logged_in": False, "auth_method": "none", "error": None}
    for txt in ("Failed to authenticate: OAuth session expired and could not be refreshed", "Invalid API key · Please run /login", "Not logged in"):
        assert agent.AUTH_ERROR_RE.search(txt), txt
    assert not agent.AUTH_ERROR_RE.search("Model OK: 1 body")


def test_every_local_module_is_packaged():
    """Briefcase only bundles what pyproject's `sources` lists. A repo-root module imported by the app but missing
    from that list ships a broken installer (0.12.0 shipped without slicer.py)."""
    import ast
    import tomllib
    root = Path(__file__).resolve().parents[1]
    sources = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["briefcase"]["app"]["agenticcad"]["sources"]
    packaged = {Path(s).name for s in sources}
    local = {p.stem for p in root.glob("*.py")}
    local_pkgs = {p.parent.name for p in root.glob("*/__init__.py") if p.parent.name not in ("tests", "src", "build", "dist")}
    seen, todo = set(), ["server", "agent"] + ["agenticcad.app"]
    missing = set()
    while todo:
        mod = todo.pop()
        if mod in seen:
            continue
        seen.add(mod)
        path = root / f"{mod}.py" if mod in local else (root / Path(*mod.split(".")).with_suffix(".py") if mod.split(".")[0] in local_pkgs
                                                         else root / "src" / Path(*mod.split(".")).with_suffix(".py"))
        if not path.exists():
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
            for n in names:
                top = n.split(".")[0]
                if top in local:
                    todo.append(top)
                    if f"{top}.py" not in packaged:
                        missing.add(top)
                elif top in local_pkgs:
                    if top not in packaged:
                        missing.add(top)
                    for sub in (root / top).rglob("*.py"):
                        todo.append(".".join(sub.relative_to(root).with_suffix("").parts))
    assert not missing, f"imported by the app but not in pyproject [tool.briefcase.app.agenticcad].sources: {sorted(missing)}"


def test_installer_licence_files_are_in_sync():
    """Both installers show the licence: Windows merges pyproject license-files into LICENSE.rtf; the macOS .pkg shows
    packaging/macos-installer/LICENSE, which must be exactly INSTALLER-TERMS.txt followed by LICENSE."""
    import tomllib
    root = Path(__file__).resolve().parents[1]
    pp = tomllib.loads((root / "pyproject.toml").read_text())
    assert pp["project"]["license-files"] == ["packaging/INSTALLER-TERMS.txt", "LICENSE"]
    mac = pp["tool"]["briefcase"]["app"]["agenticcad"]["macOS"]
    assert mac["installer_resources"] == "packaging/macos-installer"
    assert "briefcase package macOS -p pkg" in (root / ".github" / "workflows" / "package.yml").read_text()
    terms = (root / "packaging" / "INSTALLER-TERMS.txt").read_text()
    assert (root / "packaging" / "macos-installer" / "LICENSE").read_text() == terms + (root / "LICENSE").read_text()
    from version import __version__  # noqa: F401  (terms mention no version, so they don't need bumping)
    assert "A$99" in terms and "terms.html" in terms and "PolyForm Noncommercial" in terms


@pytest.mark.skipif(sys.platform != "darwin", reason="Cocoa only")
def test_clicks_and_keys_reach_the_page_without_python():
    """The server's OCCT builds hold the GIL for seconds; a click whose path runs Python waits for them (a tab click
    waited up to 6 s during a build). After _native_input the view and window pywebview creates handle no mouse or
    key event in Python; the methods kept from pywebview still run (they call super(BrowserView.WebKitHost, ...))."""
    objc = pytest.importorskip("objc")
    import AppKit
    import WebKit
    import webview
    src = (Path(webview.__file__).parent / "platforms" / "cocoa.py").read_text()   # (importing it makes an NSApplication)
    for name in ("from objc import nil, super", "performDragOperation_", "addSubview_", "willOpenMenu_withEvent_", "mouseDown_", "keyDown_",
                 "def _add_edit_menu", "WebKitHost.alloc()", "WindowHost.alloc()"):
        assert name in src, f"pywebview changed: {name} (pinned in pyproject; see app._native_input)"

    calls = []

    class FakeWebKitHost(WebKit.WKWebView):           # stand-ins for pywebview's, with the same Python overrides
        def mouseDown_(self, event):
            objc.super(BrowserView.WebKitHost, self).mouseDown_(event)

        def mouseDragged_(self, event):
            objc.super(BrowserView.WebKitHost, self).mouseDragged_(event)

        def keyDown_(self, event):
            objc.super(BrowserView.WebKitHost, self).keyDown_(event)

        def performDragOperation_(self, sender):
            calls.append("drop")
            return objc.super(BrowserView.WebKitHost, self).performDragOperation_(sender)

        def addSubview_(self, view):
            calls.append("subview")
            objc.super(BrowserView.WebKitHost, self).addSubview_(view)

        def willOpenMenu_withEvent_(self, menu, event):
            calls.append("menu")

    class FakeWindowHost(AppKit.NSWindow):
        def canBecomeKeyWindow(self):
            return self.focus

    class BrowserView:
        WebKitHost, WindowHost = FakeWebKitHost, FakeWindowHost

        def _add_edit_menu(self, main_menu):
            calls.append("edit menu")

    launcher._native_input(BrowserView)
    view, window = BrowserView.WebKitHost, BrowserView.WindowHost

    def in_python(cls, name):
        return any(isinstance(k.__dict__.get(name), objc.python_selector) for k in cls.__mro__ if hasattr(k, "__dict__"))

    assert not [n for n in launcher.INPUT_SELECTORS if in_python(view, n)]
    assert not in_python(window, "canBecomeKeyWindow")
    assert all(in_python(view, n) for n in ("performDragOperation_", "addSubview_", "willOpenMenu_withEvent_"))
    v = view.alloc().initWithFrame_configuration_(AppKit.NSMakeRect(0, 0, 10, 10), WebKit.WKWebViewConfiguration.alloc().init())
    n, sub = len(calls), AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 2, 2))
    v.addSubview_(sub)                                # pywebview's body runs, its super() reaches WKWebView
    assert calls[n:] == ["subview"] and sub.superview() == v
    launcher._native_input(BrowserView)               # once only
    assert BrowserView.WebKitHost is view
