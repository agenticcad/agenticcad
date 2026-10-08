"""Hole Report: every round hole in the design with diameter, depth, thread and body; click a row to find it.

Built-in example extension: a table panel fed by ctx.holes(), with selection and an "ask the agent" button.
"""


def _rows(holes, units):
    rows = []
    for h in holes:
        d = h["d"] / 25.4 if units == "in" else h["d"]
        depth = h["depth"] / 25.4 if units == "in" else h["depth"]
        rows.append({"cells": [h["body"].split("/")[-1], f"Ø{d:.3g}", "thru" if h["through"] else f"{depth:.3g}",
                               h["thread"] or "", f"({', '.join(f'{v:g}' for v in h['center'])})"],
                     "face": h["face"], "body": h["body"], "data": h})
    return rows


@panel("Hole report", icon="◎", description="Every round hole: Ø, depth, thread, position — click a row to select it")
def hole_report(ctx):
    bodies = [b.path for b in ctx.bodies]
    which = ctx.state.get("body") or ""
    holes = ctx.holes(which or None)
    if ctx.state.get("threaded_only"):
        holes = [h for h in holes if h["thread"]]
    sizes = {}
    for h in holes:
        sizes[h["d"]] = sizes.get(h["d"], 0) + 1
    summary = ", ".join(f"{n}× Ø{d:g}" for d, n in sorted(sizes.items())) or "none"
    kids = [ui.row(ui.select("body", "Body", which, [["", "all bodies"]] + bodies, call=None),
                   ui.checkbox("threaded_only", "threaded only", bool(ctx.state.get("threaded_only")))),
            ui.text(f"{len(holes)} hole{'s' if len(holes) != 1 else ''}: {summary}", muted=True)]
    if holes:
        kids.append(ui.table(["Body", "Ø", "Depth", "Thread", "Centre"], _rows(holes, ctx.units)))
        kids.append(ui.row(
            ui.button("Select all", action={"select": {"faces": [h["face"] for h in holes]}}),
            ui.button("Light up", action={"highlight": {"faces": [h["face"] for h in holes]}}),
            ui.button("Ask the agent for matching screws", chat=f"Add kit screws that fit the {len(holes)} holes in "
                      f"{which or 'the design'} (sizes: {summary}); use real threads only where a hole is tapped.")))
    else:
        kids.append(ui.text("No round holes found." + (" Untick 'threaded only'." if ctx.state.get("threaded_only") else ""), muted=True))
    return ui.panel(*kids)


@tool("hole_report", "List every round hole (diameter, depth, through, thread, centre, axis, face id), optionally for one body")
def hole_report_tool(ctx, body: str = None):
    holes = ctx.holes(body or None)
    if not holes:
        return "no round holes"
    return "\n".join(f"face#{h['face']} {h['body']}: Ø{h['d']:g} {'thru' if h['through'] else f'depth {h['depth']:g}'}"
                     f"{' ' + h['thread'] if h['thread'] else ''} at ({', '.join(f'{v:g}' for v in h['center'])}) axis "
                     f"({', '.join(f'{v:g}' for v in h['axis'])})" for h in holes)
