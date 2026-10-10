"""AgenticCAD desktop launcher.

- keeps the user's data in the OS user-data folder (Application Support / AppData / ~/.local/share)
- finds Claude Code, which is NOT bundled and must be installed separately; opens the setup page if missing
- starts the FastAPI server on a free localhost port in a background thread
- shows the UI in a native window (pywebview: WKWebView on macOS, WebView2 on Windows)
Works from a checkout too: `.venv/bin/python -m agenticcad` (with src/ on sys.path).
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path


def _repo_root() -> Path:
    """Directory holding server.py: the app dir in a Briefcase bundle, or the checkout in development."""
    here = Path(__file__).resolve()
    for cand in (here.parent.parent, here.parent.parent.parent):      # app/ (bundle)  |  <checkout>/src/../
        if (cand / "server.py").exists():
            return cand
    raise SystemExit("AgenticCAD launcher: cannot find server.py next to the agenticcad package")


def _free_port(preferred: int = 8765) -> int:
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise SystemExit("no free localhost port")


SPLASH_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:#14171c;color:#e6e9ee;font:13px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif}
.box{position:absolute;inset:0;display:flex;align-items:center;justify-content:center}.in{width:420px;max-width:90vw}
.logo{font-size:30px;font-weight:800;letter-spacing:-.02em;color:#fff}.logo span{color:#ff8c42}.sub{color:#8b94a1;margin:4px 0 18px}
.bar{height:4px;background:#232830;border-radius:2px;overflow:hidden;margin-bottom:14px}.bar div{height:100%;width:35%;background:linear-gradient(90deg,#4ea1ff,#ff8c42);border-radius:2px;animation:s 1.4s ease-in-out infinite}
@keyframes s{0%{margin-left:-35%}100%{margin-left:100%}}ul{list-style:none;margin:0;padding:0}li{display:flex;gap:10px;padding:4px 0;color:#8b94a1}
li.active{color:#e6e9ee}li .ic{width:16px;text-align:center;color:#4cd37a}li.active .ic{color:#4ea1ff}.hint{margin-top:14px;font-size:12px;color:#8b94a1;min-height:16px}
</style></head><body><div class="box"><div class="in"><div class="logo">Agentic<span>CAD</span></div><div class="sub" id="sub">starting…</div>
<div class="bar"><div></div></div><ul id="steps"></ul><div class="hint" id="hint"></div></div></div>
<script>
var steps=[];function step(t){for(var i=0;i<steps.length;i++)steps[i].done=true;steps.push({text:t,done:false});render()}
function render(){document.getElementById('steps').innerHTML=steps.map(function(s,i){return '<li class="'+(s.done?'done':'active')+'"><span class="ic">'+(s.done?'✓':'●')+'</span><span>'+s.text+'</span></li>'}).join('')}
function hint(t){document.getElementById('hint').textContent=t}
</script></body></html>"""


def main() -> None:
    root = _repo_root()
    sys.path.insert(0, str(root))
    os.chdir(root)

    import platformdirs
    data = Path(os.environ.get("AGENTICCAD_WORKSPACE") or platformdirs.user_data_dir("AgenticCAD", "AgenticCAD"))
    data.mkdir(parents=True, exist_ok=True)
    os.environ["AGENTICCAD_WORKSPACE"] = str(data)
    os.environ.setdefault("AGENTICCAD_PACKAGED", "1")
    os.environ.setdefault("MPLBACKEND", "Agg")
    port = _free_port(int(os.environ.get("PORT") or 8765))
    os.environ["PORT"] = str(port)
    base = f"http://127.0.0.1:{port}"
    state: dict = {"srv": None, "url": None}

    def boot(window=None) -> None:
        """Runs in a background thread while the splash is on screen: heavy imports, the server, then the real page."""
        say = (lambda t: window.evaluate_js(f"step({t!r})")) if window else (lambda t: print(t))
        say("Loading the CAD kernel (OCCT, build123d)")
        import agent as agent_mod                   # noqa: E402  (after sys.path)
        cli = agent_mod.find_claude_cli()
        say("Starting the local server")
        import uvicorn
        import server                               # noqa: E402  (reads AGENTICCAD_WORKSPACE at import)
        config = uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning")
        srv = uvicorn.Server(config)
        state["srv"] = srv
        threading.Thread(target=srv.run, name="agenticcad-server", daemon=True).start()
        for _ in range(600):                        # wait for the server (≤30 s); the first design builds in the background behind the page's own splash
            try:
                urllib.request.urlopen(base + "/api/version", timeout=0.5).read()
                break
            except Exception:
                time.sleep(0.05)
        state["url"] = base + ("/" if cli else "/setup")
        if window is not None:
            say("Opening the workspace")
            window.load_url(state["url"])
            smoke = os.environ.get("AGENTICCAD_SMOKE")
            if smoke:                               # test runs: close the window after N seconds
                time.sleep(float(smoke)); window.destroy()

    try:
        import webview
    except Exception:                               # no native web view available: fall back to the browser
        import webbrowser
        boot(None)
        webbrowser.open(state["url"])
        print(f"AgenticCAD running at {state['url']} (workspace {data}); Ctrl-C to quit")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    else:
        import version as _v
        window = webview.create_window(f"AgenticCAD {_v.__version__}", html=SPLASH_HTML, width=1500, height=950, min_size=(1000, 650),
                                       background_color="#14171c", text_select=True)
        webview.start(boot, window, debug=bool(os.environ.get("AGENTICCAD_DEBUG")))   # blocks until the window closes
        del window
    if state["srv"] is not None:
        state["srv"].should_exit = True
    time.sleep(0.5)
    os._exit(0)                                    # the SDK's CLI subprocess and the server thread go with us


if __name__ == "__main__":
    main()
