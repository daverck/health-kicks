from app.main import app
from starlette.testclient import TestClient


def test_security_headers_present_on_response() -> None:
    """Verify essential OWASP security headers are present on all HTTP responses."""
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("x-frame-options") == "DENY"
    assert response.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
    assert response.headers.get("x-xss-protection") == "0"
    assert response.headers.get("content-security-policy") == "default-src 'self'"


def test_hsts_header_conditional() -> None:
    """Verify HSTS is included on HTTPS/forwarded-proto requests and excluded in plain dev HTTP."""
    client = TestClient(app, raise_server_exceptions=False)

    # Plain HTTP dev request -> no HSTS to protect local dev environment
    plain_response = client.get("/")
    assert "strict-transport-security" not in plain_response.headers

    # HTTPS forwarded request -> HSTS must be present
    https_response = client.get("/", headers={"x-forwarded-proto": "https"})
    assert https_response.headers.get("strict-transport-security") == "max-age=31536000; includeSubDomains"


def test_cors_preflight_and_headers() -> None:
    """Verify CORS preflight and allow headers."""
    client = TestClient(app, raise_server_exceptions=False)

    # Preflight from allowed local dev origin (Angular)
    response = client.options(
        "/api/v1/devices",
        headers={
            "Origin": "http://localhost:4200",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "http://localhost:4200"
    assert response.headers.get("access-control-allow-credentials") == "true"
    assert "GET" in response.headers.get("access-control-allow-methods", "")

    # Request from unauthorized origin -> no Access-Control-Allow-Origin
    unauthorized_response = client.get(
        "/",
        headers={"Origin": "https://malicious-site.example"},
    )
    assert "access-control-allow-origin" not in unauthorized_response.headers

