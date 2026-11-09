"""Tiny sqlite helper used by the demo order service."""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).with_name("orders.db")


def get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection
