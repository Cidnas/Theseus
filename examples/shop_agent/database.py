"""Create and query the small SQLite database used by the shop example."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATABASE_PATH = PROJECT_ROOT / ".sample-data" / "shop.db"


def create_sample_database() -> Path:
    """Recreate the shop database with a small, predictable set of sample data."""

    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            DROP TABLE IF EXISTS order_items;
            DROP TABLE IF EXISTS orders;
            DROP TABLE IF EXISTS inventory;
            DROP TABLE IF EXISTS products;
            DROP TABLE IF EXISTS customers;

            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE
            );

            CREATE TABLE products (
                id INTEGER PRIMARY KEY,
                sku TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                price_cents INTEGER NOT NULL CHECK (price_cents >= 0)
            );

            CREATE TABLE inventory (
                product_id INTEGER PRIMARY KEY REFERENCES products(id),
                units_in_stock INTEGER NOT NULL CHECK (units_in_stock >= 0)
            );

            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES customers(id),
                ordered_at TEXT NOT NULL,
                status TEXT NOT NULL
            );

            CREATE TABLE order_items (
                order_id INTEGER NOT NULL REFERENCES orders(id),
                product_id INTEGER NOT NULL REFERENCES products(id),
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
                PRIMARY KEY (order_id, product_id)
            );

            INSERT INTO customers (id, name, email) VALUES
                (1, 'Alice Carter', 'alice@example.com'),
                (2, 'Bob Diaz', 'bob@example.com');

            INSERT INTO products (id, sku, name, category, price_cents) VALUES
                (1, 'NOTE-001', 'Hardcover Notebook', 'Stationery', 625),
                (2, 'PEN-001', 'Fountain Pen', 'Stationery', 1499),
                (3, 'COF-001', 'Coffee Beans', 'Groceries', 1250),
                (4, 'LAMP-001', 'Desk Lamp', 'Office', 3200);

            INSERT INTO inventory (product_id, units_in_stock) VALUES
                (1, 5),
                (2, 0),
                (3, 12),
                (4, 3);

            INSERT INTO orders (id, customer_id, ordered_at, status) VALUES
                (1, 1, '2026-06-15T10:30:00Z', 'delivered'),
                (2, 1, '2026-07-18T14:15:00Z', 'delivered'),
                (3, 2, '2026-07-20T09:00:00Z', 'processing');

            INSERT INTO order_items (
                order_id,
                product_id,
                quantity,
                unit_price_cents
            ) VALUES
                (1, 3, 2, 1250),
                (2, 1, 1, 625),
                (3, 2, 1, 1499),
                (3, 4, 1, 3200);
            """
        )
        connection.commit()
    return DATABASE_PATH


def fetch_one(query: str, parameters: Sequence[Any] = ()) -> dict[str, Any] | None:
    """Return one database row as a dictionary, or ``None`` when no row matches."""

    with closing(_connect()) as connection:
        row = connection.execute(query, parameters).fetchone()
    return dict(row) if row is not None else None


def fetch_all(query: str, parameters: Sequence[Any] = ()) -> list[dict[str, Any]]:
    """Return every matching database row as a list of dictionaries."""

    with closing(_connect()) as connection:
        rows = connection.execute(query, parameters).fetchall()
    return [dict(row) for row in rows]


def _connect() -> sqlite3.Connection:
    """Open the initialized sample database with named row access enabled."""

    if not DATABASE_PATH.is_file():
        raise RuntimeError("shop database is missing; call create_sample_database() first")
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection
