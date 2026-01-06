# backend/api_laser.py
"""
Laser control API endpoints.

Provides REST API for:
- Querying laser status (power, temperature, alarms)
- Enabling/disabling laser emission
- Setting laser power setpoint
- Safety interlocks and health monitoring
"""

from fastapi import APIRouter, HTTPException, Depends
from backend.models import (
    LaserStatusResponse,
    LaserEnableRequest,
    LaserSetpointRequest,
    LaserStatusFlags,
)
from backend.config import settings
from backend.auth.dep import require_roles
import sys
import os

# Add Laser directory to path to import laser_decoder
sys.path.insert(0, str(settings.PROJECT_ROOT.parent / "Laser"))

try:
    from laser_decoder import LaserStatusDecoder
except ImportError:
    LaserStatusDecoder = None
    print("[WARNING] laser_decoder.py not found - laser endpoints will be unavailable")

router = APIRouter(prefix="/laser", tags=["Laser Control"])

# Global laser decoder instance
_laser_decoder = None


def get_laser_decoder() -> LaserStatusDecoder:
    """Get or create laser decoder singleton."""
    global _laser_decoder
    if _laser_decoder is None:
        if LaserStatusDecoder is None:
            raise HTTPException(
                status_code=503,
                detail="Laser control not available (laser_decoder.py not found)"
            )
        try:
            _laser_decoder = LaserStatusDecoder(
                ip=settings.LASER_IP,
                port=settings.LASER_PORT,
                config_path=str(settings.LASER_CONFIG_PATH)
            )
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to initialize laser decoder: {e}"
            )
    return _laser_decoder


@router.get("/status", response_model=LaserStatusResponse)
async def get_laser_status():
    """
    Get current laser telemetry.

    Returns:
        - Output power (W)
        - Case temperature (°C)
        - Current setpoint (%)
        - Status flags (emission on, alarms, warnings)
        - Connection status
    """
    try:
        decoder = get_laser_decoder()
        telemetry = decoder.get_laser_telemetry()

        # Convert to Pydantic model
        return LaserStatusResponse(
            connection_status=telemetry.get("connection_status", "Disconnected"),
            output_power_watts=telemetry.get("output_power_watts", 0.0),
            temperature_c=telemetry.get("temperature_c", 0.0),
            setpoint_percent=telemetry.get("setpoint_percent", 0.0),
            status_flags=LaserStatusFlags(**telemetry.get("status_flags", {})),
            error=telemetry.get("error")
        )
    except HTTPException:
        raise
    except Exception as e:
        # Return disconnected status instead of failing
        return LaserStatusResponse(
            connection_status="Disconnected",
            output_power_watts=0.0,
            temperature_c=0.0,
            setpoint_percent=0.0,
            status_flags=LaserStatusFlags(),
            error=str(e)
        )


@router.post("/enable")
async def enable_laser(
    req: LaserEnableRequest,
    user=Depends(require_roles("DEVELOPER", "ADMIN"))
):
    """
    Enable or disable laser emission.

    **Requires DEVELOPER or ADMIN role.**

    Args:
        req: Enable/disable request with optional target power

    Returns:
        Status confirmation

    Safety Notes:
        - Laser will only emit if tracking is active
        - Safety interlocks must be satisfied
        - Check alarm flags before enabling
    """
    try:
        decoder = get_laser_decoder()

        # Check connection
        telemetry = decoder.get_laser_telemetry()

        if telemetry.get("connection_status") != "Connected":
            raise HTTPException(
                status_code=503,
                detail="Cannot control laser: not connected"
            )

        # Check for critical alarms
        flags = telemetry.get("status_flags", {})
        if flags.get("alarm_critical") and req.enable:
            raise HTTPException(
                status_code=400,
                detail="Cannot enable laser: critical alarm active"
            )

        # Execute enable or disable command
        if req.enable:
            # Set power setpoint first if specified
            if req.target_power_percent is not None:
                setpoint_result = decoder.set_power_setpoint(req.target_power_percent)
                if not setpoint_result.get("success"):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Failed to set power: {setpoint_result.get('message')}"
                    )

            # Enable emission
            result = decoder.enable_emission()
            if not result.get("success"):
                raise HTTPException(
                    status_code=400,
                    detail=result.get("message", "Failed to enable laser")
                )

            return {
                "status": "ok",
                "enabled": True,
                "target_power_percent": req.target_power_percent,
                "message": result.get("message", "Laser enabled")
            }
        else:
            # Disable emission
            result = decoder.disable_emission()
            if not result.get("success"):
                raise HTTPException(
                    status_code=400,
                    detail=result.get("message", "Failed to disable laser")
                )

            return {
                "status": "ok",
                "enabled": False,
                "target_power_percent": 0,
                "message": result.get("message", "Laser disabled")
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to control laser: {e}"
        )


@router.post("/setpoint")
async def set_laser_setpoint(
    req: LaserSetpointRequest,
    user=Depends(require_roles("DEVELOPER", "ADMIN"))
):
    """
    Set laser power setpoint.

    **Requires DEVELOPER or ADMIN role.**

    Args:
        req: Setpoint request (0-100%)

    Returns:
        Status confirmation

    Safety Notes:
        - Setpoint change may take several seconds
        - Power will ramp smoothly to new setpoint
        - Monitor temperature during high-power operation
    """
    try:
        decoder = get_laser_decoder()

        # Check connection
        telemetry = decoder.get_laser_telemetry()
        if telemetry.get("connection_status") != "Connected":
            raise HTTPException(
                status_code=503,
                detail="Cannot set setpoint: laser not connected"
            )

        # Send setpoint command
        result = decoder.set_power_setpoint(req.setpoint_percent)

        if not result.get("success"):
            raise HTTPException(
                status_code=400,
                detail=result.get("message", "Failed to set setpoint")
            )

        return {
            "status": "ok",
            "setpoint_percent": result.get("setpoint", req.setpoint_percent),
            "message": result.get("message", f"Laser setpoint set to {req.setpoint_percent}%")
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to set laser setpoint: {e}"
        )


@router.get("/health")
async def laser_health_check():
    """
    Quick health check endpoint.

    Returns basic connection status without full telemetry query.
    Useful for monitoring and UI status indicators.
    """
    try:
        decoder = get_laser_decoder()
        telemetry = decoder.get_laser_telemetry()

        connected = telemetry.get("connection_status") == "Connected"
        flags = telemetry.get("status_flags", {})
        has_alarms = any([
            flags.get("alarm_critical", False),
            flags.get("alarm_overheat", False),
            flags.get("alarm_back_reflection", False),
            flags.get("alarm_fiber_break", False),
        ])

        return {
            "connected": connected,
            "healthy": connected and not has_alarms,
            "emission_on": flags.get("emission_on", False),
            "power_watts": telemetry.get("output_power_watts", 0.0),
            "alarms_active": has_alarms
        }
    except Exception as e:
        return {
            "connected": False,
            "healthy": False,
            "emission_on": False,
            "power_watts": 0.0,
            "alarms_active": False,
            "error": str(e)
        }

