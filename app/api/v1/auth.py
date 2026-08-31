"""Google SSO authentication routes.

Callback behaviour (per project decision):
- If ``frontend_redirect_url`` is configured, redirect the SPA to
  ``<frontend>#access_token=<jwt>`` (fragment, never sent to servers/logs).
- Otherwise return the token as JSON so the flow is testable via Swagger UI.
"""

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
            raise HTTPException(status_code=401, detail=str(error)) from error
        user = auth_service.get_or_create_user(db, claims)
        token = auth_service.issue_access_token(user)

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
