import requests
import json
import io
import csv
import os
import time

BASE_URL = "http://127.0.0.1:8000"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "canteen.db")


def _unique_offset():
    ts = int(time.time() * 1000) % 100000
    return ts


OFFSET = _unique_offset()


def _date(offset_days):
    base_year = 2090
    total_days = OFFSET + offset_days
    year = base_year + total_days // 365
    day_in_year = total_days % 365
    month = 1 + (day_in_year // 28) % 12
    day = 1 + day_in_year % 28
    return f"{year:04d}-{month:02d}-{day:02d}"


def print_section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


def print_result(label, data):
    print(f"  [{label}]")
    if isinstance(data, dict) and "errors" in data and data["errors"]:
        print(f"    错误数量: {len(data['errors'])}")
        for e in data["errors"][:5]:
            print(f"      - {e}")
        if len(data["errors"]) > 5:
            print(f"      ... 还有 {len(data['errors']) - 5} 条错误")
    if isinstance(data, dict) and "conflicts" in data and data["conflicts"]:
        print(f"    冲突数量: {len(data['conflicts'])}")
        for c in data["conflicts"]:
            status = "已发布" if c["existing_is_published"] else "草稿"
            print(f"      - {c['serving_date']}: {c['existing_menu_name']} ({status})")
    if isinstance(data, dict) and all(k in data for k in ("total", "created", "updated", "skipped")):
        print(f"    总计={data['total']}, 新建={data['created']}, 更新={data['updated']}, 跳过={data['skipped']}")
    print()


def api(method, path, **kwargs):
    return requests.request(method, f"{BASE_URL}{path}", **kwargs)


def build_sample_menus(start_date="2026-07-01", count=3, published=False):
    menus = []
    base_year, base_month, base_day = map(int, start_date.split("-"))
    for i in range(count):
        day = base_day + i
        date_str = f"{base_year:04d}-{base_month:02d}-{day:02d}"
        menus.append({
            "name": f"周{i+1}午餐",
            "serving_date": date_str,
            "deadline": f"{date_str} 09:00:00",
            "is_published": published,
            "items": [
                {"name": f"荤菜{i+1}", "price": 18.0 + i, "stock": 50 + i * 10},
                {"name": f"素菜{i+1}", "price": 8.0 + i, "stock": 80 + i * 10},
                {"name": f"汤品{i+1}", "price": 5.0, "stock": 60},
                {"name": "米饭", "price": 2.0, "stock": 200},
            ],
        })
    return menus


def build_sample_csv(menus):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "serving_date", "menu_name", "deadline", "is_published",
        "item_name", "price", "stock",
    ])
    for menu in menus:
        pub = "1" if menu.get("is_published", False) else "0"
        for item in menu["items"]:
            writer.writerow([
                menu["serving_date"], menu["name"], menu["deadline"], pub,
                item["name"], item["price"], item["stock"],
            ])
    return output.getvalue()


def check_json_import_success():
    print_section("测试 1: JSON 格式成功导入")
    start = _date(0)
    menus = build_sample_menus(start, 3, published=False)
    r = api("POST", "/api/admin/menus/import/json?conflict_strategy=skip", json=menus)
    data = r.json()
    print_result("JSON导入结果", data)
    assert r.status_code == 200, f"期望200，实际{r.status_code}"
    assert data["created"] == 3, f"期望新建3个，实际{data['created']}"
    assert data["total"] == 3
    print(f"  起始日期: {start}")
    print("  [PASS] JSON 导入成功")


def check_csv_import_success():
    print_section("测试 2: CSV 格式成功导入")
    start = _date(10)
    menus = build_sample_menus(start, 2, published=False)
    csv_content = build_sample_csv(menus)
    files = {"file": ("test_menus.csv", csv_content, "text/csv")}
    r = api("POST", "/api/admin/menus/import/csv?conflict_strategy=skip", files=files)
    data = r.json()
    print_result("CSV导入结果", data)
    assert r.status_code == 200, f"期望200，实际{r.status_code}"
    assert data["created"] == 2, f"期望新建2个，实际{data['created']}"
    print(f"  起始日期: {start}")
    print("  [PASS] CSV 导入成功")


def check_field_validation_errors():
    print_section("测试 3: 字段错误校验（带行号和原因）")
    bad_menus = [
        {
            "name": "",
            "serving_date": "bad-date",
            "deadline": "2026-07-15",
            "items": [
                {"name": "", "price": "abc", "stock": -5},
            ],
        },
    ]
    r = api("POST", "/api/admin/menus/import/json?conflict_strategy=skip", json=bad_menus)
    data = r.json()
    print_result("字段校验结果", data)
    assert r.status_code == 400, f"期望400，实际{r.status_code}"
    assert data["detail"]["code"] == "IMPORT_VALIDATION_ERROR"
    errors = data["detail"]["message"] if "message" in data["detail"] else ""
    error_list = data["detail"].get("errors", []) if "detail" in data else data.get("errors", [])
    assert len(error_list) > 0, "应该有错误信息"
    print(f"  错误条数: {len(error_list)}")
    for e in error_list:
        print(f"    - {e}")
    print("  [PASS] 字段校验正确，返回了详细错误")


def check_conflict_skip_strategy():
    print_section("测试 4: 同日期冲突 - skip 策略")
    start = _date(0)
    menus = build_sample_menus(start, 3, published=False)
    r = api("POST", "/api/admin/menus/import/json?conflict_strategy=skip", json=menus)
    data = r.json()
    print_result("skip策略结果", data)
    assert r.status_code == 200
    assert data["created"] == 0, f"已有菜单应跳过，期望新建0，实际{data['created']}"
    assert data["skipped"] == 3, f"期望跳过3，实际{data['skipped']}"
    assert len(data["conflicts"]) == 3, f"期望3个冲突，实际{len(data['conflicts'])}"
    print("  [PASS] skip 策略正确")


def check_conflict_report_strategy():
    print_section("测试 5: 同日期冲突 - report 策略（仅报告，不修改）")
    start = _date(0)
    menus = build_sample_menus(start, 3, published=False)
    for m in menus:
        m["name"] = "修改后的名称"
    r = api("POST", "/api/admin/menus/import/json?conflict_strategy=report", json=menus)
    data = r.json()
    print_result("report策略结果", data)
    assert r.status_code == 200
    assert data["created"] == 0
    assert data["updated"] == 0
    assert len(data["conflicts"]) == 3
    first_date = _date(0)
    r2 = api("GET", "/api/admin/menus")
    menus_list = r2.json()
    names = [m["name"] for m in menus_list if m["serving_date"] == first_date]
    assert "修改后的名称" not in names, "report策略不应修改数据"
    print("  [PASS] report 策略正确，仅报告不修改")


def check_conflict_update_draft_strategy():
    print_section("测试 6: 同日期冲突 - update_draft 策略（更新草稿菜单）")
    start = _date(10)
    menus = build_sample_menus(start, 2, published=False)
    for m in menus:
        m["name"] = "更新版-" + m["name"]
        m["items"].append({"name": "新加菜", "price": 15.0, "stock": 30})
    r = api("POST", "/api/admin/menus/import/json?conflict_strategy=update_draft", json=menus)
    data = r.json()
    print_result("update_draft策略结果", data)
    assert r.status_code == 200
    assert data["updated"] == 2, f"期望更新2个，实际{data['updated']}"
    assert data["created"] == 0
    first_date = _date(10)
    r2 = api("GET", "/api/admin/menus")
    menus_list = r2.json()
    target = [m for m in menus_list if m["serving_date"] == first_date]
    assert len(target) == 1
    assert target[0]["name"].startswith("更新版-"), "菜单名称应已更新"
    print("  [PASS] update_draft 策略正确，草稿菜单已更新")


def check_published_menu_protection():
    print_section("测试 7: 已发布菜单不能被修改")
    start = _date(20)
    menus = build_sample_menus(start, 1, published=True)
    api("POST", "/api/admin/menus/import/json?conflict_strategy=skip", json=menus)
    menus[0]["name"] = "尝试修改已发布菜单"
    menus[0]["is_published"] = True
    r = api("POST", "/api/admin/menus/import/json?conflict_strategy=update_draft", json=menus)
    data = r.json()
    print_result("修改已发布菜单结果", data)
    assert data["updated"] == 0, "已发布菜单不应被更新"
    assert data["skipped"] == 1
    assert any("已发布" in e for e in data["errors"]), "错误信息应说明已发布"
    target_date = _date(20)
    r2 = api("GET", "/api/admin/menus")
    menus_list = r2.json()
    target = [m for m in menus_list if m["serving_date"] == target_date]
    assert len(target) == 1
    assert not target[0]["name"].startswith("尝试修改"), "已发布菜单名称不应被修改"
    print("  [PASS] 已发布菜单保护正确，无法被静默修改")


def check_export_json():
    print_section("测试 8: JSON 格式导出")
    r = api("GET", "/api/admin/menus/export/json", params={"start_date": "2026-07-01", "end_date": "2026-07-05"})
    data = r.json()
    print(f"  导出菜单数量: {len(data)}")
    if data:
        print(f"  第一个菜单: {data[0]['name']}, 日期: {data[0]['serving_date']}")
        print(f"  菜品数量: {len(data[0]['items'])}")
        print(f"  发布状态: {data[0]['is_published']}")
    assert r.status_code == 200
    assert isinstance(data, list)
    assert len(data) > 0
    assert "items" in data[0]
    print("  [PASS] JSON 导出成功")


def check_export_csv():
    print_section("测试 9: CSV 格式导出")
    r = api("GET", "/api/admin/menus/export/csv", params={"start_date": "2026-07-01", "end_date": "2026-07-05"})
    content = r.text
    lines = content.strip().split("\n")
    print(f"  CSV 行数: {len(lines)} (含表头)")
    print(f"  表头: {lines[0]}")
    if len(lines) > 1:
        print(f"  第一行数据: {lines[1][:80]}...")
    assert r.status_code == 200
    assert "serving_date" in lines[0]
    assert "item_name" in lines[0]
    assert len(lines) > 1
    print("  [PASS] CSV 导出成功")


def check_export_import_roundtrip():
    print_section("测试 10: 导出后再导入一致性（往返验证）")
    src_start = _date(0)
    src_end = _date(2)
    r = api("GET", "/api/admin/menus/export/json",
            params={"start_date": src_start, "end_date": src_end})
    exported = r.json()
    print(f"  导出 {len(exported)} 个菜单")
    assert len(exported) == 3

    original_items_count = sum(len(m["items"]) for m in exported)
    print(f"  原始菜品总数: {original_items_count}")

    dst_start = _date(30)
    reimport_menus = []
    for i, m in enumerate(exported):
        new_date = _date(30 + i)
        reimport_menus.append({
            "name": m["name"],
            "serving_date": new_date,
            "deadline": m["deadline"],
            "is_published": m["is_published"],
            "items": [
                {"name": it["name"], "price": it["price"], "stock": it["stock"]}
                for it in m["items"]
            ],
        })

    print(f"  转换日期后重新导入（模拟新环境恢复）...")
    r2 = api("POST", "/api/admin/menus/import/json?conflict_strategy=skip",
             json=reimport_menus)
    import_result = r2.json()
    print(f"  导入结果: 新建={import_result['created']}, 跳过={import_result['skipped']}")
    assert import_result["created"] == 3

    dst_start = _date(30)
    dst_end = _date(32)
    r3 = api("GET", "/api/admin/menus/export/json",
            params={"start_date": dst_start, "end_date": dst_end})
    reimported = r3.json()
    reimported_items_count = sum(len(m["items"]) for m in reimported)
    print(f"  重新导出验证: {len(reimported)} 个菜单, {reimported_items_count} 个菜品")

    assert len(reimported) == len(exported)
    assert reimported_items_count == original_items_count

    for i, (orig, reim) in enumerate(zip(exported, reimported)):
        assert orig["name"] == reim["name"]
        assert orig["is_published"] == reim["is_published"]
        assert len(orig["items"]) == len(reim["items"])
        for j, (orig_item, reim_item) in enumerate(zip(orig["items"], reim["items"])):
            assert orig_item["name"] == reim_item["name"], f"菜单{i} 菜品{j} 名称不一致"
            assert abs(orig_item["price"] - reim_item["price"]) < 0.001, f"菜单{i} 菜品{j} 价格不一致"
            assert orig_item["stock"] == reim_item["stock"], f"菜单{i} 菜品{j} 库存不一致"

    print()
    print("  [PASS] 导出-导入往返一致，数据完整无丢失")


def check_csv_field_errors():
    print_section("测试 11: CSV 字段错误（带行号）")
    bad_csv = """serving_date,menu_name,deadline,is_published,item_name,price,stock
bad-date,周一午餐,2026-08-01 09:00:00,0,红烧肉,abc,50
2026-08-02,,2026-08-02 09:00:00,0,清蒸鱼,25,-10
"""
    files = {"file": ("bad.csv", bad_csv, "text/csv")}
    r = api("POST", "/api/admin/menus/import/csv?conflict_strategy=skip", files=files)
    data = r.json()
    print(f"  状态码: {r.status_code}")
    detail = data.get("detail", {})
    error_list = detail.get("errors", []) if isinstance(detail, dict) else data.get("errors", [])
    print(f"  错误条数: {len(error_list)}")
    for e in error_list:
        print(f"    - {e}")
    assert r.status_code == 400
    assert len(error_list) >= 3
    has_row2 = any("第2行" in e for e in error_list)
    has_row3 = any("第3行" in e for e in error_list)
    assert has_row2, "应该有第2行的错误"
    assert has_row3, "应该有第3行的错误"
    print("  [PASS] CSV 错误带行号，正确标识问题位置")


def main():
    print("\n" + "=" * 60)
    print("  菜单批量导入导出功能测试")
    print("=" * 60)
    print()
    print("  注意: 测试前请确保服务已启动")
    print(f"  服务地址: {BASE_URL}")
    print()

    try:
        r = api("GET", "/api/health")
        r.raise_for_status()
    except Exception:
        print("  [ERROR] 无法连接服务，请先启动服务:")
        print("    python -m uvicorn main:app --host 127.0.0.1 --port 8000")
        print()
        return

    all_passed = True
    tests = [
        check_json_import_success,
        check_csv_import_success,
        check_field_validation_errors,
        check_conflict_skip_strategy,
        check_conflict_report_strategy,
        check_conflict_update_draft_strategy,
        check_published_menu_protection,
        check_export_json,
        check_export_csv,
        check_export_import_roundtrip,
        check_csv_field_errors,
    ]

    passed = 0
    failed = 0
    for test_fn in tests:
        try:
            test_fn()
            passed += 1
        except AssertionError as e:
            failed += 1
            all_passed = False
            print(f"  [FAIL] {test_fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            all_passed = False
            print(f"  [ERROR] {test_fn.__name__}: {e}")

    print_section("测试汇总")
    print(f"  通过: {passed}")
    print(f"  失败: {failed}")
    print(f"  总计: {len(tests)}")
    print()
    if all_passed:
        print("  [ALL PASSED] 所有测试通过！")
    else:
        print("  [SOME FAILED] 部分测试失败")
    print()


if __name__ == "__main__":
    main()
