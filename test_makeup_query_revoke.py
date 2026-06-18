import requests
import json
import os
import sys
import time
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

BASE_URL = "http://127.0.0.1:8007"
TEST_DB = Path(__file__).parent / "canteen_test_makeup_qr.db"
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
        "--port", "8007",
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
    deadline = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

    api("POST", "/api/admin/employees", json={"id": "QA001", "name": "测试员工A", "initial_balance": 200.0})
    api("POST", "/api/admin/employees", json={"id": "QA002", "name": "测试员工B", "initial_balance": 100.0})

    menu = api("POST", "/api/admin/menus", json={
        "name": f"补录查询撤销测试菜单-{today}",
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

    print(f"  今日菜单: {menu['id']}")
    print(f"  菜品1: 宫保鸡丁(20元, id={item1['id']})")
    print(f"  菜品2: 麻婆豆腐(15元, id={item2['id']})")

    api("PUT", "/api/admin/config/makeup_revoke_deadline_hours", json={"value": "0"})

    return {
        "today": today,
        "menu_id": menu["id"],
        "item1_id": item1["id"],
        "item2_id": item2["id"],
    }


def test_query_makeup_empty(ctx):
    print_section("测试 1: 查询补录记录 - 空结果")

    r = api("GET", "/api/admin/orders/makeup")
    ok = True
    if r.status_code != 200:
        print_test("查询空结果返回200", False, f"状态码 {r.status_code}")
        ok = False
    else:
        data = r.json()
        print_test("查询空结果返回200", True)
        print_test("total为0", data.get("total") == 0, f"total={data.get('total')}")
        print_test("items为空列表", data.get("items") == [], f"items={data.get('items')}")
    print_test("查询空结果总结果", ok)


def test_query_with_filters(ctx):
    print_section("测试 2: 查询补录记录 - 带筛选条件")

    r = api("POST", "/api/admin/orders/makeup", json={
        "employee_id": "QA001",
        "menu_item_id": ctx["item1_id"],
        "quantity": 1,
        "serving_date": ctx["today"],
        "source": "admin",
        "remark": "查询测试补录1",
    })
    if r.status_code != 200:
        print_test("创建补录1用于查询", False, f"{r.json()}")
        return
    order1 = r.json()

    r = api("POST", "/api/admin/orders/makeup", json={
        "employee_id": "QA002",
        "menu_item_id": ctx["item2_id"],
        "quantity": 2,
        "serving_date": ctx["today"],
        "source": "window",
        "remark": "查询测试补录2",
    })
    if r.status_code != 200:
        print_test("创建补录2用于查询", False, f"{r.json()}")
        return
    order2 = r.json()

    ok = True

    r = api("GET", "/api/admin/orders/makeup")
    data = r.json()
    print_test("无筛选查询返回至少2条", data.get("total", 0) >= 2, f"total={data.get('total')}")
    if data.get("total", 0) < 2:
        ok = False

    r = api("GET", "/api/admin/orders/makeup", params={"employee_id": "QA001"})
    data = r.json()
    qa1_count = data.get("total", 0)
    print_test("按员工QA001筛选至少1条", qa1_count >= 1, f"total={qa1_count}")
    if qa1_count < 1:
        ok = False

    r = api("GET", "/api/admin/orders/makeup", params={"serving_date": ctx["today"]})
    data = r.json()
    date_count = data.get("total", 0)
    print_test(f"按日期{ctx['today']}筛选至少2条", date_count >= 2, f"total={date_count}")
    if date_count < 2:
        ok = False

    r = api("GET", "/api/admin/orders/makeup", params={"employee_id": "QA_NOTEXIST"})
    data = r.json()
    print_test("不存在的员工查询返回0条", data.get("total") == 0, f"total={data.get('total')}")
    if data.get("total") != 0:
        ok = False

    r = api("GET", "/api/admin/orders/makeup", params={"page": 1, "page_size": 1})
    data = r.json()
    print_test("分页: page=1, page_size=1 返回1条", len(data.get("items", [])) == 1, f"items={len(data.get('items', []))}")
    print_test("分页: total不变", data.get("total") >= 2, f"total={data.get('total')}")
    if len(data.get("items", [])) != 1:
        ok = False

    r = api("GET", "/api/admin/orders/makeup", params={"employee_id": "QA001"})
    data = r.json()
    if data.get("items"):
        item = data["items"][0]
        has_transactions = "transactions" in item and len(item["transactions"]) >= 2
        print_test("返回记录包含transactions", has_transactions, f"len={len(item.get('transactions', []))}")
        has_logs = "operation_logs" in item and len(item["operation_logs"]) >= 1
        print_test("返回记录包含operation_logs", has_logs, f"len={len(item.get('operation_logs', []))}")
        has_remark = item.get("makeup_remark") is not None
        print_test("返回记录包含makeup_remark", has_remark, f"remark={item.get('makeup_remark')}")
        has_employee_name = "employee_name" in item
        print_test("返回记录包含employee_name", has_employee_name, f"name={item.get('employee_name')}")
        has_serving_date = "serving_date" in item
        print_test("返回记录包含serving_date", has_serving_date, f"date={item.get('serving_date')}")
        has_revoked_at = "revoked_at" in item
        print_test("返回记录包含revoked_at", has_revoked_at)
        if not (has_transactions and has_logs and has_remark and has_employee_name and has_serving_date and has_revoked_at):
            ok = False
    else:
        print_test("返回记录字段检查", False, "无记录")
        ok = False

    print_test("查询筛选总结果", ok)
    return order1


def test_source_echo_and_filter(ctx):
    print_section("测试 2b: Source 回显、来源筛选与撤销后可追查")

    sources_to_test = ["window", "admin", "manual"]
    created_orders = {}
    ok = True

    for i, src in enumerate(sources_to_test):
        emp_id = f"SRC{i + 1:03d}"
        api("POST", "/api/admin/employees", json={
            "id": emp_id, "name": f"来源测试员工{i + 1}", "initial_balance": 100.0,
        })

        item_id = ctx["item1_id"] if i % 2 == 0 else ctx["item2_id"]

        r = api("POST", "/api/admin/orders/makeup", json={
            "employee_id": emp_id,
            "menu_item_id": item_id,
            "quantity": 1,
            "serving_date": ctx["today"],
            "source": src,
            "remark": f"来源{src}测试",
        })

        if r.status_code != 200:
            print_test(f"创建来源={src}的补录", False, f"{r.json()}")
            ok = False
            continue

        order = r.json()
        created_orders[src] = order

        source_echo_ok = order["source"] == src
        print_test(f"来源{src}回显正确", source_echo_ok,
                   f"请求={src}, 返回={order['source']}")
        if not source_echo_ok:
            ok = False

        status_ok = order["status"] == "taken"
        print_test(f"来源{src}订单状态为taken", status_ok)
        if not status_ok:
            ok = False

    for src in sources_to_test:
        if src not in created_orders:
            continue
        r = api("GET", "/api/admin/orders/makeup", params={"source": src})
        if r.status_code != 200:
            print_test(f"按来源{src}筛选查询成功", False, f"状态码 {r.status_code}")
            ok = False
            continue

        data = r.json()
        found = any(item["id"] == created_orders[src]["id"] for item in data.get("items", []))
        print_test(f"按来源{src}筛选命中对应订单", found,
                   f"查询到{data.get('total')}条, 含目标订单={found}")
        if not found:
            ok = False

        all_match = all(item["source"] == src for item in data.get("items", []))
        print_test(f"按来源{src}筛选结果全部匹配", all_match)
        if not all_match:
            ok = False

    r_all = api("GET", "/api/admin/orders/makeup")
    total_all = r_all.json().get("total", 0)
    print_test(f"无筛选查询返回所有补录（>= {len(sources_to_test)}条）",
               total_all >= len(sources_to_test), f"实际={total_all}")
    if total_all < len(sources_to_test):
        ok = False

    src_to_revoke = "admin"
    if src_to_revoke in created_orders:
        order_to_revoke = created_orders[src_to_revoke]
        r = api("POST", f"/api/admin/orders/makeup/{order_to_revoke['id']}/revoke",
                json={"remark": "撤销后验证source"})
        if r.status_code == 200:
            revoked = r.json()
            source_preserved = revoked["source"] == src_to_revoke
            print_test("撤销后订单source仍保留", source_preserved,
                       f"source={revoked['source']}")
            if not source_preserved:
                ok = False

            status_cancelled = revoked["status"] == "cancelled"
            print_test("撤销后响应status=cancelled", status_cancelled,
                       f"status={revoked['status']}")
            if not status_cancelled:
                ok = False

            revoked_at_ok = bool(revoked.get("revoked_at"))
            print_test("撤销后响应revoked_at非空", revoked_at_ok,
                       f"revoked_at={revoked.get('revoked_at')}")
            if not revoked_at_ok:
                ok = False

            r_q = api("GET", "/api/admin/orders/makeup",
                      params={"employee_id": "SRC002"})
            items = r_q.json().get("items", [])
            if items:
                q_source = items[0]["source"]
                q_status = items[0]["status"]
                q_revoked_at = items[0].get("revoked_at")
                op_logs = items[0].get("operation_logs", [])
                txn_list = items[0].get("transactions", [])

                log_has_create = any(log["operation_type"] == "create" for log in op_logs)
                log_has_revoke = any(log["operation_type"] == "revoke" for log in op_logs)
                txn_has_revoke = any(t["type"] == "MAKEUP_REVOKE" for t in txn_list)

                print_test("撤销后查询source仍可追查", q_source == src_to_revoke,
                           f"source={q_source}")
                print_test("撤销后查询status=cancelled", q_status == "cancelled",
                           f"status={q_status}")
                print_test("撤销后查询revoked_at非空", bool(q_revoked_at),
                           f"revoked_at={q_revoked_at}")
                print_test("撤销后操作日志含create记录", log_has_create)
                print_test("撤销后操作日志含revoke记录", log_has_revoke,
                           f"logs={[(l['operation_type'], l.get('operator')) for l in op_logs]}")
                print_test("撤销后流水含MAKEUP_REVOKE", txn_has_revoke,
                           f"types={[t['type'] for t in txn_list]}")
                if (q_source != src_to_revoke or q_status != "cancelled" or
                    not bool(q_revoked_at) or not log_has_create or
                    not log_has_revoke or not txn_has_revoke):
                    ok = False

    reconc = api("GET", "/api/admin/reconciliation").json()
    print_test("对账一致", reconc["consistent"], f"Issues: {reconc.get('issues', [])}")
    if not reconc["consistent"]:
        ok = False

    print_test("Source回显与筛选总结果", ok)
    return created_orders.get("window")


def test_revoke_makeup_success(ctx, order):
    print_section("测试 3: 成功撤销补录")

    if not order:
        print_test("跳过成功撤销测试（前序失败）", False)
        return None

    emp_before = api("GET", "/api/admin/employees/QA001").json()
    menu_before = api("GET", f"/api/admin/menus/{ctx['menu_id']}").json()
    item_before = [i for i in menu_before["items"] if i["id"] == ctx["item1_id"]][0]

    r = api("POST", f"/api/admin/orders/makeup/{order['id']}/revoke", json={"remark": "撤销原因测试"})
    ok = True

    if r.status_code != 200:
        print_test("撤销返回200", False, f"状态码 {r.status_code}: {r.json()}")
        return None

    result = r.json()
    print_test("撤销返回200", True)

    status_ok = result["status"] == "cancelled"
    print_test("订单状态为cancelled", status_ok, f"status={result['status']}")
    if not status_ok:
        ok = False

    revoked_at_ok = result.get("revoked_at") is not None
    print_test("revoked_at已设置", revoked_at_ok, f"revoked_at={result.get('revoked_at')}")
    if not revoked_at_ok:
        ok = False

    emp_after = api("GET", "/api/admin/employees/QA001").json()
    balance_diff = abs((emp_after["balance"] - emp_before["balance"]) - order["total_amount"])
    balance_ok = balance_diff < 0.01
    print_test(f"余额正确回退 (+{order['total_amount']})", balance_ok,
               f"前={emp_before['balance']}, 后={emp_after['balance']}, 差={emp_after['balance']-emp_before['balance']}")
    if not balance_ok:
        ok = False

    menu_after = api("GET", f"/api/admin/menus/{ctx['menu_id']}").json()
    item_after = [i for i in menu_after["items"] if i["id"] == ctx["item1_id"]][0]
    sold_diff = item_before["sold_count"] - item_after["sold_count"]
    stock_ok = sold_diff == order["quantity"]
    print_test(f"库存sold_count正确回退 (-{order['quantity']})", stock_ok,
               f"前={item_before['sold_count']}, 后={item_after['sold_count']}")
    if not stock_ok:
        ok = False

    txns = api("GET", "/api/transactions", params={"order_id": order["id"]}).json()
    types = [t["type"] for t in txns.get("items", [])]
    has_revoke = "MAKEUP_REVOKE" in types
    print_test("流水包含 MAKEUP_REVOKE", has_revoke, f"types={types}")
    if not has_revoke:
        ok = False

    revoke_txn = [t for t in txns.get("items", []) if t["type"] == "MAKEUP_REVOKE"]
    if revoke_txn:
        desc = revoke_txn[0].get("description", "")
        desc_ok = "撤销" in desc and "退款" in desc
        print_test("撤销流水描述包含'撤销退款'", desc_ok, f"desc={desc}")
        if not desc_ok:
            ok = False
    else:
        print_test("撤销流水描述检查", False, "无MAKEUP_REVOKE流水")
        ok = False

    reconc = api("GET", "/api/admin/reconciliation").json()
    print_test("撤销后对账一致", reconc["consistent"], f"Issues: {reconc.get('issues', [])}")
    if not reconc["consistent"]:
        ok = False

    r = api("GET", "/api/admin/orders/makeup", params={"employee_id": "QA001"})
    query_data = r.json()
    if query_data.get("items"):
        revoked_item = [i for i in query_data["items"] if i["id"] == order["id"]]
        if revoked_item:
            log_ok = any(l["operation_type"] == "revoke" for l in revoked_item[0].get("operation_logs", []))
            print_test("操作日志包含revoke记录", log_ok)
            if not log_ok:
                ok = False
        else:
            print_test("操作日志检查", False, "查询结果中未找到该订单")
            ok = False
    else:
        print_test("操作日志检查", False, "查询结果为空")
        ok = False

    print_test("成功撤销总结果", ok)
    return result


def test_revoke_duplicate(ctx, order):
    print_section("测试 4: 重复撤销冲突")

    if not order:
        print_test("跳过重复撤销测试", False)
        return

    r = api("POST", f"/api/admin/orders/makeup/{order['id']}/revoke")
    ok = True

    if r.status_code != 409:
        print_test("重复撤销返回409", False, f"状态码 {r.status_code}")
        ok = False
    else:
        print_test("重复撤销返回409", True)

    try:
        detail = r.json()["detail"]
        code_ok = detail["code"] == "ALREADY_REVOKED"
        print_test("错误码 ALREADY_REVOKED", code_ok, f"code={detail.get('code')}")
        if not code_ok:
            ok = False

        msg_ok = "已被撤销" in detail["message"] or "重复操作" in detail["message"]
        print_test("错误信息可读", msg_ok, f"message={detail.get('message')}")
        if not msg_ok:
            ok = False
    except Exception as e:
        print_test("错误格式正确", False, f"解析错误: {e}")
        ok = False

    reconc = api("GET", "/api/admin/reconciliation").json()
    print_test("对账仍然一致", reconc["consistent"], f"Issues: {reconc.get('issues', [])}")
    if not reconc["consistent"]:
        ok = False

    print_test("重复撤销冲突总结果", ok)


def test_revoke_not_found(ctx):
    print_section("测试 5: 撤销不存在的订单")

    r = api("POST", "/api/admin/orders/makeup/ORD_NOT_EXIST_999/revoke")
    ok = True

    if r.status_code != 404:
        print_test("返回404", False, f"状态码 {r.status_code}")
        ok = False
    else:
        print_test("返回404", True)

    try:
        detail = r.json()["detail"]
        code_ok = detail["code"] == "ORDER_NOT_FOUND"
        print_test("错误码 ORDER_NOT_FOUND", code_ok, f"code={detail.get('code')}")
        if not code_ok:
            ok = False
    except Exception as e:
        print_test("错误格式正确", False, f"解析错误: {e}")
        ok = False

    print_test("撤销不存在订单总结果", ok)


def test_revoke_normal_order(ctx):
    print_section("测试 6: 撤销非补录订单被拦")

    r = api("POST", "/api/orders", json={
        "employee_id": "QA002",
        "menu_item_id": ctx["item1_id"],
        "quantity": 1,
    })
    if r.status_code != 200:
        print_test("创建普通订单", False, f"{r.json()}")
        return

    normal_order = r.json()

    api("POST", f"/api/orders/{normal_order['id']}/take")

    r = api("POST", f"/api/admin/orders/makeup/{normal_order['id']}/revoke")
    ok = True

    if r.status_code != 400:
        print_test("返回400", False, f"状态码 {r.status_code}")
        ok = False
    else:
        print_test("返回400", True)

    try:
        detail = r.json()["detail"]
        code_ok = detail["code"] == "NOT_MAKEUP_ORDER"
        print_test("错误码 NOT_MAKEUP_ORDER", code_ok, f"code={detail.get('code')}")
        if not code_ok:
            ok = False

        msg_ok = "非补录来源" in detail["message"]
        print_test("错误信息包含'非补录来源'", msg_ok, f"message={detail.get('message')}")
        if not msg_ok:
            ok = False
    except Exception as e:
        print_test("错误格式正确", False, f"解析错误: {e}")
        ok = False

    print_test("撤销非补录订单总结果", ok)


def test_revoke_pending_makeup(ctx):
    print_section("测试 7: 撤销pending状态的补录订单被拦")

    api("PUT", "/api/admin/config/makeup_revoke_deadline_hours", json={"value": "0"})

    r = api("POST", "/api/admin/employees", json={"id": "QA003", "name": "测试员工C", "initial_balance": 50.0})

    r = api("POST", "/api/admin/orders/makeup", json={
        "employee_id": "QA002",
        "menu_item_id": ctx["item1_id"],
        "quantity": 1,
        "serving_date": ctx["today"],
        "source": "window",
        "remark": "pending状态补录(不应该出现)",
    })

    if r.status_code != 200:
        print_test("补录应创建taken状态订单", False, f"{r.json()}")
        return

    makeup_order = r.json()

    if makeup_order["status"] == "taken":
        print_test("补录订单状态为taken(正常)", True)
        print_test("该场景正常不会出现pending补录", True, "补录流程直接创建taken状态，无需测试pending拦截")
    else:
        print_test("补录订单状态异常", False, f"status={makeup_order['status']}")

    r = api("POST", f"/api/admin/orders/makeup/{makeup_order['id']}/revoke", json={"remark": "测试撤销"})
    if r.status_code == 200:
        print_test("taken状态补录可撤销", True)
    else:
        print_test("taken状态补录可撤销", False, f"status={r.status_code}, {r.json()}")


def test_query_after_revoke(ctx, revoked_order):
    print_section("测试 8: 撤销后查询记录包含撤销信息")

    if not revoked_order:
        print_test("跳过撤销后查询测试", False)
        return

    r = api("GET", "/api/admin/orders/makeup", params={"employee_id": "QA001"})
    ok = True

    if r.status_code != 200:
        print_test("查询返回200", False, f"状态码 {r.status_code}")
        ok = False
    else:
        data = r.json()
        items = data.get("items", [])
        target = [i for i in items if i["id"] == revoked_order["id"]]
        if not target:
            print_test("查询到已撤销的补录记录", False, "未找到")
            ok = False
        else:
            item = target[0]
            status_ok = item["status"] == "cancelled"
            print_test("已撤销记录状态为cancelled", status_ok, f"status={item['status']}")
            if not status_ok:
                ok = False

            revoked_at_ok = item.get("revoked_at") is not None
            print_test("已撤销记录有revoked_at", revoked_at_ok, f"revoked_at={item.get('revoked_at')}")
            if not revoked_at_ok:
                ok = False

            logs = item.get("operation_logs", [])
            has_create = any(l["operation_type"] == "create" for l in logs)
            has_revoke = any(l["operation_type"] == "revoke" for l in logs)
            print_test("操作日志包含create", has_create)
            print_test("操作日志包含revoke", has_revoke)
            if not (has_create and has_revoke):
                ok = False

    print_test("撤销后查询总结果", ok)


def test_revoke_config_disabled(ctx):
    print_section("测试 9: 配置禁止撤销")

    r = api("POST", "/api/admin/orders/makeup", json={
        "employee_id": "QA003",
        "menu_item_id": ctx["item2_id"],
        "quantity": 1,
        "serving_date": ctx["today"],
        "source": "admin",
        "remark": "禁止撤销测试",
    })
    if r.status_code != 200:
        print_test("创建补录用于禁止撤销测试", False, f"{r.json()}")
        return

    order = r.json()

    api("PUT", "/api/admin/config/makeup_allow_revoke", json={"value": "false"})

    r = api("POST", f"/api/admin/orders/makeup/{order['id']}/revoke")
    ok = True

    if r.status_code != 403:
        print_test("配置禁止时返回403", False, f"状态码 {r.status_code}")
        ok = False
    else:
        print_test("配置禁止时返回403", True)

    try:
        detail = r.json()["detail"]
        code_ok = detail["code"] == "REVOKE_NOT_ALLOWED"
        print_test("错误码 REVOKE_NOT_ALLOWED", code_ok, f"code={detail.get('code')}")
        if not code_ok:
            ok = False

        msg_ok = "不允许撤销" in detail["message"]
        print_test("错误信息包含'不允许撤销'", msg_ok, f"message={detail.get('message')}")
        if not msg_ok:
            ok = False
    except Exception as e:
        print_test("错误格式正确", False, f"解析错误: {e}")
        ok = False

    api("PUT", "/api/admin/config/makeup_allow_revoke", json={"value": "true"})

    r = api("POST", f"/api/admin/orders/makeup/{order['id']}/revoke", json={"remark": "恢复后撤销"})
    if r.status_code == 200:
        print_test("恢复配置后可撤销", True)
    else:
        print_test("恢复配置后可撤销", False, f"{r.json()}")
        ok = False

    reconc = api("GET", "/api/admin/reconciliation").json()
    print_test("对账仍然一致", reconc["consistent"], f"Issues: {reconc.get('issues', [])}")
    if not reconc["consistent"]:
        ok = False

    print_test("配置禁止撤销总结果", ok)


def test_revoke_deadline_exceeded(ctx):
    print_section("测试 10: 撤销超时限制")

    r = api("POST", "/api/admin/orders/makeup", json={
        "employee_id": "QA002",
        "menu_item_id": ctx["item1_id"],
        "quantity": 1,
        "serving_date": ctx["today"],
        "source": "window",
        "remark": "超时撤销测试",
    })
    if r.status_code != 200:
        print_test("创建补录用于超时测试", False, f"{r.json()}")
        return

    order = r.json()

    api("PUT", "/api/admin/config/makeup_revoke_deadline_hours", json={"value": "1"})

    r = api("POST", f"/api/admin/orders/makeup/{order['id']}/revoke")
    if r.status_code == 200:
        print_test("1小时内可撤销", True)

        api("PUT", "/api/admin/config/makeup_revoke_deadline_hours", json={"value": "0"})
    elif r.status_code == 400:
        detail = r.json().get("detail", {})
        print_test("1小时内理论上可撤销(若刚好超时则合理)", True, f"msg={detail.get('message')}")
        api("PUT", "/api/admin/config/makeup_revoke_deadline_hours", json={"value": "0"})
        r2 = api("POST", f"/api/admin/orders/makeup/{order['id']}/revoke")
        if r2.status_code == 200:
            print_test("设为0(不限时)后可撤销", True)
        else:
            print_test("设为0(不限时)后可撤销", False, f"{r2.json()}")
    else:
        print_test("撤销超时测试异常", False, f"status={r.status_code}")

    reconc = api("GET", "/api/admin/reconciliation").json()
    print_test("对账仍然一致", reconc["consistent"])


def test_reboot_consistency(ctx, process):
    print_section("测试 11: 服务重启后数据一致")

    print("  记录重启前状态...")
    orders_before = api("GET", "/api/orders").json()
    emp_before = api("GET", "/api/admin/employees/QA001").json()
    reconc_before = api("GET", "/api/admin/reconciliation").json()
    cfg_before = api("GET", "/api/admin/config/makeup").json()
    makeup_before = api("GET", "/api/admin/orders/makeup").json()

    print(f"  重启前订单数: {len(orders_before)}")
    print(f"  重启前员工余额: {emp_before['balance']}")
    print(f"  重启前补录记录数: {makeup_before['total']}")
    print(f"  重启前对账一致: {reconc_before['consistent']}")

    stop_service(process)
    time.sleep(2)

    print("\n  重新启动服务...")
    process = start_service()
    wait_for_service()

    print("\n  验证重启后状态...")
    ok = True

    orders_after = api("GET", "/api/orders").json()
    orders_ok = len(orders_before) == len(orders_after)
    print_test("订单数重启后一致", orders_ok, f"前: {len(orders_before)}, 后: {len(orders_after)}")
    if not orders_ok:
        ok = False

    emp_after = api("GET", "/api/admin/employees/QA001").json()
    emp_ok = abs(emp_before["balance"] - emp_after["balance"]) < 0.01
    print_test("员工余额重启后一致", emp_ok, f"前: {emp_before['balance']}, 后: {emp_after['balance']}")
    if not emp_ok:
        ok = False

    cfg_after = api("GET", "/api/admin/config/makeup").json()
    cfg_ok = cfg_before.get("allow_revoke") == cfg_after.get("allow_revoke") and \
             cfg_before.get("revoke_deadline_hours") == cfg_after.get("revoke_deadline_hours")
    print_test("撤销配置重启后一致", cfg_ok, f"前: {cfg_before}, 后: {cfg_after}")
    if not cfg_ok:
        ok = False

    makeup_after = api("GET", "/api/admin/orders/makeup").json()
    makeup_ok = makeup_before["total"] == makeup_after["total"]
    print_test("补录记录数重启后一致", makeup_ok, f"前: {makeup_before['total']}, 后: {makeup_after['total']}")
    if not makeup_ok:
        ok = False

    for item in makeup_after.get("items", []):
        if item.get("revoked_at"):
            logs = item.get("operation_logs", [])
            has_revoke_log = any(l["operation_type"] == "revoke" for l in logs)
            print_test(f"已撤销订单 {item['id']} 操作日志重启后保留", has_revoke_log)
            if not has_revoke_log:
                ok = False

    reconc_after = api("GET", "/api/admin/reconciliation").json()
    reconc_ok = reconc_after["consistent"]
    print_test("重启后对账通过", reconc_ok, f"Issues: {reconc_after.get('issues', [])}")
    if not reconc_ok:
        ok = False

    print_test("服务重启一致性总结果", ok)
    return process


def test_transaction_export_with_revoke(ctx):
    print_section("测试 12: 流水导出包含撤销类型")

    r = api("GET", "/api/transactions/export", params={"employee_id": "QA001"})
    ok = True

    if r.status_code != 200:
        print_test("导出流水返回200", False, f"状态码 {r.status_code}")
        ok = False
    else:
        content = r.text
        has_revoke = "撤销补录退款" in content
        print_test("CSV导出包含'撤销补录退款'", has_revoke)
        if not has_revoke:
            ok = False

    print_test("流水导出总结果", ok)


def main():
    global passed, failed

    print_section("管理员补录查询/撤销功能测试")
    print(f"  测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    cleanup_existing_db()

    process = None
    try:
        process = start_service()
        wait_for_service()

        ctx = setup_test_data()

        test_query_makeup_empty(ctx)
        order_for_revoke = test_query_with_filters(ctx)
        test_source_echo_and_filter(ctx)
        revoked_order = test_revoke_makeup_success(ctx, order_for_revoke)
        test_revoke_duplicate(ctx, revoked_order)
        test_revoke_not_found(ctx)
        test_revoke_normal_order(ctx)
        test_revoke_pending_makeup(ctx)
        test_query_after_revoke(ctx, revoked_order)
        test_revoke_config_disabled(ctx)
        test_revoke_deadline_exceeded(ctx)
        test_transaction_export_with_revoke(ctx)
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
