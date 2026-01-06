# state.py - Your reference guide

import asyncio
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Deque
from collections import deque
import time

import numpy as np

"""
PURPOSE: Thread-safe storage for real-time telemetry and events

WHY NEEDED:
- Parsers (reading process stdout) run in separate async tasks
- WebSocket handlers (sending to UI) run in separate async tasks
- Without synchronization → race conditions, data corruption

ARCHITECTURE:
- Single SharedState instance (singleton pattern)
- All data access goes through async methods with locks
- Ring buffers (deque) for bounded memory usage
"""

@dataclass
class SharedState:
    """
    Central Data store for the backend
    Dataclass so there is a celar structure
    All instances get there own Dequeue
    Async locks to ensure thread safety
    FastAPI uses asyncio
    """

    # ========================================================================
    # WEBSOCKET BROADCAST CALLBACK
    # ========================================================================
    # Function to broadcast events to connected WebSocket clients
    # Set by main.py after WebSocket server is initialized
    ws_broadcast: Optional[callable] = None

    # ========================================================================
    # PROCESS LIFECYCLE
    # ========================================================================
    # Check if the process is running
    status: str = "DISCONNECTED"  # DISCONNECTED, CONNECTING, READY, RAMPING, STOPPING, SAFE

    process_pids: Dict[str, Optional[int]] = field(default_factory=lambda: {
        "ground": None,
        "air": None,
        "relay": None
    })

    # Need PIDs to kill processes on shutdown

    # ========================================================================
    # CURRENT SESSION
    # ========================================================================
    # Track whats running, CSV for export
    session_id: Optional[str] = None  # ✓ Now has a default
    scenario: str = "Unknown"
    ramp_params: Optional[Dict] = None
    session_start_time: float = 0.0  # time.time() when session started

    # ========================================================================
    # TELEMETRY BUFFERS
    # ========================================================================
    # WebSocket sends latest snapshots not history

    telemetry: Dict = field(default_factory=lambda: {
        # Power
        "commanded_pct": 0,
        "commanded_w": 0.0,
        "received_mw": 0.0,
        "efficiency_pct": 0.0,

        # Link
        "link_quality_pct": 0,
        "rtt_ms": 0.0,

        # Permit
        "granted": False,
        "deny_reason": None,
        "grants_total": 0,
        "denies_total": 0,
        "seq": 0,

        # Battery
        "voltage_mv": 0,
        "current_ma": 0,
        "soc_pct": 0.0,
        "temp_cdeg": 0,

        # Attitude
        "distance_m": 0.0,
        "roll_deg": 0.0,
        "pitch_deg": 0.0,
        "yaw_deg": 0.0,

        # GPS Position (from PX4)
        "gps_lat_deg": None,
        "gps_lon_deg": None,
        "gps_alt_m": None,
        "gps_rel_alt_m": None,
        "home_lat_deg": None,  # Ground station / home position
        "home_lon_deg": None,

        # Panel Gimbal Tracking (for laser power reception optimization)
        "panel_target_azimuth_deg": 0.0,     # Absolute bearing to ground station (world frame)
        "panel_target_elevation_deg": 0.0,    # Elevation angle to ground station (world frame)
        "panel_gimbal_azimuth_deg": 0.0,      # Gimbal azimuth in drone body frame
        "panel_gimbal_elevation_deg": 0.0,    # Gimbal elevation in drone body frame
        "panel_relative_azimuth_deg": 0.0,    # DEPRECATED: use gimbal_azimuth instead
        "panel_misalignment_deg": 0.0,        # True 3D misalignment angle (0° = perfect)
        "panel_efficiency_factor": 1.0,       # cos(misalignment), 1.0 = perfect alignment

        # Relay
        "relay_udp_to_ser_total": 0,
        "relay_ser_to_udp_total": 0,
    })

    # ========================================================================
    # STATISTICS BUFFERS
    # ========================================================================
    # UI shows p95/p99 RTT and not just average
    # Maxlen = 100 keeps last 10 seonds at 10 Hz 
    # Deque because O(1) append the automatic old data removal
    rtt_samples: Deque[float] = field(default_factory=lambda: deque(maxlen=100))

    # ========================================================================
    # EVENT LOGS
    # ========================================================================
    # UI shows recent GRANT/DENY events
    events: Deque[Dict] = field(default_factory=lambda: deque(maxlen=500))
    # So 1 minute of events at 10 Hz max

    # ========================================================================
    # ERROR LOGS
    # ========================================================================
    # UI shows recent ERROR/WARN events
    errors: Deque[Dict] = field(default_factory=lambda: deque(maxlen=50))
    # So 5 seconds of errors at 10 Hz max

    # ========================================================================
    # TIMESTAMPS
    # ========================================================================
    last_telemetry_ts: float = 0.0  # time.time() of
    last_heartbeat_ts: float = 0.0  # time.time() of last heartbeat from air

    # ========================================================================
    # THREAD SAFETY
    # ========================================================================
    _lock: Optional[asyncio.Lock] = field(default=None, init=False)
    
    def __post_init__(self):
        """Initialize lock after instance creation"""
        # Only create lock if not already set (allows testing with mock locks)
        if self._lock is None:
            try:
                self._lock = asyncio.Lock()
            except RuntimeError:
                # No event loop yet - will be created on first use
                pass
    
    async def _ensure_lock(self):
        """Ensure lock exists (creates if needed)"""
        if self._lock is None or not isinstance(self._lock, asyncio.Lock):
            self._lock = asyncio.Lock()
        return self._lock

    # ========================================================================
    # METHODS: Telemetry
    # ========================================================================
    async def update_telemetry(self, data: Dict):
        lock = await self._ensure_lock()
        async with lock:
            self.telemetry.update(data)
            self.last_telemetry_ts = time.time()
            if "rtt_ms" in data and data["rtt_ms"] > 0:
                self.rtt_samples.append(data["rtt_ms"])
    
    async def get_telemetry_snapshot(self) -> Dict:
        lock = await self._ensure_lock()
        async with lock:
            return self.telemetry.copy()
        
    # ========================================================================
    # METHODS: Events
    # ========================================================================
    async def add_event(self, level: str, src: str, code: str, msg: str):
        """ Add an event log entry in a thread-safe manner
        Structure
        - ts: For sorting and displaying time
        - level: INFO, WARN, ERROR for filtering
        - src: For debugging (ground, air, relay, runner, server)
        - code: Short code for programmatic filtering (GRANT, DENY, PX4
        - msg: Human-readable message (truncated to 200 chars)
        """
        lock = await self._ensure_lock()
        async with lock:
            event = {
                "ts": int(time.time() * 1000),
                "level": level,
                "src": src,
                "code": code,
                "msg": msg[:200]
            }
            self.events.append(event)
            if level in ["ERROR", "WARN"]:
                self.errors.append(event)

        # Broadcast event to WebSocket clients (outside lock to avoid deadlock)
        if self.ws_broadcast:
            try:
                self.ws_broadcast({
                    "type": "event",
                    "event": event
                })
            except Exception as e:
                # Don't crash if broadcast fails
                print(f"[State] Failed to broadcast event: {e}")
    
    async def get_recent_events(self, count: int = 50) -> List[Dict]:
        lock = await self._ensure_lock()
        async with lock:
            return list(self.events)[-count:]
        
    # ========================================================================
    # METHODS: Status
    # ========================================================================
    async def set_status(self, new_status: str):
        lock = await self._ensure_lock()
        async with lock:
            old_status = self.status
            self.status = new_status
        
        # Call add_event outside lock to avoid deadlock
        await self.add_event(
            "INFO", "server", "STATUS_CHANGE", 
            f"Status changed from {old_status} to {new_status}"
        )
    
    async def set_process_pid(self, process_name: str, pid: Optional[int]):
        lock = await self._ensure_lock()
        async with lock:
            self.process_pids[process_name] = pid
    

    # ============================================================
    # METHODS: Statistics
    # ============================================================
    
    async def calculate_rtt_percentiles(self) -> tuple[float, float]:
        lock = await self._ensure_lock()
        async with lock:
            if len(self.rtt_samples) < 10:
                return 0.0, 0.0
            samples = np.array(list(self.rtt_samples))
            p95 = np.percentile(samples, 95)
            p99 = np.percentile(samples, 99)
            return float(p95), float(p99)
        
    # ============================================================
    # METHODS: Session Management
    # ============================================================
    
    async def start_session(self, session_id: str, scenario: str, params: Dict):
        lock = await self._ensure_lock()
        async with lock:
            self.session_id = session_id
            self.scenario = scenario
            self.ramp_params = params
            self.session_start_time = time.time()
            
            # Reset telemetry
            self.telemetry = {
                "commanded_pct": 0,
                "commanded_w": 0.0,
                "received_mw": 0.0,
                "efficiency_pct": 0.0,
                "link_quality_pct": 0,
                "rtt_ms": 0.0,
                "granted": False,
                "deny_reason": None,
                "grants_total": 0,
                "denies_total": 0,
                "seq": 0,
                "voltage_mv": 0,
                "current_ma": 0,
                "soc_pct": 0.0,
                "temp_cdeg": 0,
                "distance_m": 0.0,
                "roll_deg": 0.0,
                "pitch_deg": 0.0,
                "yaw_deg": 0.0,
                "relay_udp_to_ser_total": 0,
                "relay_ser_to_udp_total": 0,
            }
            
            self.events.clear()
            self.rtt_samples.clear()
            self.errors.clear()
    
    async def get_session_duration(self) -> float:
        lock = await self._ensure_lock()
        async with lock:
            if self.session_start_time == 0.0:
                return 0.0
            return time.time() - self.session_start_time

# ============================================================
# PANEL GIMBAL TRACKING CALCULATIONS
# ============================================================

def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate bearing from point 1 to point 2 in degrees (0-360°)
    0° = North, 90° = East, 180° = South, 270° = West
    """
    import math
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlon_rad = math.radians(lon2 - lon1)

    y = math.sin(dlon_rad) * math.cos(lat2_rad)
    x = math.cos(lat1_rad) * math.sin(lat2_rad) - \
        math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(dlon_rad)

    bearing_rad = math.atan2(y, x)
    bearing_deg = (math.degrees(bearing_rad) + 360) % 360
    return bearing_deg


def calculate_horizontal_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate horizontal distance between two GPS coordinates using Haversine formula.
    Returns distance in meters.
    """
    import math

    # Earth radius in meters
    R = 6371000.0

    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlat_rad = math.radians(lat2 - lat1)
    dlon_rad = math.radians(lon2 - lon1)

    a = math.sin(dlat_rad / 2) ** 2 + \
        math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon_rad / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    distance_m = R * c
    return distance_m


def calculate_elevation_angle(horizontal_dist_m: float, altitude_diff_m: float) -> float:
    """
    Calculate elevation angle from drone to ground station
    Positive = looking down, Negative = looking up
    """
    import math
    if horizontal_dist_m < 0.1:  # Avoid division by zero
        return -90.0 if altitude_diff_m > 0 else 0.0

    # Elevation angle (negative because drone looks DOWN at ground)
    elevation_rad = math.atan2(-altitude_diff_m, horizontal_dist_m)
    return math.degrees(elevation_rad)


def calculate_panel_angles(
    drone_lat: float, drone_lon: float, drone_alt_m: float,
    ground_lat: float, ground_lon: float,
    drone_yaw_deg: float, horizontal_dist_m: float,
    drone_roll_deg: float = 0.0, drone_pitch_deg: float = 0.0
) -> dict:
    """
    Calculate 2-axis gimbal angles for panel to point at ground station.
    Simple azimuth-only tracking - compensates for drone yaw rotation.

    Returns dict with gimbal angles, misalignment, and efficiency.
    """
    import math

    # Step 1: Calculate target direction in world frame
    target_azimuth = calculate_bearing(drone_lat, drone_lon, ground_lat, ground_lon)
    altitude_diff = drone_alt_m
    target_elevation = calculate_elevation_angle(horizontal_dist_m, altitude_diff)

    # Step 2: Calculate gimbal azimuth (compensate for drone yaw)
    gimbal_azimuth_deg = target_azimuth - drone_yaw_deg
    while gimbal_azimuth_deg > 180:
        gimbal_azimuth_deg -= 360
    while gimbal_azimuth_deg < -180:
        gimbal_azimuth_deg += 360

    # Step 3: Gimbal elevation (direct tilt)
    gimbal_elevation_deg = target_elevation

    # Step 4: Apply mechanical limits
    GIMBAL_EL_MIN = -45.0
    GIMBAL_EL_MAX = 45.0
    
    gimbal_limited = False
    actual_elevation_deg = gimbal_elevation_deg
    
    if gimbal_elevation_deg < GIMBAL_EL_MIN:
        gimbal_limited = True
        actual_elevation_deg = GIMBAL_EL_MIN
    elif gimbal_elevation_deg > GIMBAL_EL_MAX:
        gimbal_limited = True
        actual_elevation_deg = GIMBAL_EL_MAX

    # Step 5: Calculate misalignment
    if gimbal_limited:
        misalignment_deg = abs(target_elevation - actual_elevation_deg)
    else:
        misalignment_deg = 0.0

    # Step 6: Calculate efficiency
    efficiency_factor = max(0.0, math.cos(math.radians(misalignment_deg)))

    return {
        "panel_target_azimuth_deg": target_azimuth,
        "panel_target_elevation_deg": target_elevation,
        "panel_gimbal_azimuth_deg": gimbal_azimuth_deg,
        "panel_gimbal_elevation_deg": actual_elevation_deg,
        "panel_gimbal_limited": gimbal_limited,
        "panel_relative_azimuth_deg": gimbal_azimuth_deg,
        "panel_misalignment_deg": misalignment_deg,
        "panel_efficiency_factor": efficiency_factor,
    }



# ============================================================
# SINGLETON INSTANCE
# ============================================================
state = SharedState()

"""
Why singleton?
- Only one instance of state should exist
- All modules import the same instance
- Ensures data consistency

Usage in other files:
    from backend.state import state
    
    # Update telemetry:
    await state.update_telemetry({"commanded_w": 100.0})
    
    # Read telemetry:
    data = await state.get_telemetry_snapshot()
    
    # Add event:
    await state.add_event("WARN", "air", "DENY", "PX4 not OK")
"""