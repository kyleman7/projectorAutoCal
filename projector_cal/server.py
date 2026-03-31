"""FastAPI web server + WebSocket broadcaster.

The server runs in the main thread (via uvicorn). The calibration engine runs in a
background worker thread. They communicate via an asyncio.Queue bridged to a
threading.Queue through a thread-safe put-event mechanism.

WebSocket clients receive a stream of JSON event objects as calibration progresses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import ConfigError, load_command_table, load_config
from .discovery import NetworkDiscovery
from .engine import CalibrationEngine, CalibrationReport
from .pgen import NullPatchDisplay, PGenClient, RGBPatch
from .profiles import (
    CalibrationProfile,
    apply_profile,
    delete_profile,
    list_profiles,
    load_profile,
    profile_from_projector,
    save_profile,
)
from .projector import ProjectorClient, ProjectorError

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent.parent / "static"
_PROFILES_DIR = Path(__file__).parent.parent / "profiles"

app = FastAPI(title="ProjectorCal", version="0.1.0")

# ---------------------------------------------------------------------------
# Shared server state
# ---------------------------------------------------------------------------

class _ServerState:
    def __init__(self) -> None:
        self.config_path: str | None = None
        self.config = load_config()
        self.command_table: dict = {}
        self._engine_thread: threading.Thread | None = None
        self._engine: CalibrationEngine | None = None
        self._ws_clients: set[WebSocket] = set()
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def broadcast_event(self, event: dict) -> None:
        """Thread-safe: put event into async queue from any thread."""
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._event_queue.put_nowait, event)

    def is_running(self) -> bool:
        return self._engine_thread is not None and self._engine_thread.is_alive()


_state = _ServerState()


# ---------------------------------------------------------------------------
# WebSocket broadcaster
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def _startup() -> None:
    _state.set_loop(asyncio.get_event_loop())
    # Load command table if it exists
    try:
        _state.command_table = load_command_table()
    except ConfigError:
        _state.command_table = {}
    asyncio.create_task(_ws_broadcaster())


async def _ws_broadcaster() -> None:
    """Consume events from the queue and broadcast to all WebSocket clients."""
    while True:
        event = await _state._event_queue.get()
        dead: set[WebSocket] = set()
        for ws in list(_state._ws_clients):
            try:
                await ws.send_text(json.dumps(event))
            except Exception:
                dead.add(ws)
        _state._ws_clients -= dead


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    _state._ws_clients.add(ws)
    logger.info("WebSocket client connected (%d total)", len(_state._ws_clients))
    try:
        while True:
            await ws.receive_text()  # keep alive; client messages ignored
    except WebSocketDisconnect:
        pass
    finally:
        _state._ws_clients.discard(ws)
        logger.info("WebSocket client disconnected (%d remaining)", len(_state._ws_clients))


# ---------------------------------------------------------------------------
# Config API
# ---------------------------------------------------------------------------

@app.get("/api/config")
async def get_config() -> dict:
    from dataclasses import asdict
    return asdict(_state.config)


class ConfigUpdate(BaseModel):
    config: dict


@app.post("/api/config")
async def update_config(body: ConfigUpdate) -> dict:
    import json, tempfile
    from pathlib import Path
    tmp = Path(tempfile.mktemp(suffix=".json"))
    try:
        tmp.write_text(json.dumps(body.config))
        _state.config = load_config(str(tmp))
        return {"ok": True}
    except ConfigError as e:
        raise HTTPException(400, detail=str(e))
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Probe API
# ---------------------------------------------------------------------------

@app.post("/api/probe")
async def run_probe() -> dict:
    if _state.is_running():
        raise HTTPException(409, detail="Calibration is already running")

    def _do_probe() -> None:
        from .probe import run_probe as _probe, save_command_table
        from .config import _DEFAULT_COMMAND_TABLE_PATH
        cfg = _state.config.projector
        try:
            with ProjectorClient.from_config(cfg) as proj:
                result = _probe(
                    proj,
                    progress_cb=lambda msg: _state.broadcast_event({"event": "probe_progress", "message": msg}),
                )
                save_command_table(result, _DEFAULT_COMMAND_TABLE_PATH)
                _state.command_table = result.to_command_table()
                _state.broadcast_event({
                    "event": "probe_done",
                    "missing": result.missing_slots(),
                    "summary": result.summary_lines(),
                })
        except Exception as e:
            _state.broadcast_event({"event": "error", "message": str(e)})

    threading.Thread(target=_do_probe, daemon=True, name="probe").start()
    return {"ok": True, "message": "Probe started — watch WebSocket for progress"}


# ---------------------------------------------------------------------------
# Calibration run API
# ---------------------------------------------------------------------------

class RunRequest(BaseModel):
    mode: Literal["sdr", "hdr10"] = "sdr"
    phase: Literal["wb", "cms", "all"] = "all"
    dry_run: bool = False


@app.post("/api/run")
async def start_run(body: RunRequest) -> dict:
    if _state.is_running():
        raise HTTPException(409, detail="Calibration is already running")

    def _do_run() -> None:
        proj_cfg = _state.config.projector
        pgen_cfg = _state.config.pgen
        cal_cfg = _state.config.calibration

        try:
            projector = ProjectorClient.from_config(
                proj_cfg, command_table=_state.command_table
            )
            projector.connect()

            if body.dry_run:
                display = NullPatchDisplay(patch_settle_ms=pgen_cfg.patch_settle_ms)
            else:
                display = PGenClient(
                    host=pgen_cfg.host,
                    port=pgen_cfg.port,
                    patch_settle_ms=pgen_cfg.patch_settle_ms,
                )

            # Colorimeter is POSIX-only — on Windows use a dummy measure function
            try:
                from .colorimeter import Colorimeter
                col = Colorimeter(
                    spotread_path=_state.config.colorimeter.spotread_path,
                    display_index=_state.config.colorimeter.display_index,
                    measurement_retries=_state.config.colorimeter.measurement_retries,
                    measurement_timeout=_state.config.colorimeter.measurement_timeout,
                )
                col.start()
                measure_fn = col.measure
            except ImportError:
                logger.warning("Colorimeter module unavailable on this platform — using dummy measure")
                def measure_fn():
                    import random
                    Y = 50.0 + random.uniform(-5, 5)
                    return (0.95 * Y, Y, 1.08 * Y)

            engine = CalibrationEngine(
                projector=projector,
                display=display,
                measure=measure_fn,
                config=cal_cfg,
                mode=body.mode,
                dry_run=body.dry_run,
                on_event=_state.broadcast_event,
            )
            _state._engine = engine
            display.connect()

            try:
                if body.phase == "all":
                    engine.run_all()
                elif body.phase == "wb":
                    engine.run_wb_only()
                else:
                    engine.run_cms_only()
            finally:
                display.disconnect()
                projector.disconnect()
                try:
                    col.stop()
                except Exception:
                    pass

        except Exception as e:
            logger.exception("Calibration run failed")
            _state.broadcast_event({"event": "error", "message": str(e)})
        finally:
            _state._engine = None

    thread = threading.Thread(target=_do_run, daemon=True, name="calibration")
    thread.start()
    _state._engine_thread = thread
    return {"ok": True, "message": "Calibration started"}


@app.post("/api/run/stop")
async def stop_run() -> dict:
    if _state._engine is not None:
        _state._engine.abort()
        return {"ok": True, "message": "Abort signal sent"}
    return {"ok": False, "message": "No calibration running"}


# ---------------------------------------------------------------------------
# Warm-up monitoring
# ---------------------------------------------------------------------------

@app.get("/api/warmup")
async def warmup_reading() -> dict:
    """Take a single colorimeter reading for the warm-up monitor."""
    try:
        from .colorimeter import Colorimeter
        col_cfg = _state.config.colorimeter
        col = Colorimeter(
            spotread_path=col_cfg.spotread_path,
            display_index=col_cfg.display_index,
            measurement_retries=1,
            measurement_timeout=col_cfg.measurement_timeout,
        )
        with col:
            X, Y, Z = col.measure()
        return {"Y": round(Y, 3), "X": round(X, 3), "Z": round(Z, 3)}
    except ImportError:
        raise HTTPException(501, detail="Colorimeter not available on this platform")
    except Exception as e:
        raise HTTPException(500, detail=str(e))


# ---------------------------------------------------------------------------
# Profiles API
# ---------------------------------------------------------------------------

@app.get("/api/profiles")
async def get_profiles() -> list[dict]:
    from dataclasses import asdict
    profiles = list_profiles(_PROFILES_DIR)
    return [asdict(p) for p in profiles]


class SaveProfileRequest(BaseModel):
    name: str
    mode: Literal["sdr", "hdr10"]
    screen_info: dict = {}


@app.post("/api/profiles")
async def save_current_profile(body: SaveProfileRequest) -> dict:
    proj_cfg = _state.config.projector
    try:
        with ProjectorClient.from_config(proj_cfg, command_table=_state.command_table) as proj:
            profile = profile_from_projector(
                name=body.name,
                mode=body.mode,
                projector=proj,
                screen_info=body.screen_info,
            )
            path = save_profile(profile, _PROFILES_DIR)
            return {"ok": True, "file": path.name}
    except ProjectorError as e:
        raise HTTPException(500, detail=str(e))


@app.delete("/api/profiles/{filename}")
async def delete_profile_endpoint(filename: str) -> dict:
    path = _PROFILES_DIR / filename
    if not path.exists():
        raise HTTPException(404, detail="Profile not found")
    delete_profile(path)
    return {"ok": True}


@app.post("/api/profiles/{filename}/apply")
async def apply_profile_endpoint(filename: str) -> dict:
    path = _PROFILES_DIR / filename
    if not path.exists():
        raise HTTPException(404, detail="Profile not found")
    try:
        profile = load_profile(path)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))

    proj_cfg = _state.config.projector
    try:
        with ProjectorClient.from_config(proj_cfg, command_table=_state.command_table) as proj:
            apply_profile(profile, proj)
            _state.broadcast_event({"event": "profile_applied", "profile": profile.name})
            return {"ok": True}
    except ProjectorError as e:
        raise HTTPException(500, detail=str(e))


@app.post("/api/profiles/compare")
async def compare_profiles_endpoint(body: dict) -> dict:
    from .agents.profile_advisor import compare_profiles
    filename_a = body.get("a")
    filename_b = body.get("b")
    if not filename_a or not filename_b:
        raise HTTPException(400, detail="Provide 'a' and 'b' filenames")
    try:
        pa = load_profile(_PROFILES_DIR / filename_a)
        pb = load_profile(_PROFILES_DIR / filename_b)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(400, detail=str(e))

    result = compare_profiles(pa, pb)
    _state.broadcast_event({"event": "agent_result", "agent": "profile_advisor", "result": result})
    return result


# ---------------------------------------------------------------------------
# Discovery API
# ---------------------------------------------------------------------------

@app.post("/api/discover")
async def start_discovery() -> dict:
    def _do_scan() -> None:
        disc = NetworkDiscovery(
            pgen_port=_state.config.pgen.port,
            progress_cb=lambda msg: _state.broadcast_event({"event": "discovery_progress", "message": msg}),
        )
        subnet = disc.get_local_subnet()
        _state.broadcast_event({"event": "discovery_start", "method": "mdns+port_scan", "subnet": subnet})
        devices = disc.scan(subnet=subnet)
        for dev in devices:
            _state.broadcast_event({"event": "discovery_found", "device": dev.as_dict()})
        disc.save_last_scan(devices)
        _state.broadcast_event({
            "event": "discovery_done",
            "total": len(devices),
            "devices": [d.as_dict() for d in devices],
        })

    threading.Thread(target=_do_scan, daemon=True, name="discovery").start()
    return {"ok": True, "message": "Discovery started"}


@app.get("/api/discover/last")
async def last_discovery() -> dict:
    disc = NetworkDiscovery()
    devices = disc.load_last_scan()
    return {"devices": [d.as_dict() for d in devices]}


@app.post("/api/discover/confirm/{ip}")
async def confirm_device(ip: str, body: dict) -> dict:
    device_type = body.get("device_type", "projector")
    disc = NetworkDiscovery(pgen_port=_state.config.pgen.port)
    dev = disc.confirm_device(ip, device_type)
    if dev:
        return {"confirmed": True, "device": dev.as_dict()}
    return {"confirmed": False}


# ---------------------------------------------------------------------------
# Agent — setup validation
# ---------------------------------------------------------------------------

@app.post("/api/setup/validate")
async def validate_setup_endpoint(body: dict) -> dict:
    from .agents.setup_validator import validate_setup

    result = validate_setup(
        projector_connected=body.get("projector_connected", False),
        pgen_connected=body.get("pgen_connected", False),
        command_table=_state.command_table,
        warm_up_stable=body.get("warm_up_stable", False),
        colorimeter_detected=body.get("colorimeter_detected", False),
        screen_info=body.get("screen_info", {}),
    )
    _state.broadcast_event({"event": "setup_check", **result})
    return result


# ---------------------------------------------------------------------------
# Connection test
# ---------------------------------------------------------------------------

@app.post("/api/test-connection")
async def test_connection(body: dict) -> dict:
    device_type = body.get("type", "projector")
    transport = body.get("transport", "tcp")
    ip = body.get("ip", "")
    port = int(body.get("port", 3629))
    serial_port = body.get("serial_port", "/dev/ttyUSB0")
    serial_baud = int(body.get("serial_baud", 9600))

    if device_type == "projector":
        try:
            if transport == "serial":
                proj = ProjectorClient.from_serial(
                    port=serial_port, baud=serial_baud, command_timeout=3.0
                )
            else:
                proj = ProjectorClient.from_tcp(
                    host=ip, port=port, connection_timeout=3.0, command_timeout=2.0
                )
            with proj:
                if transport == "serial":
                    return {"connected": True, "transport": "serial", "port": serial_port}
                return {"connected": True, "transport": "tcp", "ip": ip, "port": port}
        except ProjectorError as e:
            return {"connected": False, "error": str(e)}
    elif device_type == "pgen":
        try:
            with PGenClient(host=ip, port=port, connection_timeout=3.0):
                return {"connected": True, "ip": ip, "port": port}
        except Exception as e:
            return {"connected": False, "error": str(e)}

    return {"connected": False, "error": "Unknown device type"}


@app.get("/api/serial-ports")
async def get_serial_ports() -> list[dict]:
    """Return available serial ports on the host machine.

    Used by the Setup tab to populate the serial port dropdown.
    Returns empty list if pyserial is not installed.
    """
    from .projector import list_serial_ports
    return list_serial_ports()


# ---------------------------------------------------------------------------
# Static file serving (SPA)
# ---------------------------------------------------------------------------

if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/")
    async def serve_index() -> FileResponse:
        return FileResponse(str(_STATIC_DIR / "index.html"))

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str) -> FileResponse:
        """Catch-all: return index.html for all non-API routes (SPA routing)."""
        if not full_path.startswith("api/"):
            return FileResponse(str(_STATIC_DIR / "index.html"))
        raise HTTPException(404)
