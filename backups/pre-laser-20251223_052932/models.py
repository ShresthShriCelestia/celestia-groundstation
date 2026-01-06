# backend/models.py
from __future__ import annotations

from typing import Dict, List, Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


# ==============================================================================
# REQUEST MODELS (UI -> Server)
# ==============================================================================

class RampStartRequest(BaseModel):
    """
    Request to start a power ramp.
    Cross-field checks (min/max) are validated in a model_validator (v2 style).
    """
    min_power_pct: int = Field(ge=1, le=100, description="Starting power level (%)")
    max_power_pct: int = Field(ge=1, le=100, description="Ending power level (%)")
    step_pct: int = Field(ge=1, le=100, description="Step size between levels (%)")
    dwell_time_s: float = Field(ge=1.0, le=60.0, description="Time per power level (s)")
    max_power_w: float = Field(ge=0, description="Maximum power (watts)")
    scenario: str = Field(default="Hover", description="Flight scenario name")

    @model_validator(mode="after")
    def check_power_range(self) -> "RampStartRequest":
        if self.max_power_pct <= self.min_power_pct:
            raise ValueError("max_power_pct must be greater than min_power_pct")
        return self

    @field_validator("scenario")
    @classmethod
    def validate_scenario(cls, v: str) -> str:
        valid = [
            "Hover",
            "Hold",
            "Circle",
            "Square",
            "Figure8",
        ]
        if v not in valid:
            raise ValueError(f"scenario must be one of {valid}")
        return v


# Backwards-compat alias if any code still refers to RampRequest
RampRequest = RampStartRequest


class PermitConfigRequest(BaseModel):
    """
    POST /permit/config
    """
    send_hz: float = Field(ge=0.1, le=50.0, description="Frequency to send permits (Hz)")
    ttl_ms: int = Field(ge=200, le=2000, description="Permit time to live (ms)")
    duplicate: bool = Field(default=False, description="Send each permit twice for reliability")

    @model_validator(mode="after")
    def validate_ttl(self) -> "PermitConfigRequest":
        period_ms = 1000.0 / self.send_hz
        if self.ttl_ms < period_ms * 2:
            raise ValueError(f"ttl_ms must be at least {period_ms * 2:.1f} ms to avoid timeouts")
        return self


class DrillUpdateRequest(BaseModel):
    """
    POST /drill/update request body.
    """
    loss_pct: float = Field(ge=0.0, le=50.0, description="Packet loss rate (%)")
    delay_ms: int = Field(ge=0, le=500, description="Added latency (ms)")
    jitter_ms: int = Field(ge=0, le=100, description="Latency jitter (±ms)")
    dup_pct: float = Field(ge=0.0, le=20.0, description="Packet duplication rate (%)")
    reorder_pct: float = Field(ge=0.0, le=10.0, description="Packet reordering rate (%)")


# ==============================================================================
# RESPONSE MODELS (Server → UI)
# ==============================================================================

class TelemetryMessage(BaseModel):
    """
    WebSocket /ws/telemetry message. Matches the shape you build in main.py.
    """
    ts: int = Field(description="Unix timestamp (ms)")
    scenario: str
    session_id: str

    class RampStatus(BaseModel):
        current_pct: int
        current_w: float
        level_str: str  # e.g., "2/4"
        dwell_remaining_s: float

    class PowerStatus(BaseModel):
        commanded_w: float
        received_mw: float
        efficiency_pct: float

    class LinkStatus(BaseModel):
        quality_pct: int
        rtt_ms: float
        rtt_p95_ms: float
        rtt_p99_ms: float

    class PermitStatus(BaseModel):
        granted: bool
        deny_reason: Optional[str]
        grants_total: int
        denies_total: int
        grant_rate_pct: float
        seq: int

    class BatteryStatus(BaseModel):
        voltage_mv: int
        current_ma: int  # Negative = charging
        soc_pct: float
        temp_cdeg: int

    class AttitudeStatus(BaseModel):
        distance_m: float
        roll_deg: float
        pitch_deg: float
        yaw_deg: float
        cone_violation: bool  # True if |attitude| > 12°

    ramp: RampStatus
    power: PowerStatus
    link: LinkStatus
    permit: PermitStatus
    battery: BatteryStatus
    attitude: AttitudeStatus


class EventMessage(BaseModel):
    """
    WebSocket /ws/events message.
    """
    ts: int = Field(description="Unix timestamp (ms)")
    level: Literal["INFO", "WARN", "ERROR"]
    src: Literal["ground", "air", "relay", "runner", "server"]
    code: str  # "GRANT", "DENY", "PX4NotOK", "SeqWindow", etc.
    msg: str = Field(max_length=200, description="Human-readable message")


class SystemStatus(BaseModel):
    """
    GET /status response.
    """
    server_version: str
    status: Literal["DISCONNECTED", "CONNECTING", "READY", "RAMPING", "STOPPING", "SAFE"]
    processes: Dict[str, Optional[int]] = Field(description="PID and status of Ground/Air/Relay")
    last_telemetry_ts: Optional[int]
    errors: List[str]


class SessionInfo(BaseModel):
    """
    (Optional) GET /session/current response if you expose it.
    """
    run_id: str
    start_time: int  # Unix timestamp
    scenario: str
    status: str
    samples_logged: int
    csv_path: Optional[str]


# ==============================================================================
# PX4 / SCENARIO MODELS
# ==============================================================================

class PX4ConnectResponse(BaseModel):
    connected: bool
    address: str  # e.g., "udp://:14540"


class PX4ArmRequest(BaseModel):
    arm: bool = True


class PX4TakeoffRequest(BaseModel):
    altitude_m: float = Field(10.0, ge=1.0)


class PX4OffboardStartRequest(BaseModel):
    scenario: Literal[
        "Hover",
        "Hold",
        "Circle",
        "Square",
        "Figure8",
    ]
    send_hz: Optional[float] = Field(None, gt=0)


class PX4StatusResponse(BaseModel):
    connected: bool
    armed: bool
    in_offboard: bool
    scenario: Optional[str] = None
    takeoff_alt_m: Optional[float] = None


class RunExperimentRequest(BaseModel):
    """
    High-level "do it for me":
      1) connect + takeoff
      2) start offboard for scenario
      3) start ramp with given params
    """
    scenario: PX4OffboardStartRequest
    takeoff_alt_m: float = Field(10.0, ge=1.0)
    ramp: RampStartRequest


# Ensure forward refs (safe even with __future__.annotations)
RunExperimentRequest.model_rebuild()
TelemetryMessage.model_rebuild()