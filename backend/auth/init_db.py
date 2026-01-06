# backend/auth/init_db.py
from sqlalchemy import select
from backend.db import engine, SessionLocal
from backend.auth.model import Base, Role, User, UserRole
import bcrypt

def init_db():
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        # seed roles
        for code, desc in [("ADMIN","Full control"), ("DEVELOPER","Control actions"), ("VIEWER","Read-only")]:
            if not db.query(Role).filter_by(code=code).first():
                db.add(Role(code=code, description=desc))
        db.commit()
        # ensure one admin
        email = "shresth@celestiaenergy.com"
        pwd = "admin123"
        u = db.query(User).filter_by(email=email).first()
        if not u:
            password_hash = bcrypt.hashpw(pwd.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
            u = User(email=email, password_hash=password_hash, is_approved=True)
            db.add(u); db.commit()
        r = db.query(Role).filter_by(code="ADMIN").first()
        if not db.query(UserRole).filter_by(user_id=u.id, role_id=r.id).first():
            db.add(UserRole(user_id=u.id, role_id=r.id)); db.commit()
