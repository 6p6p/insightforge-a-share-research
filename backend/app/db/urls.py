"""Database connection URI helpers."""

from sqlalchemy import make_url


def to_postgres_connection_uri(database_url: str) -> str:
    """Convert a postgresql+psycopg:// SQLAlchemy URL to a plain postgresql:// URI.

    LangGraph's checkpointer does not accept the psycopg driver suffix. Errors
    are raised without echoing the full URL or any password.
    """
    try:
        url = make_url(database_url)
    except Exception as exc:
        raise ValueError("invalid database_url format") from exc
    if url.drivername != "postgresql+psycopg":
        raise ValueError("expected a postgresql+psycopg:// database URL")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)
