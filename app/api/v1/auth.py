"""SSO (OAuth2/OIDC) authentication routes for Google and Microsoft Entra ID.

SPA Frontend-First flow:
1. Frontend calls GET /api/v1/auth/{provider}/login?redirect=false to obtain the authorization URL & state.
2. User authenticates on the provider and is redirected directly to the SPA callback route.
3. The SPA sends code & state via POST /api/v1/auth/{provider}/callback.
4. Backend verifies state, exchanges code for user profile, and returns the session JWT as JSON.
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
from app.services import azure_auth_service, google_auth_service, token_service
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


class OAuthLoginResponse(StrictModel):
    authorization_url: str
    state: str


class OAuthCallbackRequest(StrictModel):
    code: str
    state: str


GoogleLoginResponse = OAuthLoginResponse
GoogleCallbackRequest = OAuthCallbackRequest
AzureLoginResponse = OAuthLoginResponse
AzureCallbackRequest = OAuthCallbackRequest


def create_auth_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])

    @router.get("/google/login", response_model=GoogleLoginResponse)
    def google_login(request: Request, redirect: bool = False):
        if not settings.google_client_id or not settings.google_client_secret:
            raise HTTPException(status_code=503, detail="Google SSO is not configured")
        state = _state_serializer.dumps({"nonce": secrets.token_urlsafe(16), "provider": "google"})
        url = google_auth_service.google_authorization_url(state)

        accept = request.headers.get("accept", "")
        if redirect and ("text/html" in accept or "application/json" not in accept):
            return RedirectResponse(url)
        return GoogleLoginResponse(authorization_url=url, state=state)

    @router.post("/google/callback", response_model=TokenResponse)
    def google_callback(payload: GoogleCallbackRequest, db: Session = Depends(get_db)) -> TokenResponse:
        try:
            _state_serializer.loads(payload.state)
        except BadData as error:
            raise HTTPException(status_code=400, detail="Invalid OAuth state") from error

        try:
            claims = google_auth_service.exchange_code_for_id_token(payload.code)
        except google_auth_service.GoogleAuthError as error:
            logger.error("Google SSO callback: token exchange failed: %s", error)
            raise HTTPException(status_code=401, detail=str(error)) from error

        user = google_auth_service.get_or_create_user(db, claims)
        token = token_service.issue_access_token(user)
        logger.info("Google SSO callback: issued access token for user id=%s", user.id)

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
            url = azure_auth_service.azure_authorization_url(state)
        except azure_auth_service.AzureAuthError as error:
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
            user_info = azure_auth_service.exchange_code_for_azure_user(payload.code)
        except azure_auth_service.AzureAuthError as error:
            logger.error("Azure SSO callback: token exchange failed: %s", error)
            raise HTTPException(status_code=401, detail=str(error)) from error

        user = azure_auth_service.get_or_create_azure_user(db, user_info)
        token = token_service.issue_access_token(user)
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
