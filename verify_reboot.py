import requests
import json

BASE_URL = "http://127.0.0.1:8000"


def api(method, path, **kwargs):
    return requests.request(method, f"{BASE_URL}{path}", **kwargs)


def main():
    print("=" * 60)
    print("  服务重启后数据一致性验证")
    print("=" * 60)
    print()

    print("1. 对账检查:")
    r = api("GET", "/api/admin/reconciliation")
    data = r.json()
    status = "[PASS] 一致" if data["consistent"] else "[FAIL] 不一致"
    print(f"   状态: {status}")
    if data["issues"]:
        for issue in data["issues"]:
            print(f"   - {issue}")
    print()

    print("2. 员工余额:")
    r = api("GET", "/api/admin/employees")
    for emp in r.json():
        print(f"   {emp['id']} {emp['name']}: 余额={emp['balance']}, 冻结={emp['frozen_balance']}")
    print()

    print("3. 订单状态:")
    r = api("GET", "/api/orders")
    orders = r.json()
    print(f"   总订单数: {len(orders)}")
    for order in orders:
        print(f"   {order['id']}: {order['status']}, 金额={order['total_amount']}, 员工={order['employee_id']}")
    print()

    print("4. 库存检查:")
    r = api("GET", "/api/admin/menus/1")
    menu = r.json()
    for item in menu["items"]:
        available = item["stock"] - item["sold_count"]
        print(f"   {item['name']}: 库存={item['stock']}, 已售={item['sold_count']}, 可售={available}")
    print()

    print("5. 流水记录:")
    r = api("GET", "/api/transactions", params={"limit": 1})
    data = r.json()
    print(f"   总流水数: {data['total']}")
    print()

    print("6. 验证取餐订单的扣减是否正确:")
    r = api("GET", "/api/orders", params={"status": "taken"})
    taken_orders = r.json()
    if taken_orders:
        order = taken_orders[0]
        emp_id = order["employee_id"]
        r = api("GET", f"/api/admin/employees/{emp_id}")
        emp = r.json()
        print(f"   订单 {order['id']} 金额={order['total_amount']}")
        print(f"   员工 {emp_id} 余额={emp['balance']}, 冻结={emp['frozen_balance']}")
        print(f"   (初始100 - 消费36 = 64) 实际余额: {emp['balance']}")
    print()

    print("7. 验证取消订单的库存和冻结是否恢复:")
    r = api("GET", "/api/orders", params={"status": "cancelled"})
    cancelled_orders = r.json()
    if cancelled_orders:
        order = cancelled_orders[0]
        emp_id = order["employee_id"]
        r = api("GET", f"/api/admin/employees/{emp_id}")
        emp = r.json()
        print(f"   订单 {order['id']} 金额={order['total_amount']} (已取消)")
        print(f"   员工 {emp_id} 余额={emp['balance']}, 冻结={emp['frozen_balance']}")
        print(f"   (初始200, 取消后余额和冻结都应为200/0)")
    print()

    print("=" * 60)
    print("  验证完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
