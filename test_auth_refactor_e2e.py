"""
权限重构回归测试
验证统一鉴权守卫是否正确工作，覆盖三类请求：
1. 匿名无头请求 -> 401 UNAUTHORIZED
2. 无权限有头请求 -> 403 PERMISSION_DENIED
3. 授权用户请求 -> 200 OK / 正常业务逻辑

同时验证不破坏已有授权成功链路、作业持久化、撤销或回放、重启后查询。
"""
import json
import os
import sys
import time
import sqlite3
import subprocess
import tempfile
import uuid

import requests

BASE_URL = None
DB_PATH = None
SERVER_PROC = None
SERVER_PORT = 18999

PASS = 0
FAIL = 0
ERRORS = []


def _report(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        ERRORS.append(f"{name}: {detail}")
        print(f"  [FAIL] {name} -- {detail}")


def start_server():
    global BASE_URL, DB_PATH, SERVER_PROC
    DB_PATH = os.path.join(tempfile.gettempdir(), f"auth_refactor_test_{uuid.uuid4().hex[:8]}.db")
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    BASE_URL = f"http://127.0.0.1:{SERVER_PORT}"
    env = os.environ.copy()
    env["CANTEEN_DB_PATH"] = DB_PATH
    SERVER_PROC = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(SERVER_PORT)],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    for _ in range(30):
        try:
            r = requests.get(f"{BASE_URL}/api/health", timeout=2)
            if r.status_code == 200:
                print(f"  服务启动成功 port={SERVER_PORT} db={DB_PATH}")
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("服务启动超时")


def stop_server():
    global SERVER_PROC
    if SERVER_PROC:
        SERVER_PROC.terminate()
        try:
            SERVER_PROC.wait(timeout=10)
        except Exception:
            SERVER_PROC.kill()
        SERVER_PROC = None


def restart_server():
    stop_server()
    time.sleep(1)
    global SERVER_PROC
    env = os.environ.copy()
    env["CANTEEN_DB_PATH"] = DB_PATH
    SERVER_PROC = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(SERVER_PORT)],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    for _ in range(30):
        try:
            r = requests.get(f"{BASE_URL}/api/health", timeout=2)
            if r.status_code == 200:
                print(f"  服务重启成功 port={SERVER_PORT}")
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("服务重启超时")


def cleanup_db():
    global DB_PATH
    if DB_PATH and os.path.exists(DB_PATH):
        try:
            os.remove(DB_PATH)
        except Exception:
            pass


def _get_error_code(response):
    try:
        data = response.json()
        return data.get("detail", {}).get("code", "UNKNOWN")
    except Exception:
        return "PARSE_ERROR"


# ============================================================
# 测试用例
# ============================================================

TEST_JOB_ID = None


def grant_permissions():
    """预先授予各种权限给测试用户"""
    print("\n=== 预先授权 ===")
    
    permissions = [
        ("auditor", "import_audit_view"),
        ("auditor", "import_audit_export"),
        ("importer", "import_manage"),
        ("revoker", "import_revoke"),
        ("revoker", "import_audit_view"),
        ("superadmin", "import_audit_view"),
        ("superadmin", "import_audit_export"),
        ("superadmin", "import_manage"),
        ("superadmin", "import_revoke"),
    ]
    
    for user_id, perm_type in permissions:
        r = requests.post(
            f"{BASE_URL}/api/admin/import-replay/permissions/grant",
            json={"target_user_id": user_id, "permission_type": perm_type},
            headers={"X-Operator": "admin"},
        )
        ok = r.status_code == 200
        _report(f"授予 {user_id} {perm_type}", ok, r.text if not ok else "")


def test_anonymous_access():
    """测试匿名请求（不带 X-User-Id 头）"""
    print("\n=== 测试1: 匿名请求拦截（无头）===")
    
    test_cases = [
        ("GET", "/api/admin/source-rules/import", "导入历史列表"),
        ("GET", "/api/admin/source-rules/export/json", "导出规则JSON"),
        ("GET", "/api/admin/source-rules/export/csv", "导出规则CSV"),
        ("GET", "/api/admin/source-rules/audit-log", "审计日志"),
        ("GET", "/api/admin/import-replay/jobs", "作业列表"),
        ("GET", "/api/admin/import-replay/audit-log", "结构化审计日志"),
        ("GET", "/api/admin/import-replay/lineage", "溯源链"),
        ("POST", "/api/admin/source-rules/import", "执行导入"),
        ("POST", "/api/admin/source-rules/import/dry-run", "导入预检"),
    ]
    
    for method, path, desc in test_cases:
        if method == "GET":
            r = requests.get(f"{BASE_URL}{path}")
        else:
            r = requests.post(
                f"{BASE_URL}{path}",
                json={"rules": [{"code": "TEST", "name": "test"}], "conflict_strategy": "skip"},
            )
        
        status_ok = r.status_code == 401
        code_ok = _get_error_code(r) == "UNAUTHORIZED"
        ok = status_ok and code_ok
        _report(
            f"匿名访问{desc} -> 401",
            ok,
            f"status={r.status_code}, code={_get_error_code(r)}",
        )


def test_no_permission_access():
    """测试无权限用户请求（带 X-User-Id 头但无权限）"""
    print("\n=== 测试2: 无权限用户拦截（有头但无权限）===")
    
    test_cases = [
        ("GET", "/api/admin/source-rules/import-history", "import_audit_view", "导入历史列表"),
        ("GET", "/api/admin/source-rules/export/json", "import_audit_export", "导出规则JSON"),
        ("GET", "/api/admin/source-rules/export/csv", "import_audit_export", "导出规则CSV"),
        ("GET", "/api/admin/source-rules/audit-log", "import_audit_view", "审计日志"),
        ("GET", "/api/admin/import-replay/jobs", "import_audit_view", "作业列表"),
        ("GET", "/api/admin/import-replay/audit-log", "import_audit_view", "结构化审计日志"),
        ("GET", "/api/admin/import-replay/lineage", "import_audit_view", "溯源链"),
        ("POST", "/api/admin/source-rules/import", "import_manage", "执行导入"),
        ("POST", "/api/admin/source-rules/import/dry-run", "import_manage", "导入预检"),
    ]
    
    for method, path, required_perm, desc in test_cases:
        headers = {"X-User-Id": "nobody"}
        if method == "GET":
            r = requests.get(f"{BASE_URL}{path}", headers=headers)
        else:
            r = requests.post(
                f"{BASE_URL}{path}",
                json={"rules": [{"code": "TEST", "name": "test"}], "conflict_strategy": "skip"},
                headers=headers,
            )
        
        status_ok = r.status_code == 403
        code_ok = _get_error_code(r) == "PERMISSION_DENIED"
        ok = status_ok and code_ok
        _report(
            f"无权限用户访问{desc} -> 403",
            ok,
            f"status={r.status_code}, code={_get_error_code(r)}",
        )


def test_authorized_import_and_export():
    """测试授权用户的导入和导出功能"""
    print("\n=== 测试3: 授权用户导入与导出 ===")
    
    # 测试 dry-run
    r = requests.post(
        f"{BASE_URL}/api/admin/source-rules/import/dry-run",
        json={
            "rules": [
                {"code": "TEST_RULE_1", "name": "测试规则1", "category": "general", "priority": 10},
                {"code": "TEST_RULE_2", "name": "测试规则2", "category": "system", "priority": 20},
            ],
            "conflict_strategy": "skip",
            "dry_run": True,
        },
        headers={"X-User-Id": "importer", "X-Operator": "importer"},
    )
    ok = r.status_code == 200
    _report("授权用户 dry-run 导入", ok, r.text[:200] if not ok else "")
    
    # 测试真实导入
    r = requests.post(
        f"{BASE_URL}/api/admin/source-rules/import",
        json={
            "rules": [
                {"code": "TEST_RULE_1", "name": "测试规则1", "category": "general", "priority": 10},
                {"code": "TEST_RULE_2", "name": "测试规则2", "category": "system", "priority": 20},
            ],
            "conflict_strategy": "skip",
            "dry_run": False,
        },
        headers={"X-User-Id": "importer", "X-Operator": "importer"},
    )
    ok = r.status_code == 200
    _report("授权用户执行导入", ok, r.text[:200] if not ok else "")
    
    global TEST_JOB_ID
    if ok:
        data = r.json()
        TEST_JOB_ID = data.get("job_id")
        _report("导入返回 job_id", TEST_JOB_ID is not None, str(TEST_JOB_ID))
    
    # 测试导出来源规则 JSON
    r = requests.get(
        f"{BASE_URL}/api/admin/source-rules/export/json",
        headers={"X-User-Id": "auditor"},
    )
    ok = r.status_code == 200
    _report("授权用户导出规则 JSON", ok, f"status={r.status_code}")
    
    # 测试导出来源规则 CSV
    r = requests.get(
        f"{BASE_URL}/api/admin/source-rules/export/csv",
        headers={"X-User-Id": "auditor"},
    )
    ok = r.status_code == 200
    _report("授权用户导出规则 CSV", ok, f"status={r.status_code}")


def test_authorized_audit_access():
    """测试授权用户的审计访问功能"""
    print("\n=== 测试4: 授权用户审计访问 ===")
    
    if not TEST_JOB_ID:
        print("  跳过：无作业ID")
        return
    
    # 作业列表
    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs",
        headers={"X-User-Id": "auditor"},
    )
    ok = r.status_code == 200
    _report("授权用户查作业列表", ok, f"status={r.status_code}")
    if ok:
        data = r.json()
        _report("作业列表 has_audit_permission=True", data.get("has_audit_permission") is True, str(data.get("has_audit_permission")))
    
    # 作业详情
    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs/{TEST_JOB_ID}",
        headers={"X-User-Id": "auditor"},
    )
    ok = r.status_code == 200
    _report("授权用户查作业详情", ok, f"status={r.status_code}")
    
    # 作业明细
    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs/{TEST_JOB_ID}/details",
        headers={"X-User-Id": "auditor"},
    )
    ok = r.status_code == 200
    _report("授权用户查作业明细", ok, f"status={r.status_code}")
    
    # 作业快照
    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs/{TEST_JOB_ID}/snapshots",
        headers={"X-User-Id": "auditor"},
    )
    ok = r.status_code == 200
    _report("授权用户查作业快照", ok, f"status={r.status_code}")
    
    # 作业冲突
    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs/{TEST_JOB_ID}/conflicts",
        headers={"X-User-Id": "auditor"},
    )
    ok = r.status_code == 200
    _report("授权用户查作业冲突", ok, f"status={r.status_code}")
    
    # 结构化审计日志
    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/audit-log",
        headers={"X-User-Id": "auditor"},
    )
    ok = r.status_code == 200
    _report("授权用户查结构化审计日志", ok, f"status={r.status_code}")
    
    # 溯源链
    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/lineage",
        params={"rule_code": "TEST_RULE_1"},
        headers={"X-User-Id": "auditor"},
    )
    ok = r.status_code == 200
    _report("授权用户查溯源链", ok, f"status={r.status_code}")
    
    # 导入历史
    r = requests.get(
        f"{BASE_URL}/api/admin/source-rules/import-history",
        headers={"X-User-Id": "auditor"},
    )
    ok = r.status_code == 200
    _report("授权用户查导入历史", ok, f"status={r.status_code}")
    
    # 审计日志
    r = requests.get(
        f"{BASE_URL}/api/admin/source-rules/audit-log",
        headers={"X-User-Id": "auditor"},
    )
    ok = r.status_code == 200
    _report("授权用户查审计日志", ok, f"status={r.status_code}")


def test_job_export():
    """测试作业导出功能"""
    print("\n=== 测试5: 作业导出功能 ===")
    
    if not TEST_JOB_ID:
        print("  跳过：无作业ID")
        return
    
    # 作业 JSON 导出
    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs/{TEST_JOB_ID}/export/json",
        params={"export_type": "full"},
        headers={"X-User-Id": "auditor"},
    )
    ok = r.status_code == 200
    _report("授权用户导出作业 JSON", ok, f"status={r.status_code}")
    
    # 作业 CSV 导出
    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs/{TEST_JOB_ID}/export/csv",
        params={"export_type": "details"},
        headers={"X-User-Id": "auditor"},
    )
    ok = r.status_code == 200
    _report("授权用户导出作业 CSV", ok, f"status={r.status_code}")


def test_revoke_and_replay():
    """测试撤销和回放功能"""
    print("\n=== 测试6: 撤销与回放 ===")
    
    if not TEST_JOB_ID:
        print("  跳过：无作业ID")
        return
    
    # 先测试无撤销权限的用户
    r = requests.post(
        f"{BASE_URL}/api/admin/import-replay/jobs/{TEST_JOB_ID}/revoke",
        json={"reason": "测试撤销"},
        headers={"X-User-Id": "auditor", "X-Operator": "auditor"},
    )
    ok = r.status_code == 403
    _report("无撤销权限用户被拦截", ok, f"status={r.status_code}")
    
    # 有撤销权限的用户执行撤销
    r = requests.post(
        f"{BASE_URL}/api/admin/import-replay/jobs/{TEST_JOB_ID}/revoke",
        json={"reason": "测试撤销"},
        headers={"X-User-Id": "revoker", "X-Operator": "revoker"},
    )
    ok = r.status_code == 200
    _report("授权用户撤销作业", ok, f"status={r.status_code}, body={r.text[:200]}")
    
    # 查看回放数据
    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs/{TEST_JOB_ID}/replay",
        headers={"X-User-Id": "revoker"},
    )
    ok = r.status_code == 200
    _report("授权用户查看回放数据", ok, f"status={r.status_code}")
    
    # 验证规则已被撤销
    r = requests.get(f"{BASE_URL}/api/admin/source-rules/TEST_RULE_1")
    ok = r.status_code == 404
    _report("撤销后新建规则被删除", ok, f"status={r.status_code}")


def test_restart_persistence():
    """测试重启后数据持久化"""
    print("\n=== 测试7: 重启后持久化 ===")
    
    if not TEST_JOB_ID:
        print("  跳过：无作业ID")
        return
    
    restart_server()
    
    # 重启后查询作业
    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs/{TEST_JOB_ID}",
        headers={"X-User-Id": "auditor"},
    )
    ok = r.status_code == 200
    _report("重启后查作业详情", ok, f"status={r.status_code}")
    
    # 重启后查溯源链
    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/lineage",
        params={"rule_code": "TEST_RULE_1"},
        headers={"X-User-Id": "auditor"},
    )
    ok = r.status_code == 200
    _report("重启后查溯源链", ok, f"status={r.status_code}")
    
    # 重启后权限仍然生效（匿名应该被拒）
    r = requests.get(f"{BASE_URL}/api/admin/import-replay/jobs")
    ok = r.status_code == 401
    _report("重启后匿名仍被拦截", ok, f"status={r.status_code}")
    
    # 重启后授权用户仍可访问
    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs",
        headers={"X-User-Id": "auditor"},
    )
    ok = r.status_code == 200
    _report("重启后授权用户仍可访问", ok, f"status={r.status_code}")


def test_default_admin():
    """测试默认管理员权限"""
    print("\n=== 测试8: 默认管理员权限 ===")
    
    # admin 是默认管理员（import_audit_default_admin = admin）
    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs",
        headers={"X-User-Id": "admin"},
    )
    ok = r.status_code == 200
    _report("默认管理员 admin 可访问作业列表", ok, f"status={r.status_code}")
    
    r = requests.post(
        f"{BASE_URL}/api/admin/source-rules/import",
        json={
            "rules": [{"code": "ADMIN_TEST", "name": "管理员测试规则", "category": "general", "priority": 5}],
            "conflict_strategy": "skip",
        },
        headers={"X-User-Id": "admin", "X-Operator": "admin"},
    )
    ok = r.status_code == 200
    _report("默认管理员 admin 可执行导入", ok, f"status={r.status_code}")


def main():
    global TEST_JOB_ID
    TEST_JOB_ID = None
    
    print(f"权限重构回归测试")
    print(f"=" * 60)
    
    try:
        start_server()
        
        grant_permissions()
        test_anonymous_access()
        test_no_permission_access()
        test_authorized_import_and_export()
        test_authorized_audit_access()
        test_job_export()
        test_revoke_and_replay()
        test_restart_persistence()
        test_default_admin()
        
    except Exception as e:
        print(f"\n!!! 测试异常: {e}")
        import traceback
        traceback.print_exc()
    finally:
        stop_server()
        cleanup_db()
    
    print(f"\n{'=' * 60}")
    print(f"测试结果: PASS={PASS}  FAIL={FAIL}")
    if ERRORS:
        print(f"\n失败项:")
        for e in ERRORS:
            print(f"  - {e}")
    print(f"{'=' * 60}")
    
    if FAIL > 0:
        sys.exit(1)
    else:
        print("全部通过!")
        sys.exit(0)


if __name__ == "__main__":
    main()
