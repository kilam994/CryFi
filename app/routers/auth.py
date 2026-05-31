"""Login / logout / session-check endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from .. import config
from ..core import auth

router = APIRouter(prefix="/api", tags=["auth"])


class Login(BaseModel):
    username: str
    password: str


class ChangePassword(BaseModel):
    current_password: str
    new_password: str


@router.post("/login")
def login(body: Login, request: Request, response: Response) -> dict:
    if not auth.check_credentials(body.username, body.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = auth.create_session()
    # Mark the cookie Secure when served over HTTPS (e.g. behind a TLS proxy);
    # uvicorn --proxy-headers makes request.url.scheme reflect X-Forwarded-Proto.
    secure = request.url.scheme == "https"
    response.set_cookie(
        auth.COOKIE_NAME, token,
        httponly=True, samesite="lax", path="/", secure=secure,
        max_age=int(config.SESSION_HOURS * 3600),
    )
    return {"ok": True, "user": auth.current_user()}


@router.post("/change-password")
def change_password(body: ChangePassword, request: Request) -> dict:
    # The auth gate already requires a session; re-verify the current password.
    if not auth.valid_session(request.cookies.get(auth.COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="Not signed in")
    if not auth.check_credentials(auth.current_user(), body.current_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if len(body.new_password) < 4:
        raise HTTPException(status_code=400, detail="New password must be at least 4 characters")
    auth.set_password(body.new_password)
    return {"ok": True}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    auth.destroy_session(request.cookies.get(auth.COOKIE_NAME))
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
def me(request: Request) -> dict:
    if auth.valid_session(request.cookies.get(auth.COOKIE_NAME)):
        return {"authenticated": True, "user": auth.current_user()}
    return {"authenticated": False}
