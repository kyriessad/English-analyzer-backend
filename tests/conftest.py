"""Keep the ordinary unit suite isolated from the formal PostgreSQL database."""

import os


postgres_test_url = os.environ.get("POSTGRES_TEST_DATABASE_URL", "").strip()
os.environ["APP_ENV"] = "test"
os.environ["JWT_EXPIRE_DAYS"] = "3"

if postgres_test_url:
    os.environ["DATABASE_URL"] = postgres_test_url
    os.environ["EXPECTED_DATABASE_DIALECT"] = "postgresql"
    os.environ["ALLOW_SQLITE_FOR_TESTS"] = "false"
else:
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ["EXPECTED_DATABASE_DIALECT"] = "postgresql"
    os.environ["ALLOW_SQLITE_FOR_TESTS"] = "true"
