#!/usr/bin/env python3
"""
browser-eyes daemon (full DevTools edition).

Runs in background. Launches a chromium-family browser with CDP enabled,
holds persistent connections, mirrors network/console/script events into
memory and a SQLite ring buffer, captures the screen, fulfills Fetch
interceptions for the local-override system, and exposes everything over
a Unix socket.
"""
import asyncio
import json
import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cdp import CDPClient, CDPError
import handlers

# ---------- Paths ----------

STATE_DIR   = Path.home() / ".local" / "state" / "browser-eyes"
STATE_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH     = STATE_DIR / "events.db"
SOCK_PATH   = STATE_DIR / "daemon.sock"
FRAME_DIR   = STATE_DIR / "frames"
FRAME_DIR.mkdir(exist_ok=True)
PID_FILE    = STATE_DIR / "daemon.pid"
LOG_FILE    = STATE_DIR / "daemon.log"

CDP_PORT          = 9222
FRAME_INTERVAL    = 1.0
RING_BUFFER_SECS  = 600   # 10 min on disk
MAX_FRAMES_ON_DISK = 120

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE),
    ],
)
log = logging.getLogger("browser-eyes.daemon")

# ---------- Environment detection ----------

def detect_display():
    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if session == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    return "x11"

def pick_screenshot_tool(kind):
    if kind == "wayland":
        if shutil.which("grim"):              return ["grim", "{out}"]
        if shutil.which("gnome-screenshot"): return ["gnome-screenshot", "-f", "{out}"]
        if shutil.which("spectacle"):        return ["spectacle", "-b", "-n", "-o", "{out}"]
    else:
        if shutil.which("scrot"):  return ["scrot", "-z", "{out}"]
        if shutil.which("maim"):   return ["maim", "{out}"]
        if shutil.which("import"): return ["import", "-window", "root", "{out}"]
    return None  # daemon still works without desktop frames

def pick_browser():
    for name in ["google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "brave-browser", "microsoft-edge", "vivaldi"]:
        path = shutil.which(name)
        if path:
            return path, name
    raise RuntimeError("no chromium-family browser found")

# ---------- DB ----------

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      REAL    NOT NULL,
            kind    TEXT    NOT NULL,
            payload TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_ts   ON events(ts);
        CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
    """)
    con.commit()
    return con

def log_event(con, kind, payload):
    con.execute(
        "INSERT INTO events(ts, kind, payload) VALUES(?,?,?)",
        (time.time(), kind, json.dumps(payload, default=str)[:100_000]),
    )
    con.commit()

def prune(con):
    cutoff = time.time() - RING_BUFFER_SECS
    con.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
    con.commit()
    frames = sorted(FRAME_DIR.glob("frame*.png"))
    if len(frames) > MAX_FRAMES_ON_DISK:
        for f in frames[:-MAX_FRAMES_ON_DISK]:
            with suppress(FileNotFoundError):
                f.unlink()

# ---------- Browser launch ----------

def launch_browser(path):
    profile = STATE_DIR / "browserprofile"
    profile.mkdir(exist_ok=True)
    args = [
        path,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--restore-last-session=false",
        "--disable-features=AutomationControlled",
        "about:blank",
    ]
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

async def wait_for_cdp(timeout=20):
    import aiohttp
    deadline = time.time() + timeout
    async with aiohttp.ClientSession() as sess:
        while time.time() < deadline:
            try:
                async with sess.get(f"http://127.0.0.1:{CDP_PORT}/json/version") as r:
                    if r.status == 200:
                        return await r.json()
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.3)
    raise RuntimeError("browser CDP didn't come up")

# ---------- Per-target attach ----------

async def attach_target(cdp: CDPClient, target_id: str):
    """Enable the domains we care about so events stream in."""
    for method in [
        "Page.enable", "Runtime.enable", "Network.enable", "Log.enable",
        "DOM.enable", "DOMStorage.enable", "Debugger.enable", "CSS.enable",
    ]:
        with suppress(CDPError):
            await cdp.targetcall(target_id, method)

async def target_watcher(cdp: CDPClient, db_con):
    """Poll for new tabs and attach to them."""
    attached = set()
    while True:
        try:
            targets = await cdp.listtargets()
        except Exception:
            await asyncio.sleep(2)
            continue
        for t in targets:
            if t.get("type") != "page":
                continue
            tid = t["id"]
            if tid in attached:
                continue
            attached.add(tid)
            try:
                await cdp.connecttarget(tid)
                await attach_target(cdp, tid)
                log_event(db_con, "navigation", {
                    "phase": "attached", "target": tid,
                    "url": t.get("url"), "title": t.get("title"),
                })
                log.info("attached to %s %s", tid[:8], t.get("url", "")[:80])
            except Exception as e:
                log.warning("attach failed for %s: %s", tid, e)
                attached.discard(tid)
        live_ids = {t["id"] for t in targets if t.get("type") == "page"}
        for tid in list(attached):
            if tid not in live_ids:
                attached.discard(tid)
                with suppress(Exception):
                    await cdp.disconnecttarget(tid)
        await asyncio.sleep(2)

# ---------- Global event handler ----------

def make_event_handler(state: handlers.HandlerState, db_con, cdp: CDPClient):
    async def handle(target_id, method, params):
        # ----- Network -----
        if method == "Network.requestWillBeSent":
            rid = params["requestId"]
            entry = state.networklog.setdefault(rid, {})
            entry["targetid"] = target_id
            entry["request"] = params.get("request")
            entry["type"]    = params.get("type")
            entry["started"] = time.time()
            entry["documenturl"] = params.get("documentURL")
            log_event(db_con, "network", {
                "phase": "request", "id": rid,
                "method": entry["request"].get("method"),
                "url": entry["request"].get("url"),
                "type": entry["type"],
            })
        elif method == "Network.responseReceived":
            rid = params["requestId"]
            entry = state.networklog.setdefault(rid, {})
            entry["response"] = params.get("response")
            log_event(db_con, "network", {
                "phase": "response", "id": rid,
                "status": entry["response"].get("status"),
                "url": entry["response"].get("url"),
                "mime": entry["response"].get("mimeType"),
            })
        elif method == "Network.loadingFinished":
            rid = params["requestId"]
            entry = state.networklog.setdefault(rid, {})
            entry["ended"] = time.time()
            entry["encodedsize"] = params.get("encodedDataLength")
        elif method == "Network.loadingFailed":
            rid = params["requestId"]
            entry = state.networklog.setdefault(rid, {})
            entry["failed"] = params.get("errorText")
            entry["ended"]  = time.time()

        if len(state.networklog) > 2000:  # cap network log
            keep = sorted(
                state.networklog.items(),
                key=lambda kv: kv[1].get("started", 0)
            )[-1000:]
            state.networklog = dict(keep)

        # ----- Console -----
        if method == "Runtime.consoleAPICalled":
            args_repr = []
            for a in params.get("args", []):
                v = a.get("value")
                if v is None:
                    v = a.get("description", a.get("type"))
                args_repr.append(v)
            entry = {
                "ts": time.time(), "target": target_id,
                "level": params.get("type"), "args": args_repr,
                "stack": params.get("stackTrace"),
            }
            state.consolelog.append(entry)
            if len(state.consolelog) > 5000:
                state.consolelog = state.consolelog[-2500:]
            log_event(db_con, "console", entry)
        elif method == "Log.entryAdded":
            e = params.get("entry", {})
            entry = {
                "ts": time.time(), "target": target_id,
                "level": e.get("level"), "text": e.get("text"),
                "source": e.get("source"), "url": e.get("url"),
            }
            state.consolelog.append(entry)
            log_event(db_con, "console", entry)
        elif method == "Runtime.exceptionThrown":
            ex = params.get("exceptionDetails", {})
            entry = {
                "ts": time.time(), "target": target_id, "level": "error",
                "text": ex.get("text"),
                "exception": ex.get("exception", {}).get("description"),
                "stack": ex.get("stackTrace"),
            }
            state.consolelog.append(entry)
            log_event(db_con, "console", entry)

        # ----- Navigation -----
        elif method == "Page.frameNavigated":
            f = params.get("frame", {})
            if not f.get("parentId"):
                log_event(db_con, "navigation", {
                    "phase": "loaded", "target": target_id, "url": f.get("url"),
                })

        # ----- Scripts (for editjs / listscripts) -----
        elif method == "Debugger.scriptParsed":
            scripts = state.dict.setdefault("scripts", [])
            scripts.append({
                "scriptid": params.get("scriptId"),
                "url":      params.get("url"),
                "target":   target_id,
                "hash":     params.get("hash"),
                "length":   params.get("length"),
            })
            if len(scripts) > 5000:
                state.dict["scripts"] = scripts[-2500:]

        # ----- Stylesheets (for editcss / listscripts) -----
        elif method == "CSS.styleSheetAdded":
            sheets = state.dict.setdefault("stylesheets", [])
            h = params.get("header", {})
            sheets.append({
                "stylesheetid": h.get("styleSheetId"),
                "url":          h.get("sourceURL"),
                "origin":       h.get("origin"),
                "target":       target_id,
            })
            if len(sheets) > 2000:
                state.dict["stylesheets"] = sheets[-1000:]

        # ----- Fetch interception (file overrides) -----
        elif method == "Fetch.requestPaused":
            req_id = params.get("requestId")
            url    = params.get("request", {}).get("url", "")
            import fnmatch, base64 as b64
            match = None
            for o in state.overrides.values():
                if fnmatch.fnmatch(url, o["pattern"]):
                    match = o
                    break
            if match:
                body_bytes = Path(match["bodypath"]).read_bytes()
                await cdp.targetcall(target_id, "Fetch.fulfillRequest", {
                    "requestId": req_id,
                    "responseCode": match["status"],
                    "responseHeaders": [
                        {"name": "Content-Type", "value": match["mime"]},
                        {"name": "X-Browser-Eyes-Override", "value": match["id"]},
                    ],
                    "body": b64.b64encode(body_bytes).decode(),
                })
                log.info("override served %s -> %s", url, match["id"])
            else:
                with suppress(CDPError):
                    await cdp.targetcall(target_id, "Fetch.continueRequest", {"requestId": req_id})
    return handle

# ---------- Screen capture loop ----------

async def screenloop(con, cmd_template):
    if not cmd_template:
        log.warning("no screenshot tool installed, desktop frames disabled")
        return
    while True:
        out = FRAME_DIR / f"frame{int(time.time()*1000)}.png"
        cmd = [a.replace("{out}", str(out)) for a in cmd_template]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if out.exists() and out.stat().st_size > 0:
                log_event(con, "frame", {"path": str(out), "size": out.stat().st_size})
        except Exception as e:
            log.warning("screenshot failed: %s", e)
        prune(con)
        await asyncio.sleep(FRAME_INTERVAL)

# ---------- Socket server ----------

async def handle_client(reader, writer, state: handlers.HandlerState):
    try:
        line = await reader.readline()
        if not line:
            return
        req = json.loads(line.decode())
        op = req.pop("op", None)
        handler = handlers.HANDLERS.get(op)
        if not handler:
            resp = {"ok": False, "error": f"unknown op: {op}",
                    "available": sorted(handlers.HANDLERS.keys())}
        else:
            try:
                data = await handler(state, req)
                resp = {"ok": True, "data": data}
            except Exception as e:
                log.exception("handler %s failed", op)
                resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    except Exception as e:
        resp = {"ok": False, "error": f"protocol: {e}"}
    try:
        writer.write(json.dumps(resp, default=str).encode() + b"\n")
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()

# ---------- Main ----------

async def main():
    PID_FILE.write_text(str(os.getpid()))
    display       = detect_display()
    screenshot_cmd = pick_screenshot_tool(display)
    browser_path, browser_name = pick_browser()
    log.info("display=%s browser=%s", display, browser_name)
    log.info("screenshots: %s", " ".join(screenshot_cmd) if screenshot_cmd else "none")

    con = init_db()
    log_event(con, "console", {"level": "daemon", "text": "starting"})

    browser_proc = launch_browser(browser_path)
    log.info("launched browser pid=%d", browser_proc.pid)

    info = await wait_for_cdp()
    log.info("cdp ready: %s", info.get("Browser"))

    cdp   = CDPClient(port=CDP_PORT)
    await cdp.connectbrowser()
    state = handlers.HandlerState(cdp, DB_PATH, STATE_DIR)
    cdp.onevent(make_event_handler(state, con, cdp))

    if SOCK_PATH.exists():
        SOCK_PATH.unlink()
    server = await asyncio.start_unix_server(
        lambda r, w: handle_client(r, w, state),
        path=str(SOCK_PATH),
    )
    os.chmod(SOCK_PATH, 0o600)

    tasks = [
        asyncio.create_task(target_watcher(cdp, con)),
        asyncio.create_task(screenloop(con, screenshot_cmd)),
        asyncio.create_task(server.serve_forever()),
    ]

    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: stop.set_result(None))

    await stop
    log.info("shutting down")
    for t in tasks:
        t.cancel()
    with suppress(Exception):
        await asyncio.gather(*tasks, return_exceptions=True)
    server.close()
    await cdp.close()
    with suppress(FileNotFoundError):
        SOCK_PATH.unlink()
    PID_FILE.unlink()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
