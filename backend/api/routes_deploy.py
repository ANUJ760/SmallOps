"""Deploy-related endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends
from pydantic import BaseModel

from backend.agent.planner import plan_and_execute
from backend.agent.trace_logger import log_steps
from backend.auth.roles import require_editor
from backend.config import get_settings
from backend.deploy.lambda_deployer import deploy_to_lambda
from backend.models.app import App, StepType, TimelineStep
from backend.storage.dynamodb_client import put_item

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/deploy", tags=["deploy"])


class DeployRequest(BaseModel):
    """Request body for deploying an app."""

    prompt: str
    owner_id: str
    title: str = ""
    clarifications: dict[str, str] | None = None
    credentials: dict | None = None


async def _run_deploy_pipeline(app_id: str, body: DeployRequest):
    """Background task that runs the actual deploy pipeline."""
    settings = get_settings()

    try:
        code, steps = await plan_and_execute(
            body.prompt,
            clarifications=body.clarifications,
            model_id=settings.bedrock_model_id,
            region=settings.aws_region,
            credentials=body.credentials,
            app_id=app_id,
        )

        if not code:
            await log_steps(steps)
            # Update app status to failed
            _update_app_status(app_id, body, "failed", "")
            return

        deploy_status = "deployed"
        function_url = ""
        reasoning = ""

        try:
            function_url = await deploy_to_lambda(
                app_id,
                code,
                function_name=settings.deploy_lambda_function_name,
                region=settings.aws_region,
            )
            reasoning = f"Deployed successfully to Lambda: {function_url}"
        except Exception as exc:
            logger.warning(f"Lambda deploy failed: {exc}")
            function_url = f"/apps/{app_id}/live"
            reasoning = f"Deployed inline to {function_url} (AWS Lambda deployment skipped: Missing Lambda permissions or AccessDenied)"

        deploy_step = TimelineStep(
            app_id=app_id,
            step_type=StepType.DEPLOY,
            parent_step_id=steps[-1].step_id if steps else None,
            code_snapshot=code,
            reasoning=reasoning,
        )
        steps.append(deploy_step)
        await log_steps(steps)

        _update_app_status(app_id, body, deploy_status, function_url)

    except Exception as e:
        logger.error(f"Deploy pipeline failed for app {app_id}: {e}")
        _update_app_status(app_id, body, "failed", "")


def _update_app_status(app_id: str, body: DeployRequest, status: str, function_url: str):
    settings = get_settings()
    app = App(
        app_id=app_id,
        owner_id=body.owner_id,
        title=body.title or f"App {app_id[:8]}",
        prompt=body.prompt,
        live_url=function_url,
        status=status,
    )
    put_item(
        settings.dynamodb_table_name,
        {"app_id": app_id, "step_id": "__metadata__", **app.model_dump(mode="json")},
        region=settings.aws_region,
    )


@router.post("/{app_id}", dependencies=[Depends(require_editor)])
async def deploy_app(app_id: str, body: DeployRequest, background_tasks: BackgroundTasks):
    """Start the deploy pipeline in the background and return immediately."""
    
    # Set status to building immediately
    _update_app_status(app_id, body, "building", "")
    
    background_tasks.add_task(_run_deploy_pipeline, app_id, body)
    
    return {
        "app_id": app_id,
        "status": "building",
        "message": "Deploy pipeline started in background"
    }


@router.get("/{app_id}/status")
async def deploy_status(app_id: str):
    """Check the deploy status of an app."""
    settings = get_settings()
    from backend.storage.dynamodb_client import get_item

    item = get_item(
        settings.dynamodb_table_name,
        app_id,
        "__metadata__",
        region=settings.aws_region,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="App not found")
    return {
        "app_id": app_id,
        "status": item.get("status", "unknown"),
        "live_url": item.get("live_url"),
    }
