import socket
import json
import time
from typing import Optional

class LaserStatusDecoder:
    def __init__(self, ip, port, config_path: str = "laser_config.json"):
        self.ip = ip
        self.port = port
        self.config = self._load_config(config_path)

        # Persistent connection management
        self._socket: Optional[socket.socket] = None
        self._last_command_time = 0.0
        self._connection_timeout = 30.0  # Close connection after 30s idle

    def _load_config(self, config_path: str) -> dict:
        """Load configuration."""
        try:
            with open(config_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Config load warning: {e}")
            return {}

    def _get_connection(self) -> socket.socket:
        """Get or create persistent socket connection."""
        current_time = time.time()

        # Close stale connection
        if self._socket and (current_time - self._last_command_time) > self._connection_timeout:
            self._close_connection()

        # Create new connection if needed
        if self._socket is None:
            try:
                self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._socket.settimeout(2)
                self._socket.connect((self.ip, self.port))
            except Exception as e:
                self._socket = None
                raise

        self._last_command_time = current_time
        return self._socket

    def _close_connection(self):
        """Close persistent connection."""
        if self._socket:
            try:
                self._socket.close()
            except:
                pass
            finally:
                self._socket = None

    def __del__(self):
        """Cleanup on destruction."""
        self._close_connection()

    def send_command(self, sock, cmd):
        try:
            sock.sendall(f"{cmd}\r".encode('ascii'))
            data = sock.recv(1024).decode('ascii').strip()
            if ':' in data:
                return data.split(':', 1)[1].strip()
            return data
        except Exception as e:
            print(f"Comms Error: {e}")
            return None

    def decode_status_word(self, status_int):
        """
        Decode the 32-bit STA status integer into UI-ready flags.
        Based on IPG Photonics YLR-U3 specification.
        """
        return {
            # === STATUS INDICATORS (Bits 0-31) ===

            # Alarms (Red indicators)
            "cmd_buffer_overflow": bool((status_int >> 0) & 1),      # Bit 0
            "alarm_overheat": bool((status_int >> 1) & 1),           # Bit 1
            "alarm_back_reflection": bool((status_int >> 3) & 1),    # Bit 3
            "pulse_too_long": bool((status_int >> 5) & 1),           # Bit 5
            "pulse_too_short": bool((status_int >> 9) & 1),          # Bit 9 (yellow)
            "high_pulse_energy": bool((status_int >> 17) & 1),       # Bit 17
            "power_supply_failure": bool((status_int >> 19) & 1),    # Bit 19
            "duty_cycle_too_high": bool((status_int >> 23) & 1),     # Bit 23
            "alarm_temp_low": bool((status_int >> 24) & 1),          # Bit 24
            "power_supply_alarm": bool((status_int >> 25) & 1),      # Bit 25
            "guide_laser_alarm": bool((status_int >> 28) & 1),       # Bit 28
            "alarm_critical": bool((status_int >> 29) & 1),          # Bit 29
            "fiber_interlock": bool((status_int >> 30) & 1),         # Bit 30
            "high_average_power": bool((status_int >> 31) & 1),      # Bit 31

            # Status Indicators (Green/Gray indicators)
            "emission_on": bool((status_int >> 2) & 1),              # Bit 2
            "ext_power_control": bool((status_int >> 4) & 1),        # Bit 4
            "guide_laser_on": bool((status_int >> 8) & 1),           # Bit 8
            "pulse_mode": bool((status_int >> 10) & 1),              # Bit 10
            "power_supply_on": bool((status_int >> 11) & 1),         # Bit 11
            "modulation_mode": bool((status_int >> 12) & 1),         # Bit 12
            "gate_mode": bool((status_int >> 16) & 1),               # Bit 16
            "ext_emission_control": bool((status_int >> 18) & 1),    # Bit 18
            "waveform_mode": bool((status_int >> 22) & 1),           # Bit 22
            "ext_guide_control": bool((status_int >> 27) & 1),       # Bit 27

            # Warnings (Yellow indicators)
            "humidity_too_high": bool((status_int >> 7) & 1),        # Bit 7
        }

    def get_laser_telemetry(self):
        """Returns full JSON-ready dictionary for the frontend."""
        data = {}

        try:
            # Use persistent connection
            s = self._get_connection()

            # Average Output Power (ch1) in Watts
            avg_pwr_resp = self.send_command(s, "ROP")
            data['avg_power_w'] = 0.0 if avg_pwr_resp == "OFF" else float(avg_pwr_resp)

            # Peak Power (ch1) in Watts
            try:
                peak_pwr_resp = self.send_command(s, "RPP")
                data['peak_power_w'] = 0.0 if peak_pwr_resp == "OFF" else float(peak_pwr_resp)
            except Exception:
                data['peak_power_w'] = 0.0

            # Case Temperature in Celsius
            case_temp_resp = self.send_command(s, "RCT")
            data['case_temperature_c'] = float(case_temp_resp) if case_temp_resp else 0.0

            # Board Temperature in Celsius
            try:
                board_temp_resp = self.send_command(s, "RBT")
                data['board_temperature_c'] = float(board_temp_resp) if board_temp_resp else 0.0
            except Exception:
                data['board_temperature_c'] = 0.0

            # Current Setpoint (%)
            set_resp = self.send_command(s, "RCS")
            data['setpoint_pct'] = float(set_resp) if set_resp else 0.0

            # Commanded Angular Velocity
            try:
                rcw_resp = self.send_command(s, "RCW")
                data["commanded_w"] = float(rcw_resp) if rcw_resp else 0.0
            except Exception:
                data["commanded_w"] = 0.0

            # Status Word (32-bit)
            status_resp = self.send_command(s, "STA")
            if status_resp:
                status_int = int(status_resp)
                data['status_flags'] = self.decode_status_word(status_int)
                data['status_word'] = status_int
            else:
                data['status_flags'] = {}
                data['status_word'] = 0

            # Firmware Info
            try:
                device_id_resp = self.send_command(s, "RID")
                data['device_id'] = device_id_resp if device_id_resp else "Unknown"

                revision_resp = self.send_command(s, "RFV")
                data['firmware_revision'] = revision_resp if revision_resp else "Unknown"
            except Exception:
                data['device_id'] = "Unknown"
                data['firmware_revision'] = "Unknown"

            data['connected'] = True
            data['error'] = None

        except Exception as e:
            # Close connection on error
            self._close_connection()

            data['connected'] = False
            data['error'] = str(e)
            data['status_flags'] = {"emission_on": False, "alarm_critical": True}
            data['avg_power_w'] = 0.0
            data['peak_power_w'] = 0.0
            data['case_temperature_c'] = 0.0
            data['board_temperature_c'] = 0.0
            data['setpoint_pct'] = 0.0
            data['commanded_w'] = 0.0
            data['status_word'] = 0
            data['device_id'] = "Unknown"
            data['firmware_revision'] = "Unknown"

        return data

    def enable_emission(self):
        """Enable laser emission."""
        try:
            s = self._get_connection()
            response = self.send_command(s, "EMON")
            if response and "OK" in response:
                return {"success": True, "message": "Laser emission enabled"}
            elif response and "ERROR_PS_OFF" in response:
                return {"success": False, "message": "Cannot enable: Power supply is off"}
            elif response and "ERROR_ALARM" in response:
                return {"success": False, "message": "Cannot enable: Critical alarm active"}
            else:
                return {"success": False, "message": f"Enable failed: {response}"}
        except Exception as e:
            self._close_connection()
            return {"success": False, "message": f"Connection error: {str(e)}"}

    def disable_emission(self):
        """Disable laser emission."""
        try:
            s = self._get_connection()
            response = self.send_command(s, "EMOFF")
            if response and "OK" in response:
                return {"success": True, "message": "Laser emission disabled"}
            else:
                return {"success": False, "message": f"Disable failed: {response}"}
        except Exception as e:
            self._close_connection()
            return {"success": False, "message": f"Connection error: {str(e)}"}

    def set_power_setpoint(self, percent: float):
        """Set laser power setpoint (0-100%)."""
        if not (0 <= percent <= 100):
            return {"success": False, "message": "Setpoint must be between 0 and 100%", "setpoint": 0}
        try:
            s = self._get_connection()
            response = self.send_command(s, f"SCS {percent}")
            if response and "OK" in response:
                return {"success": True, "message": f"Setpoint set to {percent}%", "setpoint": percent}
            elif response and "ERROR_RANGE" in response:
                return {"success": False, "message": "Setpoint out of range (0-100%)", "setpoint": 0}
            else:
                return {"success": False, "message": f"Setpoint command failed: {response}", "setpoint": 0}
        except Exception as e:
            self._close_connection()
            return {"success": False, "message": f"Connection error: {str(e)}", "setpoint": 0}
