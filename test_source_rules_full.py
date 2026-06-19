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


def safe_json(response):
    try:
        return response.json()
    except Exception:
        return {"raw_text": response.text[:500]}


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
    api("POST", "/api/admin/employees", json={"id": "SR003", "name": "来源规则测试员工3", "initial_balance": 150.0})

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

    item3 = api("POST", f"/api/admin/menus/{menu['id']}/items", json={
        "name": "鱼香肉丝", "price": 22.0, "stock": 30,
    }).json()

    api("POST", f"/api/admin/menus/{menu['id']}/publish")

    api("PUT", "/api/admin/config/makeup_revoke_deadline_hours", json={"value": "0"})

    print(f"  今日菜单: {menu['id']}")
    print(f"  菜品1: 宫保鸡丁(20元, id={item1['id']})")
    print(f"  菜品2: 麻婆豆腐(15元, id={item2['id']})")
    print(f"  菜品3: 鱼香肉丝(22元, id={item3['id']})")

    return {
        "today": today,
        "menu_id": menu["id"],
        "item1_id": item1["id"],
        "item2_id": item2["id"],
        "item3_id": item3["id"],
    }


# ============================================================
# Test 1: Default rules loading
# ============================================================

def test_01_default_rules(ctx):
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

    default_rules = data.get("layers", {}).get("default", [])
    print_test("默认层级有5条规则", len(default_rules) == 5, f"count={len(default_rules)}")
    if len(default_rules) != 5:
        ok = False

    default_codes = [r["code"] for r in default_rules]
    for expected_code in ["window", "admin", "manual", "phone_order", "pos"]:
        has_code = expected_code in default_codes
        print_test(f"默认规则包含 {expected_code}", has_code, f"codes={default_codes}")
        if not has_code:
            ok = False

    categories = data.get("categories", {})
    for expected_cat in ["general", "system", "import", "remote"]:
        has_cat = expected_cat in categories
        print_test(f"分类包含 {expected_cat}", has_cat, f"categories={list(categories.keys())}")
        if not has_cat:
            ok = False

    allowed_sources = data.get("allowed_sources", [])
    for expected_source in ["window", "admin", "manual", "phone_order", "pos"]:
        has_source = expected_source in allowed_sources
        print_test(f"allowed_sources 包含 {expected_source}", has_source, f"allowed={allowed_sources}")
        if not has_source:
            ok = False

    merged = data.get("rules", [])
    is_sorted = True
    for i in range(len(merged) - 1):
        if merged[i]["priority"] < merged[i + 1]["priority"]:
            is_sorted = False
            break
    print_test("合并规则按优先级降序排列", is_sorted,
               f"顺序: {[r['code'] for r in merged]}, 优先级: {[r['priority'] for r in merged]}")
    if not is_sorted:
        ok = False

    print_test("默认规则加载总结果", ok)


# ============================================================
# Test 2: Environment variable rules merge
# ============================================================

def test_02_environment_rules(ctx):
    print_section("测试 2: 环境变量规则合并")

    r = api("GET", "/api/admin/source-rules")
    data = r.json()

    ok = True

    env_rules_list = data.get("layers", {}).get("environment", [])
    print_test("环境变量层级有2条规则", len(env_rules_list) == 2, f"count={len(env_rules_list)}")
    if len(env_rules_list) != 2:
        ok = False

    env_codes = [r["code"] for r in env_rules_list]
    print_test("环境变量包含 env_test", "env_test" in env_codes, f"env_codes={env_codes}")
    print_test("环境变量包含 window", "window" in env_codes, f"env_codes={env_codes}")
    if "env_test" not in env_codes or "window" not in env_codes:
        ok = False

    merged_rules = data.get("rules", [])
    window_rule = next((r for r in merged_rules if r["code"] == "window"), None)
    if window_rule:
        print_test("合并后window优先级为150",
                   window_rule["priority"] == 150,
                   f"priority={window_rule['priority']}, layer={window_rule.get('source_layer')}")
        print_test("合并后window来自environment层",
                   window_rule.get("source_layer") == "environment",
                   f"layer={window_rule.get('source_layer')}")
        if window_rule["priority"] != 150 or window_rule.get("source_layer") != "environment":
            ok = False

    env_test_rule = next((r for r in merged_rules if r["code"] == "env_test"), None)
    if env_test_rule:
        print_test("env_test规则存在于合并列表", env_test_rule is not None)
        print_test("env_test规则名称正确", env_test_rule.get("name") == "环境变量测试来源",
                   f"name={env_test_rule.get('name')}")
        if env_test_rule.get("name") != "环境变量测试来源":
            ok = False

    allowed_sources = data.get("allowed_sources", [])
    print_test("allowed_sources 包含 env_test", "env_test" in allowed_sources, f"allowed={allowed_sources}")
    if "env_test" not in allowed_sources:
        ok = False

    print_test("环境变量规则合并总结果", ok)


# ============================================================
# Test 3: Runtime rules CRUD
# ============================================================

def test_03_runtime_rules_crud(ctx):
    print_section("测试 3: 运行时规则 CRUD")

    ok = True

    r = api("POST", "/api/admin/source-rules", json={
        "code": "runtime_new",
        "name": "运行时新增来源",
        "description": "通过API新增的规则",
        "category": "general",
        "priority": 75,
        "is_enabled": True,
    })

    if r.status_code != 200:
        print_test("创建规则返回200", False, f"状态码 {r.status_code}: {safe_json(r)}")
        ok = False
        return

    created = r.json()
    print_test("规则创建成功", created.get("code") == "runtime_new", f"code={created.get('code')}")
    if created.get("code") != "runtime_new":
        ok = False

    print_test("返回规则ID", created.get("id") is not None, f"id={created.get('id')}")
    if created.get("id") is None:
        ok = False

    print_test("规则包含category字段", created.get("category") == "general", f"category={created.get('category')}")
    if created.get("category") != "general":
        ok = False

    print_test("规则包含priority字段", created.get("priority") == 75, f"priority={created.get('priority')}")
    if created.get("priority") != 75:
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
        print_test("更新规则返回200", False, f"状态码 {r.status_code}: {safe_json(r)}")
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
    print_test("禁用的规则不在allowed_sources中", "runtime_new" not in allowed,
               f"runtime_new in allowed: {'runtime_new' in allowed}")
    if "runtime_new" in allowed:
        ok = False

    r = api("DELETE", "/api/admin/source-rules/runtime_new")
    if r.status_code != 200:
        print_test("删除规则返回200", False, f"状态码 {r.status_code}: {safe_json(r)}")
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


# ============================================================
# Test 4: Rule validation
# ============================================================

def test_04_rule_validation(ctx):
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
        "code": "test_empty_name",
        "name": "",
        "priority": 50,
    })
    print_test("空name返回400", r.status_code == 400, f"状态码 {r.status_code}")
    if r.status_code != 400:
        ok = False

    r = api("POST", "/api/admin/source-rules", json={
        "code": "test_bad_priority",
        "name": "无效优先级",
        "priority": 1500,
    })
    print_test("优先级超出范围返回400", r.status_code == 400, f"状态码 {r.status_code}")
    if r.status_code != 400:
        ok = False

    r = api("POST", "/api/admin/source-rules", json={
        "code": "test_bad_regex",
        "name": "无效正则",
        "priority": 50,
        "match_pattern": "[invalid",
    })
    print_test("无效正则返回400", r.status_code == 400, f"状态码 {r.status_code}")
    if r.status_code != 400:
        ok = False

    r = api("POST", "/api/admin/source-rules", json={
        "code": "test_bad_category",
        "name": "无效类别",
        "priority": 50,
        "category": "nonexistent_category",
    })
    print_test("无效category返回400", r.status_code == 400, f"状态码 {r.status_code}")
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

    r = api("GET", "/api/admin/source-rules/nonexistent_rule")
    print_test("查询不存在的规则返回404", r.status_code == 404, f"状态码 {r.status_code}")
    if r.status_code != 404:
        ok = False

    r = api("PATCH", "/api/admin/source-rules/nonexistent_rule", json={"name": "test"})
    print_test("更新不存在的规则返回404", r.status_code == 404, f"状态码 {r.status_code}")
    if r.status_code != 404:
        ok = False

    r = api("DELETE", "/api/admin/source-rules/nonexistent_rule")
    print_test("删除不存在的规则返回404", r.status_code == 404, f"状态码 {r.status_code}")
    if r.status_code != 404:
        ok = False

    print_test("规则数据验证总结果", ok)


# ============================================================
# Test 5: Export with all layers
# ============================================================

def test_05_export_all_layers(ctx):
    print_section("测试 5: 导出包含所有层级")

    ok = True

    r = api("GET", "/api/admin/source-rules/export/json", params={"include_all_layers": "false"})
    if r.status_code != 200:
        print_test("不含层级导出返回200", False, f"状态码 {r.status_code}")
        ok = False
    else:
        data = r.json()
        print_test("不含层级导出有version", data.get("version") == "1.0", f"version={data.get('version')}")
        print_test("不含层级导出有rules数组", isinstance(data.get("rules"), list))
        print_test("不含层级导出无layers", "layers" not in data or data.get("include_all_layers") == False,
                   f"has_layers={'layers' in data}")
        if data.get("version") != "1.0" or not isinstance(data.get("rules"), list):
            ok = False

    r = api("GET", "/api/admin/source-rules/export/json", params={"include_all_layers": "true"})
    if r.status_code != 200:
        print_test("含层级导出返回200", False, f"状态码 {r.status_code}")
        ok = False
    else:
        data = r.json()
        print_test("含层级导出有layers字段", "layers" in data, f"keys={list(data.keys())}")
        if "layers" not in data:
            ok = False
        else:
            layers = data["layers"]
            print_test("layers包含default", "default" in layers, f"layer_keys={list(layers.keys())}")
            print_test("layers包含environment", "environment" in layers, f"layer_keys={list(layers.keys())}")
            print_test("layers包含runtime", "runtime" in layers, f"layer_keys={list(layers.keys())}")
            if not all(k in layers for k in ["default", "environment", "runtime"]):
                ok = False

            print_test("default层有5条规则",
                       isinstance(layers.get("default"), list) and len(layers["default"]) == 5,
                       f"count={len(layers.get('default', []))}")
            if not isinstance(layers.get("default"), list) or len(layers["default"]) != 5:
                ok = False

            print_test("include_all_layers标志为true",
                       data.get("include_all_layers") == True,
                       f"include_all_layers={data.get('include_all_layers')}")
            if data.get("include_all_layers") != True:
                ok = False

    print_test("导出包含所有层级总结果", ok)


# ============================================================
# Test 6: Import with conflict strategies
# ============================================================

def test_06_import_conflict_strategies(ctx):
    print_section("测试 6: 导入冲突处理策略")

    ok = True

    print("  --- 测试 skip 策略 (与admin冲突) ---")
    r = api("POST", "/api/admin/source-rules/import", json={
        "rules": [
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
        ],
        "conflict_strategy": "skip",
    })

    if r.status_code != 200:
        print_test("skip策略导入返回200", False, f"状态码 {r.status_code}: {safe_json(r)}")
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

        conflict = result.get("conflicts", [{}])[0] if result.get("conflicts") else {}
        print_test("skip冲突有existing_rule信息", "existing_rule" in conflict,
                   f"keys={list(conflict.keys())}")
        print_test("skip冲突有incoming_rule信息", "incoming_rule" in conflict,
                   f"keys={list(conflict.keys())}")
        if "existing_rule" not in conflict or "incoming_rule" not in conflict:
            ok = False

        if "existing_rule" in conflict:
            existing = conflict["existing_rule"]
            print_test("existing_rule包含code", "code" in existing, f"existing={existing}")
            print_test("existing_rule的code是admin", existing.get("code") == "admin",
                       f"code={existing.get('code')}")
            if "code" not in existing or existing.get("code") != "admin":
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
        print_test("overwrite策略导入返回200", False, f"状态码 {r.status_code}: {safe_json(r)}")
        ok = False
    else:
        result = r.json()
        print_test("overwrite策略成功1条", result.get("success_count") == 1,
                   f"success_count={result.get('success_count')}")
        print_test("overwrite策略有冲突记录", len(result.get("conflicts", [])) == 1,
                   f"conflicts={len(result.get('conflicts', []))}")
        if result.get("success_count") != 1 or len(result.get("conflicts", [])) != 1:
            ok = False

        conflict = result.get("conflicts", [{}])[0] if result.get("conflicts") else {}
        print_test("overwrite冲突有existing_rule", "existing_rule" in conflict,
                   f"keys={list(conflict.keys())}")
        print_test("overwrite冲突有incoming_rule", "incoming_rule" in conflict,
                   f"keys={list(conflict.keys())}")
        if "existing_rule" not in conflict or "incoming_rule" not in conflict:
            ok = False

    r = api("GET", "/api/admin/source-rules/admin")
    if r.status_code == 200:
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

        conflicts = detail.get("conflicts", [])
        print_test("report响应包含conflicts", len(conflicts) >= 1,
                   f"conflicts_count={len(conflicts)}")
        if conflicts:
            conflict = conflicts[0]
            print_test("report冲突有existing_rule", "existing_rule" in conflict,
                       f"keys={list(conflict.keys())}")
            print_test("report冲突有incoming_rule", "incoming_rule" in conflict,
                       f"keys={list(conflict.keys())}")
            if "existing_rule" not in conflict or "incoming_rule" not in conflict:
                ok = False

    r = api("GET", "/api/admin/source-rules/admin")
    if r.status_code == 200:
        unchanged = r.json()
        print_test("report策略未修改数据", unchanged.get("priority") == 250,
                   f"priority={unchanged.get('priority')} (应保持250)")
        if unchanged.get("priority") != 250:
            ok = False

    print_test("导入冲突策略总结果", ok)


# ============================================================
# Test 7: Import validation
# ============================================================

def test_07_import_validation(ctx):
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
                   f"errors_count={len(detail.get('errors', []))}")
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


# ============================================================
# Test 8: Import duplicate detection against merged rules
# ============================================================

def test_08_import_duplicate_merged(ctx):
    print_section("测试 8: 导入重复检测（合并规则层面）")

    ok = True

    r = api("POST", "/api/admin/source-rules/import", json={
        "rules": [
            {
                "code": "window",
                "name": "导入的window",
                "priority": 50,
                "is_enabled": True,
                "version": "1.0",
            }
        ],
        "conflict_strategy": "skip",
    })

    if r.status_code != 200:
        print_test("skip策略返回200", False, f"状态码 {r.status_code}: {safe_json(r)}")
        ok = False
    else:
        result = r.json()
        print_test("skip策略跳过1条", result.get("skipped_count") == 1,
                   f"skipped_count={result.get('skipped_count')}")
        if result.get("skipped_count") != 1:
            ok = False

        conflicts = result.get("conflicts", [])
        print_test("有1条冲突记录", len(conflicts) == 1, f"count={len(conflicts)}")
        if len(conflicts) != 1:
            ok = False

        if conflicts:
            conflict = conflicts[0]
            existing = conflict.get("existing_rule", {})
            print_test("existing_rule有source_layer", "source_layer" in existing,
                       f"existing={existing}")
            if "source_layer" in existing:
                print_test("检测到default层冲突",
                           existing.get("source_layer") in ("default", "environment"),
                           f"source_layer={existing.get('source_layer')}")
                if existing.get("source_layer") not in ("default", "environment"):
                    ok = False
            else:
                ok = False

    print_test("导入重复检测（合并规则层面）总结果", ok)


# ============================================================
# Test 9: Import dry-run mode
# ============================================================

def test_09_import_dry_run(ctx):
    print_section("测试 9: 导入 dry-run 模式")

    ok = True

    r_before = api("GET", "/api/admin/source-rules")
    rules_before = r_before.json()
    runtime_before = len(rules_before.get("layers", {}).get("runtime", []))

    r = api("POST", "/api/admin/source-rules/import", json={
        "rules": [
            {
                "code": "dry_run_test",
                "name": "Dry Run测试规则",
                "priority": 30,
                "is_enabled": True,
                "version": "1.0",
            }
        ],
        "conflict_strategy": "skip",
        "dry_run": True,
    })

    if r.status_code != 200:
        print_test("dry_run导入返回200", False, f"状态码 {r.status_code}: {safe_json(r)}")
        ok = False
    else:
        result = r.json()
        print_test("dry_run标志为true", result.get("dry_run") == True,
                   f"dry_run={result.get('dry_run')}")
        print_test("dry_run成功1条", result.get("success_count") == 1,
                   f"success_count={result.get('success_count')}")
        if result.get("dry_run") != True or result.get("success_count") != 1:
            ok = False

        imported = result.get("imported_rules", [])
        if imported:
            print_test("dry_run预览规则有dry_run标志",
                       imported[0].get("dry_run") == True,
                       f"rule={imported[0]}")
            if imported[0].get("dry_run") != True:
                ok = False

    r_after = api("GET", "/api/admin/source-rules")
    rules_after = r_after.json()
    runtime_after = len(rules_after.get("layers", {}).get("runtime", []))

    print_test("dry_run后runtime规则数未增加",
               runtime_after == runtime_before,
               f"before={runtime_before}, after={runtime_after}")
    if runtime_after != runtime_before:
        ok = False

    merged = rules_after.get("rules", [])
    dry_run_exists = any(r.get("code") == "dry_run_test" for r in merged)
    print_test("dry_run_test未出现在合并规则中", not dry_run_exists,
               f"dry_run_test in merged: {dry_run_exists}")
    if dry_run_exists:
        ok = False

    print_test("导入 dry-run 模式总结果", ok)


# ============================================================
# Test 10: Makeup order with source rules
# ============================================================

def test_10_makeup_with_source_rules(ctx):
    print_section("测试 10: 补录时来源规则匹配")

    ok = True

    api("POST", "/api/admin/source-rules", json={
        "code": "log_test",
        "name": "日志测试来源",
        "description": "用于测试结构化日志",
        "category": "general",
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
        print_test("补录返回200", False, f"状态码 {r.status_code}: {safe_json(r)}")
        ok = False
        return None

    order = r.json()
    print_test("补录成功状态为taken", order.get("status") == "taken", f"status={order.get('status')}")
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
        print_test("匹配规则category正确", "category" in rule,
                   f"category={rule.get('category')}")
        print_test("匹配规则source_layer为runtime", rule.get("source_layer") == "runtime",
                   f"layer={rule.get('source_layer')}")
        if (rule.get("code") != "log_test" or
            rule.get("name") != "日志测试来源" or
            "category" not in rule or
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
        print_test("撤销返回200", False, f"状态码 {r.status_code}: {safe_json(r)}")
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


# ============================================================
# Test 11: Pattern matching
# ============================================================

def test_11_pattern_matching(ctx):
    print_section("测试 11: 正则模式匹配")

    ok = True

    api("POST", "/api/admin/source-rules", json={
        "code": "pos_system",
        "name": "POS系统导入",
        "description": "匹配所有pos_开头的来源",
        "category": "system",
        "priority": 95,
        "is_enabled": True,
        "match_pattern": "^pos_terminal_.*",
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
        print_test("模式匹配补录返回200", False, f"状态码 {r.status_code}: {safe_json(r)}")
        ok = False
        return

    order = r.json()
    print_test("模式匹配成功source为pos_terminal_1", order.get("source") == "pos_terminal_1",
               f"source={order.get('source')}")
    if order.get("source") != "pos_terminal_1":
        ok = False

    if "matched_source_rule" in order:
        rule = order["matched_source_rule"]
        print_test("匹配的规则code为pos_system", rule.get("code") == "pos_system",
                   f"code={rule.get('code')}")
        print_test("匹配的规则有match_pattern", rule.get("match_pattern") == "^pos_terminal_.*",
                   f"pattern={rule.get('match_pattern')}")
        print_test("匹配的规则category为system", rule.get("category") == "system",
                   f"category={rule.get('category')}")
        if (rule.get("code") != "pos_system" or
            rule.get("match_pattern") != "^pos_terminal_.*" or
            rule.get("category") != "system"):
            ok = False

    r = api("GET", "/api/admin/orders/makeup", params={"source": "pos_terminal_1"})
    data = r.json()
    print_test("按实际来源值pos_terminal_1可筛选", data.get("total") >= 1,
               f"total={data.get('total')}")
    if data.get("total") < 1:
        ok = False

    reconc = api("GET", "/api/admin/reconciliation").json()
    print_test("对账一致", reconc["consistent"], f"Issues: {reconc.get('issues', [])}")
    if not reconc["consistent"]:
        ok = False

    print_test("正则模式匹配总结果", ok)


# ============================================================
# Test 12: Auto source detection
# ============================================================

def test_12_auto_source_detection(ctx):
    print_section("测试 12: 自动来源检测")

    ok = True

    r = api("POST", "/api/admin/orders/makeup", json={
        "employee_id": "SR003",
        "menu_item_id": ctx["item3_id"],
        "quantity": 1,
        "serving_date": ctx["today"],
        "remark": "自动来源检测测试",
    })

    if r.status_code != 200:
        print_test("无source补录返回200", False, f"状态码 {r.status_code}: {safe_json(r)}")
        ok = False
        return

    order = r.json()
    print_test("自动检测source为window", order.get("source") == "window",
               f"source={order.get('source')}")
    if order.get("source") != "window":
        ok = False

    print_test("返回包含matched_source_rule", "matched_source_rule" in order,
               f"keys={list(order.keys())}")
    if "matched_source_rule" not in order:
        ok = False
    else:
        rule = order["matched_source_rule"]
        print_test("自动匹配规则code为window", rule.get("code") == "window",
                   f"code={rule.get('code')}")
        if rule.get("code") != "window":
            ok = False

    reconc = api("GET", "/api/admin/reconciliation").json()
    print_test("对账一致", reconc["consistent"], f"Issues: {reconc.get('issues', [])}")
    if not reconc["consistent"]:
        ok = False

    print_test("自动来源检测总结果", ok)


# ============================================================
# Test 13: Disabled rule blocking
# ============================================================

def test_13_disabled_rule_blocking(ctx):
    print_section("测试 13: 禁用规则阻止新补录")

    ok = True

    api("POST", "/api/admin/source-rules", json={
        "code": "to_disable",
        "name": "待禁用来源",
        "category": "general",
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
        print(f"    响应: {safe_json(r)}")
        ok = False

    api("PATCH", "/api/admin/source-rules/to_disable", json={
        "is_enabled": False,
    })

    r = api("GET", "/api/admin/source-rules")
    allowed = r.json().get("allowed_sources", [])
    print_test("禁用后不在allowed_sources中", "to_disable" not in allowed,
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

    print_test("禁用规则阻止新补录总结果", ok)


# ============================================================
# Test 14: Priority override
# ============================================================

def test_14_priority_override(ctx):
    print_section("测试 14: 优先级覆盖逻辑")

    ok = True

    api("POST", "/api/admin/source-rules", json={
        "code": "priority_test",
        "name": "低优先级运行时规则",
        "category": "general",
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

    is_sorted = True
    for i in range(len(merged) - 1):
        if merged[i]["priority"] < merged[i + 1]["priority"]:
            is_sorted = False
            break

    rules_order = [r["code"] for r in merged]
    priorities = {r["code"]: r["priority"] for r in merged}
    print_test("规则按优先级降序排列",
               is_sorted,
               f"顺序: {rules_order}, 优先级: {[priorities[c] for c in rules_order]}")
    if not is_sorted:
        ok = False

    print_test("优先级覆盖总结果", ok)


# ============================================================
# Test 15: Reboot consistency
# ============================================================

def test_15_reboot_consistency(ctx, process):
    print_section("测试 15: 重启后来源规则一致性")

    print("  记录重启前规则状态...")
    rules_before = api("GET", "/api/admin/source-rules").json()
    orders_before = api("GET", "/api/admin/orders/makeup").json()
    reconc_before = api("GET", "/api/admin/reconciliation").json()
    audit_before = api("GET", "/api/admin/source-rules/audit-log").json()

    runtime_before = rules_before.get("layers", {}).get("runtime", [])
    runtime_codes_before = [r["code"] for r in runtime_before]
    print(f"  重启前运行时规则: {runtime_codes_before}")
    print(f"  重启前补录记录数: {orders_before['total']}")
    print(f"  重启前审计日志数: {len(audit_before)}")

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
    print_test("重启后环境变量规则仍加载", len(env_after) == 2,
               f"环境规则数: {len(env_after)}")
    if len(env_after) != 2:
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

    audit_after = api("GET", "/api/admin/source-rules/audit-log").json()
    print_test("重启后审计日志数一致",
               len(audit_after) == len(audit_before),
               f"前: {len(audit_before)}, 后: {len(audit_after)}")
    if len(audit_after) != len(audit_before):
        ok = False

    for item in orders_after.get("items", []):
        if item.get("source") in ("log_test", "pos_terminal_1", "window"):
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


# ============================================================
# Test 16: Audit log
# ============================================================

def test_16_audit_log(ctx):
    print_section("测试 16: 审计日志")

    ok = True

    r = api("POST", "/api/admin/source-rules", json={
        "code": "audit_test",
        "name": "审计测试规则",
        "category": "general",
        "priority": 55,
        "is_enabled": True,
    })

    if r.status_code != 200:
        print_test("创建审计测试规则", False, f"状态码 {r.status_code}: {safe_json(r)}")
        ok = False
        return

    r = api("PATCH", "/api/admin/source-rules/audit_test", json={
        "name": "审计测试规则-已更新",
        "priority": 60,
    })

    if r.status_code != 200:
        print_test("更新审计测试规则", False, f"状态码 {r.status_code}: {safe_json(r)}")
        ok = False
        return

    r = api("GET", "/api/admin/source-rules/audit-log", params={"rule_code": "audit_test"})
    if r.status_code != 200:
        print_test("获取审计日志返回200", False, f"状态码 {r.status_code}")
        ok = False
        return

    logs = r.json()
    print_test("审计日志至少2条", len(logs) >= 2, f"count={len(logs)}")
    if len(logs) < 2:
        ok = False

    create_log = next((l for l in logs if l.get("operation") == "create"), None)
    update_log = next((l for l in logs if l.get("operation") == "update"), None)

    if create_log:
        print_test("create日志有after数据", create_log.get("after") is not None,
                   f"after={create_log.get('after')}")
        if create_log.get("after") is None:
            ok = False
    else:
        print_test("找到create日志", False, "未找到create操作日志")
        ok = False

    if update_log:
        print_test("update日志有before数据", update_log.get("before") is not None,
                   f"before_keys={list(update_log.get('before', {}).keys()) if isinstance(update_log.get('before'), dict) else 'N/A'}")
        print_test("update日志有after数据", update_log.get("after") is not None,
                   f"after_keys={list(update_log.get('after', {}).keys()) if isinstance(update_log.get('after'), dict) else 'N/A'}")
        if update_log.get("before") is None or update_log.get("after") is None:
            ok = False

        after_data = update_log.get("after", {})
        if isinstance(after_data, dict):
            print_test("update后名称正确", after_data.get("name") == "审计测试规则-已更新",
                       f"name={after_data.get('name')}")
            print_test("update后优先级正确", after_data.get("priority") == 60,
                       f"priority={after_data.get('priority')}")
            if after_data.get("name") != "审计测试规则-已更新" or after_data.get("priority") != 60:
                ok = False
    else:
        print_test("找到update日志", False, "未找到update操作日志")
        ok = False

    api("DELETE", "/api/admin/source-rules/audit_test")

    print_test("审计日志总结果", ok)


# ============================================================
# Test 17: End-to-end chain
# ============================================================

def test_17_e2e_chain(ctx):
    print_section("测试 17: 端到端链路")

    ok = True

    r = api("POST", "/api/admin/orders/makeup", json={
        "employee_id": "SR002",
        "menu_item_id": ctx["item3_id"],
        "quantity": 1,
        "serving_date": ctx["today"],
        "source": "admin",
        "remark": "E2E链路测试",
    })

    if r.status_code != 200:
        print_test("admin来源补录返回200", False, f"状态码 {r.status_code}: {safe_json(r)}")
        ok = False
        return

    order = r.json()
    order_id = order["id"]

    print_test("订单source为admin", order.get("source") == "admin",
               f"source={order.get('source')}")
    print_test("订单status为taken", order.get("status") == "taken",
               f"status={order.get('status')}")
    if order.get("source") != "admin" or order.get("status") != "taken":
        ok = False

    r = api("GET", "/api/admin/orders/makeup", params={"source": "admin"})
    if r.status_code != 200:
        print_test("按source=admin查询返回200", False, f"状态码 {r.status_code}")
        ok = False
    else:
        data = r.json()
        found = any(item.get("id") == order_id for item in data.get("items", []))
        print_test("查询到admin来源的订单", found, f"total={data.get('total')}")
        if not found:
            ok = False

    r = api("POST", f"/api/admin/orders/makeup/{order_id}/revoke",
            json={"remark": "E2E链路撤销"})
    if r.status_code != 200:
        print_test("撤销返回200", False, f"状态码 {r.status_code}: {safe_json(r)}")
        ok = False
    else:
        revoked = r.json()
        print_test("撤销后status为cancelled", revoked.get("status") == "cancelled",
                   f"status={revoked.get('status')}")
        print_test("撤销后source仍为admin", revoked.get("source") == "admin",
                   f"source={revoked.get('source')}")
        print_test("撤销返回包含matched_source_rule", "matched_source_rule" in revoked,
                   f"keys={list(revoked.keys())}")
        if (revoked.get("status") != "cancelled" or
            revoked.get("source") != "admin"):
            ok = False

    r = api("GET", "/api/admin/orders/makeup", params={"source": "admin"})
    if r.status_code == 200:
        data = r.json()
        target_items = [i for i in data.get("items", []) if i.get("id") == order_id]
        if target_items:
            item = target_items[0]
            print_test("撤销后查询source仍为admin", item.get("source") == "admin",
                       f"source={item.get('source')}")
            print_test("撤销后查询status为cancelled", item.get("status") == "cancelled",
                       f"status={item.get('status')}")
            print_test("撤销后查询仍包含matched_source_rule", "matched_source_rule" in item,
                       f"keys={list(item.keys())}")
            if item.get("source") != "admin" or item.get("status") != "cancelled":
                ok = False

            txns = item.get("transactions", [])
            txn_types = [t.get("type") for t in txns]
            print_test("流水包含MAKEUP_REVOKE", "MAKEUP_REVOKE" in txn_types,
                       f"types={txn_types}")
            if "MAKEUP_REVOKE" not in txn_types:
                ok = False

            logs = item.get("operation_logs", [])
            log_types = [l.get("operation_type") for l in logs]
            print_test("操作日志包含create", "create" in log_types,
                       f"types={log_types}")
            print_test("操作日志包含revoke", "revoke" in log_types,
                       f"types={log_types}")
            if "create" not in log_types or "revoke" not in log_types:
                ok = False

    reconc = api("GET", "/api/admin/reconciliation").json()
    print_test("对账一致", reconc["consistent"], f"Issues: {reconc.get('issues', [])}")
    if not reconc["consistent"]:
        ok = False

    print_test("端到端链路总结果", ok)


# ============================================================
# Main
# ============================================================

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

        test_01_default_rules(ctx)
        test_02_environment_rules(ctx)
        test_03_runtime_rules_crud(ctx)
        test_04_rule_validation(ctx)
        test_05_export_all_layers(ctx)
        test_06_import_conflict_strategies(ctx)
        test_07_import_validation(ctx)
        test_08_import_duplicate_merged(ctx)
        test_09_import_dry_run(ctx)
        test_10_makeup_with_source_rules(ctx)
        test_11_pattern_matching(ctx)
        test_12_auto_source_detection(ctx)
        test_13_disabled_rule_blocking(ctx)
        test_14_priority_override(ctx)
        test_16_audit_log(ctx)
        test_17_e2e_chain(ctx)
        process = test_15_reboot_consistency(ctx, process)

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
