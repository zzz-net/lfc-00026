import requests
import json
import os
import sys
import time
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path

BASE_URL = "http://127.0.0.1:8010"
TEST_DB = Path(__file__).parent / "canteen_test_batch_maintenance.db"
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


def start_service():
    print_section("启动测试服务")

    env = os.environ.copy()
    env["CANTEEN_DB_PATH"] = str(TEST_DB)

    cmd = [
        sys.executable, "-m", "uvicorn", "main:app",
        "--host", "127.0.0.1",
        "--port", "8010",
    ]

    print(f"  命令: {' '.join(cmd)}")
    print(f"  数据库: {TEST_DB}")
    print(f"  服务地址: {BASE_URL}")
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
    log_file = SCRIPT_DIR / "test_batch_maintenance_server.log"
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

    api("POST", "/api/admin/employees", json={"id": "BM001", "name": "批量维护测试员工1", "initial_balance": 500.0})
    api("POST", "/api/admin/employees", json={"id": "BM002", "name": "批量维护测试员工2", "initial_balance": 300.0})
    api("POST", "/api/admin/employees", json={"id": "BM003", "name": "批量维护测试员工3", "initial_balance": 200.0})

    menu = api("POST", "/api/admin/menus", json={
        "name": f"批量维护测试菜单-{today}",
        "serving_date": today,
        "deadline": deadline,
    }).json()

    item1 = api("POST", f"/api/admin/menus/{menu['id']}/items", json={
        "name": "红烧肉", "price": 25.0, "stock": 100,
    }).json()

    item2 = api("POST", f"/api/admin/menus/{menu['id']}/items", json={
        "name": "清蒸鲈鱼", "price": 35.0, "stock": 50,
    }).json()

    item3 = api("POST", f"/api/admin/menus/{menu['id']}/items", json={
        "name": "蒜蓉西兰花", "price": 12.0, "stock": 80,
    }).json()

    api("POST", f"/api/admin/menus/{menu['id']}/publish")

    api("PUT", "/api/admin/config/makeup_revoke_deadline_hours", json={"value": "0"})

    print(f"  今日菜单: {menu['id']}")
    print(f"  菜品1: 红烧肉(25元, id={item1['id']})")
    print(f"  菜品2: 清蒸鲈鱼(35元, id={item2['id']})")
    print(f"  菜品3: 蒜蓉西兰花(12元, id={item3['id']})")

    return {
        "today": today,
        "menu_id": menu["id"],
        "item1_id": item1["id"],
        "item2_id": item2["id"],
        "item3_id": item3["id"],
    }


# ============================================================
# Test 1: Dry-run preview with clear categorization
# ============================================================

def test_01_dry_run_preview(ctx):
    print_section("测试 1: Dry-run 预检与分类明细")

    ok = True

    import_rules = [
        {
            "code": "batch_new_1",
            "name": "批量导入新来源1",
            "description": "测试dry-run新增",
            "category": "import",
            "priority": 60,
            "is_enabled": True,
            "version": "1.0",
        },
        {
            "code": "batch_new_2",
            "name": "批量导入新来源2",
            "description": "测试dry-run新增",
            "category": "system",
            "priority": 55,
            "is_enabled": True,
            "version": "1.0",
        },
        {
            "code": "admin",
            "name": "dry-run尝试覆盖admin",
            "priority": 200,
            "is_enabled": True,
            "version": "1.0",
        },
        {
            "code": "window",
            "name": "dry-run尝试覆盖window（default层）",
            "priority": 180,
            "is_enabled": True,
            "version": "1.0",
        },
        {
            "code": "invalid_rule",
            "priority": 50,
        },
    ]

    r = api("POST", "/api/admin/source-rules/import/dry-run", json={
        "rules": import_rules,
        "conflict_strategy": "skip",
    }, headers={"X-Operator": "test_operator_001"})

    if r.status_code != 200:
        print_test("Dry-run 返回200", False, f"状态码 {r.status_code}: {safe_json(r)}")
        ok = False
        return ok

    result = r.json()

    print_test("dry_run标志为True", result.get("dry_run") == True,
               f"dry_run={result.get('dry_run')}")
    if result.get("dry_run") != True:
        ok = False

    print_test("包含result_summary", "result_summary" in result,
               f"summary={result.get('result_summary')}")
    if "result_summary" not in result:
        ok = False

    print_test("包含new_count字段", "new_count" in result,
               f"new_count={result.get('new_count')}")
    print_test("new_count为2", result.get("new_count") == 2,
               f"new_count={result.get('new_count')}")
    if result.get("new_count") != 2:
        ok = False

    print_test("包含overwritten_count字段", "overwritten_count" in result,
               f"overwritten_count={result.get('overwritten_count')}")

    print_test("包含skipped_count字段", "skipped_count" in result,
               f"skipped_count={result.get('skipped_count')}")
    print_test("skipped_count为2", result.get("skipped_count") == 2,
               f"skipped_count={result.get('skipped_count')}")
    if result.get("skipped_count") != 2:
        ok = False

    print_test("包含error_count字段", "error_count" in result,
               f"error_count={result.get('error_count')}")
    print_test("error_count为1", result.get("error_count") == 1,
               f"error_count={result.get('error_count')}")
    if result.get("error_count") != 1:
        ok = False

    print_test("包含disabled_blocked_count字段", "disabled_blocked_count" in result,
               f"disabled_blocked_count={result.get('disabled_blocked_count')}")

    print_test("包含new_rules数组", isinstance(result.get("new_rules"), list),
               f"new_rules类型={type(result.get('new_rules'))}")
    print_test("new_rules有2条", len(result.get("new_rules", [])) == 2,
               f"count={len(result.get('new_rules', []))}")
    if len(result.get("new_rules", [])) != 2:
        ok = False

    new_codes = [r.get("code") for r in result.get("new_rules", [])]
    print_test("new_rules包含batch_new_1", "batch_new_1" in new_codes, f"codes={new_codes}")
    print_test("new_rules包含batch_new_2", "batch_new_2" in new_codes, f"codes={new_codes}")
    if "batch_new_1" not in new_codes or "batch_new_2" not in new_codes:
        ok = False

    for nr in result.get("new_rules", []):
        print_test(f"new_rule[{nr.get('code')}]有would_create动作",
                   nr.get("action") == "would_create",
                   f"action={nr.get('action')}")
        if nr.get("action") != "would_create":
            ok = False

    print_test("包含skipped_rules数组", isinstance(result.get("skipped_rules"), list),
               f"skipped_rules类型={type(result.get('skipped_rules'))}")
    print_test("skipped_rules有2条", len(result.get("skipped_rules", [])) == 2,
               f"count={len(result.get('skipped_rules', []))}")
    if len(result.get("skipped_rules", [])) != 2:
        ok = False

    skipped_codes = [r.get("code") for r in result.get("skipped_rules", [])]
    print_test("skipped_rules包含admin", "admin" in skipped_codes, f"codes={skipped_codes}")
    print_test("skipped_rules包含window", "window" in skipped_codes, f"codes={skipped_codes}")
    if "admin" not in skipped_codes or "window" not in skipped_codes:
        ok = False

    for sr in result.get("skipped_rules", []):
        print_test(f"skipped_rule[{sr.get('code')}]有skipped动作",
                   sr.get("action") == "skipped",
                   f"action={sr.get('action')}")
        if sr.get("action") != "skipped":
            ok = False

        print_test(f"skipped_rule[{sr.get('code')}]有existing_rule信息",
                   "existing_rule" in sr and sr["existing_rule"] is not None,
                   f"existing_rule={sr.get('existing_rule')}")
        if "existing_rule" not in sr or sr["existing_rule"] is None:
            ok = False

        if sr.get("code") == "window":
            existing = sr.get("existing_rule", {})
            print_test("window的existing_rule包含source_layer",
                       "source_layer" in existing,
                       f"source_layer={existing.get('source_layer')}")
            print_test("window的source_layer为default或runtime",
                       existing.get("source_layer") in ("default", "runtime"),
                       f"source_layer={existing.get('source_layer')}")

    print_test("包含invalid_rules数组", isinstance(result.get("invalid_rules"), list),
               f"invalid_rules类型={type(result.get('invalid_rules'))}")
    print_test("invalid_rules有1条", len(result.get("invalid_rules", [])) == 1,
               f"count={len(result.get('invalid_rules', []))}")
    if len(result.get("invalid_rules", [])) != 1:
        ok = False

    print_test("包含conflict_rules数组", isinstance(result.get("conflict_rules"), list),
               f"conflict_rules类型={type(result.get('conflict_rules'))}")

    print_test("包含operator字段", result.get("operator") == "test_operator_001",
               f"operator={result.get('operator')}")
    if result.get("operator") != "test_operator_001":
        ok = False

    r_rules = api("GET", "/api/admin/source-rules")
    rules_after = r_rules.json()
    runtime_rules = rules_after.get("layers", {}).get("runtime", [])
    runtime_codes = [r["code"] for r in runtime_rules]

    print_test("Dry-run后runtime规则未变化",
               "batch_new_1" not in runtime_codes and "batch_new_2" not in runtime_codes,
               f"runtime_codes={runtime_codes}")
    if "batch_new_1" in runtime_codes or "batch_new_2" in runtime_codes:
        ok = False

    r_history = api("GET", "/api/admin/source-rules/import-history")
    history = r_history.json()
    print_test("Dry-run未写入导入历史", len(history) == 0,
               f"history_count={len(history)}")
    if len(history) != 0:
        ok = False

    print_test("Dry-run预检总结果", ok)
    return ok


# ============================================================
# Test 2: Formal import with overwrite strategy
# ============================================================

def test_02_formal_import_overwrite(ctx):
    print_section("测试 2: 正式导入（覆盖策略）")

    ok = True

    api("POST", "/api/admin/source-rules", json={
        "code": "to_overwrite",
        "name": "待覆盖的规则",
        "description": "将被导入覆盖",
        "category": "general",
        "priority": 10,
        "is_enabled": True,
    })

    import_rules = [
        {
            "code": "import_new_1",
            "name": "正式导入新来源1",
            "description": "正式导入新增测试",
            "category": "import",
            "priority": 70,
            "is_enabled": True,
            "version": "1.0",
        },
        {
            "code": "to_overwrite",
            "name": "已被覆盖的规则",
            "description": "已通过导入更新",
            "category": "system",
            "priority": 80,
            "is_enabled": True,
            "version": "1.0",
        },
    ]

    r = api("POST", "/api/admin/source-rules/import", json={
        "rules": import_rules,
        "conflict_strategy": "overwrite",
        "dry_run": False,
    }, headers={"X-Operator": "admin_zhang"})

    if r.status_code != 200:
        print_test("正式导入返回200", False, f"状态码 {r.status_code}: {safe_json(r)}")
        ok = False
        return ok

    result = r.json()

    print_test("dry_run标志为False", result.get("dry_run") == False,
               f"dry_run={result.get('dry_run')}")
    if result.get("dry_run") != False:
        ok = False

    print_test("success为True", result.get("success") == True,
               f"success={result.get('success')}")
    if result.get("success") != True:
        ok = False

    print_test("new_count为1", result.get("new_count") == 1,
               f"new_count={result.get('new_count')}")
    print_test("overwritten_count为1", result.get("overwritten_count") == 1,
               f"overwritten_count={result.get('overwritten_count')}")
    print_test("success_count为2", result.get("success_count") == 2,
               f"success_count={result.get('success_count')}")
    if result.get("new_count") != 1 or result.get("overwritten_count") != 1 or result.get("success_count") != 2:
        ok = False

    print_test("包含import_id", result.get("import_id") is not None,
               f"import_id={result.get('import_id')}")
    if result.get("import_id") is None:
        ok = False

    import_id = result.get("import_id")

    print_test("new_rules有1条", len(result.get("new_rules", [])) == 1,
               f"count={len(result.get('new_rules', []))}")
    print_test("overwritten_rules有1条", len(result.get("overwritten_rules", [])) == 1,
               f"count={len(result.get('overwritten_rules', []))}")
    if len(result.get("new_rules", [])) != 1 or len(result.get("overwritten_rules", [])) != 1:
        ok = False

    new_rule = result.get("new_rules", [{}])[0]
    print_test("new_rule action为created", new_rule.get("action") == "created",
               f"action={new_rule.get('action')}")
    print_test("new_rule有after数据", "after" in new_rule,
               f"has_after={'after' in new_rule}")
    if new_rule.get("action") != "created" or "after" not in new_rule:
        ok = False

    overwritten_rule = result.get("overwritten_rules", [{}])[0]
    print_test("overwritten_rule action为overwritten",
               overwritten_rule.get("action") == "overwritten",
               f"action={overwritten_rule.get('action')}")
    print_test("overwritten_rule有before数据", "before" in overwritten_rule,
               f"has_before={'before' in overwritten_rule}")
    print_test("overwritten_rule有after数据", "after" in overwritten_rule,
               f"has_after={'after' in overwritten_rule}")
    if (overwritten_rule.get("action") != "overwritten" or
        "before" not in overwritten_rule or
        "after" not in overwritten_rule):
        ok = False

    print_test("operator为admin_zhang", result.get("operator") == "admin_zhang",
               f"operator={result.get('operator')}")
    if result.get("operator") != "admin_zhang":
        ok = False

    if import_id:
        r_audit = api("GET", f"/api/admin/source-rules/audit-log?import_id={import_id}")
        if r_audit.status_code == 200:
            audit_entries = r_audit.json()
            print_test("审计日志按import_id过滤有记录",
                       len(audit_entries) >= 2,
                       f"audit_count={len(audit_entries)}")
            if len(audit_entries) < 2:
                ok = False

            audit_codes = [e.get("rule_code") for e in audit_entries]
            print_test("审计日志包含import_new_1",
                       "import_new_1" in audit_codes,
                       f"codes={audit_codes}")
            print_test("审计日志包含to_overwrite",
                       "to_overwrite" in audit_codes,
                       f"codes={audit_codes}")
            if "import_new_1" not in audit_codes or "to_overwrite" not in audit_codes:
                ok = False

            for entry in audit_entries:
                print_test(f"审计[{entry.get('rule_code')}]import_id匹配",
                           entry.get("import_id") == import_id,
                           f"import_id={entry.get('import_id')}")
                if entry.get("import_id") != import_id:
                    ok = False

    r_rules = api("GET", "/api/admin/source-rules")
    rules_after = r_rules.json()
    merged_rules = rules_after.get("rules", [])
    merged_codes = [r["code"] for r in merged_rules]

    print_test("新规则import_new_1已生效", "import_new_1" in merged_codes,
               f"merged_codes={merged_codes}")
    if "import_new_1" not in merged_codes:
        ok = False

    for r in merged_rules:
        if r["code"] == "to_overwrite":
            print_test("覆盖后规则名称已更新", r["name"] == "已被覆盖的规则",
                       f"name={r['name']}")
            print_test("覆盖后优先级已更新", r["priority"] == 80,
                       f"priority={r['priority']}")
            print_test("覆盖后category已更新", r["category"] == "system",
                       f"category={r['category']}")
            if r["name"] != "已被覆盖的规则" or r["priority"] != 80 or r["category"] != "system":
                ok = False

    allowed = rules_after.get("allowed_sources", [])
    print_test("新规则在allowed_sources中", "import_new_1" in allowed,
               f"allowed={allowed}")
    print_test("覆盖后规则在allowed_sources中", "to_overwrite" in allowed,
               f"allowed={allowed}")
    if "import_new_1" not in allowed or "to_overwrite" not in allowed:
        ok = False

    r_history = api("GET", "/api/admin/source-rules/import-history")
    history = r_history.json()
    print_test("导入历史有1条记录", len(history) == 1,
               f"history_count={len(history)}")
    if len(history) != 1:
        ok = False

    r_history_by_op = api("GET", "/api/admin/source-rules/import-history", params={"operator": "admin_zhang"})
    if r_history_by_op.status_code == 200:
        history_by_op = r_history_by_op.json()
        print_test("按operator过滤导入历史有结果",
                   len(history_by_op) >= 1,
                   f"count={len(history_by_op)}")
        if len(history_by_op) < 1:
            ok = False

    if history:
        latest = history[0]
        print_test("导入历史包含operator", latest.get("operator") == "admin_zhang",
                   f"operator={latest.get('operator')}")
        print_test("导入历史包含new_count", latest.get("new_count") == 1,
                   f"new_count={latest.get('new_count')}")
        print_test("导入历史包含overwritten_count", latest.get("overwritten_count") == 1,
                   f"overwritten_count={latest.get('overwritten_count')}")
        print_test("导入历史包含summary", "summary" in latest,
                   f"has_summary={'summary' in latest}")
        if (latest.get("operator") != "admin_zhang" or
            latest.get("new_count") != 1 or
            latest.get("overwritten_count") != 1 or
            "summary" not in latest):
            ok = False

        if "summary" in latest:
            summary = latest["summary"]
            print_test("summary包含effective_rules", "effective_rules" in summary,
                       f"keys={list(summary.keys())}")
            print_test("effective_rules包含2条", len(summary.get("effective_rules", [])) == 2,
                       f"effective_rules={summary.get('effective_rules')}")
            if "effective_rules" not in summary or len(summary.get("effective_rules", [])) != 2:
                ok = False

        import_id = latest.get("id")
        r_detail = api("GET", f"/api/admin/source-rules/import-history/{import_id}")
        if r_detail.status_code == 200:
            detail = r_detail.json()
            print_test("导入详情包含details", "details" in detail,
                       f"has_details={'details' in detail}")
            if "details" in detail:
                print_test("details包含effective_rules",
                           "effective_rules" in detail["details"],
                           f"detail_keys={list(detail['details'].keys())}")

    print_test("正式导入（覆盖策略）总结果", ok)
    return ok


# ============================================================
# Test 3: Disabled rule blocking detection
# ============================================================

def test_03_disabled_rule_blocking(ctx):
    print_section("测试 3: 禁用规则拦截检测")

    ok = True

    api("POST", "/api/admin/source-rules", json={
        "code": "to_disable_import",
        "name": "将被禁用的来源",
        "category": "general",
        "priority": 40,
        "is_enabled": True,
    })

    r_before = api("GET", "/api/admin/source-rules")
    allowed_before = r_before.json().get("allowed_sources", [])
    print_test("禁用前to_disable_import在allowed_sources中",
               "to_disable_import" in allowed_before,
               f"allowed_before={allowed_before}")

    import_rules = [
        {
            "code": "to_disable_import",
            "name": "已被禁用的来源",
            "priority": 45,
            "is_enabled": False,
            "version": "1.0",
        },
    ]

    r = api("POST", "/api/admin/source-rules/import", json={
        "rules": import_rules,
        "conflict_strategy": "overwrite",
    }, headers={"X-Operator": "admin_li"})

    if r.status_code != 200:
        print_test("导入禁用规则返回200", False, f"状态码 {r.status_code}: {safe_json(r)}")
        ok = False
        return ok

    result = r.json()

    print_test("disabled_blocked_count为1", result.get("disabled_blocked_count") == 1,
               f"disabled_blocked_count={result.get('disabled_blocked_count')}")
    if result.get("disabled_blocked_count") != 1:
        ok = False

    print_test("disabled_blocked_rules有1条", len(result.get("disabled_blocked_rules", [])) == 1,
               f"count={len(result.get('disabled_blocked_rules', []))}")
    if len(result.get("disabled_blocked_rules", [])) != 1:
        ok = False

    blocked = result.get("disabled_blocked_rules", [{}])[0]
    print_test("blocked规则code正确", blocked.get("code") == "to_disable_import",
               f"code={blocked.get('code')}")
    print_test("blocked有impact说明", "impact" in blocked,
               f"impact={blocked.get('impact')}")
    print_test("blocked有incoming_rule", "incoming_rule" in blocked,
               f"has_incoming={'incoming_rule' in blocked}")
    print_test("blocked有existing_rule", "existing_rule" in blocked,
               f"has_existing={'existing_rule' in blocked}")
    if (blocked.get("code") != "to_disable_import" or
        "impact" not in blocked or
        "incoming_rule" not in blocked or
        "existing_rule" not in blocked):
        ok = False

    if "existing_rule" in blocked:
        print_test("existing_rule.is_enabled为True",
                   blocked["existing_rule"].get("is_enabled") == True,
                   f"is_enabled={blocked['existing_rule'].get('is_enabled')}")
        if blocked["existing_rule"].get("is_enabled") != True:
            ok = False

    if "incoming_rule" in blocked:
        print_test("incoming_rule.is_enabled为False",
                   blocked["incoming_rule"].get("is_enabled") == False,
                   f"is_enabled={blocked['incoming_rule'].get('is_enabled')}")
        if blocked["incoming_rule"].get("is_enabled") != False:
            ok = False

    r_after = api("GET", "/api/admin/source-rules")
    allowed_after = r_after.json().get("allowed_sources", [])
    print_test("导入后to_disable_import不在allowed_sources中",
               "to_disable_import" not in allowed_after,
               f"allowed_after={allowed_after}")
    if "to_disable_import" in allowed_after:
        ok = False

    r_makeup = api("POST", "/api/admin/orders/makeup", json={
        "employee_id": "BM001",
        "menu_item_id": ctx["item1_id"],
        "quantity": 1,
        "serving_date": ctx["today"],
        "source": "to_disable_import",
        "remark": "测试禁用规则拦截",
    })

    print_test("禁用后补录返回400", r_makeup.status_code == 400,
               f"状态码 {r_makeup.status_code}")
    if r_makeup.status_code != 400:
        ok = False
    else:
        detail = safe_json(r_makeup).get("detail", {})
        print_test("错误码为DISABLED_SOURCE_RULE",
                   detail.get("code") == "DISABLED_SOURCE_RULE",
                   f"code={detail.get('code')}")
        if detail.get("code") != "DISABLED_SOURCE_RULE":
            ok = False

    print_test("禁用规则拦截检测总结果", ok)
    return ok


# ============================================================
# Test 4: Source hit makeup after import
# ============================================================

def test_04_source_hit_after_import(ctx):
    print_section("测试 4: 导入后来源命中补录")

    ok = True

    import_rules = [
        {
            "code": "batch_channel",
            "name": "批量渠道补录",
            "description": "通过批量导入新增的渠道",
            "category": "import",
            "priority": 65,
            "is_enabled": True,
            "version": "1.0",
        },
        {
            "code": "auto_detect_test",
            "name": "自动检测测试",
            "description": "测试模式匹配",
            "category": "system",
            "priority": 75,
            "is_enabled": True,
            "match_pattern": "^auto_.*",
            "version": "1.0",
        },
    ]

    r = api("POST", "/api/admin/source-rules/import", json={
        "rules": import_rules,
        "conflict_strategy": "skip",
    }, headers={"X-Operator": "operator_wang"})

    if r.status_code != 200:
        print_test("导入测试来源返回200", False, f"状态码 {r.status_code}: {safe_json(r)}")
        ok = False
        return ok

    result = r.json()
    print_test("新增2条成功", result.get("new_count") == 2,
               f"new_count={result.get('new_count')}")
    if result.get("new_count") != 2:
        ok = False

    r1 = api("POST", "/api/admin/orders/makeup", json={
        "employee_id": "BM001",
        "menu_item_id": ctx["item1_id"],
        "quantity": 1,
        "serving_date": ctx["today"],
        "source": "batch_channel",
        "remark": "测试精确匹配",
    })

    if r1.status_code != 200:
        print_test("精确匹配补录返回200", False, f"状态码 {r1.status_code}: {safe_json(r1)}")
        ok = False
    else:
        order1 = r1.json()
        print_test("精确匹配补录状态为taken", order1.get("status") == "taken",
                   f"status={order1.get('status')}")
        print_test("返回包含matched_source_rule", "matched_source_rule" in order1,
                   f"keys={list(order1.keys())}")
        if order1.get("status") != "taken" or "matched_source_rule" not in order1:
            ok = False
        else:
            rule = order1["matched_source_rule"]
            print_test("匹配规则code正确", rule.get("code") == "batch_channel",
                       f"code={rule.get('code')}")
            print_test("匹配规则name正确", rule.get("name") == "批量渠道补录",
                       f"name={rule.get('name')}")
            print_test("匹配规则source_layer为runtime", rule.get("source_layer") == "runtime",
                       f"layer={rule.get('source_layer')}")
            if (rule.get("code") != "batch_channel" or
                rule.get("name") != "批量渠道补录" or
                rule.get("source_layer") != "runtime"):
                ok = False

    r2 = api("POST", "/api/admin/orders/makeup", json={
        "employee_id": "BM002",
        "menu_item_id": ctx["item2_id"],
        "quantity": 1,
        "serving_date": ctx["today"],
        "source": "auto_channel_001",
        "remark": "测试模式匹配",
    })

    if r2.status_code != 200:
        print_test("模式匹配补录返回200", False, f"状态码 {r2.status_code}: {safe_json(r2)}")
        ok = False
    else:
        order2 = r2.json()
        print_test("模式匹配补录状态为taken", order2.get("status") == "taken",
                   f"status={order2.get('status')}")
        print_test("返回包含matched_source_rule", "matched_source_rule" in order2,
                   f"keys={list(order2.keys())}")
        if order2.get("status") != "taken" or "matched_source_rule" not in order2:
            ok = False
        else:
            rule = order2["matched_source_rule"]
            print_test("匹配规则code正确", rule.get("code") == "auto_detect_test",
                       f"code={rule.get('code')}")
            print_test("匹配规则有match_pattern", rule.get("match_pattern") == "^auto_.*",
                       f"pattern={rule.get('match_pattern')}")
            if (rule.get("code") != "auto_detect_test" or
                rule.get("match_pattern") != "^auto_.*"):
                ok = False

    r_q1 = api("GET", "/api/admin/orders/makeup", params={"source": "batch_channel"})
    if r_q1.status_code != 200:
        print_test("按batch_channel筛选返回200", False, f"状态码 {r_q1.status_code}")
        ok = False
    else:
        data1 = r_q1.json()
        print_test("按batch_channel筛选命中记录", data1.get("total", 0) >= 1,
                   f"total={data1.get('total')}")
        if data1.get("total", 0) < 1:
            ok = False
        else:
            item = data1["items"][0]
            print_test("查询结果包含matched_source_rule",
                       "matched_source_rule" in item,
                       f"keys={list(item.keys())}")
            if "matched_source_rule" in item:
                print_test("查询结果规则信息正确",
                           item["matched_source_rule"].get("code") == "batch_channel",
                           f"code={item['matched_source_rule'].get('code')}")

    r_q2 = api("GET", "/api/admin/orders/makeup", params={"source": "auto_channel_001"})
    if r_q2.status_code != 200:
        print_test("按auto_channel_001筛选返回200", False, f"状态码 {r_q2.status_code}")
        ok = False
    else:
        data2 = r_q2.json()
        print_test("按实际来源值筛选命中记录", data2.get("total", 0) >= 1,
                   f"total={data2.get('total')}")
        if data2.get("total", 0) < 1:
            ok = False

    reconc = api("GET", "/api/admin/reconciliation").json()
    print_test("对账一致", reconc["consistent"], f"Issues: {reconc.get('issues', [])}")
    if not reconc["consistent"]:
        ok = False

    print_test("导入后来源命中补录总结果", ok)
    return ok


# ============================================================
# Test 5: Revoke and continue tracking by source
# ============================================================

def test_05_revoke_and_track_by_source(ctx):
    print_section("测试 5: 撤销后继续按来源追查")

    ok = True

    r = api("POST", "/api/admin/orders/makeup", json={
        "employee_id": "BM003",
        "menu_item_id": ctx["item3_id"],
        "quantity": 2,
        "serving_date": ctx["today"],
        "source": "import_new_1",
        "remark": "测试撤销后追查",
    })

    if r.status_code != 200:
        print_test("创建待撤销补录返回200", False, f"状态码 {r.status_code}: {safe_json(r)}")
        ok = False
        return ok

    order = r.json()
    order_id = order["id"]
    print_test("补录创建成功source为import_new_1", order.get("source") == "import_new_1",
               f"source={order.get('source')}")
    if order.get("source") != "import_new_1":
        ok = False

    r_revoke = api("POST", f"/api/admin/orders/makeup/{order_id}/revoke",
                   json={"remark": "测试撤销来源追查"})

    if r_revoke.status_code != 200:
        print_test("撤销返回200", False, f"状态码 {r_revoke.status_code}: {safe_json(r_revoke)}")
        ok = False
    else:
        revoked = r_revoke.json()
        print_test("撤销后status为cancelled", revoked.get("status") == "cancelled",
                   f"status={revoked.get('status')}")
        print_test("撤销后source保留", revoked.get("source") == "import_new_1",
                   f"source={revoked.get('source')}")
        print_test("撤销后包含matched_source_rule", "matched_source_rule" in revoked,
                   f"keys={list(revoked.keys())}")
        if (revoked.get("status") != "cancelled" or
            revoked.get("source") != "import_new_1" or
            "matched_source_rule" not in revoked):
            ok = False
        else:
            rule = revoked["matched_source_rule"]
            print_test("撤销时规则匹配正确", rule.get("code") == "import_new_1",
                       f"code={rule.get('code')}")
            if rule.get("code") != "import_new_1":
                ok = False

    r_q = api("GET", "/api/admin/orders/makeup", params={"source": "import_new_1"})
    if r_q.status_code != 200:
        print_test("撤销后按来源筛选返回200", False, f"状态码 {r_q.status_code}")
        ok = False
    else:
        data = r_q.json()
        print_test("撤销后仍可按来源筛选到记录", data.get("total", 0) >= 1,
                   f"total={data.get('total')}")
        if data.get("total", 0) < 1:
            ok = False
        else:
            target = None
            for item in data.get("items", []):
                if item["id"] == order_id:
                    target = item
                    break
            if target:
                print_test("撤销后记录source保留", target.get("source") == "import_new_1",
                           f"source={target.get('source')}")
                print_test("撤销后记录status为cancelled", target.get("status") == "cancelled",
                           f"status={target.get('status')}")
                print_test("撤销后记录包含revoked_at", target.get("revoked_at") is not None,
                           f"revoked_at={target.get('revoked_at')}")
                print_test("撤销后记录包含matched_source_rule",
                           "matched_source_rule" in target,
                           f"keys={list(target.keys())}")
                if (target.get("source") != "import_new_1" or
                    target.get("status") != "cancelled" or
                    target.get("revoked_at") is None or
                    "matched_source_rule" not in target):
                    ok = False

                if "matched_source_rule" in target:
                    print_test("撤销后规则信息仍可追查",
                               target["matched_source_rule"].get("code") == "import_new_1",
                               f"code={target['matched_source_rule'].get('code')}")

                logs = target.get("operation_logs", [])
                has_create = any(log["operation_type"] == "create" for log in logs)
                has_revoke = any(log["operation_type"] == "revoke" for log in logs)
                print_test("操作日志包含create", has_create)
                print_test("操作日志包含revoke", has_revoke)
                if not has_create or not has_revoke:
                    ok = False

                txns = target.get("transactions", [])
                txn_types = [t["type"] for t in txns]
                print_test("流水包含MAKEUP_REVOKE", "MAKEUP_REVOKE" in txn_types,
                           f"types={txn_types}")
                if "MAKEUP_REVOKE" not in txn_types:
                    ok = False

    reconc = api("GET", "/api/admin/reconciliation").json()
    print_test("对账一致", reconc["consistent"], f"Issues: {reconc.get('issues', [])}")
    if not reconc["consistent"]:
        ok = False

    print_test("撤销后继续按来源追查总结果", ok)
    return ok


# ============================================================
# Test 6: Export fields completeness
# ============================================================

def test_06_export_fields_completeness(ctx):
    print_section("测试 6: 导出字段完整性")

    ok = True

    r_json = api("GET", "/api/admin/source-rules/export/json", params={"only_enabled": "false"})
    if r_json.status_code != 200:
        print_test("JSON导出返回200", False, f"状态码 {r_json.status_code}")
        ok = False
    else:
        data = r_json.json()
        print_test("JSON导出包含version", "version" in data, f"keys={list(data.keys())}")
        print_test("JSON导出包含exported_at", "exported_at" in data,
                   f"keys={list(data.keys())}")
        print_test("JSON导出包含only_enabled", "only_enabled" in data,
                   f"keys={list(data.keys())}")
        if "version" not in data or "exported_at" not in data or "only_enabled" not in data:
            ok = False

        rules = data.get("rules", [])
        if rules:
            r = rules[0]
            expected_fields = ["code", "name", "description", "category", "priority",
                              "is_enabled", "match_pattern", "version", "created_at", "updated_at"]
            for field in expected_fields:
                has_field = field in r
                print_test(f"JSON导出包含{field}", has_field,
                           f"rule_keys={list(r.keys())}")
                if not has_field:
                    ok = False

    r_csv = api("GET", "/api/admin/source-rules/export/csv", params={"only_enabled": "false"})
    if r_csv.status_code != 200:
        print_test("CSV导出返回200", False, f"状态码 {r_csv.status_code}")
        ok = False
    else:
        content = r_csv.text
        print_test("CSV有内容", len(content) > 0, f"length={len(content)}")
        if len(content) == 0:
            ok = False

        header_line = content.split("\n")[0]
        expected_headers = ["code", "name", "description", "category", "priority",
                           "is_enabled", "match_pattern", "version", "created_at", "updated_at"]
        for h in expected_headers:
            has_header = h in header_line
            print_test(f"CSV表头包含{h}", has_header, f"header={header_line}")
            if not has_header:
                ok = False

        print_test("CSV Content-Type正确",
                   r_csv.headers.get("Content-Type", "").startswith("text/csv"),
                   f"Content-Type={r_csv.headers.get('Content-Type')}")
        print_test("CSV Content-Disposition包含文件名",
                   "Content-Disposition" in r_csv.headers,
                   f"headers={list(r_csv.headers.keys())}")

    print_test("导出字段完整性总结果", ok)
    return ok


# ============================================================
# Test 7: Report conflict strategy
# ============================================================

def test_07_report_conflict_strategy(ctx):
    print_section("测试 7: Report 冲突策略")

    ok = True

    import_rules = [
        {
            "code": "admin",
            "name": "尝试覆盖admin（report策略）",
            "priority": 200,
            "is_enabled": True,
            "version": "1.0",
        },
        {
            "code": "report_new_1",
            "name": "Report策略新增规则",
            "category": "import",
            "priority": 50,
            "is_enabled": True,
            "version": "1.0",
        },
    ]

    r = api("POST", "/api/admin/source-rules/import", json={
        "rules": import_rules,
        "conflict_strategy": "report",
    }, headers={"X-Operator": "test_report_op"})

    if r.status_code != 400:
        print_test("report策略冲突返回400", False, f"状态码 {r.status_code}: {safe_json(r)}")
        ok = False
        return ok

    detail = safe_json(r)
    detail_data = detail.get("detail", detail)
    print_test("返回SOURCE_RULE_IMPORT_ERROR",
               detail_data.get("code") == "SOURCE_RULE_IMPORT_ERROR",
               f"code={detail_data.get('code')}")
    if detail_data.get("code") != "SOURCE_RULE_IMPORT_ERROR":
        ok = False

    r_rules = api("GET", "/api/admin/source-rules")
    merged = r_rules.json().get("rules", [])
    merged_codes = [r["code"] for r in merged]

    print_test("admin规则未被覆盖",
               any(r["code"] == "admin" and r["name"] == "管理员后台" for r in merged),
               f"admin_in_merged={'admin' in merged_codes}")
    print_test("report_new_1未被创建",
               "report_new_1" not in merged_codes,
               f"report_new_1_in_merged={'report_new_1' in merged_codes}")
    if "report_new_1" in merged_codes:
        ok = False

    print_test("Report冲突策略总结果", ok)
    return ok


# ============================================================
# Test 8: Audit import_id cross-reference and operator filter
# ============================================================

def test_08_audit_import_id_crossref(ctx):
    print_section("测试 8: 审计日志 import_id 交叉追查与 operator 过滤")

    ok = True

    r_history = api("GET", "/api/admin/source-rules/import-history")
    history = r_history.json()
    print_test("导入历史有记录", len(history) >= 1,
               f"count={len(history)}")
    if len(history) < 1:
        ok = False
        print_test("审计日志import_id追查总结果", ok)
        return ok

    first_import = history[0]
    import_id = first_import.get("id")
    operator_val = first_import.get("operator")
    print(f"  最早导入批次: import_id={import_id}, operator={operator_val}")

    r_audit = api("GET", f"/api/admin/source-rules/audit-log?import_id={import_id}")
    if r_audit.status_code != 200:
        print_test("按import_id查审计日志返回200", False, f"状态码 {r_audit.status_code}")
        ok = False
    else:
        audit_entries = r_audit.json()
        print_test(f"import_id={import_id}关联审计日志有{len(audit_entries)}条",
                   len(audit_entries) >= 1,
                   f"count={len(audit_entries)}")
        if len(audit_entries) < 1:
            ok = False

        for entry in audit_entries:
            has_import_id = entry.get("import_id") == import_id
            print_test(f"审计[{entry.get('rule_code')}]import_id={import_id}",
                       has_import_id,
                       f"actual={entry.get('import_id')}")
            if not has_import_id:
                ok = False

    if operator_val:
        r_history_op = api("GET", "/api/admin/source-rules/import-history",
                           params={"operator": operator_val})
        if r_history_op.status_code == 200:
            op_history = r_history_op.json()
            print_test(f"按operator={operator_val}过滤有结果",
                       len(op_history) >= 1,
                       f"count={len(op_history)}")
            for h in op_history:
                print_test(f"operator匹配",
                           h.get("operator") == operator_val,
                           f"operator={h.get('operator')}")
                if h.get("operator") != operator_val:
                    ok = False
        else:
            print_test("按operator过滤返回200", False, f"状态码 {r_history_op.status_code}")
            ok = False

    r_audit_all = api("GET", "/api/admin/source-rules/audit-log")
    if r_audit_all.status_code == 200:
        all_audit = r_audit_all.json()
        with_import_id = [e for e in all_audit if e.get("import_id") is not None]
        print_test("审计日志中有关联import_id的记录",
                   len(with_import_id) >= 1,
                   f"with_import_id={len(with_import_id)}, total={len(all_audit)}")
        if len(with_import_id) < 1:
            ok = False

    print_test("审计日志import_id追查总结果", ok)
    return ok


# ============================================================
# Test 9: Reboot persistence
# =======================================================================

def test_09_reboot_persistence(ctx, process):
    print_section("测试 9: 重启后规则和导入记录持久化")

    print("  记录重启前状态...")
    rules_before = api("GET", "/api/admin/source-rules").json()
    orders_before = api("GET", "/api/admin/orders/makeup").json()
    history_before = api("GET", "/api/admin/source-rules/import-history").json()
    reconc_before = api("GET", "/api/admin/reconciliation").json()

    runtime_before = rules_before.get("layers", {}).get("runtime", [])
    runtime_codes_before = sorted([r["code"] for r in runtime_before])
    history_count_before = len(history_before)
    orders_count_before = orders_before.get("total", 0)

    print(f"  重启前运行时规则数: {len(runtime_before)}")
    print(f"  重启前运行时规则codes: {runtime_codes_before}")
    print(f"  重启前导入历史记录数: {history_count_before}")
    print(f"  重启前补录记录数: {orders_count_before}")
    print(f"  重启前对账一致: {reconc_before['consistent']}")

    import_rules_to_check = ["import_new_1", "to_overwrite",
                             "batch_channel", "auto_detect_test", "to_disable_import"]
    for code in import_rules_to_check:
        exists_before = any(r["code"] == code for r in runtime_before)
        print(f"  重启前规则 {code} 存在: {exists_before}")

    stop_service(process)
    time.sleep(3)

    print("\n  重新启动服务...")
    process = start_service()
    wait_for_service()

    print("\n  验证重启后状态...")
    ok = True

    rules_after = api("GET", "/api/admin/source-rules").json()
    runtime_after = rules_after.get("layers", {}).get("runtime", [])
    runtime_codes_after = sorted([r["code"] for r in runtime_after])

    print_test("重启后运行时规则数一致",
               len(runtime_before) == len(runtime_after),
               f"before={len(runtime_before)}, after={len(runtime_after)}")
    if len(runtime_before) != len(runtime_after):
        ok = False

    print_test("重启后运行时规则codes一致",
               runtime_codes_before == runtime_codes_after,
               f"before={runtime_codes_before}, after={runtime_codes_after}")
    if runtime_codes_before != runtime_codes_after:
        ok = False

    for code in import_rules_to_check:
        exists_after = any(r["code"] == code for r in runtime_after)
        print_test(f"重启后规则 {code} 仍存在", exists_after, f"exists={exists_after}")
        if not exists_after:
            ok = False

    merged_after = rules_after.get("rules", [])
    merged_codes_after = [r["code"] for r in merged_after]
    for code in import_rules_to_check:
        if code != "to_disable_import":
            in_merged = code in merged_codes_after
            print_test(f"重启后规则 {code} 在合并规则中", in_merged,
                       f"in_merged={in_merged}")
            if not in_merged:
                ok = False

    history_after = api("GET", "/api/admin/source-rules/import-history").json()
    print_test("重启后导入历史记录数一致",
               len(history_after) == history_count_before,
               f"before={history_count_before}, after={len(history_after)}")
    if len(history_after) != history_count_before:
        ok = False

    if history_after:
        latest = history_after[0]
        print_test("重启后导入历史operator保留",
                   latest.get("operator") is not None,
                   f"operator={latest.get('operator')}")
        print_test("重启后导入历史new_count保留",
                   latest.get("new_count") is not None,
                   f"new_count={latest.get('new_count')}")
        print_test("重启后导入历史summary保留",
                   "summary" in latest,
                   f"has_summary={'summary' in latest}")
        if (latest.get("operator") is None or
            latest.get("new_count") is None or
            "summary" not in latest):
            ok = False

    orders_after = api("GET", "/api/admin/orders/makeup").json()
    print_test("重启后补录记录数一致",
               orders_after.get("total", 0) == orders_count_before,
               f"before={orders_count_before}, after={orders_after.get('total', 0)}")
    if orders_after.get("total", 0) != orders_count_before:
        ok = False

    for src in ["batch_channel", "import_new_1", "auto_channel_001"]:
        r_q = api("GET", "/api/admin/orders/makeup", params={"source": src})
        if r_q.status_code == 200:
            count = r_q.json().get("total", 0)
            print_test(f"重启后按来源{src}筛选可用", count >= 1,
                       f"{src}_count={count}")
            if count < 1:
                ok = False

    r_audit = api("GET", "/api/admin/source-rules/audit-log")
    if r_audit.status_code == 200:
        audit_logs = r_audit.json()
        print_test("重启后审计日志保留", len(audit_logs) > 0,
                   f"audit_count={len(audit_logs)}")
        if len(audit_logs) == 0:
            ok = False

        with_import_id = [e for e in audit_logs if e.get("import_id") is not None]
        print_test("重启后审计日志import_id保留",
                   len(with_import_id) >= 1,
                   f"with_import_id={len(with_import_id)}")
        if len(with_import_id) < 1:
            ok = False

    if history_after:
        first_import = history_after[0]
        import_id_after = first_import.get("id")
        if import_id_after:
            r_audit_by_import = api("GET", f"/api/admin/source-rules/audit-log?import_id={import_id_after}")
            if r_audit_by_import.status_code == 200:
                audit_by_import = r_audit_by_import.json()
                print_test(f"重启后按import_id={import_id_after}查审计日志可用",
                           len(audit_by_import) >= 1,
                           f"count={len(audit_by_import)}")
                if len(audit_by_import) < 1:
                    ok = False

    reconc_after = api("GET", "/api/admin/reconciliation").json()
    print_test("重启后对账通过", reconc_after["consistent"],
               f"Issues: {reconc_after.get('issues', [])}")
    if not reconc_after["consistent"]:
        ok = False

    print_test("重启后规则和导入记录持久化总结果", ok)
    return process, ok


def main():
    global passed, failed

    print_section("补录来源规则批量维护链路综合测试")
    print(f"  测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    cleanup_existing_db()

    process = None
    try:
        process = start_service()
        wait_for_service()

        ctx = setup_test_data()

        test_01_dry_run_preview(ctx)
        test_02_formal_import_overwrite(ctx)
        test_03_disabled_rule_blocking(ctx)
        test_04_source_hit_after_import(ctx)
        test_05_revoke_and_track_by_source(ctx)
        test_06_export_fields_completeness(ctx)
        test_07_report_conflict_strategy(ctx)
        test_08_audit_import_id_crossref(ctx)
        process, reboot_ok = test_09_reboot_persistence(ctx, process)

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
