# backend/api_px4.py
from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends
from backend.supervisor import supervisor
from backend.config import settings
from backend.models import (
    PX4ConnectResponse, PX4ArmRequest, PX4TakeoffRequest,
    PX4OffboardStartRequest, PX4StatusResponse,
    RunExperimentRequest
)
from backend.auth.dep import require_roles

router = APIRouter(prefix="/px4", tags=["PX4 / Scenarios"])


# --- tiny helper to broadcast WS/status without exploding if not wired -------
def _ws(payload: dict):
    try:
        # expected to exist in your supervisor; if not, this no-ops
        supervisor.ws_broadcast(payload)
    except Exception:
        pass


def _px4_status_safe() -> PX4StatusResponse:
    s = getattr(supervisor, "px4", None)
    s = getattr(s, "status", None)
    if not s:
        # Not connected yet
        return PX4StatusResponse(
            connected=False, armed=False, in_offboard=False,
            scenario=None, takeoff_alt_m=None
        )
    return PX4StatusResponse(
        connected=s.connected,
        armed=s.armed,
        in_offboard=s.in_offboard,
        scenario=s.scenario_name,
        takeoff_alt_m=s.takeoff_alt_m,
    )


# --- basic controls -----------------------------------------------------------

@router.post("/connect", response_model=PX4ConnectResponse)
async def px4_connect():
    try:
        await supervisor.px4_connect()  # fast connect (≤3s) after your controller changes
        s = _px4_status_safe()
        return PX4ConnectResponse(
            connected=s.connected,
            address=getattr(settings, "PX4_MAVSDK_ADDR", "udp://:14540"),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PX4 connect failed: {e}")


@router.get("/status", response_model=PX4StatusResponse)
async def px4_status():
    return _px4_status_safe()


@router.post("/arm")
async def px4_arm(req: PX4ArmRequest, user=Depends(require_roles("DEVELOPER","ADMIN"))):
    try:
        await supervisor.px4_connect()
        if req.arm:
            await supervisor.px4.arm()
        else:
            await supervisor.px4.disarm()
        return {"armed": supervisor.px4.status.armed}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Arm/disarm failed: {e}")


@router.post("/takeoff")
async def px4_takeoff(req: PX4TakeoffRequest, user=Depends(require_roles("DEVELOPER","ADMIN"))):
    try:
        await supervisor.px4_takeoff(req.altitude_m)  # returns when airborne (not full alt)
        return {"ok": True, "altitude_m": req.altitude_m}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Takeoff failed: {e}")


@router.post("/offboard/start")
async def px4_offboard_start(req: PX4OffboardStartRequest, user=Depends(require_roles("DEVELOPER","ADMIN"))):
    try:
        await supervisor.px4_offboard_start(req.scenario, send_hz=req.send_hz)
        return {"ok": True, "scenario": req.scenario}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Offboard start failed: {e}")


@router.post("/offboard/stop")
async def px4_offboard_stop(user=Depends(require_roles("DEVELOPER","ADMIN"))):
    try:
        await supervisor.px4_offboard_stop()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Offboard stop failed: {e}")


@router.post("/land")
async def px4_land(user=Depends(require_roles("DEVELOPER","ADMIN"))):
    try:
        await supervisor.px4_land()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Land failed: {e}")


# --- one-button experiment (NON-BLOCKING) ------------------------------------

@router.post("/experiment/run", status_code=202)
async def run_experiment(req: RunExperimentRequest, bg: BackgroundTasks, user=Depends(require_roles("DEVELOPER","ADMIN"))):
    """
    Immediately returns 202 and performs:
      - PX4 connect (fast), takeoff (quick confirm), offboard start
      - Start ground/relay/air 'ramp' stack
    Streams status to UI via WS as phases complete.
    """
    try:
        if supervisor.is_running():
            raise HTTPException(status_code=400, detail="Ramp already in progress")

        # Tell the UI right away
        _ws({"type": "status", "status": "RAMPING", "phase": "BOOTSTRAP"})

        async def _do():
            try:
                # PX4 flight setup (each call should be fast after your controller edits)
                print(f"[Experiment] Starting with scenario: {req.scenario.scenario}")
                _ws({"type": "status", "status": "RAMPING", "phase": "PX4_CONNECTING"})
                await supervisor.px4_connect()
                print("[Experiment] PX4 connected")

                _ws({"type": "status", "status": "RAMPING", "phase": "PX4_TAKEOFF"})
                await supervisor.px4_takeoff(req.takeoff_alt_m)
                print(f"[Experiment] Takeoff to {req.takeoff_alt_m}m complete")

                _ws({"type": "status", "status": "RAMPING", "phase": "PX4_OFFBOARD_START"})
                print(f"[Experiment] Starting offboard mode: {req.scenario.scenario}, hz={req.scenario.send_hz}")
                await supervisor.px4_offboard_start(req.scenario.scenario, send_hz=req.scenario.send_hz)
                print(f"[Experiment] Offboard mode started successfully")

                # Start the full experiment stack (ELRS relay, air node, etc.)
                session_id = await supervisor.start_all(req.ramp)

                s = getattr(supervisor.px4, "status", None)
                _ws({
                    "type": "status",
                    "status": "RAMPING",
                    "phase": "RUNNING",
                    "session_id": session_id,
                    "px4": {
                        "armed": getattr(s, "armed", False),
                        "in_offboard": getattr(s, "in_offboard", False),
                        "scenario": getattr(s, "scenario_name", None),
                    },
                })

            except Exception as e:
                print(f"[Experiment] ERROR: {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
                # Best-effort cleanup if anything partially started
                try:
                    await supervisor.stop_all()
                except Exception:
                    pass
                _ws({
                    "type": "event",
                    "level": "ERROR",
                    "src": "BACKEND",
                    "code": "EXPERIMENT_START_FAILED",
                    "msg": str(e),
                })

        bg.add_task(_do)
        return {"status": "starting"}

    except HTTPException:
        # Forward explicit 4xx messages (e.g., already running)
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Experiment run failed: {e}")


@router.get("/ping")
async def ping():
    return {"ok": True}