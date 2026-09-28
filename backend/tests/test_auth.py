"""Unit and integration tests for Cognito auth verification and RBAC roles."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from jose import jwt

from backend.auth.cognito_client import (
    _normalize_role,
    create_user,
    verify_cognito_token,
)
from backend.auth.roles import check_permission, get_current_user
from backend.main import app
from backend.models.app import Role

TEST_SECRET = "super-secret-test-key-for-jwt"


# ── Role Hierarchy Unit Tests ─────────────────────────────────────────────


class TestRoleHierarchy:
    """Test check_permission logic for viewer, editor, and owner roles."""

    def test_owner_permissions(self):
        assert check_permission(Role.OWNER, Role.VIEWER) is True
        assert check_permission(Role.OWNER, Role.EDITOR) is True
        assert check_permission(Role.OWNER, Role.OWNER) is True

    def test_editor_permissions(self):
        assert check_permission(Role.EDITOR, Role.VIEWER) is True
        assert check_permission(Role.EDITOR, Role.EDITOR) is True
        assert check_permission(Role.EDITOR, Role.OWNER) is False

    def test_viewer_permissions(self):
        assert check_permission(Role.VIEWER, Role.VIEWER) is True
        assert check_permission(Role.VIEWER, Role.EDITOR) is False
        assert check_permission(Role.VIEWER, Role.OWNER) is False

    def test_invalid_role_permissions(self):
        assert check_permission("invalid", Role.VIEWER) is False
        assert check_permission(Role.VIEWER, "invalid") is False


# ── JWT Verification Unit Tests ──────────────────────────────────────────


class TestCognitoVerification:
    """Test verify_cognito_token claims extraction and role normalization."""

    def test_normalize_role_priority(self):
        assert _normalize_role(["Viewer"]) == "viewer"
        assert _normalize_role(["Editor", "Viewer"]) == "editor"
        assert _normalize_role(["Editor", "Owner"]) == "owner"
        assert _normalize_role([]) == "viewer"

    def test_verify_hs256_test_token_editor(self):
        payload = {
            "sub": "usr-123",
            "email": "editor@bharatbuilds.dev",
            "cognito:groups": ["Editor"],
        }
        token = jwt.encode(payload, TEST_SECRET, algorithm="HS256")
        user = verify_cognito_token(token, test_secret=TEST_SECRET)

        assert user["user_id"] == "usr-123"
        assert user["email"] == "editor@bharatbuilds.dev"
        assert user["role"] == "editor"
        assert "Editor" in user["groups"]

    def test_verify_hs256_test_token_viewer(self):
        payload = {
            "sub": "usr-456",
            "email": "viewer@bharatbuilds.dev",
            "cognito:groups": ["Viewer"],
        }
        token = jwt.encode(payload, TEST_SECRET, algorithm="HS256")
        user = verify_cognito_token(token, test_secret=TEST_SECRET)

        assert user["user_id"] == "usr-456"
        assert user["role"] == "viewer"

    def test_verify_unverified_mode(self):
        payload = {
            "sub": "usr-789",
            "email": "admin@bharatbuilds.dev",
            "cognito:groups": ["Owner"],
        }
        token = jwt.encode(payload, TEST_SECRET, algorithm="HS256")
        user = verify_cognito_token(token, verify_signature=False)

        assert user["user_id"] == "usr-789"
        assert user["role"] == "owner"


# ── FastAPI Endpoint RBAC Tests ───────────────────────────────────────────


@pytest.fixture()
def client():
    """FastAPI TestClient."""
    return TestClient(app)


class TestRouteRBAC:
    """Test role-based access control on /deploy and /revert endpoints."""

    def test_deploy_unauthenticated_returns_401(self, client):
        resp = client.post("/deploy/test-app-1", json={"prompt": "build app", "owner_id": "u1"})
        assert resp.status_code == 401
        assert "Missing Bearer" in resp.json().get("detail", "")

    @patch("backend.auth.roles.verify_cognito_token")
    def test_deploy_viewer_returns_403(self, mock_verify, client):
        mock_verify.return_value = {
            "user_id": "usr-view",
            "email": "viewer@test.com",
            "role": "viewer",
            "groups": ["Viewer"],
        }
        resp = client.post(
            "/deploy/test-app-1",
            headers={"Authorization": "Bearer fake-viewer-token"},
            json={"prompt": "build app", "owner_id": "u1"},
        )
        assert resp.status_code == 403
        assert "requires 'editor' role" in resp.json().get("detail", "")

    @patch("backend.api.routes_deploy.put_item")
    @patch("backend.api.routes_deploy.log_steps", new_callable=AsyncMock)
    @patch("backend.api.routes_deploy.deploy_to_lambda", new_callable=AsyncMock)
    @patch("backend.api.routes_deploy.plan_and_execute", new_callable=AsyncMock)
    @patch("backend.api.routes_deploy.get_settings")
    @patch("backend.auth.roles.verify_cognito_token")
    def test_deploy_editor_returns_200(
        self,
        mock_verify,
        mock_settings,
        mock_plan,
        mock_deploy,
        mock_log,
        mock_put,
        client,
    ):
        mock_verify.return_value = {
            "user_id": "usr-edit",
            "email": "editor@test.com",
            "role": "editor",
            "groups": ["Editor"],
        }
        mock_settings.return_value = MagicMock(
            gemini_api_key="mock",
            gemini_model_id="gemini-2.5-pro",
            aws_region="ap-south-1",
            deploy_lambda_function_name="fn-deploy",
            dynamodb_table_name="tbl-deploy",
        )
        mock_plan.return_value = ("def render(): return 'ok'", [])
        mock_deploy.return_value = "https://example.lambda-url.ap-south-1.on.aws/"

        resp = client.post(
            "/deploy/test-app-1",
            headers={"Authorization": "Bearer fake-editor-token"},
            json={"prompt": "build app", "owner_id": "usr-edit"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["app_id"] == "test-app-1"
        assert data["status"] in ("building", "deployed")

    @patch("backend.auth.roles.verify_cognito_token")
    def test_revert_viewer_returns_403(self, mock_verify, client):
        mock_verify.return_value = {
            "user_id": "usr-view",
            "email": "viewer@test.com",
            "role": "viewer",
            "groups": ["Viewer"],
        }
        resp = client.post(
            "/apps/test-app-1/revert/step-123",
            headers={"Authorization": "Bearer fake-viewer-token"},
        )
        assert resp.status_code == 403
        assert "requires 'editor' role" in resp.json().get("detail", "")
