import requests
import json
import time
from datetime import datetime

BASE_URL = "http://127.0.0.1:8009"
headers = {}

today = datetime.now().strftime("%Y-%m-%d")
r = requests.post(f"{BASE_URL}/api/admin/menus", json={
    "name": f"测试菜单_{today}",
    "serving_date": today,
    "deadline": f"{today} 23:59:59",
    "is_published": True
}, headers=headers)
print(f"创建菜单: {r.status_code}")
menu_id = r.json()["id"]

r = requests.post(f"{BASE_URL}/api/admin/menus/{menu_id}/items", json={
    "name": "宫保鸡丁",
    "price": 20,
    "stock": 100
}, headers=headers)
print(f"添加菜品1: {r.status_code}")
item1_id = r.json()["id"]

r = requests.post(f"{BASE_URL}/api/admin/employees", json={
    "id": "TEST001",
    "name": "测试员工",
    "department": "测试部门",
    "balance": 1000
}, headers=headers)
print(f"创建员工: {r.status_code}")

print("\n--- 测试来源规则接口 ---")
r = requests.get(f"{BASE_URL}/api/admin/source-rules", headers=headers, timeout=10)
print(f"获取规则: {r.status_code}, 数量={len(r.json())}")

r = requests.post(f"{BASE_URL}/api/admin/source-rules", json={
    "code": "test_source",
    "name": "测试来源",
    "priority": 50,
    "is_enabled": True
}, headers=headers, timeout=10)
print(f"创建规则: {r.status_code}")

print("\n--- 测试补录接口 ---")
start = time.time()
r = requests.post(f"{BASE_URL}/api/admin/orders/makeup", json={
    "employee_id": "TEST001",
    "menu_item_id": item1_id,
    "quantity": 1,
    "serving_date": today,
    "source": "test_source",
    "remark": "测试补录"
}, headers=headers, timeout=30)
end = time.time()
print(f"补录状态: {r.status_code}, 耗时: {end - start:.2f}s")
if r.status_code != 200:
    print(f"错误: {r.json()}")
else:
    order = r.json()
    print(f"补录成功，订单ID: {order.get('id')}")
    print(f"包含matched_source_rule: {'matched_source_rule' in order}")
    if "matched_source_rule" in order:
        print(f"匹配规则: {order['matched_source_rule']}")

print("\n--- 测试按来源筛选 ---")
r = requests.get(f"{BASE_URL}/api/admin/orders/makeup", params={"source": "test_source"}, headers=headers, timeout=10)
print(f"筛选状态: {r.status_code}, 总数={r.json().get('total')}")
if r.status_code == 200 and r.json()["items"]:
    item = r.json()["items"][0]
    print(f"查询结果包含matched_source_rule: {'matched_source_rule' in item}")

print("\n--- 测试撤销 ---")
order_id = r.json()["items"][0]["id"]
r = requests.post(f"{BASE_URL}/api/admin/orders/makeup/{order_id}/revoke", json={"remark": "测试撤销"}, headers=headers, timeout=10)
print(f"撤销状态: {r.status_code}")
if r.status_code == 200:
    revoked = r.json()
    print(f"撤销结果包含matched_source_rule: {'matched_source_rule' in revoked}")

print("\n--- 测试完成 ---")
