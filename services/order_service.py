import uuid
from datetime import datetime

from database import get_db, transaction, now_str
from services.menu_service import MenuService
from services.employee_service import EmployeeService


class OrderService:
    ORDER_STATUS_PENDING = "pending"
    ORDER_STATUS_TAKEN = "taken"
    ORDER_STATUS_CANCELLED = "cancelled"

    @staticmethod
    def place_order(employee_id: str, menu_item_id: int, quantity: int, idempotency_key: str = None):
        if quantity <= 0:
            raise ValueError("数量必须大于0")

        if idempotency_key:
            existing = get_db().execute(
                "SELECT * FROM orders WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                return dict(existing)

        with transaction() as conn:
            menu_item = MenuService.get_menu_item(menu_item_id, conn)
            menu_id = menu_item["menu_id"]

            menu_row = conn.execute(
                "SELECT * FROM menus WHERE id = ?", (menu_id,)
            ).fetchone()
            if not menu_row["is_published"]:
                raise ValueError("菜单未发布，不能下单")

            deadline = datetime.strptime(menu_row["deadline"], "%Y-%m-%d %H:%M:%S")
            if datetime.now() > deadline:
                raise ValueError("已过订餐截止时间，无法下单")

            if menu_item["stock"] - menu_item["sold_count"] < quantity:
                raise ValueError("库存不足")

            emp = EmployeeService.get_employee(employee_id, conn)
            total_amount = menu_item["price"] * quantity
            available_balance = emp["balance"] - emp["frozen_balance"]
            if available_balance < total_amount:
                raise ValueError("余额不足")

            order_id = f"ORD{int(datetime.now().timestamp())}{uuid.uuid4().hex[:8].upper()}"
            now = now_str()

            frozen_before = emp["frozen_balance"]
            frozen_after = frozen_before + total_amount

            conn.execute(
                "UPDATE employees SET frozen_balance = ?, updated_at = ? WHERE id = ?",
                (frozen_after, now, employee_id),
            )

            conn.execute(
                "UPDATE menu_items SET sold_count = sold_count + ?, updated_at = ? WHERE id = ?",
                (quantity, now, menu_item_id),
            )

            conn.execute(
                """INSERT INTO orders 
                   (id, idempotency_key, employee_id, menu_id, menu_item_id, 
                    item_name, price, quantity, total_amount, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_id, idempotency_key, employee_id, menu_id, menu_item_id,
                 menu_item["name"], menu_item["price"], quantity, total_amount,
                 OrderService.ORDER_STATUS_PENDING, now, now),
            )

            conn.execute(
                """INSERT INTO transactions
                   (type, employee_id, order_id, amount, balance_before, balance_after,
                    frozen_before, frozen_after, description, idempotency_key, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("FREEZE", employee_id, order_id, total_amount,
                 emp["balance"], emp["balance"],
                 frozen_before, frozen_after,
                 f"下单冻结: {menu_item['name']} x{quantity}",
                 idempotency_key, now),
            )

            row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            return dict(row)

    @staticmethod
    def take_meal(order_id: str):
        with transaction() as conn:
            order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if not order:
                raise ValueError(f"订单 {order_id} 不存在")

            if order["status"] == OrderService.ORDER_STATUS_TAKEN:
                return dict(order)
            if order["status"] != OrderService.ORDER_STATUS_PENDING:
                raise ValueError(f"订单状态为 {order['status']}，无法取餐")

            emp = EmployeeService.get_employee(order["employee_id"], conn)
            now = now_str()

            balance_before = emp["balance"]
            balance_after = balance_before - order["total_amount"]
            frozen_before = emp["frozen_balance"]
            frozen_after = frozen_before - order["total_amount"]

            conn.execute(
                "UPDATE employees SET balance = ?, frozen_balance = ?, updated_at = ? WHERE id = ?",
                (balance_after, frozen_after, now, order["employee_id"]),
            )

            conn.execute(
                "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
                (OrderService.ORDER_STATUS_TAKEN, now, order_id),
            )

            conn.execute(
                """INSERT INTO transactions
                   (type, employee_id, order_id, amount, balance_before, balance_after,
                    frozen_before, frozen_after, description, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("SETTLE", order["employee_id"], order_id, order["total_amount"],
                 balance_before, balance_after,
                 frozen_before, frozen_after,
                 f"取餐结算: {order['item_name']} x{order['quantity']}",
                 now),
            )

            row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            return dict(row)

    @staticmethod
    def cancel_order(order_id: str):
        with transaction() as conn:
            order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if not order:
                raise ValueError(f"订单 {order_id} 不存在")

            if order["status"] == OrderService.ORDER_STATUS_CANCELLED:
                return dict(order)
            if order["status"] == OrderService.ORDER_STATUS_TAKEN:
                raise ValueError("已取餐的订单不能取消")
            if order["status"] != OrderService.ORDER_STATUS_PENDING:
                raise ValueError(f"订单状态为 {order['status']}，无法取消")

            menu_row = conn.execute(
                "SELECT deadline FROM menus WHERE id = ?", (order["menu_id"],)
            ).fetchone()
            deadline = datetime.strptime(menu_row["deadline"], "%Y-%m-%d %H:%M:%S")
            if datetime.now() > deadline:
                raise ValueError("已过订餐截止时间，无法取消")

            emp = EmployeeService.get_employee(order["employee_id"], conn)
            now = now_str()

            frozen_before = emp["frozen_balance"]
            frozen_after = frozen_before - order["total_amount"]

            conn.execute(
                "UPDATE employees SET frozen_balance = ?, updated_at = ? WHERE id = ?",
                (frozen_after, now, order["employee_id"]),
            )

            conn.execute(
                "UPDATE menu_items SET sold_count = sold_count - ?, updated_at = ? WHERE id = ?",
                (order["quantity"], now, order["menu_item_id"]),
            )

            conn.execute(
                "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
                (OrderService.ORDER_STATUS_CANCELLED, now, order_id),
            )

            conn.execute(
                """INSERT INTO transactions
                   (type, employee_id, order_id, amount, balance_before, balance_after,
                    frozen_before, frozen_after, description, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("UNFREEZE", order["employee_id"], order_id, order["total_amount"],
                 emp["balance"], emp["balance"],
                 frozen_before, frozen_after,
                 f"取消订单释放: {order['item_name']} x{order['quantity']}",
                 now),
            )

            row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            return dict(row)

    @staticmethod
    def get_order(order_id: str):
        row = get_db().execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            raise ValueError(f"订单 {order_id} 不存在")
        return dict(row)

    @staticmethod
    def list_orders(employee_id: str = None, status: str = None, menu_id: int = None):
        conn = get_db()
        query = "SELECT * FROM orders WHERE 1=1"
        params = []
        if employee_id:
            query += " AND employee_id = ?"
            params.append(employee_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        if menu_id:
            query += " AND menu_id = ?"
            params.append(menu_id)
        query += " ORDER BY created_at DESC"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
