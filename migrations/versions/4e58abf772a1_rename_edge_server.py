"""Use Edge Server names without recreating installation, event or job records."""

import sqlalchemy as sa
from alembic import op

revision = "4e58abf772a1"
down_revision = "7c195c8eabb1"
branch_labels = None
depends_on = None


def _rename(up: bool) -> None:
    old_table, new_table = ("agents", "edge_servers") if up else ("edge_servers", "agents")
    old_id, new_id = ("agent_id", "edge_server_id") if up else ("edge_server_id", "agent_id")
    old_key, new_key = (
        ("agentVersion", "edgeServerVersion") if up else ("edgeServerVersion", "agentVersion")
    )
    old_scope, new_scope = (
        ("dsc/agent.work", "dsc/edge-server.work")
        if up
        else ("dsc/edge-server.work", "dsc/agent.work")
    )
    version = "1.1" if up else "1.0"
    dialect = op.get_context().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError(f"Unsupported edge server migration dialect: {dialect}")

    op.rename_table(old_table, new_table)
    for table in ("events", "file_requests"):
        op.alter_column(table, old_id, new_column_name=new_id, existing_type=sa.String(128))
    op.drop_index(f"ix_file_requests_{old_id}", table_name="file_requests")
    op.create_index(f"ix_file_requests_{new_id}", "file_requests", [new_id])

    if dialect == "postgresql":
        for table, current, target in (
            (new_table, f"{old_table}_pkey", f"{new_table}_pkey"),
            (new_table, f"{old_table}_principal_id_key", f"{new_table}_principal_id_key"),
            (new_table, f"{old_table}_principal_id_fkey", f"{new_table}_principal_id_fkey"),
            ("events", f"events_{old_id}_fkey", f"events_{new_id}_fkey"),
            ("file_requests", f"file_requests_{old_id}_fkey", f"file_requests_{new_id}_fkey"),
        ):
            op.execute(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{current}" TO "{target}"')
        op.execute(
            f"""
            UPDATE principals AS p
            SET scopes = (
                SELECT json_agg(CASE value WHEN '{old_scope}' THEN '{new_scope}' ELSE value END
                                ORDER BY ordinal)
                FROM json_array_elements_text(p.scopes) WITH ORDINALITY AS j(value, ordinal)
            )
            WHERE CAST(p.scopes AS jsonb) ? '{old_scope}'
            """
        )
        op.execute(
            f"""
            UPDATE {new_table}
            SET heartbeat = CAST(
                (CAST(heartbeat AS jsonb) - '{old_key}')
                || jsonb_build_object(
                    '{new_key}', heartbeat -> '{old_key}', 'schemaVersion', '{version}'
                )
                AS json
            )
            WHERE CAST(heartbeat AS jsonb) ? '{old_key}'
            """
        )
    else:
        op.execute(
            f"""
            UPDATE principals
            SET scopes = (
                SELECT json_group_array(
                    CASE value WHEN '{old_scope}' THEN '{new_scope}' ELSE value END
                )
                FROM json_each(principals.scopes)
            )
            WHERE EXISTS (SELECT 1 FROM json_each(principals.scopes) WHERE value = '{old_scope}')
            """
        )
        op.execute(
            f"""
            UPDATE {new_table}
            SET heartbeat = json_set(
                json_remove(heartbeat, '$.{old_key}'),
                '$.{new_key}', json_extract(heartbeat, '$.{old_key}'),
                '$.schemaVersion', '{version}'
            )
            WHERE json_type(heartbeat, '$.{old_key}') IS NOT NULL
            """
        )


def upgrade() -> None:
    _rename(up=True)


def downgrade() -> None:
    _rename(up=False)
