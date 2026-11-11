"""Add verified office replicas and separately assigned upload workers."""

import sqlalchemy as sa
from alembic import op

revision = "952701c2d6ef"
down_revision = "4e58abf772a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "edge_servers", sa.Column("role", sa.String(16), nullable=False, server_default="legacy")
    )
    op.add_column("edge_servers", sa.Column("archive_url", sa.Text(), nullable=True))
    if op.get_context().dialect.name == "sqlite":
        op.execute(
            "ALTER TABLE edge_servers ADD COLUMN archive_server_id VARCHAR(128) "
            "CONSTRAINT fk_edge_servers_archive REFERENCES edge_servers(id)"
        )
    else:
        op.add_column("edge_servers", sa.Column("archive_server_id", sa.String(128), nullable=True))
        op.create_foreign_key(
            "fk_edge_servers_archive", "edge_servers", "edge_servers", ["archive_server_id"], ["id"]
        )
    with op.batch_alter_table("file_requests") as batch:
        batch.add_column(sa.Column("media_revision", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("expected_size", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("expected_checksum", sa.String(44), nullable=True))
        batch.add_column(sa.Column("assigned_at", sa.BigInteger(), nullable=True))
    op.create_table(
        "clip_replicas",
        sa.Column("event_id", sa.String(36), sa.ForeignKey("events.id"), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column(
            "edge_server_id", sa.String(128), sa.ForeignKey("edge_servers.id"), primary_key=True
        ),
        sa.Column("clip_id", sa.String(36), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("checksum", sa.String(44), nullable=False),
        sa.Column("present", sa.Boolean(), nullable=False),
        sa.Column("verified_at", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "archive_transfers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_id", sa.String(36), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("clip_id", sa.String(36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.String(128), sa.ForeignKey("edge_servers.id"), nullable=False),
        sa.Column("receiver_id", sa.String(128), sa.ForeignKey("edge_servers.id"), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("checksum", sa.String(44), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("completed_at", sa.BigInteger(), nullable=True),
    )
    op.create_index("ix_archive_transfers_event_id", "archive_transfers", ["event_id"])
    op.create_index("ix_archive_transfers_expires_at", "archive_transfers", ["expires_at"])


def downgrade() -> None:
    # An operator must export transfer and replica records before opting into destructive rollback.
    connection = op.get_bind()
    if (
        connection.execute(sa.text("SELECT 1 FROM clip_replicas LIMIT 1")).first()
        or connection.execute(sa.text("SELECT 1 FROM archive_transfers LIMIT 1")).first()
    ):
        raise RuntimeError(
            "replication records exist; retain the additive schema when rolling back code"
        )
    op.drop_table("archive_transfers")
    op.drop_table("clip_replicas")
    for name in ("assigned_at", "expected_checksum", "expected_size", "media_revision"):
        op.drop_column("file_requests", name)
    if op.get_context().dialect.name != "sqlite":
        op.drop_constraint("fk_edge_servers_archive", "edge_servers", type_="foreignkey")
    for name in ("archive_server_id", "archive_url", "role"):
        op.drop_column("edge_servers", name)
