# backend/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path

class Settings(BaseSettings):
    """
    Server Configuration loaded from environment variables
    Why Pydantic settings? https://pydantic-docs.helpmanual.io/usage/settings/
    - Type Validation (ensures WEBSOCKET_PORT is an integer)
    - .env file support (for local development)
    - Automatic documentation generation
    """
    # Server Configuration
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    WEBSOCKET_UPDATE_HZ: float = 10.0  # Fixed typo: was WEBSOCKET_PORT
    
    # Process Paths (relative to the root of the project)
    PROJECT_ROOT: Path = Path(__file__).parent.parent
    GROUND_SCRIPT: Path = PROJECT_ROOT / "permit_ground_power_ramp.py"
    AIR_SCRIPT: Path = PROJECT_ROOT / "permit_air_power_ramp.py"
    RELAY_SCRIPT: Path = PROJECT_ROOT / "mav_relay.py"
    
    # MAVLink configuration
    GROUND_UDP_OUT: str = "udpout:127.0.0.1:14600"
    GROUND_UDP_IN: str = "udpin:0.0.0.0:14560"
    AIR_UDP_IN: str = "udpin:0.0.0.0:14600"
    AIR_UDP_OUT: str = "udpout:127.0.0.1:14560"
    RELAY_UDP_IN: str = "udpin:0.0.0.0:14600"
    RELAY_UDP_OUT: str = "udpout:127.0.0.1:14560"
    RELAY_SERIAL: str = "/tmp/ELRS_TX,57600"
    
    # Safety Limits
    MIN_PERMIT_TTL_MS: int = 200
    MAX_PERMIT_TTL_MS: int = 2000
    MIN_SEND_HZ: float = 1.0
    MAX_SEND_HZ: float = 50.0
    MAX_POWER_W: float = 500.0

    # Virtual ELRS serial pair (created by socat)
    ELRS_TX_LINK: str = "/tmp/ELRS_TX,57600"  # Relay writes here
    ELRS_RX_LINK: str = "/tmp/ELRS_RX,57600"  # Air reads here
    SOCAT_BIN: str = "socat"
    # Keep existing RELAY_SERIAL for backward compat, but not used after this change
    RELAY_SERIAL: str = "/tmp/ELRS_TX,57600"
    
    # Data Persistence
    DATA_DIR: Path = PROJECT_ROOT / "data"

    # Laser Configuration
    LASER_IP: str = "127.0.0.1"  # Use localhost for mock laser (change to 192.168.3.230 for real hardware)
    LASER_PORT: int = 10001
    LASER_CONFIG_PATH: Path = PROJECT_ROOT.parent / "Laser" / "laser_config.json"

    # Safety Bypass for Testing (DANGER: Only use for mock laser testing!)
    # Set to True to disable Photon Handshake safety checks
    # WARNING: NEVER enable this with a real laser - safety interlocks MUST be active
    BYPASS_PHOTON_HANDSHAKE: bool = False  # Set to True to test laser controls without PX4
    
    # # Security
    # ENABLE_AUTH: bool = False
    # API_KEY: str = "changeme"
    # Security
    ENABLE_AUTH: bool = True            # <— set to True to enforce
    JWT_SECRET: str = "change-me-please"
    JWT_ALG: str = "HS256"
    ACCESS_TOKEN_MIN: int = 120         # minutes (2 hours for dev - was 15 min)
    REFRESH_TOKEN_DAYS: int = 14
    
    # Email Configuration (SendGrid)
    SENDGRID_API_KEY: str = ""
    SENDGRID_FROM_EMAIL: str = "noreply@celestiaenergy.com"

    # CORS Configuration (Cross-Origin Resource Sharing)
    # Comma-separated list of allowed origins
    # For hybrid architecture: include cloud frontend + local dev
    CORS_ORIGINS: str = (
        "http://localhost:5173,"
        "http://localhost:3000,"
        "http://127.0.0.1:5173,"
        "http://127.0.0.1:3000,"
        "https://ui.celestiaenergy.com,"  # Cloud frontend (when ready)
        "https://*.vercel.app"  # Temporary Vercel URLs
    )

    # HTTPS Configuration
    ENABLE_HTTPS: bool = True  # Enable self-signed HTTPS for local deployment
    CERT_DIR: Path = PROJECT_ROOT / "certs"

    # Device Information
    DEVICE_NAME: str = "Ground Station"  # Displayed in UI
    DEVICE_MODEL: str = "GS-Pro-v1"
    HARDWARE_ID: str = ""  # Auto-generated on first run


    # Pydantic v2 configuration
    model_config = SettingsConfigDict(
        env_file="/home/ce/celestia-groundstation/.env",
        case_sensitive=True,
        extra="allow"
    )
    
    def validate_scripts_exist(self):
        """Ensure all required scripts exist"""
        for script_name, script_path in [
            ("Ground", self.GROUND_SCRIPT),
            ("Air", self.AIR_SCRIPT),
            ("Relay", self.RELAY_SCRIPT),
        ]:
            if not script_path.exists():
                raise FileNotFoundError(f"{script_name} script not found: {script_path}")
    
    def ensure_data_dir(self):
        """Create data directory if it doesn't exist"""
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)

settings = Settings()
