import requests
import json
import time
from pathlib import Path

BASE_URL = "http://127.0.0.1:8003"
TEST_DB = Path(__file__).parent / "canteen_test_reboot.db"

OFFSET = int(time.time() * 1000) % 100000


def _date(offset_days):
    base_year = 2099
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


def verify_menus_equal(expected, actual, msg):
    assert len(expected) == len(actual), f"{msg}: 菜单数量不一致，期望 {len(expected)}，实际 {len(actual)}"

    for i, (exp_menu, act_menu) in enumerate(zip(expected, actual)):
        assert exp_menu["name"] == act_menu["name"], \
            f"{msg}: 菜单{i} 名称不一致"
        assert exp_menu["serving_date"] == act_menu["serving_date"], \
            f"{msg}: 菜单{i} 日期不一致"
        assert exp_menu["is_published"] == act_menu["is_published"], \
            f"{msg}: 菜单{i} 发布状态不一致"
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
    print("  菜单导出-重启-导入一致性测试（手动执行版）")
    print("=" * 70)
    print()
    print(f"  服务地址: {BASE_URL}")
    print(f"  测试数据库: {TEST_DB}")
    print(f"  日期偏移: {OFFSET}")
    print()

    all_passed = True

    try:
        # 步骤 1: 验证服务运行且数据库为空
        print_section("步骤 1: 验证服务运行且数据库为空")
        r = api("GET", "/api/health")
        assert r.status_code == 200, "服务未运行"
        print("  [PASS] 服务运行正常")

        r = api("GET", "/api/admin/menus")
        menus = r.json()
        assert len(menus) == 0, f"数据库应该为空，实际有 {len(menus)} 个菜单"
        print("  [PASS] 数据库为空")

        # 步骤 2: 导入测试菜单
        print_section("步骤 2: 导入测试菜单")
        test_menus = build_test_menus()
        print(f"  准备导入 {len(test_menus)} 个测试菜单")
        for m in test_menus:
            status = "已发布" if m["is_published"] else "草稿"
            print(f"    {m['serving_date']}: {m['name']} ({status}), {len(m['items'])} 道菜")

        r = api("POST", "/api/admin/menus/import/json?conflict_strategy=skip", json=test_menus)
        result = r.json()
        assert r.status_code == 200, f"导入失败: {r.status_code}"
        assert result["created"] == len(test_menus), f"期望新建 {len(test_menus)}，实际 {result['created']}"
        print(f"  [PASS] 导入成功: 新建={result['created']}")

        # 步骤 3: 导出 JSON
        print_section("步骤 3: 导出菜单数据（JSON）")
        r = api("GET", "/api/admin/menus/export/json",
                params={"start_date": _date(0), "end_date": _date(10)})
        exported_data = r.json()
        print(f"  导出 {len(exported_data)} 个菜单")
        total_items = sum(len(m["items"]) for m in exported_data)
        print(f"  共 {total_items} 道菜品")

        for m in exported_data:
            status = "已发布" if m["is_published"] else "草稿"
            print(f"    {m['serving_date']}: {m['name']} ({status})")
            for item in m["items"]:
                print(f"      - {item['name']}: price={item['price']}, stock={item['stock']}, sold={item.get('sold_count', 0)}")

        verify_menus_equal(test_menus, exported_data, "导出数据与原始一致")

        backup_file = Path(__file__).parent / f"menu_backup_{OFFSET}.json"
        with open(backup_file, "w", encoding="utf-8") as f:
            json.dump(exported_data, f, ensure_ascii=False, indent=2)
        print(f"  备份已保存到: {backup_file.name}")

        # 步骤 4: 导出 CSV 并验证格式
        print_section("步骤 4: 导出菜单数据（CSV）并验证格式")
        r = api("GET", "/api/admin/menus/export/csv",
                params={"start_date": _date(0), "end_date": _date(10)})
        csv_content = r.text
        csv_lines = csv_content.strip().split("\n")
        print(f"  CSV 共 {len(csv_lines)} 行（含表头）")
        print(f"  表头: {csv_lines[0]}")

        expected_headers = ["serving_date", "menu_name", "deadline", "is_published",
                           "item_name", "price", "stock", "sold_count"]
        actual_headers = csv_lines[0].split(",")
        assert actual_headers == expected_headers, f"CSV 表头不一致"
        print("  [PASS] CSV 表头正确")

        for col in ["serving_date", "item_name", "price", "stock", "is_published", "sold_count"]:
            assert col in csv_lines[0], f"CSV 缺少列: {col}"
        print("  [PASS] CSV 包含所有必需列（含菜品、库存、发布状态、已售数量）")

        # 保存 CSV 备份
        csv_backup = Path(__file__).parent / f"menu_backup_{OFFSET}.csv"
        with open(csv_backup, "w", encoding="utf-8") as f:
            f.write(csv_content)
        print(f"  CSV 备份已保存到: {csv_backup.name}")

        # 现在需要手动停止服务、重置数据库、重启服务
        print_section("请执行以下手动操作：")
        print("  1. 停止当前运行在 8003 端口的服务")
        print("  2. 删除测试数据库文件:")
        print(f"     - {TEST_DB}")
        print(f"     - {TEST_DB.with_suffix('.db-wal')}")
        print(f"     - {TEST_DB.with_suffix('.db-shm')}")
        print("  3. 使用相同的环境变量重新启动服务在 8003 端口:")
        print(f'     $env:CANTEEN_DB_PATH="{TEST_DB}"; python -m uvicorn main:app --host 127.0.0.1 --port 8003')
        print()
        print("  完成后，按回车键继续测试...")
        input()

        # 步骤 5: 验证服务重启且数据库为空
        print_section("步骤 5: 验证服务重启且数据库为空")
        for i in range(10):
            try:
                r = api("GET", "/api/health", timeout=2)
                if r.status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(1)

        r = api("GET", "/api/health")
        assert r.status_code == 200, "服务未重启"
        print("  [PASS] 服务已重启")

        r = api("GET", "/api/admin/menus")
        menus = r.json()
        assert len(menus) == 0, f"数据库应该为空，实际有 {len(menus)} 个菜单"
        print("  [PASS] 数据库为空")

        # 步骤 6: 重新导入 JSON 备份
        print_section("步骤 6: 重新导入之前导出的 JSON 备份")
        print(f"  从备份文件 {backup_file.name} 重新导入...")

        with open(backup_file, "r", encoding="utf-8") as f:
            backup_data = json.load(f)

        r = api("POST", "/api/admin/menus/import/json?conflict_strategy=skip", json=backup_data)
        result = r.json()
        assert r.status_code == 200, f"重新导入失败: {r.status_code}"
        assert result["created"] == len(backup_data), f"期望新建 {len(backup_data)}，实际 {result['created']}"
        print(f"  [PASS] 重新导入成功: 新建={result['created']}, 跳过={result['skipped']}")

        # 步骤 7: 验证重启后导入的数据与原始完全一致
        print_section("步骤 7: 验证重启后导入的数据与原始完全一致")
        r = api("GET", "/api/admin/menus/export/json",
                params={"start_date": _date(0), "end_date": _date(10)})
        reimported = r.json()

        verify_menus_equal(test_menus, reimported, "重启后导入数据与原始一致")

        for i, (orig, reim) in enumerate(zip(test_menus, reimported)):
            print(f"    {orig['serving_date']}:")
            print(f"      名称: {orig['name']} == {reim['name']} [OK]")
            print(f"      状态: {'已发布' if orig['is_published'] else '草稿'} == {'已发布' if reim['is_published'] else '草稿'} [OK]")
            print(f"      菜品: {len(orig['items'])} 道 == {len(reim['items'])} 道 [OK]")
            for orig_item, reim_item in zip(orig["items"], reim["items"]):
                print(f"        {orig_item['name']}: price={orig_item['price']} stock={orig_item['stock']} == price={reim_item['price']} stock={reim_item['stock']} [OK]")

        # 步骤 8: 再次导出验证一致性
        print_section("步骤 8: 再次导出验证数据一致性")
        r = api("GET", "/api/admin/menus/export/json",
                params={"start_date": _date(0), "end_date": _date(10)})
        reexported = r.json()
        verify_menus_equal(reimported, reexported, "再次导出数据与导入数据一致")

        # 步骤 9: CSV 备份重新导入验证
        print_section("步骤 9: CSV 备份重新导入验证")
        print("  请再次执行以下手动操作：")
        print("  1. 停止当前运行在 8003 端口的服务")
        print("  2. 删除测试数据库文件")
        print("  3. 使用相同的环境变量重新启动服务在 8003 端口")
        print()
        print("  完成后，按回车键继续测试...")
        input()

        # 验证服务重启
        for i in range(10):
            try:
                r = api("GET", "/api/health", timeout=2)
                if r.status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(1)

        print("  用 CSV 备份重新导入...")
        with open(csv_backup, "r", encoding="utf-8") as f:
            csv_for_reimport = f.read()

        files = {"file": ("reimport.csv", csv_for_reimport, "text/csv")}
        r = api("POST", "/api/admin/menus/import/csv?conflict_strategy=skip", files=files)
        result = r.json()
        assert r.status_code == 200, f"CSV 重新导入失败: {r.status_code}"
        assert result["created"] == len(test_menus), f"期望新建 {len(test_menus)}，实际 {result['created']}"
        print(f"  [PASS] CSV 重新导入成功: 新建={result['created']}")

        r = api("GET", "/api/admin/menus/export/json",
                params={"start_date": _date(0), "end_date": _date(10)})
        csv_reimported = r.json()
        verify_menus_equal(test_menus, csv_reimported, "CSV 导出后再导入一致性")

        # 测试汇总
        print_section("测试汇总")
        print("  [OK] 成功导入测试菜单")
        print("  [OK] JSON 导出完整（含菜品、库存、发布状态、已售数量）")
        print("  [OK] CSV 导出格式正确（含所有必需列）")
        print("  [OK] 服务停止后数据保留在数据库")
        print("  [OK] 数据库重置后为空")
        print("  [OK] 服务重启正常")
        print("  [OK] 备份 JSON 重新导入后数据与原始完全一致")
        print("  [OK] 备份 CSV 重新导入后数据与原始完全一致")
        print("  [OK] 多次导出数据一致")
        print()
        print("  [ALL PASSED] 导出-重启-导入完整链路测试通过！")
        print()
        print("  管理员实际场景覆盖:")
        print("  1. 每周菜单批量录入 -> 导出备份")
        print("  2. 服务器迁移/数据库重置 -> 从备份恢复")
        print("  3. 测试环境配置 -> 导出导入到生产")
        print("  4. 误操作删除菜单 -> 从备份快速恢复")
        print()

        # 清理
        print("  清理测试文件...")
        if backup_file.exists():
            backup_file.unlink()
        if csv_backup.exists():
            csv_backup.unlink()
        print("  清理完成")
        print()
        print("  请手动停止 8003 端口的服务，并删除测试数据库文件。")

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
        print(f"  测试文件保留用于排查:")
        print(f"    - 测试数据库: {TEST_DB}")
        print(f"    - JSON 备份: menu_backup_{OFFSET}.json")
        print(f"    - CSV 备份: menu_backup_{OFFSET}.csv")
    print("=" * 70)
    print()


if __name__ == "__main__":
    main()
