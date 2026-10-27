"""SSO (OAuth2/OIDC) authentication routes for Google and Microsoft Entra ID.

SPA Frontend-First flow:
1. Frontend calls GET /api/v1/auth/{provider}/login?redirect=false to obtain the authorization URL & state.
2. User authenticates on the provider and is redirected directly to the SPA callback route.
3. The SPA sends code & state via POST /api/v1/auth/{provider}/callback.
4. Backend verifies state, exchanges code for user profile, and returns the session JWT as JSON.
"""

import logging
from typing import Any, Callable
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_sts_service
from app.core.config import settings
from app.core.rate_limit import rate_limit
from app.db.database import get_db
from app.db.models import DeviceOwnership, User, UserRole
from app.schemas.auth import (
    AzureCallbackRequest,
    AzureLoginResponse,
    GoogleCallbackRequest,
    GoogleLoginResponse,
    IoTCredentialsRequest,
    IoTCredentialsResponse,
    OAuthCallbackRequest,
    OAuthLoginResponse,
    OAuthTicketExchangeRequest,
    RefreshTokenRequest,
    TokenResponse,
    UserResponse,
)
from app.services import azure_auth_service, google_auth_service, token_service
from app.services.aws_sts_service import AWSSTSService
from app.services.oauth_ticket_service import TICKET_TTL_SECONDS, oauth_ticket_store
from app.services.pkce_service import (
    generate_oauth_state,
    get_state_platform,
    get_timed_serializer,
    verify_oauth_state,
    verify_pkce_challenge,
)

logger = logging.getLogger(__name__)

# Expose aliases and memory store for tests / backward compatibility
_ONE_TIME_TICKETS = oauth_ticket_store._memory_store
_verify_pkce_challenge = verify_pkce_challenge


def _create_one_time_ticket(
    access_token: str,
    refresh_token: str,
    user_resp: UserResponse,
    code_challenge: str | None = None,
) -> str:
    """Create a short-lived one-time authentication ticket to avoid exposing JWTs in redirect URLs."""
    return oauth_ticket_store.create_ticket(
        access_token=access_token,
        refresh_token=refresh_token,
        user=user_resp,
        code_challenge=code_challenge,
    )


def _handle_oauth_get_callback(
    provider: str,
    code: str | None,
    state: str | None,
    error: str | None,
    error_description: str | None,
    db: Session,
    exchange_and_provision: Callable[[str, bool, Session], User],
) -> RedirectResponse:
    """Common handler for browser GET redirect callbacks from SSO providers."""
    raw_platform = get_state_platform(state, config=settings)
    is_mobile = (raw_platform == "mobile")
    frontend_base = getattr(settings, "frontend_url", "https://healthkicks.duckdns.org").rstrip("/")

    def _redirect_error(message: str) -> RedirectResponse:
        encoded_msg = quote(message, safe="")
        if is_mobile:
            return RedirectResponse(f"healthkicks://auth/callback?error={encoded_msg}")
        return RedirectResponse(f"{frontend_base}/login?error={encoded_msg}")

    if error:
        err_detail = error_description or error
        logger.warning("%s SSO callback GET received error: %s", provider.capitalize(), err_detail)
        return _redirect_error(err_detail)

    if not code or not state:
        return _redirect_error("Paramètres d'autorisation manquants (code ou state absent)")

    try:
        state_data = verify_oauth_state(state, expected_provider=provider, config=settings)
        is_mobile = (state_data.get("platform") == "mobile")
    except HTTPException as exc:
        return _redirect_error(exc.detail or "Paramètre state invalide ou expiré")

    try:
        user = exchange_and_provision(code, is_mobile, db)
        access_token = token_service.issue_access_token(user)
        refresh_token = token_service.issue_refresh_token(user)
    except Exception as err:
        logger.exception("%s SSO callback GET failed: %s", provider.capitalize(), err)
        return _redirect_error(str(err) if isinstance(err, (google_auth_service.GoogleAuthError, azure_auth_service.AzureAuthError)) else f"Erreur lors de l'authentification {provider}")

    ticket = _create_one_time_ticket(
        access_token=access_token,
        refresh_token=refresh_token,
        user_resp=UserResponse.from_user(user),
        code_challenge=state_data.get("cc"),
    )
    logger.info("%s SSO callback GET: success for user id=%s (is_mobile=%s)", provider.capitalize(), user.id, is_mobile)
    if is_mobile:
        return RedirectResponse(f"healthkicks://auth/callback?ticket={ticket}&state={state}")
    return RedirectResponse(f"{frontend_base}/auth/{provider}/callback?ticket={ticket}&state={state}")


def create_auth_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])

    @router.get("/google/login", response_model=GoogleLoginResponse)
    def google_login(
        request: Request,
        redirect: bool = False,
        platform: str | None = None,
        code_challenge: str | None = Query(default=None, min_length=43, max_length=128),
        code_challenge_method: str | None = Query(default="S256"),
    ):
        if not settings.google_client_id or not settings.google_client_secret:
            raise HTTPException(status_code=503, detail="Google SSO is not configured")
        if code_challenge is not None and (code_challenge_method or "S256").upper() != "S256":
            raise HTTPException(
                status_code=400, detail="Only S256 PKCE code_challenge_method is supported"
            )
        is_mobile = (redirect or platform == "mobile")
        target_platform = "mobile" if is_mobile else "web"
        state = generate_oauth_state(
            "google", platform=target_platform, code_challenge=code_challenge, config=settings
        )
        url = google_auth_service.google_authorization_url(state, is_mobile=is_mobile)

        accept = request.headers.get("accept", "")
        if redirect and ("text/html" in accept or "application/json" not in accept):
            return RedirectResponse(url)
        return GoogleLoginResponse(authorization_url=url, state=state)

    @router.get("/google/callback")
    def google_callback_get(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
        error_description: str | None = None,
        db: Session = Depends(get_db),
    ):
        def _exchange_google(auth_code: str, mobile_flag: bool, session: Session) -> User:
            claims = google_auth_service.exchange_code_for_id_token(auth_code, is_mobile=mobile_flag)
            return google_auth_service.get_or_create_user(session, claims)

        return _handle_oauth_get_callback(
            provider="google",
            code=code,
            state=state,
            error=error,
            error_description=error_description,
            db=db,
            exchange_and_provision=_exchange_google,
        )

    @router.post("/google/callback", response_model=TokenResponse)
    def google_callback(payload: GoogleCallbackRequest, db: Session = Depends(get_db)) -> TokenResponse:
        state_data = verify_oauth_state(payload.state, expected_provider="google", config=settings)
        verify_pkce_challenge(state_data.get("cc"), payload.code_verifier)
        is_mobile = (state_data.get("platform") == "mobile")

        try:
            claims = google_auth_service.exchange_code_for_id_token(payload.code, is_mobile=is_mobile)
        except google_auth_service.GoogleAuthError as error:
            logger.error("Google SSO callback: token exchange failed: %s", error)
            raise HTTPException(status_code=401, detail=str(error)) from error
        except Exception as error:
            logger.exception("Google SSO callback: unexpected error during code exchange: %s", error)
            raise HTTPException(status_code=500, detail="Internal server error during Google authentication") from error

        try:
            user = google_auth_service.get_or_create_user(db, claims)
            access_token = token_service.issue_access_token(user)
            refresh_token = token_service.issue_refresh_token(user)
        except Exception as error:
            logger.exception("Google SSO callback: user provisioning failed: %s", error)
            raise HTTPException(status_code=500, detail="User persistence failed") from error

        logger.info("Google SSO callback: issued access token for user id=%s", user.id)
        return TokenResponse.build(user, access_token, refresh_token)

    @router.get("/azure/login")
    def azure_login(
        request: Request,
        redirect: bool = False,
        platform: str | None = None,
        code_challenge: str | None = Query(default=None, min_length=43, max_length=128),
        code_challenge_method: str | None = Query(default="S256"),
    ):
        if not settings.azure_client_id or not settings.azure_client_secret:
            raise HTTPException(status_code=503, detail="Azure SSO is not configured")
        if code_challenge is not None and (code_challenge_method or "S256").upper() != "S256":
            raise HTTPException(
                status_code=400, detail="Only S256 PKCE code_challenge_method is supported"
            )
        is_mobile = (redirect or platform == "mobile")
        target_platform = "mobile" if is_mobile else "web"
        state = generate_oauth_state(
            "azure", platform=target_platform, code_challenge=code_challenge, config=settings
        )
        try:
            url = azure_auth_service.azure_authorization_url(state, is_mobile=is_mobile)
        except azure_auth_service.AzureAuthError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

        accept = request.headers.get("accept", "")
        if redirect and ("text/html" in accept or "application/json" not in accept):
            return RedirectResponse(url)
        return AzureLoginResponse(authorization_url=url, state=state)

    @router.get("/azure/callback")
    def azure_callback_get(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
        error_description: str | None = None,
        db: Session = Depends(get_db),
    ):
        def _exchange_azure(auth_code: str, mobile_flag: bool, session: Session) -> User:
            user_info = azure_auth_service.exchange_code_for_azure_user(auth_code, is_mobile=mobile_flag)
            return azure_auth_service.get_or_create_azure_user(session, user_info)

        return _handle_oauth_get_callback(
            provider="azure",
            code=code,
            state=state,
            error=error,
            error_description=error_description,
            db=db,
            exchange_and_provision=_exchange_azure,
        )

    @router.post("/azure/callback", response_model=TokenResponse)
    def azure_callback(payload: AzureCallbackRequest, db: Session = Depends(get_db)) -> TokenResponse:
        state_data = verify_oauth_state(payload.state, expected_provider="azure", config=settings)
        verify_pkce_challenge(state_data.get("cc"), payload.code_verifier)
        is_mobile = (state_data.get("platform") == "mobile")

        try:
            user_info = azure_auth_service.exchange_code_for_azure_user(payload.code, is_mobile=is_mobile)
        except azure_auth_service.AzureAuthError as error:
            logger.error("Azure SSO callback: token exchange failed: %s", error)
            raise HTTPException(status_code=401, detail=str(error)) from error
        except Exception as error:
            logger.exception("Azure SSO callback: unexpected error during code exchange: %s", error)
            raise HTTPException(status_code=500, detail="Internal server error during Azure authentication") from error

        try:
            user = azure_auth_service.get_or_create_azure_user(db, user_info)
            access_token = token_service.issue_access_token(user)
            refresh_token = token_service.issue_refresh_token(user)
        except Exception as error:
            logger.exception("Azure SSO callback: user provisioning failed: %s", error)
            raise HTTPException(status_code=500, detail="User persistence failed") from error

        logger.info("Azure SSO callback: issued access token for user id=%s", user.id)
        return TokenResponse.build(user, access_token, refresh_token)

    @router.post(
        "/token/exchange",
        response_model=TokenResponse,
        dependencies=[Depends(rate_limit("token_exchange", max_requests=20, window_seconds=60.0))],
    )
    def exchange_auth_ticket(payload: OAuthTicketExchangeRequest) -> TokenResponse:
        """Atomically exchange a one-time OAuth ticket for access and refresh tokens."""
        entry = oauth_ticket_store.consume_ticket(payload.ticket)
        if entry is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or already consumed authentication ticket",
            )
        verify_pkce_challenge(entry.get("code_challenge"), payload.code_verifier)
        user_val = entry["user"]
        user_resp = UserResponse(**user_val) if isinstance(user_val, dict) else user_val
        return TokenResponse(
            access_token=entry["access_token"],
            refresh_token=entry["refresh_token"],
            token_type="bearer",
            user=user_resp,
        )

    @router.post(
        "/refresh",
        response_model=TokenResponse,
        dependencies=[Depends(rate_limit("token_refresh", max_requests=30, window_seconds=60.0))],
    )
    def refresh(payload: RefreshTokenRequest, db: Session = Depends(get_db)) -> TokenResponse:
        sub = token_service.verify_refresh_token(payload.refresh_token)
        user = token_service.resolve_user_from_sub(db, sub)
        if user is None or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unknown or inactive user",
            )

        new_access_token = token_service.issue_access_token(user)
        new_refresh_token = token_service.issue_refresh_token(user)
        logger.info("Session refreshed for user id=%s (uuid=%s)", user.id, user.uuid)
        return TokenResponse.build(user, new_access_token, new_refresh_token)

    @router.get("/me", response_model=UserResponse)
    def me(user: CurrentUser) -> UserResponse:
        return UserResponse.from_user(user)

    @router.post("/iot-credentials", response_model=IoTCredentialsResponse)
    def get_iot_credentials(
        user: CurrentUser,
        payload: IoTCredentialsRequest | None = None,
        device_id: str | None = None,
        db: Session = Depends(get_db),
        sts_service: AWSSTSService = Depends(get_sts_service),
    ) -> IoTCredentialsResponse:
        """Exchange authenticated user session for temporary AWS STS IoT credentials."""
        requested_device_id = None
        if payload and payload.device_id:
            requested_device_id = payload.device_id.strip()
        elif device_id:
            requested_device_id = device_id.strip()

        ownerships = (
            db.query(DeviceOwnership)
            .filter_by(user_id=user.id)
            .all()
        )
        owned_device_ids = [o.device_id for o in ownerships]

        if user.role == UserRole.admin:
            if requested_device_id:
                target_device_ids = [requested_device_id]
            elif owned_device_ids:
                target_device_ids = owned_device_ids
            else:
                target_device_ids = ["*"]
        else:
            if requested_device_id:
                if requested_device_id not in owned_device_ids:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="You do not own this device",
                    )
                target_device_ids = [requested_device_id]
            else:
                if not owned_device_ids:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="No devices bound to this user account",
                    )
                target_device_ids = owned_device_ids

        credentials_data = sts_service.generate_iot_credentials(
            user_id=str(user.uuid),
            device_ids=target_device_ids,
        )
        return IoTCredentialsResponse(**credentials_data)

    return router
