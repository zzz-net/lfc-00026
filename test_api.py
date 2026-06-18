import requests
import json
import time

BASE_URL = "http://127.0.0.1:8000"


def print_section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}\n")


def print_response(resp, show_data=True):
    print(f"  状态码: {resp.status_code}")
    if show_data:
        try:
            data = resp.json()
            print(f"  响应: {json.dumps(data, ensure_ascii=False, indent=2)}")
        except Exception:
            print(f"  响应: {resp.text[:200]}")
    return resp


def api(method, path, **kwargs):
    url = f"{BASE_URL}{path}"
    resp = requests.request(method, url, **kwargs)
    return resp


def main():
    print_section("0. 健康检查")
    r = api("GET", "/api/health")
    print_response(r)

    print_section("1. 初始化示例数据")
    r = api("POST", "/api/admin/init-sample")
    print_response(r)

    print_section("2. 管理员 - 查看员工列表")
    r = api("GET", "/api/admin/employees")
    print_response(r)

    print_section("3. 管理员 - 查看菜单列表")
    r = api("GET", "/api/admin/menus")
    menus = r.json()
    print_response(r)
    menu_id = menus[0]["id"] if menus else None
    print(f"  菜单ID: {menu_id}")

    print_section("4. 管理员 - 查看菜单详情（含菜品）")
    if menu_id:
        r = api("GET", f"/api/admin/menus/{menu_id}")
        menu_detail = r.json()
        print_response(r)
        items = menu_detail.get("items", [])
        if items:
            item_id = items[0]["id"]
            print(f"  第一个菜品ID: {item_id}")

    print_section("5. 员工端 - 查看已发布菜单")
    r = api("GET", "/api/menus")
    print_response(r)

    print_section("6. 员工端 - 员工张三下单（红烧肉 x 2）")
    if items:
        item_id = items[0]["id"]
        r = api(
            "POST",
            "/api/orders",
            json={
                "employee_id": "EMP001",
                "menu_item_id": item_id,
                "quantity": 2,
            },
            headers={"X-Idempotency-Key": "order-test-001"},
        )
        order_data = r.json()
        print_response(r)
        order_id = order_data.get("id")
        print(f"  订单ID: {order_id}")

    print_section("7. 验证幂等性 - 同一幂等键重复下单")
    if items and order_id:
        r = api(
            "POST",
            "/api/orders",
            json={
                "employee_id": "EMP001",
                "menu_item_id": item_id,
                "quantity": 2,
            },
            headers={"X-Idempotency-Key": "order-test-001"},
        )
        print_response(r)
        print(f"  (返回的订单ID应该与上一次相同，验证幂等性)")

    print_section("8. 查看张三账户余额（应有冻结金额）")
    r = api("GET", "/api/admin/employees/EMP001")
    print_response(r)

    print_section("9. 员工李四下单（余额不足测试）")
    if items:
        expensive_item = max(items, key=lambda x: x["price"])
        r = api(
            "POST",
            "/api/orders",
            json={
                "employee_id": "EMP002",
                "menu_item_id": expensive_item["id"],
                "quantity": 5,
            },
            headers={"X-Idempotency-Key": "order-test-002"},
        )
        print_response(r)

    print_section("10. 取餐结算")
    if order_id:
        r = api("POST", f"/api/orders/{order_id}/take")
        print_response(r)

    print_section("11. 取餐后查看张三余额")
    r = api("GET", "/api/admin/employees/EMP001")
    print_response(r)

    print_section("12. 已取餐订单尝试取消（应该失败）")
    if order_id:
        r = api("POST", f"/api/orders/{order_id}/cancel")
        print_response(r)

    print_section("13. 王五下单，然后取消")
    if items:
        item_id = items[2]["id"]  # 番茄炒蛋
        r = api(
            "POST",
            "/api/orders",
            json={
                "employee_id": "EMP003",
                "menu_item_id": item_id,
                "quantity": 3,
            },
            headers={"X-Idempotency-Key": "order-test-003"},
        )
        cancel_order = r.json()
        print_response(r)
        cancel_order_id = cancel_order.get("id")

        print(f"\n  --- 取消订单 ---")
        if cancel_order_id:
            r = api("POST", f"/api/orders/{cancel_order_id}/cancel")
            print_response(r)

    print_section("14. 取消后查看王五余额（冻结金额应释放）")
    r = api("GET", "/api/admin/employees/EMP003")
    print_response(r)

    print_section("15. 查看流水记录")
    r = api("GET", "/api/transactions", params={"limit": 10})
    print_response(r)

    print_section("16. 查看张三的流水")
    r = api("GET", "/api/transactions", params={"employee_id": "EMP001"})
    print_response(r)

    print_section("17. 数据一致性对账检查")
    r = api("GET", "/api/admin/reconciliation")
    print_response(r)

    print_section("18. 导出流水CSV")
    r = api("GET", "/api/transactions/export")
    print(f"  状态码: {r.status_code}")
    print(f"  Content-Type: {r.headers.get('Content-Type')}")
    print(f"  CSV内容前500字符:\n{r.text[:500]}")

    print_section("19. 管理员手动调整余额")
    r = api(
        "POST",
        "/api/admin/employees/EMP002/adjust",
        json={"amount": 100.0, "description": "充值100元"},
    )
    print_response(r)

    print_section("20. 调整后再次对账")
    r = api("GET", "/api/admin/reconciliation")
    result = r.json()
    print_response(r)
    status_str = "[PASS] 一致" if result["consistent"] else "[FAIL] 不一致"
    print(f"\n  对账结果: {status_str}")
    if result["issues"]:
        for issue in result["issues"]:
            print(f"    - {issue}")

    print("\n" + "="*60)
    print("  所有测试完成！")
    print("="*60)


if __name__ == "__main__":
    main()
