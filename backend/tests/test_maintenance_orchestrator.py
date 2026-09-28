"""Tests for the Maintenance Orchestrator."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure boto3 and submodule mocks exist in test environments if missing
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

import pytest

from backend.agent.maintenance_orchestrator import MaintenanceOrchestrator
from backend.agent.repair import CodeRepairProvider
from backend.models.app import (
    CandidateVerificationResult,
    MaintenanceIssue,
    MaintenanceStatus,
    RepairResult,
    StepStatus,
    StepType,
    TimelineStep,
)

SAMPLE_APP_ID = "test-app-123"
SAMPLE_BROKEN_CODE = "def render(): return 1 / 0"
SAMPLE_VALID_PATCH = "def render(): return '<h1>Fixed App</h1>'"
SAMPLE_INVALID_PATCH = "def broken( return 1"


@pytest.fixture
def sample_issue() -> MaintenanceIssue:
    return MaintenanceIssue(
        issue_type="5xx_error",
        severity="high",
        error_message="ZeroDivisionError in render()",
        endpoint="/",
        detection_source="synthetic_probe",
    )


class TestMaintenanceOrchestrator:
    """MaintenanceOrchestrator unit test suite."""

    @pytest.mark.asyncio
    @patch("backend.agent.maintenance_orchestrator.log_step", new_callable=AsyncMock)
    async def test_successful_maintenance_first_attempt(self, mock_log_step, sample_issue):
        mock_provider = MagicMock(spec=CodeRepairProvider)
        mock_provider.diagnose_and_repair = AsyncMock(
            return_value=RepairResult(
                diagnosis="Division by zero in render",
                summary="Replaced division with valid HTML string",
                patched_code=SAMPLE_VALID_PATCH,
                is_success=True,
            )
        )

        orchestrator = MaintenanceOrchestrator(repair_provider=mock_provider)
        result = await orchestrator.run_maintenance(
            SAMPLE_APP_ID,
            sample_issue,
            existing_code=SAMPLE_BROKEN_CODE,
            persist_timeline=True,
        )

        assert result.status == MaintenanceStatus.PROMOTED
        assert result.job.attempt_count == 1
        assert result.candidate_code == SAMPLE_VALID_PATCH
        assert result.verification_result.passed is True
        assert len(result.timeline_steps) == 5  # DETECT, DIAGNOSE, PATCH, VERIFY, PROMOTE

        step_types = [s.step_type for s in result.timeline_steps]
        assert step_types == [
            StepType.MAINTENANCE_DETECT,
            StepType.MAINTENANCE_DIAGNOSE,
            StepType.MAINTENANCE_PATCH,
            StepType.MAINTENANCE_VERIFY,
            StepType.MAINTENANCE_PROMOTE,
        ]
        assert mock_log_step.call_count == 5

    @pytest.mark.asyncio
    @patch("backend.agent.maintenance_orchestrator.log_step", new_callable=AsyncMock)
    async def test_successful_maintenance_on_second_attempt(self, mock_log_step, sample_issue):
        mock_provider = MagicMock(spec=CodeRepairProvider)
        # Attempt 1 returns broken syntax, Attempt 2 returns valid code
        mock_provider.diagnose_and_repair = AsyncMock(
            side_effect=[
                RepairResult(
                    diagnosis="Attempt 1 bug diagnosis",
                    summary="Syntax error patch",
                    patched_code=SAMPLE_INVALID_PATCH,
                    is_success=True,
                ),
                RepairResult(
                    diagnosis="Attempt 2 refined diagnosis",
                    summary="Corrected syntax patch",
                    patched_code=SAMPLE_VALID_PATCH,
                    is_success=True,
                ),
            ]
        )

        orchestrator = MaintenanceOrchestrator(repair_provider=mock_provider, max_attempts=2)
        result = await orchestrator.run_maintenance(
            SAMPLE_APP_ID,
            sample_issue,
            existing_code=SAMPLE_BROKEN_CODE,
            persist_timeline=True,
        )

        assert result.status == MaintenanceStatus.PROMOTED
        assert result.job.attempt_count == 2
        assert mock_provider.diagnose_and_repair.call_count == 2
        assert result.verification_result.passed is True

    @pytest.mark.asyncio
    @patch("backend.agent.maintenance_orchestrator.log_step", new_callable=AsyncMock)
    async def test_rejection_after_max_attempts(self, mock_log_step, sample_issue):
        mock_provider = MagicMock(spec=CodeRepairProvider)
        # Both attempts return code that fails candidate verification
        mock_provider.diagnose_and_repair = AsyncMock(
            return_value=RepairResult(
                diagnosis="Repeatedly produces invalid code",
                summary="Broken syntax",
                patched_code=SAMPLE_INVALID_PATCH,
                is_success=True,
            )
        )

        orchestrator = MaintenanceOrchestrator(repair_provider=mock_provider, max_attempts=2)
        result = await orchestrator.run_maintenance(
            SAMPLE_APP_ID,
            sample_issue,
            existing_code=SAMPLE_BROKEN_CODE,
            persist_timeline=True,
        )

        assert result.status == MaintenanceStatus.REJECTED
        assert result.job.attempt_count == 2
        assert mock_provider.diagnose_and_repair.call_count == 2
        assert result.verification_result.passed is False

        last_step = result.timeline_steps[-1]
        assert last_step.step_type == StepType.MAINTENANCE_REJECT
        assert "production remains untouched" in last_step.reasoning

    @pytest.mark.asyncio
    @patch("backend.agent.maintenance_orchestrator.log_step", new_callable=AsyncMock)
    async def test_repair_provider_failure(self, mock_log_step, sample_issue):
        mock_provider = MagicMock(spec=CodeRepairProvider)
        mock_provider.diagnose_and_repair = AsyncMock(
            return_value=RepairResult(
                diagnosis="",
                summary="",
                patched_code="",
                is_success=False,
                error_message="Bedrock quota exceeded",
            )
        )

        orchestrator = MaintenanceOrchestrator(repair_provider=mock_provider)
        result = await orchestrator.run_maintenance(
            SAMPLE_APP_ID,
            sample_issue,
            existing_code=SAMPLE_BROKEN_CODE,
            persist_timeline=True,
        )

        assert result.status == MaintenanceStatus.FAILED
        assert "Bedrock quota exceeded" in result.summary

        last_step = result.timeline_steps[-1]
        assert last_step.step_type == StepType.MAINTENANCE_REJECT
        assert last_step.status == StepStatus.ERROR

    @pytest.mark.asyncio
    @patch("backend.agent.maintenance_orchestrator.get_timeline", new_callable=AsyncMock)
    @patch("backend.agent.maintenance_orchestrator.log_step", new_callable=AsyncMock)
    async def test_no_existing_code_found(self, mock_log_step, mock_get_timeline, sample_issue):
        mock_get_timeline.return_value = []  # No steps in timeline

        orchestrator = MaintenanceOrchestrator()
        result = await orchestrator.run_maintenance(
            SAMPLE_APP_ID,
            sample_issue,
            existing_code=None,
            persist_timeline=True,
        )

        assert result.status == MaintenanceStatus.FAILED
        assert "No existing code snapshot found" in result.summary
        assert len(result.timeline_steps) == 1
        assert result.timeline_steps[0].step_type == StepType.MAINTENANCE_REJECT

    @pytest.mark.asyncio
    @patch("backend.agent.maintenance_orchestrator.log_step", new_callable=AsyncMock)
    async def test_persist_timeline_false_does_not_call_log_step(self, mock_log_step, sample_issue):
        mock_provider = MagicMock(spec=CodeRepairProvider)
        mock_provider.diagnose_and_repair = AsyncMock(
            return_value=RepairResult(
                diagnosis="OK",
                summary="Fixed",
                patched_code=SAMPLE_VALID_PATCH,
                is_success=True,
            )
        )

        orchestrator = MaintenanceOrchestrator(repair_provider=mock_provider)
        result = await orchestrator.run_maintenance(
            SAMPLE_APP_ID,
            sample_issue,
            existing_code=SAMPLE_BROKEN_CODE,
            persist_timeline=False,
        )

        assert result.status == MaintenanceStatus.PROMOTED
        assert mock_log_step.call_count == 0
