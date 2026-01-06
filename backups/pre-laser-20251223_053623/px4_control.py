# backend/px4_control.py
import asyncio
import os
import contextlib
import time
from typing import Optional, Callable
from dataclasses import dataclass
from backend.state import state

try:
    from mavsdk import System
    from mavsdk.offboard import OffboardError, VelocityNedYaw
except Exception:
    System = None  # type: ignore

DEFAULT_PX4_ADDR = os.getenv("PX4_UDP_ADDR", "udp://:14540")
DEFAULT_SEND_HZ = float(os.getenv("SCENARIO_SEND_HZ", "20.0"))
HOME_ALT_FLOOR = 2.0
# ENABLE_PX4 SWITCH ADDED HERE
ENABLE_PX4 = os.getenv("ENABLE_PX4", "false").lower() == "true"

@dataclass
class PX4Status:
    connected: bool = False
    armed: bool = False
    in_offboard: bool = False
    takeoff_alt_m: float = 10.0
    scenario_name: Optional[str] = None

class PX4Controller:
    def __init__(self, address: str = DEFAULT_PX4_ADDR, send_hz: float = DEFAULT_SEND_HZ):
        self._address = address
        self._send_hz = max(1.0, send_hz)
        self._drone: Optional[System] = None
        self._offboard_task: Optional[asyncio.Task] = None
        self._status = PX4Status(takeoff_alt_m=10.0)
        self._conn_watch_task: Optional[asyncio.Task] = None

        # Optional: supervisor can set this to broadcast to WS / state
        self.on_status: Optional[Callable[[str, dict], None]] = None  # ("PX4_*", {...})

    # -------------------------------------------------------------------------
    # Helpers
    def _emit(self, phase: str, **kv):
        if self.on_status:
            with contextlib.suppress(Exception):
                self.on_status(phase, kv)

    @property
    def status(self) -> PX4Status:
        return self._status

    async def _ensure(self):
        """Ensure we have a valid PX4 connection, reconnecting if needed"""
        if not ENABLE_PX4:
             return

        if not self._status.connected or self._drone is None:
            await self.connect(timeout_s=3.0)
        else:
            # Verify connection is still alive by checking health
            try:
                # Quick health check - if this fails, connection is dead
                async for health in self._drone.telemetry.health():
                    break  # Just need first response
            except Exception as e:
                print(f"[PX4] Connection health check failed: {e}, reconnecting...")
                self._status.connected = False
                self._drone = None
                await self.connect(timeout_s=3.0)

    async def _wait_first(self, async_iter, attr: Optional[str] = None, expect: Optional[bool] = None):
        """Wait for first item (optionally matching attr==expect), then return."""
        async for item in async_iter:
            if attr is None:
                return item
            val = getattr(item, attr, None)
            if expect is None or bool(val) == bool(expect):
                return item

    # -------------------------------------------------------------------------
    # Connection & state watching
    async def connect(self, timeout_s: float = 3.0):
        # --- FIX ADDED HERE ---
        if not ENABLE_PX4:
            print("[PX4] Connection disabled (ENABLE_PX4=false)")
            return
        # ----------------------

        """
        Fast, non-blocking connect:
        - Kick off MAVSDK connect
        - Wait up to `timeout_s` for first connection_state=True
        - Start a watcher that flips to DISCONNECTED if link drops
        """
        if System is None:
            raise RuntimeError("mavsdk not available. `pip install mavsdk`")
        if self._status.connected:
            return

        self._emit("PX4_CONNECTING", addr=self._address)
        if self._drone is None:
            self._drone = System()
            await self._drone.connect(system_address=self._address)

        async def watch_connection():
            last = None
            async for s in self._drone.core.connection_state():
                if s.is_connected and not self._status.connected:
                    self._status.connected = True
                    self._emit("PX4_CONNECTED", addr=self._address)
                if last is None:
                    last = s.is_connected
                elif last and not s.is_connected:
                    # lost heartbeat
                    self._status.connected = False
                    self._status.armed = False
                    self._status.in_offboard = False
                    self._status.scenario_name = None
                    self._emit("PX4_DISCONNECTED")
                last = s.is_connected

        # Launch watcher once
        if not self._conn_watch_task or self._conn_watch_task.done():
            self._conn_watch_task = asyncio.create_task(watch_connection())
        
        # NEW: launch a telemetry tap once
        if not getattr(self, "_telemetry_task", None) or self._telemetry_task.done():
            self._telemetry_task = asyncio.create_task(self._telemetry_tap())

        # Set telemetry rates for smooth data flow
        with contextlib.suppress(Exception):
            await self._drone.telemetry.set_rate_position(20.0)
            await self._drone.telemetry.set_rate_position_velocity_ned(20.0)
            await self._drone.telemetry.set_rate_battery(10.0)
            await self._drone.telemetry.set_rate_attitude_euler(20.0)

        # Give it a short head start
        try:
            await asyncio.wait_for(
                self._wait_first(self._drone.core.connection_state(), "is_connected", True),
                timeout=timeout_s,
            )
            self._status.connected = True
            self._emit("PX4_CONNECTED", addr=self._address)
        except asyncio.TimeoutError:
            self._emit("PX4_CONNECTING_SLOW", timeout_s=timeout_s)

    async def _telemetry_tap(self):
        """
        Continuously collect telemetry from PX4 and update state.
        Collects: GPS position, attitude, battery, velocity
        """
        try:
            async def pump_position():
                """Collect GPS coordinates and altitude"""
                count = 0
                async for pos in self._drone.telemetry.position():
                    gps_data = {
                        "gps_lat_deg": float(pos.latitude_deg),
                        "gps_lon_deg": float(pos.longitude_deg),
                        "gps_alt_m": float(pos.absolute_altitude_m),
                        "gps_rel_alt_m": float(pos.relative_altitude_m),
                    }
                    # Log every 20th GPS update (once per 2 seconds at 10Hz)
                    if count % 20 == 0:
                        print(f"[PX4] GPS: lat={gps_data['gps_lat_deg']:.7f}, lon={gps_data['gps_lon_deg']:.7f}, alt={gps_data['gps_rel_alt_m']:.1f}m")
                    count += 1
                    await state.update_telemetry(gps_data)

            async def pump_attitude():
                """Collect roll, pitch, yaw"""
                async for att in self._drone.telemetry.attitude_euler():
                    await state.update_telemetry({
                        "roll_deg": float(att.roll_deg),
                        "pitch_deg": float(att.pitch_deg),
                        "yaw_deg": float(att.yaw_deg),
                    })

            async def pump_battery():
                """Collect PX4 battery data"""
                async for bat in self._drone.telemetry.battery():
                    await state.update_telemetry({
                        "px4_voltage_mv": int(bat.voltage_v * 1000),
                        "px4_current_ma": int(bat.current_battery_a * 1000),
                    })

            # Run all pumps concurrently
            await asyncio.gather(
                pump_position(),
                pump_attitude(),
                pump_battery(),
                return_exceptions=True
            )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[PX4] Telemetry tap error: {e}")

    # -------------------------------------------------------------------------
    # Basic actions
    async def arm(self):
        await self._ensure()
        if not self._status.connected: return
        await self._drone.action.arm()
        self._status.armed = True
        self._emit("PX4_ARMED")

    async def disarm(self):
        await self._ensure()
        if not self._status.connected: return
        await self._drone.action.disarm()
        self._status.armed = False
        self._emit("PX4_DISARMED")

    async def takeoff(self, alt_m: float = 10.0, ready_timeout_s: float = 30.0):
        await self._ensure()
        if not self._status.connected: 
            print("[PX4] Takeoff ignored (not connected)")
            return

        alt_m = max(HOME_ALT_FLOOR, float(alt_m))
        self._status.takeoff_alt_m = alt_m
        await self._drone.action.set_takeoff_altitude(alt_m)

        if not self._status.armed:
            await self.arm()

        await self._drone.action.takeoff()
        self._emit("PX4_TAKEOFF_SENT", alt_m=alt_m)
        print(f"[PX4] Takeoff command sent, target altitude: {alt_m}m")

        async def wait_for_altitude():
            """Wait for drone to reach target altitude (with 1m tolerance)"""
            # First wait for in_air telemetry
            print("[PX4] Waiting for in_air telemetry...")
            async for v in self._drone.telemetry.in_air():
                if v:
                    print("[PX4] Drone reports in_air=True")
                    break

            # Now wait for target altitude
            print(f"[PX4] Waiting to reach {alt_m}m altitude...")
            async for pos in self._drone.telemetry.position():
                current_alt = getattr(pos, "relative_altitude_m", 0.0)
                if current_alt >= (alt_m - 1.0):  # Within 1m of target
                    print(f"[PX4] Reached target altitude: {current_alt:.1f}m")
                    break
                if int(current_alt) % 2 == 0:  # Log every 2m
                    print(f"[PX4] Climbing... current altitude: {current_alt:.1f}m")

        try:
            await asyncio.wait_for(wait_for_altitude(), timeout=ready_timeout_s)
            self._emit("PX4_TAKEOFF_CONFIRMED")
        except asyncio.TimeoutError:
            print(f"[PX4] Takeoff timeout after {ready_timeout_s}s")
            self._emit("PX4_TAKEOFF_PENDING", waited_s=ready_timeout_s)

    async def land(self, settle_timeout_s: float = 10.0):
        await self._ensure()
        if not self._status.connected: return
        await self._drone.action.land()
        self._emit("PX4_LAND_SENT")

        async def wait_landed():
            async for in_air in self._drone.telemetry.in_air():
                if not in_air:
                    break

        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(wait_landed(), timeout=settle_timeout_s)

        self._status.in_offboard = False
        self._status.scenario_name = None
        self._emit("PX4_LANDED")

    # -------------------------------------------------------------------------
    # Scenario Definitions
    def _create_scenario(self, scenario_name: str):
        """Create a scenario object based on the scenario name"""
        import math

        class HoverScenario:
            """Hover in place - zero velocity"""
            name = "Hover"
            def next_setpoint(self, t: float) -> VelocityNedYaw:
                return VelocityNedYaw(0.0, 0.0, 0.0, 0.0)

        class HoldScenario:
            """Same as Hover - hold position"""
            name = "Hold"
            def next_setpoint(self, t: float) -> VelocityNedYaw:
                return VelocityNedYaw(0.0, 0.0, 0.0, 0.0)

        class CircleScenario:
            """Fly in a circle - 10m radius at 2 m/s tangential speed"""
            name = "Circle"
            radius = 10.0  # meters
            speed = 2.0    # m/s tangential

            def next_setpoint(self, t: float) -> VelocityNedYaw:
                # Angular velocity: omega = v / r
                omega = self.speed / self.radius

                # Velocities in NED frame for circular motion
                # vx (North) = -v * sin(omega*t)
                # vy (East)  =  v * cos(omega*t)
                # vz (Down)  =  0 (maintain altitude)
                vn = -self.speed * math.sin(omega * t)
                ve =  self.speed * math.cos(omega * t)
                vd = 0.0

                # Yaw rate to point in direction of travel
                yaw_rate = omega * (180.0 / math.pi)  # Convert to deg/s

                return VelocityNedYaw(vn, ve, vd, yaw_rate)

        class FigureEightScenario:
            """Fly in a figure-8 pattern"""
            name = "Figure8"
            radius = 8.0   # meters
            speed = 2.0    # m/s

            def next_setpoint(self, t: float) -> VelocityNedYaw:
                omega = self.speed / self.radius
                # Figure-8: x = r*sin(t), y = r*sin(2t)/2
                vn = self.speed * math.cos(omega * t)
                ve = self.speed * math.cos(2 * omega * t)
                vd = 0.0
                yaw_rate = 20.0  # Slow rotation
                return VelocityNedYaw(vn, ve, vd, yaw_rate)

        class SquareScenario:
            """Fly in a square pattern - 20m sides"""
            name = "Square"
            side_length = 20.0  # meters
            speed = 2.0         # m/s

            def next_setpoint(self, t: float) -> VelocityNedYaw:
                # Time for one complete side
                side_time = self.side_length / self.speed

                # Determine which side we're on (0-3)
                cycle_time = 4 * side_time
                t_cycle = t % cycle_time
                side = int(t_cycle / side_time)

                # Velocity depends on which side of square
                if side == 0:    # North
                    return VelocityNedYaw(self.speed, 0.0, 0.0, 0.0)
                elif side == 1:  # East
                    return VelocityNedYaw(0.0, self.speed, 0.0, 90.0)
                elif side == 2:  # South
                    return VelocityNedYaw(-self.speed, 0.0, 0.0, 180.0)
                else:            # West
                    return VelocityNedYaw(0.0, -self.speed, 0.0, 270.0)

        # Map scenario names to classes
        scenarios = {
            "Hover": HoverScenario,
            "Hold": HoldScenario,
            "Circle": CircleScenario,
            "Figure8": FigureEightScenario,
            "Square": SquareScenario,
        }

        scenario_class = scenarios.get(scenario_name, HoverScenario)
        return scenario_class()

    # -------------------------------------------------------------------------
    # Offboard
    async def start_offboard(self, scenario_name: str, send_hz: Optional[float] = None):
        await self._ensure()
        if not self._status.connected: return
        
        hz = max(1.0, float(send_hz or self._send_hz))
        period = 1.0 / hz

        # Create scenario based on name
        scenario = self._create_scenario(scenario_name or "Hover")

        if not self._status.armed:
            await self.arm()

        # seed a setpoint BEFORE .start() (SDK requirement)
        sp0 = scenario.next_setpoint(0.0)
        try:
            await self._drone.offboard.set_velocity_ned(sp0)
            await self._drone.offboard.start()
        except OffboardError as e:
            self._emit("PX4_OFFBOARD_START_FAILED", reason=str(e))
            raise

        self._status.in_offboard = True
        self._status.scenario_name = scenario.name
        self._emit("PX4_OFFBOARD_START", scenario=scenario.name, hz=hz)

        await self.stop_offboard()  # stop prior loop if any

        async def _run():
            print(f"[PX4] Offboard task starting for scenario: {scenario.name}")
            t0 = asyncio.get_running_loop().time()
            iteration = 0
            try:
                while True:
                    t = asyncio.get_running_loop().time() - t0
                    sp = scenario.next_setpoint(t)

                    # Debug: log first few setpoints
                    if iteration < 5 or iteration % 20 == 0:
                        print(f"[PX4] Offboard t={t:.1f}s: vn={sp.north_m_s:.2f}, ve={sp.east_m_s:.2f}, vd={sp.down_m_s:.2f}, yaw={sp.yaw_deg:.1f}")

                    await self._drone.offboard.set_velocity_ned(sp)
                    await asyncio.sleep(period)
                    iteration += 1
            except asyncio.CancelledError:
                print(f"[PX4] Offboard cancelled after {iteration} iterations")
                pass
            except Exception as e:
                print(f"[PX4] Offboard error: {e}")
            finally:
                with contextlib.suppress(Exception):
                    await self._drone.offboard.stop()
                self._status.in_offboard = False
                self._status.scenario_name = None
                self._emit("PX4_OFFBOARD_STOP")

        self._offboard_task = asyncio.create_task(_run())
        print(f"[PX4] Offboard task created: {self._offboard_task}")

    async def stop_offboard(self):
        if self._offboard_task and not self._offboard_task.done():
            self._offboard_task.cancel()
            with contextlib.suppress(Exception):
                await self._offboard_task
        self._emit("PX4_OFFBOARD_STOP")

    # -------------------------------------------------------------------------
    async def close(self):
        await self.stop_offboard()
        self._drone = None
        self._status = PX4Status(takeoff_alt_m=self._status.takeoff_alt_m)
        self._emit("PX4_CLOSED")
