"""Share a design by email: bundle it (design script, CAM script, exact STEP, a README) into a .zip and open a new
message in the user's own mail app with the .zip attached, ready for them to address and send. Nothing is sent by the
app and nothing goes through any service: the mail app does the sending, when the user presses Send.

How the message is opened: mailto: works with every mail app (Apple Mail, Outlook old and new, Thunderbird, a webmail
site set as the browser's mail handler) but can't carry an attachment, so where the user's default mail app has a way
to take one, that is used, and mailto: is the fallback that always works:
  Apple Mail       macOS's "compose email" share (NSSharingService), run on the desktop app's main thread; subject, text
                   and the .zip. (That share is Apple Mail's: other apps don't reliably take it.)
  Outlook (Mac)    AppleScript: a new message with subject, text and the .zip. Legacy Outlook supports it; new Outlook's
                   scripting is partial, so a failure falls through to mailto. macOS asks once to let AgenticCAD control it.
  Thunderbird      its -compose command line (only when it isn't already running).
  Windows          Simple MAPI (MAPISendMailW), what Explorer's Send to > Mail recipient uses: Outlook (classic),
                   Thunderbird, eM Client... New Outlook isn't fully MAPI-compatible: an error falls through to mailto.
  Linux            xdg-email --attach.
  anything else    mailto: with the subject and text, and the .zip shown selected in Finder / Explorer to drag in.

The other half: read_bundle() takes a shared .zip back (File > Import design), and risky() lists what in a script
reaches past geometry (files, network, processes), because design and CAM scripts run as Python on the user's computer.
"""
from __future__ import annotations

import ast
import io
import os
import re
import subprocess
import sys
import threading
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any

SITE = "https://agenticcad.github.io/agenticcad/"
MAX_SCRIPT = 2_000_000                  # bytes: a bigger "script" in a shared zip is not one


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name or "").strip("_.")[:60] or "design"


# ---------------------------------------------------------------- the bundle
def readme(name: str, version: str, cam: dict | None, has_step: bool) -> str:
    lines = [f"{name}: a design shared from AgenticCAD {version}", "", "Files:",
             f"  {_safe(name)}.py       the design (a build123d script). Open it in AgenticCAD with File > Import design;",
             "                   importing the .zip brings the CAM program with it."]
    if cam:
        lines.append(f"  {_safe(name)}.cam.py   its CAM program" + (f" ({cam['summary']})" if cam.get("summary") else "") + ".")
    if has_step:
        lines.append(f"  {_safe(name)}.step     the exact geometry, for any CAD program.")
    lines += ["", f"AgenticCAD: {SITE}", "",
              "Design and CAM scripts are Python and run on the computer that opens them: open designs from people you trust.",
              "CAM is experimental: simulate and air-cut a program before cutting with it."]
    return "\n".join(lines) + "\n"


def bundle(out_dir: Path, name: str, code: str, cam_code: str | None, step: Path | None, version: str,
           cam: dict | None = None) -> Path:
    """<out_dir>/<name>.zip with the design, its CAM script, the STEP and a README; returns the path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = _safe(name)
    path = out_dir / f"{base}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{base}/{base}.py", code)
        if cam_code and cam_code.strip():
            z.writestr(f"{base}/{base}.cam.py", cam_code)
        if step is not None and Path(step).exists():
            z.write(step, f"{base}/{base}.step")
        z.writestr(f"{base}/README.txt", readme(name, version, cam if cam_code and cam_code.strip() else None, step is not None))
    return path


def message(name: str, version: str, cam: dict | None, has_step: bool) -> tuple[str, str]:
    """(subject, text) for the email."""
    subject = f"{name} (AgenticCAD design)"
    parts = [f"Here's {name}, a design from AgenticCAD.", ""]
    att = [f"{_safe(name)}.zip has the design script"]
    if cam:
        att.append("its CAM program" + (f" ({cam['summary']})" if cam.get("summary") else ""))
    if has_step:
        att.append("a STEP file for any CAD program")
    parts.append(", ".join(att[:-1]) + (" and " if len(att) > 1 else "") + att[-1] + ".")
    parts += ["", "To open it in AgenticCAD: File > Import design, and pick the .zip.", f"AgenticCAD: {SITE}", ""]
    return subject, "\n".join(parts)


# ---------------------------------------------------------------- reading one back
def read_bundle(data: bytes) -> dict[str, Any]:
    """A shared .zip -> {name, code, cam_code}: the design is the one .py that isn't a .cam.py."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ValueError("not a .zip file") from e
    pys = [i for i in z.infolist() if i.filename.endswith(".py") and not i.is_dir() and "__MACOSX" not in i.filename]
    designs = [i for i in pys if not i.filename.endswith(".cam.py")]
    if len(designs) != 1:
        raise ValueError(f"expected one design script in the .zip, found {len(designs)}")
    d = designs[0]
    if d.file_size > MAX_SCRIPT:
        raise ValueError("the design script in the .zip is too big")
    name = Path(d.filename).name[:-3]
    cams = [i for i in pys if i.filename.endswith(".cam.py") and Path(i.filename).name[:-7] == name] or \
           [i for i in pys if i.filename.endswith(".cam.py")]
    cam = cams[0] if len(cams) == 1 and cams[0].file_size <= MAX_SCRIPT else None
    return {"name": name, "code": z.read(d).decode("utf-8", "replace"),
            "cam_code": z.read(cam).decode("utf-8", "replace") if cam else None}


# modules design and CAM scripts use (geometry, maths); anything else is listed for the user before the script runs
_OK_MODULES = {"build123d", "math", "cmath", "numpy", "shapely", "scipy", "OCP", "cam_kernel", "bd_warehouse", "kit", "designkit",
               "typing", "dataclasses", "itertools", "functools", "collections", "copy", "enum", "statistics", "random",
               "fractions", "decimal", "operator", "string", "textwrap", "__future__"}
_RISKY_CALLS = {"open", "exec", "eval", "compile", "__import__", "input", "breakpoint", "globals", "vars", "getattr", "setattr", "delattr"}


def risky(code: str, label: str = "script") -> list[str]:
    """What in a script reaches past geometry: imports of other modules, file/eval-style builtins, dunder access."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []                          # it can't run either: the build reports the error
    out: list[str] = []
    for n in ast.walk(tree):
        mods = []
        if isinstance(n, ast.Import):
            mods = [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            mods = [n.module]
        for m in mods:
            if m.split(".")[0] not in _OK_MODULES:
                out.append(f"{label} line {n.lineno}: imports {m}")
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _RISKY_CALLS:
            out.append(f"{label} line {n.lineno}: calls {n.func.id}()")
        if isinstance(n, ast.Attribute) and n.attr.startswith("__") and n.attr.endswith("__") and n.attr not in ("__init__", "__name__"):
            out.append(f"{label} line {n.lineno}: uses {n.attr}")
    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))][:20]


# ---------------------------------------------------------------- opening the user's mail app
def default_mail_app() -> dict[str, str] | None:
    """The app that opens mailto: links: {id (bundle id on macOS), name, path}."""
    if sys.platform != "darwin":
        return None
    try:
        import AppKit  # type: ignore
        import Foundation  # type: ignore
        u = AppKit.NSWorkspace.sharedWorkspace().URLForApplicationToOpenURL_(Foundation.NSURL.URLWithString_("mailto:"))
        if u is None:
            return None
        b = Foundation.NSBundle.bundleWithURL_(u)
        return {"id": str(b.bundleIdentifier() or "") if b else "", "name": Path(str(u.path())).stem, "path": str(u.path())}
    except Exception:  # noqa: BLE001
        return None


def compose(path: Path, subject: str, text: str) -> dict[str, str]:
    """Open a new message with `path` attached in the user's mail app. Returns {how, app, note}: how = share | outlook |
    thunderbird | mapi | xdg | open (attached) or manual (mailto: opened the message with the subject and text, and the
    file is shown in the file manager to drag in)."""
    path = Path(path).resolve()
    app = default_mail_app()
    name = app["name"] if app else ""
    note = ""
    try:
        r = None
        if sys.platform == "darwin":
            bid = (app or {}).get("id", "")
            if bid == "com.apple.mail":
                r = _mac_share(path, subject, text) or _mac_open(path, app)
            elif bid.startswith("com.microsoft.Outlook"):
                r = _outlook_mac(path, subject, text)
            elif bid == "org.mozilla.thunderbird":
                r = _thunderbird_mac(path, subject, text, app)
        elif sys.platform == "win32":
            r = _win_mapi(path, subject, text)
        else:
            r = _xdg(path, subject, text)
        if r:
            return {"app": name, "note": "", **r}
    except Exception as e:  # noqa: BLE001 - any failure falls through to mailto
        note = f"{type(e).__name__}: {e}"
    _reveal(path)
    _open_url(mailto_url(subject, text, path.name))
    return {"how": "manual", "app": name, "note": note}


def mailto_url(subject: str, text: str, fname: str) -> str:
    return "mailto:?" + urllib.parse.urlencode({"subject": subject, "body": text + f"\n(Attach {fname}: it's selected in the window that just opened.)\n"},
                                               quote_via=urllib.parse.quote)


def _mac_share(path: Path, subject: str, text: str) -> dict | None:
    try:
        import AppKit  # type: ignore
        import Foundation  # type: ignore
        from PyObjCTools import AppHelper  # type: ignore
    except ImportError:
        return None
    app = AppKit.NSApp()
    if app is None or not app.isRunning():           # no window loop to run on (the dev server in a browser)
        return None
    res: dict[str, Any] = {}
    done = threading.Event()

    def go():
        try:
            svc = AppKit.NSSharingService.sharingServiceNamed_(AppKit.NSSharingServiceNameComposeEmail)
            items = [text, Foundation.NSURL.fileURLWithPath_(str(path))]
            if svc is None or not svc.canPerformWithItems_(items):
                res["err"] = "this Mac has no mail app set up for sharing"
            else:
                svc.setSubject_(subject)
                svc.performWithItems_(items)
                res["ok"] = True
        except Exception as e:  # noqa: BLE001
            res["err"] = f"{type(e).__name__}: {e}"
        finally:
            done.set()

    AppHelper.callAfter(go)
    if not done.wait(15) or not res.get("ok"):
        return None
    return {"how": "share"}


def _mac_open(path: Path, app: dict | None) -> dict | None:
    """Apple Mail opens a file it's given as a new message with it attached (no subject or text)."""
    if not app:
        return None
    r = subprocess.run(["open", "-a", app["path"], str(path)], capture_output=True, timeout=20)
    return {"how": "open"} if r.returncode == 0 else None


OUTLOOK_SCRIPT = """on run argv
    tell application id "com.microsoft.Outlook"
        set m to make new outgoing message with properties {subject:(item 1 of argv), plain text content:(item 2 of argv)}
        make new attachment at m with properties {file:(POSIX file (item 3 of argv))}
        open m
        activate
    end tell
end run
"""


def _outlook_mac(path: Path, subject: str, text: str) -> dict | None:
    r = subprocess.run(["osascript", "-", subject, text, str(path)], input=OUTLOOK_SCRIPT, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("Outlook didn't take the message (new Outlook's scripting is partial; macOS may also have "
                           "been told not to let AgenticCAD control Outlook): " + (r.stderr or "").strip()[:200])
    return {"how": "outlook"}


def thunderbird_args(path: Path, subject: str, text: str) -> str:
    """Thunderbird's -compose argument: field='value' pairs; values can't hold a straight quote."""
    q = lambda v: v.replace("'", "\u2019")
    return f"subject='{q(subject)}',body='{q(text)}',attachment='{path.as_uri()}'"


def _thunderbird_mac(path: Path, subject: str, text: str, app: dict) -> dict | None:
    import AppKit  # type: ignore
    if AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_("org.mozilla.thunderbird"):
        return None                                     # -compose only works when it isn't running (one instance on macOS)
    exe = next((p for p in (Path(app["path"]) / "Contents" / "MacOS" / n for n in ("thunderbird", "thunderbird-bin")) if p.exists()), None)
    if exe is None:
        return None
    subprocess.Popen([str(exe), "-compose", thunderbird_args(path, subject, text)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return {"how": "thunderbird"}


def _win_mapi(path: Path, subject: str, text: str) -> dict | None:
    import ctypes
    from ctypes import wintypes

    class FileDesc(ctypes.Structure):
        _fields_ = [("ulReserved", wintypes.ULONG), ("flFlags", wintypes.ULONG), ("nPosition", wintypes.ULONG),
                    ("lpszPathName", wintypes.LPWSTR), ("lpszFileName", wintypes.LPWSTR), ("lpFileType", ctypes.c_void_p)]

    class Message(ctypes.Structure):
        _fields_ = [("ulReserved", wintypes.ULONG), ("lpszSubject", wintypes.LPWSTR), ("lpszNoteText", wintypes.LPWSTR),
                    ("lpszMessageType", wintypes.LPWSTR), ("lpszDateReceived", wintypes.LPWSTR), ("lpszConversationID", wintypes.LPWSTR),
                    ("flFlags", wintypes.ULONG), ("lpOriginator", ctypes.c_void_p), ("nRecipCount", wintypes.ULONG),
                    ("lpRecips", ctypes.c_void_p), ("nFileCount", wintypes.ULONG), ("lpFiles", ctypes.POINTER(FileDesc))]

    try:
        send = ctypes.WinDLL("mapi32.dll").MAPISendMailW
    except (OSError, AttributeError):
        return None
    f = FileDesc(0, 0, 0xFFFFFFFF, str(path), path.name, None)
    msg = Message(0, subject, text, None, None, None, 0, None, 0, None, 1, ctypes.pointer(f))
    res: dict[str, int] = {}

    def go():                                          # MAPI_DIALOG is modal: it returns when the user sends or closes
        res["rc"] = send(0, 0, ctypes.byref(msg), 0x1 | 0x8, 0)   # MAPI_LOGON_UI | MAPI_DIALOG

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(4)
    if "rc" in res and res["rc"] not in (0, 1):        # 0 sent, 1 user abort; anything else: no mail client took it
        return None
    return {"how": "mapi"}


def _xdg(path: Path, subject: str, text: str) -> dict | None:
    try:
        r = subprocess.run(["xdg-email", "--subject", subject, "--body", text, "--attach", str(path)], capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {"how": "xdg"} if r.returncode == 0 else None


def _reveal(path: Path) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", str(path)], timeout=10)
        elif sys.platform == "win32":
            subprocess.run(["explorer", f"/select,{path}"], timeout=10)
        else:
            subprocess.run(["xdg-open", str(path.parent)], timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _open_url(url: str) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", url], timeout=10)
        elif sys.platform == "win32":
            os.startfile(url)  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", url], timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass
