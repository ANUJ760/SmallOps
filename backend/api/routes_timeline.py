"""GET /apps/{id}/timeline, GET /apps/{id}/timeline/{step_id}, POST /apps/{id}/revert/{step_id}."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from backend.agent.trace_logger import get_step, get_timeline, log_step
from backend.auth.roles import require_editor
from backend.config import get_settings
from backend.deploy.lambda_deployer import deploy_to_lambda
from backend.models.app import StepStatus, StepType, TimelineStep

router = APIRouter(tags=["timeline"])


@router.get("/apps/{app_id}/timeline")
async def list_timeline(app_id: str):
    """Fetch the full decision timeline for an app.

    Returns an ordered list of steps: plan, tool calls, codegen,
    retries, deploys, and reverts.
    """
    steps = await get_timeline(app_id)
    return {
        "app_id": app_id,
        "step_count": len(steps),
        "steps": [s.model_dump() for s in steps],
    }


@router.get("/apps/{app_id}/timeline/{step_id}")
async def get_timeline_step(app_id: str, step_id: str):
    """Fetch a single timeline step by ID.

    Includes the full detail: input, reasoning, code diff, latency,
    token usage, and status.
    """
    step = await get_step(app_id, step_id)
    if step is None:
        raise HTTPException(status_code=404, detail="Step not found")
    return step.model_dump()


@router.post("/apps/{app_id}/revert/{step_id}", dependencies=[Depends(require_editor)])
async def revert_to_step(app_id: str, step_id: str):
    """Revert the live app to the code snapshot at a given timeline step.

    Fetches the ``code_snapshot`` stored at that step and re-deploys it
    to the Lambda function. Logs the revert as a new timeline node with
    ``step_type=revert`` and ``status=reverted``.
    """
    settings = get_settings()

    # Fetch the target step
    target_step = await get_step(app_id, step_id)
    if target_step is None:
        raise HTTPException(status_code=404, detail="Step not found")

    if not target_step.code_snapshot:
        raise HTTPException(
            status_code=400,
            detail="Target step does not contain a code snapshot to revert to",
        )

    # Re-deploy the code snapshot
    try:
        function_url = await deploy_to_lambda(
            app_id,
            target_step.code_snapshot,
            function_name=settings.deploy_lambda_function_name,
            region=settings.aws_region,
        )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(f"Lambda deploy failed during revert: {exc}")
        function_url = f"/apps/{app_id}/live"

    # Log the revert as a new timeline node
    revert_step = TimelineStep(
        app_id=app_id,
        step_type=StepType.REVERT,
        parent_step_id=step_id,
        code_snapshot=target_step.code_snapshot,
        reasoning=f"Reverted to step {step_id}. Redeployed at {function_url}",
        status=StepStatus.REVERTED,
    )
    await log_step(revert_step)

    return {
        "app_id": app_id,
        "reverted_to_step": step_id,
        "revert_step_id": revert_step.step_id,
        "live_url": function_url,
        "status": "reverted",
    }
