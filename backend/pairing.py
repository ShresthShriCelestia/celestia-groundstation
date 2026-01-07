"""
Pairing and Authentication System
Provides secure pairing for local devices to connect to the ground station
"""
import secrets
import random
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from pydantic import BaseModel
import logging
from pathlib import Path
import json

logger = logging.getLogger(__name__)


class PairedDevice(BaseModel):
    """Represents a paired device"""
    token: str
    device_name: str
    device_type: str  # "browser", "mobile", "desktop_app"
    paired_at: datetime
    last_seen: datetime
    access_level: str = "operator"  # "view_only", "operator", "admin"


class PairingManager:
    """Manages device pairing and authentication"""

    def __init__(self, storage_file: str = "./data/paired_devices.json"):
        self.storage_file = Path(storage_file)
        # New: Path for the ephemeral pairing state (code/timeout)
        self.pairing_state_file = Path(storage_file).parent / "pairing_state.json"
        
        self.pairing_code: Optional[int] = None
        self.pairing_expires_at: Optional[datetime] = None
        self.paired_devices: Dict[str, PairedDevice] = {}
        
        # Ensure directory exists
        self.storage_file.parent.mkdir(parents=True, exist_ok=True)
        
        self._load_paired_devices()
        self._load_pairing_state()

    def _load_paired_devices(self):
        """Load paired devices from storage"""
        if self.storage_file.exists():
            try:
                with open(self.storage_file, 'r') as f:
                    data = json.load(f)
                    for token, device_data in data.items():
                        # Convert string dates back to datetime
                        device_data['paired_at'] = datetime.fromisoformat(device_data['paired_at'])
                        device_data['last_seen'] = datetime.fromisoformat(device_data['last_seen'])
                        self.paired_devices[token] = PairedDevice(**device_data)
                logger.info(f"Loaded {len(self.paired_devices)} paired devices")
            except Exception as e:
                logger.error(f"Failed to load paired devices: {e}")

    def _save_paired_devices(self):
        """Save paired devices to storage"""
        try:
            data = {}
            for token, device in self.paired_devices.items():
                device_dict = device.dict()
                # Convert datetime to ISO format string
                device_dict['paired_at'] = device_dict['paired_at'].isoformat()
                device_dict['last_seen'] = device_dict['last_seen'].isoformat()
                data[token] = device_dict

            with open(self.storage_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save paired devices: {e}")

    def _load_pairing_state(self):
        """Load active pairing state from storage"""
        if self.pairing_state_file.exists():
            try:
                with open(self.pairing_state_file, 'r') as f:
                    data = json.load(f)
                    if data.get('pairing_code') and data.get('pairing_expires_at'):
                        self.pairing_code = data['pairing_code']
                        self.pairing_expires_at = datetime.fromisoformat(data['pairing_expires_at'])
                        
                        # Check if still valid
                        if datetime.now() >= self.pairing_expires_at:
                            self.pairing_code = None
                            self.pairing_expires_at = None
                            self._save_pairing_state()
            except Exception as e:
                logger.error(f"Failed to load pairing state: {e}")

    def _save_pairing_state(self):
        """Save active pairing state to storage"""
        try:
            data = {
                'pairing_code': self.pairing_code,
                'pairing_expires_at': self.pairing_expires_at.isoformat() if self.pairing_expires_at else None
            }
            with open(self.pairing_state_file, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            logger.error(f"Failed to save pairing state: {e}")

    def start_pairing_mode(self, timeout_seconds: int = 300) -> int:
        """
        Start pairing mode and generate a 6-digit pairing code.
        Returns the pairing code that should be displayed to the user.
        """
        self.pairing_code = random.randint(100000, 999999)
        self.pairing_expires_at = datetime.now() + timedelta(seconds=timeout_seconds)

        logger.info(f"═══════════════════════════════════════")
        logger.info(f"  PAIRING MODE ACTIVE")
        logger.info(f"  ")
        logger.info(f"  Code: {self.pairing_code}")
        logger.info(f"  ")
        logger.info(f"  Valid for: {timeout_seconds // 60} minutes")
        logger.info(f"═══════════════════════════════════════")

        self._save_pairing_state()
        return self.pairing_code

    def cancel_pairing_mode(self):
        """Cancel pairing mode"""
        self.pairing_code = None
        self.pairing_expires_at = None
        self._save_pairing_state()
        logger.info("Pairing mode cancelled")

    def is_pairing_active(self) -> bool:
        """Check if pairing mode is currently active"""
        # Always refresh state from disk to catch CLI updates
        self._load_pairing_state()

        if not self.pairing_code or not self.pairing_expires_at:
            return False
            
        if datetime.now() > self.pairing_expires_at:
            # Expired
            self.pairing_code = None
            self.pairing_expires_at = None
            self._save_pairing_state()
            return False

        return True

    def pair_device(
        self, 
        code: int, 
        device_name: str, 
        device_type: str = "browser",
        access_level: str = "operator"
    ) -> Optional[str]:
        """
        Pair a device using the pairing code.
        Returns authentication token if successful, None otherwise.
        """
        # Refresh state
        self._load_pairing_state()

        # Check if pairing is active
        if not self.is_pairing_active():
            logger.warning("Pairing attempt with no active pairing mode")
            return None

        # Verify code
        try:
            if int(code) != self.pairing_code:
                logger.warning(f"Invalid pairing code attempt: {code}")
                return None
        except (ValueError, TypeError):
            return None

        # Generate secure token
        token = secrets.token_urlsafe(32)

        # Create paired device
        device = PairedDevice(
            token=token,
            device_name=device_name,
            device_type=device_type,
            paired_at=datetime.now(),
            last_seen=datetime.now(),
            access_level=access_level
        )

        self.paired_devices[token] = device
        self._save_paired_devices()

        # Clear pairing mode
        self.pairing_code = None
        self.pairing_expires_at = None
        self._save_pairing_state()

        logger.info(f"✓ Device paired successfully: {device_name} ({device_type})")
        return token

    def verify_token(self, token: str) -> Optional[PairedDevice]:
        """
        Verify an authentication token.
        Returns PairedDevice if valid, None otherwise.
        """
        if token not in self.paired_devices:
            return None

        device = self.paired_devices[token]
        # Update last seen
        device.last_seen = datetime.now()
        self._save_paired_devices()

        return device

    def unpair_device(self, token: str) -> bool:
        """Unpair a device"""
        if token in self.paired_devices:
            device = self.paired_devices.pop(token)
            self._save_paired_devices()
            logger.info(f"Device unpaired: {device.device_name}")
            return True
        return False

    def get_paired_devices(self) -> List[PairedDevice]:
        """Get list of all paired devices"""
        return list(self.paired_devices.values())

    def unpair_all(self):
        """Unpair all devices (factory reset)"""
        count = len(self.paired_devices)
        self.paired_devices.clear()
        self._save_paired_devices()
        logger.info(f"All devices unpaired (removed {count} devices)")

    def get_status(self) -> dict:
        """Get pairing system status"""
        # Refresh state for accurate reporting
        self._load_pairing_state()
        
        active = self.is_pairing_active()
        
        return {
            "pairing_active": active,
            "pairing_expires_at": self.pairing_expires_at.isoformat() if active else None,
            "code": self.pairing_code if active else None,
            "paired_device_count": len(self.paired_devices),
            "paired_devices": [
                {
                    "name": d.device_name,
                    "type": d.device_type,
                    "access_level": d.access_level,
                    "paired_at": d.paired_at.isoformat(),
                    "last_seen": d.last_seen.isoformat()
                }
                for d in self.paired_devices.values()
            ]
        }


# Singleton instance
pairing_manager = PairingManager("/home/ce/celestia-groundstation/data/paired_devices.json")
