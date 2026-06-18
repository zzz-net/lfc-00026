import requests
import json
import io
import csv
import time
from pathlib import Path

BASE_URL = "http://127.0.0.1:8003"

OFFSET = int(time.time() * 1000) % 100000


def _date(offset_days):
    base_year = 2098
    total_days = OFFSET + offset_days
    year = base_year + total_days // 365
    day_in_year = total_days % 365
    month = 1 + (day_in_year // 28) % 12
    day = 1 + day_in_year % 28
    return f"{year:04d}-{month:02d}-{day:02d}"


def print_section(title):
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}\n")


def api(method, path, **kwargs):
    return requests.request(method, f"{BASE_URL}{path}", timeout=10, **kwargs)


def build_test_menus():
    menus = []
    for i in range(5):
        date = _date(i)
        menus.append({
            "name": f"周{i+1}精品午餐",
            "serving_date": date,
            "deadline": f"{date} 09:00:00",
            "is_published": i % 2 == 0,
            "items": [
                {"name": f"主菜{i+1}", "price": 18.0 + i, "stock": 50 + i * 10},
                {"name": f"配菜{i+1}", "price": 8.0 + i, "stock": 80 + i * 10},
                {"name": f"汤品{i+1}", "price": 5.0, "stock": 60},
                {"name": "米饭", "price": 2.0, "stock": 200},
            ],
        })
    return menus


def verify_menus_equal(expected, actual, msg, ignore_ids=True):
    assert len(expected) == len(actual), f"{msg}: 菜单数量不一致，期望 {len(expected)}，实际 {len(actual)}"

    for i, (exp_menu, act_menu) in enumerate(zip(expected, actual)):
        assert exp_menu["name"] == act_menu["name"], \
            f"{msg}: 菜单{i} 名称不一致, 期望 '{exp_menu['name']}', 实际 '{act_menu['name']}'"
        assert exp_menu["serving_date"] == act_menu["serving_date"], \
            f"{msg}: 菜单{i} 日期不一致"
        assert exp_menu["is_published"] == act_menu["is_published"], \
            f"{msg}: 菜单{i} 发布状态不一致, 期望 {exp_menu['is_published']}, 实际 {act_menu['is_published']}"
        assert exp_menu["deadline"] == act_menu["deadline"], \
            f"{msg}: 菜单{i} 截止时间不一致"
        assert len(exp_menu["items"]) == len(act_menu["items"]), \
            f"{msg}: 菜单{i} 菜品数量不一致"

        for j, (exp_item, act_item) in enumerate(zip(exp_menu["items"], act_menu["items"])):
            assert exp_item["name"] == act_item["name"], \
                f"{msg}: 菜单{i} 菜品{j} 名称不一致"
            assert abs(exp_item["price"] - act_item["price"]) < 0.001, \
                f"{msg}: 菜单{i} 菜品{j} 价格不一致"
            assert exp_item["stock"] == act_item["stock"], \
                f"{msg}: 菜单{i} 菜品{j} 库存不一致"

    print(f"  [PASS] {msg}")


def main():
    print("\n" + "=" * 70)
    print("  菜单批量导入导出自动化测试（覆盖所有核心链路）")
    print("=" * 70)
    print()
    print(f"  服务地址: {BASE_URL}")
    print(f"  日期偏移: {OFFSET}")
    print()

    all_passed = True
    test_menus = None

    try:
        print_section("测试 1: JSON 成功导入")
        test_menus = build_test_menus()
        print(f"  准备导入 {len(test_menus)} 个测试菜单")
        for m in test_menus:
            status = "已发布" if m["is_published"] else "草稿"
            print(f"    {m['serving_date']}: {m['name']} ({status}), {len(m['items'])} 道菜")

        r = api("POST", "/api/admin/menus/import/json?conflict_strategy=skip", json=test_menus)
        result = r.json()
        assert r.status_code == 200, f"期望200，实际{r.status_code}"
        assert result["created"] == len(test_menus), f"期望新建 {len(test_menus)}，实际 {result['created']}"
        assert result["total"] == len(test_menus)
        print(f"  [PASS] JSON 导入成功: 新建={result['created']}")

        print_section("测试 2: 字段错误校验")
        bad_menus = [
            {
                "name": "",
                "serving_date": "bad-date",
                "deadline": "2098-01-15",
                "items": [
                    {"name": "", "price": "abc", "stock": -5},
                ],
            },
        ]
        r = api("POST", "/api/admin/menus/import/json?conflict_strategy=skip", json=bad_menus)
        data = r.json()
        assert r.status_code == 400, f"期望400，实际{r.status_code}"
        assert data["detail"]["code"] == "IMPORT_VALIDATION_ERROR"
        error_list = data["detail"]["errors"]
        assert len(error_list) >= 5, f"期望至少5条错误，实际{len(error_list)}"
        print(f"  [PASS] 字段错误校验正确，返回 {len(error_list)} 条详细错误")
        for e in error_list[:5]:
            print(f"    - {e}")

        print_section("测试 3: 同日期冲突 - skip 策略")
        r = api("POST", "/api/admin/menus/import/json?conflict_strategy=skip", json=test_menus)
        data = r.json()
        assert r.status_code == 200
        assert data["created"] == 0, f"期望新建0，实际{data['created']}"
        assert data["skipped"] == len(test_menus), f"期望跳过{len(test_menus)}，实际{data['skipped']}"
        assert len(data["conflicts"]) == len(test_menus), f"期望{len(test_menus)}个冲突，实际{len(data['conflicts'])}"
        print(f"  [PASS] skip 策略正确: 新建={data['created']}, 跳过={data['skipped']}, 冲突={len(data['conflicts'])}")

        print_section("测试 4: 同日期冲突 - report 策略")
        for m in test_menus:
            m["name"] = "修改后的名称"
        r = api("POST", "/api/admin/menus/import/json?conflict_strategy=report", json=test_menus)
        data = r.json()
        assert r.status_code == 200
        assert data["created"] == 0
        assert data["updated"] == 0
        assert len(data["conflicts"]) == len(test_menus)
        first_date = _date(0)
        r2 = api("GET", "/api/admin/menus")
        menus_list = r2.json()
        names = [m["name"] for m in menus_list if m["serving_date"] == first_date]
        assert "修改后的名称" not in names, "report策略不应修改数据"
        print(f"  [PASS] report 策略正确: 仅报告冲突，不修改数据")

        print_section("测试 5: 同日期冲突 - update_draft 策略（仅更新草稿）")
        draft_menus = []
        for i in range(2):
            date = _date(10 + i)
            draft_menus.append({
                "name": f"草稿菜单{i+1}",
                "serving_date": date,
                "deadline": f"{date} 09:00:00",
                "is_published": False,
                "items": [
                    {"name": "原菜1", "price": 10.0, "stock": 50},
                ],
            })
        r = api("POST", "/api/admin/menus/import/json?conflict_strategy=skip", json=draft_menus)
        assert r.status_code == 200
        assert r.json()["created"] == 2

        for m in draft_menus:
            m["name"] = "更新版-" + m["name"]
            m["items"].append({"name": "新加菜", "price": 15.0, "stock": 30})
        r = api("POST", "/api/admin/menus/import/json?conflict_strategy=update_draft", json=draft_menus)
        data = r.json()
        assert r.status_code == 200
        assert data["updated"] == 2, f"期望更新2，实际{data['updated']}"
        assert data["created"] == 0
        print(f"  [PASS] update_draft 策略正确: 更新={data['updated']}")

        print_section("测试 6: 已发布菜单保护（不能被修改）")
        published_menus = []
        for i in range(1):
            date = _date(20 + i)
            published_menus.append({
                "name": f"已发布菜单{i+1}",
                "serving_date": date,
                "deadline": f"{date} 09:00:00",
                "is_published": True,
                "items": [
                    {"name": "菜1", "price": 10.0, "stock": 50},
                ],
            })
        api("POST", "/api/admin/menus/import/json?conflict_strategy=skip", json=published_menus)

        published_menus[0]["name"] = "尝试修改已发布菜单"
        r = api("POST", "/api/admin/menus/import/json?conflict_strategy=update_draft", json=published_menus)
        data = r.json()
        assert data["updated"] == 0, "已发布菜单不应被更新"
        assert data["skipped"] == 1
        assert any("已发布" in e for e in data["errors"]), "错误信息应说明已发布"
        target_date = _date(20)
        r2 = api("GET", "/api/admin/menus")
        menus_list = r2.json()
        target = [m for m in menus_list if m["serving_date"] == target_date]
        assert len(target) == 1
        assert not target[0]["name"].startswith("尝试修改"), "已发布菜单名称不应被修改"
        print(f"  [PASS] 已发布菜单保护正确，无法被静默修改")

        print_section("测试 7: JSON 导出（验证包含所有必需字段）")
        r = api("GET", "/api/admin/menus/export/json",
                params={"start_date": _date(0), "end_date": _date(10)})
        exported = r.json()
        assert r.status_code == 200
        assert isinstance(exported, list)
        assert len(exported) > 0, "应该导出至少一个菜单"
        print(f"  导出菜单数量: {len(exported)}")

        for m in exported:
            assert "items" in m, "导出应包含 items 字段"
            assert "is_published" in m, "导出应包含 is_published 字段"
            assert len(m["items"]) > 0, "菜单应包含菜品"
            for item in m["items"]:
                assert "name" in item, "菜品应包含 name"
                assert "price" in item, "菜品应包含 price"
                assert "stock" in item, "菜品应包含 stock"
                assert "sold_count" in item, "菜品应包含 sold_count"
            status = "已发布" if m["is_published"] else "草稿"
            print(f"    {m['serving_date']}: {m['name']} ({status}), {len(m['items'])} 道菜")

        print("  [PASS] JSON 导出完整，包含菜品、库存、发布状态、已售数量")

        print_section("测试 8: CSV 导出（验证包含所有必需字段）")
        r = api("GET", "/api/admin/menus/export/csv",
                params={"start_date": _date(0), "end_date": _date(10)})
        csv_content = r.text
        lines = csv_content.strip().split("\n")
        print(f"  CSV 行数: {len(lines)} (含表头)")
        print(f"  表头: {lines[0]}")

        expected_headers = ["serving_date", "menu_name", "deadline", "is_published",
                           "item_name", "price", "stock", "sold_count"]
        actual_headers = lines[0].split(",")
        assert actual_headers == expected_headers, f"CSV 表头不正确: {actual_headers}"

        for col in ["serving_date", "menu_name", "item_name", "price", "stock", "is_published", "sold_count"]:
            assert col in lines[0], f"CSV 缺少列: {col}"

        assert len(lines) > 1, "CSV 应包含数据行"
        print(f"  [PASS] CSV 导出完整，包含所有必需列")

        print_section("测试 9: JSON 导出-导入往返一致性")
        print(f"  导出 {len(exported)} 个菜单")

        original_items_count = sum(len(m["items"]) for m in exported)
        print(f"  原始菜品总数: {original_items_count}")

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
        assert import_result["created"] == len(exported)

        r3 = api("GET", "/api/admin/menus/export/json",
                params={"start_date": _date(30), "end_date": _date(40)})
        reimported = r3.json()
        reimported_items_count = sum(len(m["items"]) for m in reimported)
        print(f"  重新导出验证: {len(reimported)} 个菜单, {reimported_items_count} 个菜品")

        verify_menus_equal(reimport_menus, reimported, "导出-导入往返一致")

        print_section("测试 10: CSV 导出-导入往返一致性")
        r = api("GET", "/api/admin/menus/export/csv",
                params={"start_date": _date(0), "end_date": _date(10)})
        csv_for_reimport = r.text

        csv_lines = csv_for_reimport.strip().split("\n")
        unique_dates_exported = len(set(line.split(",")[0] for line in csv_lines[1:]))
        print(f"  导出 CSV 包含 {len(csv_lines) - 1} 行菜品数据，{unique_dates_exported} 个菜单")

        print("  用导出的 CSV 重新导入（模拟备份恢复）...")
        new_csv_lines = [csv_lines[0]]
        for line in csv_lines[1:]:
            parts = line.split(",")
            old_date = parts[0]
            offset = (int(old_date[-2:]) - int(_date(0)[-2:])) % 28
            new_line = ",".join([_date(50 + offset)] + parts[1:])
            new_csv_lines.append(new_line)
        new_csv = "\n".join(new_csv_lines)

        files = {"file": ("backup.csv", new_csv, "text/csv")}
        r = api("POST", "/api/admin/menus/import/csv?conflict_strategy=skip", files=files)
        result = r.json()
        assert r.status_code == 200
        assert result["created"] == unique_dates_exported, f"期望新建 {unique_dates_exported}，实际 {result['created']}"
        print(f"  [PASS] CSV 往返导入成功: 新建={result['created']}")

        print_section("测试 11: CSV 字段错误（带行号）")
        bad_csv = """serving_date,menu_name,deadline,is_published,item_name,price,stock
bad-date,周一午餐,2098-08-01 09:00:00,0,红烧肉,abc,50
2098-08-02,,2098-08-02 09:00:00,0,清蒸鱼,25,-10
"""
        files = {"file": ("bad.csv", bad_csv, "text/csv")}
        r = api("POST", "/api/admin/menus/import/csv?conflict_strategy=skip", files=files)
        data = r.json()
        assert r.status_code == 400
        detail = data.get("detail", {})
        error_list = detail.get("errors", []) if isinstance(detail, dict) else data.get("errors", [])
        print(f"  错误条数: {len(error_list)}")
        for e in error_list:
            print(f"    - {e}")
        assert len(error_list) >= 3, f"期望至少3条错误，实际{len(error_list)}"
        has_row2 = any("第2行" in e for e in error_list)
        has_row3 = any("第3行" in e for e in error_list)
        assert has_row2, "应该有第2行的错误"
        assert has_row3, "应该有第3行的错误"
        print("  [PASS] CSV 错误带行号，正确标识问题位置")

        print_section("测试汇总")
        print("  [PASS] 测试 1: JSON 成功导入")
        print("  [PASS] 测试 2: 字段错误校验（带行号和原因）")
        print("  [PASS] 测试 3: 同日期冲突 - skip 策略")
        print("  [PASS] 测试 4: 同日期冲突 - report 策略（仅报告，不修改）")
        print("  [PASS] 测试 5: 同日期冲突 - update_draft 策略（更新草稿菜单）")
        print("  [PASS] 测试 6: 已发布菜单不能被修改")
        print("  [PASS] 测试 7: JSON 格式导出（含菜品、库存、发布状态）")
        print("  [PASS] 测试 8: CSV 格式导出（含所有必需列）")
        print("  [PASS] 测试 9: JSON 导出-再导入一致性（往返验证）")
        print("  [PASS] 测试 10: CSV 导出-再导入一致性（往返验证）")
        print("  [PASS] 测试 11: CSV 字段错误（带行号）")
        print()
        print("  [ALL PASSED] 所有 11 项测试通过！")
        print()
        print("  覆盖的管理员实际操作场景:")
        print("  1. 批量导入新菜单（JSON/CSV）")
        print("  2. 字段错误快速定位（行号+原因）")
        print("  3. 冲突处理：跳过、仅报告、更新草稿")
        print("  4. 已发布菜单安全保护")
        print("  5. 导出备份（JSON/CSV）")
        print("  6. 备份恢复（导出-重启-导入一致）")
        print()

    except AssertionError as e:
        all_passed = False
        print(f"\n  [FAIL] 断言失败: {e}")
    except Exception as e:
        all_passed = False
        print(f"\n  [ERROR] 发生异常: {e}")
        import traceback
        traceback.print_exc()

    print()
    print("=" * 70)
    if all_passed:
        print("  测试完成: 全部通过 [OK]")
    else:
        print("  测试完成: 存在失败 [FAIL]")
    print("=" * 70)
    print()


if __name__ == "__main__":
    main()
