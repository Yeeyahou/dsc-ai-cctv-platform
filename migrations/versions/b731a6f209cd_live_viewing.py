"""Add live camera registration and expiring viewing sessions."""

import sqlalchemy as sa
from alembic import op

revision = "b731a6f209cd"
down_revision = "952701c2d6ef"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "live_cameras",
        sa.Column("asset_id", sa.String(128), sa.ForeignKey("assets.id"), primary_key=True),
        sa.Column(
            "edge_server_id", sa.String(128), sa.ForeignKey("edge_servers.id"), nullable=False
        ),
        sa.Column("path", sa.String(64), nullable=False),
        sa.Column("gateway_url", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("edge_server_id", "path"),
    )
    op.create_table(
        "live_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "asset_id", sa.String(128), sa.ForeignKey("live_cameras.asset_id"), nullable=False
        ),
        sa.Column("principal_id", sa.String(256), sa.ForeignKey("principals.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("revoked_at", sa.BigInteger(), nullable=True),
    )
    op.create_index("ix_live_sessions_expires_at", "live_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_table("live_sessions")
    op.drop_table("live_cameras")
