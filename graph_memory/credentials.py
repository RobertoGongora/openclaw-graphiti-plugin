"""The default database password is for a first look, not for a graph worth keeping."""

import os

DEFAULT_PASSWORD = "graph-memory"
ADVICE = (
    "Before this graph holds anything you care about, change them. MEMORY_HTTP_TOKEN "
    "only needs a new value in .env. For the Neo4j password:\n"
    "  1. In the database: ALTER CURRENT USER SET PASSWORD FROM 'graph-memory' TO '<new>'\n"
    "     (docker compose exec neo4j cypher-shell -u neo4j -p graph-memory)\n"
    "     A database that has never started with a password takes the one in .env instead.\n"
    "  2. Set NEO4J_PASSWORD=<new> in your .env (openssl rand -hex 24 makes a good one).\n"
    "  3. docker compose up -d\n"
    "For a throwaway graph, set MEMORY_ALLOW_DEFAULT_PASSWORD=1 to continue as is."
)


def refuse_default_password():
    """Called before the schema is touched, so the reminder arrives while changing
    the password is still a one-line job and not a migration."""
    unchanged = [
        name
        for name in ("NEO4J_PASSWORD", "MEMORY_HTTP_TOKEN")
        if os.environ.get(name) == DEFAULT_PASSWORD
    ]
    if not unchanged:
        return
    if os.environ.get("MEMORY_ALLOW_DEFAULT_PASSWORD", "").lower() in {"1", "true", "yes"}:
        return
    raise ValueError(f"Still at the default value: {', '.join(unchanged)}.\n" + ADVICE)
