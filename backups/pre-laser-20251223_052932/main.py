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
from backend.auth.router import router as auth_router
from backend.auth.jwt import decode_token as decode_token
from backend.auth.init_db import init_db  # add near other imports
from backend.api_pairing import router as pairing_router  # Pairing & device info



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
    """
    Adds security headers to all HTTP responses.

    Headers added:
    - X-Content-Type-Options: Prevents MIME type sniffing
    - X-Frame-Options: Prevents clickjacking attacks
    - X-XSS-Protection: Enables browser XSS filter
    - Strict-Transport-Security: Forces HTTPS (production only)
    - Referrer-Policy: Controls referrer information leakage
    - Permissions-Policy: Disables unnecessary browser features
    - Content-Security-Policy: Comprehensive XSS protection
    """

    async def dispatch(self, request: Request, call_next):
        # Process the request and get the response
        response = await call_next(request)

        # Add security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"

        # Only add HSTS in production (HTTPS environments)
        # Don't add on localhost because it breaks HTTP development
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains; preload"
            )

        # Control referer information
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # Disable browser features that aren't needed
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=()"
        )

        # Content Security Policy
        # Note: 'unsafe-inline' and 'unsafe-eval' are needed for Vite dev mode
        # In production, you should remove these for maximum security
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

"""
Purpose: Main FastAPI application for the backend server.
FLOW:
1. Server starts → lifespan startup
2. Frontend connects → WebSocket established
3. User clicks "Start" → POST /ramp/start
4. Supervisor spawns processes
5. Parsers update state
6. WebSocket broadcasts telemetry
7. User clicks "Stop" → POST /ramp/stop
8. Supervisor kills processes
9. Server stops → lifespan shutdown
"""


# ════════════════════════════════════════════════════════════════
# LIFESPAN MANAGEMENT
# ════════════════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("="*70)
    print("LASER POWER BEAMING BACKEND - STARTING")
    print("="*70)
    print(f"Server: {settings.HOST}:{settings.PORT}")
    print(f"Data directory: {settings.DATA_DIR}")
    print(f"Ground script: {settings.GROUND_SCRIPT}")
    print(f"Air script: {settings.AIR_SCRIPT}")
    print(f"Relay script: {settings.RELAY_SCRIPT}")
    print("="*70)

    try:
        settings.validate_scripts_exist()
        print("All required scripts found.")
    except FileNotFoundError as e:
        print(f"Error: {e}")
        raise

    settings.ensure_data_dir()
    print(f"Data directory ready.: {settings.DATA_DIR}")

    await state.set_status("DISCONNECTED")
    await state.add_event(
        "INFO", "server", "SERVER_START",
        f"Backend started on {settings.HOST}:{settings.PORT}"
    )

    init_db()

    # Auto-connect to PX4 for GPS telemetry
    print("Attempting to connect to PX4...")
    try:
        await supervisor.px4_connect()
        print("✓ PX4 connected - GPS telemetry active")
    except Exception as e:
        print(f"⚠ PX4 connection failed: {e}")
        print("  (GPS data will not be available)")

    print("Backend ready")
    print("="*70)

    # --- SERVER RUNS HERE ---
    yield
    # --- SERVER SHUTTING DOWN ---

    print("\n" + "="*70)
    print("LASER POWER BEAMING BACKEND - SHUTTING DOWN")
    print("="*70)

    if supervisor.is_running():
        print("Stopping all processes...")
        await supervisor.stop_all()
        print("All processes stopped")

    await state.add_event("INFO", "server", "SERVER_STOP", "Backend shutting down")
    print("Cleanup complete")
    print("="*70)

# ════════════════════════════════════════════════════════════════
# CREATE FASTAPI APP
# ════════════════════════════════════════════════════════════════

app = FastAPI(
    title="Laser Power Beaming API",
    description="Backend for laser power beaming experiments",
    version="1.0.0",
    lifespan=lifespan
)
app.include_router(px4_router)
app.include_router(auth_router)
app.include_router(pairing_router)  # Device pairing & discovery

# Add security headers to all responses
app.add_middleware(SecurityHeadersMiddleware)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS.split(","),  # Load from environment
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# Add Private Network Access (PNA) headers middleware
# This MUST be added after CORS to ensure headers are in the final response
# Allows public HTTPS sites to access localhost (Chrome Private Network Access)
class PrivateNetworkAccessMiddleware(BaseHTTPMiddleware):
    """
    Adds Access-Control-Allow-Private-Network header to enable
    Chrome's Private Network Access for cross-origin requests from
    public HTTPS sites to local/private network resources.
    """
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
        "docs": "/docs",
        "status": await state.get_telemetry_snapshot()
    }

@app.get("/health")
async def health_check():
    """
    Health check endpoint for monitoring and load balancers.

    Returns:
        - 200 OK if server is healthy
        - Basic system status

    Used by:
        - Docker HEALTHCHECK
        - Kubernetes liveness/readiness probes
        - Uptime monitoring services (UptimeRobot, Pingdom, etc.)
        - Load balancers (AWS ELB, Nginx, etc.)
    """
    return {
        "status": "healthy",
        "version": "1.0.0",
        "timestamp": int(time.time() * 1000),
        "system": {
            "connected_websockets": len(active_connections),
            "running_processes": len([p for p in supervisor.get_process_status().values() if p is not None]),
            "state_status": state.status,
        }
    }

@app.get("/status", response_model=SystemStatus)
async def get_status():
    errors = []
    async with state._lock:
        errors = [
            event["msg"] for event in list(state.events)[-10:]
            if event["level"] == "ERROR"
        ]

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
        raise HTTPException(status_code=400, detail="Ramp already in progress. Stop current ramp first.")

    if req.max_power_w > settings.MAX_POWER_W:
        raise HTTPException(status_code=400, detail=f"max_power_w exceeds hardware limit ({settings.MAX_POWER_W}W)")

    try:
        session_id = await supervisor.start_all(req)
        return {
            "status": "started",
            "session_id": session_id,
            "message": f"Ramp started: {req.min_power_pct}% to {req.max_power_pct}%"
        }
    except Exception as e:
        await state.add_event("ERROR", "server", "START_FAILED", f"Failed to start ramp: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to start ramp: {e}")

@app.post("/ramp/stop")
async def stop_ramp():
    if not supervisor.is_running():
        raise HTTPException(status_code=400, detail="No ramp is currently running")

    try:
        # Land the drone FIRST
        try:
            await supervisor.px4_land()
            print("[API] Drone landing initiated")
            await asyncio.sleep(3)  # Wait for landing
            print("[API] Drone landed")
        except Exception as e:
            print(f"[API] Landing failed: {e}")
            # Continue to stop processes anyway
        
        # Then stop all processes
        await supervisor.stop_all()
        
        return {"status": "stopped", "message": "Ramp stopped and drone landed"}
    except Exception as e:
        await state.add_event("ERROR", "server", "STOP_FAILED", f"Failed to stop ramp: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to stop ramp: {e}")

@app.post("/permit/config")
async def update_permit_config(req: PermitConfigRequest):
    return {
        "status": "updated",
        "message": "Permit config will apply to next ramp",
        "config": req.model_dump()
    }

@app.post("/drill/update")
async def update_drill_params(req: DrillUpdateRequest):
    return {
        "status": "acknowledged",
        "message": "Drill parameters updated (restart relay to apply)",
        "params": req.model_dump()
    }

@app.get("/events")
async def get_events(count: int = 50):
    return await state.get_recent_events(count)

# ════════════════════════════════════════════════════════════════
# WEBSOCKET ENDPOINTS
# ════════════════════════════════════════════════════════════════

active_connections: list[WebSocket] = []

async def _broadcast_ws(payload: dict):
    """Send JSON payload to all live WS clients; prune dead ones."""
    dead = []
    for ws in list(active_connections):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        with contextlib.suppress(Exception):
            active_connections.remove(ws)

# Connect broadcast callback to both supervisor and state
broadcast_func = lambda payload: asyncio.create_task(_broadcast_ws(payload))
supervisor.ws_broadcast = broadcast_func
state.ws_broadcast = broadcast_func


@app.websocket("/ws/telemetry")
async def websocket_telemetry(websocket: WebSocket):
    print(f"[WS] Client attempting to connect")

    # ── WS AUTH GUARD (before accept) ───────────────────────────
    token = websocket.query_params.get("token")
    if settings.ENABLE_AUTH:
        if token:
            payload = decode_token(token or "")
            roles = set((payload or {}).get("roles", []))
            if not payload or not roles.intersection({"VIEWER", "DEVELOPER", "ADMIN"}):
                await websocket.close(code=4403)
                print("[WS] Forbidden WS (bad token/roles)")
                return
        else:
            # No token → allow read-only viewers so the UI can render
            payload = {"roles": ["VIEWER"]}
    # ────────────────────────────────────────────────────────────


    await websocket.accept()
    active_connections.append(websocket)
    print(f"[WS] Client connected, total: {len(active_connections)}")

    await state.add_event(
        "INFO", "server", "WS_CONNECT",
        f"WebSocket client connected (total: {len(active_connections)})"
    )

    period = 1.0 / settings.WEBSOCKET_UPDATE_HZ


    try:
        while True:
            try:
                telemetry = await state.get_telemetry_snapshot()
                rtt_p95, rtt_p99 = await state.calculate_rtt_percentiles()

                # Calculate panel gimbal angles for laser tracking (full 3D)
                from backend.state import calculate_panel_angles, calculate_horizontal_distance
                panel_data = {}
                if (telemetry.get("gps_lat_deg") is not None and
                    telemetry.get("home_lat_deg") is not None):
                    try:
                        # Calculate horizontal distance: prefer air node data, fallback to GPS calculation
                        horizontal_dist_m = telemetry.get("distance_m", 0.0)  # Already in meters
                        if horizontal_dist_m < 0.1:  # No air node data, calculate from GPS
                            horizontal_dist_m = calculate_horizontal_distance(
                                telemetry["gps_lat_deg"], telemetry["gps_lon_deg"],
                                telemetry["home_lat_deg"], telemetry["home_lon_deg"]
                            )

                        panel_angles = calculate_panel_angles(
                            drone_lat=telemetry["gps_lat_deg"],
                            drone_lon=telemetry["gps_lon_deg"],
                            drone_alt_m=telemetry.get("gps_rel_alt_m", 0.0),
                            ground_lat=telemetry["home_lat_deg"],
                            ground_lon=telemetry["home_lon_deg"],
                            drone_yaw_deg=telemetry.get("yaw_deg", 0.0),
                            horizontal_dist_m=horizontal_dist_m,
                            drone_roll_deg=telemetry.get("roll_deg", 0.0),  # NEW: 3D gimbal
                            drone_pitch_deg=telemetry.get("pitch_deg", 0.0),  # NEW: 3D gimbal
                        )
                        panel_data = panel_angles
                    except Exception as e:
                        print(f"[WS] Panel angle calculation error: {e}")
                        import traceback
                        traceback.print_exc()

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
                        "grant_rate_pct": telemetry.get("grant_rate_pct", 0.0),
                        "seq": telemetry.get("seq", 0),
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
                        "rel_alt_m": telemetry.get("gps_rel_alt_m", 0.0),
                        "cone_violation": telemetry.get("cone_violation", False),
                    },
                    "panel": panel_data if panel_data else None,
                    "ramp": {
                        "current_pct": telemetry.get("commanded_pct", 0),
                        "level_str": telemetry.get("ramp_level_str", "0/0"),
                    },
                    "status": state.status,
                    "session_id": state.session_id or "",
                    "scenario": state.scenario,
                }

                # Send the message
                message["server_ts_ms"] = int(time.time() * 1000)
                await websocket.send_json(message)
                
                # Wait before next update
                await asyncio.sleep(period)

            except WebSocketDisconnect:
                print(f"[WS] Client disconnected during send")
                break
            except Exception as e:
                print(f"[WS] Error in send loop: {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
                break

    except Exception as e:
        print(f"[WS] Outer exception: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if websocket in active_connections:
            active_connections.remove(websocket)
        print(f"[WS] Client removed, remaining: {len(active_connections)}")
        
        await state.add_event("INFO", "server", "WS_DISCONNECT",
                              f"WebSocket client disconnected (remaining: {len(active_connections)})")

# ════════════════════════════════════════════════════════════════
# RUN SERVER
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    from backend.ssl_manager import ssl_manager

    # Generate or load SSL certificates if HTTPS is enabled
    if settings.ENABLE_HTTPS:
        try:
            cert_file, key_file = ssl_manager.ensure_certificates()
            print(f"\n🔒 HTTPS enabled with self-signed certificate")
            print(f"   Certificate: {cert_file}")
            print(f"   Backend URL: https://localhost:{settings.PORT}")
            print(f"\n⚠️  IMPORTANT: You'll need to accept the self-signed certificate in your browser")
            print(f"   Click 'Advanced' → 'Proceed to localhost' when prompted\n")

            uvicorn.run(
                app,
                host=settings.HOST,
                port=settings.PORT,
                log_level="info",
                ssl_keyfile=str(key_file),
                ssl_certfile=str(cert_file)
            )
        except Exception as e:
            print(f"\n❌ Failed to setup HTTPS: {e}")
            print(f"   Falling back to HTTP...\n")
            uvicorn.run(app, host=settings.HOST, port=settings.PORT, log_level="info")
    else:
        print(f"\n🔓 HTTPS disabled - running on HTTP")
        print(f"   Backend URL: http://localhost:{settings.PORT}\n")
        uvicorn.run(app, host=settings.HOST, port=settings.PORT, log_level="info")