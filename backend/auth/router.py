from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr
import bcrypt
from sqlalchemy import select
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
import uuid
import pyotp
import qrcode
from io import BytesIO
import base64
import logging

from backend.auth.jwt import create_access_token
from backend.auth.model import User, Role, UserRole
from backend.auth.dep import require_device_pairing, require_roles
from backend.config import settings
from backend.db import SessionLocal
from backend.auth.email_service import email_service

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    totp_code: str | None = None  # 2FA code

class MeResponse(BaseModel):
    id: uuid.UUID
    email: EmailStr
    roles: list[str]

def verify_totp(secret: str, code: str) -> bool:
    """Verify TOTP code"""
    try:
        totp = pyotp.TOTP(secret)
        return totp.verify(code, valid_window=1)  # Allow 1 window tolerance
    except:
        return False

def roles_of(user: User, db: Session) -> list[str]:
    q = (
        db.query(Role.code)
          .join(UserRole, UserRole.role_id == Role.id)
          .filter(UserRole.user_id == user.id)
    )
    return [r[0] for r in q.all()]

@router.post("/login")
def login(req: LoginRequest, device=Depends(require_device_pairing)):
    with SessionLocal() as db:
        user = db.execute(select(User).where(User.email == req.email)).scalar_one_or_none()
        if not user or not bcrypt.checkpw(req.password.encode('utf-8'), user.password_hash.encode('utf-8')):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="User disabled")
        if not user.is_approved:
            raise HTTPException(status_code=403, detail="Account pending admin approval")
        
        # Verify 2FA if enabled for user
        if user.totp_secret:
            if not req.totp_code:
                raise HTTPException(status_code=400, detail="2FA code required")
            if not verify_totp(user.totp_secret, req.totp_code):
                raise HTTPException(status_code=401, detail="Invalid 2FA code")

        roles = roles_of(user, db)
        token = create_access_token(str(user.id), user.email, roles)
        return {"access_token": token, "user": {"id": str(user.id), "email": user.email}, "roles": roles}
    
class SignupRequest(BaseModel):
    email: EmailStr
    password: str
    role: str | None = "VIEWER"  # default viewers

@router.post("/signup")
def signup(req: SignupRequest, device=Depends(require_device_pairing)):
    with SessionLocal() as db:
        if db.query(User).filter_by(email=req.email).first():
            raise HTTPException(status_code=400, detail="Email already registered")
        
        password_hash = bcrypt.hashpw(req.password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        u = User(email=req.email, password_hash=password_hash, is_approved=False)
        db.add(u); db.commit()
        
        # attach requested role if valid; default VIEWER
        # Prevent ADMIN role selection during signup
        requested_role = req.role or "VIEWER"
        if requested_role not in ["VIEWER", "DEVELOPER"]:
            requested_role = "VIEWER"  # Force to VIEWER if invalid role
        r = db.query(Role).filter_by(code=requested_role).first()
        if not r: r = db.query(Role).filter_by(code="VIEWER").first()
        db.add(UserRole(user_id=u.id, role_id=r.id)); db.commit()
        
        # Send approval email to admin
        message = "Account created successfully. Awaiting admin approval."
        try:
            email_service.send_signup_approval_email(u.email, str(u.id))
            message = "Account created successfully. Admin notification sent. Awaiting approval."
        except Exception as e:
            logger.error(f"Failed to send approval email: {e}")
            # We swallow the error so the user still gets their account created, 
            # even if the email notification failed.

        return {
            "message": message,
            "user_id": str(u.id),
            "status": "pending_approval"
        }

@router.get("/me", response_model=MeResponse)
def me(user=Depends(lambda: None)):
    # In a real app, use get_current_user; here we just parse from header via OAuth2
    from backend.auth.deps import get_current_user
    payload = get_current_user()
    return MeResponse(id=uuid.UUID(payload["sub"]), email=payload["email"], roles=payload["roles"])

# Convenience: seed roles and an initial admin/developer
@router.post("/seed")
def seed():
    with SessionLocal() as db:
        # roles
        for code, desc in [("ADMIN","Full control"), ("VIEWER","Read-only")]:
            if not db.execute(select(Role).where(Role.code==code)).scalar_one_or_none():
                db.add(Role(code=code, description=desc))
        db.commit()
        # users (change emails/passwords)
        def ensure_user(email, pwd, role_code, approved=True):
            u = db.execute(select(User).where(User.email==email)).scalar_one_or_none()
            if not u:
                password_hash = bcrypt.hashpw(pwd.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
                u = User(email=email, password_hash=password_hash, is_approved=approved)
                db.add(u); db.commit()
            # Ensure admin is always approved
            if role_code == "ADMIN" and not u.is_approved:
                u.is_approved = True
                db.commit()
            r = db.execute(select(Role).where(Role.code==role_code)).scalar_one()
            if not db.query(UserRole).filter_by(user_id=u.id, role_id=r.id).first():
                db.add(UserRole(user_id=u.id, role_id=r.id)); db.commit()
        ensure_user("admin@example.com","admin123","ADMIN", True)
        ensure_user("viewer@example.com","viewer123","VIEWER", True)
    return {"ok": True}

# 2FA Management Endpoints
@router.post("/2fa/setup")
def setup_2fa(user=Depends(require_roles("ADMIN", "VIEWER"))):
    """Generate QR code for 2FA setup"""
    with SessionLocal() as db:
        user_record = db.execute(select(User).where(User.id == uuid.UUID(user["sub"]))).scalar_one()
        
        # Generate secret if not exists
        if not user_record.totp_secret:
            user_record.totp_secret = pyotp.random_base32()
            db.commit()
        
        # Generate QR code
        totp = pyotp.TOTP(user_record.totp_secret)
        provisioning_uri = totp.provisioning_uri(
            name=user_record.email,
            issuer_name="Terra Station"
        )
        
        # Create QR code image
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(provisioning_uri)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        
        # Convert to base64
        qr_code_base64 = base64.b64encode(buffer.getvalue()).decode()
        
        return {
            "secret": user_record.totp_secret,
            "qr_code": f"data:image/png;base64,{qr_code_base64}",
            "manual_entry_key": user_record.totp_secret
        }

@router.post("/2fa/verify")
def verify_2fa_setup(code: str, user=Depends(require_roles("ADMIN", "VIEWER"))):
    """Verify 2FA setup is working"""
    with SessionLocal() as db:
        user_record = db.execute(select(User).where(User.id == uuid.UUID(user["sub"]))).scalar_one()
        
        if not user_record.totp_secret:
            raise HTTPException(status_code=400, detail="2FA not set up")
        
        if verify_totp(user_record.totp_secret, code):
            return {"verified": True, "message": "2FA setup successful"}
        else:
            raise HTTPException(status_code=400, detail="Invalid 2FA code")

@router.post("/2fa/disable")
def disable_2fa(user=Depends(require_roles("ADMIN", "VIEWER"))):
    """Disable 2FA for current user"""
    with SessionLocal() as db:
        user_record = db.execute(select(User).where(User.id == uuid.UUID(user["sub"]))).scalar_one()
        user_record.totp_secret = None
        db.commit()
        return {"message": "2FA disabled"}

# Admin Management Endpoints
@router.get("/admin/users")
def list_users(user=Depends(require_roles("ADMIN"))):
    """List all users for admin management"""
    with SessionLocal() as db:
        users = db.execute(select(User)).scalars().all()
        result = []
        for u in users:
            roles = roles_of(u, db)
            result.append({
                "id": str(u.id),
                "email": u.email,
                "is_active": u.is_active,
                "is_approved": u.is_approved,
                "has_2fa": bool(u.totp_secret),
                "roles": roles,
                "created_at": u.created_at.isoformat()
            })
        return result

@router.post("/admin/users/{user_id}/approve")
def approve_user(user_id: str, user=Depends(require_roles("ADMIN"))):
    """Approve a pending user"""
    with SessionLocal() as db:
        target_user = db.execute(select(User).where(User.id == uuid.UUID(user_id))).scalar_one_or_none()
        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")
        
        target_user.is_approved = True
        db.commit()
        return {"message": f"User {target_user.email} approved"}

@router.post("/admin/users/{user_id}/deactivate")
def deactivate_user(user_id: str, user=Depends(require_roles("ADMIN"))):
    """Deactivate a user"""
    with SessionLocal() as db:
        target_user = db.execute(select(User).where(User.id == uuid.UUID(user_id))).scalar_one_or_none()
        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")
        
        target_user.is_active = False
        db.commit()
        return {"message": f"User {target_user.email} deactivated"}

# Email-based approval endpoints
@router.get("/admin/approve-token/{token}")
def approve_user_by_token(token: str):
    """One-click user approval via email link"""
    user_id = email_service.verify_approval_token(token)
    if not user_id:
        raise HTTPException(status_code=400, detail="Invalid or expired approval token")

    with SessionLocal() as db:
        user = db.execute(select(User).where(User.id == uuid.UUID(user_id))).scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        user.is_approved = True
        db.commit()

        return HTMLResponse(f"""
        <html><body style="font-family: Arial; text-align: center; margin-top: 50px;">
            <h2 style="color: #10b981;">✅ User Approved Successfully</h2>
            <p>User <strong>{user.email}</strong> has been granted access to Terra Station.</p>
            <p>They can now log in with their credentials.</p>
        </body></html>
        """)

@router.get("/admin/deny-token/{token}")
def deny_user_by_token(token: str):
    """One-click user denial via email link"""
    user_id = email_service.verify_approval_token(token)
    if not user_id:
        raise HTTPException(status_code=400, detail="Invalid or expired approval token")

    with SessionLocal() as db:
        user = db.execute(select(User).where(User.id == uuid.UUID(user_id))).scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Delete the user account
        db.delete(user)
        db.commit()

        return HTMLResponse(f"""
        <html><body style="font-family: Arial; text-align: center; margin-top: 50px;">
            <h2 style="color: #ef4444;">❌ User Access Denied</h2>
            <p>Access request for <strong>{user.email}</strong> has been denied.</p>
            <p>The account has been removed from the system.</p>
        </body></html>
        """)
