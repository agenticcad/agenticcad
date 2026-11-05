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
    """Stands in for the NSWindow and the WKWebView of the frozen-page watch; ticks run when the test says so."""
    def __init__(self):
        self.on_screen, self.page, self.woken, self.looks, self.next = True, "visible", 0, 0, None

    def isVisible(self):
        return self.on_screen

    def occlusionState(self):
        return 8194 if self.on_screen else 8192

    def evaluateJavaScript_completionHandler_(self, _js, done):
        self.looks += 1
        done(self.page, None)

    def _prepareForMoveToWindow_completionHandler_(self, _window, _done):
        self.woken += 1
        self.page = "visible"

    def later(self, _seconds, fn):
        self.next = fn

    def ticks(self, n):
        for _ in range(n):
            self.next()


def test_frozen_page_in_a_visible_window_is_woken_after_two_looks():
    n = _FakeNative()
    launcher._watch_for_frozen_page(n, n, lambda: True, n.later)
    n.ticks(3)
    assert n.woken == 0                             # a healthy page is left alone
    n.page = "hidden"
    n.ticks(1)
    assert n.woken == 0                             # one look may be an update still on its way
    n.ticks(1)
    assert n.woken == 1 and n.page == "visible"
    n.ticks(3)
    assert n.woken == 1


def test_a_covered_window_may_sleep_and_is_not_woken_or_touched():
    n = _FakeNative()
    launcher._watch_for_frozen_page(n, n, lambda: True, n.later)
    n.page = "hidden"
    n.ticks(1)                                      # one hidden look, then the window is covered
    n.on_screen = False
    looks = n.looks
    n.ticks(5)
    assert n.woken == 0 and n.looks == looks        # asking would wake a suspended page
    n.on_screen = True
    n.ticks(1)
    assert n.woken == 0                             # the count started again


def test_frozen_page_watch_stops_with_the_window():
    n = _FakeNative()
    alive = [True]
    launcher._watch_for_frozen_page(n, n, lambda: alive[0], n.later)
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
