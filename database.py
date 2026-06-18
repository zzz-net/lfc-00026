import sqlite3
import threading
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

_env_db_path = os.environ.get("CANTEEN_DB_PATH")
if _env_db_path:
    DB_PATH = Path(_env_db_path)
else:
    DB_PATH = Path(__file__).parent / "canteen.db"

_local = threading.local()


def get_db():
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(str(DB_PATH))
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA foreign_keys = ON")
        _local.conn.execute("PRAGMA journal_mode = WAL")
        _local.conn.execute("PRAGMA synchronous = NORMAL")
    return _local.conn


@contextmanager
def transaction():
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.executescript("""
    CREATE TABLE IF NOT EXISTS employees (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        balance REAL NOT NULL DEFAULT 0,
        frozen_balance REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS menus (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        serving_date TEXT NOT NULL,
        deadline TEXT NOT NULL,
        is_published INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(serving_date)
    );

    CREATE TABLE IF NOT EXISTS menu_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        menu_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        price REAL NOT NULL,
        stock INTEGER NOT NULL,
        sold_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (menu_id) REFERENCES menus(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS orders (
        id TEXT PRIMARY KEY,
        idempotency_key TEXT UNIQUE,
        employee_id TEXT NOT NULL,
        menu_id INTEGER NOT NULL,
        menu_item_id INTEGER NOT NULL,
        item_name TEXT NOT NULL,
        price REAL NOT NULL,
        quantity INTEGER NOT NULL,
        total_amount REAL NOT NULL,
        status TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'normal',
        makeup_remark TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (employee_id) REFERENCES employees(id),
        FOREIGN KEY (menu_id) REFERENCES menus(id),
        FOREIGN KEY (menu_item_id) REFERENCES menu_items(id)
    );

    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL,
        employee_id TEXT,
        order_id TEXT,
        amount REAL NOT NULL,
        balance_before REAL,
        balance_after REAL,
        frozen_before REAL,
        frozen_after REAL,
        description TEXT,
        idempotency_key TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS makeup_operation_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT NOT NULL,
        operation_type TEXT NOT NULL,
        operator TEXT,
        remark TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (order_id) REFERENCES orders(id)
    );

    CREATE TABLE IF NOT EXISTS config (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        description TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_transactions_employee ON transactions(employee_id);
    CREATE INDEX IF NOT EXISTS idx_transactions_order ON transactions(order_id);
    CREATE INDEX IF NOT EXISTS idx_orders_employee ON orders(employee_id);
    CREATE INDEX IF NOT EXISTS idx_orders_idempotency ON orders(idempotency_key);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_makeup_unique 
        ON orders(employee_id, menu_id, menu_item_id, status) 
        WHERE status = 'taken' AND source = 'makeup';
    CREATE INDEX IF NOT EXISTS idx_orders_makeup_query 
        ON orders(employee_id, source, created_at);
    CREATE INDEX IF NOT EXISTS idx_makeup_log_order ON makeup_operation_log(order_id);
    CREATE INDEX IF NOT EXISTS idx_makeup_log_type ON makeup_operation_log(operation_type);
    CREATE INDEX IF NOT EXISTS idx_makeup_log_created ON makeup_operation_log(created_at);
    """)

    _add_column_if_not_exists(cur, "orders", "source", "TEXT NOT NULL DEFAULT 'normal'")
    _add_column_if_not_exists(cur, "orders", "makeup_remark", "TEXT")
    _add_column_if_not_exists(cur, "orders", "revoked_at", "TEXT")

    _init_default_config(cur)

    conn.commit()


def _init_default_config(cur):
    defaults = [
        ("makeup_days_limit", "7", "补录允许的最大天数（向前追溯）"),
        ("makeup_default_source", "window", "补录默认来源标识"),
        ("makeup_allowed_sources", "window,admin,manual", "允许的补录来源列表，逗号分隔"),
        ("makeup_default_remark", "线下窗口补录", "补录默认备注"),
        ("makeup_allow_revoke", "true", "是否允许撤销补录"),
        ("makeup_revoke_deadline_hours", "24", "补录后允许撤销的小时数"),
    ]
    now = now_str()
    for key, value, desc in defaults:
        cur.execute(
            "INSERT OR IGNORE INTO config (key, value, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (key, value, desc, now, now),
        )


def _add_column_if_not_exists(cur, table, column, definition):
    cur.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in cur.fetchall()]
    if column not in columns:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
