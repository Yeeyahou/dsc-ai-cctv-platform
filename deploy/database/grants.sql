-- Run as dsc_migrator after alembic upgrade head, including after new migrations.
GRANT SELECT ON assets, principals, edge_servers TO dsc_runtime;
GRANT UPDATE (last_seen, heartbeat) ON edge_servers TO dsc_runtime;
GRANT SELECT, INSERT ON events, event_assets TO dsc_runtime;
GRANT SELECT, INSERT, UPDATE ON event_media, file_requests, job_leases, clip_replicas, archive_transfers TO dsc_runtime;
GRANT INSERT ON audit_log TO dsc_runtime;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO dsc_runtime;
