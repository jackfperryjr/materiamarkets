import psycopg
from psycopg.rows import dict_row

from config import DATABASE_URL

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    movers_count INT NOT NULL DEFAULT 10,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS collections (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    moxfield_collection_id TEXT NOT NULL,
    label TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS snapshots (
    id SERIAL PRIMARY KEY,
    collection_id INT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    fetched_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_items (
    id SERIAL PRIMARY KEY,
    snapshot_id INT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    entry_id TEXT NOT NULL,
    name TEXT NOT NULL,
    set_code TEXT NOT NULL,
    set_name TEXT NOT NULL,
    collector_number TEXT,
    finish TEXT NOT NULL,
    quantity INT NOT NULL,
    unit_price_usd NUMERIC,
    scryfall_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_collections_user_id ON collections(user_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_collection_id ON snapshots(collection_id);
CREATE INDEX IF NOT EXISTS idx_snapshot_items_snapshot_id ON snapshot_items(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_snapshot_items_entry_id ON snapshot_items(entry_id);
"""


def get_connection():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    with get_connection() as conn:
        conn.execute(SCHEMA)


# --- users -----------------------------------------------------------------

def create_user(email, password_hash):
    with get_connection() as conn:
        row = conn.execute(
            "INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING *",
            (email, password_hash),
        ).fetchone()
        return row


def get_user_by_email(email):
    with get_connection() as conn:
        return conn.execute("SELECT * FROM users WHERE email = %s", (email,)).fetchone()


def get_user_by_id(user_id):
    with get_connection() as conn:
        return conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()


def update_movers_count(user_id, movers_count):
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET movers_count = %s WHERE id = %s", (movers_count, user_id)
        )


# --- collections -------------------------------------------------------------

def create_collection(user_id, moxfield_collection_id, label):
    with get_connection() as conn:
        row = conn.execute(
            """
            INSERT INTO collections (user_id, moxfield_collection_id, label)
            VALUES (%s, %s, %s) RETURNING *
            """,
            (user_id, moxfield_collection_id, label),
        ).fetchone()
        return row


def get_collections_for_user(user_id):
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM collections WHERE user_id = %s ORDER BY created_at ASC", (user_id,)
        ).fetchall()


def get_collection(collection_id):
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM collections WHERE id = %s", (collection_id,)
        ).fetchone()


def get_all_collections():
    with get_connection() as conn:
        return conn.execute("SELECT * FROM collections").fetchall()


def rename_collection(collection_id, label):
    with get_connection() as conn:
        conn.execute("UPDATE collections SET label = %s WHERE id = %s", (label, collection_id))


def delete_collection(collection_id):
    with get_connection() as conn:
        conn.execute("DELETE FROM collections WHERE id = %s", (collection_id,))


# --- snapshots ---------------------------------------------------------------

def save_snapshot(collection_id, items, fetched_at):
    with get_connection() as conn:
        snapshot_id = conn.execute(
            "INSERT INTO snapshots (collection_id, fetched_at) VALUES (%s, %s) RETURNING id",
            (collection_id, fetched_at),
        ).fetchone()["id"]

        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO snapshot_items
                    (snapshot_id, entry_id, name, set_code, set_name, collector_number,
                     finish, quantity, unit_price_usd, scryfall_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        snapshot_id,
                        item["entry_id"],
                        item["name"],
                        item["set_code"],
                        item["set_name"],
                        item["collector_number"],
                        item["finish"],
                        item["quantity"],
                        item["unit_price_usd"],
                        item["scryfall_id"],
                    )
                    for item in items
                ],
            )
        return snapshot_id


def get_value_history(collection_id):
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.fetched_at, SUM(i.quantity * COALESCE(i.unit_price_usd, 0)) AS total_usd
            FROM snapshots s
            JOIN snapshot_items i ON i.snapshot_id = s.id
            WHERE s.collection_id = %s
            GROUP BY s.id
            ORDER BY s.fetched_at ASC
            """,
            (collection_id,),
        ).fetchall()
        for row in rows:
            row["total_usd"] = float(row["total_usd"])
        return rows


def get_latest_snapshot_ids(collection_id, limit=2):
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT id, fetched_at FROM snapshots
            WHERE collection_id = %s
            ORDER BY fetched_at DESC LIMIT %s
            """,
            (collection_id, limit),
        ).fetchall()


def get_latest_summary(collection_id):
    history = get_value_history(collection_id)
    if not history:
        return None
    return history[-1]


def _diff_snapshots(latest_id, previous_id):
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                new.entry_id,
                new.name,
                new.set_name,
                new.finish,
                new.quantity AS quantity,
                old.unit_price_usd AS old_price,
                new.unit_price_usd AS new_price
            FROM snapshot_items new
            JOIN snapshot_items old
                ON old.entry_id = new.entry_id AND old.snapshot_id = %s
            WHERE new.snapshot_id = %s
                AND old.unit_price_usd IS NOT NULL
                AND new.unit_price_usd IS NOT NULL
            """,
            (previous_id, latest_id),
        ).fetchall()

    movers = []
    for row in rows:
        price_change = float(row["new_price"]) - float(row["old_price"])
        if price_change == 0:
            continue
        row = dict(row)
        row["old_price"] = float(row["old_price"])
        row["new_price"] = float(row["new_price"])
        row["price_change"] = price_change
        row["value_change"] = price_change * row["quantity"]
        row["pct_change"] = (price_change / row["old_price"] * 100) if row["old_price"] else None
        movers.append(row)

    return movers


def get_movers(collection_id, limit=10, lookback=30):
    snapshots = get_latest_snapshot_ids(collection_id, lookback)
    if len(snapshots) < 2:
        return {"ready": False, "gainers": [], "losers": [], "as_of": None, "stale": False}

    for i in range(len(snapshots) - 1):
        movers = _diff_snapshots(snapshots[i]["id"], snapshots[i + 1]["id"])
        if movers:
            return {
                "ready": True,
                "gainers": sorted(
                    (m for m in movers if m["value_change"] > 0),
                    key=lambda r: r["value_change"],
                    reverse=True,
                )[:limit],
                "losers": sorted(
                    (m for m in movers if m["value_change"] < 0),
                    key=lambda r: r["value_change"],
                )[:limit],
                "as_of": snapshots[i]["fetched_at"],
                "stale": i > 0,
            }

    return {
        "ready": True,
        "gainers": [],
        "losers": [],
        "as_of": snapshots[0]["fetched_at"],
        "stale": False,
    }
