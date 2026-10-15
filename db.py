"""
Supabase access, for every entry point.

One client, one environment check, one way to read a table completely.

This used to be four copies. post_to_supabase, metrics, sign_ups and
rotate_credentials each built their own client at import and three of them
carried their own env check - so one sync run, which imports both
post_to_supabase and metrics, held two clients. metrics even documented the
duplication as deliberate, because the only alternative on offer was
importing post_to_supabase and inheriting its side effects. A module with no
side effects of its own is the fix for that.

More importantly, only metrics had a paged read. PostgREST caps every
response at Supabase's max-rows (1,000 by default) and does it SILENTLY - a
normal 200 with fewer rows - so every other unbounded select was a
truncation waiting for the table to grow:

    sync           credentials past row 1,000 never synced, green run
    rotation       re-encrypted the first 1,000 credentials, then verified
                   the same 1,000 and announced the retired key safe to
                   delete - stranding everything after it, permanently

Every read of a table that grows with participants now goes through
fetch_all().
"""

import logging
import os
import threading

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

logger = logging.getLogger(__name__)

# One request's worth of rows. If the server's max-rows is lower, fetch_all
# still reads everything - it steps by what came back, not by this.
PAGE_SIZE = 1000

# Refuse to walk further than this in one read. A participant with a million
# snapshots is a bug, not a trader, and pulling it would take the whole run
# down with it.
MAX_FETCH_ROWS = 500_000

_client: Client | None = None
_client_lock = threading.Lock()


def require_env(*names: str, hint: str = "") -> None:
    """
    Fail with a message naming what's missing, rather than letting a library
    raise several frames deep. On a scheduled runner the environment is the
    most common thing to get wrong, and the default traceback doesn't say
    which variable was absent.
    """
    missing = [n for n in names if not os.getenv(n)]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): " + ", ".join(missing)
            + ". Set them in your .env locally, or in the service's Variables "
              "on your host." + (f" {hint}" if hint else ""))


def client() -> Client:
    """
    The process's Supabase client, built on first use.

    create_client only constructs - it does not connect - so building this
    needs the environment but not the network.
    """
    global _client
    with _client_lock:
        if _client is None:
            require_env("SUPABASE_URL", "SUPABASE_KEY")
            _client = create_client(os.getenv("SUPABASE_URL"),
                                    os.getenv("SUPABASE_KEY"))
        return _client


def fetch_all(build_query, label: str) -> list[dict]:
    """
    Run a select to completion, page by page.

    `build_query` returns a FRESH query each call, because a PostgREST query
    object cannot be re-ranged once executed. It must be ORDERED on something
    unique: range paging over an ambiguous sort can shift rows between pages,
    dropping one and repeating another.

    Steps by the number of rows actually RETURNED rather than the page size
    asked for. If the server's cap is lower than PAGE_SIZE, a fixed stride
    would skip everything between the cap and the stride - reading a fraction
    of the table while looking like a full scan.
    """
    rows: list[dict] = []
    start = 0

    while True:
        page = (build_query()
                .range(start, start + PAGE_SIZE - 1)
                .execute().data) or []
        if not page:
            break

        rows.extend(page)
        start += len(page)

        if start >= MAX_FETCH_ROWS:
            logger.warning(
                "Stopped reading %s at %d rows - refusing to pull more. "
                "Anything computed from it is working on a truncated result.",
                label, start)
            break

    return rows
