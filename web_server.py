"""FastAPI + WebSocket server for the Forge Gauntlet live tournament UI."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from tournament_state import TournamentState

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Forge Gauntlet")

# ---------------------------------------------------------------------------
# Connection manager — no bare globals, avoids Python scoping bugs
# ---------------------------------------------------------------------------

class _ConnectionManager:
    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.queue: Optional[asyncio.Queue] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.state: Optional[TournamentState] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.queue = asyncio.Queue()

    def enqueue(self, payload: dict) -> None:
        """Called from the sync tournament thread — thread-safe."""
        if self.loop is None or self.queue is None:
            return
        try:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, payload)
        except Exception:
            pass

    async def broadcast_worker(self) -> None:
        """Runs inside the asyncio event loop — drains queue → all clients."""
        while True:
            payload = await self.queue.get()
            text = json.dumps(payload)
            dead = set()
            for ws in list(self.clients):
                try:
                    await ws.send_text(text)
                except Exception:
                    dead.add(ws)
            self.clients -= dead

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)
        if self.state is not None:
            await ws.send_text(json.dumps(self.state.snapshot()))

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)


mgr = _ConnectionManager()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def index():
    p = STATIC_DIR / "index.html"
    return FileResponse(str(p), media_type="text/html") if p.exists() else HTMLResponse("Not found", status_code=404)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/api/state")
async def api_state():
    return mgr.state.snapshot() if mgr.state else {"error": "no state"}

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await mgr.connect(ws)
    try:
        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                await ws.send_text(json.dumps({"type": "ping"}))
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        mgr.disconnect(ws)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def start_server(
    state: TournamentState,
    host: str = "0.0.0.0",
    port: int = 7777,
) -> None:
    """Start the web server in a background daemon thread."""
    mgr.state = state

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        mgr.set_loop(loop)

        # Wire broadcast callback
        state.broadcast_callback = mgr.enqueue

        async def _serve():
            asyncio.ensure_future(mgr.broadcast_worker())
            config = uvicorn.Config(
                app,
                host=host,
                port=port,
                loop="none",
                log_level="warning",
                access_log=False,
            )
            server = uvicorn.Server(config)
            await server.serve()

        loop.run_until_complete(_serve())

    t = threading.Thread(target=_run, daemon=True, name="forge-gauntlet-webserver")
    t.start()
