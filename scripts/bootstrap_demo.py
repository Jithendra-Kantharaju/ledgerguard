
"""Seed synthetic development reference data only.

Never run against a real customer database.
"""

from app.settings import get_settings
from scripts.seed import seed_all


def main() -> None:
    settings = get_settings()

    # Random API keys are generated rather than using
    # the well-known local development keys.
    # They are not printed or distributed.
    result = seed_all(
        settings.admin_database_url,
        random_keys=True,
        quiet=True,
    )

    print(
        "Synthetic tenants seeded:",
        ", ".join(result["_tenants"].keys()),
    )


if __name__ == "__main__":
    main()
