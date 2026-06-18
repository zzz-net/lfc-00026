from datetime import datetime
from typing import Optional

from database import get_db, transaction, now_str


class MenuService:
    @staticmethod
    def create_menu(name: str, serving_date: str, deadline: str):
        try:
            datetime.strptime(serving_date, "%Y-%m-%d")
        except ValueError:
            raise ValueError("供餐日期格式错误，应为 YYYY-MM-DD")
        try:
            datetime.strptime(deadline, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            raise ValueError("截止时间格式错误，应为 YYYY-MM-DD HH:MM:SS")

        with transaction() as conn:
            cur = conn.cursor()
            now = now_str()
            try:
                cur.execute(
                    """INSERT INTO menus (name, serving_date, deadline, is_published, created_at, updated_at)
                       VALUES (?, ?, ?, 0, ?, ?)""",
                    (name, serving_date, deadline, now, now),
                )
                menu_id = cur.lastrowid
            except Exception:
                raise ValueError(f"供餐日期 {serving_date} 已存在菜单")

            return MenuService.get_menu(menu_id, conn)

    @staticmethod
    def get_menu(menu_id: int, conn=None):
        if conn is None:
            conn = get_db()
        row = conn.execute("SELECT * FROM menus WHERE id = ?", (menu_id,)).fetchone()
        if not row:
            raise ValueError(f"菜单 {menu_id} 不存在")
        menu = dict(row)
        items = conn.execute(
            "SELECT * FROM menu_items WHERE menu_id = ? ORDER BY id", (menu_id,)
        ).fetchall()
        menu["items"] = [dict(i) for i in items]
        return menu

    @staticmethod
    def list_menus(only_published: bool = False):
        conn = get_db()
        query = "SELECT * FROM menus"
        params = ()
        if only_published:
            query += " WHERE is_published = 1"
        query += " ORDER BY serving_date DESC"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def add_menu_item(menu_id: int, name: str, price: float, stock: int):
        if price < 0:
            raise ValueError("价格不能为负")
        if stock < 0:
            raise ValueError("库存不能为负")

        with transaction() as conn:
            MenuService.get_menu(menu_id, conn)
            now = now_str()
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO menu_items (menu_id, name, price, stock, sold_count, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 0, ?, ?)""",
                (menu_id, name, price, stock, now, now),
            )
            item_id = cur.lastrowid
            row = conn.execute("SELECT * FROM menu_items WHERE id = ?", (item_id,)).fetchone()
            return dict(row)

    @staticmethod
    def update_menu_item(item_id: int, name: str = None, price: float = None, stock: int = None):
        with transaction() as conn:
            row = conn.execute("SELECT * FROM menu_items WHERE id = ?", (item_id,)).fetchone()
            if not row:
                raise ValueError(f"菜品 {item_id} 不存在")

            updates = []
            params = []
            if name is not None:
                updates.append("name = ?")
                params.append(name)
            if price is not None:
                if price < 0:
                    raise ValueError("价格不能为负")
                updates.append("price = ?")
                params.append(price)
            if stock is not None:
                if stock < 0:
                    raise ValueError("库存不能为负")
                updates.append("stock = ?")
                params.append(stock)

            if updates:
                updates.append("updated_at = ?")
                params.append(now_str())
                params.append(item_id)
                conn.execute(
                    f"UPDATE menu_items SET {', '.join(updates)} WHERE id = ?",
                    params,
                )

            row = conn.execute("SELECT * FROM menu_items WHERE id = ?", (item_id,)).fetchone()
            return dict(row)

    @staticmethod
    def publish_menu(menu_id: int):
        with transaction() as conn:
            menu = MenuService.get_menu(menu_id, conn)
            items = conn.execute(
                "SELECT COUNT(*) as cnt FROM menu_items WHERE menu_id = ?", (menu_id,)
            ).fetchone()
            if items["cnt"] == 0:
                raise ValueError("菜单为空，无法发布")

            now = now_str()
            conn.execute(
                "UPDATE menus SET is_published = 1, updated_at = ? WHERE id = ?",
                (now, menu_id),
            )
            return MenuService.get_menu(menu_id, conn)

    @staticmethod
    def is_order_deadline_passed(menu_id: int, conn=None) -> bool:
        if conn is None:
            conn = get_db()
        row = conn.execute("SELECT deadline FROM menus WHERE id = ?", (menu_id,)).fetchone()
        if not row:
            raise ValueError(f"菜单 {menu_id} 不存在")
        deadline = datetime.strptime(row["deadline"], "%Y-%m-%d %H:%M:%S")
        return datetime.now() > deadline

    @staticmethod
    def get_menu_item(item_id: int, conn=None):
        if conn is None:
            conn = get_db()
        row = conn.execute("SELECT * FROM menu_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise ValueError(f"菜品 {item_id} 不存在")
        return dict(row)
