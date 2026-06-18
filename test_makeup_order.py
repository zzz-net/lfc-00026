import requests
import json
import os
import sys
import time
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

BASE_URL = "http://127.0.0.1:8006"
TEST_DB = Path(__file__).parent / "canteen_test_makeup.db"
SCRIPT_DIR = Path(__file__).parent

passed = 0
failed = 0


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
    kwargs.setdefault("timeout", 30)
    return requests.request(method, f"{BASE_URL}{path}", **kwargs)


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
        "--port", "8006",
    ]

    print(f"  命令: {' '.join(cmd)}")
    print(f"  数据库: {TEST_DB}")
    print(f"  服务地址: {BASE_URL}")
    print()

    process = subprocess.Popen(
        cmd,
        cwd=str(SCRIPT_DIR),
        env=env,
        stdout=None,
        stderr=None,
    )

    print(f"  服务 PID: {process.pid}")
    print("  等待服务启动...", end="", flush=True)

    last_error = None
    for i in range(120):
        if process.poll() is not None:
            print(f"\n  [ERROR] 服务启动失败！退出码: {process.returncode}")
            print("  请查看服务输出以了解错误详情")
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
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
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
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    day_before = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    old_date = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    future_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    deadline = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

    api("POST", "/api/admin/init-sample")

    emp = api("GET", "/api/admin/employees/EMP001").json()

    print(f"  创建今天的菜单用于补录测试...")
    today_menu = api("POST", "/api/admin/menus", json={
        "name": f"今日午餐-{today}",
        "serving_date": today,
        "deadline": deadline,
    }).json()

    item1 = api("POST", f"/api/admin/menus/{today_menu['id']}/items", json={
        "name": "红烧肉",
        "price": 18.0,
        "stock": 50,
    }).json()

    item2 = api("POST", f"/api/admin/menus/{today_menu['id']}/items", json={
        "name": "番茄炒蛋",
        "price": 12.0,
        "stock": 100,
    }).json()

    api("POST", f"/api/admin/menus/{today_menu['id']}/publish")

    print(f"  员工 EMP001 余额: {emp['balance']}")
    print(f"  今日菜单日期: {today}")
    print(f"  今日菜品: 红烧肉(18元), 番茄炒蛋(12元)")

    return {
        "today": today,
        "yesterday": yesterday,
        "day_before": day_before,
        "old_date": old_date,
        "future_date": future_date,
        "menu_date": today,
        "menu_id": today_menu["id"],
        "item_id": item1["id"],
        "item_name": item1["name"],
        "item_price": item1["price"],
        "item_stock": item1["stock"],
        "item2_id": item2["id"],
        "emp_balance": emp["balance"],
    }


def test_successful_makeup(ctx):
    print_section("测试 1: 成功补录")

    r = api(
        "POST",
        "/api/admin/orders/makeup",
        json={
            "employee_id": "EMP001",
            "menu_item_id": ctx["item_id"],
            "quantity": 1,
            "serving_date": ctx["menu_date"],
            "source": "window",
            "remark": "测试补录",
        },
        headers={"X-Idempotency-Key": "makeup-test-001"},
    )

    if r.status_code != 200:
        print_test("成功补录", False, f"状态码 {r.status_code}: {r.json()}")
        return None

    order = r.json()
    expected_amount = ctx["item_price"] * 1

    ok = True
    checks = [
        ("订单状态为 taken", order["status"] == "taken"),
        ("来源为 makeup", order["source"] == "makeup"),
        ("备注正确", order.get("makeup_remark") == "测试补录"),
        ("金额正确", abs(order["total_amount"] - expected_amount) < 0.001),
        ("数量正确", order["quantity"] == 1),
        ("菜品名称正确", order["item_name"] == ctx["item_name"]),
    ]

    for name, success in checks:
        print_test(name, success)
        if not success:
            ok = False

    emp = api("GET", "/api/admin/employees/EMP001").json()
    expected_balance = ctx["emp_balance"] - expected_amount
    balance_ok = abs(emp["balance"] - expected_balance) < 0.001
    print_test(f"余额正确扣减 ({ctx['emp_balance']} - {expected_amount} = {expected_balance})", balance_ok)
    if not balance_ok:
        print(f"    实际余额: {emp['balance']}")
        ok = False

    reconc = api("GET", "/api/admin/reconciliation").json()
    print_test("对账一致", reconc["consistent"], f"Issues: {reconc.get('issues', [])}")
    if not reconc["consistent"]:
        ok = False

    print_test("成功补录总结果", ok)
    return order if ok else None


def test_duplicate_makeup(ctx, order):
    print_section("测试 2: 重复补录冲突")

    if not order:
        print_test("跳过重复补录测试（前序测试失败）", False)
        return

    r = api(
        "POST",
        "/api/admin/orders/makeup",
        json={
            "employee_id": "EMP001",
            "menu_item_id": ctx["item_id"],
            "quantity": 1,
            "serving_date": ctx["menu_date"],
            "source": "window",
        },
    )

    ok = True
    if r.status_code != 409:
        print_test("返回 409 状态码", False, f"实际状态码: {r.status_code}")
        ok = False
    else:
        print_test("返回 409 状态码", True)

    try:
        detail = r.json()["detail"]
        code_ok = detail["code"] == "DUPLICATE_MAKEUP"
        print_test(f"错误码正确 (DUPLICATE_MAKEUP)", code_ok, f"实际: {detail.get('code')}")
        if not code_ok:
            ok = False

        msg_ok = "已补录过" in detail["message"] or "重复补录" in detail["message"]
        print_test("错误信息可读", msg_ok, f"实际: {detail.get('message')}")
        if not msg_ok:
            ok = False
    except Exception as e:
        print_test("错误格式正确", False, f"解析错误: {e}")
        ok = False

    reconc = api("GET", "/api/admin/reconciliation").json()
    print_test("对账仍然一致", reconc["consistent"], f"Issues: {reconc.get('issues', [])}")
    if not reconc["consistent"]:
        ok = False

    print_test("重复补录冲突总结果", ok)


def test_insufficient_balance(ctx):
    print_section("测试 3: 余额不足")

    low_balance_emp = "EMP_LOW"
    api("POST", "/api/admin/employees", json={
        "id": low_balance_emp,
        "name": "余额少的员工",
        "initial_balance": 1.0,
    })

    r = api(
        "POST",
        "/api/admin/orders/makeup",
        json={
            "employee_id": low_balance_emp,
            "menu_item_id": ctx["item_id"],
            "quantity": 2,
            "serving_date": ctx["menu_date"],
        },
    )

    ok = True
    if r.status_code != 400:
        print_test("返回 400 状态码", False, f"实际状态码: {r.status_code}")
        ok = False
    else:
        print_test("返回 400 状态码", True)

    try:
        detail = r.json()["detail"]
        code_ok = detail["code"] == "INSUFFICIENT_BALANCE"
        print_test(f"错误码正确 (INSUFFICIENT_BALANCE)", code_ok, f"实际: {detail.get('code')}")
        if not code_ok:
            ok = False

        msg_ok = "余额不足" in detail["message"]
        print_test("错误信息可读", msg_ok, f"实际: {detail.get('message')}")
        if not msg_ok:
            ok = False
    except Exception as e:
        print_test("错误格式正确", False, f"解析错误: {e}")
        ok = False

    reconc = api("GET", "/api/admin/reconciliation").json()
    print_test("对账仍然一致", reconc["consistent"], f"Issues: {reconc.get('issues', [])}")
    if not reconc["consistent"]:
        ok = False

    print_test("余额不足测试总结果", ok)


def test_insufficient_stock(ctx):
    print_section("测试 4: 库存不足")

    menu_detail = api("GET", f"/api/admin/menus/{ctx['menu_id']}").json()
    item = [i for i in menu_detail["items"] if i["id"] == ctx["item_id"]][0]
    available = item["stock"] - item["sold_count"]

    r = api(
        "POST",
        "/api/admin/orders/makeup",
        json={
            "employee_id": "EMP002",
            "menu_item_id": ctx["item_id"],
            "quantity": available + 100,
            "serving_date": ctx["menu_date"],
        },
    )

    ok = True
    if r.status_code != 400:
        print_test("返回 400 状态码", False, f"实际状态码: {r.status_code}")
        ok = False
    else:
        print_test("返回 400 状态码", True)

    try:
        detail = r.json()["detail"]
        code_ok = detail["code"] == "OUT_OF_STOCK"
        print_test(f"错误码正确 (OUT_OF_STOCK)", code_ok, f"实际: {detail.get('code')}")
        if not code_ok:
            ok = False

        msg_ok = "库存不足" in detail["message"]
        print_test("错误信息可读", msg_ok, f"实际: {detail.get('message')}")
        if not msg_ok:
            ok = False
    except Exception as e:
        print_test("错误格式正确", False, f"解析错误: {e}")
        ok = False

    reconc = api("GET", "/api/admin/reconciliation").json()
    print_test("对账仍然一致", reconc["consistent"], f"Issues: {reconc.get('issues', [])}")
    if not reconc["consistent"]:
        ok = False

    print_test("库存不足测试总结果", ok)


def test_menu_not_published(ctx):
    print_section("测试 5: 菜单未发布")

    draft_menu = api("POST", "/api/admin/menus", json={
        "name": "草稿菜单",
        "serving_date": ctx["day_before"],
        "deadline": f"{ctx['day_before']} 09:00:00",
    }).json()

    draft_item = api("POST", f"/api/admin/menus/{draft_menu['id']}/items", json={
        "name": "测试菜",
        "price": 10.0,
        "stock": 10,
    }).json()

    r = api(
        "POST",
        "/api/admin/orders/makeup",
        json={
            "employee_id": "EMP001",
            "menu_item_id": draft_item["id"],
            "quantity": 1,
            "serving_date": ctx["day_before"],
        },
    )

    ok = True
    if r.status_code != 400:
        print_test("返回 400 状态码", False, f"实际状态码: {r.status_code}")
        ok = False
    else:
        print_test("返回 400 状态码", True)

    try:
        detail = r.json()["detail"]
        code_ok = detail["code"] == "MENU_NOT_PUBLISHED"
        print_test(f"错误码正确 (MENU_NOT_PUBLISHED)", code_ok, f"实际: {detail.get('code')}")
        if not code_ok:
            ok = False

        msg_ok = "菜单未发布" in detail["message"]
        print_test("错误信息可读", msg_ok, f"实际: {detail.get('message')}")
        if not msg_ok:
            ok = False
    except Exception as e:
        print_test("错误格式正确", False, f"解析错误: {e}")
        ok = False

    reconc = api("GET", "/api/admin/reconciliation").json()
    print_test("对账仍然一致", reconc["consistent"], f"Issues: {reconc.get('issues', [])}")
    if not reconc["consistent"]:
        ok = False

    print_test("菜单未发布测试总结果", ok)


def test_date_mismatch(ctx):
    print_section("测试 6: 日期不匹配")

    r = api(
        "POST",
        "/api/admin/orders/makeup",
        json={
            "employee_id": "EMP001",
            "menu_item_id": ctx["item_id"],
            "quantity": 1,
            "serving_date": ctx["yesterday"],
        },
    )

    ok = True
    if r.status_code != 400:
        print_test("返回 400 状态码", False, f"实际状态码: {r.status_code}")
        ok = False
    else:
        print_test("返回 400 状态码", True)

    try:
        detail = r.json()["detail"]
        code_ok = detail["code"] == "DATE_MISMATCH"
        print_test(f"错误码正确 (DATE_MISMATCH)", code_ok, f"实际: {detail.get('code')}")
        if not code_ok:
            ok = False

        msg_ok = "不匹配" in detail["message"]
        print_test("错误信息可读", msg_ok, f"实际: {detail.get('message')}")
        if not msg_ok:
            ok = False
    except Exception as e:
        print_test("错误格式正确", False, f"解析错误: {e}")
        ok = False

    reconc = api("GET", "/api/admin/reconciliation").json()
    print_test("对账仍然一致", reconc["consistent"], f"Issues: {reconc.get('issues', [])}")
    if not reconc["consistent"]:
        ok = False

    print_test("日期不匹配测试总结果", ok)


def test_date_out_of_range(ctx):
    print_section("测试 7: 超出补录天数限制")

    old_menu = api("POST", "/api/admin/menus", json={
        "name": "旧菜单",
        "serving_date": ctx["old_date"],
        "deadline": f"{ctx['old_date']} 09:00:00",
    }).json()

    old_item = api("POST", f"/api/admin/menus/{old_menu['id']}/items", json={
        "name": "旧菜",
        "price": 10.0,
        "stock": 10,
    }).json()

    api("POST", f"/api/admin/menus/{old_menu['id']}/publish")

    r = api(
        "POST",
        "/api/admin/orders/makeup",
        json={
            "employee_id": "EMP001",
            "menu_item_id": old_item["id"],
            "quantity": 1,
            "serving_date": ctx["old_date"],
        },
    )

    ok = True
    if r.status_code != 400:
        print_test("返回 400 状态码", False, f"实际状态码: {r.status_code}")
        ok = False
    else:
        print_test("返回 400 状态码", True)

    try:
        detail = r.json()["detail"]
        code_ok = detail["code"] == "DATE_OUT_OF_RANGE"
        print_test(f"错误码正确 (DATE_OUT_OF_RANGE)", code_ok, f"实际: {detail.get('code')}")
        if not code_ok:
            ok = False

        msg_ok = "超出允许范围" in detail["message"] or "超过" in detail["message"]
        print_test("错误信息可读", msg_ok, f"实际: {detail.get('message')}")
        if not msg_ok:
            ok = False
    except Exception as e:
        print_test("错误格式正确", False, f"解析错误: {e}")
        ok = False

    reconc = api("GET", "/api/admin/reconciliation").json()
    print_test("对账仍然一致", reconc["consistent"], f"Issues: {reconc.get('issues', [])}")
    if not reconc["consistent"]:
        ok = False

    print_test("超出补录天数限制测试总结果", ok)


def test_invalid_source(ctx):
    print_section("测试 8: 补录来源不合法")

    r = api(
        "POST",
        "/api/admin/orders/makeup",
        json={
            "employee_id": "EMP001",
            "menu_item_id": ctx["item_id"],
            "quantity": 1,
            "serving_date": ctx["menu_date"],
            "source": "invalid_source",
        },
    )

    ok = True
    if r.status_code != 400:
        print_test("返回 400 状态码", False, f"实际状态码: {r.status_code}")
        ok = False
    else:
        print_test("返回 400 状态码", True)

    try:
        detail = r.json()["detail"]
        code_ok = detail["code"] == "INVALID_SOURCE"
        print_test(f"错误码正确 (INVALID_SOURCE)", code_ok, f"实际: {detail.get('code')}")
        if not code_ok:
            ok = False

        msg_ok = "不合法" in detail["message"] or "来源" in detail["message"]
        print_test("错误信息可读", msg_ok, f"实际: {detail.get('message')}")
        if not msg_ok:
            ok = False
    except Exception as e:
        print_test("错误格式正确", False, f"解析错误: {e}")
        ok = False

    reconc = api("GET", "/api/admin/reconciliation").json()
    print_test("对账仍然一致", reconc["consistent"], f"Issues: {reconc.get('issues', [])}")
    if not reconc["consistent"]:
        ok = False

    print_test("补录来源不合法测试总结果", ok)


def test_config_management(ctx):
    print_section("测试 9: 配置管理")

    ok = True

    r = api("GET", "/api/admin/config/makeup")
    cfg = r.json()
    print_test("获取补录配置成功", r.status_code == 200)
    if r.status_code != 200:
        ok = False

    default_days = cfg.get("days_limit", 7)
    print_test(f"默认补录天数为 {default_days}", "days_limit" in cfg, f"配置: {cfg}")
    if "days_limit" not in cfg:
        ok = False

    r = api("PUT", "/api/admin/config/makeup_days_limit", json={
        "value": "30",
        "description": "测试修改补录天数",
    })
    print_test("更新配置成功", r.status_code == 200)
    if r.status_code != 200:
        ok = False

    r = api("GET", "/api/admin/config/makeup")
    new_cfg = r.json()
    days_updated = new_cfg.get("days_limit") == 30
    print_test("配置更新生效", days_updated, f"更新后天数: {new_cfg.get('days_limit')}")
    if not days_updated:
        ok = False

    r = api("PUT", "/api/admin/config/makeup_days_limit", json={"value": str(default_days)})
    print_test("恢复默认配置", r.status_code == 200)

    reconc = api("GET", "/api/admin/reconciliation").json()
    print_test("对账仍然一致", reconc["consistent"], f"Issues: {reconc.get('issues', [])}")
    if not reconc["consistent"]:
        ok = False

    print_test("配置管理测试总结果", ok)


def test_idempotency(ctx):
    print_section("测试 10: 幂等性")

    key = f"makeup-idempotency-{int(time.time())}"

    r1 = api(
        "POST",
        "/api/admin/orders/makeup",
        json={
            "employee_id": "EMP003",
            "menu_item_id": ctx["item_id"],
            "quantity": 1,
            "serving_date": ctx["menu_date"],
        },
        headers={"X-Idempotency-Key": key},
    )

    if r1.status_code != 200:
        print_test("首次请求成功", False, f"状态码 {r1.status_code}: {r1.json()}")
        return

    order1 = r1.json()
    order_id1 = order1["id"]
    print_test("首次补录成功", True, f"订单ID: {order_id1}")

    r2 = api(
        "POST",
        "/api/admin/orders/makeup",
        json={
            "employee_id": "EMP003",
            "menu_item_id": ctx["item_id"],
            "quantity": 1,
            "serving_date": ctx["menu_date"],
        },
        headers={"X-Idempotency-Key": key},
    )

    ok = True
    if r2.status_code != 200:
        print_test("重复幂等请求成功", False, f"状态码 {r2.status_code}")
        ok = False
    else:
        order2 = r2.json()
        same_id = order2["id"] == order_id1
        print_test("返回同一订单ID", same_id, f"首次: {order_id1}, 重复: {order2['id']}")
        if not same_id:
            ok = False

    reconc = api("GET", "/api/admin/reconciliation").json()
    print_test("对账仍然一致", reconc["consistent"], f"Issues: {reconc.get('issues', [])}")
    if not reconc["consistent"]:
        ok = False

    orders = api("GET", "/api/orders", params={"employee_id": "EMP003", "status": "taken"}).json()
    makeup_count = sum(1 for o in orders if o.get("source") == "makeup")
    count_ok = makeup_count == 1
    print_test("只创建了1条补录订单", count_ok, f"实际: {makeup_count}")
    if not count_ok:
        ok = False

    print_test("幂等性测试总结果", ok)


def test_reboot_consistency(ctx, process):
    print_section("测试 11: 服务重启后配置和数据一致，对账通过")

    print("  记录重启前状态...")
    cfg_before = api("GET", "/api/admin/config/makeup").json()
    orders_before = api("GET", "/api/orders").json()
    emp_before = api("GET", "/api/admin/employees/EMP001").json()
    reconc_before = api("GET", "/api/admin/reconciliation").json()

    print(f"  重启前订单数: {len(orders_before)}")
    print(f"  重启前员工余额: {emp_before['balance']}")
    print(f"  重启前对账一致: {reconc_before['consistent']}")

    stop_service(process)
    time.sleep(2)

    print("\n  重新启动服务...")
    process = start_service()
    wait_for_service()

    print("\n  验证重启后状态...")
    ok = True

    cfg_after = api("GET", "/api/admin/config/makeup").json()
    cfg_ok = cfg_before == cfg_after
    print_test("配置重启后一致", cfg_ok)
    if not cfg_ok:
        print(f"    前: {cfg_before}")
        print(f"    后: {cfg_after}")
        ok = False

    orders_after = api("GET", "/api/orders").json()
    orders_ok = len(orders_before) == len(orders_after)
    print_test("订单数重启后一致", orders_ok, f"前: {len(orders_before)}, 后: {len(orders_after)}")
    if not orders_ok:
        ok = False

    emp_after = api("GET", "/api/admin/employees/EMP001").json()
    emp_ok = abs(emp_before["balance"] - emp_after["balance"]) < 0.001
    print_test("员工余额重启后一致", emp_ok, f"前: {emp_before['balance']}, 后: {emp_after['balance']}")
    if not emp_ok:
        ok = False

    reconc_after = api("GET", "/api/admin/reconciliation").json()
    reconc_ok = reconc_after["consistent"]
    print_test("重启后对账通过", reconc_ok, f"Issues: {reconc_after.get('issues', [])}")
    if not reconc_ok:
        ok = False

    print_test("服务重启一致性测试总结果", ok)
    return process


def test_transactions_complete(ctx):
    print_section("测试 12: 流水记录完整")

    orders = api("GET", "/api/orders", params={"employee_id": "EMP001", "status": "taken"}).json()
    makeup_orders = [o for o in orders if o.get("source") == "makeup"]

    if not makeup_orders:
        print_test("跳过流水测试（无补录订单）", False)
        return

    ok = True
    for order in makeup_orders[:1]:
        txns = api("GET", "/api/transactions", params={"order_id": order["id"]}).json()
        txn_count = txns.get("total", 0)
        print_test(f"订单 {order['id']} 流水完整（需要2条）", txn_count >= 2, f"实际: {txn_count}")
        if txn_count < 2:
            ok = False

        types = [t["type"] for t in txns.get("items", [])]
        has_freeze = "FREEZE" in types
        has_settle = "SETTLE" in types
        print_test("包含 FREEZE 流水", has_freeze, f"类型: {types}")
        print_test("包含 SETTLE 流水", has_settle, f"类型: {types}")
        if not (has_freeze and has_settle):
            ok = False

        for t in txns.get("items", []):
            if "补录" in (t.get("description") or ""):
                print_test("流水描述包含'补录'标识", True, t.get("description"))
                break
        else:
            print_test("流水描述包含'补录'标识", False)
            ok = False

    print_test("流水记录完整测试总结果", ok)


def main():
    global passed, failed

    print_section("管理员补录取餐功能测试")
    print(f"  测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    cleanup_existing_db()

    process = None
    try:
        process = start_service()
        wait_for_service()

        ctx = setup_test_data()

        order = test_successful_makeup(ctx)
        test_duplicate_makeup(ctx, order)
        test_insufficient_balance(ctx)
        test_insufficient_stock(ctx)
        test_menu_not_published(ctx)
        test_date_mismatch(ctx)
        test_date_out_of_range(ctx)
        test_invalid_source(ctx)
        test_config_management(ctx)
        test_idempotency(ctx)
        test_transactions_complete(ctx)
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
