from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .auth import (
    AuthContext,
    authenticate_user,
    create_access_token,
    get_current_user,
    hash_password,
    permissions_for,
    user_payload,
    verify_password,
)
from .database import get_db


router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=120)


class ChangePasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=120)
    new_password: str = Field(min_length=8, max_length=120)


@router.post("/login")
def login(payload: LoginIn, db: Annotated[Session, Depends(get_db)]) -> dict:
    user = authenticate_user(db, payload.username.strip(), payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="账号或密码错误，或账号未启用")
    context = AuthContext(user=user, permissions=permissions_for(user))
    return {
        "access_token": create_access_token(user),
        "token_type": "bearer",
        "user": user_payload(context),
    }


@router.get("/me")
def me(context: Annotated[AuthContext, Depends(get_current_user)]) -> dict:
    return user_payload(context)


@router.post("/change-password")
def change_password(
    payload: ChangePasswordIn,
    context: Annotated[AuthContext, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    user = context.user
    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="原密码不正确")
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    return {"ok": True}
