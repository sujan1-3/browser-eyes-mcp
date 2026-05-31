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
        return {"ok": False,
                "error": f"daemon not running. Start it: browser-eyes start"}
    try:
        reader, writer = await asyncio.open_unix_connection(str(SOCK_PATH))
    except (FileNotFoundError, ConnectionRefusedError) as e:
        return {"ok": False, "error": f"daemon unreachable: {e}"}

    req = {"op": op, **kwargs}
    writer.write((json.dumps(req) + "\n").encode())
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
    return json.loads(data.decode().split("\n", 1)[0])


def text(s):
    return [TextContent(type="text", text=s)]


def json_text(d):
    return [TextContent(type="text", text=json.dumps(d, indent=2, default=str))]


# ---------- Tool definitions ----------
# (name, description, schema)
TOOLS = [
    # ===== VISION =====
    ("see_screen",
     "Latest screenshot of the user's whole desktop from the ring buffer. "
     "Use when the user says 'look at my screen' or visual context matters.",
     {}),
    ("screenshot_tab",
     "Screenshot just the active browser tab's viewport (cleaner than the "
     "full desktop). Returns the saved file path.",
     {"format": {"type": "string", "enum": ["png", "jpeg"]},
      "quality": {"type": "integer"}}),
    ("screenshot_full_page",
     "Capture the entire scrollable page, not just the viewport. Great for "
     "documentation or long pages.", {}),
    ("screenshot_element",
     "Screenshot a single element matched by CSS selector.",
     {"selector": {"type": "string", "required": True}}),

    # ===== DOM =====
    ("get_dom",
     "Full HTML of the active tab. Truncates after 200k chars.", {}),
    ("query",
     "Find elements by CSS selector. Returns tag, id, classes, text, "
     "attributes, and outerHTML for each match.",
     {"selector": {"type": "string", "required": True},
      "limit": {"type": "integer"}}),
    ("set_html",
     "Replace an element's outerHTML. Live DOM edit.",
     {"selector": {"type": "string", "required": True},
      "html": {"type": "string", "required": True}}),
    ("set_attribute",
     "Set an attribute on an element.",
     {"selector": {"type": "string", "required": True},
      "name": {"type": "string", "required": True},
      "value": {"type": "string", "required": True}}),
    ("remove_element",
     "Remove an element from the DOM.",
     {"selector": {"type": "string", "required": True}}),
    ("accessibility_tree",
     "Full accessibility (AX) tree of the page. Use for understanding "
     "semantic structure.", {}),
    ("inspect_at_point",
     "Get element info at viewport coordinates (x, y). Returns tag, "
     "classes, text, and a best-guess selector.",
     {"x": {"type": "number", "required": True},
      "y": {"type": "number", "required": True}}),

    # ===== CONSOLE / JS =====
    ("run_js",
     "Execute JavaScript in the active tab. Awaits promises. Use for "
     "extracting data the DOM doesn't show, testing behavior, monkey-"
     "patching functions, anything.",
     {"code": {"type": "string", "required": True},
      "await": {"type": "boolean"}}),
    ("console_history",
     "Rolling console log captured from the page (log, info, warn, error, "
     "thrown exceptions). Filter by level.",
     {"level": {"type": "string"},
      "limit": {"type": "integer"}}),
    ("clear_console", "Clear the captured console log.", {}),

    # ===== NETWORK =====
    ("list_requests",
     "Filter and list captured network requests. Filters: url_contains, "
     "method, status, type (Document/Stylesheet/Script/XHR/Fetch/Image/etc).",
     {"url_contains": {"type": "string"},
      "method": {"type": "string"},
      "status": {"type": "integer"},
      "type": {"type": "string"},
      "limit": {"type": "integer"}}),
    ("request_details",
     "Full request + response metadata for one request id (from list_requests).",
     {"id": {"type": "string", "required": True}}),
    ("response_body",
     "Fetch the response body of a captured request. Returns text or "
     "saves binary to disk.",
     {"id": {"type": "string", "required": True}}),
    ("get_curl",
     "Reconstruct a curl command for replaying a captured request.",
     {"id": {"type": "string", "required": True}}),
    ("export_har",
     "Export the network log as a HAR file (importable into DevTools, "
     "Charles, etc).", {}),
    ("block_url",
     "Block all requests matching a URL pattern (glob-style).",
     {"pattern": {"type": "string", "required": True}}),
    ("unblock_url", "Remove a URL block.",
     {"pattern": {"type": "string", "required": True}}),
    ("list_blocks", "List currently-blocked URL patterns.", {}),
    ("set_extra_headers",
     "Inject extra HTTP headers into every outgoing request from the tab. "
     "Useful for testing auth bypasses, language headers, etc.",
     {"headers": {"type": "object", "required": True}}),

    # ===== COOKIES =====
    ("cookies_list",
     "List cookies, optionally filtered by domain substring.",
     {"domain": {"type": "string"}}),
    ("cookies_set",
     "Set one or more cookies. Pass 'cookies' as an array of cookie "
     "objects, each with name, value, domain, path, etc.",
     {"cookies": {"type": "array"}}),
    ("cookies_delete",
     "Delete a single cookie by name (+ optional url or domain).",
     {"name": {"type": "string", "required": True},
      "url": {"type": "string"},
      "domain": {"type": "string"}}),
    ("cookies_delete_all", "Wipe ALL cookies (browser-wide). Destructive.", {}),

    # ===== LOCAL / SESSION STORAGE / INDEXEDDB =====
    ("local_storage_list",
     "List localStorage entries for the tab's origin (or a given origin).",
     {"origin": {"type": "string"}}),
    ("local_storage_set",
     "Set a localStorage key.",
     {"key": {"type": "string", "required": True},
      "value": {"type": "string", "required": True},
      "origin": {"type": "string"}}),
    ("local_storage_delete",
     "Delete a localStorage key.",
     {"key": {"type": "string", "required": True},
      "origin": {"type": "string"}}),
    ("local_storage_clear",
     "Clear all localStorage for an origin.",
     {"origin": {"type": "string"}}),
    ("session_storage_list",
     "List sessionStorage entries for an origin.",
     {"origin": {"type": "string"}}),
    ("session_storage_set",
     "Set a sessionStorage key.",
     {"key": {"type": "string", "required": True},
      "value": {"type": "string", "required": True},
      "origin": {"type": "string"}}),
    ("indexeddb_list",
     "List IndexedDB database names for an origin.",
     {"origin": {"type": "string"}}),
    ("clear_browser_data",
     "Wipe storage. Types is a comma list: cookies,local_storage,"
     "session_storage,indexeddb,cache_storage,service_workers,etc.",
     {"types": {"type": "string"}, "origin": {"type": "string"}}),

    # ===== RESOURCES (the file inspector) =====
    ("list_resources",
     "List every resource loaded by the page: HTML, CSS, JS, images, "
     "fonts, everything. Returns url, type, mime, size, frame_id.", {}),
    ("get_resource",
     "Fetch the content of one resource by URL. Falls back to fetch() "
     "if not cached.",
     {"url": {"type": "string", "required": True},
      "frame_id": {"type": "string"}}),
    ("save_all_resources",
     "Download every loaded resource to a folder. Returns the folder "
     "path and a manifest.", {}),
    ("list_scripts",
     "List all parsed JS scripts (script_id, url, hash, length). Use "
     "script_id with edit_js.", {}),
    ("edit_css",
     "Live-edit CSS. Pass either stylesheet_id+content or url+content. "
     "Falls back to injecting a <style> override.",
     {"stylesheet_id": {"type": "string"},
      "url": {"type": "string"},
      "content": {"type": "string", "required": True}}),
    ("edit_js",
     "Live-patch a JS source by script_id. Get the id from list_scripts.",
     {"script_id": {"type": "string", "required": True},
      "content": {"type": "string", "required": True}}),

    # ===== FILE OVERRIDES (DevTools 'Local Overrides') =====
    ("override_create",
     "Intercept requests matching a URL pattern (glob like "
     "'*example.com/api/*') and serve local content instead. Pass "
     "either local_path or content. Survives navigation.",
     {"url_pattern": {"type": "string", "required": True},
      "local_path": {"type": "string"},
      "content": {"type": "string"},
      "mime_type": {"type": "string"},
      "status": {"type": "integer"}}),
    ("override_list", "List active overrides.", {}),
    ("override_remove", "Remove an override by id.",
     {"id": {"type": "string", "required": True}}),
    ("override_clear", "Remove all overrides.", {}),

    # ===== PAGE =====
    ("navigate", "Point the active tab at a URL.",
     {"url": {"type": "string", "required": True}}),
    ("reload", "Reload the page. Pass hard=true to bypass cache.",
     {"hard": {"type": "boolean"}}),
    ("go_back", "Browser back.", {}),
    ("go_forward", "Browser forward.", {}),
    ("get_url", "Current URL.", {}),
    ("get_title", "Current page title.", {}),
    ("print_to_pdf",
     "Render the page to a PDF file.",
     {"landscape": {"type": "boolean"},
      "background": {"type": "boolean"}}),
    ("save_html", "Save the current page's HTML to a file.", {}),

    # ===== TABS =====
    ("list_tabs", "List all open tabs with id, title, url.", {}),
    ("new_tab", "Open a new tab.",
     {"url": {"type": "string"}}),
    ("close_tab", "Close a tab by id.",
     {"target_id": {"type": "string", "required": True}}),
    ("focus_tab", "Bring a tab to the front.",
     {"target_id": {"type": "string", "required": True}}),

    # ===== EMULATION =====
    ("set_user_agent", "Override the User-Agent string.",
     {"user_agent": {"type": "string", "required": True}}),
    ("set_geolocation",
     "Fake the user's GPS coords. Sites that ask for location will get this.",
     {"latitude": {"type": "number", "required": True},
      "longitude": {"type": "number", "required": True},
      "accuracy": {"type": "number"}}),
    ("clear_geolocation", "Stop spoofing location.", {}),
    ("set_viewport", "Override viewport dimensions.",
     {"width": {"type": "integer", "required": True},
      "height": {"type": "integer", "required": True},
      "dpr": {"type": "number"},
      "mobile": {"type": "boolean"}}),
    ("set_device",
     "Apply a device preset: iphone15, iphone_se, ipad, pixel8, "
     "galaxy_s23, desktop.",
     {"preset": {"type": "string", "required": True}}),
    ("set_network_conditions",
     "Throttle network. offline=true kills connectivity. latency_ms, "
     "download/upload_bytes_per_sec for throttling.",
     {"offline": {"type": "boolean"},
      "latency_ms": {"type": "integer"},
      "download_bytes_per_sec": {"type": "integer"},
      "upload_bytes_per_sec": {"type": "integer"}}),
    ("clear_network_conditions", "Reset network throttling.", {}),
    ("set_cpu_throttle",
     "Slow down JS execution. rate=4 means 4x slower.",
     {"rate": {"type": "number", "required": True}}),
    ("set_dark_mode",
     "Force prefers-color-scheme. dark=true|false.",
     {"dark": {"type": "boolean"}}),
    ("set_timezone", "Override the browser's timezone (e.g. 'Asia/Tokyo').",
     {"timezone": {"type": "string", "required": True}}),
    ("set_locale", "Override the browser's locale (e.g. 'fr-FR').",
     {"locale": {"type": "string", "required": True}}),

    # ===== INTERACTION =====
    ("click", "Click an element by CSS selector. Scrolls into view first.",
     {"selector": {"type": "string", "required": True}}),
    ("type_text",
     "Type into an element. Uses native input setter so React/Vue notice.",
     {"selector": {"type": "string", "required": True},
      "text": {"type": "string", "required": True}}),
    ("scroll",
     "Scroll. Either pass a selector to scroll-into-view, or delta_x/"
     "delta_y for relative scroll.",
     {"selector": {"type": "string"},
      "delta_x": {"type": "integer"},
      "delta_y": {"type": "integer"}}),
    ("hover", "Fire mouseover/enter/move on an element.",
     {"selector": {"type": "string", "required": True}}),
    ("key_press", "Dispatch a key (e.g. 'Enter', 'Escape', 'ArrowDown').",
     {"key": {"type": "string", "required": True}}),
    ("focus", "Focus an element.",
     {"selector": {"type": "string", "required": True}}),

    # ===== DEBUGGER =====
    ("pause", "Pause JS execution.", {}),
    ("resume", "Resume.", {}),
    ("step_over", "Debugger step over.", {}),
    ("step_into", "Debugger step into.", {}),
    ("step_out", "Debugger step out.", {}),
    ("set_breakpoint",
     "Set a breakpoint by URL/line. Optionally with a condition.",
     {"line": {"type": "integer", "required": True},
      "url": {"type": "string"},
      "url_regex": {"type": "string"},
      "column": {"type": "integer"},
      "condition": {"type": "string"}}),

    # ===== PERFORMANCE =====
    ("get_metrics",
     "Performance metrics: timestamps, frame counts, JS heap size, "
     "layout count, etc.", {}),
    ("coverage_start", "Start JS+CSS coverage tracking.", {}),
    ("coverage_stop",
     "Stop coverage. Returns scripts and CSS rules that were/weren't used.",
     {}),
    ("heap_snapshot", "Take a V8 heap snapshot, save to .heapsnapshot.", {}),

    # ===== SECURITY =====
    ("security_state",
     "Page security info: protocol, isSecureContext, crossOriginIsolated.",
     {}),
    ("list_service_workers", "List registered service workers.", {}),
    ("unregister_service_worker",
     "Unregister a service worker by scope URL.",
     {"scope": {"type": "string", "required": True}}),

    # ===== SEARCH =====
    ("search_in_resources",
     "Grep across every loaded resource (HTML/CSS/JS/etc) on the page. "
     "Supports regex.",
     {"query": {"type": "string", "required": True},
      "regex": {"type": "boolean"},
      "case_sensitive": {"type": "boolean"}}),
    ("recent_events",
     "Read from the SQLite ring buffer of events (last 10 min). Filter "
     "by kind: console, network, navigation, frame.",
     {"kind": {"type": "string"},
      "limit": {"type": "integer"},
      "since": {"type": "number"}}),

    # ===== META =====
    ("status", "Daemon + browser health, counts of captured data.", {}),
]


def schema_for(props):
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
    # Always allow target_id (every handler accepts it)
    schema["properties"]["target_id"] = {
        "type": "string",
        "description": "Optional tab id. Defaults to most recent page tab.",
    }
    if required:
        schema["required"] = required
    return schema


# ---------- MCP server ----------
server = Server("browser-eyes")


@server.list_tools()
async def list_tools():
    out = []
    for name, desc, props in TOOLS:
        out.append(Tool(
            name=name,
            description=desc,
            inputSchema=schema_for(props),
        ))
    return out


@server.call_tool()
async def call_tool(name, arguments):
    arguments = arguments or {}

    # Special-case: see_screen returns an image
    if name == "see_screen":
        r = await call_daemon("latest_frame")
        if not r["ok"]:
            return text(f"error: {r['error']}")
        frame = r["data"].get("frame") if r.get("data") else None
        if not frame:
            return text("no frame captured yet (daemon just started, "
                        "or screenshot tool not installed)")
        path = Path(frame["path"])
        if not path.exists():
            return text(f"frame missing on disk: {path}")
        b64 = base64.b64encode(path.read_bytes()).decode()
        return [ImageContent(type="image", data=b64, mimeType="image/png")]

    # Special-case: tab/element/full-page screenshots also return images
    if name in ("screenshot_tab", "screenshot_full_page",
                "screenshot_element"):
        r = await call_daemon(name, **arguments)
        if not r["ok"]:
            return text(f"error: {r['error']}")
        path = Path(r["data"]["path"])
        if not path.exists():
            return text(f"capture saved but file missing: {path}")
        b64 = base64.b64encode(path.read_bytes()).decode()
        mime = "image/jpeg" if path.suffix == ".jpg" else "image/png"
        return [
            ImageContent(type="image", data=b64, mimeType=mime),
            TextContent(type="text",
                        text=f"saved: {path} ({path.stat().st_size} bytes)"),
        ]

    # Everything else: forward to daemon and return JSON text
    r = await call_daemon(name, **arguments)
    return json_text(r)


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
