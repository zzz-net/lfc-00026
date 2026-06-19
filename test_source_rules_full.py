import requests
import json
import os
import sys
import time
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path

BASE_URL = "http://127.0.0.1:8009"
TEST_DB = Path(__file__).parent / "canteen_test_source_rules.db"
SCRIPT_DIR = Path(__file__).parent

passed = 0
failed = 0
_server_output_thread = None
_server_output_stop = threading.Event()


def _read_server_output(process, log_file):
    with open(log_file, "w", encoding="utf-8") as f:
        for line in process.stdout:
            if _server_output_stop.is_set():
                break
            f.write(line)
            f.flush()


def print_section(title):
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}\n")


def print_test(name, success, detail=""):
    global passed, failed
    status = "[PASS]" if success else "[FAIL]"
    print(f"  {status} - {name}")
    if detail:
        print(f"    {detail}")
    if success:
        passed += 1
    else:
        failed += 1


def api(method, path, **kwargs):
    kwargs.setdefault("timeout", 60)
    return requests.request(method, f"{BASE_URL}{path}", **kwargs)


def cleanup_existing_db():
    for f in [TEST_DB, TEST_DB.with_suffix(".db-wal"), TEST_DB.with_suffix(".db-shm")]:
        if f.exists():
            f.unlink()
            print(f"  [清理] 已删除旧的测试数据库: {f.name}")


def start_service(extra_env=None):
    print_section("启动测试服务")

    env = os.environ.copy()
    env["CANTEEN_DB_PATH"] = str(TEST_DB)
    if extra_env:
        env.update(extra_env)

    cmd = [
        sys.executable, "-m", "uvicorn", "main:app",
        "--host", "127.0.0.1",
        "--port", "8009",
    ]

    print(f"  命令: {' '.join(cmd)}")
    print(f"  数据库: {TEST_DB}")
    print(f"  服务地址: {BASE_URL}")
    if extra_env:
        print(f"  额外环境变量: {extra_env}")
    print()

    process = subprocess.Popen(
        cmd,
        cwd=str(SCRIPT_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    _server_output_stop.clear()
    log_file = SCRIPT_DIR / "test_server.log"
    _server_output_thread = threading.Thread(
        target=_read_server_output,
        args=(process, log_file),
        daemon=True,
    )
    _server_output_thread.start()

    print(f"  服务 PID: {process.pid}")
    print(f"  日志文件: {log_file}")
    print("  等待服务启动...", end="", flush=True)

    last_error = None
    for i in range(120):
        if process.poll() is not None:
            print(f"\n  [ERROR] 服务启动失败！退出码: {process.returncode}")
            try:
                output = process.stdout.read()
                if output:
                    print(f"  输出: {output[:500]}")
            except:
                pass
            raise RuntimeError("服务启动失败")
        try:
            r = requests.get(f"{BASE_URL}/api/health", timeout=5)
            if r.status_code == 200:
                print(" 启动成功！")
                return process
        except Exception as e:
            last_error = str(e)
        print(".", end="", flush=True)
        time.sleep(0.5)

    print(f"\n  [ERROR] 服务启动超时！最后错误: {last_error}")
    process.terminate()
    raise RuntimeError("服务启动超时")


def stop_service(process):
    print(f"\n  停止服务 PID={process.pid}...")
    try:
        _server_output_stop.set()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        if _server_output_thread and _server_output_thread.is_alive():
            _server_output_thread.join(timeout=2)
        print("  服务已停止")
    except Exception as e:
        print(f"  停止服务时出错: {e}")


def wait_for_service():
    print("  等待服务就绪...", end="", flush=True)
    for i in range(30):
        try:
            r = api("GET", "/api/health")
            if r.status_code == 200:
                print(" 就绪！")
                return
        except Exception:
            pass
        print(".", end="", flush=True)
        time.sleep(0.5)
    raise RuntimeError("服务未就绪")


def setup_test_data():
    print_section("初始化测试数据")

    today = datetime.now().strftime("%Y-%m-%d")
    deadline = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

    api("POST", "/api/admin/employees", json={"id": "SR001", "name": "来源规则测试员工1", "initial_balance": 200.0})
    api("POST", "/api/admin/employees", json={"id": "SR002", "name": "来源规则测试员工2", "initial_balance": 100.0})

    menu = api("POST", "/api/admin/menus", json={
        "name": f"来源规则测试菜单-{today}",
        "serving_date": today,
        "deadline": deadline,
    }).json()

    item1 = api("POST", f"/api/admin/menus/{menu['id']}/items", json={
        "name": "宫保鸡丁", "price": 20.0, "stock": 50,
    }).json()

    item2 = api("POST", f"/api/admin/menus/{menu['id']}/items", json={
        "name": "麻婆豆腐", "price": 15.0, "stock": 40,
    }).json()

    api("POST", f"/api/admin/menus/{menu['id']}/publish")

    api("PUT", "/api/admin/config/makeup_revoke_deadline_hours", json={"value": "0"})

    print(f"  今日菜单: {menu['id']}")
    print(f"  菜品1: 宫保鸡丁(20元, id={item1['id']})")
    print(f"  菜品2: 麻婆豆腐(15元, id={item2['id']})")

    return {
        "today": today,
        "menu_id": menu["id"],
        "item1_id": item1["id"],
        "item2_id": item2["id"],
    }


def test_default_rules(ctx):
    print_section("测试 1: 默认来源规则加载")

    r = api("GET", "/api/admin/source-rules")
    ok = True

    if r.status_code != 200:
        print_test("获取规则列表返回200", False, f"状态码 {r.status_code}")
        ok = False
        return

    data = r.json()

    print_test("版本号正确", data.get("version") == "1.0", f"version={data.get('version')}")
    if data.get("version") != "1.0":
        ok = False

    print_test("至少3条默认规则", data.get("total", 0) >= 3, f"total={data.get('total')}")
    if data.get("total", 0) < 3:
        ok = False

    default_rules = data.get("layers", {}).get("default", [])
    print_test("默认层级有3条规则", len(default_rules) == 3, f"count={len(default_rules)}")
    if len(default_rules) != 3:
        ok = False

    default_codes = [r["code"] for r in default_rules]
    for expected_code in ["window", "admin", "manual"]:
        has_code = expected_code in default_codes
        print_test(f"默认规则包含 {expected_code}", has_code, f"codes={default_codes}")
        if not has_code:
            ok = False

    allowed_sources = data.get("allowed_sources", [])
    print_test("允许的来源包含默认3种",
               all(s in allowed_sources for s in ["window", "admin", "manual"]),
               f"allowed={allowed_sources}")
    if not all(s in allowed_sources for s in ["window", "admin", "manual"]):
        ok = False

    for rule in data.get("rules", []):
        if rule["code"] == "window":
            print_test("window规则优先级最高", rule.get("priority") >= 90, f"priority={rule.get('priority')}")
            print_test("window规则启用", rule.get("is_enabled") == True, f"enabled={rule.get('is_enabled')}")
            if rule.get("priority") < 90 or not rule.get("is_enabled"):
                ok = False

    print_test("默认规则加载总结果", ok)


def test_environment_rules(ctx):
    print_section("测试 2: 环境变量规则合并")

    env_rules = json.dumps([
        {
            "code": "env_test",
            "name": "环境变量测试来源",
            "description": "通过环境变量配置的规则",
            "priority": 95,
            "is_enabled": True,
        },
        {
            "code": "window",
            "name": "环境变量覆盖的窗口",
            "priority": 150,
            "is_enabled": True,
        }
    ])

    print(f"  环境变量规则: {env_rules}")

    r = api("GET", "/api/admin/source-rules")
    data = r.json()

    ok = True

    env_rules_list = data.get("layers", {}).get("environment", [])
    print_test("环境变量层级有2条规则", len(env_rules_list) == 2, f"count={len(env_rules_list)}")
    if len(env_rules_list) != 2:
        ok = False

    merged_rules = data.get("rules", [])
    window_rule = next((r for r in merged_rules if r["code"] == "window"), None)
    if window_rule:
        print_test("高优先级环境变量规则覆盖window",
                   window_rule["priority"] == 150,
                   f"priority={window_rule['priority']}, layer={window_rule.get('source_layer')}")
        print_test("覆盖后的window来自环境层",
                   window_rule.get("source_layer") == "environment",
                   f"layer={window_rule.get('source_layer')}")
        if window_rule["priority"] != 150 or window_rule.get("source_layer") != "environment":
            ok = False

    env_test_rule = next((r for r in merged_rules if r["code"] == "env_test"), None)
    if env_test_rule:
        print_test("新增的环境变量规则存在", env_test_rule is not None)
        print_test("环境变量规则名称正确", env_test_rule.get("name") == "环境变量测试来源",
                   f"name={env_test_rule.get('name')}")
        if env_test_rule.get("name") != "环境变量测试来源":
            ok = False

    allowed_sources = data.get("allowed_sources", [])
    print_test("允许的来源包含env_test", "env_test" in allowed_sources, f"allowed={allowed_sources}")
    if "env_test" not in allowed_sources:
        ok = False

    print_test("环境变量规则合并总结果", ok)


def test_runtime_rules_crud(ctx):
    print_section("测试 3: 运行时规则 CRUD")

    ok = True

    r = api("POST", "/api/admin/source-rules", json={
        "code": "runtime_new",
        "name": "运行时新增来源",
        "description": "通过API新增的规则",
        "priority": 75,
        "is_enabled": True,
    })

    if r.status_code != 200:
        print_test("创建规则返回200", False, f"状态码 {r.status_code}: {r.json()}")
        ok = False
        return

    created = r.json()
    print_test("规则创建成功", created.get("code") == "runtime_new", f"code={created.get('code')}")
    if created.get("code") != "runtime_new":
        ok = False

    rule_id = created.get("id")
    print_test("返回规则ID", rule_id is not None, f"id={rule_id}")
    if rule_id is None:
        ok = False

    r = api("GET", "/api/admin/source-rules/runtime_new")
    if r.status_code != 200:
        print_test("获取单个规则返回200", False, f"状态码 {r.status_code}")
        ok = False
    else:
        fetched = r.json()
        print_test("获取的规则code正确", fetched.get("code") == "runtime_new",
                   f"code={fetched.get('code')}")
        print_test("获取的规则来源层级为runtime", fetched.get("source_layer") == "runtime",
                   f"layer={fetched.get('source_layer')}")
        if fetched.get("code") != "runtime_new" or fetched.get("source_layer") != "runtime":
            ok = False

    r = api("PATCH", "/api/admin/source-rules/runtime_new", json={
        "name": "运行时更新的名称",
        "priority": 85,
        "is_enabled": False,
    })

    if r.status_code != 200:
        print_test("更新规则返回200", False, f"状态码 {r.status_code}: {r.json()}")
        ok = False
    else:
        updated = r.json()
        print_test("规则名称已更新", updated.get("name") == "运行时更新的名称",
                   f"name={updated.get('name')}")
        print_test("规则优先级已更新", updated.get("priority") == 85,
                   f"priority={updated.get('priority')}")
        print_test("规则已禁用", updated.get("is_enabled") == False,
                   f"enabled={updated.get('is_enabled')}")
        if (updated.get("name") != "运行时更新的名称" or
            updated.get("priority") != 85 or
            updated.get("is_enabled") != False):
            ok = False

    r = api("GET", "/api/admin/source-rules")
    data = r.json()
    allowed = data.get("allowed_sources", [])
    print_test("禁用的规则不在允许列表中", "runtime_new" not in allowed,
               f"runtime_new in allowed: {'runtime_new' in allowed}")
    if "runtime_new" in allowed:
        ok = False

    r = api("DELETE", "/api/admin/source-rules/runtime_new")
    if r.status_code != 200:
        print_test("删除规则返回200", False, f"状态码 {r.status_code}: {r.json()}")
        ok = False
    else:
        result = r.json()
        print_test("删除成功响应", result.get("success") == True, f"result={result}")
        if result.get("success") != True:
            ok = False

    r = api("GET", "/api/admin/source-rules/runtime_new")
    print_test("删除后获取返回404", r.status_code == 404, f"状态码 {r.status_code}")
    if r.status_code != 404:
        ok = False

    print_test("运行时规则CRUD总结果", ok)


def test_rule_validation(ctx):
    print_section("测试 4: 规则数据验证")

    ok = True

    r = api("POST", "/api/admin/source-rules", json={
        "name": "缺少code",
        "priority": 50,
    })
    print_test("缺少code返回400", r.status_code == 400, f"状态码 {r.status_code}")
    if r.status_code != 400:
        ok = False

    r = api("POST", "/api/admin/source-rules", json={
        "code": "test_validation",
        "name": "",
        "priority": 50,
    })
    print_test("空name返回400", r.status_code == 400, f"状态码 {r.status_code}")
    if r.status_code != 400:
        ok = False

    r = api("POST", "/api/admin/source-rules", json={
        "code": "test_validation",
        "name": "无效优先级",
        "priority": 1500,
    })
    print_test("优先级超出范围返回400", r.status_code == 400, f"状态码 {r.status_code}")
    if r.status_code != 400:
        ok = False

    r = api("POST", "/api/admin/source-rules", json={
        "code": "test_validation",
        "name": "无效正则",
        "priority": 50,
        "match_pattern": "[invalid",
    })
    print_test("无效正则返回400", r.status_code == 400, f"状态码 {r.status_code}")
    if r.status_code != 400:
        ok = False

    r = api("POST", "/api/admin/source-rules", json={
        "code": "window",
        "name": "重复code",
        "priority": 50,
    })
    print_test("重复code返回409", r.status_code == 409, f"状态码 {r.status_code}")
    if r.status_code != 409:
        ok = False

    r = api("GET", "/api/admin/source-rules/not_exist")
    print_test("查询不存在的规则返回404", r.status_code == 404, f"状态码 {r.status_code}")
    if r.status_code != 404:
        ok = False

    r = api("PATCH", "/api/admin/source-rules/not_exist", json={"name": "test"})
    print_test("更新不存在的规则返回404", r.status_code == 404, f"状态码 {r.status_code}")
    if r.status_code != 404:
        ok = False

    r = api("DELETE", "/api/admin/source-rules/not_exist")
    print_test("删除不存在的规则返回404", r.status_code == 404, f"状态码 {r.status_code}")
    if r.status_code != 404:
        ok = False

    print_test("规则数据验证总结果", ok)


def test_import_export(ctx):
    print_section("测试 5: 规则导入导出")

    ok = True

    r = api("GET", "/api/admin/source-rules/export/json")
    if r.status_code != 200:
        print_test("导出返回200", False, f"状态码 {r.status_code}")
        ok = False
        return

    export_data = r.json()
    print_test("导出版本号正确", export_data.get("version") == "1.0",
               f"version={export_data.get('version')}")
    print_test("导出有count字段", "count" in export_data, f"keys={list(export_data.keys())}")
    print_test("导出有rules数组", isinstance(export_data.get("rules"), list),
               f"rules type={type(export_data.get('rules'))}")
    if (export_data.get("version") != "1.0" or
        "count" not in export_data or
        not isinstance(export_data.get("rules"), list)):
        ok = False

    export_count = export_data["count"]
    print(f"  导出 {export_count} 条规则")

    new_rules = [
        {
            "code": "import_test1",
            "name": "导入测试1",
            "description": "导入的规则1",
            "priority": 60,
            "is_enabled": True,
            "version": "1.0",
        },
        {
            "code": "import_test2",
            "name": "导入测试2",
            "priority": 55,
            "is_enabled": True,
            "version": "1.0",
        },
    ]

    r = api("POST", "/api/admin/source-rules/import", json={
        "rules": new_rules,
        "conflict_strategy": "skip",
    })

    if r.status_code != 200:
        print_test("导入返回200", False, f"状态码 {r.status_code}: {r.json()}")
        ok = False
        return

    import_result = r.json()
    print_test("导入成功", import_result.get("success") == True, f"success={import_result.get('success')}")
    print_test("导入成功数量正确", import_result.get("success_count") == 2,
               f"success_count={import_result.get('success_count')}")
    print_test("导入无跳过", import_result.get("skipped_count") == 0,
               f"skipped_count={import_result.get('skipped_count')}")
    print_test("导入无错误", import_result.get("error_count") == 0,
               f"error_count={import_result.get('error_count')}")
    if (import_result.get("success") != True or
        import_result.get("success_count") != 2 or
        import_result.get("skipped_count") != 0 or
        import_result.get("error_count") != 0):
        ok = False

    r = api("GET", "/api/admin/source-rules/export/json")
    new_export = r.json()
    print_test("导出数量增加2条", new_export.get("count") == export_count + 2,
               f"before={export_count}, after={new_export.get('count')}")
    if new_export.get("count") != export_count + 2:
        ok = False

    imported_codes = [r["code"] for r in new_export["rules"]]
    for expected in ["import_test1", "import_test2"]:
        has_code = expected in imported_codes
        print_test(f"导出包含 {expected}", has_code, f"codes={imported_codes}")
        if not has_code:
            ok = False

    r = api("GET", "/api/admin/source-rules/import-history")
    if r.status_code != 200:
        print_test("获取导入历史返回200", False, f"状态码 {r.status_code}")
        ok = False
    else:
        history = r.json()
        print_test("至少有1条导入历史", len(history) >= 1, f"count={len(history)}")
        if len(history) >= 1:
            latest = history[0]
            print_test("历史记录成功数正确", latest.get("success_count") == 2,
                       f"success_count={latest.get('success_count')}")
            print_test("历史记录冲突策略正确", latest.get("conflict_strategy") == "skip",
                       f"strategy={latest.get('conflict_strategy')}")
            if latest.get("success_count") != 2 or latest.get("conflict_strategy") != "skip":
                ok = False

    print_test("规则导入导出总结果", ok)


def test_import_conflict_strategies(ctx):
    print_section("测试 6: 导入冲突处理策略")

    ok = True

    existing_rules = [
        {
            "code": "admin",
            "name": "导入覆盖admin",
            "priority": 200,
            "is_enabled": True,
            "version": "1.0",
        },
        {
            "code": "conflict_new",
            "name": "新规则",
            "priority": 50,
            "is_enabled": True,
            "version": "1.0",
        },
    ]

    print("  --- 测试 skip 策略 ---")
    r = api("POST", "/api/admin/source-rules/import", json={
        "rules": existing_rules,
        "conflict_strategy": "skip",
    })

    if r.status_code != 200:
        print_test("skip策略导入返回200", False, f"状态码 {r.status_code}: {r.json()}")
        ok = False
    else:
        result = r.json()
        print_test("skip策略成功1条", result.get("success_count") == 1,
                   f"success_count={result.get('success_count')}")
        print_test("skip策略跳过1条", result.get("skipped_count") == 1,
                   f"skipped_count={result.get('skipped_count')}")
        print_test("skip策略有冲突记录", len(result.get("conflicts", [])) == 1,
                   f"conflicts={len(result.get('conflicts', []))}")
        if (result.get("success_count") != 1 or
            result.get("skipped_count") != 1 or
            len(result.get("conflicts", [])) != 1):
            ok = False

    print("  --- 测试 overwrite 策略 ---")
    r = api("POST", "/api/admin/source-rules/import", json={
        "rules": [
            {
                "code": "admin",
                "name": "overwrite后的admin",
                "priority": 250,
                "is_enabled": True,
                "version": "1.0",
            }
        ],
        "conflict_strategy": "overwrite",
    })

    if r.status_code != 200:
        print_test("overwrite策略导入返回200", False, f"状态码 {r.status_code}: {r.json()}")
        ok = False
    else:
        result = r.json()
        print_test("overwrite策略成功1条", result.get("success_count") == 1,
                   f"success_count={result.get('success_count')}")
        print_test("overwrite策略有冲突记录", len(result.get("conflicts", [])) == 1,
                   f"conflicts={len(result.get('conflicts', []))}")
        if result.get("success_count") != 1 or len(result.get("conflicts", [])) != 1:
            ok = False

    r = api("GET", "/api/admin/source-rules/admin")
    updated = r.json()
    print_test("overwrite后名称已更新", updated.get("name") == "overwrite后的admin",
               f"name={updated.get('name')}")
    print_test("overwrite后优先级已更新", updated.get("priority") == 250,
               f"priority={updated.get('priority')}")
    if updated.get("name") != "overwrite后的admin" or updated.get("priority") != 250:
        ok = False

    print("  --- 测试 report 策略 ---")
    r = api("POST", "/api/admin/source-rules/import", json={
        "rules": [
            {
                "code": "admin",
                "name": "report测试",
                "priority": 300,
                "is_enabled": True,
                "version": "1.0",
            }
        ],
        "conflict_strategy": "report",
    })

    print_test("report策略返回400", r.status_code == 400, f"状态码 {r.status_code}")
    if r.status_code != 400:
        ok = False
    else:
        detail = r.json().get("detail", {})
        print_test("report策略错误码正确", detail.get("code") == "SOURCE_RULE_IMPORT_ERROR",
                   f"code={detail.get('code')}")
        print_test("report策略有错误信息", len(detail.get("errors", [])) >= 1,
                   f"errors={detail.get('errors')}")
        if detail.get("code") != "SOURCE_RULE_IMPORT_ERROR" or len(detail.get("errors", [])) < 1:
            ok = False

    r = api("GET", "/api/admin/source-rules/admin")
    unchanged = r.json()
    print_test("report策略未修改数据", unchanged.get("priority") == 250,
               f"priority={unchanged.get('priority')} (应保持250)")
    if unchanged.get("priority") != 250:
        ok = False

    print_test("导入冲突策略总结果", ok)


def test_import_validation(ctx):
    print_section("测试 7: 导入数据验证")

    ok = True

    print("  --- 测试字段缺失 ---")
    r = api("POST", "/api/admin/source-rules/import", json={
        "rules": [
            {"name": "缺少code", "priority": 50},
            {"code": "has_code", "priority": 50},
        ],
        "conflict_strategy": "skip",
    })

    if r.status_code != 400:
        print_test("字段缺失返回400", False, f"状态码 {r.status_code}")
        ok = False
    else:
        detail = r.json().get("detail", {})
        print_test("有2条错误", detail.get("error_count") == 2,
                   f"error_count={detail.get('error_count')}")
        print_test("有错误详情", len(detail.get("errors", [])) == 2,
                   f"errors={len(detail.get('errors', []))}")
        if detail.get("error_count") != 2 or len(detail.get("errors", [])) != 2:
            ok = False

    print("  --- 测试版本不兼容 ---")
    r = api("POST", "/api/admin/source-rules/import", json={
        "rules": [
            {
                "code": "version_test",
                "name": "版本测试",
                "priority": 50,
                "version": "99.0",
            }
        ],
        "conflict_strategy": "skip",
    })

    if r.status_code != 400:
        print_test("版本不兼容返回400", False, f"状态码 {r.status_code}")
        ok = False
    else:
        detail = r.json().get("detail", {})
        print_test("版本错误被捕获", "不支持" in str(detail.get("errors", [])),
                   f"errors={detail.get('errors')}")
        if "不支持" not in str(detail.get("errors", [])):
            ok = False

    print("  --- 测试非法枚举值 ---")
    r = api("POST", "/api/admin/source-rules/import", json={
        "rules": [
            {
                "code": "enum_test",
                "name": "枚举测试",
                "priority": 50,
                "is_enabled": "yes",
                "version": "1.0",
            }
        ],
        "conflict_strategy": "skip",
    })

    if r.status_code != 400:
        print_test("非法枚举返回400", False, f"状态码 {r.status_code}")
        ok = False
    else:
        detail = r.json().get("detail", {})
        print_test("布尔值错误被捕获", "布尔值" in str(detail.get("errors", [])),
                   f"errors={detail.get('errors')}")
        if "布尔值" not in str(detail.get("errors", [])):
            ok = False

    print("  --- 测试无效冲突策略 ---")
    r = api("POST", "/api/admin/source-rules/import", json={
        "rules": [],
        "conflict_strategy": "invalid_strategy",
    })

    print_test("无效冲突策略返回400", r.status_code == 400, f"状态码 {r.status_code}")
    if r.status_code != 400:
        ok = False

    print("  --- 测试非数组数据 ---")
    r = api("POST", "/api/admin/source-rules/import", json={
        "rules": "not_an_array",
        "conflict_strategy": "skip",
    })

    if r.status_code != 400:
        print_test("非数组数据返回400", False, f"状态码 {r.status_code}")
        ok = False
    else:
        detail = r.json().get("detail", {})
        errors_str = str(detail.get("errors", []))
        print_test("数组类型错误被捕获", "list" in errors_str or "数组" in errors_str,
                   f"errors={detail.get('errors')}")
        if "list" not in errors_str and "数组" not in errors_str:
            ok = False

    print_test("导入数据验证总结果", ok)


def test_makeup_with_source_rules(ctx, process):
    print_section("测试 8: 补录时来源规则匹配与结构化日志")

    ok = True

    api("POST", "/api/admin/source-rules", json={
        "code": "log_test",
        "name": "日志测试来源",
        "description": "用于测试结构化日志",
        "priority": 70,
        "is_enabled": True,
    })

    r = api("POST", "/api/admin/orders/makeup", json={
        "employee_id": "SR001",
        "menu_item_id": ctx["item1_id"],
        "quantity": 1,
        "serving_date": ctx["today"],
        "source": "log_test",
        "remark": "来源规则日志测试",
    }, timeout=120)

    if r.status_code != 200:
        print_test("补录返回200", False, f"状态码 {r.status_code}: {r.json()}")
        ok = False
        return

    order = r.json()
    print_test("补录成功", order.get("status") == "taken", f"status={order.get('status')}")
    if order.get("status") != "taken":
        ok = False

    print_test("返回包含matched_source_rule", "matched_source_rule" in order,
               f"keys={list(order.keys())}")
    if "matched_source_rule" not in order:
        ok = False
    else:
        rule = order["matched_source_rule"]
        print_test("匹配规则code正确", rule.get("code") == "log_test",
                   f"code={rule.get('code')}")
        print_test("匹配规则name正确", rule.get("name") == "日志测试来源",
                   f"name={rule.get('name')}")
        print_test("匹配规则来源层级正确", rule.get("source_layer") == "runtime",
                   f"layer={rule.get('source_layer')}")
        if (rule.get("code") != "log_test" or
            rule.get("name") != "日志测试来源" or
            rule.get("source_layer") != "runtime"):
            ok = False

    r = api("GET", "/api/admin/orders/makeup", params={"source": "log_test"}, timeout=60)
    if r.status_code != 200:
        print_test("按来源筛选返回200", False, f"状态码 {r.status_code}")
        ok = False
    else:
        data = r.json()
        print_test("按来源筛选命中订单", data.get("total") >= 1,
                   f"total={data.get('total')}")
        if data.get("total") >= 1:
            item = data["items"][0]
            print_test("查询结果包含matched_source_rule",
                       "matched_source_rule" in item,
                       f"keys={list(item.keys())}")
            if "matched_source_rule" in item:
                rule = item["matched_source_rule"]
                print_test("查询结果规则信息正确",
                           rule.get("code") == "log_test",
                           f"code={rule.get('code')}")
                if rule.get("code") != "log_test":
                    ok = False

    r = api("POST", f"/api/admin/orders/makeup/{order['id']}/revoke",
            json={"remark": "测试撤销日志"})
    if r.status_code != 200:
        print_test("撤销返回200", False, f"状态码 {r.status_code}: {r.json()}")
        ok = False
    else:
        revoked = r.json()
        print_test("撤销返回包含matched_source_rule",
                   "matched_source_rule" in revoked,
                   f"keys={list(revoked.keys())}")
        if "matched_source_rule" in revoked:
            rule = revoked["matched_source_rule"]
            print_test("撤销时规则匹配正确",
                       rule.get("code") == "log_test",
                       f"code={rule.get('code')}")
            if rule.get("code") != "log_test":
                ok = False

    r = api("GET", "/api/admin/orders/makeup", params={"employee_id": "SR001"})
    items = r.json().get("items", [])
    if items:
        item = items[0]
        print_test("撤销后查询仍包含规则信息",
                   "matched_source_rule" in item,
                   f"keys={list(item.keys())}")
        if "matched_source_rule" in item:
            rule = item["matched_source_rule"]
            print_test("撤销后规则信息正确",
                       rule.get("code") == "log_test",
                       f"code={rule.get('code')}")
            if rule.get("code") != "log_test":
                ok = False

    reconc = api("GET", "/api/admin/reconciliation").json()
    print_test("对账一致", reconc["consistent"], f"Issues: {reconc.get('issues', [])}")
    if not reconc["consistent"]:
        ok = False

    print_test("补录来源规则匹配总结果", ok)
    return order


def test_pattern_matching(ctx):
    print_section("测试 9: 正则模式匹配")

    ok = True

    api("POST", "/api/admin/source-rules", json={
        "code": "pos_system",
        "name": "POS系统导入",
        "description": "匹配所有pos_开头的来源",
        "priority": 65,
        "is_enabled": True,
        "match_pattern": "^pos_.*",
    })

    r = api("POST", "/api/admin/orders/makeup", json={
        "employee_id": "SR002",
        "menu_item_id": ctx["item2_id"],
        "quantity": 1,
        "serving_date": ctx["today"],
        "source": "pos_terminal_1",
        "remark": "模式匹配测试",
    })

    if r.status_code != 200:
        print_test("模式匹配补录返回200", False, f"状态码 {r.status_code}: {r.json()}")
        ok = False
        return

    order = r.json()
    print_test("模式匹配成功", order.get("source") == "pos_terminal_1",
               f"source={order.get('source')}")
    if order.get("source") != "pos_terminal_1":
        ok = False

    if "matched_source_rule" in order:
        rule = order["matched_source_rule"]
        print_test("匹配的规则code正确", rule.get("code") == "pos_system",
                   f"code={rule.get('code')}")
        print_test("匹配的规则有pattern", rule.get("match_pattern") == "^pos_.*",
                   f"pattern={rule.get('match_pattern')}")
        if rule.get("code") != "pos_system" or rule.get("match_pattern") != "^pos_.*":
            ok = False

    r = api("GET", "/api/admin/orders/makeup", params={"source": "pos_terminal_1"})
    data = r.json()
    print_test("按实际来源值可筛选", data.get("total") >= 1,
               f"total={data.get('total')}")
    if data.get("total") < 1:
        ok = False

    reconc = api("GET", "/api/admin/reconciliation").json()
    print_test("对账一致", reconc["consistent"], f"Issues: {reconc.get('issues', [])}")
    if not reconc["consistent"]:
        ok = False

    print_test("正则模式匹配总结果", ok)


def test_reboot_consistency(ctx, process):
    print_section("测试 10: 重启后来源规则一致性")

    print("  记录重启前规则状态...")
    rules_before = api("GET", "/api/admin/source-rules").json()
    orders_before = api("GET", "/api/admin/orders/makeup").json()
    reconc_before = api("GET", "/api/admin/reconciliation").json()

    runtime_before = rules_before.get("layers", {}).get("runtime", [])
    runtime_codes_before = [r["code"] for r in runtime_before]
    print(f"  重启前运行时规则: {runtime_codes_before}")
    print(f"  重启前补录记录数: {orders_before['total']}")

    import_history_before = api("GET", "/api/admin/source-rules/import-history").json()
    import_count_before = len(import_history_before)
    print(f"  重启前导入历史记录数: {import_count_before}")

    stop_service(process)
    time.sleep(2)

    print("\n  重新启动服务...")

    env_rules = json.dumps([
        {
            "code": "env_test",
            "name": "环境变量测试来源",
            "priority": 95,
            "is_enabled": True,
        }
    ])

    process = start_service(extra_env={"CANTEEN_SOURCE_RULES": env_rules})
    wait_for_service()

    print("\n  验证重启后状态...")
    ok = True

    rules_after = api("GET", "/api/admin/source-rules").json()
    runtime_after = rules_after.get("layers", {}).get("runtime", [])
    runtime_codes_after = [r["code"] for r in runtime_after]

    print_test("重启后运行时规则一致",
               runtime_codes_before == runtime_codes_after,
               f"前: {runtime_codes_before}, 后: {runtime_codes_after}")
    if runtime_codes_before != runtime_codes_after:
        ok = False

    for rule_before in runtime_before:
        rule_after = next((r for r in runtime_after if r["code"] == rule_before["code"]), None)
        if rule_after:
            print_test(f"规则 {rule_before['code']} 优先级一致",
                       rule_before["priority"] == rule_after["priority"],
                       f"前: {rule_before['priority']}, 后: {rule_after['priority']}")
            print_test(f"规则 {rule_before['code']} 启用状态一致",
                       rule_before["is_enabled"] == rule_after["is_enabled"],
                       f"前: {rule_before['is_enabled']}, 后: {rule_after['is_enabled']}")
            if (rule_before["priority"] != rule_after["priority"] or
                rule_before["is_enabled"] != rule_after["is_enabled"]):
                ok = False

    env_after = rules_after.get("layers", {}).get("environment", [])
    print_test("重启后环境变量规则仍加载", len(env_after) == 1,
               f"环境规则数: {len(env_after)}")
    if len(env_after) != 1:
        ok = False

    orders_after = api("GET", "/api/admin/orders/makeup").json()
    print_test("重启后补录记录数一致",
               orders_before["total"] == orders_after["total"],
               f"前: {orders_before['total']}, 后: {orders_after['total']}")
    if orders_before["total"] != orders_after["total"]:
        ok = False

    import_history_after = api("GET", "/api/admin/source-rules/import-history").json()
    print_test("重启后导入历史一致",
               len(import_history_after) == import_count_before,
               f"前: {import_count_before}, 后: {len(import_history_after)}")
    if len(import_history_after) != import_count_before:
        ok = False

    for item in orders_after.get("items", []):
        if item.get("source") == "log_test":
            print_test("重启后订单仍包含规则信息",
                       "matched_source_rule" in item,
                       f"订单 {item['id']} 有matched_source_rule")
            if "matched_source_rule" not in item:
                ok = False

    reconc_after = api("GET", "/api/admin/reconciliation").json()
    print_test("重启后对账通过", reconc_after["consistent"],
               f"Issues: {reconc_after.get('issues', [])}")
    if not reconc_after["consistent"]:
        ok = False

    print_test("服务重启一致性总结果", ok)
    return process


def test_disabled_rule(ctx):
    print_section("测试 11: 禁用规则的影响")

    ok = True

    api("POST", "/api/admin/source-rules", json={
        "code": "to_disable",
        "name": "待禁用来源",
        "priority": 40,
        "is_enabled": True,
    })

    r = api("POST", "/api/admin/orders/makeup", json={
        "employee_id": "SR001",
        "menu_item_id": ctx["item2_id"],
        "quantity": 1,
        "serving_date": ctx["today"],
        "source": "to_disable",
        "remark": "禁用前测试",
    })
    print_test("启用时可使用该来源", r.status_code == 200, f"状态码 {r.status_code}")
    if r.status_code != 200:
        ok = False

    api("PATCH", "/api/admin/source-rules/to_disable", json={
        "is_enabled": False,
    })

    r = api("GET", "/api/admin/source-rules")
    allowed = r.json().get("allowed_sources", [])
    print_test("禁用后不在允许列表", "to_disable" not in allowed,
               f"to_disable in allowed: {'to_disable' in allowed}")
    if "to_disable" in allowed:
        ok = False

    r = api("POST", "/api/admin/orders/makeup", json={
        "employee_id": "SR002",
        "menu_item_id": ctx["item1_id"],
        "quantity": 1,
        "serving_date": ctx["today"],
        "source": "to_disable",
        "remark": "禁用后测试",
    })
    print_test("禁用后使用返回400", r.status_code == 400, f"状态码 {r.status_code}")
    if r.status_code != 400:
        ok = False

    r = api("GET", "/api/admin/orders/makeup", params={"source": "to_disable"})
    data = r.json()
    print_test("禁用后仍可按来源筛选历史记录", data.get("total") >= 1,
               f"total={data.get('total')}")
    if data.get("total") < 1:
        ok = False

    reconc = api("GET", "/api/admin/reconciliation").json()
    print_test("对账一致", reconc["consistent"], f"Issues: {reconc.get('issues', [])}")
    if not reconc["consistent"]:
        ok = False

    print_test("禁用规则影响总结果", ok)


def test_priority_override(ctx):
    print_section("测试 12: 优先级覆盖逻辑")

    ok = True

    api("POST", "/api/admin/source-rules", json={
        "code": "priority_test",
        "name": "低优先级运行时规则",
        "priority": 10,
        "is_enabled": True,
    })

    r = api("GET", "/api/admin/source-rules")
    data = r.json()

    merged = data.get("rules", [])
    window_rule = next((r for r in merged if r["code"] == "window"), None)

    if window_rule:
        print_test("高优先级环境规则仍然生效",
                   window_rule.get("priority") == 150,
                   f"priority={window_rule.get('priority')}")
        print_test("来源层级为environment",
                   window_rule.get("source_layer") == "environment",
                   f"layer={window_rule.get('source_layer')}")
        if window_rule.get("priority") != 150 or window_rule.get("source_layer") != "environment":
            ok = False

    rules_order = [r["code"] for r in merged]
    priorities = {r["code"]: r["priority"] for r in merged}
    window_idx = rules_order.index("window") if "window" in rules_order else -1
    admin_idx = rules_order.index("admin") if "admin" in rules_order else -1
    manual_idx = rules_order.index("manual") if "manual" in rules_order else -1

    is_sorted = True
    for i in range(len(merged) - 1):
        if merged[i]["priority"] < merged[i + 1]["priority"]:
            is_sorted = False
            break

    print_test("规则按优先级降序排列",
               is_sorted,
               f"顺序: {rules_order}, 优先级: {[priorities[c] for c in rules_order]}")
    if not is_sorted:
        ok = False

    print_test("优先级覆盖总结果", ok)


def main():
    global passed, failed

    print_section("来源规则系统完整回归测试")
    print(f"  测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    cleanup_existing_db()

    env_rules = json.dumps([
        {
            "code": "env_test",
            "name": "环境变量测试来源",
            "description": "通过环境变量配置的规则",
            "priority": 95,
            "is_enabled": True,
        },
        {
            "code": "window",
            "name": "环境变量覆盖的窗口",
            "priority": 150,
            "is_enabled": True,
        }
    ])

    process = None
    try:
        process = start_service(extra_env={"CANTEEN_SOURCE_RULES": env_rules})
        wait_for_service()

        ctx = setup_test_data()

        test_default_rules(ctx)
        test_environment_rules(ctx)
        test_runtime_rules_crud(ctx)
        test_rule_validation(ctx)
        test_import_export(ctx)
        test_import_conflict_strategies(ctx)
        test_import_validation(ctx)
        test_makeup_with_source_rules(ctx, process)
        test_pattern_matching(ctx)
        test_disabled_rule(ctx)
        test_priority_override(ctx)
        process = test_reboot_consistency(ctx, process)

    except Exception as e:
        print(f"\n  [FATAL] 测试执行出错: {e}")
        import traceback
        traceback.print_exc()
        failed += 1
    finally:
        if process and process.poll() is None:
            stop_service(process)
        cleanup_existing_db()

    print_section("测试总结")
    print(f"  [PASS] 通过: {passed}")
    print(f"  [FAIL] 失败: {failed}")
    print(f"  总计: {passed + failed}")
    print()

    if failed == 0:
        print("  所有测试通过！")
        sys.exit(0)
    else:
        print(f"  有 {failed} 个测试失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
