import os
from importlib.resources import files

import psycopg

from brain import config

DOCKER_DSN = "postgresql://postgres@127.0.0.1:54329/brain"  # matches docker-compose.yml


def mode() -> str:
    return config.load().get("db", {}).get("mode", "docker")


def dsn() -> str:
    if os.environ.get("BRAIN_DSN"):
        return os.environ["BRAIN_DSN"]
    return DOCKER_DSN if mode() == "docker" else config.load().get("db", {}).get("dsn", "dbname=brain")


def connect() -> psycopg.Connection:
    return psycopg.connect(dsn(), autocommit=True, options="-c paradedb.planner_warnings=off")


def migrate(conn: psycopg.Connection) -> None:
    conn.execute(files("brain").joinpath("schema.sql").read_text())


def record_run(conn: psycopg.Connection, connector: str, error: str | None = None, config_hash: str | None = None) -> None:
    if error is None:
        conn.execute(
            """insert into connector_runs (connector, last_success, config_hash) values (%s, now(), %s)
               on conflict (connector) do update set last_success = now(),
                   config_hash = coalesce(excluded.config_hash, connector_runs.config_hash)""",
            (connector, config_hash),
        )
    else:
        conn.execute(
            """insert into connector_runs (connector, last_error, last_error_at) values (%s, %s, now())
               on conflict (connector) do update set last_error = excluded.last_error, last_error_at = now()""",
            (connector, error[:2000]),
        )
