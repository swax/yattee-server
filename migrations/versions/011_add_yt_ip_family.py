"""Add yt_ip_family setting.

Forces the IP family (IPv4/IPv6) for YouTube-bound egress across yt-dlp,
InnerTube, and the /proxy/relay endpoint (see issue #7).

Revision ID: 011
Revises: 010
Create Date: 2026-07-19
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
    if not _column_exists("settings", "yt_ip_family"):
        op.execute("ALTER TABLE settings ADD COLUMN yt_ip_family TEXT DEFAULT 'auto'")


def downgrade() -> None:
    if _column_exists("settings", "yt_ip_family"):
        op.execute("ALTER TABLE settings DROP COLUMN yt_ip_family")
