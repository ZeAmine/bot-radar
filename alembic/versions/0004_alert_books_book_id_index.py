"""Index alert_books.book_id for lookups that do not start from alert_id.

Revision ID: 0004_alert_books_book_id_index
Revises: 0003_add_gaming_category
Create Date: 2026-09-21 20:50:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_alert_books_book_id_index"
down_revision: str | None = "0003_add_gaming_category"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_alert_books_book_id", "alert_books", ["book_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_alert_books_book_id", table_name="alert_books")
