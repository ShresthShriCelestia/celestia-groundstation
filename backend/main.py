# main.py
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import contextlib
import asyncio
from typing import List
import time
from pathlib import Path
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from backend.config import settings
from backend.state import state
from backend.supervisor import supervisor
from backend.api_px4 import router as px4_router
from backend.api_laser import router as laser_router
from backend.auth.router import router as auth_router
from backend.auth.jwt import decode_token as decode_token
from backend.auth.init_db import init_db
from backend.api_pairing import router as pairing_router

from backend.models import (
    RampStartRequest,
    PermitConfigRequest,
    DrillUpdateRequest,
    SystemStatus,
)

# ════════════════════════════════════════════════════════════════
# SECURITY HEADERS MIDDLEWARE
# ════════════════════════════════════════════════════════════════

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"

        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains; preload"
            )

        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=()"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; "
            "font-src 'self' data:; "
            "connect-src 'self' ws: wss: http://localhost:* http://127.0.0.1:*; "
            "frame-ancestors 'none';"
        )
        return response

# ════════════════════════════════════════════════════════════════
# BACKGROUND TASKS
# ════════════════════════════════════════════════════════════════

async def _poll_laser_telemetry():
    from backend.api_laser import get_laser_decoder
    print("[Laser] Starting telemetry polling task")

    while True:
        try:
            decoder = get_laser_decoder()
            telemetry = decoder.get_laser_telemetry()
            print(f"[Laser] Retrieved telemetry: {telemetry}")
            await state.update_laser_telemetry(telemetry)
            print(f"[Laser] Updated state successfully")
            await asyncio.sleep(1.0)  # Poll at 1 Hz
        except asyncio.CancelledError:
            print("[Laser] Telemetry polling task cancelled")
            break
        except Exception as e:
            print(f"[Laser] Polling error: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(5.0)  # Back off on error

# ════════════════════════════════════════════════════════════════
# LIFESPAN MANAGEMENT
# ════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("="*70)
    print("LASER POWER BEAMING BACKEND - STARTING")
    print("="*70)

    try:
        settings.validate_scripts_exist()
        print("All required scripts found.")
    except FileNotFoundError as e:
        print(f"Warning: {e}")
        print("Some features may be unavailable, but laser control will work.")

    settings.ensure_data_dir()
    await state.set_status("DISCONNECTED")
    await state.add_event(
        "INFO",
        "server",
        "SERVER_START",
        f"Backend started on {settings.HOST}:{settings.PORT}",
    )

    init_db()
    laser_task = asyncio.create_task(_poll_laser_telemetry())
    print("Laser telemetry polling started")
    print("Backend ready")
    print("="*70)

    yield

    print("\n" + "="*70)
    print("LASER POWER BEAMING BACKEND - SHUTTING DOWN")
    laser_task.cancel()
    try:
        await laser_task
    except asyncio.CancelledError:
        pass

    if supervisor.is_running():
        await supervisor.stop_all()

    await state.add_event("INFO", "server", "SERVER_STOP", "Backend shutting down")
    print("Cleanup complete")
    print("="*70)

# ════════════════════════════════════════════════════════════════
# CREATE FASTAPI APP
# ════════════════════════════════════════════════════════════════

app = FastAPI(
    title="Laser Power Beaming API",
    version="1.0.0",
    lifespan=lifespan
)

app.include_router(px4_router)
app.include_router(laser_router)
app.include_router(auth_router)
app.include_router(pairing_router)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

class PrivateNetworkAccessMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response

app.add_middleware(PrivateNetworkAccessMiddleware)

# ════════════════════════════════════════════════════════════════
# REST API ENDPOINTS
# ════════════════════════════════════════════════════════════════

@app.get("/")
async def root():
    return {
        "message": "Laser Power Beaming API",
        "version": "1.0.0",
        "status": await state.get_telemetry_snapshot()
    }

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": int(time.time() * 1000),
        "system": {
            "connected_websockets": len(active_connections),
            "running_processes": len([p for p in supervisor.get_process_status().values() if p is not None]),
            "state_status": state.status,
        }
    }

@app.get("/status", response_model=SystemStatus)
async def get_status():
    # Use the method-based access for thread safety
    events = await state.get_recent_events(10)
    errors = [event["msg"] for event in events if event["level"] == "ERROR"]

    return SystemStatus(
        server_version="1.0.0",
        status=state.status,
        processes=supervisor.get_process_status(),
        last_telemetry_ts=int(state.last_telemetry_ts * 1000) if state.last_telemetry_ts > 0 else None,
        errors=errors
    )

@app.post("/ramp/start")
async def start_ramp(req: RampStartRequest):
    if supervisor.is_running():
        raise HTTPException(status_code=400, detail="Ramp already in progress.")
    if req.max_power_w > settings.MAX_POWER_W:
        raise HTTPException(status_code=400, detail=f"Exceeds hardware limit ({settings.MAX_POWER_W}W)")

    try:
        session_id = await supervisor.start_all(req)
        return {"status": "started", "session_id": session_id}
    except Exception as e:
        await state.add_event("ERROR", "server", "START_FAILED", f"Failed to start ramp: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/ramp/stop")
async def stop_ramp():
    if not supervisor.is_running():
        raise HTTPException(status_code=400, detail="No ramp running")
    try:
        if supervisor.px4 and supervisor.px4._drone:
            with contextlib.suppress(Exception):
                await supervisor.px4_land()
                await asyncio.sleep(3)
        await supervisor.stop_all()
        return {"status": "stopped"}
    except Exception as e:
        await state.add_event("ERROR", "server", "STOP_FAILED", f"Failed to stop ramp: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/events")
async def get_events(count: int = 50):
    return await state.get_recent_events(count)

# ════════════════════════════════════════════════════════════════
# WEBSOCKET ENDPOINTS
# ════════════════════════════════════════════════════════════════

active_connections: list[WebSocket] = []

async def _broadcast_ws(payload: dict):
    dead = []
    for ws in list(active_connections):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        with contextlib.suppress(Exception):
            active_connections.remove(ws)

broadcast_func = lambda payload: asyncio.create_task(_broadcast_ws(payload))
supervisor.ws_broadcast = broadcast_func
state.ws_broadcast = broadcast_func

@app.websocket("/ws/telemetry")
async def websocket_telemetry(websocket: WebSocket):
    token = websocket.query_params.get("token")
    if settings.ENABLE_AUTH:
        if token:
            payload = decode_token(token or "")
            roles = set((payload or {}).get("roles", []))
            if not payload or not roles.intersection({"VIEWER", "DEVELOPER", "ADMIN"}):
                await websocket.close(code=4403)
                return

    await websocket.accept()
    active_connections.append(websocket)
    await state.add_event(
        "INFO",
        "server",
        "WS_CONNECT",
        f"Client connected (total: {len(active_connections)})"
    )

    period = 1.0 / settings.WEBSOCKET_UPDATE_HZ


    # Send initial telemetry immediately on connect
    print("[WS] Sending initial telemetry snapshot")
    initial_telemetry = await state.get_telemetry_snapshot()
    await websocket.send_json({"type": "telemetry", "status": state.status, **initial_telemetry})
    

    # Send initial telemetry immediately on connect
    print("[WS] Sending initial telemetry snapshot")
    telemetry = await state.get_telemetry_snapshot()
    initial_payload = {
        "type": "telemetry",
        "power": {
            "commanded_w": telemetry.get("commanded_w", 0.0),
            "received_mw": telemetry.get("received_mw", 0.0),
            "efficiency_pct": telemetry.get("efficiency_pct", 0.0),
        },
        "status": state.status,
        "session_id": state.session_id or "",
    }
    await websocket.send_json(initial_payload)
    

    # Send initial telemetry immediately on connect
    print("[WS] Sending initial telemetry snapshot")
    telemetry = await state.get_telemetry_snapshot()
    rtt_p95, rtt_p99 = await state.calculate_rtt_percentiles()
    
    initial_payload = {
        "type": "telemetry",
        "ts": int(time.time() * 1000),
        "power": {
            "commanded_w": telemetry.get("commanded_w", 0.0),
            "received_mw": telemetry.get("received_mw", 0.0),
            "efficiency_pct": telemetry.get("efficiency_pct", 0.0),
        },
        "link": {
            "quality_pct": telemetry.get("link_quality_pct", 0),
            "rtt_ms": telemetry.get("rtt_ms", 0.0),
            "rtt_p95_ms": rtt_p95,
            "rtt_p99_ms": rtt_p99,
        },
        "permit": {
            "granted": telemetry.get("granted", False),
            "deny_reason": telemetry.get("deny_reason"),
            "grants_total": telemetry.get("grants_total", 0),
            "denies_total": telemetry.get("denies_total", 0),
            "seq": telemetry.get("seq", 0),
            "bypass_enabled": settings.BYPASS_PHOTON_HANDSHAKE,
        },
        "battery": {
            "voltage_mv": telemetry.get("voltage_mv") or telemetry.get("px4_voltage_mv", 0),
            "current_ma": telemetry.get("current_ma") or telemetry.get("px4_current_ma", 0),
            "soc_pct": telemetry.get("soc_pct", 0.0),
            "temp_cdeg": telemetry.get("temp_cdeg", 0),
        },
        "gps": None,
        "attitude": {
            "distance_m": telemetry.get("distance_m", 0.0),
            "roll_deg": telemetry.get("roll_deg", 0.0),
            "pitch_deg": telemetry.get("pitch_deg", 0.0),
            "yaw_deg": telemetry.get("yaw_deg", 0.0),
            "cone_violation": telemetry.get("cone_violation", False),
        },
        "panel": None,
        "ramp": {
            "current_pct": telemetry.get("commanded_pct", 0),
            "level_str": telemetry.get("ramp_level_str", "0/0"),
        },
        "laser": {
            "connected": telemetry.get("laser_connected", False),
            "avg_power_w": telemetry.get("laser_avg_power_w", 0.0),
            "status_flags": telemetry.get("laser_status_flags", {}),
        },
        "status": state.status,
        "session_id": state.session_id or "",
        "scenario": state.scenario,
        "server_ts_ms": int(time.time() * 1000)
    }
    await websocket.send_json(initial_payload)
    
    try:
        while True:
            print(f"[WS] Loop start - connections: {len(active_connections)}, auth: {settings.ENABLE_AUTH}")
            telemetry = await state.get_telemetry_snapshot()
            print("[WS] Got telemetry snapshot")
            rtt_p95, rtt_p99 = await state.calculate_rtt_percentiles()
            print(f"[WS] Got RTT percentiles: p95={rtt_p95}, p99={rtt_p99}")

            # Panel calculation
            print("[WS] Starting panel calculation")
            from backend.state import calculate_panel_angles, calculate_horizontal_distance
            panel_data = {}
            print("[WS] Panel data initialized")
            if (telemetry.get("gps_lat_deg") is not None and telemetry.get("home_lat_deg") is not None):
                print("[WS] GPS data present, calculating panel angles")
                try:
                    h_dist = telemetry.get("distance_m", 0.0)
                    if h_dist < 0.1:
                        print("[WS] Calling calculate_horizontal_distance")
                        h_dist = calculate_horizontal_distance(
                            telemetry["gps_lat_deg"], telemetry["gps_lon_deg"],
                            telemetry["home_lat_deg"], telemetry["home_lon_deg"]
                        )
                    print("[WS] Calling calculate_panel_angles")
                    panel_data = calculate_panel_angles(
                        drone_lat=telemetry["gps_lat_deg"],
                        drone_lon=telemetry["gps_lon_deg"],
                        drone_alt_m=telemetry.get("gps_rel_alt_m", 0.0),
                        ground_lat=telemetry["home_lat_deg"],
                        ground_lon=telemetry["home_lon_deg"],
                        drone_yaw_deg=telemetry.get("yaw_deg", 0.0),
                        horizontal_dist_m=h_dist,
                        drone_roll_deg=telemetry.get("roll_deg", 0.0),
                        drone_pitch_deg=telemetry.get("pitch_deg", 0.0),
                    )
                except Exception as e:
                    # Keep this lightweight; don't spam tracebacks every tick.
                    print(f"[Panel] Angle calc error: {e}")

            print("[WS] Building message dict")
            message = {
                "type": "telemetry",
                "ts": int(time.time() * 1000),
                "power": {
                    "commanded_w": telemetry.get("commanded_w", 0.0),
                    "received_mw": telemetry.get("received_mw", 0.0),
                    "efficiency_pct": telemetry.get("efficiency_pct", 0.0),
                },
                "link": {
                    "quality_pct": telemetry.get("link_quality_pct", 0),
                    "rtt_ms": telemetry.get("rtt_ms", 0.0),
                    "rtt_p95_ms": rtt_p95,
                    "rtt_p99_ms": rtt_p99,
                },
                "permit": {
                    "granted": telemetry.get("granted", False),
                    "deny_reason": telemetry.get("deny_reason"),
                    "grants_total": telemetry.get("grants_total", 0),
                    "denies_total": telemetry.get("denies_total", 0),
                    "seq": telemetry.get("seq", 0),
                    "bypass_enabled": settings.BYPASS_PHOTON_HANDSHAKE,
                },
                "battery": {
                    "voltage_mv": telemetry.get("voltage_mv") or telemetry.get("px4_voltage_mv", 0),
                    "current_ma": telemetry.get("current_ma") or telemetry.get("px4_current_ma", 0),
                    "soc_pct": telemetry.get("soc_pct", 0.0),
                    "temp_cdeg": telemetry.get("temp_cdeg", 0),
                },
                "gps": {
                    "lat_deg": telemetry.get("gps_lat_deg"),
                    "lon_deg": telemetry.get("gps_lon_deg"),
                    "alt_m": telemetry.get("gps_alt_m"),
                    "rel_alt_m": telemetry.get("gps_rel_alt_m"),
                } if telemetry.get("gps_lat_deg") is not None else None,
                "attitude": {
                    "distance_m": telemetry.get("distance_m", 0.0),
                    "roll_deg": telemetry.get("roll_deg", 0.0),
                    "pitch_deg": telemetry.get("pitch_deg", 0.0),
                    "yaw_deg": telemetry.get("yaw_deg", 0.0),
                    "cone_violation": telemetry.get("cone_violation", False),
                },
                "panel": panel_data if panel_data else None,
                "ramp": {
                    "current_pct": telemetry.get("commanded_pct", 0),
                    "level_str": telemetry.get("ramp_level_str", "0/0"),
                },
                "laser": {
                    "connected": telemetry.get("laser_connected", False),
                    "avg_power_w": telemetry.get("laser_avg_power_w", 0.0),
                    "peak_power_w": telemetry.get("laser_peak_power_w", 0.0),
                    "case_temperature_c": telemetry.get("laser_case_temperature_c", 0.0),
                    "board_temperature_c": telemetry.get("laser_board_temperature_c", 0.0),
                    "setpoint_pct": telemetry.get("laser_setpoint_pct", 0.0),
                    "status_flags": telemetry.get("laser_status_flags", {}),
                    "status_word": telemetry.get("laser_status_word", 0),
                    "device_id": telemetry.get("laser_device_id", "Unknown"),
                    "firmware_revision": telemetry.get("laser_firmware_revision", "Unknown"),
                    "error": telemetry.get("laser_error"),

                    # Legacy aliases for backward compatibility
                    "output_power_w": telemetry.get("laser_avg_power_w", 0.0),
                    "temperature_c": telemetry.get("laser_case_temperature_c", 0.0),
                    "emission_on": telemetry.get("laser_status_flags", {}).get("emission_on", False),
                    "power_supply_on": telemetry.get("laser_status_flags", {}).get("power_supply_on", False),
                    "alarm_critical": telemetry.get("laser_status_flags", {}).get("alarm_critical", False),
                    "alarm_overheat": telemetry.get("laser_status_flags", {}).get("alarm_overheat", False),
                },
                "status": state.status,
                "session_id": state.session_id or "",
                "scenario": state.scenario,
                "server_ts_ms": int(time.time() * 1000)
            }

            print(f"[WS] Sending laser data: connected={message['laser']['connected']}, power={message['laser']['avg_power_w']}W")

            print(f"[WS] Message built, size: {len(str(message))} chars")
            await websocket.send_json(message)
            print("[WS] Message sent successfully")
            await asyncio.sleep(period)

    except (WebSocketDisconnect, Exception):
        if websocket in active_connections:
            active_connections.remove(websocket)
    finally:
        await state.add_event(
            "INFO",
            "server",
            "WS_DISCONNECT",
            f"Client disconnected. Remaining: {len(active_connections)}"
        )

# ════════════════════════════════════════════════════════════════
# RUN SERVER
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    from backend.ssl_manager import ssl_manager

    if settings.ENABLE_HTTPS:
        try:
            cert_file, key_file = ssl_manager.ensure_certificates()
            uvicorn.run(
                app,
                host=settings.HOST,
                port=settings.PORT,
                ssl_keyfile=str(key_file),
                ssl_certfile=str(cert_file),
            )
        except Exception:
            uvicorn.run(app, host=settings.HOST, port=settings.PORT)
    else:
        uvicorn.run(app, host=settings.HOST, port=settings.PORT)

