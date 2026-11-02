"""Share by email (2026-11-02: "a share button ... bundles up the design and cam and sends it to the users default mail
client") and the Analytics workspace's planned-vs-actual analysis of recorded runs."""
import base64
import gzip
import io
import json
import os
import sys
import zipfile

import pytest
from fastapi.testclient import TestClient

import run_analysis as ra
import run_recorder
import share

DESIGN = "from build123d import *\nimport math\nplate = Box(40, 30, 6)\nresult = plate\n"


# ---------------------------------------------------------------- share
def test_a_bundle_round_trips_and_says_whats_in_it(tmp_path):
    step = tmp_path / "x.step"; step.write_text("ISO-10303-21;")
    p = share.bundle(tmp_path / "out", "boat latch", DESIGN, "program = None\n", step, "9.9.9", {"summary": "Makera Z1, 4 operations, about 119 min"})
    names = sorted(zipfile.ZipFile(p).namelist())
    assert names == ["boat_latch/README.txt", "boat_latch/boat_latch.cam.py", "boat_latch/boat_latch.py", "boat_latch/boat_latch.step"]
    readme = zipfile.ZipFile(p).read("boat_latch/README.txt").decode()
    assert "AgenticCAD 9.9.9" in readme and "Makera Z1, 4 operations" in readme and "people you trust" in readme
    b = share.read_bundle(p.read_bytes())
    assert b == {"name": "boat_latch", "code": DESIGN, "cam_code": "program = None\n"}
    subject, text = share.message("boat latch", "9.9.9", {"summary": "Makera Z1"}, True)
    assert subject == "boat latch (AgenticCAD design)" and "its CAM program (Makera Z1)" in text and "STEP" in text
    with pytest.raises(ValueError):
        share.read_bundle(b"not a zip")


def test_what_reaches_past_geometry_is_listed():
    assert share.risky(DESIGN) == []
    assert share.risky("import numpy as np\nfrom shapely.ops import unary_union\nfrom OCP.TopoDS import TopoDS\n") == []
    bad = share.risky("import os\nfrom subprocess import run\nx = open('/etc/passwd').read()\ny = ().__class__.__bases__\neval('1')\n")
    assert any("imports os" in b for b in bad) and any("imports subprocess" in b for b in bad)
    assert any("calls open()" in b for b in bad) and any("__bases__" in b for b in bad) and any("calls eval()" in b for b in bad)


def test_share_goes_by_the_default_mail_app_and_falls_back_to_mailto(tmp_path, monkeypatch):
    """No universal way exists: mailto: can't carry an attachment, so each mail app gets its own way and mailto: (with the
    file shown to drag in) is the fallback for the rest and for any failure. Nothing is opened here: the steps are stubs."""
    z = tmp_path / "d.zip"; z.write_bytes(b"PK")
    calls = []
    monkeypatch.setattr(share.sys, "platform", "darwin")
    monkeypatch.setattr(share, "_reveal", lambda p: calls.append(("reveal", p.name)))
    monkeypatch.setattr(share, "_open_url", lambda u: calls.append(("url", u)))
    monkeypatch.setattr(share, "_mac_share", lambda *a: {"how": "share"})
    monkeypatch.setattr(share, "_outlook_mac", lambda *a: {"how": "outlook"})
    monkeypatch.setattr(share, "_thunderbird_mac", lambda *a: None)          # e.g. already running
    for bid, name, how in [("com.apple.mail", "Mail", "share"), ("com.microsoft.Outlook", "Microsoft Outlook", "outlook"),
                           ("org.mozilla.thunderbird", "Thunderbird", "manual"), ("com.readdle.SparkDesktop", "Spark", "manual")]:
        calls.clear()
        monkeypatch.setattr(share, "default_mail_app", lambda b=bid, n=name: {"id": b, "name": n, "path": "/Applications/x.app"})
        r = share.compose(z, "d (AgenticCAD design)", "Here's d.")
        assert (r["how"], r["app"]) == (how, name)
        if how == "manual":
            assert calls[0] == ("reveal", "d.zip") and calls[1][1].startswith("mailto:?subject=d%20%28AgenticCAD%20design%29&body=Here%27s%20d.")
            assert "Attach%20d.zip" in calls[1][1]
        else:
            assert calls == []

    def boom(*a):                                       # new Outlook refusing the script
        raise RuntimeError("Outlook didn't take the message")
    monkeypatch.setattr(share, "_outlook_mac", boom)
    monkeypatch.setattr(share, "default_mail_app", lambda: {"id": "com.microsoft.Outlook", "name": "Microsoft Outlook", "path": "/x"})
    calls.clear()
    r = share.compose(z, "s", "t")
    assert r["how"] == "manual" and "didn't take" in r["note"] and calls[1][1].startswith("mailto:")


def test_thunderbird_compose_argument_survives_quotes_and_spaces(tmp_path):
    a = share.thunderbird_args(tmp_path / "my part.zip", "Bob's part", "a, b")
    assert a.startswith("subject='Bob\u2019s part',body='a, b',attachment='file://") and a.endswith("my%20part.zip'")


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    ws = tmp_path_factory.mktemp("ws")
    os.environ["AGENTICCAD_WORKSPACE"] = str(ws)
    os.environ["AGENTICCAD_NO_AGENT"] = "1"
    os.environ["AGENTICCAD_STARTUP_SYNC"] = "1"
    os.environ["AGENTICCAD_NO_MAIL"] = "1"
    (ws / "designs").mkdir(exist_ok=True); (ws / "designs" / "plate.py").write_text(DESIGN)
    (ws / "model.py").write_text(DESIGN); (ws / "state.json").write_text('{"design": "plate"}')
    for m in ("server", "agent"):
        sys.modules.pop(m, None)
    import server
    with TestClient(server.app) as c:
        c.ws = ws
        yield c
    os.environ.pop("AGENTICCAD_NO_MAIL", None)


def test_share_packs_what_is_on_screen_and_import_brings_it_back(client):
    r = client.post("/api/design/share", json={}).json()
    assert r["how"] == "none" and r["file"].endswith("plate.zip")
    z = zipfile.ZipFile(r["file"])
    assert {"plate/plate.py", "plate/plate.step", "plate/README.txt"} <= set(z.namelist())
    assert z.read("plate/plate.py").decode() == DESIGN
    data = base64.b64encode(open(r["file"], "rb").read()).decode()
    j = client.post("/api/design/import", json={"name": "plate.zip", "data": "data:application/zip;base64," + data}).json()
    assert j["name"] == "plate" and not j.get("confirm")


def test_importing_a_script_that_reaches_further_asks_first(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x/x.py", "import os\n" + DESIGN)
    data = base64.b64encode(buf.getvalue()).decode()
    j = client.post("/api/design/import", json={"name": "x.zip", "data": data}).json()
    assert j["confirm"] == ["design line 1: imports os"]
    assert not (client.ws / "designs" / "x.py").exists()                 # nothing ran or was saved
    j = client.post("/api/design/import", json={"name": "x.zip", "data": data, "confirm": True}).json()
    assert j["name"] == "x" and (client.ws / "designs" / "x.py").exists()
    j = client.post("/api/design/import", json={"name": "y.py", "code": "import socket\n" + DESIGN}).json()
    assert j["confirm"] == ["design line 1: imports socket"]               # a plain .py the same way


# ---------------------------------------------------------------- analytics
PROG = "\n".join(["G21 G90 G17", "G54", "M6 T2", "G53 G0 Z-3", "G0 X0 Y0", "M3 S10000", "G0 Z1",
                  "G1 Z-1 F100", "G1 X40 F600", "G1 Y20", "G2 X40 Y30 I0 J5", "G1 X0", "G1 Y0", "G0 Z5", "M5"]) + "\n"
OFF = (-100.0, -50.0, -60.0)                             # machine = work + OFF


def _fake_run(root, with_wpos: bool, slow_corner: bool = False, rpm=None):
    """A run of PROG sampled at 4 Hz as the Z1 reports it: the line it is reading runs ahead of the motion."""
    moves = ra.parse_program(PROG, [{"name": "Rect", "start": 3, "end": 15, "time": 0.4}])
    pts = []                                             # (work xyz, feed mm/min, line)
    for m in moves:
        if m["machine"] or m["a"] is None:
            continue
        seg = ra._arc_points(m["a"], m["b"], m["c"], m["g"] == 2) if m["g"] in (2, 3) else [m["a"], m["b"]]
        cum = [0.0]
        for a, b in zip(seg, seg[1:]):
            cum.append(cum[-1] + sum((p - q) ** 2 for p, q in zip(a, b)) ** 0.5)
        v = (m["f"] or 3000.0) * (0.4 if slow_corner and m["g"] == 2 else 1.0)
        n = max(1, round(cum[-1] / (v / 60 * 0.25)))
        for i in range(n):                               # evenly along the whole move, one sample per 0.25 s
            d, j = cum[-1] * i / n, 0
            while j < len(cum) - 2 and cum[j + 1] < d:
                j += 1
            f = (d - cum[j]) / max(cum[j + 1] - cum[j], 1e-9)
            pts.append(([seg[j][k] + (seg[j + 1][k] - seg[j][k]) * f for k in range(3)], m["ln"]))
    info = {"program": "rect", "setup": "Top", "time_min": 0.4, "owner": {"doc": "d1", "name": "rect"}}
    job = {"name": "rect.nc", "lines": 15, "ops": [{"name": "Rect", "kind": "pocket", "key": "Rect", "start": 3, "end": 15, "tool": 2, "time": 0.4}]}
    rec = run_recorder.RunRecorder(root, info, job, PROG, "program = ...\n")
    with open(rec.dir / "samples.jsonl", "w") as f:
        for i, (w, ln) in enumerate(pts):
            row = {"t": round(i * 0.25, 2), "s": "Run", "ln": min(ln + 3, 15), "op": 0, "m": [w[k] + OFF[k] for k in range(3)],
                   "f": [600.0, 600.0, 100.0], "sp": [rpm(i) if rpm else 9990.0, 10000.0, 100.0, 40.0 + i * 0.01, 30.0], "tl": 2}
            if with_wpos:
                row["w"] = w
            f.write(json.dumps(row) + "\n")
    rec._samples = open(rec.dir / "samples.jsonl", "a"); rec._events = open(rec.dir / "events.jsonl", "a")
    rec.close("done")
    return rec.dir


@pytest.mark.parametrize("with_wpos", [True, False])
def test_the_analysis_places_the_run_on_its_program(tmp_path, with_wpos):
    d = _fake_run(tmp_path, with_wpos)
    a = ra.analyse(d, use_cache=False)
    s = a["summary"]
    assert s["match_rate"] > 0.95 and s["match_median_mm"] < 0.05
    if not with_wpos:                                    # the work offset recovered from the data alone
        assert s["offset"]["fitted"] and s["offset"]["xy"] == pytest.approx(list(OFF[:2]), abs=0.05)
        assert s["offset"]["z"]["2"] == pytest.approx(OFF[2], abs=0.05)
    o = a["ops"][0]
    assert o["feed_reached"] == pytest.approx(1.0, abs=0.08) and o["rpm_min"] == pytest.approx(0.999, abs=0.001)
    assert a["timeline"] and a["path"] and a["planned"]
    assert (d / "analysis.json").exists() and ra.analyse(d)["summary"] == json.loads((d / "analysis.json").read_text())["summary"]


def test_a_slow_arc_shows_as_lost_time(tmp_path):
    a = ra.analyse(_fake_run(tmp_path, True, slow_corner=True), use_cache=False)
    arc = next(g for g in a["move_groups"] if g["kind"].startswith("arc"))
    assert arc["spent_s"] > 2 * arc["planned_s"]
    assert a["ops"][0]["feed_reached"] < 0.95


def test_runs_api_lists_and_analyses(client):
    root = client.ws / "runs"
    d = _fake_run(root, True)
    runs = client.get("/api/runs").json()
    rid = f"{d.parent.name}/{d.name}"
    assert any(r["id"] == rid for r in runs["runs"])
    a = client.get("/api/runs/analysis", params={"id": rid}).json()
    assert a["id"] == rid and a["ops"][0]["name"] == "Rect" and a["suggestions"] == []
    assert client.get("/api/runs/analysis", params={"id": "../../etc"}).status_code == 404


def test_a_sudden_load_then_nothing_is_flagged_where_it_happened(tmp_path):
    # the 2026-11-02 tab break: steady cutting, rpm dips to 99.1 %, then runs unloaded above target
    d = _fake_run(tmp_path, True, rpm=lambda i: 9910.0 if i == 60 else 10030.0 if 61 <= i <= 64 else 9990.0)
    a = ra.analyse(d, use_cache=False)
    ev = a["summary"]["load_events"]
    assert len(ev) == 1 and ev[0]["rpm"] == pytest.approx(0.991) and ev[0]["after"] == pytest.approx(1.003)
    f = next(f for f in a["findings"] if f["kind"] == "load")
    assert "then the spindle ran unloaded" in f["title"] and f["lines"] == [ev[0]["line"]]
    assert sum(a["summary"]["time_by_kind_min"].values()) > 0
    # a dip that recovers without running unloaded is just a heavier moment, not an event
    d2 = _fake_run(tmp_path / "b", True, rpm=lambda i: 9910.0 if i == 60 else 9990.0)
    assert ra.analyse(d2, use_cache=False)["summary"]["load_events"] == []


def test_the_user_edits_the_lessons_in_settings(client):
    rows = client.get("/api/lessons").json()["lessons"]
    assert rows and rows[0]["id"] == "L1"
    r = client.post("/api/lessons", json={"action": "add", "title": "Deburr before flipping", "rule": "Deburr the register holes.",
                                          "why": "2026-11-02: BP3 found burrs", "tags": "cam, flip", "check": ""}).json()
    new = r["lessons"][-1]
    assert new["source"] == "user" and new["tags"] == ["cam", "flip"] and new["check"] is None
    r = client.post("/api/lessons", json={"action": "update", "id": new["id"], "rule": "Deburr all four register holes.", "why": "edited"}).json()
    row = next(x for x in r["lessons"] if x["id"] == new["id"])
    assert row["rule"] == "Deburr all four register holes." and row["why"] == "edited"
    assert next(x for x in client.post("/api/lessons", json={"action": "retire", "id": new["id"]}).json()["lessons"] if x["id"] == new["id"])["status"] == "retired"
    assert all(x["id"] != new["id"] for x in client.post("/api/lessons", json={"action": "delete", "id": new["id"]}).json()["lessons"])
    assert client.post("/api/lessons", json={"action": "add", "title": "x"}).status_code == 400


def test_the_consolidation_settings_and_undo(client):
    d = client.get("/api/dreams").json()
    assert d["enabled"] is True and d["running"] is False
    assert client.post("/api/dreams", json={"action": "disable"}).json()["enabled"] is False
    assert client.get("/api/dreams").json()["enabled"] is False
    assert client.post("/api/dreams", json={"action": "enable"}).json()["enabled"] is True
    assert client.post("/api/dreams", json={"action": "undo"}).status_code == 400          # nothing to undo yet


def test_the_power_estimate_says_why_when_it_cant(client):
    d = _fake_run(client.ws / "runs", True)
    (d / "cam.py").unlink()                                                    # a file run: no CAM script to simulate
    r = client.get("/api/runs/power", params={"id": f"{d.parent.name}/{d.name}"}).json()
    assert "no CAM script" in r["error"]
    assert client.get("/api/runs/power", params={"id": "../x"}).status_code == 404
