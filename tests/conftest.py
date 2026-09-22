"""Shared fixtures. Every test gets an isolated workspace; the real workspace/ is never touched."""
import os
import sys
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cad_kernel as ck  # noqa: E402
import cam_kernel as cam  # noqa: E402
from cam_data import Library  # noqa: E402
from library import PartLibrary  # noqa: E402


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    for sub in ("exports", "history", "designs", "imports", "images", "drawings"):
        (ws / sub).mkdir(parents=True)
    monkeypatch.setattr(ck, "WORKSPACE", ws)
    monkeypatch.setattr(ck, "LIBRARY", PartLibrary(ws))
    return ws


@pytest.fixture(scope="session")
def demo_model():
    """The default two-body demo (Bracket + Pin), built once per session."""
    return ck.run_script(ck.DEFAULT_CODE)


@pytest.fixture
def bracket(demo_model):
    return demo_model.body_by_name("Bracket").shape


@pytest.fixture
def cam_setup(workspace, demo_model, bracket):
    lib = Library(workspace)
    tools = lib.tool_map()
    machines = lib.machines()
    stock = cam.Stock.from_model(bracket, margin=3, top=1.0)
    setup = cam.Setup(machines["Generic 3018"], stock, origin="stock-top-left")
    return setup, tools, machines


# --------------------------------------------------------------------------- geometry helpers
def bbox_size(model_or_shape):
    if hasattr(model_or_shape, "bbox_min"):
        lo, hi = model_or_shape.bbox_min, model_or_shape.bbox_max
        return tuple(b - a for a, b in zip(lo, hi))
    bb = model_or_shape.bounding_box()
    return (bb.size.X, bb.size.Y, bb.size.Z)


def close(a, b, tol=1e-3):
    return abs(a - b) <= tol
