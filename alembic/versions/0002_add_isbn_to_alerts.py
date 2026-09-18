"""Add isbn column to alerts.

Revision ID: 0002_add_isbn_to_alerts
Revises: 0001_initial_schema
Create Date: 2026-09-10 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_add_isbn_to_alerts"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("alerts", sa.Column("isbn", sa.String(length=20), nullable=True))
    op.create_index("ix_alerts_isbn", "alerts", ["isbn"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_alerts_isbn", table_name="alerts")
    op.drop_column("alerts", "isbn")
