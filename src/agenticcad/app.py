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


def _import_tabs_test_build(tabs_dir: Path, data: Path) -> list[str]:
    """Once, on the first start after the tabs test build (AgenticCAD Tabs, 1.4.0a1-a2) became the main app: copy the
    saved designs (and the STEP / image files they import) that exist only in the test build's folder into this one.
    Nothing is overwritten and the test build's folder is only read."""
    import shutil
    mark = data / ".imported-tabs-test-build"
    if mark.exists() or not tabs_dir.is_dir():
        return []
    copied = []
    for sub in ("designs", "imports"):
        src = tabs_dir / sub
        if not src.is_dir():
            continue
        for f in sorted(src.rglob("*")):
            dst = data / sub / f.relative_to(src)
            if f.is_file() and not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst)
                copied.append(f"{sub}/{f.relative_to(src)}")
    mark.write_text("\n".join(copied) + "\n")
    return copied


STUCK_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:#14171c;color:#e6e9ee;font:14px/1.5 -apple-system,BlinkMacSystemFont,sans-serif}
.box{position:absolute;inset:0;display:flex;align-items:center;justify-content:center}.in{width:440px;max-width:90vw}
h1{font-size:20px;margin:0 0 8px}p{color:#8b94a1;margin:0}</style></head><body><div class="box"><div class="in">
<h1>This window has stopped drawing</h1><p>Quit AgenticCAD and open it again. Your designs are saved, and anything running on a machine is not affected by this window.</p>
</div></div></body></html>"""
HIDDEN_LOOKS = 4                                    # looks in a row, 1.5 s apart: replacing the page must not answer a slow update
SCREENS_SETTLE = 10.0                               # seconds: a monitor change comes in several steps
RECOVERY_GAP = 30.0                                 # seconds a new page gets to load and report visible
RUNAWAY_STEP = 200 << 20                            # the page's footprint per look: the two real runaways ran at 200-300 MB/s
RUNAWAY_LOOKS = 4
RUNAWAY_FLOOR = 3 << 30                             # a healthy page sits near 400 MB
RECOVERY_WINDOW, RECOVERY_LIMIT = 600.0, 2          # a third in ten minutes: a new page is not what it needs


def _watch_page(native, alive, later, replace, give_up, footprint, screens, clock=time.monotonic, every: float = 1.5) -> None:
    """macOS: WKWebView applies each visibility change through a one-shot callback (a Core Animation post-commit handler
    or a run-loop observer). Around a display sleep or unplug that callback can be lost, and WebKit then drops every later
    change because it still thinks one is scheduled, so a page that was hidden stays frozen and the window shows black
    (the unified log shows activityStateDidChange with no dispatchActivityStateChange). Forcing the state through WebKit's
    immediate path does not help: the next update jams again, and twice the woken page's graphics memory ran at 200-300 MB/s
    to WebKit's 16 GB limit. The flag lives in the page's proxy and survives a web process reload, so the way out is a new
    WKWebView: `replace(why)` swaps one in. Two things call for it: a page that says it is hidden in a window that is
    on screen, on the active Space, with the displays settled; and a footprint that keeps climbing at the rate of those
    runaways, wherever the window is. A page that keeps needing it gets `give_up()`, a plain one with no WebGL.
    Runs on the main thread; `later(seconds, fn)` schedules the next look."""
    hidden = rising = 0
    size = None
    layout, moved = screens(), clock()
    recoveries: list[float] = []
    done = False

    def recover(why: str) -> None:
        nonlocal hidden, rising, size, done
        hidden = rising = 0
        size = None
        now = clock()
        recoveries[:] = [t for t in recoveries if now - t < RECOVERY_WINDOW] + [now]
        if len(recoveries) > RECOVERY_LIMIT:
            done = True
            give_up(why)
        else:
            replace(why)

    def seen(state, error=None) -> None:
        nonlocal hidden
        hidden = hidden + 1 if state == "hidden" else 0
        if hidden >= HIDDEN_LOOKS and not done:
            recover("hidden in a visible window")

    def tick() -> None:
        nonlocal hidden, rising, size, layout, moved
        if not alive() or done:
            return
        now = clock()
        current = screens()
        if current != layout:
            layout, moved, hidden = current, now, 0
        view, window = native.webview, native.window
        was, size = size, footprint(view)
        rising = rising + 1 if was is not None and size - was >= RUNAWAY_STEP else 0
        if rising >= RUNAWAY_LOOKS and size >= RUNAWAY_FLOOR:
            recover(f"footprint running away at {size >> 20} MB")
        elif (window.isVisible() and window.occlusionState() & 2 and window.screen() is not None and window.isOnActiveSpace()
              and now - moved >= SCREENS_SETTLE and not (recoveries and now - recoveries[-1] < RECOVERY_GAP)):
            view.evaluateJavaScript_completionHandler_("document.visibilityState", seen)
        else:                                       # covered, minimised, on another Space, or just moved: the page may sleep
            hidden = 0
        later(every, tick)

    tick()


def _footprint(view) -> int:
    """Bytes charged to the page's web process: the number WebKit's 16 GB limit goes by, and it counts the unmapped
    graphics memory that resident size misses. 0 if the process is gone or the private call is."""
    import ctypes
    info = (ctypes.c_uint64 * 64)()                 # rusage_info_v4; ri_phys_footprint is the tenth 64-bit field
    if ctypes.CDLL(None).proc_pid_rusage(view._webProcessIdentifier(), 4, info) != 0:
        return 0
    return info[9]


def _screens() -> list:
    import AppKit
    return [(int(s.deviceDescription()["NSScreenNumber"]), tuple(s.frame().origin), tuple(s.frame().size)) for s in AppKit.NSScreen.screens()]


def _new_web_view(native, load) -> None:
    """Swap the window's WKWebView for a new one on the same configuration. It goes into the window before it loads, so
    the new page takes its visibility from the window when it is created and not through an update that can jam.
    `load(view)` starts it. Without _close the old page's process stays up, hidden, with its WebSocket still open."""
    from webview.platforms.cocoa import BrowserView
    old = native.webview
    new = BrowserView.WebKitHost.alloc().initWithFrame_configuration_(old.frame(), old.configuration())
    new.pywebview_window = old.pywebview_window
    new.setUIDelegate_(native._browserDelegate)
    new.setNavigationDelegate_(native._browserDelegate)
    old.setNavigationDelegate_(None)
    old.setUIDelegate_(None)
    native.webview = new
    native.window.setContentView_(new)
    native.window.makeFirstResponder_(new)
    load(new)
    old._close()


def _start_page_watch(window, note) -> None:
    from Foundation import NSURLRequest
    from PyObjCTools import AppHelper
    from webview.platforms.cocoa import BrowserView
    native = BrowserView.instances.get(window.uid)
    if native is None or not all(native.webview.respondsToSelector_(s) for s in (b"_webProcessIdentifier", b"_close")):
        return

    def replace(why: str) -> None:
        note(f"replacing the web view: {why}; screens {_screens()}")
        url = native.webview.URL()
        _new_web_view(native, lambda view: view.loadRequest_(NSURLRequest.requestWithURL_(url)))

    def give_up(why: str) -> None:
        note(f"giving up, showing the quit page: {why}; screens {_screens()}")
        _new_web_view(native, lambda view: view.loadHTMLString_baseURL_(STUCK_HTML, None))

    AppHelper.callAfter(_watch_page, native, lambda: window.uid in BrowserView.instances, AppHelper.callLater,
                        replace, give_up, _footprint, _screens)


def main() -> None:
    root = _repo_root()
    sys.path.insert(0, str(root))
    os.chdir(root)

    import platformdirs
    app_name = "AgenticCAD"
    data = Path(os.environ.get("AGENTICCAD_WORKSPACE") or platformdirs.user_data_dir(app_name, "AgenticCAD"))
    data.mkdir(parents=True, exist_ok=True)
    if not os.environ.get("AGENTICCAD_WORKSPACE"):
        _import_tabs_test_build(Path(platformdirs.user_data_dir("AgenticCAD Tabs", "AgenticCAD")), data)
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
        state["url"] = base + ("/tabs" if cli else "/setup")
        if window is not None:
            say("Opening the workspace")
            window.load_url(state["url"])
            if sys.platform == "darwin":
                def note(text: str) -> None:        # stdout goes nowhere in the app; this is what the next incident leaves behind
                    with (data / "window.log").open("a") as f:
                        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
                try:
                    _start_page_watch(window, note)
                except Exception as e:              # private WebKit API: without it the app still runs as before
                    note(f"no page watch: {e}")
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
        window = webview.create_window(f"{app_name} {_v.__version__}", html=SPLASH_HTML, width=1500, height=950, min_size=(1000, 650),
                                       background_color="#14171c", text_select=True)
        webview.start(boot, window, debug=bool(os.environ.get("AGENTICCAD_DEBUG")))   # blocks until the window closes
        del window
    if state["srv"] is not None:
        state["srv"].should_exit = True
    time.sleep(0.5)
    os._exit(0)                                    # the SDK's CLI subprocess and the server thread go with us


if __name__ == "__main__":
    main()
