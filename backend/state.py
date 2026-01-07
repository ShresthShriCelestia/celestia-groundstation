import asyncio
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Deque
from collections import deque
import time
import numpy as np

@dataclass
class SharedState:
    # WEBSOCKET BROADCAST CALLBACK
    ws_broadcast: Optional[callable] = None

    # PROCESS LIFECYCLE
    status: str = "DISCONNECTED"
    process_pids: Dict[str, Optional[int]] = field(default_factory=lambda: {
        "ground": None, "air": None, "relay": None
    })

    # CURRENT SESSION
    session_id: Optional[str] = None
    scenario: str = "Unknown"
    ramp_params: Optional[Dict] = None
    session_start_time: float = 0.0

    # TELEMETRY BUFFERS
    telemetry: Dict = field(default_factory=lambda: {
        "commanded_pct": 0, "commanded_w": 0.0, "received_mw": 0.0, "efficiency_pct": 0.0,
        "link_quality_pct": 0, "rtt_ms": 0.0, "granted": False, "deny_reason": None,
        "grants_total": 0, "denies_total": 0, "seq": 0, "voltage_mv": 0, "current_ma": 0,
        "soc_pct": 0.0, "temp_cdeg": 0, "distance_m": 0.0, "roll_deg": 0.0, "pitch_deg": 0.0,
        "yaw_deg": 0.0, "gps_lat_deg": None, "gps_lon_deg": None, "gps_alt_m": None,
        "gps_rel_alt_m": None, "home_lat_deg": None, "home_lon_deg": None,
        "panel_target_azimuth_deg": 0.0, "panel_target_elevation_deg": 0.0,
        "panel_gimbal_azimuth_deg": 0.0, "panel_gimbal_elevation_deg": 0.0,
        "panel_relative_azimuth_deg": 0.0, "panel_misalignment_deg": 0.0,
        "panel_efficiency_factor": 1.0, "relay_udp_to_ser_total": 0, "relay_ser_to_udp_total": 0,
        
        # Laser Status (complete telemetry)
        "laser_connected": False,
        "laser_avg_power_w": 0.0,
        "laser_peak_power_w": 0.0,
        "laser_case_temperature_c": 0.0,
        "laser_board_temperature_c": 0.0,
        "laser_setpoint_pct": 0.0,
        "laser_status_flags": {},
        "laser_status_word": 0,
        "laser_device_id": "Unknown",
        "laser_firmware_revision": "Unknown",
        "laser_emission_on": False,
        "laser_power_supply_on": False,
        "laser_alarm_critical": False,
        "laser_alarm_overheat": False,
        "laser_error": None,

        # Legacy aliases
        "laser_output_power_w": 0.0,
        "laser_temperature_c": 0.0,
    })

    rtt_samples: Deque[float] = field(default_factory=lambda: deque(maxlen=100))
    events: Deque[Dict] = field(default_factory=lambda: deque(maxlen=500))
    errors: Deque[Dict] = field(default_factory=lambda: deque(maxlen=50))
    last_telemetry_ts: float = 0.0
    last_heartbeat_ts: float = 0.0

    _lock: Optional[asyncio.Lock] = field(default=None, init=False)

    def __post_init__(self):
        if self._lock is None:
            try:
                self._lock = asyncio.Lock()
            except RuntimeError:
                pass
    
    async def _ensure_lock(self):
        if self._lock is None or not isinstance(self._lock, asyncio.Lock):
            self._lock = asyncio.Lock()
        return self._lock

    # ============================================================
    # CORE UPDATE METHODS
    # ============================================================

    async def update_telemetry(self, data: Dict):
        lock = await self._ensure_lock()
        async with lock:
            self.telemetry.update(data)
            self.last_telemetry_ts = time.time()
            if "rtt_ms" in data and data["rtt_ms"] > 0:
                self.rtt_samples.append(data["rtt_ms"])

    async def update_laser_telemetry(self, laser_data: Dict):
        """Update laser telemetry from laser status decoder."""
        lock = await self._ensure_lock()
        async with lock:
            # Extract status_flags for convenience
            status_flags = laser_data.get("status_flags", {})

            self.telemetry.update({
                "laser_connected": laser_data.get("connected", False),
                "laser_avg_power_w": laser_data.get("avg_power_w", 0.0),
                "laser_peak_power_w": laser_data.get("peak_power_w", 0.0),
                "laser_commanded_w": laser_data.get("commanded_w", 0.0),
                "laser_case_temperature_c": laser_data.get("case_temperature_c", 0.0),
                "laser_board_temperature_c": laser_data.get("board_temperature_c", 0.0),
                "laser_setpoint_pct": laser_data.get("setpoint_pct", 0.0),
                "laser_status_flags": status_flags,
                "laser_status_word": laser_data.get("status_word", 0),
                "laser_device_id": laser_data.get("device_id", "Unknown"),
                "laser_firmware_revision": laser_data.get("firmware_revision", "Unknown"),
                "laser_emission_on": status_flags.get("emission_on", False),
                "laser_power_supply_on": status_flags.get("power_supply_on", False),
                "laser_alarm_critical": status_flags.get("alarm_critical", False),
                "laser_alarm_overheat": status_flags.get("alarm_overheat", False),
                "laser_error": laser_data.get("error"),

                # Legacy aliases for backward compatibility
                "laser_output_power_w": laser_data.get("avg_power_w", 0.0),
                "laser_temperature_c": laser_data.get("case_temperature_c", 0.0),
            })

    async def start_session(self, session_id: str, scenario: str, params: Dict):
        lock = await self._ensure_lock()
        async with lock:
            self.session_id = session_id
            self.scenario = scenario
            self.ramp_params = params
            self.session_start_time = time.time()
            
            # Full reset including new laser keys
            self.telemetry = {
                "commanded_pct": 0, "commanded_w": 0.0, "received_mw": 0.0, "efficiency_pct": 0.0,
                "link_quality_pct": 0, "rtt_ms": 0.0, "granted": False, "deny_reason": None,
                "grants_total": 0, "denies_total": 0, "seq": 0, "voltage_mv": 0, "current_ma": 0,
                "soc_pct": 0.0, "temp_cdeg": 0, "distance_m": 0.0, "roll_deg": 0.0, "pitch_deg": 0.0,
                "yaw_deg": 0.0, "relay_udp_to_ser_total": 0, "relay_ser_to_udp_total": 0,
                "laser_connected": False, "laser_avg_power_w": 0.0, "laser_peak_power_w": 0.0,
                "laser_commanded_w": 0.0, "laser_case_temperature_c": 0.0, "laser_board_temperature_c": 0.0,
                "laser_setpoint_pct": 0.0, "laser_status_flags": {}, "laser_status_word": 0,
                "laser_device_id": "Unknown", "laser_firmware_revision": "Unknown",
                "laser_emission_on": False, "laser_power_supply_on": False,
                "laser_alarm_critical": False, "laser_alarm_overheat": False,
                "laser_error": None, "laser_output_power_w": 0.0, "laser_temperature_c": 0.0,
            }
            self.events.clear()
            self.rtt_samples.clear()
            self.errors.clear()

    # ============================================================
    # ADDED METHODS
    # ============================================================

    async def get_telemetry_snapshot(self) -> Dict:
        lock = await self._ensure_lock()
        async with lock:
            return self.telemetry.copy()

    async def add_event(self, level: str, src: str, code: str, msg: str):
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

        if self.ws_broadcast:
            try:
                self.ws_broadcast({"type": "event", "event": event})
            except Exception as e:
                print(f"[State] Failed to broadcast event: {e}")

    async def get_recent_events(self, count: int = 50) -> List[Dict]:
        lock = await self._ensure_lock()
        async with lock:
            return list(self.events)[-count:]

    async def set_status(self, new_status: str):
        lock = await self._ensure_lock()
        async with lock:
            old_status = self.status
            self.status = new_status
        await self.add_event("INFO", "server", "STATUS_CHANGE", f"Status changed from {old_status} to {new_status}")

    async def set_process_pid(self, process_name: str, pid: Optional[int]):
        lock = await self._ensure_lock()
        async with lock:
            self.process_pids[process_name] = pid

    async def calculate_rtt_percentiles(self) -> tuple[float, float]:
        # No lock needed for reading – eventual consistency is fine
        if len(self.rtt_samples) < 10:
            return 0.0, 0.0
        samples = np.array(list(self.rtt_samples))
        p95 = np.percentile(samples, 95)
        p99 = np.percentile(samples, 99)
        return float(p95), float(p99)

    async def get_session_duration(self) -> float:
        lock = await self._ensure_lock()
        async with lock:
            if self.session_start_time == 0.0:
                return 0.0
            return time.time() - self.session_start_time

# Singleton instance
state = SharedState()

# ============================================================
# PANEL GIMBAL TRACKING CALCULATIONS
# ============================================================

def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate bearing from point 1 to point 2 in degrees (0-360°)"""
    import math
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlon_rad = math.radians(lon2 - lon1)
    y = math.sin(dlon_rad) * math.cos(lat2_rad)
    x = math.cos(lat1_rad) * math.sin(lat2_rad) - math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(dlon_rad)
    bearing_rad = math.atan2(y, x)
    return (math.degrees(bearing_rad) + 360) % 360


def calculate_horizontal_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate horizontal distance using Haversine formula (meters)"""
    import math
    R = 6371000.0
    lat1_rad, lat2_rad = math.radians(lat1), math.radians(lat2)
    dlat_rad, dlon_rad = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dlat_rad / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon_rad / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def calculate_elevation_angle(horizontal_dist_m: float, altitude_diff_m: float) -> float:
    """Calculate elevation angle"""
    import math
    if horizontal_dist_m < 0.1:
        return -90.0 if altitude_diff_m > 0 else 0.0
    return math.degrees(math.atan2(-altitude_diff_m, horizontal_dist_m))


def calculate_panel_angles(
    drone_lat: float, drone_lon: float, drone_alt_m: float,
    ground_lat: float, ground_lon: float,
    drone_yaw_deg: float, horizontal_dist_m: float,
    drone_roll_deg: float = 0.0, drone_pitch_deg: float = 0.0
) -> dict:
    """Calculate 2-axis gimbal angles for panel pointing"""
    import math
    target_azimuth = calculate_bearing(drone_lat, drone_lon, ground_lat, ground_lon)
    target_elevation = calculate_elevation_angle(horizontal_dist_m, drone_alt_m)
    gimbal_azimuth_deg = target_azimuth - drone_yaw_deg
    while gimbal_azimuth_deg > 180:
        gimbal_azimuth_deg -= 360
    while gimbal_azimuth_deg < -180:
        gimbal_azimuth_deg += 360
    GIMBAL_EL_MIN, GIMBAL_EL_MAX = -45.0, 45.0
    actual_elevation_deg = max(GIMBAL_EL_MIN, min(GIMBAL_EL_MAX, target_elevation))
    gimbal_limited = actual_elevation_deg != target_elevation
    misalignment_deg = abs(target_elevation - actual_elevation_deg) if gimbal_limited else 0.0
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
