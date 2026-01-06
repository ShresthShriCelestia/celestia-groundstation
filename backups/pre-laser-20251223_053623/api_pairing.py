"""
Pairing and Device Information API
Provides endpoints for device pairing and discovery
"""
from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel
from typing import Optional, List
import uuid
import platform
import socket

from backend.pairing import pairing_manager, PairedDevice
from backend.config import settings

router = APIRouter(prefix="/api/device", tags=["Device & Pairing"])


# ═══════════════════════════════════════════════════════════════════
# Request/Response Models
# ═══════════════════════════════════════════════════════════════════

class DeviceInfoResponse(BaseModel):
    """Device information for discovery"""
    name: str
    model: str
    hardware_id: str
    version: str
    local_ip: str
    hostname: str
    platform: str
    pairing_required: bool


class StartPairingRequest(BaseModel):
    """Request to start pairing mode"""
    timeout_seconds: int = 300  # 5 minutes default


class StartPairingResponse(BaseModel):
    """Response with pairing code"""
    pairing_code: int
    expires_in_seconds: int
    message: str


class PairDeviceRequest(BaseModel):
    """Request to pair a device"""
    pairing_code: int
    device_name: str
    device_type: str = "browser"  # "browser", "mobile", "desktop_app"


class PairDeviceResponse(BaseModel):
    """Response with authentication token"""
    token: str
    message: str


class PairingStatusResponse(BaseModel):
    """Current pairing system status"""
    pairing_active: bool
    pairing_expires_at: Optional[str]
    paired_device_count: int
    paired_devices: List[dict]


# ═══════════════════════════════════════════════════════════════════
# Helper Functions
# ═══════════════════════════════════════════════════════════════════

def get_local_ip() -> str:
    """Get the local IP address"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_hardware_id() -> str:
    """Get or generate hardware ID"""
    # In production, this would read from a persistent file
    # For now, generate a unique ID
    if not settings.HARDWARE_ID:
        # Generate once and save
        hardware_id = str(uuid.uuid4())[:8].upper()
        settings.HARDWARE_ID = f"GS-{hardware_id}"
    return settings.HARDWARE_ID


async def verify_auth_token(authorization: Optional[str] = Header(None)) -> PairedDevice:
    """Dependency to verify authentication token"""
    if not authorization:
        raise HTTPException(status_code=401, detail="No authorization header")

    # Extract token from "Bearer <token>"
    token = authorization.replace("Bearer ", "").strip()

    device = pairing_manager.verify_token(token)
    if not device:
        raise HTTPException(status_code=403, detail="Invalid or expired token")

    return device


# ═══════════════════════════════════════════════════════════════════
# Public Endpoints (No Auth Required)
# ═══════════════════════════════════════════════════════════════════

@router.get("/info", response_model=DeviceInfoResponse)
async def get_device_info(authorization: Optional[str] = Header(None)):
    """
    Get device information for discovery.
    This endpoint is public so frontends can discover and identify devices.

    If an Authorization header is provided, checks if THIS specific device is paired.
    If no Authorization header, assumes new device and returns pairing_required=True.
    """
    # Check if this specific device is paired (based on token)
    pairing_required = True  # Default: assume pairing needed

    if authorization:
        # Extract token from "Bearer <token>"
        token = authorization.replace("Bearer ", "").strip()
        device = pairing_manager.verify_token(token)
        if device:
            # This device has a valid pairing token, no pairing needed
            pairing_required = False

    return DeviceInfoResponse(
        name=settings.DEVICE_NAME,
        model=settings.DEVICE_MODEL,
        hardware_id=get_hardware_id(),
        version="1.0.0",
        local_ip=get_local_ip(),
        hostname=socket.gethostname(),
        platform=platform.system(),
        pairing_required=pairing_required
    )


@router.post("/pair", response_model=PairDeviceResponse)
async def pair_device(request: PairDeviceRequest):
    """
    Pair a new device using a pairing code.
    Returns an authentication token that should be stored by the client.
    """
    token = pairing_manager.pair_device(
        code=request.pairing_code,
        device_name=request.device_name,
        device_type=request.device_type
    )

    if not token:
        raise HTTPException(
            status_code=400,
            detail="Invalid pairing code or pairing mode not active"
        )

    return PairDeviceResponse(
        token=token,
        message=f"Device '{request.device_name}' paired successfully"
    )


# ═══════════════════════════════════════════════════════════════════
# PAIRING ENDPOINTS REMOVED FOR SECURITY
# ═══════════════════════════════════════════════════════════════════
# 
# All pairing mode endpoints have been removed to prevent unauthorized access.
# Pairing codes can ONLY be generated via CLI on the device itself:
#   python cli_pairing.py start
#
# This ensures only users with physical or SSH access can enable pairing.
# ═══════════════════════════════════════════════════════════════════
