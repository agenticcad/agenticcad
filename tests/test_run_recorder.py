"""Run recorder (2026-11-02): every program sent to the machine is recorded with its design - the uploaded text, the CAM
script, the machine's status as the app saw it and the run's events - for planned-vs-actual analysis later."""
import asyncio
import gzip
import json

import machine_link as ml
import run_recorder
from test_machine_link import PROG1, _measured, _probed, _setup, _until, run


def test_a_program_run_is_recorded_with_its_design(tmp_path):
    async def go():
        emu, mc, _ = await _setup(speed=8.0)
        mc.runs_dir = tmp_path / "runs"
        try:
            await mc.arm(True); await mc.home(); await _until(lambda: mc.homed)
            _measured(mc, emu)
            emu.wco = [-150.0, -150.0, -60.0]
            await asyncio.sleep(0.4)
            _probed(mc)                                       # the setup was zeroed this session (a run on an unzeroed one is refused)
            prep = ml.prepare_program(PROG1, ["Face"])
            prep["ops"][0].update(kind="face", key="Face", time=0.2)
            info = {"program": "plate", "setup": "Top", "wcs": "G54", "time_min": 0.2, "cam_code": "program = ...\n",
                    "owner": {"doc": "d1", "name": "boat latch"}}
            await mc.run_program("plate ops", prep, info)
            assert "cam_code" not in mc.job                           # the script goes to the record, not the job payload
            await _until(lambda: mc.job["stage"] in ("done", "failed"), timeout=60)
            assert mc.job["stage"] == "done", mc.job
        finally:
            await mc.disconnect(); await emu.stop()
        return mc.job
    job = run(go())
    runs = run_recorder.list_runs(tmp_path / "runs")
    assert len(runs) == 1 and runs[0]["design"] == "boat latch" and runs[0]["outcome"] == "done"
    d = tmp_path / "runs" / runs[0]["id"]
    assert str(d) == job["record"]
    assert (d / "program.nc").read_text() == prep_text() and (d / "cam.py").read_text() == "program = ...\n"
    rows = [json.loads(x) for x in gzip.open(d / "samples.jsonl.gz", "rt")]
    assert len(rows) > 3 and rows == sorted(rows, key=lambda r: r["t"])
    assert any(r["s"] == "Run" and r["op"] == 0 for r in rows) and all(len(r["sp"]) in (5, 6) for r in rows)
    assert runs[0]["ops"][0]["name"] == "Face" and runs[0]["ops"][0]["actual_min"] > 0
    events = [json.loads(x) for x in (d / "events.jsonl").read_text().splitlines()]
    assert any("finished" in e["text"] for e in events)
    assert run_recorder.list_runs(tmp_path / "runs", "boat latch")[0]["id"] == runs[0]["id"]


def prep_text():
    return ml.prepare_program(PROG1, ["Face"])["text"]


def test_a_broken_recorder_never_stops_the_job(tmp_path):
    class Boom:
        def sample(self, *a, **k):
            raise OSError("disk full")
    mc = ml.MachineControl()
    mc.recorder = Boom()
    mc._rec("sample", None, 0.0, {})
    assert mc.recorder is None and "run recording stopped: OSError: disk full" in mc.events[-1]


def test_an_unsaved_design_is_filed_under_its_program(tmp_path):
    rec = run_recorder.RunRecorder(tmp_path, {"program": "latch sheet Z1", "owner": {"doc": "d1", "name": None}},
                                   {"name": "latch.nc", "ops": []}, "G0 X0\n")
    rec.close("done")
    runs = run_recorder.list_runs(tmp_path, "latch sheet Z1")
    assert len(runs) == 1 and runs[0]["design"] == "latch sheet Z1" and runs[0]["design_saved"] is False
