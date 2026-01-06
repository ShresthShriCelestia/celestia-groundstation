"""
SSL Certificate Manager
Generates and manages self-signed certificates for local HTTPS
"""
import os
import subprocess
from pathlib import Path
from typing import Tuple
import logging

logger = logging.getLogger(__name__)


class SSLManager:
    """Manages self-signed SSL certificates for local HTTPS"""

    def __init__(self, cert_dir: str = "./certs"):
        self.cert_dir = Path(cert_dir)
        self.cert_file = self.cert_dir / "cert.pem"
        self.key_file = self.cert_dir / "key.pem"

    def ensure_certificates(self) -> Tuple[Path, Path]:
        """
        Ensure SSL certificates exist, generate if needed.
        Returns tuple of (cert_file, key_file) paths.
        """
        if self.cert_file.exists() and self.key_file.exists():
            logger.info(f"SSL certificates found at {self.cert_dir}")
            return self.cert_file, self.key_file

        logger.info("Generating self-signed SSL certificates...")
        self._generate_certificates()
        return self.cert_file, self.key_file

    def _generate_certificates(self):
        """Generate self-signed certificate using openssl"""
        # Create cert directory
        self.cert_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Generate private key and certificate in one command
            subprocess.run([
                "openssl", "req", "-x509",
                "-newkey", "rsa:4096",
                "-keyout", str(self.key_file),
                "-out", str(self.cert_file),
                "-days", "3650",  # Valid for 10 years
                "-nodes",  # No password
                "-subj", "/CN=groundstation.local/O=Celestia Energy/C=US",
                "-addext", "subjectAltName=DNS:groundstation.local,DNS:localhost,IP:127.0.0.1"
            ], check=True, capture_output=True)

            logger.info(f"✓ SSL certificates generated successfully")
            logger.info(f"  Certificate: {self.cert_file}")
            logger.info(f"  Private key: {self.key_file}")

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to generate SSL certificates: {e.stderr.decode()}")
            raise
        except FileNotFoundError:
            logger.error("OpenSSL not found. Please install OpenSSL:")
            logger.error("  macOS: brew install openssl")
            logger.error("  Ubuntu: sudo apt-get install openssl")
            logger.error("  Windows: Download from https://slproweb.com/products/Win32OpenSSL.html")
            raise

    def get_certificate_info(self) -> dict:
        """Get information about the current certificate"""
        if not self.cert_file.exists():
            return {"status": "not_generated"}

        try:
            result = subprocess.run(
                ["openssl", "x509", "-in", str(self.cert_file), "-noout", "-dates", "-subject"],
                capture_output=True,
                text=True,
                check=True
            )

            lines = result.stdout.strip().split('\n')
            return {
                "status": "active",
                "file": str(self.cert_file),
                "details": lines
            }
        except Exception as e:
            logger.error(f"Failed to read certificate info: {e}")
            return {"status": "error", "error": str(e)}


# Singleton instance
ssl_manager = SSLManager()
