"""Tests for POST /apps/{app_id}/maintenance endpoint."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure mocks for boto3, jose, and structlog if missing in test environment
if "boto3" not in sys.modules:
    mock_boto3 = MagicMock()
    mock_boto3.__path__ = []
    sys.modules["boto3"] = mock_boto3
    sys.modules["boto3.dynamodb"] = MagicMock()
    sys.modules["boto3.dynamodb.conditions"] = MagicMock()

if "botocore" not in sys.modules:
    sys.modules["botocore"] = MagicMock()
if "botocore.exceptions" not in sys.modules:
    mock_exceptions = MagicMock()
    mock_exceptions.ClientError = type("ClientError", (Exception,), {})
    sys.modules["botocore.exceptions"] = mock_exceptions

if "jose" not in sys.modules:
    sys.modules["jose"] = MagicMock()
    sys.modules["jose.jwt"] = MagicMock()

if "structlog" not in sys.modules:
    mock_structlog = MagicMock()
    mock_structlog.contextvars = MagicMock()
    mock_structlog.stdlib = MagicMock()
    mock_structlog.processors = MagicMock()
    mock_structlog.dev = MagicMock()
    sys.modules["structlog"] = mock_structlog
    sys.modules["structlog.contextvars"] = mock_structlog.contextvars
    sys.modules["structlog.stdlib"] = mock_structlog.stdlib
    sys.modules["structlog.processors"] = mock_structlog.processors
    sys.modules["structlog.dev"] = mock_structlog.dev

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from backend.auth.roles import require_editor
from backend.main import app
from backend.models.app import (
    CandidateVerificationResult,
    MaintenanceIssue,
    MaintenanceJob,
    MaintenanceResult,
    MaintenanceStatus,
    RepairResult,
    StepStatus,
    StepType,
    TimelineStep,
)

client = TestClient(app)

SAMPLE_APP_ID = "app-maintenance-123"
SAMPLE_ISSUE_PAYLOAD = {
    "issue_type": "5xx_error",
    "severity": "high",
    "error_message": "ZeroDivisionError in render()",
    "endpoint": "/render",
    "detection_source": "synthetic_probe",
}


def _mock_settings():
    return MagicMock(
        dynamodb_table_name="test-table",
        aws_region="ap-south-1",
        bedrock_model_id="test-model",
    )


@pytest.fixture
def mock_editor_user():
    """Override require_editor dependency with an authenticated editor user."""
    app.dependency_overrides[require_editor] = lambda: {"sub": "editor-user-1", "role": "editor"}
    yield
    app.dependency_overrides.pop(require_editor, None)


class TestMaintenanceAPI:
    """API endpoint tests for POST /apps/{app_id}/maintenance."""

    @patch("backend.api.routes_maintenance.get_settings")
    @patch("backend.api.routes_maintenance.get_timeline", new_callable=AsyncMock)
    @patch("backend.api.routes_maintenance.MaintenanceOrchestrator")
    def test_successful_maintenance(self, mock_orchestrator_cls, mock_get_timeline, mock_get_settings, mock_editor_user):
        mock_get_settings.return_value = _mock_settings()

        # Mock timeline returning existing code snapshot
        mock_get_timeline.return_value = [
            TimelineStep(
                app_id=SAMPLE_APP_ID,
                step_type=StepType.CODEGEN,
                code_snapshot="def render(): return 1 / 0",
            )
        ]

        # Mock orchestrator execution result
        mock_orch_instance = MagicMock()
        mock_issue = MaintenanceIssue(**SAMPLE_ISSUE_PAYLOAD)
        mock_job = MaintenanceJob(app_id=SAMPLE_APP_ID, issue=mock_issue, status=MaintenanceStatus.PROMOTED)

        mock_orch_instance.run_maintenance = AsyncMock(
            return_value=MaintenanceResult(
                job=mock_job,
                status=MaintenanceStatus.PROMOTED,
                diagnosis="Division by zero in render",
                summary="Replaced with valid HTML",
                candidate_code="def render(): return '<h1>Fixed</h1>'",
                repair_result=RepairResult(
                    diagnosis="Division by zero in render",
                    summary="Replaced with valid HTML",
                    patched_code="def render(): return '<h1>Fixed</h1>'",
                    is_success=True,
                ),
                verification_result=CandidateVerificationResult(
                    passed=True,
                    checks_passed=["syntax_compilation", "entry_point_detection", "runtime_execution"],
                ),
                timeline_steps=[
                    TimelineStep(app_id=SAMPLE_APP_ID, step_type=StepType.MAINTENANCE_DETECT),
                    TimelineStep(app_id=SAMPLE_APP_ID, step_type=StepType.MAINTENANCE_PROMOTE),
                ],
            )
        )
        mock_orchestrator_cls.return_value = mock_orch_instance

        response = client.post(
            f"/apps/{SAMPLE_APP_ID}/maintenance",
            json=SAMPLE_ISSUE_PAYLOAD,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "promoted"
        assert data["diagnosis"] == "Division by zero in render"
        assert data["candidate_code"] == "def render(): return '<h1>Fixed</h1>'"
        assert data["verification_result"]["passed"] is True
        assert len(data["timeline_steps"]) == 2

    def test_unauthorized_request_missing_token(self):
        # Ensure no overrides
        app.dependency_overrides.pop(require_editor, None)

        response = client.post(
            f"/apps/{SAMPLE_APP_ID}/maintenance",
            json=SAMPLE_ISSUE_PAYLOAD,
        )
        # Should return 401 Unauthorized because require_editor requires Bearer token
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_invalid_issue_payload(self, mock_editor_user):
        # Empty issue_type and empty error_message
        invalid_payload = {
            "issue_type": "",
            "error_message": "",
        }

        response = client.post(
            f"/apps/{SAMPLE_APP_ID}/maintenance",
            json=invalid_payload,
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @patch("backend.api.routes_maintenance.get_settings")
    @patch("backend.api.routes_maintenance.get_timeline", new_callable=AsyncMock)
    def test_missing_app_or_code_snapshot_returns_404(self, mock_get_timeline, mock_get_settings, mock_editor_user):
        mock_get_settings.return_value = _mock_settings()
        # No timeline steps / no code snapshots found
        mock_get_timeline.return_value = []

        response = client.post(
            f"/apps/{SAMPLE_APP_ID}/maintenance",
            json=SAMPLE_ISSUE_PAYLOAD,
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert "No existing code snapshot found" in response.json()["message"]
