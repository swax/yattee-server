"""Tests for CORS configuration security."""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config
import database
import settings as settings_module
from basic_auth import BasicAuthMiddleware
from browser_access import configure_cors
from settings import Settings


@pytest.fixture(autouse=True)
def reset_cors_policy(monkeypatch):
    """Keep cases isolated from the process and developer's startup environment."""
    monkeypatch.setattr(config, "CORS_ORIGINS", "")
    monkeypatch.setattr(config, "CORS_ORIGIN_REGEX", None)
    monkeypatch.setattr(config, "CORS_ALLOW_ALL", False)
    monkeypatch.setattr(config, "CORS_ALLOW_CREDENTIALS", True)
    monkeypatch.setattr(settings_module, "get_settings", lambda: Settings())


class TestCorsConfiguration:
    """Tests for CORS middleware configuration."""

    def test_cors_disabled_by_default(self, monkeypatch):
        """Test that CORS is disabled when no configuration is provided."""
        app = FastAPI()
        configure_cors(app)

        @app.get("/test")
        def test_endpoint():
            return {"status": "ok"}

        client = TestClient(app)
        response = client.get("/test", headers={"Origin": "https://evil.example.com"})

        # No CORS headers should be present
        assert "access-control-allow-origin" not in response.headers

    def test_cors_with_specific_origins(self, monkeypatch):
        """Test CORS with specific allowed origins."""
        monkeypatch.setattr(config, "CORS_ORIGINS", "https://app.example.com,https://admin.example.com")
        monkeypatch.setattr(config, "CORS_ALLOW_ALL", False)
        monkeypatch.setattr(config, "CORS_ALLOW_CREDENTIALS", True)

        app = FastAPI()
        configure_cors(app)

        @app.get("/test")
        def test_endpoint():
            return {"status": "ok"}

        client = TestClient(app)

        # Allowed origin should work
        response = client.get("/test", headers={"Origin": "https://app.example.com"})
        assert response.headers.get("access-control-allow-origin") == "https://app.example.com"
        assert response.headers.get("access-control-allow-credentials") == "true"

        # Disallowed origin should be blocked
        response = client.get("/test", headers={"Origin": "https://evil.example.com"})
        assert response.headers.get("access-control-allow-origin") != "https://evil.example.com"

    def test_cors_origin_regex_allows_dynamic_loopback_ports(self, monkeypatch):
        """An environment regex remains available for advanced deployments."""
        monkeypatch.setattr(
            config,
            "CORS_ORIGIN_REGEX",
            r"^http://(?:localhost|127\.0\.0\.1)(?::[0-9]+)?$",
        )
        monkeypatch.setattr(config, "CORS_ALLOW_ALL", False)
        monkeypatch.setattr(config, "CORS_ALLOW_CREDENTIALS", True)

        app = FastAPI()
        configure_cors(app)

        @app.get("/test")
        def test_endpoint():
            return {"status": "ok"}

        client = TestClient(app)
        for origin in ("http://localhost:5181", "http://127.0.0.1:6199"):
            response = client.get("/test", headers={"Origin": origin})
            assert response.headers.get("access-control-allow-origin") == origin
            assert response.headers.get("access-control-allow-credentials") == "true"

        response = client.get(
            "/test",
            headers={"Origin": "http://localhost.evil.example:5181"},
        )
        assert "access-control-allow-origin" not in response.headers

    def test_runtime_origins_apply_without_restarting(self, monkeypatch):
        """Saving settings changes the cached policy used by subsequent requests."""
        current = Settings(cors_allowed_origins=["https://first.example"])
        monkeypatch.setattr(settings_module, "get_settings", lambda: current)

        app = FastAPI()
        configure_cors(app)

        @app.get("/test")
        def test_endpoint():
            return {"status": "ok"}

        client = TestClient(app)
        first = client.get("/test", headers={"Origin": "https://first.example"})
        assert first.headers.get("access-control-allow-origin") == "https://first.example"

        current.cors_allowed_origins = ["https://second.example"]
        old = client.get("/test", headers={"Origin": "https://first.example"})
        new = client.get("/test", headers={"Origin": "https://second.example"})
        assert "access-control-allow-origin" not in old.headers
        assert new.headers.get("access-control-allow-origin") == "https://second.example"

    def test_runtime_localhost_toggle_allows_only_loopback(self, monkeypatch):
        """The convenience toggle covers changing development ports without trusting lookalike hosts."""
        monkeypatch.setattr(settings_module, "get_settings", lambda: Settings(cors_allow_localhost=True))

        app = FastAPI()
        configure_cors(app)

        @app.get("/test")
        def test_endpoint():
            return {"status": "ok"}

        client = TestClient(app)
        for origin in ("http://localhost:5181", "http://127.0.0.1:6199", "http://[::1]:4173"):
            response = client.get("/test", headers={"Origin": origin})
            assert response.headers.get("access-control-allow-origin") == origin

        response = client.get("/test", headers={"Origin": "http://localhost.evil.example:5181"})
        assert "access-control-allow-origin" not in response.headers

    def test_cors_allow_all_disables_credentials(self, monkeypatch):
        """Test that CORS_ALLOW_ALL mode disables credentials for security."""
        monkeypatch.setattr(config, "CORS_ORIGINS", "")
        monkeypatch.setattr(config, "CORS_ALLOW_ALL", True)
        monkeypatch.setattr(config, "CORS_ALLOW_CREDENTIALS", True)  # Should be ignored

        app = FastAPI()
        configure_cors(app)

        @app.get("/test")
        def test_endpoint():
            return {"status": "ok"}

        client = TestClient(app)
        response = client.get("/test", headers={"Origin": "https://any-origin.example.com"})

        # Origin should be allowed
        assert response.headers.get("access-control-allow-origin") == "*"
        # Credentials should NOT be allowed (security requirement)
        assert response.headers.get("access-control-allow-credentials") != "true"

    def test_cors_origins_takes_precedence_over_allow_all(self, monkeypatch):
        """Test that CORS_ORIGINS takes precedence when both are set."""
        monkeypatch.setattr(config, "CORS_ORIGINS", "https://app.example.com")
        monkeypatch.setattr(config, "CORS_ALLOW_ALL", True)
        monkeypatch.setattr(config, "CORS_ALLOW_CREDENTIALS", True)

        app = FastAPI()
        configure_cors(app)

        @app.get("/test")
        def test_endpoint():
            return {"status": "ok"}

        client = TestClient(app)

        # Only the specific origin should work, not wildcard
        response = client.get("/test", headers={"Origin": "https://app.example.com"})
        assert response.headers.get("access-control-allow-origin") == "https://app.example.com"
        assert response.headers.get("access-control-allow-credentials") == "true"

    def test_cors_preflight_request(self, monkeypatch):
        """Test CORS preflight (OPTIONS) request handling."""
        monkeypatch.setattr(config, "CORS_ORIGINS", "https://app.example.com")
        monkeypatch.setattr(config, "CORS_ALLOW_ALL", False)
        monkeypatch.setattr(config, "CORS_ALLOW_CREDENTIALS", True)

        app = FastAPI()
        configure_cors(app)

        @app.post("/api/data")
        def post_endpoint():
            return {"status": "created"}

        client = TestClient(app)

        # Preflight request
        response = client.options(
            "/api/data",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type, Authorization",
            },
        )

        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "https://app.example.com"
        assert "POST" in response.headers.get("access-control-allow-methods", "")
        assert response.headers.get("access-control-allow-credentials") == "true"

    def test_authenticated_server_allows_cors_preflight(self, monkeypatch):
        """Basic auth protects the real request without rejecting its browser preflight."""
        monkeypatch.setattr(
            settings_module,
            "get_settings",
            lambda: Settings(cors_allowed_origins=["http://localhost:5179"]),
        )
        monkeypatch.setattr(database, "has_any_user", lambda: True)

        app = FastAPI()
        configure_cors(app)
        app.add_middleware(BasicAuthMiddleware)
        client = TestClient(app)
        response = client.options(
            "/api/data",
            headers={
                "Origin": "http://localhost:5179",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization",
            },
        )

        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "http://localhost:5179"
        assert response.headers.get("access-control-allow-credentials") == "true"

    def test_cors_wraps_basic_auth_challenge(self, monkeypatch):
        """A browser must be able to read a 401 challenge before submitting credentials."""
        monkeypatch.setattr(
            settings_module,
            "get_settings",
            lambda: Settings(cors_allowed_origins=["http://localhost:5179"]),
        )
        monkeypatch.setattr(database, "has_any_user", lambda: True)

        app = FastAPI()
        app.add_middleware(BasicAuthMiddleware)
        configure_cors(app)
        response = TestClient(app).get("/api/data", headers={"Origin": "http://localhost:5179"})

        assert response.status_code == 401
        assert response.headers.get("access-control-allow-origin") == "http://localhost:5179"
        assert response.headers.get("access-control-allow-credentials") == "true"

    def test_cors_credentials_disabled(self, monkeypatch):
        """Test CORS with credentials explicitly disabled."""
        monkeypatch.setattr(config, "CORS_ORIGINS", "https://app.example.com")
        monkeypatch.setattr(config, "CORS_ALLOW_ALL", False)
        monkeypatch.setattr(config, "CORS_ALLOW_CREDENTIALS", False)

        app = FastAPI()
        configure_cors(app)

        @app.get("/test")
        def test_endpoint():
            return {"status": "ok"}

        client = TestClient(app)
        response = client.get("/test", headers={"Origin": "https://app.example.com"})

        assert response.headers.get("access-control-allow-origin") == "https://app.example.com"
        # Credentials header should not be present or should be false
        assert response.headers.get("access-control-allow-credentials") != "true"
