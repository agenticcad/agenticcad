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

    import agent as agent_mod                       # noqa: E402  (after sys.path)
    cli = agent_mod.find_claude_cli()
    port = _free_port(int(os.environ.get("PORT") or 8765))
    os.environ["PORT"] = str(port)

    import uvicorn
    import server                                   # noqa: E402  (reads AGENTICCAD_WORKSPACE at import)
    config = uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    threading.Thread(target=srv.run, name="agenticcad-server", daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(200):                            # wait for the server (≤10 s)
        try:
            urllib.request.urlopen(base + "/api/version", timeout=0.5).read()
            break
        except Exception:
            time.sleep(0.05)

    url = base + ("/" if cli else "/setup")
    try:
        import webview
    except Exception:                               # no native web view available: fall back to the browser
        import webbrowser
        webbrowser.open(url)
        print(f"AgenticCAD running at {url} (workspace {data}); Ctrl-C to quit")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    else:
        window = webview.create_window(f"AgenticCAD {server.__version__}", url, width=1500, height=950, min_size=(1000, 650),
                                       background_color="#14171c", text_select=True)
        webview.start(debug=bool(os.environ.get("AGENTICCAD_DEBUG")))   # blocks until the window closes
        del window
    srv.should_exit = True
    time.sleep(0.5)
    os._exit(0)                                    # the SDK's CLI subprocess and the server thread go with us


if __name__ == "__main__":
    main()
