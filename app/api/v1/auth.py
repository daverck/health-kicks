"""SSO (OAuth2/OIDC) authentication routes for Google and Microsoft Entra ID.

SPA Frontend-First flow:
1. Frontend calls GET /api/v1/auth/{provider}/login?redirect=false to obtain the authorization URL & state.
2. User authenticates on the provider and is redirected directly to the SPA callback route.
3. The SPA sends code & state via POST /api/v1/auth/{provider}/callback.
4. Backend verifies state, exchanges code for user profile, and returns the session JWT as JSON.
"""

import base64
import hashlib
import logging
import secrets
import time
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from itsdangerous import BadData, SignatureExpired, URLSafeTimedSerializer
from pydantic import Field
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_sts_service
from app.core.config import settings
from app.db.database import get_db
from app.db.models import DeviceOwnership, User, UserRole
from app.schemas.auth import (
    IoTCredentialsRequest,
    IoTCredentialsResponse,
    OAuthTicketExchangeRequest,
)
from app.schemas.cloud import StrictModel
from app.services import azure_auth_service, google_auth_service, token_service
from app.services.aws_sts_service import AWSSTSService

_timed_serializer = URLSafeTimedSerializer(settings.jwt_secret, salt="oauth-state")
logger = logging.getLogger(__name__)

_ONE_TIME_TICKETS: dict[str, dict[str, Any]] = {}
TICKET_TTL_SECONDS = 60


def generate_oauth_state(
    provider: str, platform: str = "web", code_challenge: str | None = None
) -> str:
    """Generate a signed anti-CSRF OAuth state containing a nonce, provider name, platform, and optional PKCE challenge."""
    payload: dict[str, Any] = {
        "platform": platform,
        "provider": provider,
        "nonce": secrets.token_hex(16),
    }
    if code_challenge is not None:
        payload["cc"] = code_challenge
    return _timed_serializer.dumps(payload)


def verify_oauth_state(state: str, expected_provider: str, max_age: int = 600) -> dict:
    """Validate the cryptographic signature, expiration (max_age seconds), and provider."""
    try:
        data = _timed_serializer.loads(state, max_age=max_age)
    except SignatureExpired as error:
        logger.warning("Expired OAuth state token: %s", error)
        raise HTTPException(status_code=400, detail="Invalid OAuth state: state expired") from error
    except BadData as error:
        logger.warning("Invalid OAuth state signature: %s", error)
        raise HTTPException(status_code=400, detail="Invalid OAuth state") from error

    if not isinstance(data, dict) or data.get("provider") != expected_provider:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    return data


def _verify_pkce_challenge(code_challenge: str | None, code_verifier: str | None) -> None:
    """Verify S256 PKCE code_verifier against the stored code_challenge when present."""
    if not code_challenge:
        return
    if not code_verifier:
        raise HTTPException(status_code=400, detail="Missing PKCE code_verifier")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    if not secrets.compare_digest(computed, code_challenge):
        raise HTTPException(status_code=400, detail="Invalid PKCE code_verifier")


def get_state_platform(state: str | None) -> str:
    """Extract the target platform ('mobile' or 'web') from the state payload without enforcing strict signature/expiry."""
    if not state:
        return "web"
    try:
        _, payload = _timed_serializer.loads_unsafe(state)
        if isinstance(payload, dict) and "platform" in payload:
            return str(payload["platform"])
    except Exception:
        pass
    return "web"


class UserResponse(StrictModel):
    id: int
    email: str
    name: str | None = None
    avatar_url: str | None = None
    role: str
    is_active: bool

    @classmethod
    def from_user(cls, user: User) -> "UserResponse":
        return cls(
            id=user.id,
            email=user.email,
            name=user.name,
            avatar_url=user.avatar_url,
            role=user.role.value if hasattr(user.role, "value") else str(user.role),
            is_active=user.is_active,
        )


def _create_one_time_ticket(
    access_token: str,
    refresh_token: str,
    user_resp: UserResponse,
    code_challenge: str | None = None,
) -> str:
    """Create a short-lived one-time authentication ticket to avoid exposing JWTs in redirect URLs."""
    now = time.time()
    expired_keys = [
        k for k, v in _ONE_TIME_TICKETS.items() if now - v["created_at"] > TICKET_TTL_SECONDS
    ]
    for k in expired_keys:
        _ONE_TIME_TICKETS.pop(k, None)

    ticket = secrets.token_urlsafe(32)
    _ONE_TIME_TICKETS[ticket] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user": user_resp,
        "code_challenge": code_challenge,
        "created_at": now,
    }
    return ticket


class TokenResponse(StrictModel):
    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"
    user: UserResponse

    @classmethod
    def build(cls, user: User, access_token: str, refresh_token: str | None = None) -> "TokenResponse":
        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
            user=UserResponse.from_user(user),
        )


class RefreshTokenRequest(StrictModel):
    refresh_token: str


class OAuthLoginResponse(StrictModel):
    authorization_url: str
    state: str


class OAuthCallbackRequest(StrictModel):
    code: str
    state: str
    code_verifier: str | None = Field(default=None, min_length=43, max_length=128)


GoogleLoginResponse = OAuthLoginResponse
GoogleCallbackRequest = OAuthCallbackRequest
AzureLoginResponse = OAuthLoginResponse
AzureCallbackRequest = OAuthCallbackRequest


def create_auth_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])

    def _get_frontend_base() -> str:
        return getattr(settings, "frontend_url", "https://healthkicks.duckdns.org").rstrip("/")

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
            "google", platform=target_platform, code_challenge=code_challenge
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
        """Browser redirect handler for Google OAuth2.

        Extracts the authorization code and state parameter, validates CSRF & expiration,
        exchanges code for user profile, and redirects with a short-lived one-time ticket to:
        - `healthkicks://auth/callback` if target platform is mobile
        - `{frontend_url}/auth/google/callback` (or `/login?error=`) for web clients
        """
        raw_platform = get_state_platform(state)
        is_mobile = (raw_platform == "mobile")
        frontend_base = _get_frontend_base()

        def _redirect_error(message: str) -> RedirectResponse:
            encoded_msg = quote(message, safe="")
            if is_mobile:
                return RedirectResponse(f"healthkicks://auth/callback?error={encoded_msg}")
            return RedirectResponse(f"{frontend_base}/login?error={encoded_msg}")

        if error:
            err_detail = error_description or error
            logger.warning("Google SSO callback GET received error: %s", err_detail)
            return _redirect_error(err_detail)

        if not code or not state:
            return _redirect_error("Paramètres d'autorisation manquants (code ou state absent)")

        try:
            state_data = verify_oauth_state(state, expected_provider="google")
            is_mobile = (state_data.get("platform") == "mobile")
        except HTTPException as exc:
            return _redirect_error(exc.detail or "Paramètre state invalide ou expiré")

        try:
            claims = google_auth_service.exchange_code_for_id_token(code, is_mobile=is_mobile)
        except google_auth_service.GoogleAuthError as err:
            logger.error("Google SSO callback GET: token exchange failed: %s", err)
            return _redirect_error(str(err))
        except Exception as err:
            logger.exception("Google SSO callback GET: unexpected error: %s", err)
            return _redirect_error("Erreur serveur lors de l'authentification Google")

        try:
            user = google_auth_service.get_or_create_user(db, claims)
            access_token = token_service.issue_access_token(user)
            refresh_token = token_service.issue_refresh_token(user)
        except Exception as err:
            logger.exception("Google SSO callback GET: user provisioning failed: %s", err)
            return _redirect_error("Échec de persistance de l'utilisateur")

        ticket = _create_one_time_ticket(
            access_token=access_token,
            refresh_token=refresh_token,
            user_resp=UserResponse.from_user(user),
            code_challenge=state_data.get("cc"),
        )
        logger.info("Google SSO callback GET: success for user id=%s (is_mobile=%s)", user.id, is_mobile)
        if is_mobile:
            return RedirectResponse(
                f"healthkicks://auth/callback?ticket={ticket}&state={state}"
            )
        return RedirectResponse(
            f"{frontend_base}/auth/google/callback?ticket={ticket}&state={state}"
        )

    @router.post("/google/callback", response_model=TokenResponse)
    def google_callback(payload: GoogleCallbackRequest, db: Session = Depends(get_db)) -> TokenResponse:
        state_data = verify_oauth_state(payload.state, expected_provider="google")
        _verify_pkce_challenge(state_data.get("cc"), payload.code_verifier)
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
            "azure", platform=target_platform, code_challenge=code_challenge
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
        """Browser redirect handler for Azure AD / Microsoft Entra ID.

        Extracts the authorization code and state parameter, validates CSRF & expiration,
        exchanges code for user profile, and redirects with a short-lived one-time ticket to:
        - `healthkicks://auth/callback` if target platform is mobile
        - `{frontend_url}/auth/azure/callback` (or `/login?error=`) for web clients
        """
        raw_platform = get_state_platform(state)
        is_mobile = (raw_platform == "mobile")
        frontend_base = _get_frontend_base()

        def _redirect_error(message: str) -> RedirectResponse:
            encoded_msg = quote(message, safe="")
            if is_mobile:
                return RedirectResponse(f"healthkicks://auth/callback?error={encoded_msg}")
            return RedirectResponse(f"{frontend_base}/login?error={encoded_msg}")

        if error:
            err_detail = error_description or error
            logger.warning("Azure SSO callback GET received error: %s", err_detail)
            return _redirect_error(err_detail)

        if not code or not state:
            return _redirect_error("Paramètres d'autorisation manquants (code ou state absent)")

        try:
            state_data = verify_oauth_state(state, expected_provider="azure")
            is_mobile = (state_data.get("platform") == "mobile")
        except HTTPException as exc:
            return _redirect_error(exc.detail or "Paramètre state invalide ou expiré")

        try:
            user_info = azure_auth_service.exchange_code_for_azure_user(code, is_mobile=is_mobile)
        except azure_auth_service.AzureAuthError as err:
            logger.error("Azure SSO callback GET: token exchange failed: %s", err)
            return _redirect_error(str(err))
        except Exception as err:
            logger.exception("Azure SSO callback GET: unexpected error: %s", err)
            return _redirect_error("Erreur serveur lors de l'authentification Azure")

        try:
            user = azure_auth_service.get_or_create_azure_user(db, user_info)
            access_token = token_service.issue_access_token(user)
            refresh_token = token_service.issue_refresh_token(user)
        except Exception as err:
            logger.exception("Azure SSO callback GET: user provisioning failed: %s", err)
            return _redirect_error("Échec de persistance de l'utilisateur")

        ticket = _create_one_time_ticket(
            access_token=access_token,
            refresh_token=refresh_token,
            user_resp=UserResponse.from_user(user),
            code_challenge=state_data.get("cc"),
        )
        logger.info("Azure SSO callback GET: success for user id=%s (is_mobile=%s)", user.id, is_mobile)
        if is_mobile:
            return RedirectResponse(
                f"healthkicks://auth/callback?ticket={ticket}&state={state}"
            )
        return RedirectResponse(
            f"{frontend_base}/auth/azure/callback?ticket={ticket}&state={state}"
        )

    @router.post("/azure/callback", response_model=TokenResponse)
    def azure_callback(payload: AzureCallbackRequest, db: Session = Depends(get_db)) -> TokenResponse:
        state_data = verify_oauth_state(payload.state, expected_provider="azure")
        _verify_pkce_challenge(state_data.get("cc"), payload.code_verifier)
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

    @router.post("/token/exchange", response_model=TokenResponse)
    def exchange_auth_ticket(payload: OAuthTicketExchangeRequest) -> TokenResponse:
        """Atomically exchange a one-time OAuth ticket for access and refresh tokens."""
        entry = _ONE_TIME_TICKETS.pop(payload.ticket, None)
        if entry is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or already consumed authentication ticket",
            )
        if time.time() - entry["created_at"] > TICKET_TTL_SECONDS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Authentication ticket expired",
            )
        _verify_pkce_challenge(entry.get("code_challenge"), payload.code_verifier)
        return TokenResponse(
            access_token=entry["access_token"],
            refresh_token=entry["refresh_token"],
            token_type="bearer",
            user=entry["user"],
        )

    @router.post("/refresh", response_model=TokenResponse)
    def refresh(payload: RefreshTokenRequest, db: Session = Depends(get_db)) -> TokenResponse:
        user_id = token_service.verify_refresh_token(payload.refresh_token)
        user = db.query(User).filter_by(id=user_id).one_or_none()
        if user is None or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unknown or inactive user",
            )

        new_access_token = token_service.issue_access_token(user)
        new_refresh_token = token_service.issue_refresh_token(user)
        logger.info("Session refreshed for user id=%s", user.id)
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
        """Exchange authenticated user session for temporary AWS STS IoT credentials.

        Allows clients (mobile app, web dashboard) to connect directly to AWS IoT Core
        via WebSockets SigV4, dynamically scoped to the user's bound devices.
        """
        requested_device_id = None
        if payload and payload.device_id:
            requested_device_id = payload.device_id.strip()
        elif device_id:
            requested_device_id = device_id.strip()

        # Query user's bound devices from database
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
            user_id=user.id,
            device_ids=target_device_ids,
        )
        return IoTCredentialsResponse(**credentials_data)

    return router
