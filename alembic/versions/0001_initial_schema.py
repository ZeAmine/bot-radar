"""Initial schema migration with all 10 tables and initial categories.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-09-09 20:00:00.000000

"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("first_name", sa.String(length=255), nullable=True),
        sa.Column("is_allowed", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_user_id"),
    )
    op.create_index("ix_users_telegram_user_id", "users", ["telegram_user_id"])

    op.create_table(
        "categories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=50), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("emoji", sa.String(length=10), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("ix_categories_slug", "categories", ["slug"])

    # Seed initial categories
    categories_table = sa.table(
        "categories",
        sa.column("id", sa.Uuid()),
        sa.column("slug", sa.String()),
        sa.column("label", sa.String()),
        sa.column("emoji", sa.String()),
        sa.column("is_active", sa.Boolean()),
    )
    op.bulk_insert(
        categories_table,
        [
            {
                "id": uuid.uuid4(),
                "slug": "bd_comics",
                "label": "BD & Comics",
                "emoji": "📚",
                "is_active": True,
            },
            {
                "id": uuid.uuid4(),
                "slug": "clothes",
                "label": "Vêtements",
                "emoji": "👕",
                "is_active": True,
            },
            {
                "id": uuid.uuid4(),
                "slug": "tech",
                "label": "Tech",
                "emoji": "💻",
                "is_active": True,
            },
        ],
    )

    op.create_table(
        "alerts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("vinted_search_url", sa.Text(), nullable=True),
        sa.Column("include_keywords", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("exclude_keywords", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("variants", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("max_item_price_cents", sa.Integer(), nullable=True),
        sa.Column("accepted_conditions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_alerts_category_id", "alerts", ["category_id"])
    op.create_index("ix_alerts_user_id", "alerts", ["user_id"])
    op.create_index("ix_alerts_status", "alerts", ["status"])

    op.create_table(
        "books",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("isbn13", sa.String(length=13), nullable=True),
        sa.Column("isbn10", sa.String(length=10), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("normalized_title", sa.String(length=255), nullable=False),
        sa.Column("author", sa.String(length=255), nullable=True),
        sa.Column("publisher", sa.String(length=255), nullable=True),
        sa.Column("publication_year", sa.Integer(), nullable=True),
        sa.Column("edition", sa.String(length=255), nullable=True),
        sa.Column("format", sa.String(length=100), nullable=True),
        sa.Column("language", sa.String(length=10), server_default="fr", nullable=False),
        sa.Column("cover_url", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("isbn13"),
    )
    op.create_index("ix_books_isbn13", "books", ["isbn13"])
    op.create_index("ix_books_normalized_title", "books", ["normalized_title"])

    op.create_table(
        "alert_books",
        sa.Column("alert_id", sa.Uuid(), nullable=False),
        sa.Column("book_id", sa.Uuid(), nullable=False),
        sa.Column("match_confidence", sa.String(length=20), server_default="high", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["alert_id"], ["alerts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["book_id"], ["books.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("alert_id", "book_id"),
    )

    op.create_table(
        "listings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_name", sa.String(length=100), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("normalized_title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category_slug", sa.String(length=50), nullable=False),
        sa.Column("item_price_cents", sa.Integer(), nullable=False),
        sa.Column("estimated_protection_cents", sa.Integer(), nullable=True),
        sa.Column("estimated_shipping_cents", sa.Integer(), nullable=True),
        sa.Column("estimated_total_cents", sa.Integer(), nullable=True),
        sa.Column("condition", sa.String(length=100), nullable=True),
        sa.Column("seller_name", sa.String(length=255), nullable=True),
        sa.Column("seller_rating", sa.Numeric(precision=3, scale=2), nullable=True),
        sa.Column("photos", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("isbn13", sa.String(length=13), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_name", "external_id", name="uq_listings_source_external_id"),
    )
    op.create_index("ix_listings_category_slug", "listings", ["category_slug"])
    op.create_index("ix_listings_isbn13", "listings", ["isbn13"])
    op.create_index("ix_listings_normalized_title", "listings", ["normalized_title"])
    op.create_index("ix_listings_received_at", "listings", ["received_at"])

    op.create_table(
        "listing_matches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("alert_id", sa.Uuid(), nullable=False),
        sa.Column("listing_id", sa.Uuid(), nullable=False),
        sa.Column("keyword_match_score", sa.Integer(), server_default="0", nullable=False),
        sa.Column("condition_matches", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("price_matches", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["alert_id"], ["alerts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["listing_id"], ["listings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("alert_id", "listing_id", name="uq_listing_matches_alert_listing"),
    )
    op.create_index("ix_listing_matches_alert_id", "listing_matches", ["alert_id"])
    op.create_index("ix_listing_matches_listing_id", "listing_matches", ["listing_id"])

    op.create_table(
        "market_price_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("book_id", sa.Uuid(), nullable=False),
        sa.Column("provider_name", sa.String(length=100), nullable=False),
        sa.Column("item_price_cents", sa.Integer(), nullable=False),
        sa.Column("shipping_price_cents", sa.Integer(), nullable=True),
        sa.Column("delivered_price_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="EUR", nullable=False),
        sa.Column("item_condition", sa.String(length=100), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column(
            "checked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("is_comparable", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("confidence", sa.String(length=20), server_default="high", nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["book_id"], ["books.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_market_price_snapshots_book_id", "market_price_snapshots", ["book_id"])
    op.create_index(
        "ix_market_price_snapshots_checked_at", "market_price_snapshots", ["checked_at"]
    )
    op.create_index(
        "ix_market_price_snapshots_provider_name", "market_price_snapshots", ["provider_name"]
    )

    op.create_table(
        "book_market_cache",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("book_id", sa.Uuid(), nullable=False),
        sa.Column("lowest_delivered_price_cents", sa.Integer(), nullable=True),
        sa.Column("best_provider_name", sa.String(length=100), nullable=True),
        sa.Column("best_source_url", sa.Text(), nullable=True),
        sa.Column("confidence", sa.String(length=20), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "comparison_status", sa.String(length=20), server_default="unavailable", nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["book_id"], ["books.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("book_id"),
    )
    op.create_index("ix_book_market_cache_expires_at", "book_market_cache", ["expires_at"])

    op.create_table(
        "bot_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("alert_id", sa.Uuid(), nullable=True),
        sa.Column("listing_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["alert_id"], ["alerts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["listing_id"], ["listings.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bot_events_created_at", "bot_events", ["created_at"])
    op.create_index("ix_bot_events_event_type", "bot_events", ["event_type"])


def downgrade() -> None:
    op.drop_table("bot_events")
    op.drop_table("book_market_cache")
    op.drop_table("market_price_snapshots")
    op.drop_table("listing_matches")
    op.drop_table("listings")
    op.drop_table("alert_books")
    op.drop_table("books")
    op.drop_table("alerts")
    op.drop_table("categories")
    op.drop_table("users")
