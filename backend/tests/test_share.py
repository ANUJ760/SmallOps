"""Unit and integration tests for app sharing and collaborator invites via SES."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from backend.api.routes_share import _get_ses_client, send_invite_email
from backend.main import app
from backend.models.app import Role

TEST_REGION = "ap-south-1"
SENDER_EMAIL = "verified-sender@bharatbuilds.dev"
RECIPIENT_EMAIL = "collab@example.com"
TEST_APP_ID = "app-share-12345678"


# ── SES Helper Unit Tests ─────────────────────────────────────────────────


class TestSendInviteEmail:
    """Test send_invite_email helper with mocked SES."""

    def test_send_email_params_with_moto(self):
        with mock_aws():
            ses = boto3.client("ses", region_name=TEST_REGION)
            ses.verify_email_identity(EmailAddress=SENDER_EMAIL)
            ses.verify_email_identity(EmailAddress=RECIPIENT_EMAIL)

            resp = send_invite_email(
                to_email=RECIPIENT_EMAIL,
                app_id=TEST_APP_ID,
                role="editor",
                sender_email=SENDER_EMAIL,
                region=TEST_REGION,
                app_url="https://live.bharatbuilds.dev/app/123",
            )

            assert "MessageId" in resp
            assert resp["ResponseMetadata"]["HTTPStatusCode"] == 200

    @patch("backend.api.routes_share._get_ses_client")
    def test_send_email_payload_structure(self, mock_client_factory):
        mock_client = MagicMock()
        mock_client_factory.return_value = mock_client
        mock_client.send_email.return_value = {"MessageId": "msg-9999"}

        resp = send_invite_email(
            to_email=RECIPIENT_EMAIL,
            app_id=TEST_APP_ID,
            role="viewer",
            sender_email=SENDER_EMAIL,
            region=TEST_REGION,
        )

        assert resp["MessageId"] == "msg-9999"
        mock_client.send_email.assert_called_once()
        kwargs = mock_client.send_email.call_args[1]

        assert kwargs["Source"] == SENDER_EMAIL
        assert kwargs["Destination"]["ToAddresses"] == [RECIPIENT_EMAIL]
        assert "Viewer" in kwargs["Message"]["Subject"]["Data"] or TEST_APP_ID[:8] in kwargs["Message"]["Subject"]["Data"]
        assert "viewer" in kwargs["Message"]["Body"]["Text"]["Data"].lower()
        assert "viewer" in kwargs["Message"]["Body"]["Html"]["Data"].lower()
        assert TEST_APP_ID in kwargs["Message"]["Body"]["Html"]["Data"]


# ── Endpoint RBAC and Dispatch Tests ──────────────────────────────────────


@pytest.fixture()
def client():
    return TestClient(app)


class TestShareEndpoint:
    """Test /apps/{app_id}/invite endpoint RBAC and workflow."""

    def test_invite_unauthenticated_returns_401(self, client):
        resp = client.post(
            f"/apps/{TEST_APP_ID}/invite",
            json={"email": RECIPIENT_EMAIL, "role": "viewer"},
        )
        assert resp.status_code == 401

    @patch("backend.auth.roles.verify_cognito_token")
    def test_invite_viewer_returns_403(self, mock_verify, client):
        mock_verify.return_value = {
            "user_id": "usr-v",
            "email": "viewer@example.com",
            "role": "viewer",
            "groups": ["Viewer"],
        }
        resp = client.post(
            f"/apps/{TEST_APP_ID}/invite",
            headers={"Authorization": "Bearer fake-viewer-token"},
            json={"email": RECIPIENT_EMAIL, "role": "editor"},
        )
        assert resp.status_code == 403
        assert "requires 'editor' role" in resp.json()["detail"]

    @patch("backend.api.routes_share.put_item")
    @patch("backend.api.routes_share.add_user_to_group", new_callable=AsyncMock)
    @patch("backend.api.routes_share.create_user", new_callable=AsyncMock)
    @patch("backend.api.routes_share.send_invite_email")
    @patch("backend.api.routes_share.get_settings")
    @patch("backend.auth.roles.verify_cognito_token")
    def test_invite_editor_success(
        self,
        mock_verify,
        mock_settings,
        mock_send_email,
        mock_create_user,
        mock_add_group,
        mock_put,
        client,
    ):
        mock_verify.return_value = {
            "user_id": "usr-e",
            "email": "editor@example.com",
            "role": "editor",
            "groups": ["Editor"],
        }
        mock_settings.return_value = MagicMock(
            ses_sender_email=SENDER_EMAIL,
            aws_region=TEST_REGION,
            cognito_user_pool_id="ap-south-1_test",
            dynamodb_table_name="tbl-test",
        )
        mock_send_email.return_value = {"MessageId": "ses-msg-12345"}
        mock_create_user.return_value = {"Username": RECIPIENT_EMAIL}

        resp = client.post(
            f"/apps/{TEST_APP_ID}/invite",
            headers={"Authorization": "Bearer fake-editor-token"},
            json={"email": RECIPIENT_EMAIL, "role": "editor"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["app_id"] == TEST_APP_ID
        assert data["email"] == RECIPIENT_EMAIL
        assert data["role"] == "editor"
        assert data["status"] == "sent"
        assert data["message_id"] == "ses-msg-12345"

        mock_create_user.assert_called_once()
        mock_add_group.assert_called_once()
        mock_send_email.assert_called_once()
        mock_put.assert_called_once()
