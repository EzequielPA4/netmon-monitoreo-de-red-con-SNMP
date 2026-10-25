"""
Servidor web de netmon (FastAPI + Uvicorn).

  GET /              -> mapa (static/index.html)
  GET /api/snapshot  -> foto actual del estado (JSON)
  GET /api/stream    -> Server-Sent Events: una foto nueva por cada publicacion

Todo el estado vive en memoria (sin historial).

Uso:
  python -m netmon.web --host 0.0.0.0 --port 8080
"""
import argparse
import asyncio
import contextlib
import json
import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import load_config
from .engine import Engine

log = logging.getLogger("netmon.web")
STATIC_DIR = Path(__file__).parent / "static"
HEARTBEAT_S = 15          # comentario SSE para mantener viva la conexion
_stopping = False         # se activa al recibir SIGINT/SIGTERM: cierra los SSE abiertos


class NetmonServer(uvicorn.Server):
    """Uvicorn que avisa a las conexiones SSE que deben cerrarse al detener el servicio."""

    def handle_exit(self, sig, frame):
        global _stopping
        _stopping = True
        super().handle_exit(sig, frame)


class Hub:
    """Reparte snapshots y eventos a todos los navegadores conectados (una cola por cliente)."""

    def __init__(self):
        self.queues = set()

    def _put(self, kind, payload):
        data = json.dumps(payload)
        for q in list(self.queues):
            if q.full():                 # cliente lento: se descarta lo mas viejo
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait((kind, data))

    def publish(self, snap):
        self._put("snapshot", snap)

    def publish_event(self, event):
        self._put("trap", event)

    def subscribe(self):
        q = asyncio.Queue(maxsize=64)
        self.queues.add(q)
        return q

    def unsubscribe(self, q):
        self.queues.discard(q)


def create_app(inventory=None, trap_port=None):
    cfg = load_config(inventory)
    if trap_port is not None:
        cfg.trap_port = trap_port
    engine = Engine(cfg)
    hub = Hub()
    engine.listeners.append(hub.publish)
    engine.event_listeners.append(hub.publish_event)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        await engine.start(trap_port=cfg.trap_port)
        publisher = asyncio.create_task(engine.run_publisher())
        log.info("netmon: %d equipos SNMP, intervalo %d s", len(engine.pollers), cfg.interval_s)
        try:
            yield
        finally:
            publisher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await publisher
            await engine.stop()

    app = FastAPI(title="netmon", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/snapshot")
    async def api_snapshot():
        return JSONResponse(engine.last_snapshot or {"ts": None, "devices": [], "links": []})

    @app.get("/api/events")
    async def api_events():
        return JSONResponse(engine.events.recent())

    @app.post("/api/events/clear")
    async def api_events_clear():
        engine.events.clear()
        hub._put("events", [])          # todos los navegadores conectados vacian su lista
        engine.publish()                # contadores en cero
        log.info("Linea de tiempo de eventos limpiada desde la web")
        return JSONResponse({"ok": True})

    @app.get("/api/stream")
    async def api_stream(request: Request):
        q = hub.subscribe()

        async def events():
            try:
                if engine.last_snapshot:                     # foto inicial inmediata
                    yield f"event: snapshot\ndata: {json.dumps(engine.last_snapshot)}\n\n"
                yield f"event: events\ndata: {json.dumps(engine.events.recent())}\n\n"
                idle = 0
                while not _stopping and not await request.is_disconnected():
                    try:
                        kind, data = await asyncio.wait_for(q.get(), timeout=1)
                        idle = 0
                        yield f"event: {kind}\ndata: {data}\n\n"
                    except asyncio.TimeoutError:
                        idle += 1
                        if idle >= HEARTBEAT_S:
                            idle = 0
                            yield ": keepalive\n\n"
            finally:
                hub.unsubscribe(q)

        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        return StreamingResponse(events(), media_type="text/event-stream", headers=headers)

    return app


def main():
    parser = argparse.ArgumentParser(description="Servidor web netmon")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--inventory", help="ruta del inventory.yaml")
    parser.add_argument("--trap-port", type=int, default=None,
                        help="puerto UDP de traps (por defecto el del inventario; 0 = deshabilitado)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # timeout_graceful_shutdown: las conexiones SSE no terminan solas; sin esto,
    # al detener el servicio uvicorn esperaria indefinidamente a que se cierren.
    config = uvicorn.Config(create_app(args.inventory, args.trap_port), host=args.host, port=args.port,
                            log_level="info", timeout_graceful_shutdown=3)
    NetmonServer(config).run()


if __name__ == "__main__":
    main()
