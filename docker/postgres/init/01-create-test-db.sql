-- Runs once, when the Postgres container starts with an empty data volume.
-- The integration tests use this separate database so they can reset it without touching dev data.
-- If the volume already existed before this file was added, recreate it: `make reset-db`.
CREATE DATABASE jobradar_test OWNER jobradar;
