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
