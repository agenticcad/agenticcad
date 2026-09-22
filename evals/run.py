"""Run the agent evals.  .venv/bin/python evals/run.py [--filter cad] [--model claude-sonnet-5] [--repeat 2] [--parallel 3]"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import run_case  # noqa: E402
from cases import CASES  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--filter", default="", help="substring of case id or tag")
    ap.add_argument("--model", default=None)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--out", default=str(Path(__file__).parent / "results"))
    ap.add_argument("--allow-fail", action="store_true", help="exit 0 even when cases fail")
    args = ap.parse_args()

    cases = [c for c in CASES if not args.filter or args.filter in c.id or args.filter in c.tags]
    if not cases:
        print("no cases match", args.filter); return 2
    jobs = [c for c in cases for _ in range(args.repeat)]
    sem = asyncio.Semaphore(args.parallel)
    t0 = time.time()

    async def one(case):
        async with sem:
            r = await run_case(case, args.model)
            status = "PASS" if r.passed else "FAIL"
            print(f"[{status}] {case.id:26s} ${r.cost:.3f} {r.turns:2d} steps {r.duration_s:5.1f}s  "
                  f"{sum(ok for _, ok, _ in r.checks)}/{len(r.checks)} checks" + (f"  ERROR {r.error}" if r.error else ""), flush=True)
            for name, ok, detail in r.checks:
                if not ok:
                    print(f"         ✗ {name}: {detail[:160]}", flush=True)
            return r

    runs = await asyncio.gather(*(one(c) for c in jobs))
    total_cost = sum(r.cost for r in runs)
    passed = sum(1 for r in runs if r.passed)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    data = {"stamp": stamp, "model": args.model or "default", "elapsed_s": round(time.time() - t0, 1),
            "passed": passed, "total": len(runs), "cost_usd": round(total_cost, 3),
            "runs": [{"case": r.case.id, "tags": r.case.tags, "passed": r.passed, "cost": r.cost, "turns": r.turns,
                      "duration_s": round(r.duration_s, 1), "tool_errors": r.tool_errors, "tools_used": r.tools_used,
                      "error": r.error, "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in r.checks],
                      "answer": r.text.strip()[:2000]} for r in runs]}
    (out / f"{stamp}.json").write_text(json.dumps(data, indent=1))
    md = [f"# Eval run {stamp} — {passed}/{len(runs)} passed, ${total_cost:.2f}, model {data['model']}", "",
          "| case | result | checks | cost | steps | time |", "|---|---|---|---|---|---|"]
    for r in runs:
        md.append(f"| {r.case.id} | {'✅' if r.passed else '❌'} | {sum(ok for _, ok, _ in r.checks)}/{len(r.checks)} | ${r.cost:.3f} | {r.turns} | {r.duration_s:.0f}s |")
    fails = [(r, n, d) for r in runs for n, ok, d in r.checks if not ok]
    if fails:
        md += ["", "## Failed checks"] + [f"- **{r.case.id}** — {n}: {d[:200]}" for r, n, d in fails]
    (out / f"{stamp}.md").write_text("\n".join(md) + "\n")
    print(f"\n{passed}/{len(runs)} passed · ${total_cost:.2f} · {time.time() - t0:.0f}s · report {out / (stamp + '.md')}")
    return 0 if (passed == len(runs) or args.allow_fail) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
