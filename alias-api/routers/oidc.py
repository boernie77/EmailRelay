"""OIDC SSO via Authentik for the EmailRelay web UI.

Extension auth (x-api-secret + x-username) is intentionally untouched.
"""

import os
from urllib.parse import quote

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse

from database import get_db
from models import User

router = APIRouter()

_oauth: OAuth | None = None


def _ensure_oauth() -> OAuth | None:
    global _oauth
    issuer = os.getenv("OIDC_ISSUER_URL")
    cid = os.getenv("OIDC_CLIENT_ID")
    secret = os.getenv("OIDC_CLIENT_SECRET")
    if not (issuer and cid and secret):
        return None
    if _oauth is None:
        _oauth = OAuth()
        discovery = issuer.rstrip("/") + "/.well-known/openid-configuration"
        _oauth.register(
            name="authentik",
            server_metadata_url=discovery,
            client_id=cid,
            client_secret=secret,
            client_kwargs={"scope": "openid email profile"},
        )
    return _oauth


@router.get("/auth/oidc/login")
async def oidc_login(request: Request):
    oauth = _ensure_oauth()
    if oauth is None:
        return RedirectResponse(
            "/login?sso_error=" + quote("OIDC nicht konfiguriert"),
            status_code=302,
        )
    redirect_uri = str(request.url_for("oidc_callback"))
    return await oauth.authentik.authorize_redirect(request, redirect_uri)


@router.get("/auth/oidc/callback", name="oidc_callback")
async def oidc_callback(request: Request, db: AsyncSession = Depends(get_db)):
    oauth = _ensure_oauth()
    if oauth is None:
        return RedirectResponse(
            "/login?sso_error=" + quote("OIDC nicht konfiguriert"),
            status_code=302,
        )

    try:
        token = await oauth.authentik.authorize_access_token(request)
    except OAuthError as e:
        return RedirectResponse(
            "/login?sso_error=" + quote(f"Authentik abgelehnt: {e.error}"),
            status_code=302,
        )

    claims = token.get("userinfo") or {}
    sub = claims.get("sub")
    email = claims.get("email")
    if not (sub and email):
        return RedirectResponse(
            "/login?sso_error=" + quote("OIDC-Claims unvollständig"),
            status_code=302,
        )

    user = (await db.execute(select(User).where(User.oidc_subject == sub))).scalar_one_or_none()
    if user is None:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            return RedirectResponse(
                "/login?sso_error=" + quote(f"Kein EmailRelay-Account für {email}. Bitte vom Admin einladen lassen."),
                status_code=302,
            )
        user.oidc_subject = sub
        await db.commit()

    if not user.active:
        return RedirectResponse(
            "/login?sso_error=" + quote("Konto deaktiviert"),
            status_code=302,
        )

    request.session["user_id"] = user.id
    request.session["is_admin"] = user.is_admin
    return RedirectResponse("/", status_code=302)
