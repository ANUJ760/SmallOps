"""App sharing and collaborator invite endpoints via SES and Cognito."""

from __future__ import annotations

import logging
from typing import Any

import boto3
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from backend.auth.cognito_client import add_user_to_group, create_user
from backend.auth.roles import require_editor
from backend.config import get_settings
from backend.models.app import Invite, Role
from backend.storage.dynamodb_client import put_item

logger = logging.getLogger(__name__)

router = APIRouter(tags=["share"])


class InviteRequest(BaseModel):
    """Request body for sharing an app with a collaborator."""

    email: str = Field(min_length=3, max_length=254, description="Collaborator email address")
    role: Role = Field(default=Role.VIEWER, description="Assigned collaborator role")


def _get_ses_client(region: str = "ap-south-1"):
    """Return a boto3 SES client."""
    return boto3.client("ses", region_name=region)


def send_invite_email(
    to_email: str,
    app_id: str,
    role: str,
    *,
    sender_email: str,
    region: str = "ap-south-1",
    app_url: str = "",
) -> dict[str, Any]:
    """Send an invitation email using Amazon SES."""
    client = _get_ses_client(region=region)
    subject = f"Invitation to collaborate on BharatBuilds app: {app_id[:8]}"
    access_link = app_url or f"https://bharatbuilds.dev/apps/{app_id}"

    text_content = (
        f"You have been invited to collaborate on a BharatBuilds app.\n\n"
        f"App ID: {app_id}\n"
        f"Assigned Role: {role.title()}\n"
        f"Access Link: {access_link}\n\n"
        "Sign in using your email to access the app."
    )

    html_content = f"""\
<!DOCTYPE html>
<html>
<body>
  <h2>You're invited to collaborate on BharatBuilds!</h2>
  <p>You have been granted <strong>{role.title()}</strong> access to application <code>{app_id}</code>.</p>
  <p><a href="{access_link}" style="display:inline-block;padding:10px 20px;background:#2563eb;color:#fff;text-decoration:none;border-radius:6px;">Open App</a></p>
  <p><small>If the button doesn't work, visit: {access_link}</small></p>
</body>
</html>
"""

    logger.info("Sending SES invite email from %s to %s for app %s", sender_email, to_email, app_id)

    response = client.send_email(
        Source=sender_email,
        Destination={"ToAddresses": [to_email]},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {
                "Text": {"Data": text_content, "Charset": "UTF-8"},
                "Html": {"Data": html_content, "Charset": "UTF-8"},
            },
        },
    )
    return response


@router.post("/apps/{app_id}/invite", dependencies=[Depends(require_editor)])
@router.post("/share/{app_id}/invite", dependencies=[Depends(require_editor)])
async def invite_user(app_id: str, body: InviteRequest):
    """Invite a collaborator by email with a Viewer or Editor role."""
    settings = get_settings()

    sender_email = settings.ses_sender_email or "no-reply@bharatbuilds.dev"
    role_str = body.role.value if isinstance(body.role, Role) else str(body.role).lower()

    # 1. Provision / ensure Cognito user exists and assign role group
    try:
        await create_user(
            body.email,
            user_pool_id=settings.cognito_user_pool_id,
            region=settings.aws_region,
        )
        group_name = "Editor" if role_str == Role.EDITOR.value else "Viewer"
        try:
            await add_user_to_group(
                body.email,
                group_name,
                user_pool_id=settings.cognito_user_pool_id,
                region=settings.aws_region,
            )
        except Exception as e:
            logger.warning("Could not add user %s to group %s: %s", body.email, group_name, e)
    except Exception as exc:
        logger.warning("Cognito user setup skipped/failed for %s: %s", body.email, exc)

    # 2. Persist invite in DynamoDB
    invite_item = {
        "app_id": app_id,
        "step_id": f"invite#{body.email}",
        "email": body.email,
        "role": role_str,
        "status": "invited",
    }
    try:
        put_item(
            settings.dynamodb_table_name,
            invite_item,
            region=settings.aws_region,
        )
    except Exception as exc:
        logger.warning("Could not persist invite to DynamoDB: %s", exc)

    # 3. Send email invitation via SES
    try:
        ses_resp = send_invite_email(
            to_email=body.email,
            app_id=app_id,
            role=role_str,
            sender_email=sender_email,
            region=settings.aws_region,
        )
        message_id = ses_resp.get("MessageId", "mock-message-id")
    except Exception as exc:
        logger.error("Failed to send invite email via SES: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to send invite email: {exc}",
        )

    return {
        "app_id": app_id,
        "email": body.email,
        "role": role_str,
        "status": "sent",
        "message_id": message_id,
    }

@router.get("/apps/{app_id}/collaborators", dependencies=[Depends(require_editor)])
async def list_collaborators(app_id: str):
    """List all collaborators for an app."""
    from backend.storage.dynamodb_client import query_by_app_id
    settings = get_settings()
    items = query_by_app_id(settings.dynamodb_table_name, app_id, region=settings.aws_region)
    collaborators = [item for item in items if item["step_id"].startswith("invite#")]
    return {"collaborators": collaborators}

@router.delete("/apps/{app_id}/collaborators/{email}", dependencies=[Depends(require_editor)])
async def remove_collaborator(app_id: str, email: str):
    """Remove a collaborator's access."""
    from backend.storage.dynamodb_client import delete_item
    settings = get_settings()
    delete_item(
        settings.dynamodb_table_name,
        app_id,
        f"invite#{email}",
        region=settings.aws_region,
    )
    return {"status": "removed"}

class UpdateRoleRequest(BaseModel):
    role: Role

@router.put("/apps/{app_id}/collaborators/{email}", dependencies=[Depends(require_editor)])
async def update_collaborator_role(app_id: str, email: str, body: UpdateRoleRequest):
    """Update a collaborator's role."""
    from backend.storage.dynamodb_client import get_item, put_item
    settings = get_settings()
    item = get_item(
        settings.dynamodb_table_name,
        app_id,
        f"invite#{email}",
        region=settings.aws_region,
    )
    if not item:
        raise HTTPException(status_code=404, detail="Collaborator not found")
        
    role_str = body.role.value if isinstance(body.role, Role) else str(body.role).lower()
    item["role"] = role_str
    put_item(
        settings.dynamodb_table_name,
        item,
        region=settings.aws_region,
    )
    return {"status": "updated", "role": role_str}
