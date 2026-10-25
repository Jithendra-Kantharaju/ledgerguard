"""Create the schema, row-level security policies, the app role and LangGraph checkpoint tables.

Usage: python -m scripts.init_db   (uses ADMIN_DATABASE_URL; app role password from APP_DB_PASSWORD)
"""

import os

from app.db.core import init_db
from app.settings import get_settings

if __name__ == "__main__":
    init_db(get_settings().admin_database_url, app_password=os.environ.get("APP_DB_PASSWORD", "ap_app"))
    print("schema ready")
