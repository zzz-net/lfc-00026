import csv
import io
import json
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

from database import get_db, transaction, now_str
from services.menu_service import MenuService


class MenuImportExportService:
    REQUIRED_MENU_FIELDS = {"serving_date", "name", "deadline"}
    REQUIRED_ITEM_FIELDS = {"name", "price", "stock"}

    @staticmethod
    def _validate_menu_row(menu_data: Dict[str, Any], row_num: int = None) -> List[str]:
        errors = []
        prefix = f"第{row_num}行: " if row_num is not None else ""

        for field in MenuImportExportService.REQUIRED_MENU_FIELDS:
            if field not in menu_data or menu_data[field] is None or str(menu_data[field]).strip() == "":
                errors.append(f"{prefix}菜单字段 '{field}' 不能为空")

        if "serving_date" in menu_data and menu_data["serving_date"]:
            try:
                datetime.strptime(str(menu_data["serving_date"]).strip(), "%Y-%m-%d")
            except ValueError:
                errors.append(f"{prefix}供餐日期格式错误，应为 YYYY-MM-DD，实际值: {menu_data['serving_date']}")

        if "deadline" in menu_data and menu_data["deadline"]:
            try:
                datetime.strptime(str(menu_data["deadline"]).strip(), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                errors.append(f"{prefix}截止时间格式错误，应为 YYYY-MM-DD HH:MM:SS，实际值: {menu_data['deadline']}")

        return errors

    @staticmethod
    def _validate_item_row(item_data: Dict[str, Any], row_num: int = None, item_idx: int = None) -> List[str]:
        errors = []
        if row_num is not None:
            prefix = f"第{row_num}行: "
        elif item_idx is not None:
            prefix = f"第{1 + item_idx}个菜品: "
        else:
            prefix = ""

        for field in MenuImportExportService.REQUIRED_ITEM_FIELDS:
            if field not in item_data or item_data[field] is None or str(item_data[field]).strip() == "":
                errors.append(f"{prefix}菜品字段 '{field}' 不能为空")

        if "price" in item_data and item_data["price"] is not None and str(item_data["price"]).strip() != "":
            try:
                price = float(item_data["price"])
                if price < 0:
                    errors.append(f"{prefix}价格不能为负，实际值: {item_data['price']}")
            except (ValueError, TypeError):
                errors.append(f"{prefix}价格格式错误，应为数字，实际值: {item_data['price']}")

        if "stock" in item_data and item_data["stock"] is not None and str(item_data["stock"]).strip() != "":
            try:
                stock = int(item_data["stock"])
                if stock < 0:
                    errors.append(f"{prefix}库存不能为负，实际值: {item_data['stock']}")
            except (ValueError, TypeError):
                errors.append(f"{prefix}库存格式错误，应为整数，实际值: {item_data['stock']}")

        return errors

    @staticmethod
    def _parse_json_data(data: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
        errors = []
        menus = []

        if not isinstance(data, list):
            errors.append("JSON 根节点必须是菜单数组")
            return menus, errors

        for menu_idx, menu_raw in enumerate(data):
            row_num = menu_idx + 1
            if not isinstance(menu_raw, dict):
                errors.append(f"第{row_num}个菜单: 格式错误，应为对象")
                continue

            menu_errors = MenuImportExportService._validate_menu_row(menu_raw, row_num=None)
            menu_has_error = len(menu_errors) > 0
            if menu_errors:
                prefixed = [f"第{row_num}个菜单: {e}" for e in menu_errors]
                errors.extend(prefixed)

            items_raw = menu_raw.get("items", [])
            if not isinstance(items_raw, list):
                errors.append(f"第{row_num}个菜单: 'items' 必须是数组")
                continue

            if len(items_raw) == 0:
                if not menu_has_error:
                    errors.append(f"第{row_num}个菜单: 至少需要一个菜品")

            menu_items = []
            has_item_error = False
            for item_idx, item_raw in enumerate(items_raw):
                if not isinstance(item_raw, dict):
                    errors.append(f"第{row_num}个菜单 第{1 + item_idx}个菜品: 格式错误，应为对象")
                    has_item_error = True
                    continue
                item_errors = MenuImportExportService._validate_item_row(item_raw, item_idx=item_idx)
                if item_errors:
                    prefixed = [f"第{row_num}个菜单 {e}" for e in item_errors]
                    errors.extend(prefixed)
                    has_item_error = True
                    continue
                menu_items.append({
                    "name": str(item_raw["name"]).strip(),
                    "price": float(item_raw["price"]),
                    "stock": int(item_raw["stock"]),
                })

            if menu_has_error or has_item_error:
                continue

            menus.append({
                "name": str(menu_raw["name"]).strip(),
                "serving_date": str(menu_raw["serving_date"]).strip(),
                "deadline": str(menu_raw["deadline"]).strip(),
                "is_published": bool(menu_raw.get("is_published", False)),
                "items": menu_items,
            })

        return menus, errors

    @staticmethod
    def _parse_csv_data(csv_content: str) -> Tuple[List[Dict[str, Any]], List[str]]:
        errors = []
        menus_by_date: Dict[str, Dict[str, Any]] = {}
        row_errors_by_date: Dict[str, bool] = {}

        try:
            reader = csv.DictReader(io.StringIO(csv_content))
        except Exception as e:
            errors.append(f"CSV 解析失败: {str(e)}")
            return [], errors

        fieldnames = reader.fieldnames or []
        required_csv_fields = {"serving_date", "menu_name", "deadline", "item_name", "price", "stock"}
        missing = required_csv_fields - set(fieldnames)
        if missing:
            errors.append(f"CSV 缺少必需列: {', '.join(sorted(missing))}")
            return [], errors

        for row_idx, row in enumerate(reader, start=2):
            menu_errors = MenuImportExportService._validate_menu_row({
                "serving_date": row.get("serving_date"),
                "name": row.get("menu_name"),
                "deadline": row.get("deadline"),
            }, row_num=row_idx)
            menu_has_error = len(menu_errors) > 0
            if menu_errors:
                errors.extend(menu_errors)

            item_errors = MenuImportExportService._validate_item_row({
                "name": row.get("item_name"),
                "price": row.get("price"),
                "stock": row.get("stock"),
            }, row_num=row_idx)
            if item_errors:
                errors.extend(item_errors)

            if menu_has_error or item_errors:
                serving_date = str(row.get("serving_date", "")).strip()
                if serving_date:
                    row_errors_by_date[serving_date] = True
                continue

            serving_date = str(row["serving_date"]).strip()
            menu_name = str(row["menu_name"]).strip()
            deadline = str(row["deadline"]).strip()
            is_published = str(row.get("is_published", "")).strip().lower() in ("1", "true", "yes", "是")

            item = {
                "name": str(row["item_name"]).strip(),
                "price": float(row["price"]),
                "stock": int(row["stock"]),
            }

            if serving_date not in menus_by_date:
                menus_by_date[serving_date] = {
                    "name": menu_name,
                    "serving_date": serving_date,
                    "deadline": deadline,
                    "is_published": is_published,
                    "items": [],
                }
            else:
                existing = menus_by_date[serving_date]
                if existing["name"] != menu_name:
                    errors.append(f"第{row_idx}行: 同一日期 {serving_date} 的菜单名称不一致 ('{existing['name']}' vs '{menu_name}')")
                    row_errors_by_date[serving_date] = True
                    continue
                if existing["deadline"] != deadline:
                    errors.append(f"第{row_idx}行: 同一日期 {serving_date} 的截止时间不一致")
                    row_errors_by_date[serving_date] = True
                    continue
                if existing["is_published"] != is_published:
                    errors.append(f"第{row_idx}行: 同一日期 {serving_date} 的发布状态不一致")
                    row_errors_by_date[serving_date] = True
                    continue

            menus_by_date[serving_date]["items"].append(item)

        for date, menu in menus_by_date.items():
            if len(menu["items"]) == 0:
                if not row_errors_by_date.get(date, False):
                    errors.append(f"供餐日期 {date} 的菜单没有菜品")

        valid_menus = {k: v for k, v in menus_by_date.items() if not row_errors_by_date.get(k, False)}
        menus = sorted(valid_menus.values(), key=lambda m: m["serving_date"])
        return menus, errors

    @staticmethod
    def _detect_conflicts(menus: List[Dict[str, Any]], conn=None) -> List[Dict[str, Any]]:
        if conn is None:
            conn = get_db()

        conflicts = []
        for menu in menus:
            serving_date = menu["serving_date"]
            row = conn.execute(
                "SELECT * FROM menus WHERE serving_date = ?", (serving_date,)
            ).fetchone()
            if row:
                conflicts.append({
                    "serving_date": serving_date,
                    "existing_menu_id": row["id"],
                    "existing_menu_name": row["name"],
                    "existing_is_published": bool(row["is_published"]),
                    "incoming_menu_name": menu["name"],
                    "incoming_is_published": menu["is_published"],
                })
        return conflicts

    @staticmethod
    def import_menus(
        menus: List[Dict[str, Any]],
        conflict_strategy: str = "skip",
    ) -> Dict[str, Any]:
        if conflict_strategy not in ("skip", "update_draft", "report"):
            raise ValueError(f"无效的冲突策略: {conflict_strategy}，可选值: skip, update_draft, report")

        if not menus:
            return {
                "success": True,
                "total": 0,
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": [],
                "conflicts": [],
            }

        with transaction() as conn:
            conflicts = MenuImportExportService._detect_conflicts(menus, conn)

            if conflict_strategy == "report":
                return {
                    "success": True,
                    "total": len(menus),
                    "created": 0,
                    "updated": 0,
                    "skipped": 0,
                    "errors": [],
                    "conflicts": conflicts,
                }

            created = 0
            updated = 0
            skipped = 0
            import_errors = []

            conflict_map = {c["serving_date"]: c for c in conflicts}

            for menu_data in menus:
                serving_date = menu_data["serving_date"]

                if serving_date in conflict_map:
                    conflict = conflict_map[serving_date]

                    if conflict["existing_is_published"]:
                        import_errors.append(
                            f"供餐日期 {serving_date} 的菜单已发布，无法修改"
                        )
                        skipped += 1
                        continue

                    if conflict_strategy == "skip":
                        skipped += 1
                        continue

                    if conflict_strategy == "update_draft":
                        try:
                            menu_id = conflict["existing_menu_id"]
                            now = now_str()
                            conn.execute(
                                "UPDATE menus SET name = ?, deadline = ?, is_published = ?, updated_at = ? WHERE id = ?",
                                (menu_data["name"], menu_data["deadline"],
                                 1 if menu_data["is_published"] else 0, now, menu_id),
                            )
                            conn.execute("DELETE FROM menu_items WHERE menu_id = ?", (menu_id,))
                            for item in menu_data["items"]:
                                conn.execute(
                                    """INSERT INTO menu_items (menu_id, name, price, stock, sold_count, created_at, updated_at)
                                       VALUES (?, ?, ?, ?, 0, ?, ?)""",
                                    (menu_id, item["name"], item["price"], item["stock"], now, now),
                                )
                            updated += 1
                        except Exception as e:
                            import_errors.append(f"更新菜单 {serving_date} 失败: {str(e)}")
                            skipped += 1
                        continue
                else:
                    try:
                        now = now_str()
                        cur = conn.cursor()
                        cur.execute(
                            """INSERT INTO menus (name, serving_date, deadline, is_published, created_at, updated_at)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (menu_data["name"], serving_date, menu_data["deadline"],
                             1 if menu_data["is_published"] else 0, now, now),
                        )
                        menu_id = cur.lastrowid
                        for item in menu_data["items"]:
                            conn.execute(
                                """INSERT INTO menu_items (menu_id, name, price, stock, sold_count, created_at, updated_at)
                                   VALUES (?, ?, ?, ?, 0, ?, ?)""",
                                (menu_id, item["name"], item["price"], item["stock"], now, now),
                            )
                        created += 1
                    except Exception as e:
                        import_errors.append(f"创建菜单 {serving_date} 失败: {str(e)}")
                        skipped += 1

            return {
                "success": True,
                "total": len(menus),
                "created": created,
                "updated": updated,
                "skipped": skipped,
                "errors": import_errors,
                "conflicts": conflicts,
            }

    @staticmethod
    def import_menus_from_json(json_data: Any, conflict_strategy: str = "skip") -> Dict[str, Any]:
        menus, parse_errors = MenuImportExportService._parse_json_data(json_data)
        if parse_errors:
            return {
                "success": False,
                "total": 0,
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": parse_errors,
                "conflicts": [],
            }
        return MenuImportExportService.import_menus(menus, conflict_strategy)

    @staticmethod
    def import_menus_from_csv(csv_content: str, conflict_strategy: str = "skip") -> Dict[str, Any]:
        menus, parse_errors = MenuImportExportService._parse_csv_data(csv_content)
        if parse_errors:
            return {
                "success": False,
                "total": 0,
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": parse_errors,
                "conflicts": [],
            }
        return MenuImportExportService.import_menus(menus, conflict_strategy)

    @staticmethod
    def export_menus_json(
        start_date: str = None,
        end_date: str = None,
    ) -> List[Dict[str, Any]]:
        conn = get_db()
        query = "SELECT * FROM menus WHERE 1=1"
        params = []
        if start_date:
            query += " AND serving_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND serving_date <= ?"
            params.append(end_date)
        query += " ORDER BY serving_date"

        menu_rows = conn.execute(query, params).fetchall()
        result = []
        for m in menu_rows:
            menu = dict(m)
            items = conn.execute(
                "SELECT id, name, price, stock, sold_count, created_at, updated_at FROM menu_items WHERE menu_id = ? ORDER BY id",
                (m["id"],),
            ).fetchall()
            menu["items"] = [dict(i) for i in items]
            menu["is_published"] = bool(menu["is_published"])
            result.append(menu)
        return result

    @staticmethod
    def export_menus_csv(
        start_date: str = None,
        end_date: str = None,
    ) -> str:
        menus = MenuImportExportService.export_menus_json(start_date, end_date)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "serving_date", "menu_name", "deadline", "is_published",
            "item_name", "price", "stock", "sold_count",
        ])
        for menu in menus:
            published_str = "1" if menu["is_published"] else "0"
            for item in menu["items"]:
                writer.writerow([
                    menu["serving_date"],
                    menu["name"],
                    menu["deadline"],
                    published_str,
                    item["name"],
                    item["price"],
                    item["stock"],
                    item["sold_count"],
                ])
        return output.getvalue()
