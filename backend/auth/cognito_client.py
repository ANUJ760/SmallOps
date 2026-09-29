"""Amazon Cognito client for auth operations and JWT verification.

Provides:
1. Cognito Identity Provider administrative helpers (user creation, group assignment).
2. JWT signature and claims verification for Cognito ID and access tokens.
3. User identity extraction with role normalization (viewer, editor, owner).
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any
from urllib.request import urlopen

import boto3
from jose import JWTError, jwt

from backend.config import get_settings
from backend.models.app import Role

logger = logging.getLogger(__name__)


def get_cognito_client(region: str = "ap-south-1"):
    """Return a boto3 Cognito Identity Provider client."""
    return boto3.client("cognito-idp", region_name=region)


# ── Cognito IDP Admin Operations ──────────────────────────────────────────


async def create_user(
    email: str,
    *,
    user_pool_id: str = "",
    temporary_password: str | None = None,
    region: str = "",
) -> dict[str, Any]:
    """Create a user in the Cognito User Pool (for invites / OTP flow).

    Uses ``AdminCreateUser``.
    """
    if not user_pool_id or not region:
        settings = get_settings()
        user_pool_id = user_pool_id or settings.cognito_user_pool_id
        region = region or settings.aws_region

    client = get_cognito_client(region=region)
    kwargs: dict[str, Any] = {
        "UserPoolId": user_pool_id,
        "Username": email,
        "UserAttributes": [
            {"Name": "email", "Value": email},
            {"Name": "email_verified", "Value": "true"},
        ],
        "DesiredDeliveryMediums": ["EMAIL"],
    }
    if temporary_password:
        kwargs["TemporaryPassword"] = temporary_password
    else:
        # MessageAction="RESEND" or default invite email from Cognito
        kwargs["MessageAction"] = "SUPPRESS"

    try:
        response = client.admin_create_user(**kwargs)
        return response.get("User", {})
    except client.exceptions.UsernameExistsException:
        logger.info("Cognito user %s already exists", email)
        return client.admin_get_user(UserPoolId=user_pool_id, Username=email)


async def add_user_to_group(
    username: str,
    group_name: str,
    *,
    user_pool_id: str = "",
    region: str = "",
) -> None:
    """Add a Cognito user to a user group (e.g. Viewer or Editor)."""
    if not user_pool_id or not region:
        settings = get_settings()
        user_pool_id = user_pool_id or settings.cognito_user_pool_id
        region = region or settings.aws_region

    client = get_cognito_client(region=region)
    client.admin_add_user_to_group(
        UserPoolId=user_pool_id,
        Username=username,
        GroupName=group_name,
    )


# ── JWKS & JWT Verification ──────────────────────────────────────────────


@lru_cache(maxsize=16)
def _fetch_jwks(jwks_url: str) -> dict[str, Any]:
    """Fetch and cache JSON Web Key Set (JWKS) from Cognito endpoint."""
    with urlopen(jwks_url, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _normalize_role(groups: list[str] | None, fallback: str = Role.VIEWER.value) -> str:
    """Determine highest role from user's Cognito groups."""
    if not groups:
        return fallback

    normalized = [g.strip().lower() for g in groups]
    if "owner" in normalized:
        return Role.OWNER.value
    if "editor" in normalized:
        return Role.EDITOR.value
    if "viewer" in normalized:
        return Role.VIEWER.value
    return fallback


def verify_cognito_token(
    token: str,
    *,
    user_pool_id: str = "",
    client_id: str = "",
    region: str = "",
    jwks: dict[str, Any] | None = None,
    test_secret: str | None = None,
    verify_signature: bool = True,
) -> dict[str, Any]:
    """Verify a Cognito JWT token and extract user identity and permissions.

    Parameters
    ----------
    token:
        JWT token string (ID token or access token).
    user_pool_id:
        Cognito User Pool ID. Defaults to settings value.
    client_id:
        Cognito App Client ID. Defaults to settings value.
    region:
        AWS Region. Defaults to settings value.
    jwks:
        Optional pre-fetched or mock JWKS dictionary.
    test_secret:
        Optional symmetric secret for HS256 signed tokens (used in testing).
    verify_signature:
        Whether to enforce cryptographic signature verification.

    Returns
    -------
    dict[str, Any]:
        Normalized user dictionary containing:
        - ``user_id``: user's sub / ID
        - ``email``: user's email address
        - ``role``: normalized role ('viewer', 'editor', 'owner')
        - ``groups``: list of raw Cognito groups
        - ``claims``: full payload claims
    """
    try:
        settings = get_settings()
        user_pool_id = user_pool_id or settings.cognito_user_pool_id
        client_id = client_id or settings.cognito_app_client_id
        region = region or settings.aws_region
    except Exception:
        # Fallbacks when settings cannot be loaded (e.g. testing environments)
        user_pool_id = user_pool_id or "ap-south-1_testpool"
        client_id = client_id or "testclientid"
        region = region or "ap-south-1"

    expected_issuer = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"

    # Test path: HS256 token signed with test_secret
    if test_secret:
        claims = jwt.decode(
            token,
            test_secret,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
        groups = claims.get("cognito:groups", [])
        if isinstance(groups, str):
            groups = [groups]
        fallback_role = claims.get("role", Role.EDITOR.value)
        role = _normalize_role(groups, fallback=fallback_role)
        return {
            "user_id": claims.get("sub", claims.get("username", "anonymous")),
            "email": claims.get("email", ""),
            "role": role,
            "groups": groups,
            "claims": claims,
        }

    # If signature verification is disabled (explicit testing / offline bypass)
    if not verify_signature:
        claims = jwt.get_unverified_claims(token)
        groups = claims.get("cognito:groups", [])
        if isinstance(groups, str):
            groups = [groups]
        fallback_role = claims.get("role", Role.EDITOR.value)
        role = _normalize_role(groups, fallback=fallback_role)
        return {
            "user_id": claims.get("sub", claims.get("username", "anonymous")),
            "email": claims.get("email", ""),
            "role": role,
            "groups": groups,
            "claims": claims,
        }

    # Production / RS256 path with JWKS verification
    headers = jwt.get_unverified_header(token)
    kid = headers.get("kid")
    if not kid:
        raise JWTError("Token header missing key ID (kid)")

    if jwks is None:
        jwks_url = f"{expected_issuer}/.well-known/jwks.json"
        jwks = _fetch_jwks(jwks_url)

    key = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
    if not key:
        raise JWTError(f"Key with ID {kid} not found in JWKS")

    claims = jwt.decode(
        token,
        key,
        algorithms=["RS256"],
        issuer=expected_issuer,
        options={"verify_aud": False},  # Can be access or id token
    )

    # Token use check
    token_use = claims.get("token_use")
    if token_use not in ("id", "access"):
        raise JWTError(f"Invalid token_use claim: {token_use}")

    # Audience check for ID token
    if token_use == "id" and client_id:
        aud = claims.get("aud")
        if aud != client_id:
            raise JWTError(f"Audience mismatch: expected {client_id}, got {aud}")

    groups = claims.get("cognito:groups", [])
    if isinstance(groups, str):
        groups = [groups]
    fallback_role = claims.get("role", Role.EDITOR.value)
    role = _normalize_role(groups, fallback=fallback_role)

    return {
        "user_id": claims.get("sub", claims.get("username", "")),
        "email": claims.get("email", ""),
        "role": role,
        "groups": groups,
        "claims": claims,
    }
