"""Journal des changements de réglages d'alerte, une ligne par champ.

Aucune valeur antérieure n'est inventée : la table démarre vide à la mise en
service, et seule la table du journal est créée — celle des alertes ne bouge pas.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0016_alert_setting_changes"
down_revision: str | None = "0015_chasse_weekly_prices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alert_setting_changes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "alert_id",
            sa.Uuid(),
            sa.ForeignKey("alerts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("field", sa.String(64), nullable=False),
        sa.Column("old_value", sa.JSON().with_variant(JSONB, "postgresql")),
        sa.Column("new_value", sa.JSON().with_variant(JSONB, "postgresql")),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("origin", sa.String(200), nullable=False),
    )
    op.create_index(
        "ix_alert_setting_changes_alert_id_changed_at",
        "alert_setting_changes",
        ["alert_id", "changed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_alert_setting_changes_alert_id_changed_at", table_name="alert_setting_changes"
    )
    op.drop_table("alert_setting_changes")
