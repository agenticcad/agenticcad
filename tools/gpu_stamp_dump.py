"""PROTOTYPE support: dump the simulator's stamping workload for the latch job (every flat op: samples, kinds, move ids, tool profile
tables, the grid) to sim_dump.npz for the GPU micro-benchmark."""
import sys, math
from pathlib import Path
import numpy as np
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cad_kernel as ck, cam_kernel as cam, cam_sim
from cam_data import Library
from library import PartLibrary
ws = Path(sys.argv[1]); camfile = Path(sys.argv[2]); out = Path(sys.argv[3])
lib = Library(ws)
model = ck.run_script((ws / "model.py").read_text(), "normal", ws, PartLibrary(ws), "real", "CAM sheet")
ns = {"__name__": "__cam__"}
exec("from cam_kernel import *\nimport math\nfrom math import *\nimport cam_kernel as cam\n", ns)
ns.update({"model": model, "part": model.shape, "bodies": {b.path: b.shape for b in model.bodies}, "tools": lib.tool_map(), "machines": lib.machines()})
cam.reset_overrides(); exec(compile(camfile.read_text(), "cam.py", "exec"), ns)
prog = ns["program"]
boxes = [s.model_stock for s in prog.setups]
x0 = min(b.xmin for b in boxes); x1 = max(b.xmax for b in boxes); y0 = min(b.ymin for b in boxes); y1 = max(b.ymax for b in boxes)
res = max(0.15, math.sqrt((x1 - x0) * (y1 - y0) / 160_000.0))
nx = int(math.ceil((x1 - x0) / res)) + 1; ny = int(math.ceil((y1 - y0) / res)) + 1
d = {"res": res, "x0": x0, "y0": y0, "nx": nx, "ny": ny, "zmax": boxes[0].zmax, "zmin": boxes[0].zmin}
names = []
for i, op in enumerate(prog.ops):
    st = prog.setup_of(op)
    if op.kind == "probe" or not op.moves:
        continue
    ax = st.tool_axis_model
    side = 1 if ax[2] > 0.999 else -1
    tm, src = cam.tile_moves(op, prog.op_tiles(op), st.clearance_z)
    P = st.to_model(np.array([m[1:4] for m in tm], dtype=float))
    mv = [[m[0], *p, m[4]] for m, p in zip(tm, P)]
    pts, idx, kinds = cam_sim._samples([m[:4] for m in mv], res / 2, False)
    t = op.tool.shape._t
    d[f"op{i}_P"] = pts; d[f"op{i}_K"] = kinds; d[f"op{i}_I"] = idx; d[f"op{i}_side"] = side
    d[f"op{i}_zc"] = t["zc"]; d[f"op{i}_cumc"] = t["cumc"]; d[f"op{i}_R"] = op.tool.shape.cut_radius
    names.append((i, op.name, len(pts)))
np.savez_compressed(out, names=np.array([f"{i}|{n}|{k}" for i, n, k in names]), **d)
print("dumped", names, "grid", nx, ny, res)
