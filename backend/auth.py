"""
auth.py — gor://a lightweight token auth

- Email + password (hashed) based users table (in Supabase)
- Login returns access token (JWT-ish, but we can keep it simple)
- get_current_user dependency for protected routes
"""

from __future__ import annotations
import os
import uuid
import time
import hashlib
import hmac
import base64
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from supabase import create_client, Client

from .settings import get_settings

settings = get_settings()
supabase: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)

auth_router = APIRouter(prefix="/auth", tags=["auth"])
security = HTTPBearer()

# --------------------------------------------------------------------
# MODELS
# --------------------------------------------------------------------


class User(BaseModel):
    id: uuid.UUID
    email: EmailStr


class UserInDB(User):
    password_hash: str


class AuthToken(BaseModel):
    access_token: str
    token_type: str = "bearer"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class SignupRequest(BaseModel):
    email: EmailStr
    password: str


# --------------------------------------------------------------------
# HELPERS
# --------------------------------------------------------------------


def _hash_password(password: str) -> str:
    salt = os.getenv("PASSWORD_SALT", "gor-salt").encode()
    return hmac.new(salt, password.encode(), hashlib.sha256).hexdigest()


def _create_token(user_id: str) -> str:
    """
    Extremely small JWT-like token:
    base64(user_id|expires|signature)
    """
    expires = int(time.time()) + settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    raw = f"{user_id}|{expires}"
    sig = hmac.new(settings.SECRET_KEY.encode(), raw.encode(), hashlib.sha256).hexdigest()
    token_raw = f"{raw}|{sig}"
    return base64.urlsafe_b64encode(token_raw.encode()).decode()


def _decode_token(token: str) -> str:
    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        user_id, exp, sig = decoded.split("|")
        raw = f"{user_id}|{exp}"
        expected_sig = hmac.new(
            settings.SECRET_KEY.encode(), raw.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected_sig, sig):
            raise ValueError("Invalid signature")
        if int(exp) < int(time.time()):
            raise ValueError("Token expired")
        return user_id
    except Exception as exc:
        raise ValueError(f"Cannot decode token: {exc}")


def _get_user_by_email(email: str) -> Optional[UserInDB]:
    res = (
        supabase.table("users")
        .select("id, email, password_hash")
        .eq("email", email.lower())
        .maybe_single()
        .execute()
    )
    data = res.data
    if not data:
        return None
    return UserInDB(
        id=uuid.UUID(data["id"]), email=data["email"], password_hash=data["password_hash"]
    )


def _get_user_by_id(user_id: str) -> Optional[User]:
    res = (
        supabase.table("users")
        .select("id, email")
        .eq("id", user_id)
        .maybe_single()
        .execute()
    )
    data = res.data
    if not data:
        return None
    return User(id=uuid.UUID(data["id"]), email=data["email"])


# --------------------------------------------------------------------
# API
# --------------------------------------------------------------------


@auth_router.post("/signup", response_model=AuthToken)
def signup(body: SignupRequest):
    if _get_user_by_email(body.email):
        raise HTTPException(status_code=400, detail="User already exists")

    pwd_hash = _hash_password(body.password)
    res = (
        supabase.table("users")
        .insert({"email": body.email.lower(), "password_hash": pwd_hash})
        .select("id,email")
        .single()
        .execute()
    )
    uid = res.data["id"]
    token = _create_token(uid)
    return AuthToken(access_token=token)


@auth_router.post("/login", response_model=AuthToken)
def login(body: LoginRequest):
    user = _get_user_by_email(body.email)
    if not user:
        raise HTTPException(status_code=400, detail="Invalid credentials")
    if user.password_hash != _hash_password(body.password):
        raise HTTPException(status_code=400, detail="Invalid credentials")
    token = _create_token(str(user.id))
    return AuthToken(access_token=token)


def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(security),
) -> User:
    token = creds.credentials
    try:
        user_id = _decode_token(token)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
    user = _get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user
