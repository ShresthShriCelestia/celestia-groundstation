# Need to extract structured data from the logs and put it into the state store
import re
import time
from typing import Optional
from backend.state import state

"""
Parsers for extracting structured data from process logs.
One parser per process (ground, air, relay).
Each parser has multiple regex paterns 
Parser update shared state when they find matches.
"""

# ===============================================================================
# Ground Parser
# ===============================================================================

class GroundParser:
    """
    Parse Ground node stdout into telemetry
    Ground logs two types of lines thats important
    - Telemetry lines: "TELEMETRY: key1=val1 key2=val2 ..."
    - Deny lines: "DENY: reason=... details=..."
    """

    def __init__(self):
        """
        Compile regex patterns once at initialisation
        Why Compile?
        - re.compile() creates a state machine
        - Compiled paterns can be reused many times
        - Pattern compilation is expensive
        """

        # -------------------------------
        # Pattern 1: Telemetry lines
        # -------------------------------
        # Example input:
        # "  [  45%] Cmd:225.0W | Rcv:45000.0mW | Eff:20.0% | LQ:92% | RTT:34.5ms | G/D:450/89 (83%) | d=42.1m r=35.2° p=-8.1°"
        
        self.telemetry_pattern = re.compile(
            # [  50%] - Power percentage
            r'\[\s*(?P<pct>\d+)%\]'
            
            # Cmd:250.0W - Commanded power (with optional space)
            r'\s+Cmd:\s*(?P<cmd_w>[\d.]+)W'
            
            # | Rcv:94777.0mW - Received power (with optional space)
            r'\s+\|\s+Rcv:\s*(?P<rcv_mw>[\d.]+)mW'
            
            # | Eff: 37.9% - Efficiency (with optional space)
            r'\s+\|\s+Eff:\s*(?P<eff>[\d.]+)%'
            
            # | LQ: 98% - Link quality (with optional space)
            r'\s+\|\s+LQ:\s*(?P<lq>\d+)%'
            
            # | RTT: 14.1ms - Round-trip time (with optional space)
            r'\s+\|\s+RTT:\s*(?P<rtt>[\d.]+)ms'
            
            # | G/D:821/0 - Grants/Denies
            r'\s+\|\s+G/D:\s*(?P<grants>\d+)/(?P<denies>\d+)'
            
            # Optional: (100%) - Grant rate
            r'(?:\s+\((?P<grant_rate>[\d.]+)%\))?'
            
            # Optional: distance and attitude
            # | d=50.0m r=0.0° p=0.0°
            r'(?:\s+\|\s+d=(?P<dist>[\d.]+)m)?'
            r'(?:\s+r=(?P<roll>[-\d.]+)°)?'
            r'(?:\s+p=(?P<pitch>[-\d.]+)°)?',
            
            re.VERBOSE
        )
        # ────────────────────────────────────────────────────────────
        # PATTERN 2: Denial Warning
        # ────────────────────────────────────────────────────────────
        # Example input:
        # "[ground] ⚠ DENY received: seq=124 reason=PX4NotOK"
        
        self.deny_pattern = re.compile(
            r'DENY received:\s+'
            r'seq=(?P<seq>\d+)\s+'
            r'reason=(?P<reason>\w+)'
        )
        
        # ────────────────────────────────────────────────────────────
        # PATTERN 3: Ramp Level Change (optional - for UI progress)
        # ────────────────────────────────────────────────────────────
        # Example input:
        # "[RAMP] Level 9/16: 45%"
        
        self.ramp_level_pattern = re.compile(
            r'\[RAMP\]\s+Level\s+(?P<current>\d+)/(?P<total>\d+):\s+(?P<pct>\d+)%'
        )

        # ────────────────────────────────────────────────────────────
        # PATTERN 4: Battery data
        # ────────────────────────────────────────────────────────────
        self.battery_pattern = re.compile(
            r'BAT:(?P<voltage>\d+)mV\s+(?P<current>-?\d+)mA\s+(?P<temp>\d+)cdeg'
        )
                
    async def parse_line(self, line: str):
        """
        Parse a single line of Ground stdout.
        
        Why async?
        - Updating state requires acquiring a lock (async operation)
        - Can't block the supervisor while parsing
        
        Flow:
        1. Try pattern 1 (telemetry) → if match, update state and return
        2. Try pattern 2 (denial) → if match, log event and return
        3. Try pattern 3 (ramp level) → if match, update state and return
        4. No match → ignore (might be startup message, debug log, etc.)
        """
        
        # ────────────────────────────────────────────────────────────
        # Try Pattern 1: Telemetry
        # ────────────────────────────────────────────────────────────
        match = self.telemetry_pattern.search(line)
        if match:
            # Extract all captured groups into a dictionary
            data = {
                "commanded_pct": int(match.group("pct")),
                "commanded_w": float(match.group("cmd_w")),
                "received_mw": float(match.group("rcv_mw")),
                "efficiency_pct": float(match.group("eff")),
                "link_quality_pct": int(match.group("lq")),
                "rtt_ms": float(match.group("rtt")),
                "grants_total": int(match.group("grants")),
                "denies_total": int(match.group("denies")),
            }
            
            # Add optional fields if present
            # Why check? Optional groups return None if not matched
            if match.group("dist"):
                data["distance_m"] = float(match.group("dist"))
            if match.group("roll"):
                data["roll_deg"] = float(match.group("roll"))
            if match.group("pitch"):
                data["pitch_deg"] = float(match.group("pitch"))
            
            # Update shared state (thread-safe)
            await state.update_telemetry(data)
            
            # Calculate grant rate for UI
            grants = data["grants_total"]
            denies = data["denies_total"]
            total = grants + denies
            if total > 0:
                grant_rate = (grants / total) * 100.0
                await state.update_telemetry({"grant_rate_pct": grant_rate})
            
            return  # Done processing this line
        
        # ────────────────────────────────────────────────────────────
        # Try Pattern 2: Denial Warning
        # ────────────────────────────────────────────────────────────
        match = self.deny_pattern.search(line)
        if match:
            seq = match.group("seq")
            reason = match.group("reason")
            
            # Log as event for UI event stream
            await state.add_event(
                level="WARN",
                src="ground",
                code="DENY_RECEIVED",
                msg=f"Seq {seq}: {reason}"
            )
            
            return
        
        # ────────────────────────────────────────────────────────────
        # Try Pattern 3: Ramp Level Change
        # ────────────────────────────────────────────────────────────
        match = self.ramp_level_pattern.search(line)
        if match:
            current = int(match.group("current"))
            total = int(match.group("total"))
            pct = int(match.group("pct"))
            
            # Update ramp progress for UI
            await state.update_telemetry({
                "ramp_level_current": current,
                "ramp_level_total": total,
                "ramp_level_str": f"{current}/{total}"
            })
            
            return

        # Try Pattern 4: Battery data
        # In GroundParser.parse_line(), update the battery section:
        match = self.battery_pattern.search(line)
        if match:
            voltage = int(match.group("voltage"))
            current = int(match.group("current"))
            temp = int(match.group("temp"))
            
            print(f"[Parser] Battery matched: {voltage}mV {current}mA {temp}cdeg")  # DEBUG
            
            await state.update_telemetry({
                "voltage_mv": voltage,
                "current_ma": current,
                "temp_cdeg": temp,
            })
            return
                
        # ────────────────────────────────────────────────────────────
        # No Pattern Matched
        # ────────────────────────────────────────────────────────────
        # This is normal! Ground prints many lines:
        # - "[ground] Preflight OK..."
        # - "[ground] Starting power ramp..."
        # - "[ground] Wrote metadata JSON"
        # We only care about specific patterns.

# ============================================================================
# AIR PARSER
# ============================================================================

class AirParser:
    """
    Parse Air node stdout into telemetry and events.

    Air logs two critical types:
    1. GRANT lines (permit accepted)
    2. DENY lines (permit rejected with reason)
    """

    def __init__(self):
        """Compile Air-specific patterns"""

        # ────────────────────────────────────────────────────────────
        # PATTERN 1: GRANT Line
        # ────────────────────────────────────────────────────────────
        # Example input:
        # "[air] ✓ GRANT seq=123 | Cmd:100W | Rcv:40000.0mW | Eff:40.0% | d=50.0m | r=0.0° p=0.0°"

        self.grant_pattern = re.compile(
            r'✓ GRANT\s+'
            r'seq=(?P<seq>\d+)\s+\|\s+'
            r'Cmd:(?P<cmd_w>[\d.]+)W\s+\|\s+'
            r'Rcv:(?P<rcv_mw>[\d.]+)mW\s+\|\s+'
            r'Eff:(?P<eff>[\d.]+)%\s+\|\s+'
            r'd=(?P<dist>[\d.]+)m\s+\|\s+'
            r'r=(?P<roll>[-\d.]+)°\s+'
            r'p=(?P<pitch>[-\d.]+)°'
        )

        # ────────────────────────────────────────────────────────────
        # THROTTLING: Prevent GRANT event flood
        # ────────────────────────────────────────────────────────────
        # GRANT events happen at permit rate (~10 Hz) → would flood Event Log
        # Only log GRANT events every N seconds
        self.last_grant_event_time = 0.0
        self.grant_event_throttle_seconds = 5.0  # Log GRANT events every 5 seconds
        
        # ────────────────────────────────────────────────────────────
        # PATTERN 2: DENY Line
        # ────────────────────────────────────────────────────────────
        # Example input:
        # "[air] ✗ DENY seq=124 | PX4_NOT_OK | r=35.0° p=-10.0° | att_err=36.4° (cone=12°)"
        
        self.deny_pattern = re.compile(
            r'✗ DENY\s+'
            r'seq=(?P<seq>\d+)\s+\|\s+'
            r'(?P<reason>\w+)\s+\|\s+'
            r'r=(?P<roll>[-\d.]+)°\s+'
            r'p=(?P<pitch>[-\d.]+)°'
            r'(?:\s+\|\s+att_err=(?P<att_err>[\d.]+)°)?'  # Optional
        )
        
        # ────────────────────────────────────────────────────────────
        # PATTERN 3: PX4 Gate Status (debugging)
        # ────────────────────────────────────────────────────────────
        # Example input:
        # "[air] PX4 gate: hb=1 armed=1 ekf=1 cone=0 (r=35.0° p=-10.0°)"
        
        self.px4_gate_pattern = re.compile(
            r'PX4 gate:\s+'
            r'hb=(?P<hb>\d)\s+'
            r'armed=(?P<armed>\d)\s+'
            r'ekf=(?P<ekf>\d)\s+'
            r'cone=(?P<cone>\d)'
        )

        # NEW: PX4 altitude line from air node
        self.px4_alt_pattern = re.compile(
            r'\[air\]\s+PX4\s+ALT\s+rel=(?P<rel>[-\d.]+)m'
        )

        # NEW: PX4 battery line from air node
        self.px4_bat_pattern = re.compile(
            r'\[air\]\s+PX4\s+BAT\s+V=(?P<v>\d+)mV\s+I=(?P<i>-?\d+)mA\s+rem=(?P<rem>-?\d+)%'
        )

        # NEW: Home position set (ground station location)
        self.home_set_pattern = re.compile(
            r'\[air\]\s+Home\s+set:\s+(?P<lat>[-\d.]+),\s+(?P<lon>[-\d.]+)'
        )
    
    async def parse_line(self, line: str):
        """Parse a single line of Air stdout"""
        
        # ────────────────────────────────────────────────────────────
        # Try Pattern 1: GRANT
        # ────────────────────────────────────────────────────────────
        match = self.grant_pattern.search(line)
        if match:
            seq = match.group("seq")

            # Update telemetry with granted permit status (always update)
            data = {
                "granted": True,
                "deny_reason": None,
                "distance_m": float(match.group("dist")),
                "roll_deg": float(match.group("roll")),
                "pitch_deg": float(match.group("pitch")),
                "seq": int(seq),
            }
            await state.update_telemetry(data)

            # Calculate cone violation
            roll = float(match.group("roll"))
            pitch = float(match.group("pitch"))
            attitude_error = (roll**2 + pitch**2)**0.5
            await state.update_telemetry({
                "cone_violation": attitude_error > 12.0
            })

            # THROTTLE: Only log INFO events every N seconds (prevents browser crash)
            current_time = time.time()
            time_since_last_event = current_time - self.last_grant_event_time

            if time_since_last_event >= self.grant_event_throttle_seconds:
                self.last_grant_event_time = current_time
                await state.add_event(
                    level="INFO",
                    src="air",
                    code="GRANT",
                    msg=f"Seq {seq}: {match.group('cmd_w')}W @ {match.group('dist')}m"
                )

            return
        
        # ────────────────────────────────────────────────────────────
        # Try Pattern 2: DENY
        # ────────────────────────────────────────────────────────────
        match = self.deny_pattern.search(line)
        if match:
            seq = match.group("seq")
            reason = match.group("reason")
            
            # Update telemetry with denied permit status
            data = {
                "granted": False,
                "deny_reason": reason,
                "roll_deg": float(match.group("roll")),
                "pitch_deg": float(match.group("pitch")),
                "seq": int(seq),
            }
            await state.update_telemetry(data)
            
            # Calculate cone violation
            if match.group("att_err"):
                attitude_error = float(match.group("att_err"))
                await state.update_telemetry({
                    "cone_violation": attitude_error > 12.0
                })
            
            # Log as WARN event
            msg = f"Seq {seq}: {reason}"
            if match.group("att_err"):
                msg += f" (attitude {match.group('att_err')}° > cone)"
            
            await state.add_event(
                level="WARN",
                src="air",
                code=reason,
                msg=msg
            )
            
            return        
        
        m_alt = self.px4_alt_pattern.search(line)
        if m_alt:
            rel = float(m_alt.group("rel"))
            # Put altitude in the "attitude" block that your WS payload already exposes
            await state.update_telemetry({ "rel_alt_m": rel })
            return

        m_bat = self.px4_bat_pattern.search(line)
        if m_bat:
            vbatt = int(m_bat.group("v"))
            ibatt = int(m_bat.group("i"))
            rem   = int(m_bat.group("rem"))
            await state.update_telemetry({
                "voltage_mv": vbatt,
                "current_ma": ibatt,
                "battery_remaining_pct": None if rem < 0 else rem
            })
            return

        # ────────────────────────────────────────────────────────────
        # NEW: Home Position Set (Ground Station Location)
        # ────────────────────────────────────────────────────────────
        m_home = self.home_set_pattern.search(line)
        if m_home:
            home_lat = float(m_home.group("lat"))
            home_lon = float(m_home.group("lon"))
            await state.update_telemetry({
                "home_lat_deg": home_lat,
                "home_lon_deg": home_lon,
            })
            print(f"[Parser] Ground station home set: {home_lat:.6f}, {home_lon:.6f}")
            return

        # ────────────────────────────────────────────────────────────
        # Try Pattern 3: PX4 Gate Status
        # ────────────────────────────────────────────────────────────
        match = self.px4_gate_pattern.search(line)
        if match:
            # This is debug info - could update state for detailed status
            # For now, just log significant failures
            if match.group("cone") == "0":  # Cone violation
                await state.add_event(
                    level="WARN",
                    src="air",
                    code="PX4_CONE_VIOLATION",
                    msg="Attitude outside ±12° cone"
                )
            
            return

# ============================================================================
# RELAY PARSER
# ============================================================================

class RelayParser:
    """
    Parse MAV Relay stdout into message counters.

    Relay logs message flow statistics for monitoring link health.
    """

    def __init__(self):
        """Compile Relay-specific patterns"""

        # ────────────────────────────────────────────────────────────
        # PATTERN: Message Counter
        # ────────────────────────────────────────────────────────────
        # Example inputs:
        # "[mav_relay] UDP->SER: queue=5 total=1234 last=LASER_PERMIT"
        # "[mav_relay] SER->UDP: queue=2 total=987 last=LASER_PERMIT_ACK"

        self.counter_pattern = re.compile(
            r'(?P<direction>UDP->SER|SER->UDP):\s+'
            r'queue=(?P<queue>\d+)\s+'
            r'total=(?P<total>\d+)\s+'
            r'last=(?P<msg_type>\w+)'
        )

        # ────────────────────────────────────────────────────────────
        # PATTERN: Packet Drop (if you add drop logging)
        # ────────────────────────────────────────────────────────────
        # Example input:
        # "[mav_relay] Dropped packet: LASER_PERMIT (loss simulation)"

        self.drop_pattern = re.compile(
            r'Dropped packet:\s+(?P<msg_type>\w+)'
        )

        # ────────────────────────────────────────────────────────────
        # THROTTLING: Prevent event flood
        # ────────────────────────────────────────────────────────────
        # Relay logs every 100ms → 10 events/second → crashes browser
        # Only log every N seconds
        self.last_event_time = 0.0
        self.event_throttle_seconds = 5.0  # Log relay traffic every 5 seconds
    
    async def parse_line(self, line: str):
        """Parse a single line of Relay stdout"""
        
        # ────────────────────────────────────────────────────────────
        # Try Pattern 1: Message Counter
        # ────────────────────────────────────────────────────────────
        match = self.counter_pattern.search(line)
        if match:
            direction = match.group("direction")
            queue = int(match.group("queue"))
            total = int(match.group("total"))
            msg_type = match.group("msg_type")

            # Determine which counter to update based on direction
            if "UDP->SER" in direction:
                key_prefix = "relay_udp_to_ser"
            else:  # "SER->UDP"
                key_prefix = "relay_ser_to_udp"

            # Update relay statistics in state (always update telemetry)
            await state.update_telemetry({
                f"{key_prefix}_total": total,
                f"{key_prefix}_queue": queue,
                f"{key_prefix}_last_msg": msg_type
            })

            # THROTTLE: Only log INFO events every N seconds (prevents browser crash)
            current_time = time.time()
            time_since_last_event = current_time - self.last_event_time

            if time_since_last_event >= self.event_throttle_seconds:
                self.last_event_time = current_time
                await state.add_event(
                    level="INFO",
                    src="relay",
                    code=msg_type,
                    msg=f"[mav_relay] {direction}: queue={queue} total={total} last={msg_type}"
                )

            # Alert if queue is building up (WARN events always sent immediately)
            if queue > 20:
                await state.add_event(
                    level="WARN",
                    src="relay",
                    code="HIGH_QUEUE_DEPTH",
                    msg=f"{direction} queue depth: {queue}"
                )

            return
        
        # ────────────────────────────────────────────────────────────
        # Try Pattern 2: Packet Drop
        # ────────────────────────────────────────────────────────────
        match = self.drop_pattern.search(line)
        if match:
            msg_type = match.group("msg_type")
            
            await state.add_event(
                level="INFO",
                src="relay",
                code="PACKET_DROPPED",
                msg=f"Dropped: {msg_type} (drill simulation)"
            )
            
            return
        
# ============================================================================
# HELPER: Testing Individual Patterns
# ============================================================================

async def test_parsers():
    """
    Test function to validate patterns work correctly.
    
    Why test?
    - Regex is easy to get wrong
    - Subtle format changes in logs can break parsing
    - Better to catch errors in testing than production
    
    Run with: python -m backend.parsers
    """
    print("Testing Ground Parser...")
    ground_parser = GroundParser()
    
    test_line = "  [  45%] Cmd:225.0W | Rcv:45000.0mW | Eff:20.0% | LQ:92% | RTT:34.5ms | G/D:450/89 (83%) | d=42.1m r=35.2° p=-8.1°"
    await ground_parser.parse_line(test_line)
    
    data = await state.get_telemetry_snapshot()
    assert data["commanded_pct"] == 45
    assert data["commanded_w"] == 225.0
    assert data["efficiency_pct"] == 20.0
    print("✓ Ground parser test passed")
    
    print("\nTesting Air Parser...")
    air_parser = AirParser()
    
    test_line = "[air] ✓ GRANT seq=123 | Cmd:100W | Rcv:40000.0mW | Eff:40.0% | d=50.0m | r=0.0° p=0.0°"
    await air_parser.parse_line(test_line)
    
    data = await state.get_telemetry_snapshot()
    assert data["granted"] == True
    assert data["distance_m"] == 50.0
    print("✓ Air parser test passed")
    
    print("\nAll tests passed!")

if __name__ == "__main__":
    import asyncio
    asyncio.run(test_parsers())