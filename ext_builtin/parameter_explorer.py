"""Parameter Explorer: every numeric parameter in the script as a slider with live rebuild.

Built-in example extension. Top-level `name = number` lines are the design's parameters (the same ones the
Parameters card edits); here each one gets a slider spanning a sensible range around its current value.
"""


def _range(v: float) -> tuple[float, float, float]:
    """A slider range around v: 0..2v for positive values (10 → 0..20), symmetric otherwise; step from magnitude."""
    span = max(abs(v), 1.0)
    lo, hi = (0.0, 2 * span) if v > 0 else (-2 * span, 0.0) if v < 0 else (-10.0, 10.0)
    step = 10 ** (round(__import__("math").log10(span)) - 2)
    return lo, hi, max(step, 0.001)


@panel("Parameters", icon="🎚", description="Every numeric parameter as a slider with live rebuild")
def parameter_explorer(ctx):
    ps = ctx.params()
    if not ps:
        return ui.panel(ui.text("No numeric parameters. Top-level `name = number` lines in the script become sliders here "
                                "(ask the agent to parameterise the design).", muted=True))
    s = ctx.state
    kids = []
    for p in ps:
        v = float(p["value"])
        key = f"_r:{p['name']}"
        if key not in s:                                 # fix the range the first time, so it doesn't drift while dragging
            s[key] = list(_range(v))
        lo, hi, step = s[key]
        label = p["name"] + (f"  · {p['comment']}" if p.get("comment") else "")
        kids.append(ui.slider(p["name"], label, v, lo, hi, step=step, unit=p.get("unit") or ctx.units, call="apply_parameter"))
    kids.append(ui.row(ui.button("Widen ranges", call="widen_ranges"), ui.text("drag a slider: the model rebuilds as you go", muted=True)))
    return ui.panel(*kids)


@tool("apply_parameter", "Set the parameter that just changed", agent=False)
def apply_parameter(ctx, **state):
    if not ctx.event or ctx.event.startswith("_"):
        return None
    return {"set_params": {ctx.event: float(state.get(ctx.event, 0))}}


@tool("widen_ranges", "Double every slider's range", agent=False)
def widen_ranges(ctx, **state):
    for k, v in list(state.items()):
        if k.startswith("_r:") and isinstance(v, list) and len(v) == 3:
            lo, hi, step = v
            mid, half = (lo + hi) / 2, (hi - lo)
            state[k] = [mid - half, mid + half, step * 2]
    return {"state": state}
