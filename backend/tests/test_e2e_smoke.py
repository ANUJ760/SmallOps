"""End-to-End Smoke Test — BharatBuilds platform lifecycle.

Executes the complete user flow in sequence against mocked AWS services:
1. Clarify pass (POST /apps/clarify)
2. Initial App Deploy with ReAct plan/codegen (POST /deploy/{id})
3. Timeline read & audit (GET /apps/{id}/timeline)
4. Second deploy / live edit (POST /deploy/{id})
5. Backtrack / Revert to initial step (POST /apps/{id}/revert/{step_id})
6. Share with collaborator (POST /apps/{id}/invite)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from backend.main import app
from backend.models.app import StepStatus, StepType

TEST_REGION = "ap-south-1"
TEST_TABLE = "smoke-bharatbuilds-table"
TEST_FUNCTION = "smoke-deploy-function"
TEST_BUCKET = "smoke-assets-bucket"
TEST_SENDER = "noreply@bharatbuilds.dev"

CODE_V1 = """\
def render():
    return "<h1>Feedback Tracker v1</h1>"
"""

CODE_V2 = """\
def render():
    return "<h1>Feedback Tracker v2 — with sentiment</h1>"
"""


@pytest.fixture()
def mocked_aws_env():
    """Spin up mocked DynamoDB and SES environments."""
    with mock_aws():
        # Setup DynamoDB
        ddb = boto3.resource("dynamodb", region_name=TEST_REGION)
        ddb.create_table(
            TableName=TEST_TABLE,
            KeySchema=[
                {"AttributeName": "app_id", "KeyType": "HASH"},
                {"AttributeName": "step_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "app_id", "AttributeType": "S"},
                {"AttributeName": "step_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # Setup SES
        ses = boto3.client("ses", region_name=TEST_REGION)
        ses.verify_email_identity(EmailAddress=TEST_SENDER)

        yield {
            "table": TEST_TABLE,
            "function": TEST_FUNCTION,
            "sender": TEST_SENDER,
        }


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


class TestEndToEndSmokeFlow:
    """End-to-end integration test of the complete platform pipeline."""

    @patch("backend.agent.trace_logger.get_settings")
    @patch("backend.api.routes_timeline.get_settings")
    @patch("backend.api.routes_share.get_settings")
    @patch("backend.api.routes_deploy.get_settings")
    @patch("backend.api.routes_apps.get_settings")
    @patch("backend.auth.roles.verify_cognito_token")
    @patch("backend.agent.planner.generate_code", new_callable=AsyncMock)
    @patch("backend.agent.planner.invoke_model_json")
    @patch("backend.agent.clarify.invoke_model_json")
    @patch("backend.api.routes_timeline.deploy_to_lambda", new_callable=AsyncMock)
    @patch("backend.api.routes_deploy.deploy_to_lambda", new_callable=AsyncMock)
    def test_full_pipeline_clarify_deploy_timeline_edit_revert_share(
        self,
        mock_deploy_lambda,
        mock_revert_lambda,
        mock_clarify_invoke,
        mock_planner_invoke,
        mock_codegen,
        mock_auth_token,
        mock_apps_settings,
        mock_deploy_settings,
        mock_share_settings,
        mock_timeline_settings,
        mock_logger_settings,
        mocked_aws_env,
        client,
    ):
        settings_mock = MagicMock(
            aws_region=TEST_REGION,
            gemini_api_key="mock",
            gemini_model_id="gemini-2.5-pro",
            dynamodb_table_name=TEST_TABLE,
            s3_assets_bucket=TEST_BUCKET,
            cognito_user_pool_id="ap-south-1_smoke",
            cognito_app_client_id="smoke-client-id",
            ses_sender_email=TEST_SENDER,
            deploy_lambda_function_name=TEST_FUNCTION,
        )
        for s_mock in (
            mock_apps_settings,
            mock_deploy_settings,
            mock_share_settings,
            mock_timeline_settings,
            mock_logger_settings,
        ):
            s_mock.return_value = settings_mock

        # Authenticate as Editor
        mock_auth_token.return_value = {
            "user_id": "owner-123",
            "email": "builder@bharatbuilds.dev",
            "role": "editor",
            "groups": ["Editor"],
        }

        app_id = "demo-app-001"
        editor_headers = {"Authorization": "Bearer fake-editor-token"}

        # ── Step 1: Clarify Pass ─────────────────────────────────────────
        mock_clarify_invoke.return_value = {
            "needs_clarification": True,
            "questions": [
                {
                    "question": "Collect feedback anonymously or require email?",
                    "suggested_default": "Anonymous",
                    "why_it_matters": "Determines auth requirement for respondents",
                }
            ],
        }
        clarify_resp = client.post(
            "/apps/clarify",
            json={"prompt": "Build an authenticated customer feedback collector"},
        )
        assert clarify_resp.status_code == 200
        clarify_data = clarify_resp.json()
        assert clarify_data["needs_clarification"] is True
        assert len(clarify_data["questions"]) == 1

        # ── Step 2: Build & Initial Deploy ───────────────────────────────
        mock_planner_invoke.side_effect = [
            # Plan
            {"app_title": "Feedback Tracker", "features": ["Submit", "List"]},
            # Review pass
            {"is_valid": True, "issues": []},
        ]
        mock_codegen.return_value = CODE_V1
        live_url = f"https://{TEST_FUNCTION}.lambda-url.{TEST_REGION}.on.aws/"
        mock_deploy_lambda.return_value = live_url
        mock_revert_lambda.return_value = live_url

        deploy_resp = client.post(
            f"/deploy/{app_id}",
            headers=editor_headers,
            json={
                "prompt": "Build customer feedback collector",
                "owner_id": "owner-123",
                "title": "Feedback Tracker",
                "clarifications": {"auth": "Anonymous"},
            },
        )
        assert deploy_resp.status_code == 200
        deploy_data = deploy_resp.json()
        assert deploy_data["app_id"] == app_id
        assert deploy_data["status"] in ("building", "deployed")

        # ── Step 3: Inspect Decision Timeline ────────────────────────────
        timeline_resp = client.get(f"/apps/{app_id}/timeline")
        assert timeline_resp.status_code == 200
        timeline_data = timeline_resp.json()
        assert timeline_data["app_id"] == app_id
        steps = timeline_data["steps"]
        assert len(steps) >= 3

        # Locate the first codegen step to test reverting to it later
        codegen_v1_step = next((s for s in steps if s["step_type"] == StepType.CODEGEN.value), None)
        assert codegen_v1_step is not None
        assert codegen_v1_step["code_snapshot"] == CODE_V1
        codegen_v1_step_id = codegen_v1_step["step_id"]

        # ── Step 4: Live Edit (Second Deploy) ────────────────────────────
        mock_planner_invoke.side_effect = [
            # Plan edit
            {"app_title": "Feedback Tracker v2", "features": ["Sentiment tags"]},
            # Review pass
            {"is_valid": True, "issues": []},
        ]
        mock_codegen.return_value = CODE_V2

        edit_resp = client.post(
            f"/deploy/{app_id}",
            headers=editor_headers,
            json={
                "prompt": "Add sentiment ratings to feedback entries",
                "owner_id": "owner-123",
                "title": "Feedback Tracker v2",
            },
        )
        assert edit_resp.status_code == 200

        # Verify timeline grew with the new edit steps
        timeline_v2_resp = client.get(f"/apps/{app_id}/timeline")
        assert len(timeline_v2_resp.json()["steps"]) > len(steps)

        # ── Step 5: Backtrack / Revert to V1 ──────────────────────────────
        revert_resp = client.post(
            f"/apps/{app_id}/revert/{codegen_v1_step_id}",
            headers=editor_headers,
        )
        assert revert_resp.status_code == 200
        revert_data = revert_resp.json()
        assert revert_data["status"] == "reverted"
        assert revert_data["reverted_to_step"] == codegen_v1_step_id

        # Verify revert node in timeline
        post_revert_timeline = client.get(f"/apps/{app_id}/timeline").json()["steps"]
        revert_node = post_revert_timeline[-1]
        assert revert_node["step_type"] == StepType.REVERT.value
        assert revert_node["status"] == StepStatus.REVERTED.value
        assert revert_node["parent_step_id"] == codegen_v1_step_id
        assert revert_node["code_snapshot"] == CODE_V1

        # ── Step 6: Zero-Config Share & Invite ───────────────────────────
        with patch("backend.api.routes_share.create_user", new_callable=AsyncMock), \
             patch("backend.api.routes_share.add_user_to_group", new_callable=AsyncMock):
            share_resp = client.post(
                f"/apps/{app_id}/invite",
                headers=editor_headers,
                json={"email": "teammate@bharatbuilds.dev", "role": "viewer"},
            )
            assert share_resp.status_code == 200
            share_data = share_resp.json()
            assert share_data["status"] == "sent"
            assert share_data["email"] == "teammate@bharatbuilds.dev"
            assert share_data["role"] == "viewer"
            assert "message_id" in share_data
