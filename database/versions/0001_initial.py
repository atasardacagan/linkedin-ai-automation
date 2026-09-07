"""Initial production schema."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None


def upgrade():
    post_status = postgresql.ENUM(
        "draft",
        "pending_approval",
        "revision_requested",
        "approved",
        "scheduled",
        "publishing",
        "published",
        "rejected",
        "publish_failed",
        name="post_status",
        create_type=False,
    )
    postgresql.ENUM(
        "draft",
        "pending_approval",
        "revision_requested",
        "approved",
        "scheduled",
        "publishing",
        "published",
        "rejected",
        "publish_failed",
        name="post_status",
    ).create(op.get_bind(), checkfirst=True)
    op.create_table(
        "settings",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", postgresql.JSONB, nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "topics",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("weight", sa.Float, server_default="1", nullable=False),
        sa.Column("active", sa.Boolean, server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("uq_topics_name_ci", "topics", [sa.text("lower(name)")], unique=True)
    op.create_table(
        "posts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("generation_slot", sa.String(80), unique=True),
        sa.Column("topic", sa.String(240), nullable=False),
        sa.Column("category", sa.String(120)),
        sa.Column("content_type", sa.String(80), nullable=False),
        sa.Column("status", post_status, nullable=False, server_default="draft"),
        sa.Column("draft", sa.Text, nullable=False),
        sa.Column("approved_text", sa.Text),
        sa.Column("approved_image_path", sa.Text),
        sa.Column("hashtags", postgresql.ARRAY(sa.String), server_default="{}", nullable=False),
        sa.Column("sources", postgresql.JSONB, server_default="[]", nullable=False),
        sa.Column("image_path", sa.Text),
        sa.Column("image_asset_urn", sa.Text),
        sa.Column("scores", postgresql.JSONB, server_default="{}", nullable=False),
        sa.Column("embedding", postgresql.ARRAY(sa.Float)),
        sa.Column("approval_nonce", sa.String(64), unique=True),
        sa.Column("approval_expires_at", sa.DateTime(timezone=True)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("scheduled_at", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("linkedin_post_urn", sa.Text),
        sa.Column("linkedin_url", sa.Text),
        sa.Column("last_error", sa.Text),
        sa.Column("publish_attempts", sa.Integer, server_default="0", nullable=False),
        sa.Column("next_publish_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("requires_reconciliation", sa.Boolean, server_default=sa.false(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status NOT IN ('approved','scheduled','publishing','published','publish_failed') "
            "OR (approved_at IS NOT NULL AND approved_text IS NOT NULL)",
            name="ck_posts_publish_requires_approval",
        ),
        sa.CheckConstraint(
            "status != 'pending_approval' "
            "OR (approval_nonce IS NOT NULL AND approval_expires_at IS NOT NULL)",
            name="ck_posts_pending_requires_nonce",
        ),
        sa.CheckConstraint(
            "status != 'scheduled' OR scheduled_at IS NOT NULL",
            name="ck_posts_scheduled_requires_time",
        ),
        sa.CheckConstraint(
            "status != 'published' OR (published_at IS NOT NULL "
            "AND linkedin_post_urn IS NOT NULL AND linkedin_url IS NOT NULL)",
            name="ck_posts_published_requires_receipt",
        ),
        sa.CheckConstraint(
            "requires_reconciliation IS FALSE OR status = 'publishing'",
            name="ck_posts_reconciliation_state",
        ),
        sa.CheckConstraint(
            "publish_attempts >= 0",
            name="ck_posts_publish_attempts_nonnegative",
        ),
    )
    op.create_index("ix_posts_status_scheduled", "posts", ["status", "scheduled_at"])
    op.create_table(
        "revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "post_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("posts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("instruction", sa.Text),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("image_path", sa.Text),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("post_id", "version"),
    )
    op.create_table(
        "analytics",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "post_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("posts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("impressions", sa.Integer),
        sa.Column("reactions", sa.Integer),
        sa.Column("comments", sa.Integer),
        sa.Column("reposts", sa.Integer),
        sa.Column("clicks", sa.Integer),
        sa.Column("engagement_rate", sa.Float),
        sa.Column("raw", postgresql.JSONB, server_default="{}", nullable=False),
        sa.Column(
            "collected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "post_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("posts.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("revisions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("image_digest", sa.String(64)),
        sa.Column("approved_by", sa.String(160), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "notification_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        sa.Column(
            "post_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("posts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(80), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_notification_outbox_due", "notification_outbox", ["status", "next_attempt_at"]
    )
    op.create_table(
        "generation_runs",
        sa.Column("slot", sa.String(80), primary_key=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "post_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("posts.id", ondelete="SET NULL"),
        ),
        sa.Column("last_error", sa.Text),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_generation_runs_due", "generation_runs", ["status", "next_attempt_at"])
    op.create_table(
        "telegram_update_inbox",
        sa.Column("update_id", sa.BigInteger, primary_key=True),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_telegram_update_inbox_due",
        "telegram_update_inbox",
        ["status", "next_attempt_at"],
    )
    op.create_table(
        "telegram_pending_actions",
        sa.Column("user_id", sa.BigInteger, primary_key=True),
        sa.Column("chat_id", sa.BigInteger, nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column(
            "post_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("posts.id", ondelete="CASCADE"),
        ),
        sa.Column("nonce", sa.String(64)),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("event", sa.String(100), nullable=False),
        sa.Column(
            "post_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("posts.id", ondelete="SET NULL")
        ),
        sa.Column("actor", sa.String(120), nullable=False),
        sa.Column("details", postgresql.JSONB, server_default="{}", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.execute(
        """
        CREATE FUNCTION prevent_immutable_row_mutation() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'immutable records cannot be changed or deleted';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER audit_events_immutable
          BEFORE UPDATE OR DELETE ON audit_events
          FOR EACH ROW EXECUTE FUNCTION prevent_immutable_row_mutation();
        CREATE TRIGGER approvals_immutable
          BEFORE UPDATE OR DELETE ON approvals
          FOR EACH ROW EXECUTE FUNCTION prevent_immutable_row_mutation();
        CREATE TRIGGER revisions_immutable
          BEFORE UPDATE OR DELETE ON revisions
          FOR EACH ROW EXECUTE FUNCTION prevent_immutable_row_mutation();
        """
    )


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS audit_events_immutable ON audit_events")
    op.execute("DROP TRIGGER IF EXISTS approvals_immutable ON approvals")
    op.execute("DROP TRIGGER IF EXISTS revisions_immutable ON revisions")
    for table in (
        "audit_events",
        "telegram_pending_actions",
        "telegram_update_inbox",
        "generation_runs",
        "notification_outbox",
        "approvals",
        "analytics",
        "revisions",
        "posts",
        "topics",
        "settings",
    ):
        op.drop_table(table)
    op.execute("DROP FUNCTION IF EXISTS prevent_immutable_row_mutation()")
    postgresql.ENUM(name="post_status").drop(op.get_bind(), checkfirst=True)
