import requests
import json

BASE_URL = "http://127.0.0.1:8003"

menus = [
    {
        "name": "测试周一午餐",
        "serving_date": "2099-01-01",
        "deadline": "2099-01-01 09:00:00",
        "is_published": False,
        "items": [
            {"name": "红烧肉", "price": 18.0, "stock": 50},
            {"name": "米饭", "price": 2.0, "stock": 200}
        ]
    }
]

print("=== 测试 JSON 导入 ===")
r = requests.post(f"{BASE_URL}/api/admin/menus/import/json?conflict_strategy=skip", json=menus)
print(f"Status: {r.status_code}")
print(f"Response: {json.dumps(r.json(), ensure_ascii=False, indent=2)}")
print()

print("=== 测试 JSON 导出 ===")
r = requests.get(f"{BASE_URL}/api/admin/menus/export/json", params={"start_date": "2099-01-01", "end_date": "2099-12-31"})
print(f"Status: {r.status_code}")
data = r.json()
print(f"导出菜单数: {len(data)}")
if data:
    m = data[0]
    print(f"第一个菜单: {m['name']}, 日期: {m['serving_date']}, 发布状态: {m['is_published']}")
    print(f"菜品数: {len(m['items'])}")
    for item in m['items']:
        print(f"  - {item['name']}: price={item['price']}, stock={item['stock']}")
print()

print("=== 测试字段错误校验 ===")
bad_menus = [
    {
        "name": "",
        "serving_date": "bad-date",
        "deadline": "2099-01-15",
        "items": [
            {"name": "", "price": "abc", "stock": -5},
        ],
    },
]
r = requests.post(f"{BASE_URL}/api/admin/menus/import/json?conflict_strategy=skip", json=bad_menus)
print(f"Status: {r.status_code}")
print(f"Errors: {json.dumps(r.json(), ensure_ascii=False, indent=2)}")
print()

print("=== 测试同日期冲突（skip 策略） ===")
r = requests.post(f"{BASE_URL}/api/admin/menus/import/json?conflict_strategy=skip", json=menus)
print(f"Status: {r.status_code}")
result = r.json()
print(f"总计={result['total']}, 新建={result['created']}, 跳过={result['skipped']}")
print(f"冲突数: {len(result['conflicts'])}")
for c in result['conflicts']:
    print(f"  - {c['serving_date']}: {c['existing_menu_name']} (已发布={c['existing_is_published']})")
print()

print("=== 测试 CSV 导出 ===")
r = requests.get(f"{BASE_URL}/api/admin/menus/export/csv", params={"start_date": "2099-01-01", "end_date": "2099-12-31"})
print(f"Status: {r.status_code}")
lines = r.text.strip().split("\n")
print(f"CSV 行数: {len(lines)}")
print(f"表头: {lines[0]}")
if len(lines) > 1:
    print(f"第一行数据: {lines[1]}")
print()

print("=== 检查服务端日志输出 ===")
print("请查看终端 4 的服务日志，应该包含：")
print("  - [JSON导入] 开始处理/完成")
print("  - [冲突检测] 发现日期冲突")
print("  - [新建]/[跳过] 等操作日志")
print("  - [JSON导出] 开始/完成")
