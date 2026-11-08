-- Run once as the RDS owner using psql from an approved private management host.
-- psql prompts keep passwords out of shell arguments and repository files.
CREATE ROLE dsc_migrator LOGIN;
CREATE ROLE dsc_runtime LOGIN;
\password dsc_migrator
\password dsc_runtime
ALTER SCHEMA public OWNER TO dsc_migrator;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO dsc_runtime;
GRANT CONNECT ON DATABASE dsc TO dsc_migrator, dsc_runtime;
