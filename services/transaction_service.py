import csv
import io
from datetime import datetime

from database import get_db


class TransactionService:
    @staticmethod
    def list_transactions(
        employee_id: str = None,
        order_id: str = None,
        txn_type: str = None,
        start_date: str = None,
        end_date: str = None,
        limit: int = 100,
        offset: int = 0,
    ):
        conn = get_db()
        query = "SELECT * FROM transactions WHERE 1=1"
        params = []

        if employee_id:
            query += " AND employee_id = ?"
            params.append(employee_id)
        if order_id:
            query += " AND order_id = ?"
            params.append(order_id)
        if txn_type:
            query += " AND type = ?"
            params.append(txn_type)
        if start_date:
            query += " AND created_at >= ?"
            params.append(start_date)
        if end_date:
            query += " AND created_at <= ?"
            params.append(end_date)

        count_query = query.replace("SELECT *", "SELECT COUNT(*) as cnt")
        total = conn.execute(count_query, params).fetchone()["cnt"]

        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(query, params).fetchall()
        return {"total": total, "items": [dict(r) for r in rows]}

    @staticmethod
    def export_transactions_csv(
        employee_id: str = None,
        start_date: str = None,
        end_date: str = None,
    ):
        result = TransactionService.list_transactions(
            employee_id=employee_id,
            start_date=start_date,
            end_date=end_date,
            limit=100000,
            offset=0,
        )

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "流水ID", "类型", "员工ID", "订单ID", "金额",
            "变动前余额", "变动后余额", "变动前冻结", "变动后冻结",
            "描述", "幂等键", "时间",
        ])

        type_map = {
            "INITIAL": "初始余额",
            "ADJUST": "管理员调整",
            "FREEZE": "下单冻结",
            "SETTLE": "取餐结算",
            "UNFREEZE": "取消解冻",
        }

        for txn in result["items"]:
            writer.writerow([
                txn["id"],
                type_map.get(txn["type"], txn["type"]),
                txn["employee_id"] or "",
                txn["order_id"] or "",
                txn["amount"],
                txn["balance_before"],
                txn["balance_after"],
                txn["frozen_before"],
                txn["frozen_after"],
                txn["description"] or "",
                txn["idempotency_key"] or "",
                txn["created_at"],
            ])

        return output.getvalue()


class ReconciliationService:
    @staticmethod
    def check_consistency():
        conn = get_db()
        issues = []

        employees = conn.execute("SELECT * FROM employees").fetchall()

        for emp in employees:
            emp_id = emp["id"]

            txns = conn.execute(
                "SELECT * FROM transactions WHERE employee_id = ? ORDER BY id",
                (emp_id,),
            ).fetchall()

            if txns:
                last_txn = txns[-1]
                if abs(last_txn["balance_after"] - emp["balance"]) > 0.001:
                    issues.append(
                        f"员工 {emp_id} 余额不一致: 账户={emp['balance']}, 流水末尾={last_txn['balance_after']}"
                    )
                if abs(last_txn["frozen_after"] - emp["frozen_balance"]) > 0.001:
                    issues.append(
                        f"员工 {emp_id} 冻结余额不一致: 账户={emp['frozen_balance']}, 流水末尾={last_txn['frozen_after']}"
                    )
            else:
                if abs(emp["balance"]) > 0.001:
                    issues.append(f"员工 {emp_id} 有余额但无流水记录")
                if abs(emp["frozen_balance"]) > 0.001:
                    issues.append(f"员工 {emp_id} 有冻结余额但无流水记录")

            pending_orders = conn.execute(
                "SELECT SUM(total_amount) as total FROM orders WHERE employee_id = ? AND status = 'pending'",
                (emp_id,),
            ).fetchone()
            expected_frozen = pending_orders["total"] or 0
            if abs(expected_frozen - emp["frozen_balance"]) > 0.001:
                issues.append(
                    f"员工 {emp_id} 冻结金额与待结算订单不符: 冻结={emp['frozen_balance']}, 待结算订单={expected_frozen}"
                )

        items = conn.execute("SELECT * FROM menu_items").fetchall()
        for item in items:
            if item["sold_count"] < 0:
                issues.append(f"菜品 {item['id']} ({item['name']}) 已售数量为负: {item['sold_count']}")
            if item["sold_count"] > item["stock"]:
                issues.append(
                    f"菜品 {item['id']} ({item['name']}) 超卖: 库存={item['stock']}, 已售={item['sold_count']}"
                )

        orders = conn.execute("SELECT * FROM orders").fetchall()
        for order in orders:
            item = conn.execute(
                "SELECT * FROM menu_items WHERE id = ?", (order["menu_item_id"],)
            ).fetchone()
            if item and order["status"] != "cancelled":
                txn_count = conn.execute(
                    "SELECT COUNT(*) as cnt FROM transactions WHERE order_id = ?",
                    (order["id"],),
                ).fetchone()["cnt"]
                if order["status"] == "pending" and txn_count < 1:
                    issues.append(f"订单 {order['id']} 缺少冻结流水")
                if order["status"] == "taken" and txn_count < 2:
                    issues.append(f"订单 {order['id']} 流水不完整（至少需要冻结+结算）")

        return {
            "consistent": len(issues) == 0,
            "issues": issues,
            "employee_count": len(employees),
            "order_count": len(orders),
        }
