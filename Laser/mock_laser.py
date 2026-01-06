import socket
import threading
import time
import random
import json

class MockLaser:
    def __init__(self, ip, port, config_path: str = "laser_config.json"):
        self.ip = ip
        self.port = port
        self.config = self._load_config(config_path)

        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((self.ip, self.port))
        self.server_socket.listen(1)

        # Basic States
        self.emission_on = True
        self.guide_laser_on = False
        self.power_supply_on = True
        self.critical_alarm = False
        
        # New Expanded Telemetry Data
        self.avg_power = 1500.0
        self.peak_power = 1505.0  # Added for your new telemetry
        self.case_temp = 25.0
        self.board_temp = 32.0    # Added for your new telemetry
        self.setpoint = 50.0 
        self.device_id = "MOCK-UV-LASER-01"
        self.fw_rev = "v2.4.1"

    def _load_config(self, config_path: str) -> dict:
        try:
            with open(config_path, 'r') as f:
                return json.load(f)
        except:
            return {}

    def calculate_status_word(self):
        """Calculate the 32-bit status word."""
        status = 0
        if self.emission_on: status |= (1 << 2)
        if self.guide_laser_on: status |= (1 << 8)
        if self.power_supply_on: status |= (1 << 11)
        status |= (1 << 12) 
        if self.critical_alarm: status |= (1 << 29)
        return status

    def handle_client(self, conn, addr):
        print(f"MockLaser: Connection from {addr}")
        buffer = ""
        while True:
            try:
                data = conn.recv(1024).decode("ascii")
                if not data: break
                buffer += data
                if '\r' in buffer:
                    commands = buffer.split('\r')
                    for cmd in commands[:-1]:
                        self.process_command(conn, cmd)
                    buffer = commands[-1]
            except Exception as e:
                print(f"MockLaser: Error {e}")
                break
        conn.close()

    def process_command(self, conn, cmd):
        response = ""
        cmd = cmd.strip()

        # Handle Read Commands (Matched to your new backend structure)
        if cmd == "ROP":
            val = str(self.avg_power) if self.emission_on else "OFF"
            response = f"ROP:{val}\r"
        elif cmd == "RPP": # Mock Peak Power
            val = str(self.peak_power + random.uniform(0, 10)) if self.emission_on else "0.0"
            response = f"RPP:{val}\r"
        elif cmd == "RCT": # Case Temp
            val = self.case_temp + random.uniform(-0.1, 0.1)
            response = f"RCT:{val:.2f}\r"
        elif cmd == "RBT": # Board Temp
            val = self.board_temp + random.uniform(-0.1, 0.1)
            response = f"RBT:{val:.2f}\r"
        elif cmd == "RCS":
            response = f"RCS:{self.setpoint:.1f}\r"
        elif cmd == "STA":
            response = f"STA:{self.calculate_status_word()}\r"
        elif cmd == "RID": # Device ID
            response = f"RID:{self.device_id}\r"
        elif cmd == "RFV": # Firmware Version
            response = f"RFV:{self.fw_rev}\r"

        # Handle Write Commands
        elif cmd == "EMON":
            self.emission_on = True
            self.avg_power = self.setpoint * 15.0
            response = "EMON:OK\r"
        elif cmd == "EMOFF":
            self.emission_on = False
            self.avg_power = 0.0
            response = "EMOFF:OK\r"
        elif cmd.startswith("SCS "):
            try:
                self.setpoint = float(cmd.split()[1])
                if self.emission_on: self.avg_power = self.setpoint * 15.0
                response = "SCS:OK\r"
            except: response = "SCS:ERROR_FORMAT\r"
        else:
            response = f"{cmd}:OK\r" # Default ack for PSON/PSOFF

        conn.sendall(response.encode('ascii'))

    def start(self):
        print(f"MockLaser: Running on {self.ip}:{self.port}")
        while True:
            conn, addr = self.server_socket.accept()
            threading.Thread(target=self.handle_client, args=(conn, addr)).start()

if __name__ == "__main__":
    # Ensure this port matches LASER_PORT in your .env or config.py
    sim = MockLaser('127.0.0.1', 10001)
    sim.start()
