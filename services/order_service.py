import uuid
import logging
import sqlite3
from datetime import datetime, timedelta

from database import get_db, transaction, now_str
from services.menu_service import MenuService
from services.employee_service import EmployeeService
from services.config_service import ConfigService

logger = logging.getLogger(__name__)


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

    @staticmethod
    def makeup_order(
        employee_id: str,
        menu_item_id: int,
        quantity: int,
        serving_date: str,
        source: str = None,
        remark: str = None,
        idempotency_key: str = None,
    ):
        if quantity <= 0:
            raise ValueError("数量必须大于0")

        try:
            datetime.strptime(serving_date, "%Y-%m-%d")
        except ValueError:
            raise ValueError("供餐日期格式错误，应为 YYYY-MM-DD")

        if idempotency_key:
            existing = get_db().execute(
                "SELECT * FROM orders WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                logger.info(f"[补录] 幂等键命中，直接返回已有订单: {existing['id']}")
                return dict(existing)

        makeup_cfg = ConfigService.get_makeup_config()
        days_limit = makeup_cfg["days_limit"]
        allowed_sources = makeup_cfg["allowed_sources"]
        default_source = makeup_cfg["default_source"]
        default_remark = makeup_cfg["default_remark"]

        source = source or default_source
        remark = remark or default_remark

        if source not in allowed_sources:
            raise ValueError(
                f"补录来源 '{source}' 不合法，允许的来源: {', '.join(allowed_sources)}"
            )

        serving_date_obj = datetime.strptime(serving_date, "%Y-%m-%d").date()
        today = datetime.now().date()
        days_diff = (today - serving_date_obj).days

        if days_diff < 0:
            raise ValueError("补录日期不能晚于今天")
        if days_diff > days_limit:
            raise ValueError(
                f"补录日期超出允许范围，最多允许补录 {days_limit} 天内的记录，当前已过 {days_diff} 天"
            )

        logger.info(
            f"[补录] 开始处理: 员工={employee_id}, 菜品={menu_item_id}, 数量={quantity}, "
            f"日期={serving_date}, 来源={source}, 日期差={days_diff}天"
        )

        with transaction() as conn:
            menu_item = MenuService.get_menu_item(menu_item_id, conn)
            menu_id = menu_item["menu_id"]

            menu_row = conn.execute(
                "SELECT * FROM menus WHERE id = ?", (menu_id,)
            ).fetchone()

            if not menu_row["is_published"]:
                logger.warning(f"[补录] 菜单未发布: menu_id={menu_id}")
                raise ValueError("菜单未发布，不能补录")

            if menu_row["serving_date"] != serving_date:
                logger.warning(
                    f"[补录] 日期不匹配: 菜品所属菜单日期={menu_row['serving_date']}, "
                    f"请求日期={serving_date}"
                )
                raise ValueError(
                    f"菜品所属菜单日期为 {menu_row['serving_date']}，与补录日期 {serving_date} 不匹配"
                )

            existing_makeup = conn.execute(
                """SELECT * FROM orders 
                   WHERE employee_id = ? AND menu_id = ? AND menu_item_id = ? 
                   AND status = 'taken' AND source <> 'normal'""",
                (employee_id, menu_id, menu_item_id),
            ).fetchone()
            if existing_makeup:
                logger.warning(
                    f"[补录] 重复补录检测: 员工={employee_id}, menu_id={menu_id}, "
                    f"menu_item_id={menu_item_id} 已存在补录订单 {existing_makeup['id']}"
                )
                raise ValueError(
                    f"该员工在 {serving_date} 已补录过 {menu_item['name']}，请勿重复补录"
                )

            available_stock = menu_item["stock"] - menu_item["sold_count"]
            if available_stock < quantity:
                logger.warning(
                    f"[补录] 库存不足: 菜品={menu_item['name']}, 库存={menu_item['stock']}, "
                    f"已售={menu_item['sold_count']}, 可用={available_stock}, 请求={quantity}"
                )
                raise ValueError(
                    f"库存不足，{menu_item['name']} 剩余 {available_stock} 份，请求 {quantity} 份"
                )

            emp = EmployeeService.get_employee(employee_id, conn)
            total_amount = menu_item["price"] * quantity
            available_balance = emp["balance"] - emp["frozen_balance"]
            if available_balance < total_amount:
                logger.warning(
                    f"[补录] 余额不足: 员工={employee_id}, 可用余额={available_balance}, "
                    f"订单金额={total_amount}"
                )
                raise ValueError(
                    f"余额不足，可用余额 {available_balance} 元，需要 {total_amount} 元"
                )

            order_id = f"ORD{int(datetime.now().timestamp())}{uuid.uuid4().hex[:8].upper()}"
            now = now_str()

            frozen_before = emp["frozen_balance"]
            frozen_after_freeze = frozen_before + total_amount
            balance_before = emp["balance"]
            balance_after_settle = balance_before - total_amount
            frozen_after_settle = frozen_before

            conn.execute(
                "UPDATE employees SET frozen_balance = ?, updated_at = ? WHERE id = ?",
                (frozen_after_freeze, now, employee_id),
            )

            conn.execute(
                "UPDATE menu_items SET sold_count = sold_count + ?, updated_at = ? WHERE id = ?",
                (quantity, now, menu_item_id),
            )

            try:
                conn.execute(
                    """INSERT INTO orders 
                       (id, idempotency_key, employee_id, menu_id, menu_item_id, 
                        item_name, price, quantity, total_amount, status, source, 
                        makeup_remark, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (order_id, idempotency_key, employee_id, menu_id, menu_item_id,
                     menu_item["name"], menu_item["price"], quantity, total_amount,
                     OrderService.ORDER_STATUS_PENDING, source, remark, now, now),
                )
            except sqlite3.IntegrityError as e:
                if "idx_orders_makeup_unique" in str(e):
                    logger.warning(
                        f"[补录] 唯一索引冲突，重复补录: 员工={employee_id}, "
                        f"menu_id={menu_id}, menu_item_id={menu_item_id}"
                    )
                    raise ValueError(
                        f"该员工在 {serving_date} 已补录过 {menu_item['name']}，请勿重复补录"
                    )
                raise

            conn.execute(
                """INSERT INTO transactions
                   (type, employee_id, order_id, amount, balance_before, balance_after,
                    frozen_before, frozen_after, description, idempotency_key, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("FREEZE", employee_id, order_id, total_amount,
                 balance_before, balance_before,
                 frozen_before, frozen_after_freeze,
                 f"补录冻结: {menu_item['name']} x{quantity} (来源: {source})",
                 idempotency_key, now),
            )

            conn.execute(
                "UPDATE employees SET balance = ?, frozen_balance = ?, updated_at = ? WHERE id = ?",
                (balance_after_settle, frozen_after_settle, now, employee_id),
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
                ("SETTLE", employee_id, order_id, total_amount,
                 balance_before, balance_after_settle,
                 frozen_after_freeze, frozen_after_settle,
                 f"补录结算: {menu_item['name']} x{quantity} (来源: {source}, 备注: {remark})",
                 now),
            )

            conn.execute(
                """INSERT INTO makeup_operation_log
                   (order_id, operation_type, operator, remark, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (order_id, "create", source, remark, now),
            )

            logger.info(
                f"[补录] 成功: 订单={order_id}, 员工={employee_id}, 菜品={menu_item['name']}, "
                f"数量={quantity}, 金额={total_amount}, 来源={source}"
            )

            row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            return dict(row)

    @staticmethod
    def query_makeup_orders(
        employee_id: str = None,
        serving_date: str = None,
        source: str = None,
        operation_time_start: str = None,
        operation_time_end: str = None,
        page: int = 1,
        page_size: int = 20,
    ):
        conn = get_db()

        query = """
            SELECT o.id, o.employee_id, o.menu_id, o.menu_item_id,
                   o.item_name, o.price, o.quantity, o.total_amount,
                   o.status, o.source, o.makeup_remark, o.revoked_at,
                   o.created_at AS order_created_at, o.updated_at AS order_updated_at,
                   m.serving_date, mi.name AS dish_name, mi.price AS dish_price,
                   e.name AS employee_name
            FROM orders o
            JOIN menus m ON o.menu_id = m.id
            JOIN menu_items mi ON o.menu_item_id = mi.id
            JOIN employees e ON o.employee_id = e.id
            WHERE o.source <> 'normal'
        """
        params = []

        if employee_id:
            query += " AND o.employee_id = ?"
            params.append(employee_id)
        if serving_date:
            query += " AND m.serving_date = ?"
            params.append(serving_date)
        if source:
            query += " AND o.source = ?"
            params.append(source)
        if operation_time_start:
            query += " AND o.created_at >= ?"
            params.append(operation_time_start)
        if operation_time_end:
            query += " AND o.created_at <= ?"
            params.append(operation_time_end)

        count_query = f"SELECT COUNT(*) as cnt FROM ({query})"
        total = conn.execute(count_query, params).fetchone()["cnt"]

        query += " ORDER BY o.created_at DESC LIMIT ? OFFSET ?"
        offset = (page - 1) * page_size
        params.extend([page_size, offset])

        rows = conn.execute(query, params).fetchall()

        items = []
        for row in rows:
            order_dict = dict(row)
            order_id = order_dict["id"]

            txns = conn.execute(
                """SELECT type, amount, balance_before, balance_after,
                          frozen_before, frozen_after, description, created_at
                   FROM transactions WHERE order_id = ?
                   ORDER BY id""",
                (order_id,),
            ).fetchall()
            order_dict["transactions"] = [dict(t) for t in txns]

            logs = conn.execute(
                """SELECT operation_type, operator, remark, created_at
                   FROM makeup_operation_log WHERE order_id = ?
                   ORDER BY id""",
                (order_id,),
            ).fetchall()
            order_dict["operation_logs"] = [dict(l) for l in logs]

            items.append(order_dict)

        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": items,
        }

    @staticmethod
    def revoke_makeup_order(order_id: str, remark: str = None):
        makeup_cfg = ConfigService.get_makeup_config()
        allow_revoke = makeup_cfg.get("allow_revoke", True)
        if not allow_revoke:
            raise ValueError("当前配置不允许撤销补录")

        with transaction() as conn:
            order = conn.execute(
                "SELECT * FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if not order:
                raise ValueError(f"订单 {order_id} 不存在")

            if order["source"] == "normal":
                raise ValueError("只能撤销补录生成的订单，该订单非补录来源")

            if order["revoked_at"] is not None:
                raise ValueError(f"订单 {order_id} 已被撤销，请勿重复操作")

            if order["status"] == "cancelled":
                raise ValueError(f"订单 {order_id} 已处于取消状态，无法撤销补录")

            if order["status"] != "taken":
                raise ValueError(
                    f"只能撤销已取餐状态的补录订单，当前状态为 {order['status']}"
                )

            revoke_deadline_hours = makeup_cfg.get("revoke_deadline_hours", 24)
            if revoke_deadline_hours > 0:
                order_created = datetime.strptime(order["created_at"], "%Y-%m-%d %H:%M:%S")
                hours_elapsed = (datetime.now() - order_created).total_seconds() / 3600
                if hours_elapsed > revoke_deadline_hours:
                    raise ValueError(
                        f"补录已超过 {revoke_deadline_hours} 小时，不允许撤销"
                    )

            emp = EmployeeService.get_employee(order["employee_id"], conn)
            now = now_str()

            balance_before = emp["balance"]
            balance_after = balance_before + order["total_amount"]
            frozen_before = emp["frozen_balance"]
            frozen_after = frozen_before

            conn.execute(
                "UPDATE employees SET balance = ?, updated_at = ? WHERE id = ?",
                (balance_after, now, order["employee_id"]),
            )

            conn.execute(
                "UPDATE menu_items SET sold_count = sold_count - ?, updated_at = ? WHERE id = ?",
                (order["quantity"], now, order["menu_item_id"]),
            )

            conn.execute(
                "UPDATE orders SET status = ?, revoked_at = ?, updated_at = ? WHERE id = ?",
                (OrderService.ORDER_STATUS_CANCELLED, now, now, order_id),
            )

            conn.execute(
                """INSERT INTO transactions
                   (type, employee_id, order_id, amount, balance_before, balance_after,
                    frozen_before, frozen_after, description, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("MAKEUP_REVOKE", order["employee_id"], order_id, order["total_amount"],
                 balance_before, balance_after,
                 frozen_before, frozen_after,
                 f"撤销补录退款: {order['item_name']} x{order['quantity']}" + (f" ({remark})" if remark else ""),
                 now),
            )

            conn.execute(
                """INSERT INTO makeup_operation_log
                   (order_id, operation_type, operator, remark, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (order_id, "revoke", "admin", remark, now),
            )

            logger.info(
                f"[补录撤销] 成功: 订单={order_id}, 员工={order['employee_id']}, "
                f"退款={order['total_amount']}, 备注={remark}"
            )

            row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            return dict(row)
