"""Add gaming category and migrate switch game alerts.

Revision ID: 0003_add_gaming_category
Revises: 0002_add_isbn_to_alerts
Create Date: 2026-09-12 23:15:00.000000

"""

from collections.abc import Sequence
import uuid

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_add_gaming_category"
down_revision: str | None = "0002_add_isbn_to_alerts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    categories_table = sa.table(
        "categories",
        sa.column("id", sa.Uuid()),
        sa.column("slug", sa.String()),
        sa.column("label", sa.String()),
        sa.column("emoji", sa.String()),
        sa.column("is_active", sa.Boolean()),
    )
    gaming_cat_id = uuid.uuid4()
    op.execute(
        categories_table.insert().values(
            id=gaming_cat_id,
            slug="gaming",
            label="Jeux Vidéo",
            emoji="🎮",
            is_active=True,
        )
    )

    conn = op.get_bind()
    res = conn.execute(sa.text("SELECT id FROM categories WHERE slug = 'gaming'"))
    gaming_row = res.fetchone()
    if gaming_row:
        new_gaming_id = gaming_row[0]
        conn.execute(
            sa.text(
                "UPDATE alerts SET category_id = :cat_id "
                "WHERE name ILIKE '%Switch%' OR name ILIKE '%Mario%' OR name ILIKE '%Zelda%' "
                "OR name ILIKE '%Smash Bros%' OR name ILIKE '%Animal Crossing%' "
                "OR name ILIKE '%Pokemon%' OR name ILIKE '%Pok%'"
            ),
            {"cat_id": new_gaming_id},
        )


def downgrade() -> None:
    conn = op.get_bind()
    res = conn.execute(sa.text("SELECT id FROM categories WHERE slug = 'tech'"))
    tech_row = res.fetchone()
    if tech_row:
        tech_id = tech_row[0]
        conn.execute(
            sa.text(
                "UPDATE alerts SET category_id = :cat_id "
                "WHERE category_id IN (SELECT id FROM categories WHERE slug = 'gaming')"
            ),
            {"cat_id": tech_id},
        )
    conn.execute(sa.text("DELETE FROM categories WHERE slug = 'gaming'"))
