#!/usr/bin/env python3
"""
CDP client for browser-eyes.

One CDPClient instance per daemon. Holds:
  - one browser-level websocket for Browser, Target, SystemInfo domains
  - one websocket per attached target (each gets its own sessionless connection)
  - a single shared message-id counter
  - per-message Futures so concurrent calls don't step on each other
  - an event subscription system

Why per-target instead of flattening through one browser ws with sessions?
The sessioned API in CDP is finicky and most domains work cleaner on a direct
target connection. We pay a couple websocket handles in exchange for simpler code.
"""
import asyncio
import json
import logging
from contextlib import suppress

import aiohttp
import websockets

log = logging.getLogger("browser-eyes.cdp")


class CDPError(Exception):
    pass


class CDPClient:
    def __init__(self, port=9222):
        self.port = port
        self.browserws = None
        self.targetwss = {}          # targetid -> websocket
        self.targetreaders = {}      # targetid -> asyncio.Task
        self.msgid = 0
        self.pending = {}            # msgid -> Future
        self.eventhandlers = []      # list of async callables (targetid, method, params)
        self.lock = asyncio.Lock()   # protects msgid and pending

    # ---------- HTTP discovery ----------

    async def listtargets(self):
        async with aiohttp.ClientSession() as sess:
            async with sess.get(f"http://127.0.0.1:{self.port}/json") as r:
                return await r.json()

    async def browserinfo(self):
        async with aiohttp.ClientSession() as sess:
            async with sess.get(f"http://127.0.0.1:{self.port}/json/version") as r:
                return await r.json()

    async def newtab(self, url=None):
        async with aiohttp.ClientSession() as sess:
            params = {"url": url} if url else {}
            async with sess.put(
                f"http://127.0.0.1:{self.port}/json/new", params=params
            ) as r:
                return await r.json()

    async def closetab(self, targetid):
        async with aiohttp.ClientSession() as sess:
            await sess.get(f"http://127.0.0.1:{self.port}/json/close/{targetid}")

    async def activatetab(self, targetid):
        async with aiohttp.ClientSession() as sess:
            await sess.get(
                f"http://127.0.0.1:{self.port}/json/activate/{targetid}"
            )

    # ---------- Connection lifecycle ----------

    async def connectbrowser(self):
        info = await self.browserinfo()
        wsurl = info["webSocketDebuggerUrl"]
        self.browserws = await websockets.connect(wsurl, max_size=50_000_000)
        asyncio.create_task(self._readerloop(self.browserws, targetid=None))
        return info

    async def connecttarget(self, targetid):
        if targetid in self.targetwss:
            ws = self.targetwss[targetid]
            if not ws.closed:
                return ws
        targets = await self.listtargets()
        target = next((t for t in targets if t["id"] == targetid), None)
        if not target:
            raise CDPError(f"target {targetid} not found")
        ws = await websockets.connect(
            target["webSocketDebuggerUrl"], max_size=50_000_000
        )
        self.targetwss[targetid] = ws
        self.targetreaders[targetid] = asyncio.create_task(
            self._readerloop(ws, targetid=targetid)
        )
        return ws

    async def disconnecttarget(self, targetid):
        ws = self.targetwss.pop(targetid, None)
        task = self.targetreaders.pop(targetid, None)
        if ws and not ws.closed:
            await ws.close()
        if task:
            task.cancel()
            with suppress(Exception):
                await task

    async def close(self):
        for tid in list(self.targetwss.keys()):
            await self.disconnecttarget(tid)
        if self.browserws and not self.browserws.closed:
            await self.browserws.close()

    # ---------- Message I/O ----------

    async def _readerloop(self, ws, targetid):
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if "id" in msg:
                    fut = self.pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_exception(
                                CDPError(
                                    f"{msg['error'].get('code')} {msg['error'].get('message')}"
                                )
                            )
                        else:
                            fut.set_result(msg.get("result", {}))
                elif "method" in msg:
                    for handler in self.eventhandlers:
                        try:
                            await handler(targetid, msg["method"], msg.get("params", {}))
                        except Exception as e:
                            log.warning("event handler error: %s", e)
        except websockets.ConnectionClosed:
            pass
        except Exception as e:
            log.warning("reader loop error: %s", e)

    async def _send(self, ws, method, params=None, timeout=30):
        async with self.lock:
            self.msgid += 1
            mid = self.msgid
            fut = asyncio.get_event_loop().create_future()
            self.pending[mid] = fut
        try:
            await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self.pending.pop(mid, None)
            raise CDPError(f"timeout waiting for {method}")

    async def browsercall(self, method, params=None, timeout=30):
        if not self.browserws or self.browserws.closed:
            await self.connectbrowser()
        return await self._send(self.browserws, method, params, timeout)

    async def targetcall(self, targetid, method, params=None, timeout=30):
        ws = await self.connecttarget(targetid)
        return await self._send(ws, method, params, timeout)

    # ---------- Helpers ----------

    async def firstpagetarget(self):
        """Return the most likely active tab id."""
        targets = await self.listtargets()
        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            raise CDPError("no page targets open")
        return pages[0]["id"]

    def onevent(self, handler):
        """Register an async callback (targetid, method, params)."""
        self.eventhandlers.append(handler)
