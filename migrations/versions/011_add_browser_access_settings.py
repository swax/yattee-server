"""Add runtime-managed browser access settings.

Revision ID: 011
Revises: 010
Create Date: 2026-08-15
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "011"
down_revision: Union[str, Sequence[str], None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(sa.text(f"PRAGMA table_info({table})"))
    return any(row[1] == column for row in result)


def upgrade() -> None:
    if not _column_exists("settings", "cors_allowed_origins"):
        op.execute("ALTER TABLE settings ADD COLUMN cors_allowed_origins TEXT DEFAULT '[]'")
    if not _column_exists("settings", "cors_allow_localhost"):
        op.execute("ALTER TABLE settings ADD COLUMN cors_allow_localhost INTEGER DEFAULT 0")


def downgrade() -> None:
    if _column_exists("settings", "cors_allow_localhost"):
        op.execute("ALTER TABLE settings DROP COLUMN cors_allow_localhost")
    if _column_exists("settings", "cors_allowed_origins"):
        op.execute("ALTER TABLE settings DROP COLUMN cors_allowed_origins")
