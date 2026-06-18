import sqlite3
from datetime import datetime
from typing import Optional

from database import get_db, transaction, now_str


class EmployeeService:
    @staticmethod
    def create_employee(emp_id: str, name: str, initial_balance: float = 0):
        with transaction() as conn:
            cur = conn.cursor()
            now = now_str()
            try:
                cur.execute(
                    "INSERT INTO employees (id, name, balance, frozen_balance, created_at, updated_at) VALUES (?, ?, ?, 0, ?, ?)",
                    (emp_id, name, initial_balance, now, now),
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"员工ID {emp_id} 已存在")

            if initial_balance > 0:
                cur.execute(
                    """INSERT INTO transactions 
                       (type, employee_id, amount, balance_before, balance_after, 
                        frozen_before, frozen_after, description, created_at)
                       VALUES (?, ?, ?, 0, ?, 0, 0, ?, ?)""",
                    ("INITIAL", emp_id, initial_balance, initial_balance,
                     f"初始余额充值", now),
                )

            return EmployeeService.get_employee(emp_id, conn)

    @staticmethod
    def get_employee(emp_id: str, conn=None):
        if conn is None:
            conn = get_db()
        row = conn.execute("SELECT * FROM employees WHERE id = ?", (emp_id,)).fetchone()
        if not row:
            raise ValueError(f"员工 {emp_id} 不存在")
        return dict(row)

    @staticmethod
    def list_employees():
        conn = get_db()
        rows = conn.execute("SELECT * FROM employees ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def adjust_balance(emp_id: str, amount: float, description: str = "管理员调整"):
        with transaction() as conn:
            emp = EmployeeService.get_employee(emp_id, conn)
            balance_before = emp["balance"]
            balance_after = balance_before + amount
            if balance_after < 0:
                raise ValueError("余额不足")

            now = now_str()
            conn.execute(
                "UPDATE employees SET balance = ?, updated_at = ? WHERE id = ?",
                (balance_after, now, emp_id),
            )
            conn.execute(
                """INSERT INTO transactions 
                   (type, employee_id, amount, balance_before, balance_after,
                    frozen_before, frozen_after, description, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("ADJUST", emp_id, amount, balance_before, balance_after,
                 emp["frozen_balance"], emp["frozen_balance"], description, now),
            )
            return EmployeeService.get_employee(emp_id, conn)
