import asyncio
import subprocess
import os
import signal
from pathlib import Path
from typing import Optional, Dict
from datetime import datetime
import time
import sys
import shutil
import json
import contextlib

from backend.config import settings
from backend.models import RampStartRequest
from backend.parsers import GroundParser, AirParser, RelayParser
from backend.state import state
from .px4_control import PX4Controller


"""
PURPOSE: Manage lifecycle of Ground, Air, and Relay processes

RESPONSIBILITIES:
1. Spawn processes with correct environment variables
2. Stream stdout/stderr asynchronously
3. Parse output and update state
4. Detect crashes and cleanup
5. Graceful shutdown on server stop

KEY DESIGN DECISIONS:
- Separate process per component (isolation)
- Async stdout reading (non-blocking)
- Automatic restart on crash (optional, configured)
- Cleanup on exit (kill all child processes)
"""

class ProcessSupervisor:
    """
    Manages Ground, Air, and Relay subprocesses
    """
    def __init__(self):
        # Process handles
        self.processes: Dict[str, Optional[subprocess.Popen]] = {
            "ground": None,
            "air": None,
            "relay": None,
            "socat": None
        }

        # Parsers for each process
        self.parsers = {
            "ground": GroundParser(),
            "air": AirParser(),
            "relay": RelayParser()
        }

        # Background monitoring tasks
        self._monitor_tasks: list[asyncio.Task] = []

        # Current session ID
        self.session_id: Optional[str] = None
        
        # Flag fro graceful shutdown
        self._shutting_down = False

        self.ws_broadcast = lambda payload: None  # set by main.py
        self.px4 = PX4Controller()

        # Forward PX4 status changes to event log + WS
        self.px4.on_status = self._on_px4_status
        

    # --------------------------------------------------------------------------------
    # Optional: PX4 Integration
    # --------------------------------------------------------------------------------

    async def px4_connect(self):
        await self.px4.connect()

    async def px4_takeoff(self, alt_m: float):
        await self.px4.takeoff(alt_m)

    async def px4_offboard_start(self, scenario_name: str, send_hz: float | None = None):
        if send_hz:
            self.px4._send_hz = max(1.0, float(send_hz))
        await self.px4.start_offboard(scenario_name)

    async def px4_offboard_stop(self):
        await self.px4.stop_offboard()

    async def px4_land(self):
        await self.px4.land()

    # ==============================================================================
    # CLEANUP OLD PROCESSES
    # ==============================================================================

    async def _cleanup_old_processes(self):
        """
        Kill any existing laser power beaming processes before starting new ones.
        Prevents port conflicts and zombie processes.
        """
        process_patterns = [
            "mav_relay.py",
            "permit_air_power_ramp.py",
            "permit_ground_power_ramp.py"
        ]

        killed_any = False

        for pattern in process_patterns:
            try:
                # Find PIDs of matching processes
                result = subprocess.run(
                    ["pgrep", "-f", pattern],
                    capture_output=True,
                    text=True,
                    timeout=2
                )

                if result.returncode == 0 and result.stdout.strip():
                    pids = result.stdout.strip().split('\n')
                    for pid_str in pids:
                        if pid_str:
                            try:
                                pid = int(pid_str)
                                # Don't kill our own process
                                if pid != os.getpid():
                                    print(f"[supervisor] Cleaning up old process: {pattern} (PID {pid})")
                                    os.kill(pid, signal.SIGTERM)
                                    killed_any = True
                                    await state.add_event(
                                        "INFO", "supervisor", "CLEANUP",
                                        f"Killed stale process: {pattern} (PID {pid})"
                                    )
                            except (ProcessLookupError, ValueError):
                                # Process already dead or invalid PID
                                pass
                            except Exception as e:
                                print(f"[supervisor] Failed to kill PID {pid_str}: {e}")

            except subprocess.TimeoutExpired:
                print(f"[supervisor] Timeout checking for {pattern}")
            except FileNotFoundError:
                # pgrep not available on this system
                print("[supervisor] Warning: pgrep not available, cannot cleanup old processes")
                break
            except Exception as e:
                print(f"[supervisor] Cleanup error for {pattern}: {e}")

        if killed_any:
            # Wait for ports to be released
            await asyncio.sleep(1.5)
            print("[supervisor] Old processes cleaned up, ports released")
        else:
            print("[supervisor] No stale processes found")

    # ==============================================================================
    # LIFECYCLE MANAGEMENT
    # ==============================================================================
    def _on_px4_status(self, phase: str, kv: dict):
        """
        Bridge PX4Controller status updates into:
        1) the event log
        2) live WS updates (if hooked from main.py)
        """
        # 1) event log (async): don't block caller
        async def _log():
            try:
                # compact message
                msg = json.dumps(kv, separators=(",",":"))
                await state.add_event("INFO", "PX4", phase, msg)
            except Exception:
                pass

        if phase == "PX4_CONNECTED":
            asyncio.create_task(state.set_status("READY"))
        elif phase == "PX4_DISCONNECTED":
            asyncio.create_task(state.set_status("SAFE"))
            
        asyncio.create_task(_log())

        # 2) live WS broadcast (fire-and-forget)
        with contextlib.suppress(Exception):
            payload = {"type": "status", "source": "PX4", "phase": phase, **kv}
            self.ws_broadcast(payload)

    def is_running(self) -> bool:
        return any(
            self.processes[name] is not None and self.processes[name].poll() is None
            for name in ("ground", "air", "relay")
        )

    async def _start_virtual_elrs_link(self):
        """
        Ensure a PTY ↔ PTY bridge exists between /tmp/ELRS_TX and /tmp/ELRS_RX.
        Uses `socat pty,link=... pty,link=...` and keeps it running.
        """
        # If already running, nothing to do
        if self.processes.get("socat") and self.processes["socat"].poll() is None:
            return

        # Check socat exists
        if not shutil.which(settings.SOCAT_BIN):
            await state.add_event(
                "ERROR", "server", "SOCAT_NOT_FOUND",
                "socat not found. Install with: brew install socat"
            )
            raise RuntimeError("socat not found")

        # Build socat command
        # NOTE: link targets are paths without the ",baud" suffix.
        tx_path = settings.ELRS_TX_LINK.split(",")[0]
        rx_path = settings.ELRS_RX_LINK.split(",")[0]
        cmd = [
            settings.SOCAT_BIN, "-d", "-d",
            f"pty,link={tx_path},raw,echo=0,perm=0600",
            f"pty,link={rx_path},raw,echo=0,perm=0600",
        ]

        # Kill any stale symlinks/devices from a previous crash
        for p in (tx_path, rx_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            preexec_fn=os.setsid,
        )
        self.processes["socat"] = proc
        await state.add_event("INFO", "server", "SOCAT_START", f"socat PTY bridge started (PID {proc.pid})")

    async def _stop_virtual_elrs_link(self):
        """Stop socat PTY bridge if running, and remove links."""
        proc = self.processes.get("socat")
        if not proc:
            return
        try:
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                for _ in range(50):
                    if proc.poll() is not None:
                        break
                    await asyncio.sleep(0.1)
                if proc.poll() is None:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            await state.add_event("INFO", "server", "SOCAT_STOP", "socat PTY bridge stopped")
        finally:
            self.processes["socat"] = None
            # Clean up link files
            for p in (settings.ELRS_TX_LINK.split(",")[0], settings.ELRS_RX_LINK.split(",")[0]):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass

    async def start_all(self, ramp_params: RampStartRequest) -> str:
        """
        Start a complete experiment: Relay -> Air -> Ground
        Why this order?
        - Relay must be up to forward messages
        - Air needs serial ports to exist before connecting
        - Ground needs Air to be listening before sending permits
        """

        # CRITICAL: Cleanup any old processes to prevent port conflicts
        await self._cleanup_old_processes()

        # Generate unique session ID
        self.session_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

        # Initialise session in state
        await state.start_session(
            session_id= self.session_id,
            scenario=ramp_params.scenario,
            params= ramp_params.dict()
        )

        await state.set_status("CONNECTING")

        try:
            # Connect to PX4 first to start collecting telemetry (GPS, attitude, etc.)
            await state.add_event("INFO", "supervisor", "PX4_CONNECTING", "Connecting to PX4...")
            try:
                await self.px4_connect()
                await state.add_event("INFO", "supervisor", "PX4_CONNECTED", "PX4 connected, telemetry streaming")
            except Exception as e:
                await state.add_event("WARN", "supervisor", "PX4_CONNECT_FAIL", f"PX4 connection failed: {e}")
                # Continue anyway - GPS is optional for the experiment

            # Start processes in order
            await self._start_virtual_elrs_link()

            await self.start_relay()
            await asyncio.sleep(2.0)  # Give relay time to init

            await self.start_air()
            await asyncio.sleep(2.0)  # Give air time to init

            await self.start_ground(ramp_params)
            await asyncio.sleep(1.0)  # Give ground time to init
            
            await state.set_status("RAMPING")

            return self.session_id
        
        except Exception as e:
            # If any process fails to start, stop all and raise
            await state.add_event(
                "ERROR", "server", "START_FAIL", f"Experiment start failed: {e}"
            )
            await self.stop_all()
            raise RuntimeError(f"Failed to start experiment: {e}")
        
    # ────────────────────────────────────────────────────────────
    # RELAY PROCESS
    # ────────────────────────────────────────────────────────────
    
    async def start_relay(self):
        """
        Start MAV Relay process.
        
        Purpose:
        - Bridges UDP (Ground) ↔ Serial (Air)
        - Simulates ELRS radio link
        - Applies packet loss/latency (drills)
        """
        
        # Check if already running
        if self.processes["relay"] is not None:
            await self.stop_relay()
        
        # Build environment variables
        env = os.environ.copy()
        env.update({
            "MAVLINK20": "1",
            "MAVLINK_DIALECT": "laser_safety",
            "RELAY_UDP_IN": settings.RELAY_UDP_IN,
            "RELAY_UDP_OUT": settings.RELAY_UDP_OUT,
            # Relay should attach to TX end of the pair
            "RELAY_SERIAL": settings.ELRS_TX_LINK,  # CHANGED
            "DRILL_LOSS_PCT": "0.0",
            "DRILL_DELAY_MS": "0",
            "DRILL_JITTER_MS": "0",
            "PYTHONUNBUFFERED": "1",
        })
        
        # Spawn process
        proc = subprocess.Popen(
            [sys.executable,"-u", str(settings.RELAY_SCRIPT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # Merge stderr into stdout
            env=env,
            text=True,           # Get strings, not bytes
            bufsize=1,           # Line-buffered (get lines immediately)
            preexec_fn=os.setsid  # Create new process group (for cleanup)
        )
        # Why preexec_fn=os.setsid?
        # - Creates a new session (process group)
        # - Allows killing entire process tree later
        # - Without it: killing parent doesn't kill children
        
        self.processes["relay"] = proc
        await state.set_process_pid("relay", proc.pid)
        
        await state.add_event(
            "INFO", "relay", "PROCESS_START",
            f"MAV Relay started (PID {proc.pid})"
        )
        
        # Start monitoring stdout in background
        task = asyncio.create_task(self._monitor_process("relay"))
        self._monitor_tasks.append(task)
    
    async def stop_relay(self):
        """Stop Relay process gracefully"""
        await self._stop_process("relay")
    
    # ────────────────────────────────────────────────────────────
    # AIR PROCESS
    # ────────────────────────────────────────────────────────────
    
    async def start_air(self):
        """
        Start Air node process.
        
        Purpose:
        - Receives permits from Ground (via Relay)
        - Enforces safety gates (attitude, battery, watchdog)
        - Sends ACKs and telemetry back to Ground
        - Simulates optical power sensor
        """
        
        if self.processes["air"] is not None:
            await self.stop_air()
        
        env = os.environ.copy()
        env.update({
            "MAVLINK20": "1",
            "MAVLINK_DIALECT": "laser_safety",
            "USE_PX4": "1",
            "PX4_TX_PORT": "14780",
            "PX4_RX_PORT": "14740",
            "SIM_SEED": "12345",
            # Air should attach to RX end of the pair
            "ELRS_SERIAL": settings.ELRS_RX_LINK,  # NEW (your air script logs this)
            "PYTHONUNBUFFERED": "1",
        })
        
        proc = subprocess.Popen(
            [sys.executable, "-u", str(settings.AIR_SCRIPT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            bufsize=1,
            preexec_fn=os.setsid
        )
        
        self.processes["air"] = proc
        await state.set_process_pid("air", proc.pid)
        
        await state.add_event(
            "INFO", "air", "PROCESS_START",
            f"Air node started (PID {proc.pid})"
        )
        
        task = asyncio.create_task(self._monitor_process("air"))
        self._monitor_tasks.append(task)
    
    async def stop_air(self):
        """Stop Air process gracefully"""
        await self._stop_process("air")
    
    # ────────────────────────────────────────────────────────────
    # GROUND PROCESS
    # ────────────────────────────────────────────────────────────
    
    async def start_ground(self, params: RampStartRequest):
        """
        Start Ground station process.
        
        Purpose:
        - Sends permits to Air at configured rate
        - Ramps power from min to max in steps
        - Logs all data to CSV
        - Manages permit protocol (sequence, TTL, watchdogs)
        
        Args:
            params: Validated ramp parameters (Pydantic model)
        """
        
        if self.processes["ground"] is not None:
            await self.stop_ground()
        
        # Convert Pydantic model to environment variables
        env = os.environ.copy()
        env.update({
            "MAVLINK20": "1",
            "MAVLINK_DIALECT": "laser_safety",
            # Ramp parameters
            "MIN_POWER_PCT": str(params.min_power_pct),
            "MAX_POWER_PCT": str(params.max_power_pct),
            "STEP_PCT": str(params.step_pct),
            "DWELL_TIME_S": str(params.dwell_time_s),
            "MAX_POWER_W": str(params.max_power_w),
            # Session info
            "SCENARIO_NAME": params.scenario,
            "EXPERIMENT_NAME": self.session_id,
            # Permit protocol (could come from another API endpoint)
            "PERMIT_SEND_HZ": "10.0",
            "PERMIT_TTL_MS": "300",
            "PERMIT_DUPLICATE": "false",
            "SIM_SEED": "12345",
            "PYTHONUNBUFFERED": "1",
        })
        
        proc = subprocess.Popen(
            [sys.executable, "-u", str(settings.GROUND_SCRIPT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            bufsize=1,
            preexec_fn=os.setsid
        )
        
        self.processes["ground"] = proc
        await state.set_process_pid("ground", proc.pid)
        
        await state.add_event(
            "INFO", "ground", "PROCESS_START",
            f"Ground station started (PID {proc.pid})"
        )
        
        task = asyncio.create_task(self._monitor_process("ground"))
        self._monitor_tasks.append(task)
    
    async def stop_ground(self):
        """Stop Ground process gracefully"""
        await self._stop_process("ground")
    
    # ────────────────────────────────────────────────────────────
    # MONITORING & PARSING
    # ────────────────────────────────────────────────────────────

    async def _monitor_process(self, name: str):
        """
        Monitor a process's stdout and parse lines.
        Core of the supervisor
        1. Reads stdout line-by-line (async)
        2. Passes lines to parser
        3. Detects when process exits
        4. Updates state accordingly
        """

        proc = self.processes[name]
        if proc is None:
            return
        
        parser = self.parsers[name]
        
        try:
            # Read stdout line-by-line
            async for line in self._async_readline(proc.stdout):
                # Skip empty lines
                if not line:
                    continue
                
                # Log to console for debugging
                print(f"[{name}] {line}")
                
                # Parse line (updates state)
                try:
                    await parser.parse_line(line)
                except Exception as e:
                    # Don't crash on parse errors
                    await state.add_event(
                        "WARN", "server", "PARSE_ERROR",
                        f"Failed to parse {name} line: {e}"
                    )
            
            # If we get here, process has exited
            exit_code = proc.poll()
            
            if exit_code == 0:
                # Clean exit
                await state.add_event(
                    "INFO", name, "PROCESS_EXIT",
                    f"{name} exited normally"
                )
            else:
                # Crash
                await state.add_event(
                    "ERROR", name, "PROCESS_CRASH",
                    f"{name} crashed with exit code {exit_code}"
                )
                # NEW: Auto-land when Ground completes
                if name == "ground" and not self._shutting_down:
                    await state.add_event(
                        "INFO", "server", "AUTO_LAND",
                        "Ground experiment complete, initiating landing sequence"
                    )
                    try:
                        await self.px4_land()
                        await asyncio.sleep(5)  # Wait for landing to complete
                        await self.stop_all()  # Clean up Air/Relay
                    except Exception as e:
                        print(f"[supervisor] Auto-land failed: {e}")
                        await state.add_event(
                            "ERROR", "server", "LAND_FAILED",
                            f"Failed to land drone: {e}"
                        )

                if name == "ground" and not self._shutting_down:
                    await state.set_status("READY")
                    # optional: auto-cleanup
                    await self.stop_air()
                    await self.stop_relay()
                    await self._stop_virtual_elrs_link()

                # If Ground crashes during ramp, go to SAFE
                if name == "ground" and not self._shutting_down:
                    await state.set_status("SAFE")
        
        except Exception as e:
            await state.add_event(
                "ERROR", "server", "MONITOR_ERROR",
                f"Error monitoring {name}: {e}"
            )
        
        finally:
            # Cleanup
            self.processes[name] = None
            await state.set_process_pid(name, None)

    async def _async_readline(self, stream):

        """Asynchronously read lines from a stream
        How it works:
        1. readline() is a BLOCKING call (waits for \n)
        2. run_in_executor() runs it in a thread pool
        3. await yields control while waiting
        4. Other tasks can run (WebSocket, API requests)
        
        Example timeline:
        Time  Task A (readline)        Task B (WebSocket)
        ───────────────────────────────────────────────────
        0ms   await readline()
        1ms     → submitted to pool
        2ms     → waiting...           await send_telemetry()
        3ms     → waiting...             → reading state
        4ms     → waiting...             → serializing JSON
        5ms     → line ready!            → sending to browser
        6ms   ← return "  [ 45%]..."  ← done
        
        Both tasks make progress concurrently!       
        """

        loop = asyncio.get_event_loop()
        while True:
            line = await loop.run_in_executor(None, stream.readline)
            if not line:
                break
            yield line.strip()

    async def _stop_process(self, name: str):
        proc = self.processes[name]
        if proc is None:
            return
        
        # Check if already exited
        if proc.poll() is not None:
            self.processes[name] = None
            return
        
        try:
            # Try graceful shutdown (SIGTERM)
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            # Why killpg? Kills entire process group (parent + children)
            
            # Wait up to 5 seconds for graceful shutdown
            for _ in range(50):
                if proc.poll() is not None:
                    # Process exited
                    await state.add_event(
                        "INFO", name, "PROCESS_STOP",
                        f"{name} stopped gracefully"
                    )
                    self.processes[name] = None
                    return
                await asyncio.sleep(0.1)
            
            # Still alive after 5s - force kill (SIGKILL)
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            await state.add_event(
                "WARN", name, "PROCESS_KILL",
                f"{name} force killed (did not respond to SIGTERM)"
            )
            
        except ProcessLookupError:
            # Process already dead
            pass
        except Exception as e:
            await state.add_event(
                "ERROR", "server", "STOP_ERROR",
                f"Error stopping {name}: {e}"
            )
        finally:
            self.processes[name] = None
            await state.set_process_pid(name, None)

    async def stop_all(self):
        """
        Stop all processes and cleanup.

        Called when:
        - User clicks "Stop" in UI
        - Server is shutting down
        - Experiment completes
        - Error during startup
        """
        self._shutting_down = True

        await state.set_status("STOPPING")

        # Stop PX4 offboard mode first (CRITICAL for next run)
        try:
            await self.px4_offboard_stop()
            await state.add_event("INFO", "supervisor", "PX4_OFFBOARD_STOP", "PX4 offboard mode stopped")
            print("[supervisor] PX4 offboard stopped")
        except Exception as e:
            print(f"[supervisor] Failed to stop offboard: {e}")
            await state.add_event("WARN", "supervisor", "PX4_OFFBOARD_STOP_FAIL", f"Failed to stop offboard: {e}")

        # Stop in reverse order (Ground → Air → Relay)
        await self.stop_ground()
        await self.stop_air()
        await self.stop_relay()
        await self._stop_virtual_elrs_link()  # NEW


        # Cancel all monitoring tasks
        for task in self._monitor_tasks:
            task.cancel()

        # Wait for tasks to cleanup
        await asyncio.gather(*self._monitor_tasks, return_exceptions=True)
        self._monitor_tasks.clear()

        # Extra cleanup: kill any lingering Python processes by name
        # This prevents "Address already in use" errors on next run
        await asyncio.sleep(0.5)  # Give processes time to exit
        try:
            subprocess.run(
                ["pkill", "-f", "permit_air_power_ramp.py|permit_ground_power_ramp.py|mav_relay.py"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2
            )
        except Exception:
            pass  # pkill may fail if no processes found (expected)

        await state.set_status("READY")
        self._shutting_down = False

    # ════════════════════════════════════════════════════════════
    # STATUS QUERIES
    # ════════════════════════════════════════════════════════════
    
    def get_process_status(self) -> Dict[str, Optional[int]]:
        """
        Get current PIDs of all processes.
        
        Returns:
            {"ground": 12345, "air": 12346, "relay": 12347}
            or {"ground": None, ...} if not running
        """
        return {
            name: proc.pid if proc and proc.poll() is None else None
            for name, proc in self.processes.items()
        }

    def is_air_running(self) -> bool:
        p = self.processes.get("air")
        return p is not None and p.poll() is None

    def is_relay_running(self) -> bool:
        p = self.processes.get("relay")
        return p is not None and p.poll() is None
    
    def is_ground_running(self) -> bool:
        p = self.processes.get("ground")
        return p is not None and p.poll() is None


    
# ════════════════════════════════════════════════════════════════
# SINGLETON INSTANCE
# ════════════════════════════════════════════════════════════════
supervisor = ProcessSupervisor()