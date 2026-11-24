"""Test package. Importing tests.base first sets the test environment (no .env, in-memory SQLite, no
read database) before any test module imports app, whatever order the modules load in, so a test
can never reach the real databases."""
from tests import base  # noqa: F401
