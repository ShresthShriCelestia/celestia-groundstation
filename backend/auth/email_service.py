"""
Email notification service for Terra Station
Handles signup approval notifications to admins using SendGrid
"""
import uuid
from datetime import datetime, timedelta
from typing import Optional
import logging
import os
from pathlib import Path
import json

logger = logging.getLogger(__name__)

# Safe import for SendGrid
try:
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail, Email, To, Content
    SENDGRID_AVAILABLE = True
except ImportError:
    SENDGRID_AVAILABLE = False
    logger.warning("SendGrid not installed - emails will be logged only")

class EmailService:
    def __init__(self):
        from backend.config import settings
        self.admin_email = os.getenv("ADMIN_EMAIL", "shresth@celestiaenergy.com")
        self.from_email = settings.SENDGRID_FROM_EMAIL
        self.from_name = os.getenv("SENDGRID_FROM_NAME", "Terra Station")
        self.sendgrid_api_key = settings.SENDGRID_API_KEY

        # Store for pending approval tokens
        self.approval_tokens = {}  # Initialize empty dict first to prevent errors!
        
        # Path: auth/email_service.py -> parent(auth) -> parent(backend) -> data
        self.tokens_file = Path(__file__).parent.parent / "data" / "approval_tokens.json"

        # Ensure data directory exists
        self.tokens_file.parent.mkdir(parents=True, exist_ok=True)
        self._load_tokens()

        # Check if SendGrid is configured
        self.email_enabled = bool(SENDGRID_AVAILABLE and self.sendgrid_api_key)
        if not self.email_enabled:
            if not SENDGRID_AVAILABLE:
                logger.warning("⚠️  SendGrid package not installed")
            elif not self.sendgrid_api_key:
                logger.warning("⚠️  SENDGRID_API_KEY not configured in .env")
            logger.warning("⚠️  Emails will be logged only")
        else:
            logger.info(f"✅ Email service initialized with SendGrid (from: {self.from_email})")

    def generate_approval_token(self, user_id: str) -> str:
        """Generate secure token for one-click approval"""
        token = str(uuid.uuid4())
        expires_at = datetime.now() + timedelta(hours=24)  # 24 hour expiry

        self.approval_tokens[token] = {
            "user_id": user_id,
            "expires_at": expires_at.isoformat(),
            "used": False
        }
        self._save_tokens()
        return token

    def verify_approval_token(self, token: str) -> Optional[str]:
        """Verify and consume approval token, return user_id if valid"""
        if token not in self.approval_tokens:
            logger.warning(f"Invalid approval token: {token}")
            return None

        token_data = self.approval_tokens[token]

        # Check expiry
        if datetime.now() > datetime.fromisoformat(token_data["expires_at"]):
            logger.warning(f"Expired approval token: {token}")
            return None

        # Check if already used
        if token_data["used"]:
            logger.warning(f"Already used approval token: {token}")
            return None

        # Mark as used
        token_data["used"] = True
        token_data["used_at"] = datetime.now().isoformat()
        self._save_tokens()

        return token_data["user_id"]

    def _save_tokens(self):
        """Save tokens to file"""
        try:
            with open(self.tokens_file, 'w') as f:
                json.dump(self.approval_tokens, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save approval tokens: {e}")

    def _load_tokens(self):
        """Load tokens from file"""
        if self.tokens_file.exists():
            try:
                with open(self.tokens_file, 'r') as f:
                    self.approval_tokens = json.load(f)
                logger.info(f"Loaded {len(self.approval_tokens)} approval tokens")
            except Exception as e:
                logger.error(f"Failed to load approval tokens: {e}")

    def _send_sendgrid_email(self, to_email: str, subject: str, html_body: str) -> bool:
        """Send email via SendGrid API"""
        try:
            message = Mail(
                from_email=Email(self.from_email, self.from_name),
                to_emails=To(to_email),
                subject=subject,
                html_content=Content("text/html", html_body)
            )

            sg = SendGridAPIClient(self.sendgrid_api_key)
            response = sg.send(message)

            if response.status_code in [200, 201, 202]:
                logger.info(f"✅ Email sent successfully to {to_email} (status: {response.status_code})")
                return True
            else:
                logger.error(f"❌ SendGrid returned status {response.status_code}")
                return False

        except Exception as e:
            logger.error(f"❌ Failed to send email via SendGrid: {e}")
            return False

    def send_signup_approval_email(self, user_email: str, user_id: str, base_url: str = "https://groundstation.celestiaenergy.com"):
        """Send approval email to admin"""
        try:
            # Generate secure approval tokens
            approve_token = self.generate_approval_token(user_id)
            deny_token = self.generate_approval_token(user_id)

            # Create approval URLs
            approve_url = f"{base_url}/auth/admin/approve-token/{approve_token}"
            deny_url = f"{base_url}/auth/admin/deny-token/{deny_token}"

            subject = f"🛰️ Terra Station Access Request - {user_email}"

            body = f"""
            <html>
            <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                <h2 style="color: #f97316;">🛰️ Terra Station Access Request</h2>

                <div style="background: #f8fafc; padding: 20px; border-radius: 8px; margin: 20px 0;">
                    <h3>New User Registration</h3>
                    <p><strong>Email:</strong> {user_email}</p>
                    <p><strong>Registration Time:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
                    <p><strong>User ID:</strong> {user_id}</p>
                </div>

                <div style="margin: 30px 0;">
                    <a href="{approve_url}" style="background: #10b981; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; display: inline-block; margin-right: 10px;">
                        ✅ APPROVE ACCESS
                    </a>

                    <a href="{deny_url}" style="background: #ef4444; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; display: inline-block;">
                        ❌ DENY ACCESS
                    </a>
                </div>

                <hr style="margin: 30px 0;">
                <p style="color: #6b7280; font-size: 14px;">
                    This is a one-time approval for Terra Station account activation.<br>
                    Links expire in 24 hours for security.
                </p>

                <p style="color: #6b7280; font-size: 12px;">
                    Terra Station Security System<br>
                    Celestia Energy
                </p>
            </body>
            </html>
            """
            
            # Log email details
            logger.info(f"📧 Sending approval email for {user_email} to {self.admin_email}")
            logger.info(f"   Approve URL: {approve_url}")
            logger.info(f"   Deny URL: {deny_url}")
            
            # Send via SendGrid if configured, otherwise just log
            if self.email_enabled:
                return self._send_sendgrid_email(self.admin_email, subject, body)
            else:
                logger.warning("⚠️  SendGrid not configured - email logged but not sent")
                logger.info(f"Subject: {subject}")
                return False
            
        except Exception as e:
            logger.error(f"Failed to send approval email: {e}")
            return False

# Singleton instance
email_service = EmailService()
