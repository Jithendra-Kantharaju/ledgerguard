
"""Cognito access-token verification and tenant-scoped user lookup."""

from __future__ import annotations

import hmac
from functools import lru_cache

import jwt
from fastapi import HTTPException

from app.db.core import tx
from app.settings import get_settings


@lru_cache(maxsize=8)
def jwks_client(issuer: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(
        f"{issuer}/.well-known/jwks.json",
        cache_jwk_set=True,
        lifespan=300,
        timeout=5,
    )


def get_cognito_user(authorization: str | None) -> dict:
    settings = get_settings()

    if (
        not authorization
        or not authorization.startswith("Bearer ")
    ):
        raise HTTPException(
            401,
            "Bearer access token required",
        )

    token = authorization.removeprefix("Bearer ").strip()

    if not token:
        raise HTTPException(401, "empty access token")

    region = settings.aws_region
    pool_id = settings.cognito_user_pool_id
    client_id = settings.cognito_client_id

    if not pool_id or not client_id:
        raise HTTPException(
            503,
            "Cognito configuration missing",
        )

    issuer = (
        f"https://cognito-idp.{region}.amazonaws.com/"
        f"{pool_id}"
    )

    try:
        signing_key = jwks_client(
            issuer
        ).get_signing_key_from_jwt(token)

        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=issuer,
            options={"verify_aud": False},
            leeway=30,
        )

    except jwt.PyJWTError as exc:
        raise HTTPException(
            401,
            "invalid or expired access token",
        ) from exc

    if claims.get("token_use") != "access":
        raise HTTPException(
            401,
            "access token required",
        )

    if not hmac.compare_digest(
        str(claims.get("client_id", "")),
        client_id,
    ):
        raise HTTPException(
            401,
            "token client mismatch",
        )

    sub = claims.get("sub")

    if not sub:
        raise HTTPException(
            401,
            "token subject missing",
        )

    groups = claims.get("cognito:groups", [])

    if not isinstance(groups, list):
        raise HTTPException(
            403,
            "invalid Cognito groups",
        )

    tenant_groups = [
        group
        for group in groups
        if (
            isinstance(group, str)
            and group.startswith("tenant-")
        )
    ]

    # Prevent cross-tenant ambiguity.
    if len(tenant_groups) != 1:
        raise HTTPException(
            403,
            "exactly one tenant group required",
        )

    tenant_slug = tenant_groups[0].removeprefix(
        "tenant-"
    )

    # Tenant directory is readable without an active
    # tenant session. No business rows are loaded here.
    with tx() as conn:
        tenant = conn.execute(
            """
            SELECT id
            FROM tenants
            WHERE slug = %s
            """,
            (tenant_slug,),
        ).fetchone()

    if not tenant:
        raise HTTPException(
            403,
            "tenant not provisioned",
        )

    tenant_id = str(tenant["id"])

    # Row-level security applies to this query.
    with tx(tenant_id) as conn:
        user = conn.execute(
            """
            SELECT
                id,
                tenant_id,
                email,
                role,
                approval_limit
            FROM users
            WHERE cognito_sub = %s
              AND active = true
            """,
            (str(sub),),
        ).fetchone()

    if not user:
        raise HTTPException(
            403,
            "user not provisioned or inactive",
        )

    return {
        "id": str(user["id"]),
        "tenant_id": str(user["tenant_id"]),
        "email": user["email"],
        "role": user["role"],
        "approval_limit": str(user["approval_limit"]),
    }
