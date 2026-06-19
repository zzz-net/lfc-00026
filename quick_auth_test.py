
import requests
import json

BASE_URL = "http://127.0.0.1:18999"

def print_result(name, status_code, response_text):
    try:
        data = json.loads(response_text)
        print(f"  {name}: {status_code} - {json.dumps(data, ensure_ascii=False)}")
    except:
        print(f"  {name}: {status_code} - {response_text[:100]}")

print("=" * 60)
print("测试 1: 匿名访问（无 X-User-Id 头）")
print("=" * 60)

endpoints = [
    ("导出JSON", "GET", "/api/admin/source-rules/export/json"),
    ("导出CSV", "GET", "/api/admin/source-rules/export/csv"),
    ("导入历史", "GET", "/api/admin/source-rules/import-history"),
    ("审计日志", "GET", "/api/admin/source-rules/audit-log"),
    ("作业列表", "GET", "/api/admin/import-replay/jobs"),
]

for name, method, path in endpoints:
    if method == "GET":
        r = requests.get(BASE_URL + path)
    else:
        r = requests.post(BASE_URL + path)
    print_result(name, r.status_code, r.text)

print()
print("=" * 60)
print("测试 2: 无权限用户访问（有 X-User-Id 但无权限）")
print("=" * 60)

headers_no_perm = {"X-User-Id": "noperm_user"}

for name, method, path in endpoints:
    if method == "GET":
        r = requests.get(BASE_URL + path, headers=headers_no_perm)
    else:
        r = requests.post(BASE_URL + path, headers=headers_no_perm)
    print_result(name, r.status_code, r.text)

print()
print("=" * 60)
print("测试 3: 默认管理员 admin 访问")
print("=" * 60)

headers_admin = {"X-User-Id": "admin"}

for name, method, path in endpoints:
    if method == "GET":
        r = requests.get(BASE_URL + path, headers=headers_admin)
    else:
        r = requests.post(BASE_URL + path, headers=headers_admin)
    print_result(name, r.status_code, r.text[:200] if len(r.text) > 200 else r.text)

print()
print("=" * 60)
print("测试 4: 验证导入接口权限（匿名 vs 授权）")
print("=" * 60)

test_rules = [
    {"rule_code": "TEST001", "rule_name": "测试规则1", "rule_type": "food", "content": "测试内容"},
]

print("  匿名导入:")
r = requests.post(BASE_URL + "/api/admin/source-rules/import", 
                  json={"rules": test_rules, "source": "test"})
print_result("导入", r.status_code, r.text)

print("  无权限用户导入:")
r = requests.post(BASE_URL + "/api/admin/source-rules/import", 
                  json={"rules": test_rules, "source": "test"},
                  headers=headers_no_perm)
print_result("导入", r.status_code, r.text)

print("  管理员导入:")
r = requests.post(BASE_URL + "/api/admin/source-rules/import", 
                  json={"rules": test_rules, "source": "test"},
                  headers=headers_admin)
print_result("导入", r.status_code, r.text[:300] if len(r.text) > 300 else r.text)

print()
print("=" * 60)
print("总结:")
print("  匿名请求应返回 401 UNAUTHORIZED")
print("  无权限请求应返回 403 PERMISSION_DENIED")
print("  授权请求应返回 200 OK")
print("=" * 60)
