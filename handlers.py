"""
All operation handlers for browser-eyes.

Each handler returns a JSON-serializable dict. The daemon's unix socket
server dispatches on op name. The MCP server wraps these one-to-one as tools.

Conventions:
  - target_id parameter is optional everywhere; if missing we use the
    first page target.
  - everything async, returns native dicts (not strings).
  - failures raise; the socket layer turns them into {"ok": false}.
"""

import asyncio
import base64
import hashlib
import json
import sqlite3
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlparse

from cdp import CDPClient, CDPError


# ============================================================
# State the handlers need
# ============================================================
class HandlerState:
    """Bag of state passed to every handler."""
    def __init__(self, cdp: CDPClient, db_path: Path, state_dir: Path):
        self.cdp = cdp
        self.db_path = db_path
        self.state_dir = state_dir
        self.frame_dir = state_dir / "frames"
        self.overrides_dir = state_dir / "overrides"
        self.exports_dir = state_dir / "exports"
        for d in (self.frame_dir, self.overrides_dir, self.exports_dir):
            d.mkdir(parents=True, exist_ok=True)

        # In-memory state
        self.network_log = {}   # request_id -> {request, response, body}
        self.console_log = []   # rolling list of console entries
        self.overrides = {}     # override_id -> {pattern, path_or_content, ...}
        self.blocked_urls = []  # list of URL patterns currently blocked
        self.coverage_active = False

    def db(self):
        return sqlite3.connect(self.db_path)


# ============================================================
# Helper: resolve target_id
# ============================================================
async def _tid(state, args):
    return args.get("target_id") or await state.cdp.first_page_target()


# ============================================================
# VISION
# ============================================================
async def op_screenshot_tab(state, args):
    """Screenshot the viewport of a tab (just the page, not the desktop)."""
    tid = await _tid(state, args)
    fmt = args.get("format", "png")
    quality = args.get("quality", 80)
    params = {"format": fmt}
    if fmt == "jpeg":
        params["quality"] = quality
    r = await state.cdp.target_call(tid, "Page.captureScreenshot", params)
    out = state.exports_dir / f"tab_{int(time.time()*1000)}.{fmt}"
    out.write_bytes(base64.b64decode(r["data"]))
    return {"path": str(out), "size": out.stat().st_size}


async def op_screenshot_full_page(state, args):
    """Capture the entire scrollable page, not just the viewport."""
    tid = await _tid(state, args)
    # Get full document size
    layout = await state.cdp.target_call(tid, "Page.getLayoutMetrics")
    content = layout["contentSize"]
    r = await state.cdp.target_call(tid, "Page.captureScreenshot", {
        "format": "png",
        "captureBeyondViewport": True,
        "clip": {
            "x": 0, "y": 0,
            "width": content["width"],
            "height": content["height"],
            "scale": 1,
        },
    })
    out = state.exports_dir / f"fullpage_{int(time.time()*1000)}.png"
    out.write_bytes(base64.b64decode(r["data"]))
    return {"path": str(out), "size": out.stat().st_size,
            "dimensions": {"width": content["width"],
                           "height": content["height"]}}


async def op_screenshot_element(state, args):
    """Screenshot a single element via selector."""
    tid = await _tid(state, args)
    sel = args["selector"]
    # Get bounding box via JS
    box = await state.cdp.target_call(tid, "Runtime.evaluate", {
        "expression": f"""
            (() => {{
                const el = document.querySelector({json.dumps(sel)});
                if (!el) return null;
                const r = el.getBoundingClientRect();
                return {{x: r.x, y: r.y, width: r.width, height: r.height}};
            }})()
        """,
        "returnByValue": True,
    })
    val = box.get("result", {}).get("value")
    if not val:
        raise CDPError(f"element not found: {sel}")
    r = await state.cdp.target_call(tid, "Page.captureScreenshot", {
        "format": "png",
        "clip": {**val, "scale": 1},
    })
    out = state.exports_dir / f"element_{int(time.time()*1000)}.png"
    out.write_bytes(base64.b64decode(r["data"]))
    return {"path": str(out), "size": out.stat().st_size, "box": val}


# ============================================================
# DOM
# ============================================================
async def op_get_dom(state, args):
    tid = await _tid(state, args)
    r = await state.cdp.target_call(tid, "Runtime.evaluate", {
        "expression": "document.documentElement.outerHTML",
        "returnByValue": True,
    })
    return {"html": r.get("result", {}).get("value", "")}


async def op_query(state, args):
    """Return outerHTML of every element matching the selector."""
    tid = await _tid(state, args)
    sel = args["selector"]
    limit = args.get("limit", 20)
    r = await state.cdp.target_call(tid, "Runtime.evaluate", {
        "expression": f"""
            Array.from(document.querySelectorAll({json.dumps(sel)}))
                .slice(0, {limit})
                .map(el => ({{
                    tag: el.tagName.toLowerCase(),
                    id: el.id,
                    classes: el.className,
                    text: el.textContent.slice(0, 200),
                    html: el.outerHTML.slice(0, 2000),
                    attrs: Object.fromEntries(
                        Array.from(el.attributes).map(a => [a.name, a.value])
                    ),
                }}))
        """,
        "returnByValue": True,
    })
    return {"matches": r.get("result", {}).get("value", [])}


async def op_set_html(state, args):
    tid = await _tid(state, args)
    sel = args["selector"]
    html = args["html"]
    r = await state.cdp.target_call(tid, "Runtime.evaluate", {
        "expression": f"""
            (() => {{
                const el = document.querySelector({json.dumps(sel)});
                if (!el) return 'not_found';
                el.outerHTML = {json.dumps(html)};
                return 'ok';
            }})()
        """,
        "returnByValue": True,
    })
    return {"result": r.get("result", {}).get("value")}


async def op_set_attribute(state, args):
    tid = await _tid(state, args)
    r = await state.cdp.target_call(tid, "Runtime.evaluate", {
        "expression": f"""
            (() => {{
                const el = document.querySelector({json.dumps(args['selector'])});
                if (!el) return 'not_found';
                el.setAttribute({json.dumps(args['name'])}, {json.dumps(args['value'])});
                return 'ok';
            }})()
        """,
        "returnByValue": True,
    })
    return {"result": r.get("result", {}).get("value")}


async def op_remove_element(state, args):
    tid = await _tid(state, args)
    r = await state.cdp.target_call(tid, "Runtime.evaluate", {
        "expression": f"""
            (() => {{
                const el = document.querySelector({json.dumps(args['selector'])});
                if (!el) return 'not_found';
                el.remove();
                return 'ok';
            }})()
        """,
        "returnByValue": True,
    })
    return {"result": r.get("result", {}).get("value")}


async def op_get_accessibility_tree(state, args):
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "Accessibility.enable")
    r = await state.cdp.target_call(tid, "Accessibility.getFullAXTree")
    return r


async def op_inspect_at_point(state, args):
    """Get element info at viewport coordinates."""
    tid = await _tid(state, args)
    r = await state.cdp.target_call(tid, "Runtime.evaluate", {
        "expression": f"""
            (() => {{
                const el = document.elementFromPoint({args['x']}, {args['y']});
                if (!el) return null;
                return {{
                    tag: el.tagName.toLowerCase(),
                    id: el.id, classes: el.className,
                    text: el.textContent.slice(0, 200),
                    html: el.outerHTML.slice(0, 2000),
                    selector: (() => {{
                        if (el.id) return '#' + el.id;
                        let path = [];
                        let cur = el;
                        while (cur && cur.nodeType === 1 && path.length < 5) {{
                            let sel = cur.tagName.toLowerCase();
                            if (cur.className) sel += '.' + cur.className.trim().split(/\\s+/).join('.');
                            path.unshift(sel);
                            cur = cur.parentElement;
                        }}
                        return path.join(' > ');
                    }})(),
                }};
            }})()
        """,
        "returnByValue": True,
    })
    return r.get("result", {}).get("value")


# ============================================================
# CONSOLE
# ============================================================
async def op_run_js(state, args):
    tid = await _tid(state, args)
    r = await state.cdp.target_call(tid, "Runtime.evaluate", {
        "expression": args["code"],
        "returnByValue": True,
        "awaitPromise": args.get("await", True),
        "userGesture": True,
    })
    return r


async def op_console_history(state, args):
    """Return the rolling console log captured by the event subscriber."""
    level = args.get("level")
    limit = args.get("limit", 100)
    entries = state.console_log[-1000:]
    if level:
        entries = [e for e in entries if e.get("level") == level]
    return {"entries": entries[-limit:]}


async def op_clear_console(state, args):
    state.console_log.clear()
    tid = await _tid(state, args)
    with suppress(CDPError):
        await state.cdp.target_call(tid, "Runtime.discardConsoleEntries")
    return {"cleared": True}


# ============================================================
# NETWORK
# ============================================================
async def op_list_requests(state, args):
    """Filter the captured network log."""
    filt_url = args.get("url_contains")
    filt_method = args.get("method")
    filt_status = args.get("status")
    filt_type = args.get("type")
    limit = args.get("limit", 100)

    results = []
    for rid, entry in state.network_log.items():
        req = entry.get("request", {})
        rsp = entry.get("response", {})
        if filt_url and filt_url not in req.get("url", ""):
            continue
        if filt_method and req.get("method") != filt_method.upper():
            continue
        if filt_status and rsp.get("status") != int(filt_status):
            continue
        if filt_type and entry.get("type") != filt_type:
            continue
        results.append({
            "id": rid,
            "method": req.get("method"),
            "url": req.get("url"),
            "status": rsp.get("status"),
            "type": entry.get("type"),
            "mime": rsp.get("mimeType"),
            "size": rsp.get("encodedDataLength"),
            "started": entry.get("started"),
        })
    results.sort(key=lambda x: x.get("started") or 0, reverse=True)
    return {"requests": results[:limit]}


async def op_get_request_details(state, args):
    entry = state.network_log.get(args["id"])
    if not entry:
        raise CDPError(f"no request with id {args['id']}")
    return entry


async def op_get_response_body(state, args):
    rid = args["id"]
    entry = state.network_log.get(rid)
    if not entry:
        raise CDPError(f"no request with id {rid}")
    tid = entry.get("target_id") or await state.cdp.first_page_target()
    r = await state.cdp.target_call(tid, "Network.getResponseBody",
                                    {"requestId": rid})
    body = r.get("body", "")
    if r.get("base64Encoded"):
        # save binary to file
        ext = (entry.get("response", {}).get("mimeType", "")
               .split("/")[-1] or "bin")
        out = state.exports_dir / f"body_{rid}.{ext}"
        out.write_bytes(base64.b64decode(body))
        return {"binary": True, "path": str(out),
                "size": out.stat().st_size}
    return {"binary": False, "body": body}


async def op_get_curl(state, args):
    entry = state.network_log.get(args["id"])
    if not entry:
        raise CDPError(f"no request with id {args['id']}")
    req = entry["request"]
    parts = ["curl", "-X", req.get("method", "GET")]
    for k, v in (req.get("headers") or {}).items():
        parts += ["-H", f"{k}: {v}"]
    if req.get("postData"):
        parts += ["--data-raw", req["postData"]]
    parts.append(req["url"])
    # naive shell escape
    quoted = " ".join(
        p if p.startswith("-") or p in ("curl",)
        else "'" + p.replace("'", "'\\''") + "'"
        for p in parts
    )
    return {"curl": quoted}


async def op_export_har(state, args):
    """Build a minimal HAR file from the captured log."""
    entries = []
    for rid, e in state.network_log.items():
        req = e.get("request", {})
        rsp = e.get("response", {})
        entries.append({
            "startedDateTime": time.strftime(
                "%Y-%m-%dT%H:%M:%S.000Z",
                time.gmtime(e.get("started") or 0)
            ),
            "time": (e.get("ended", 0) - e.get("started", 0)) * 1000,
            "request": {
                "method": req.get("method", ""),
                "url": req.get("url", ""),
                "httpVersion": "HTTP/1.1",
                "headers": [{"name": k, "value": str(v)}
                            for k, v in (req.get("headers") or {}).items()],
                "queryString": [],
                "cookies": [],
                "headersSize": -1, "bodySize": -1,
                "postData": ({"mimeType": "", "text": req["postData"]}
                             if req.get("postData") else None),
            },
            "response": {
                "status": rsp.get("status", 0),
                "statusText": rsp.get("statusText", ""),
                "httpVersion": "HTTP/1.1",
                "headers": [{"name": k, "value": str(v)}
                            for k, v in (rsp.get("headers") or {}).items()],
                "cookies": [],
                "content": {
                    "size": rsp.get("encodedDataLength", 0),
                    "mimeType": rsp.get("mimeType", ""),
                },
                "redirectURL": "",
                "headersSize": -1, "bodySize": -1,
            },
            "cache": {},
            "timings": {"send": 0, "wait": 0, "receive": 0},
        })
    har = {"log": {
        "version": "1.2",
        "creator": {"name": "browser-eyes", "version": "1.0"},
        "entries": entries,
    }}
    out = state.exports_dir / f"capture_{int(time.time())}.har"
    out.write_text(json.dumps(har, indent=2))
    return {"path": str(out), "entries": len(entries)}


async def op_block_url(state, args):
    tid = await _tid(state, args)
    pattern = args["pattern"]
    state.blocked_urls.append(pattern)
    await state.cdp.target_call(tid, "Network.setBlockedURLs",
                                {"urls": state.blocked_urls})
    return {"blocked": state.blocked_urls}


async def op_unblock_url(state, args):
    tid = await _tid(state, args)
    pattern = args["pattern"]
    state.blocked_urls = [u for u in state.blocked_urls if u != pattern]
    await state.cdp.target_call(tid, "Network.setBlockedURLs",
                                {"urls": state.blocked_urls})
    return {"blocked": state.blocked_urls}


async def op_list_blocks(state, args):
    return {"blocked": state.blocked_urls}


async def op_set_extra_headers(state, args):
    tid = await _tid(state, args)
    headers = args["headers"]
    await state.cdp.target_call(tid, "Network.setExtraHTTPHeaders",
                                {"headers": headers})
    return {"applied": headers}


# ============================================================
# STORAGE: COOKIES
# ============================================================
async def op_cookies_list(state, args):
    domain = args.get("domain")
    if domain:
        r = await state.cdp.browser_call("Storage.getCookies")
        cookies = [c for c in r.get("cookies", [])
                   if domain in c.get("domain", "")]
    else:
        r = await state.cdp.browser_call("Storage.getCookies")
        cookies = r.get("cookies", [])
    return {"cookies": cookies}


async def op_cookies_set(state, args):
    """Set one or more cookies. args['cookies'] = list of cookie dicts."""
    cookies = args.get("cookies") or [args]
    await state.cdp.browser_call("Storage.setCookies", {"cookies": cookies})
    return {"set": len(cookies)}


async def op_cookies_delete(state, args):
    await state.cdp.browser_call("Storage.clearCookies")
    return {"cleared": True}


async def op_cookies_delete_one(state, args):
    name = args["name"]
    url = args.get("url")
    domain = args.get("domain")
    params = {"name": name}
    if url:
        params["url"] = url
    if domain:
        params["domain"] = domain
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "Network.deleteCookies", params)
    return {"deleted": name}


# ============================================================
# STORAGE: localStorage / sessionStorage
# ============================================================
async def _origin_for_target(state, target_id):
    r = await state.cdp.target_call(target_id, "Runtime.evaluate", {
        "expression": "location.origin",
        "returnByValue": True,
    })
    return r.get("result", {}).get("value", "")


async def op_local_storage_list(state, args):
    tid = await _tid(state, args)
    origin = args.get("origin") or await _origin_for_target(state, tid)
    r = await state.cdp.target_call(tid, "DOMStorage.getDOMStorageItems", {
        "storageId": {"securityOrigin": origin, "isLocalStorage": True}
    })
    return {"origin": origin, "items": dict(r.get("entries", []))}


async def op_local_storage_set(state, args):
    tid = await _tid(state, args)
    origin = args.get("origin") or await _origin_for_target(state, tid)
    await state.cdp.target_call(tid, "DOMStorage.setDOMStorageItem", {
        "storageId": {"securityOrigin": origin, "isLocalStorage": True},
        "key": args["key"],
        "value": args["value"],
    })
    return {"set": args["key"]}


async def op_local_storage_delete(state, args):
    tid = await _tid(state, args)
    origin = args.get("origin") or await _origin_for_target(state, tid)
    await state.cdp.target_call(tid, "DOMStorage.removeDOMStorageItem", {
        "storageId": {"securityOrigin": origin, "isLocalStorage": True},
        "key": args["key"],
    })
    return {"deleted": args["key"]}


async def op_local_storage_clear(state, args):
    tid = await _tid(state, args)
    origin = args.get("origin") or await _origin_for_target(state, tid)
    await state.cdp.target_call(tid, "DOMStorage.clear", {
        "storageId": {"securityOrigin": origin, "isLocalStorage": True}
    })
    return {"cleared": origin}


async def op_session_storage_list(state, args):
    tid = await _tid(state, args)
    origin = args.get("origin") or await _origin_for_target(state, tid)
    r = await state.cdp.target_call(tid, "DOMStorage.getDOMStorageItems", {
        "storageId": {"securityOrigin": origin, "isLocalStorage": False}
    })
    return {"origin": origin, "items": dict(r.get("entries", []))}


async def op_session_storage_set(state, args):
    tid = await _tid(state, args)
    origin = args.get("origin") or await _origin_for_target(state, tid)
    await state.cdp.target_call(tid, "DOMStorage.setDOMStorageItem", {
        "storageId": {"securityOrigin": origin, "isLocalStorage": False},
        "key": args["key"], "value": args["value"],
    })
    return {"set": args["key"]}


async def op_indexeddb_list_databases(state, args):
    tid = await _tid(state, args)
    origin = args.get("origin") or await _origin_for_target(state, tid)
    r = await state.cdp.target_call(tid,
        "IndexedDB.requestDatabaseNames", {"securityOrigin": origin})
    return {"origin": origin, "databases": r.get("databaseNames", [])}


async def op_clear_browser_data(state, args):
    """Wipe whichever storage types you ask for."""
    types = args.get("types", "cookies,local_storage,session_storage,"
                              "indexeddb,cache_storage,service_workers")
    origin = args.get("origin")
    if origin:
        await state.cdp.browser_call("Storage.clearDataForOrigin", {
            "origin": origin, "storageTypes": types,
        })
    else:
        # clear for all origins by hitting the known list
        await state.cdp.browser_call("Network.clearBrowserCookies")
        await state.cdp.browser_call("Network.clearBrowserCache")
    return {"cleared": types}


# ============================================================
# RESOURCES (the file inspector)
# ============================================================
async def op_list_resources(state, args):
    """List every resource loaded by the current page."""
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "Page.enable")
    tree = await state.cdp.target_call(tid, "Page.getResourceTree")
    resources = []

    def walk(frame_node):
        for r in frame_node.get("resources", []):
            resources.append({
                "url": r.get("url"),
                "type": r.get("type"),
                "mime": r.get("mimeType"),
                "size": r.get("contentSize"),
                "frame_id": frame_node["frame"]["id"],
            })
        for child in frame_node.get("childFrames", []):
            walk(child)
    walk(tree["frameTree"])
    # Also include the top-level document
    top = tree["frameTree"]["frame"]
    resources.insert(0, {
        "url": top.get("url"), "type": "Document",
        "mime": top.get("mimeType"), "size": None,
        "frame_id": top["id"],
    })
    return {"resources": resources}


async def op_get_resource(state, args):
    """Fetch the content of one resource."""
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "Page.enable")
    tree = await state.cdp.target_call(tid, "Page.getResourceTree")
    frame_id = args.get("frame_id") or tree["frameTree"]["frame"]["id"]
    try:
        r = await state.cdp.target_call(tid, "Page.getResourceContent", {
            "frameId": frame_id, "url": args["url"],
        })
    except CDPError as e:
        # Some resources aren't cached — fetch via JS
        r = await state.cdp.target_call(tid, "Runtime.evaluate", {
            "expression": f"fetch({json.dumps(args['url'])}).then(r => r.text())",
            "returnByValue": True, "awaitPromise": True,
        })
        return {"content": r.get("result", {}).get("value", ""),
                "via": "fetch"}
    content = r.get("content", "")
    if r.get("base64Encoded"):
        return {"binary": True, "base64": content}
    return {"binary": False, "content": content}


async def op_save_all_resources(state, args):
    """Dump every resource of the current page into a folder."""
    tid = await _tid(state, args)
    lst = await op_list_resources(state, args)
    folder = state.exports_dir / f"site_{int(time.time())}"
    folder.mkdir(exist_ok=True)
    saved = []
    for res in lst["resources"]:
        try:
            content = await op_get_resource(state, {
                "target_id": tid, "url": res["url"],
                "frame_id": res["frame_id"],
            })
            url = res["url"]
            name = hashlib.sha1(url.encode()).hexdigest()[:12]
            # try to keep extension
            parsed = urlparse(url)
            ext = Path(parsed.path).suffix or ".bin"
            path = folder / f"{name}{ext}"
            if content.get("binary"):
                path.write_bytes(base64.b64decode(content["base64"]))
            else:
                path.write_text(content.get("content", ""),
                                encoding="utf-8", errors="replace")
            saved.append({"url": url, "path": str(path)})
        except Exception as e:
            saved.append({"url": res["url"], "error": str(e)})
    # write a manifest
    (folder / "manifest.json").write_text(json.dumps(saved, indent=2))
    return {"folder": str(folder), "count": len(saved)}


async def op_edit_css(state, args):
    """Live-edit a stylesheet. Pass either url+content or stylesheet_id+content."""
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "DOM.enable")
    await state.cdp.target_call(tid, "CSS.enable")

    ss_id = args.get("stylesheet_id")
    if not ss_id:
        # find by url
        url = args["url"]
        # listen for CSS.styleSheetAdded? Easier: use Page.getResourceTree
        # and CSS.getMatchedStylesForNode is overkill. Use CSS-internal:
        sheets = await state.cdp.target_call(tid, "CSS.getStyleSheetText", {})
        # Actually we need the headers — use the cached events
        # Fallback: inject a new <style> overriding
        ss_id = None
        # Query getMediaQueries / styleSheets via DOM
        sheets_r = await state.cdp.target_call(tid, "Runtime.evaluate", {
            "expression": """
                Array.from(document.styleSheets).map(s => s.href)
            """,
            "returnByValue": True,
        })
        # We can't get CDP stylesheet_id without listening to CSS.styleSheetAdded.
        # Best-effort: inject an override.
        await state.cdp.target_call(tid, "Runtime.evaluate", {
            "expression": f"""
                (() => {{
                    let s = document.getElementById('__browser_eyes_override__');
                    if (!s) {{
                        s = document.createElement('style');
                        s.id = '__browser_eyes_override__';
                        document.head.appendChild(s);
                    }}
                    s.textContent = {json.dumps(args['content'])};
                    return 'injected';
                }})()
            """,
            "returnByValue": True,
        })
        return {"method": "style_injection",
                "note": "couldn't resolve stylesheet id; "
                        "injected as <style> override"}
    await state.cdp.target_call(tid, "CSS.setStyleSheetText", {
        "styleSheetId": ss_id, "text": args["content"],
    })
    return {"method": "css.setStyleSheetText", "stylesheet_id": ss_id}


async def op_edit_js(state, args):
    """Live-patch a JS script via Debugger.setScriptSource."""
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "Debugger.enable")
    script_id = args.get("script_id")
    if not script_id:
        # Best we can do without listening to Debugger.scriptParsed is fail.
        return {"error": "script_id required. Use list_scripts first.",
                "hint": "Or use run_js to redefine the function inline."}
    r = await state.cdp.target_call(tid, "Debugger.setScriptSource", {
        "scriptId": script_id,
        "scriptSource": args["content"],
    })
    return r


async def op_list_scripts(state, args):
    """List parsed JS scripts. Requires Debugger.enable to have been called."""
    tid = await _tid(state, args)
    # We track scripts in state.network_log under separate key? Simpler: use Runtime.
    await state.cdp.target_call(tid, "Debugger.enable")
    # Debugger.scriptParsed events are buffered after enable. There's no
    # direct "list scripts" call — clients track from events. We'll have
    # daemon.py listen and store them.
    return {"scripts": state.__dict__.get("_scripts", [])}


# ============================================================
# FILE OVERRIDES (the killer feature)
# ============================================================
async def op_override_create(state, args):
    """Intercept requests matching pattern and serve local content."""
    tid = await _tid(state, args)
    pattern = args["url_pattern"]
    if "local_path" in args:
        body = Path(args["local_path"]).read_bytes()
    elif "content" in args:
        c = args["content"]
        body = c.encode() if isinstance(c, str) else c
    else:
        raise CDPError("need local_path or content")
    mime = args.get("mime_type", "text/plain")
    status = args.get("status", 200)
    override_id = hashlib.sha1(
        f"{pattern}{time.time()}".encode()
    ).hexdigest()[:12]

    # Save the override body to disk
    out = state.overrides_dir / override_id
    out.write_bytes(body)

    state.overrides[override_id] = {
        "id": override_id,
        "pattern": pattern,
        "mime": mime,
        "status": status,
        "body_path": str(out),
    }
    # Make sure Fetch interception is on
    await _refresh_fetch_interception(state, tid)
    return state.overrides[override_id]


async def op_override_list(state, args):
    return {"overrides": list(state.overrides.values())}


async def op_override_remove(state, args):
    oid = args["id"]
    o = state.overrides.pop(oid, None)
    if o:
        with suppress(FileNotFoundError):
            Path(o["body_path"]).unlink()
    tid = await _tid(state, args)
    await _refresh_fetch_interception(state, tid)
    return {"removed": oid}


async def op_override_clear(state, args):
    for oid, o in list(state.overrides.items()):
        with suppress(FileNotFoundError):
            Path(o["body_path"]).unlink()
    state.overrides.clear()
    tid = await _tid(state, args)
    await _refresh_fetch_interception(state, tid)
    return {"cleared": True}


async def _refresh_fetch_interception(state, target_id):
    """Configure Fetch domain based on current overrides."""
    if state.overrides:
        patterns = [{"urlPattern": o["pattern"]}
                    for o in state.overrides.values()]
        await state.cdp.target_call(target_id, "Fetch.enable",
                                    {"patterns": patterns})
    else:
        with suppress(CDPError):
            await state.cdp.target_call(target_id, "Fetch.disable")


# ============================================================
# PAGE
# ============================================================
async def op_navigate(state, args):
    tid = await _tid(state, args)
    r = await state.cdp.target_call(tid, "Page.navigate", {"url": args["url"]})
    return r


async def op_reload(state, args):
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "Page.reload",
                                {"ignoreCache": args.get("hard", False)})
    return {"reloaded": True}


async def op_go_back(state, args):
    tid = await _tid(state, args)
    h = await state.cdp.target_call(tid, "Page.getNavigationHistory")
    idx = h["currentIndex"]
    if idx <= 0:
        return {"error": "no back history"}
    await state.cdp.target_call(tid, "Page.navigateToHistoryEntry",
                                {"entryId": h["entries"][idx - 1]["id"]})
    return {"navigated_to": h["entries"][idx - 1]["url"]}


async def op_go_forward(state, args):
    tid = await _tid(state, args)
    h = await state.cdp.target_call(tid, "Page.getNavigationHistory")
    idx = h["currentIndex"]
    if idx >= len(h["entries"]) - 1:
        return {"error": "no forward history"}
    await state.cdp.target_call(tid, "Page.navigateToHistoryEntry",
                                {"entryId": h["entries"][idx + 1]["id"]})
    return {"navigated_to": h["entries"][idx + 1]["url"]}


async def op_get_url(state, args):
    tid = await _tid(state, args)
    r = await state.cdp.target_call(tid, "Runtime.evaluate", {
        "expression": "location.href", "returnByValue": True,
    })
    return {"url": r.get("result", {}).get("value")}


async def op_get_title(state, args):
    tid = await _tid(state, args)
    r = await state.cdp.target_call(tid, "Runtime.evaluate", {
        "expression": "document.title", "returnByValue": True,
    })
    return {"title": r.get("result", {}).get("value")}


async def op_print_to_pdf(state, args):
    tid = await _tid(state, args)
    r = await state.cdp.target_call(tid, "Page.printToPDF", {
        "landscape": args.get("landscape", False),
        "printBackground": args.get("background", True),
    })
    out = state.exports_dir / f"page_{int(time.time())}.pdf"
    out.write_bytes(base64.b64decode(r["data"]))
    return {"path": str(out), "size": out.stat().st_size}


async def op_save_html(state, args):
    tid = await _tid(state, args)
    r = await state.cdp.target_call(tid, "Runtime.evaluate", {
        "expression": "document.documentElement.outerHTML",
        "returnByValue": True,
    })
    html = r.get("result", {}).get("value", "")
    out = state.exports_dir / f"page_{int(time.time())}.html"
    out.write_text(html, encoding="utf-8")
    return {"path": str(out), "size": out.stat().st_size}


# ============================================================
# TABS
# ============================================================
async def op_list_tabs(state, args):
    targets = await state.cdp.list_targets()
    return {"tabs": [
        {"id": t["id"], "title": t.get("title"),
         "url": t.get("url"), "type": t.get("type")}
        for t in targets if t.get("type") == "page"
    ]}


async def op_new_tab(state, args):
    t = await state.cdp.new_tab(args.get("url", ""))
    return {"tab": t}


async def op_close_tab(state, args):
    await state.cdp.close_tab(args["target_id"])
    return {"closed": args["target_id"]}


async def op_focus_tab(state, args):
    await state.cdp.activate_tab(args["target_id"])
    return {"focused": args["target_id"]}


# ============================================================
# EMULATION
# ============================================================
async def op_set_user_agent(state, args):
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "Network.setUserAgentOverride", {
        "userAgent": args["user_agent"]
    })
    return {"ua": args["user_agent"]}


async def op_set_geolocation(state, args):
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "Emulation.setGeolocationOverride", {
        "latitude": args["latitude"],
        "longitude": args["longitude"],
        "accuracy": args.get("accuracy", 100),
    })
    return {"geo": {"lat": args["latitude"], "lng": args["longitude"]}}


async def op_clear_geolocation(state, args):
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "Emulation.clearGeolocationOverride")
    return {"cleared": True}


async def op_set_viewport(state, args):
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "Emulation.setDeviceMetricsOverride", {
        "width": args["width"],
        "height": args["height"],
        "deviceScaleFactor": args.get("dpr", 1),
        "mobile": args.get("mobile", False),
    })
    return {"viewport": args}


DEVICE_PRESETS = {
    "iphone15":  {"width": 393, "height": 852, "dpr": 3, "mobile": True,
                  "ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"},
    "iphone_se": {"width": 375, "height": 667, "dpr": 2, "mobile": True,
                  "ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)"},
    "ipad":      {"width": 820, "height": 1180, "dpr": 2, "mobile": True,
                  "ua": "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X)"},
    "pixel8":    {"width": 412, "height": 915, "dpr": 2.6, "mobile": True,
                  "ua": "Mozilla/5.0 (Linux; Android 14; Pixel 8)"},
    "galaxy_s23":{"width": 360, "height": 780, "dpr": 3, "mobile": True,
                  "ua": "Mozilla/5.0 (Linux; Android 13; SM-S911B)"},
    "desktop":   {"width": 1920, "height": 1080, "dpr": 1, "mobile": False,
                  "ua": ""},
}


async def op_set_device(state, args):
    preset = DEVICE_PRESETS.get(args["preset"])
    if not preset:
        raise CDPError(f"unknown preset: {args['preset']}. "
                       f"options: {list(DEVICE_PRESETS.keys())}")
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "Emulation.setDeviceMetricsOverride", {
        "width": preset["width"], "height": preset["height"],
        "deviceScaleFactor": preset["dpr"], "mobile": preset["mobile"],
    })
    if preset["ua"]:
        await state.cdp.target_call(tid, "Network.setUserAgentOverride",
                                    {"userAgent": preset["ua"]})
    return {"device": args["preset"], "applied": preset}


async def op_set_network_conditions(state, args):
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "Network.emulateNetworkConditions", {
        "offline": args.get("offline", False),
        "latency": args.get("latency_ms", 0),
        "downloadThroughput": args.get("download_bytes_per_sec", -1),
        "uploadThroughput": args.get("upload_bytes_per_sec", -1),
    })
    return {"set": args}


async def op_clear_network_conditions(state, args):
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "Network.emulateNetworkConditions", {
        "offline": False, "latency": 0,
        "downloadThroughput": -1, "uploadThroughput": -1,
    })
    return {"cleared": True}


async def op_set_cpu_throttle(state, args):
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "Emulation.setCPUThrottlingRate",
                                {"rate": args["rate"]})
    return {"rate": args["rate"]}


async def op_set_dark_mode(state, args):
    tid = await _tid(state, args)
    scheme = "dark" if args.get("dark", True) else "light"
    await state.cdp.target_call(tid, "Emulation.setEmulatedMedia", {
        "media": "screen",
        "features": [{"name": "prefers-color-scheme", "value": scheme}],
    })
    return {"scheme": scheme}


async def op_set_timezone(state, args):
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "Emulation.setTimezoneOverride",
                                {"timezoneId": args["timezone"]})
    return {"timezone": args["timezone"]}


async def op_set_locale(state, args):
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "Emulation.setLocaleOverride",
                                {"locale": args["locale"]})
    return {"locale": args["locale"]}


# ============================================================
# INTERACTION
# ============================================================
async def op_click(state, args):
    tid = await _tid(state, args)
    sel = args["selector"]
    r = await state.cdp.target_call(tid, "Runtime.evaluate", {
        "expression": f"""
            (() => {{
                const el = document.querySelector({json.dumps(sel)});
                if (!el) return 'not_found';
                el.scrollIntoView({{block: 'center'}});
                el.click();
                return 'ok';
            }})()
        """,
        "returnByValue": True,
        "userGesture": True,
    })
    return {"result": r.get("result", {}).get("value")}


async def op_type_text(state, args):
    tid = await _tid(state, args)
    sel = args["selector"]
    text = args["text"]
    r = await state.cdp.target_call(tid, "Runtime.evaluate", {
        "expression": f"""
            (() => {{
                const el = document.querySelector({json.dumps(sel)});
                if (!el) return 'not_found';
                el.focus();
                if ('value' in el) {{
                    const setter = Object.getOwnPropertyDescriptor(
                        el.constructor.prototype, 'value').set;
                    setter.call(el, {json.dumps(text)});
                }} else {{
                    el.textContent = {json.dumps(text)};
                }}
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
                return 'ok';
            }})()
        """,
        "returnByValue": True,
        "userGesture": True,
    })
    return {"result": r.get("result", {}).get("value")}


async def op_scroll(state, args):
    tid = await _tid(state, args)
    if "selector" in args:
        r = await state.cdp.target_call(tid, "Runtime.evaluate", {
            "expression": f"""
                document.querySelector({json.dumps(args['selector'])})
                    ?.scrollIntoView({{behavior: 'smooth', block: 'center'}})
                    ?? 'not_found'
            """,
            "returnByValue": True,
        })
        return {"result": r.get("result", {}).get("value")}
    dx = args.get("delta_x", 0)
    dy = args.get("delta_y", 500)
    await state.cdp.target_call(tid, "Runtime.evaluate", {
        "expression": f"window.scrollBy({{left:{dx},top:{dy},"
                      f"behavior:'smooth'}})",
        "returnByValue": True,
    })
    return {"scrolled": [dx, dy]}


async def op_hover(state, args):
    tid = await _tid(state, args)
    sel = args["selector"]
    await state.cdp.target_call(tid, "Runtime.evaluate", {
        "expression": f"""
            (() => {{
                const el = document.querySelector({json.dumps(sel)});
                if (!el) return;
                const r = el.getBoundingClientRect();
                ['mouseover','mouseenter','mousemove'].forEach(t =>
                    el.dispatchEvent(new MouseEvent(t, {{
                        bubbles:true, cancelable:true,
                        clientX:r.x+r.width/2, clientY:r.y+r.height/2
                    }}))
                );
            }})()
        """,
    })
    return {"hovered": sel}


async def op_key_press(state, args):
    tid = await _tid(state, args)
    key = args["key"]
    # Use Input.dispatchKeyEvent for proper key events
    await state.cdp.target_call(tid, "Input.dispatchKeyEvent", {
        "type": "keyDown", "key": key,
    })
    await state.cdp.target_call(tid, "Input.dispatchKeyEvent", {
        "type": "keyUp", "key": key,
    })
    return {"pressed": key}


async def op_focus(state, args):
    tid = await _tid(state, args)
    sel = args["selector"]
    r = await state.cdp.target_call(tid, "Runtime.evaluate", {
        "expression": f"""
            document.querySelector({json.dumps(sel)})?.focus() ?? 'not_found'
        """,
        "returnByValue": True,
    })
    return {"result": r.get("result", {}).get("value")}


# ============================================================
# DEBUGGER
# ============================================================
async def op_pause(state, args):
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "Debugger.enable")
    await state.cdp.target_call(tid, "Debugger.pause")
    return {"paused": True}


async def op_resume(state, args):
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "Debugger.resume")
    return {"resumed": True}


async def op_step_over(state, args):
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "Debugger.stepOver")
    return {"stepped": "over"}


async def op_step_into(state, args):
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "Debugger.stepInto")
    return {"stepped": "into"}


async def op_step_out(state, args):
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "Debugger.stepOut")
    return {"stepped": "out"}


async def op_set_breakpoint(state, args):
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "Debugger.enable")
    r = await state.cdp.target_call(tid, "Debugger.setBreakpointByUrl", {
        "lineNumber": args["line"] - 1,
        "url": args.get("url"),
        "urlRegex": args.get("url_regex"),
        "columnNumber": args.get("column", 0),
        "condition": args.get("condition", ""),
    })
    return r


# ============================================================
# PERFORMANCE
# ============================================================
async def op_get_metrics(state, args):
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "Performance.enable")
    r = await state.cdp.target_call(tid, "Performance.getMetrics")
    return {"metrics": {m["name"]: m["value"]
                        for m in r.get("metrics", [])}}


async def op_coverage_start(state, args):
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "Profiler.enable")
    await state.cdp.target_call(tid, "CSS.enable")
    await state.cdp.target_call(tid, "DOM.enable")
    await state.cdp.target_call(tid, "Profiler.startPreciseCoverage", {
        "callCount": True, "detailed": True,
    })
    await state.cdp.target_call(tid, "CSS.startRuleUsageTracking")
    state.coverage_active = True
    return {"started": True}


async def op_coverage_stop(state, args):
    tid = await _tid(state, args)
    js = await state.cdp.target_call(tid, "Profiler.takePreciseCoverage")
    css = await state.cdp.target_call(tid, "CSS.stopRuleUsageTracking")
    await state.cdp.target_call(tid, "Profiler.stopPreciseCoverage")
    state.coverage_active = False
    return {"js_scripts": len(js.get("result", [])),
            "css_rules_used": len(css.get("ruleUsage", [])),
            "js": js, "css": css}


async def op_heap_snapshot(state, args):
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "HeapProfiler.enable")
    out = state.exports_dir / f"heap_{int(time.time())}.heapsnapshot"
    chunks = []

    async def grab(target_id, method, params):
        if method == "HeapProfiler.addHeapSnapshotChunk":
            chunks.append(params.get("chunk", ""))

    state.cdp.on_event(grab)
    await state.cdp.target_call(tid, "HeapProfiler.takeHeapSnapshot",
                                {"reportProgress": False}, timeout=120)
    out.write_text("".join(chunks))
    return {"path": str(out), "size": out.stat().st_size}


# ============================================================
# SECURITY / SERVICE WORKERS
# ============================================================
async def op_security_state(state, args):
    tid = await _tid(state, args)
    await state.cdp.target_call(tid, "Security.enable")
    # State comes via events; ask once
    r = await state.cdp.target_call(tid, "Runtime.evaluate", {
        "expression": """
            ({
                protocol: location.protocol,
                isSecureContext: window.isSecureContext,
                crossOriginIsolated: window.crossOriginIsolated,
            })
        """,
        "returnByValue": True,
    })
    return r.get("result", {}).get("value", {})


async def op_list_service_workers(state, args):
    r = await state.cdp.browser_call("ServiceWorker.enable")
    targets = await state.cdp.list_targets()
    sws = [t for t in targets if t.get("type") == "service_worker"]
    return {"service_workers": sws}


async def op_unregister_service_worker(state, args):
    await state.cdp.browser_call("ServiceWorker.unregister",
                                 {"scopeURL": args["scope"]})
    return {"unregistered": args["scope"]}


# ============================================================
# SEARCH
# ============================================================
async def op_search_in_resources(state, args):
    """Search across all loaded resources."""
    tid = await _tid(state, args)
    q = args["query"]
    is_regex = args.get("regex", False)
    case = args.get("case_sensitive", False)

    lst = await op_list_resources(state, {"target_id": tid})
    hits = []
    for res in lst["resources"]:
        try:
            content = await op_get_resource(state, {
                "target_id": tid, "url": res["url"],
                "frame_id": res["frame_id"],
            })
            if content.get("binary"):
                continue
            text = content.get("content", "")
            if not text:
                continue
            haystack = text if case else text.lower()
            needle = q if case else q.lower()
            if is_regex:
                import re
                matches = list(re.finditer(q, text,
                                           0 if case else re.IGNORECASE))
                if matches:
                    hits.append({
                        "url": res["url"],
                        "count": len(matches),
                        "first": text[max(0, matches[0].start()-30):
                                      matches[0].end()+30],
                    })
            elif needle in haystack:
                idx = haystack.find(needle)
                hits.append({
                    "url": res["url"],
                    "count": haystack.count(needle),
                    "first": text[max(0, idx-30):idx+len(q)+30],
                })
        except Exception:
            continue
    return {"hits": hits, "query": q}


# ============================================================
# EVENT BUFFER (recent events from ring buffer)
# ============================================================
async def op_recent_events(state, args):
    con = state.db()
    kind = args.get("kind")
    limit = int(args.get("limit", 50))
    since = float(args.get("since", 0))
    q = "SELECT ts, kind, payload FROM events WHERE ts > ?"
    a = [since]
    if kind:
        q += " AND kind = ?"
        a.append(kind)
    q += " ORDER BY ts DESC LIMIT ?"
    a.append(limit)
    rows = con.execute(q, a).fetchall()
    con.close()
    return {"events": [
        {"ts": ts, "kind": k, "payload": json.loads(p)}
        for ts, k, p in rows
    ]}


async def op_latest_frame(state, args):
    con = state.db()
    row = con.execute(
        "SELECT payload FROM events WHERE kind='frame' "
        "ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    con.close()
    if not row:
        return {"frame": None}
    return {"frame": json.loads(row[0])}


# ============================================================
# STATUS
# ============================================================
async def op_status(state, args):
    try:
        info = await state.cdp.browser_info()
        tabs = await state.cdp.list_targets()
    except Exception as e:
        info, tabs = {"error": str(e)}, []
    return {
        "browser": info,
        "tab_count": len([t for t in tabs if t.get("type") == "page"]),
        "network_log_size": len(state.network_log),
        "console_log_size": len(state.console_log),
        "overrides": len(state.overrides),
        "blocked_urls": len(state.blocked_urls),
        "coverage_active": state.coverage_active,
    }


async def op_ping(state, args):
    return {"pong": True}


# ============================================================
# Registry
# ============================================================
HANDLERS = {
    # vision
    "screenshot_tab": op_screenshot_tab,
    "screenshot_full_page": op_screenshot_full_page,
    "screenshot_element": op_screenshot_element,
    "latest_frame": op_latest_frame,
    # dom
    "get_dom": op_get_dom,
    "query": op_query,
    "set_html": op_set_html,
    "set_attribute": op_set_attribute,
    "remove_element": op_remove_element,
    "accessibility_tree": op_get_accessibility_tree,
    "inspect_at_point": op_inspect_at_point,
    # console
    "run_js": op_run_js,
    "console_history": op_console_history,
    "clear_console": op_clear_console,
    # network
    "list_requests": op_list_requests,
    "request_details": op_get_request_details,
    "response_body": op_get_response_body,
    "get_curl": op_get_curl,
    "export_har": op_export_har,
    "block_url": op_block_url,
    "unblock_url": op_unblock_url,
    "list_blocks": op_list_blocks,
    "set_extra_headers": op_set_extra_headers,
    # storage
    "cookies_list": op_cookies_list,
    "cookies_set": op_cookies_set,
    "cookies_delete_all": op_cookies_delete,
    "cookies_delete": op_cookies_delete_one,
    "local_storage_list": op_local_storage_list,
    "local_storage_set": op_local_storage_set,
    "local_storage_delete": op_local_storage_delete,
    "local_storage_clear": op_local_storage_clear,
    "session_storage_list": op_session_storage_list,
    "session_storage_set": op_session_storage_set,
    "indexeddb_list": op_indexeddb_list_databases,
    "clear_browser_data": op_clear_browser_data,
    # resources
    "list_resources": op_list_resources,
    "get_resource": op_get_resource,
    "save_all_resources": op_save_all_resources,
    "edit_css": op_edit_css,
    "edit_js": op_edit_js,
    "list_scripts": op_list_scripts,
    # overrides
    "override_create": op_override_create,
    "override_list": op_override_list,
    "override_remove": op_override_remove,
    "override_clear": op_override_clear,
    # page
    "navigate": op_navigate,
    "reload": op_reload,
    "go_back": op_go_back,
    "go_forward": op_go_forward,
    "get_url": op_get_url,
    "get_title": op_get_title,
    "print_to_pdf": op_print_to_pdf,
    "save_html": op_save_html,
    # tabs
    "list_tabs": op_list_tabs,
    "new_tab": op_new_tab,
    "close_tab": op_close_tab,
    "focus_tab": op_focus_tab,
    # emulation
    "set_user_agent": op_set_user_agent,
    "set_geolocation": op_set_geolocation,
    "clear_geolocation": op_clear_geolocation,
    "set_viewport": op_set_viewport,
    "set_device": op_set_device,
    "set_network_conditions": op_set_network_conditions,
    "clear_network_conditions": op_clear_network_conditions,
    "set_cpu_throttle": op_set_cpu_throttle,
    "set_dark_mode": op_set_dark_mode,
    "set_timezone": op_set_timezone,
    "set_locale": op_set_locale,
    # interaction
    "click": op_click,
    "type_text": op_type_text,
    "scroll": op_scroll,
    "hover": op_hover,
    "key_press": op_key_press,
    "focus": op_focus,
    # debugger
    "pause": op_pause,
    "resume": op_resume,
    "step_over": op_step_over,
    "step_into": op_step_into,
    "step_out": op_step_out,
    "set_breakpoint": op_set_breakpoint,
    # performance
    "get_metrics": op_get_metrics,
    "coverage_start": op_coverage_start,
    "coverage_stop": op_coverage_stop,
    "heap_snapshot": op_heap_snapshot,
    # security
    "security_state": op_security_state,
    "list_service_workers": op_list_service_workers,
    "unregister_service_worker": op_unregister_service_worker,
    # search & events
    "search_in_resources": op_search_in_resources,
    "recent_events": op_recent_events,
    # meta
    "status": op_status,
    "ping": op_ping,
}
