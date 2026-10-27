
"""Add Cognito identity mapping to the existing schema."""

import psycopg

from app.settings import get_settings


def main() -> None:
    settings = get_settings()

    with psycopg.connect(
        settings.admin_database_url
    ) as conn:
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS cognito_sub text
            """
        )

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                users_tenant_cognito_sub_unique
            ON users (tenant_id, cognito_sub)
            WHERE cognito_sub IS NOT NULL
            """
        )

        conn.commit()

    print("Cognito user migration complete")


if __name__ == "__main__":
    main()
