#!/usr/bin/env python3
"""
browser-eyes MCP server (full edition).

Thin layer that Claude Code spawns over stdio. Translates MCP tool
calls into Unix-socket queries against the daemon. Every handler in
handlers.py is exposed as a tool with a JSON schema.
"""

import asyncio
import base64
import json
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, ImageContent

STATE_DIR = Path.home() / ".local" / "state" / "browser-eyes"
SOCK_PATH = STATE_DIR / "daemon.sock"


# ---------- Daemon comms ----------

async def call_daemon(op, **kwargs):
    if not SOCK_PATH.exists():
        return {"ok": False, "error": f"daemon not running. Start it: browser-eyes start"}
    try:
        reader, writer = await asyncio.open_unix_connection(str(SOCK_PATH))
    except (FileNotFoundError, ConnectionRefusedError) as e:
        return {"ok": False, "error": f"daemon unreachable: {e}"}
    req = {"op": op, **kwargs}
    writer.write(json.dumps(req).encode() + b"\n")
    await writer.drain()

    # Read until newline
    chunks = []
    while True:
        chunk = await reader.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    data = b"".join(chunks)
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    if not data:
        return {"ok": False, "error": "empty response from daemon"}
    return json.loads(data.decode().split("\n")[0])


def text(s):
    return TextContent(type="text", text=s)


def jsontext(d):
    return TextContent(type="text", text=json.dumps(d, indent=2, default=str))


# ---------- MCP server ----------

# TOOLS: (name, description, schema)
TOOLS = [
    # VISION
    ("seescreen",         "Latest screenshot of the user's whole desktop from the ring buffer. Use when the user says 'look at my screen' or visual context matters.", {}),
    ("screenshottab",     "Screenshot just the active browser tab's viewport — cleaner than the full desktop. Returns the saved file path.",
        {"format": {"type": "string", "enum": ["png", "jpeg"]}, "quality": {"type": "integer"}}),
    ("screenshotfullpage", "Capture the entire scrollable page, not just the viewport. Great for documentation or long pages.", {}),
    ("screenshotelement", "Screenshot a single element matched by CSS selector.",
        {"selector": {"type": "string", "required": True}}),

    # DOM
    ("getdom",            "Full HTML of the active tab. Truncates after 200k chars.", {}),
    ("query",             "Find elements by CSS selector. Returns tag, id, classes, text, attributes, and outerHTML for each match.",
        {"selector": {"type": "string", "required": True}, "limit": {"type": "integer"}}),
    ("sethtml",           "Replace an element's outerHTML. Live DOM edit.",
        {"selector": {"type": "string", "required": True}, "html": {"type": "string", "required": True}}),
    ("setattribute",      "Set an attribute on an element.",
        {"selector": {"type": "string", "required": True}, "name": {"type": "string", "required": True}, "value": {"type": "string", "required": True}}),
    ("removeelement",     "Remove an element from the DOM.",
        {"selector": {"type": "string", "required": True}}),
    ("accessibilitytree", "Full accessibility AX tree of the page. Use for understanding semantic structure.", {}),
    ("inspectatpoint",    "Get element info at viewport coordinates (x, y). Returns tag, classes, text, and a best-guess selector.",
        {"x": {"type": "number", "required": True}, "y": {"type": "number", "required": True}}),

    # CONSOLE / JS
    ("runjs",             "Execute JavaScript in the active tab. Awaits promises. Use for extracting data the DOM doesn't show, testing behavior, monkey-patching functions, anything.",
        {"code": {"type": "string", "required": True}, "await": {"type": "boolean"}}),
    ("consolehistory",    "Rolling console log captured from the page (log, info, warn, error, thrown exceptions). Filter by level.",
        {"level": {"type": "string"}, "limit": {"type": "integer"}}),
    ("clearconsole",      "Clear the captured console log.", {}),

    # NETWORK
    ("listrequests",      "Filter and list captured network requests. Filters: urlcontains, method, status, type (Document/Stylesheet/Script/XHR/Fetch/Image/etc).",
        {"urlcontains": {"type": "string"}, "method": {"type": "string"}, "status": {"type": "integer"}, "type": {"type": "string"}, "limit": {"type": "integer"}}),
    ("requestdetails",    "Full request+response metadata for one request id (from listrequests).",
        {"id": {"type": "string", "required": True}}),
    ("responsebody",      "Fetch the response body of a captured request. Returns text or saves binary to disk.",
        {"id": {"type": "string", "required": True}}),
    ("getcurl",           "Reconstruct a curl command for replaying a captured request.",
        {"id": {"type": "string", "required": True}}),
    ("exporthar",         "Export the network log as a HAR file (importable into DevTools, Charles, etc).", {}),
    ("blockurl",          "Block all requests matching a URL pattern (glob-style).",
        {"pattern": {"type": "string", "required": True}}),
    ("unblockurl",        "Remove a URL block.",
        {"pattern": {"type": "string", "required": True}}),
    ("listblocks",        "List currently-blocked URL patterns.", {}),
    ("setextraheaders",   "Inject extra HTTP headers into every outgoing request from the tab. Useful for testing auth bypasses, language headers, etc.",
        {"headers": {"type": "object", "required": True}}),

    # COOKIES
    ("cookieslist",       "List cookies, optionally filtered by domain substring.",
        {"domain": {"type": "string"}}),
    ("cookiesset",        "Set one or more cookies. Pass cookies as an array of cookie objects, each with name, value, domain, path, etc.",
        {"cookies": {"type": "array"}}),
    ("cookiesdelete",     "Delete a single cookie by name (+ optional url or domain).",
        {"name": {"type": "string", "required": True}, "url": {"type": "string"}, "domain": {"type": "string"}}),
    ("cookiesdeleteall",  "Wipe ALL cookies browser-wide. Destructive.", {}),

    # LOCAL / SESSION STORAGE / INDEXEDDB
    ("localstoragelist",    "List localStorage entries for the tab's origin or a given origin.", {"origin": {"type": "string"}}),
    ("localstorageset",     "Set a localStorage key.",
        {"key": {"type": "string", "required": True}, "value": {"type": "string", "required": True}, "origin": {"type": "string"}}),
    ("localstoragedelete",  "Delete a localStorage key.",
        {"key": {"type": "string", "required": True}, "origin": {"type": "string"}}),
    ("localstorageclear",   "Clear all localStorage for an origin.", {"origin": {"type": "string"}}),
    ("sessionstoragelist",  "List sessionStorage entries for an origin.", {"origin": {"type": "string"}}),
    ("sessionstorageset",   "Set a sessionStorage key.",
        {"key": {"type": "string", "required": True}, "value": {"type": "string", "required": True}, "origin": {"type": "string"}}),
    ("indexeddblist",       "List IndexedDB database names for an origin.", {"origin": {"type": "string"}}),
    ("clearbrowserdata",    "Wipe storage. Types is a comma list: cookies,localstorage,sessionstorage,indexeddb,cachestorage,serviceworkers,etc.",
        {"types": {"type": "string"}, "origin": {"type": "string"}}),

    # RESOURCES (the file inspector)
    ("listresources",     "List every resource loaded by the page (HTML, CSS, JS, images, fonts, everything). Returns url, type, mime, size, frameid.", {}),
    ("getresource",       "Fetch the content of one resource by URL. Falls back to fetch() if not cached.",
        {"url": {"type": "string", "required": True}, "frameid": {"type": "string"}}),
    ("saveallresources",  "Download every loaded resource to a folder. Returns the folder path and a manifest.", {}),
    ("listscripts",       "List all parsed JS scripts (scriptid, url, hash, length). Use scriptid with editjs.", {}),
    ("editcss",           "Live-edit CSS. Pass either stylesheetid+content or url+content. Falls back to injecting a style override.",
        {"stylesheetid": {"type": "string"}, "url": {"type": "string"}, "content": {"type": "string", "required": True}}),
    ("editjs",            "Live-patch a JS source by scriptid. Get the id from listscripts.",
        {"scriptid": {"type": "string", "required": True}, "content": {"type": "string", "required": True}}),

    # FILE OVERRIDES (DevTools Local Overrides)
    ("overridecreate",    "Intercept requests matching a URL pattern (glob like *example.com/api*) and serve local content instead. Pass either localpath or content. Survives navigation.",
        {"urlpattern": {"type": "string", "required": True}, "localpath": {"type": "string"}, "content": {"type": "string"}, "mimetype": {"type": "string"}, "status": {"type": "integer"}}),
    ("overridelist",      "List active overrides.", {}),
    ("overrideremove",    "Remove an override by id.", {"id": {"type": "string", "required": True}}),
    ("overrideclear",     "Remove all overrides.", {}),

    # PAGE
    ("navigate",          "Point the active tab at a URL.", {"url": {"type": "string", "required": True}}),
    ("reload",            "Reload the page. Pass hard=true to bypass cache.", {"hard": {"type": "boolean"}}),
    ("goback",            "Browser back.", {}),
    ("goforward",         "Browser forward.", {}),
    ("geturl",            "Current URL.", {}),
    ("gettitle",          "Current page title.", {}),
    ("printtopdf",        "Render the page to a PDF file.", {"landscape": {"type": "boolean"}, "background": {"type": "boolean"}}),
    ("savehtml",          "Save the current page's HTML to a file.", {}),

    # TABS
    ("listtabs",          "List all open tabs with id, title, url.", {}),
    ("newtab",            "Open a new tab.", {"url": {"type": "string"}}),
    ("closetab",          "Close a tab by id.", {"targetid": {"type": "string", "required": True}}),
    ("focustab",          "Bring a tab to the front.", {"targetid": {"type": "string", "required": True}}),

    # EMULATION
    ("setuseragent",         "Override the User-Agent string.", {"useragent": {"type": "string", "required": True}}),
    ("setgeolocation",       "Fake the user's GPS coords. Sites that ask for location will get this.",
        {"latitude": {"type": "number", "required": True}, "longitude": {"type": "number", "required": True}, "accuracy": {"type": "number"}}),
    ("cleargeolocation",     "Stop spoofing location.", {}),
    ("setviewport",          "Override viewport dimensions.",
        {"width": {"type": "integer", "required": True}, "height": {"type": "integer", "required": True}, "dpr": {"type": "number"}, "mobile": {"type": "boolean"}}),
    ("setdevice",            "Apply a device preset: iphone15, iphonese, ipad, pixel8, galaxys23, desktop.",
        {"preset": {"type": "string", "required": True}}),
    ("setnetworkconditions", "Throttle network. offline=true kills connectivity. latencyms, download/uploadbytespersec for throttling.",
        {"offline": {"type": "boolean"}, "latencyms": {"type": "integer"}, "downloadbytespersec": {"type": "integer"}, "uploadbytespersec": {"type": "integer"}}),
    ("clearnetworkconditions", "Reset network throttling.", {}),
    ("setcputhrottle",       "Slow down JS execution. rate=4 means 4x slower.", {"rate": {"type": "number", "required": True}}),
    ("setdarkmode",          "Force prefers-color-scheme. dark=true/false.", {"dark": {"type": "boolean"}}),
    ("settimezone",          "Override the browser's timezone (e.g. Asia/Tokyo).", {"timezone": {"type": "string", "required": True}}),
    ("setlocale",            "Override the browser's locale (e.g. fr-FR).", {"locale": {"type": "string", "required": True}}),

    # INTERACTION
    ("click",             "Click an element by CSS selector. Scrolls into view first.", {"selector": {"type": "string", "required": True}}),
    ("typetext",          "Type into an element. Uses native input setter so React/Vue notice.",
        {"selector": {"type": "string", "required": True}, "text": {"type": "string", "required": True}}),
    ("scroll",            "Scroll. Either pass a selector to scroll-into-view, or deltax/deltay for relative scroll.",
        {"selector": {"type": "string"}, "deltax": {"type": "integer"}, "deltay": {"type": "integer"}}),
    ("hover",             "Fire mouseover/enter/move on an element.", {"selector": {"type": "string", "required": True}}),
    ("keypress",          "Dispatch a key (e.g. Enter, Escape, ArrowDown).", {"key": {"type": "string", "required": True}}),
    ("focus",             "Focus an element.", {"selector": {"type": "string", "required": True}}),

    # DEBUGGER
    ("pause",             "Pause JS execution.", {}),
    ("resume",            "Resume.", {}),
    ("stepover",          "Debugger step over.", {}),
    ("stepinto",          "Debugger step into.", {}),
    ("stepout",           "Debugger step out.", {}),
    ("setbreakpoint",     "Set a breakpoint by URL+line. Optionally with a condition.",
        {"line": {"type": "integer", "required": True}, "url": {"type": "string"}, "urlregex": {"type": "string"}, "column": {"type": "integer"}, "condition": {"type": "string"}}),

    # PERFORMANCE
    ("getmetrics",        "Performance metrics: timestamps, frame counts, JS heap size, layout count, etc.", {}),
    ("coveragestart",     "Start JS/CSS coverage tracking.", {}),
    ("coveragestop",      "Stop coverage. Returns scripts and CSS rules that were/weren't used.", {}),
    ("heapsnapshot",      "Take a V8 heap snapshot, save to .heapsnapshot.", {}),

    # SECURITY
    ("securitystate",        "Page security info: protocol, isSecureContext, crossOriginIsolated.", {}),
    ("listserviceworkers",   "List registered service workers.", {}),
    ("unregisterserviceworker", "Unregister a service worker by scope URL.", {"scope": {"type": "string", "required": True}}),

    # SEARCH
    ("searchinresources", "Grep across every loaded resource (HTML/CSS/JS/etc) on the page. Supports regex.",
        {"query": {"type": "string", "required": True}, "regex": {"type": "boolean"}, "casesensitive": {"type": "boolean"}}),
    ("recentevents",      "Read from the SQLite ring buffer of events (last 10 min). Filter by kind: console, network, navigation, frame.",
        {"kind": {"type": "string"}, "limit": {"type": "integer"}, "since": {"type": "number"}}),

    # META
    ("status",            "Daemon + browser health, counts of captured data.", {}),
]


def schema_for_props(props):
    """Convert our shorthand property dicts into JSON schema."""
    schema = {"type": "object", "properties": {}, "additionalProperties": True}
    required = []
    for name, spec in props.items():
        s = {k: v for k, v in spec.items() if k != "required"}
        if not s:
            s = {"type": "string"}
        schema["properties"][name] = s
        if spec.get("required"):
            required.append(name)
    # Always allow targetid — every handler accepts it
    schema["properties"]["targetid"] = {
        "type": "string",
        "description": "Optional tab id. Defaults to most recent page tab.",
    }
    if required:
        schema["required"] = required
    return schema


server = Server("browser-eyes")


@server.list_tools()
async def list_tools():
    out = []
    for name, desc, props in TOOLS:
        out.append(Tool(name=name, description=desc, inputSchema=schema_for_props(props)))
    return out


@server.call_tool()
async def call_tool(name, arguments):
    arguments = arguments or {}

    # Special-case seescreen — returns an image
    if name == "seescreen":
        r = await call_daemon("latestframe")
        if not r["ok"]:
            return [text(f"error: {r['error']}")]
        frame = r["data"].get("frame") if r.get("data") else None
        if not frame:
            return [text("no frame captured yet — daemon just started, or screenshot tool not installed")]
        path = Path(frame["path"])
        if not path.exists():
            return [text(f"frame missing on disk: {path}")]
        b64 = base64.b64encode(path.read_bytes()).decode()
        return [ImageContent(type="image", data=b64, mimeType="image/png")]

    # Special-case tab/element/full-page screenshots — also return images
    if name in ("screenshottab", "screenshotfullpage", "screenshotelement"):
        r = await call_daemon(name, **arguments)
        if not r["ok"]:
            return [text(f"error: {r['error']}")]
        path = Path(r["data"]["path"])
        if not path.exists():
            return [text(f"capture saved but file missing: {path}")]
        b64 = base64.b64encode(path.read_bytes()).decode()
        mime = "image/jpeg" if path.suffix == ".jpg" else "image/png"
        return [
            ImageContent(type="image", data=b64, mimeType=mime),
            TextContent(type="text", text=f"saved: {path} ({path.stat().st_size} bytes)"),
        ]

    # Everything else: forward to daemon and return JSON text
    r = await call_daemon(name, **arguments)
    return [jsontext(r)]


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
