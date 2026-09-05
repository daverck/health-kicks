"""Google SSO authentication routes.

Callback behaviour (per project decision):
- If ``frontend_redirect_url`` is configured, redirect the SPA to
  ``<frontend>#access_token=<jwt>`` (fragment, never sent to servers/logs).
- Otherwise return the token as JSON so the flow is testable via Swagger UI.
"""

import logging
import secrets

from itsdangerous import BadData, URLSafeSerializer
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.core.config import settings
from app.db.database import get_db
from app.db.models import User
from app.schemas.cloud import StrictModel
from app.services import auth_service
from fastapi import Depends

_state_serializer = URLSafeSerializer(settings.jwt_secret, salt="oauth-state")
logger = logging.getLogger(__name__)


class UserResponse(StrictModel):
    id: int
    email: str
    name: str | None = None
    avatar_url: str | None = None
    role: str
    is_active: bool


class TokenResponse(StrictModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class AzureLoginResponse(StrictModel):
    authorization_url: str
    state: str


class AzureCallbackRequest(StrictModel):
    code: str
    state: str


def create_auth_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])

    @router.get("/google/login")
    def google_login() -> RedirectResponse:
        if not settings.google_client_id or not settings.google_client_secret:
            raise HTTPException(status_code=503, detail="Google SSO is not configured")
        state = _state_serializer.dumps({"nonce": "hk"})
        return RedirectResponse(auth_service.google_authorization_url(state))

    @router.get("/google/callback")
    def google_callback(code: str, state: str, db: Session = Depends(get_db)):
        try:
            _state_serializer.loads(state)
        except BadData as error:
            raise HTTPException(status_code=400, detail="Invalid OAuth state") from error
        try:
            claims = auth_service.exchange_code_for_id_token(code)
        except auth_service.GoogleAuthError as error:
            logger.error("SSO callback: token exchange failed: %s", error)
            raise HTTPException(status_code=401, detail=str(error)) from error
        user = auth_service.get_or_create_user(db, claims)
        token = auth_service.issue_access_token(user)
        logger.info("SSO callback: issued access token for user id=%s", user.id)

        if settings.frontend_redirect_url:
            separator = "&" if "?" in settings.frontend_redirect_url else "?"
            return RedirectResponse(
                f"{settings.frontend_redirect_url}{separator}access_token={token}"
            )
        return TokenResponse(
            access_token=token,
            user=UserResponse(
                id=user.id,
                email=user.email,
                name=user.name,
                avatar_url=user.avatar_url,
                role=user.role.value,
                is_active=user.is_active,
            ),
        )

    @router.get("/azure/login")
    def azure_login(request: Request, redirect: bool = False):
        if not settings.azure_client_id or not settings.azure_client_secret:
            raise HTTPException(status_code=503, detail="Azure SSO is not configured")
        state = _state_serializer.dumps({"nonce": secrets.token_urlsafe(16), "provider": "azure"})
        try:
            url = auth_service.azure_authorization_url(state)
        except auth_service.AzureAuthError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

        accept = request.headers.get("accept", "")
        if redirect and ("text/html" in accept or "application/json" not in accept):
            return RedirectResponse(url)
        return AzureLoginResponse(authorization_url=url, state=state)

    @router.post("/azure/callback", response_model=TokenResponse)
    def azure_callback(payload: AzureCallbackRequest, db: Session = Depends(get_db)) -> TokenResponse:
        try:
            _state_serializer.loads(payload.state)
        except BadData as error:
            raise HTTPException(status_code=400, detail="Invalid OAuth state") from error

        try:
            user_info = auth_service.exchange_code_for_azure_user(payload.code)
        except auth_service.AzureAuthError as error:
            logger.error("Azure SSO callback: token exchange failed: %s", error)
            raise HTTPException(status_code=401, detail=str(error)) from error

        user = auth_service.get_or_create_azure_user(db, user_info)
        token = auth_service.issue_access_token(user)
        logger.info("Azure SSO callback: issued access token for user id=%s", user.id)

        return TokenResponse(
            access_token=token,
            user=UserResponse(
                id=user.id,
                email=user.email,
                name=user.name,
                avatar_url=user.avatar_url,
                role=user.role.value,
                is_active=user.is_active,
            ),
        )

    @router.get("/me", response_model=UserResponse)
    def me(user: CurrentUser) -> UserResponse:
        return UserResponse(
            id=user.id,
            email=user.email,
            name=user.name,
            avatar_url=user.avatar_url,
            role=user.role.value,
            is_active=user.is_active,
        )

    return router
