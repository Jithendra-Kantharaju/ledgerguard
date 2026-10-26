
"""Provision an authenticated Cognito user in LedgerGuard."""

from __future__ import annotations

import argparse
import uuid
from decimal import Decimal

import psycopg

from app.settings import get_settings


def main() -> None:
    parser = argparse.ArgumentParser()

    parser.add_argument("--tenant", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--sub", required=True)

    parser.add_argument(
        "--role",
        required=True,
        choices=[
            "admin",
            "approver",
            "reviewer",
            "uploader",
        ],
    )

    parser.add_argument(
        "--approval-limit",
        default="0",
    )

    args = parser.parse_args()

    # Validate subject UUID and limits before SQL.
    subject = str(uuid.UUID(args.sub))
    limit = Decimal(args.approval_limit)

    if limit < 0:
        parser.error(
            "approval limit cannot be negative"
        )

    if args.role not in ("admin", "approver") and limit:
        parser.error(
            "reviewers/uploaders must have limit 0"
        )

    settings = get_settings()

    with psycopg.connect(
        settings.admin_database_url
    ) as conn:

        tenant = conn.execute(
            """
            SELECT id
            FROM tenants
            WHERE slug = %s
            """,
            (args.tenant,),
        ).fetchone()

        if not tenant:
            raise SystemExit(
                "Tenant does not exist. "
                "Provision tenant and reference data first."
            )

        conn.execute(
            """
            INSERT INTO users (
                tenant_id,
                email,
                cognito_sub,
                role,
                approval_limit,
                active
            )
            VALUES (%s, %s, %s, %s, %s, true)
            ON CONFLICT (tenant_id, email)
            DO UPDATE
            SET cognito_sub = EXCLUDED.cognito_sub,
                role = EXCLUDED.role,
                approval_limit = EXCLUDED.approval_limit,
                active = true
            """,
            (
                tenant[0],
                args.email,
                subject,
                args.role,
                limit,
            ),
        )

        conn.commit()

    print(
        f"Provisioned {args.email} "
        f"as {args.role} in {args.tenant}"
    )


if __name__ == "__main__":
    main()
