"""
导入作业审计模块 端到端测试
真实启动服务，用 requests 跑通授权、导入、撤销、重启后查询和导出核对
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


def start_server(port=18765):
    global BASE_URL, DB_PATH, SERVER_PROC
    DB_PATH = os.path.join(tempfile.gettempdir(), f"e2e_import_audit_{uuid.uuid4().hex[:8]}.db")
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    BASE_URL = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["CANTEEN_DB_PATH"] = DB_PATH
    SERVER_PROC = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    for _ in range(30):
        try:
            r = requests.get(f"{BASE_URL}/api/health", timeout=2)
            if r.status_code == 200:
                print(f"  服务启动成功 port={port} db={DB_PATH}")
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


def restart_server(port=18765):
    stop_server()
    time.sleep(1)
    global BASE_URL, SERVER_PROC
    env = os.environ.copy()
    env["CANTEEN_DB_PATH"] = DB_PATH
    SERVER_PROC = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    for _ in range(30):
        try:
            r = requests.get(f"{BASE_URL}/api/health", timeout=2)
            if r.status_code == 200:
                print(f"  服务重启成功 port={port}")
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


# ============================================================
# 测试用例
# ============================================================


def test_01_grant_permission():
    print("\n=== 测试1: 授权生效 ===")
    r = requests.post(
        f"{BASE_URL}/api/admin/import-replay/permissions/grant",
        json={"target_user_id": "auditor", "permission_type": "import_audit_view"},
        headers={"X-Operator": "admin"},
    )
    _report("授予审计查看权限", r.status_code == 200, r.text)

    r = requests.post(
        f"{BASE_URL}/api/admin/import-replay/permissions/grant",
        json={"target_user_id": "auditor", "permission_type": "import_audit_export"},
        headers={"X-Operator": "admin"},
    )
    _report("授予审计导出权限", r.status_code == 200, r.text)

    r = requests.post(
        f"{BASE_URL}/api/admin/import-replay/permissions/grant",
        json={"target_user_id": "revoker", "permission_type": "import_revoke"},
        headers={"X-Operator": "admin"},
    )
    _report("授予撤销权限", r.status_code == 200, r.text)

    r = requests.post(
        f"{BASE_URL}/api/admin/import-replay/permissions/grant",
        json={"target_user_id": "revoker", "permission_type": "import_audit_view"},
        headers={"X-Operator": "admin"},
    )
    _report("授予撤销用户审计查看权限", r.status_code == 200, r.text)

    r = requests.post(
        f"{BASE_URL}/api/admin/import-replay/permissions/grant",
        json={"target_user_id": "importer", "permission_type": "import_manage"},
        headers={"X-Operator": "admin"},
    )
    _report("授予导入管理权限", r.status_code == 200, r.text)


def test_02_permission_enforcement():
    print("\n=== 测试2: 权限拦截 ===")
    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs",
        headers={"X-User-Id": "nobody"},
    )
    data = r.json()
    _report("无权限用户查作业列表-脱敏", data.get("has_audit_permission") is False, str(data))

    r = requests.post(
        f"{BASE_URL}/api/admin/source-rules/import",
        json={"rules": [{"code": "X", "name": "X"}], "conflict_strategy": "skip"},
        headers={"X-User-Id": "nobody"},
    )
    _report("无权限用户导入-被拦截", r.status_code == 403, f"status={r.status_code}")

    r = requests.get(
        f"{BASE_URL}/api/admin/source-rules/export/json",
        headers={"X-User-Id": "nobody"},
    )
    _report("无权限用户导出JSON-被拦截", r.status_code == 403, f"status={r.status_code}")

    r = requests.get(
        f"{BASE_URL}/api/admin/source-rules/export/csv",
        headers={"X-User-Id": "nobody"},
    )
    _report("无权限用户导出CSV-被拦截", r.status_code == 403, f"status={r.status_code}")

    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/lineage",
        headers={"X-User-Id": "nobody"},
    )
    _report("无权限用户查溯源链-被拦截", r.status_code == 403, f"status={r.status_code}")


def test_03_manual_create_rule():
    print("\n=== 测试3: 手工创建规则 ===")
    r = requests.post(
        f"{BASE_URL}/api/admin/source-rules",
        json={
            "code": "RULE_A",
            "name": "手工规则A",
            "description": "手工创建的规则",
            "category": "general",
            "priority": 10,
            "is_enabled": True,
            "match_pattern": "rule_a_*",
        },
        headers={"X-Operator": "admin"},
    )
    _report("手工创建RULE_A", r.status_code == 200, r.text)
    data = r.json()
    _report("RULE_A import_origin=manual", data.get("import_origin") == "manual", str(data.get("import_origin")))

    r = requests.post(
        f"{BASE_URL}/api/admin/source-rules",
        json={
            "code": "RULE_B",
            "name": "手工规则B",
            "description": "另一个手工规则",
            "category": "system",
            "priority": 20,
            "is_enabled": True,
        },
        headers={"X-Operator": "admin"},
    )
    _report("手工创建RULE_B", r.status_code == 200, r.text)


def test_04_first_import():
    print("\n=== 测试4: 首批导入（含覆盖+新建）===")
    import_payload = {
        "rules": [
            {
                "code": "RULE_A",
                "name": "导入覆盖规则A",
                "description": "导入覆盖描述",
                "category": "import",
                "priority": 50,
                "is_enabled": True,
                "match_pattern": "import_a_*",
            },
            {
                "code": "RULE_C",
                "name": "导入新建规则C",
                "description": "导入新建描述",
                "category": "import",
                "priority": 60,
                "is_enabled": True,
            },
        ],
        "conflict_strategy": "overwrite",
        "dry_run": False,
        "check_concurrent_modifications": True,
    }
    r = requests.post(
        f"{BASE_URL}/api/admin/source-rules/import",
        json=import_payload,
        headers={"X-Operator": "importer", "X-User-Id": "importer"},
    )
    _report("导入请求成功", r.status_code == 200, r.text)
    data = r.json()
    _report("导入成功标记", data.get("success") is True, str(data.get("success")))
    _report("新增1条", data.get("summary", {}).get("created") == 1, str(data.get("summary")))
    _report("覆盖1条", data.get("summary", {}).get("overwritten") == 1, str(data.get("summary")))
    _report("有job_id", data.get("job_id") is not None, str(data.get("job_id")))

    global JOB_ID
    JOB_ID = data.get("job_id")

    r = requests.get(
        f"{BASE_URL}/api/admin/source-rules/RULE_A",
    )
    rule_a = r.json()
    _report("RULE_A被覆盖", rule_a.get("name") == "导入覆盖规则A", rule_a.get("name"))
    _report("RULE_A import_origin=import", rule_a.get("import_origin") == "import", str(rule_a.get("import_origin")))

    r = requests.get(
        f"{BASE_URL}/api/admin/source-rules/RULE_C",
    )
    rule_c = r.json()
    _report("RULE_C新建成功", rule_c.get("name") == "导入新建规则C", rule_c.get("name"))


def test_05_manual_update_after_import():
    print("\n=== 测试5: 导入后人工修改 ===")
    r = requests.patch(
        f"{BASE_URL}/api/admin/source-rules/RULE_A",
        json={
            "name": "人工修改后的规则A",
            "description": "人工修改描述",
            "priority": 99,
        },
        headers={"X-Operator": "admin"},
    )
    _report("人工修改RULE_A", r.status_code == 200, r.text)
    data = r.json()
    _report("RULE_A名称已改", data.get("name") == "人工修改后的规则A", data.get("name"))
    _report("RULE_A import_origin=manual", data.get("import_origin") == "manual", str(data.get("import_origin")))


def test_06_re_import_after_manual():
    print("\n=== 测试6: 再次导入（人工修改后再导入覆盖）===")
    import_payload = {
        "rules": [
            {
                "code": "RULE_A",
                "name": "第二次导入覆盖A",
                "description": "第二次导入",
                "category": "import",
                "priority": 70,
                "is_enabled": True,
            },
            {
                "code": "RULE_D",
                "name": "第二次导入新建D",
                "description": "新建D",
                "category": "general",
                "priority": 30,
                "is_enabled": True,
            },
        ],
        "conflict_strategy": "overwrite",
        "dry_run": False,
    }
    r = requests.post(
        f"{BASE_URL}/api/admin/source-rules/import",
        json=import_payload,
        headers={"X-Operator": "importer", "X-User-Id": "importer"},
    )
    _report("第二次导入成功", r.status_code == 200, r.text)
    data = r.json()
    _report("第二次导入覆盖1条", data.get("summary", {}).get("overwritten") == 1, str(data.get("summary")))
    _report("第二次导入新建1条", data.get("summary", {}).get("created") == 1, str(data.get("summary")))

    global JOB_ID_2
    JOB_ID_2 = data.get("job_id")

    r = requests.get(
        f"{BASE_URL}/api/admin/source-rules/RULE_A",
    )
    rule_a = r.json()
    _report("RULE_A再次被覆盖", rule_a.get("name") == "第二次导入覆盖A", rule_a.get("name"))
    _report("RULE_A import_origin=import", rule_a.get("import_origin") == "import", str(rule_a.get("import_origin")))


def test_07_lineage_tracking():
    print("\n=== 测试7: 溯源链查询 ===")
    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/lineage",
        params={"rule_code": "RULE_A"},
        headers={"X-User-Id": "auditor"},
    )
    _report("溯源链查询成功", r.status_code == 200, r.text)
    data = r.json()
    _report("溯源链有审计权限", data.get("has_audit_permission") is True, str(data.get("has_audit_permission")))
    lineage = data.get("lineage", [])
    source_types = [e["source_type"] for e in lineage]
    _report("溯源链包含manual_create", "manual_create" in source_types, str(source_types))
    _report("溯源链包含import_overwrite", "import_overwrite" in source_types, str(source_types))
    _report("溯源链包含manual_update", "manual_update" in source_types, str(source_types))
    _report("溯源链至少4条", len(lineage) >= 4, f"实际{len(lineage)}条")

    for entry in lineage:
        _report(
            f"溯源条目有content_hash source_type={entry['source_type']}",
            entry.get("content_hash") is not None and len(entry["content_hash"]) == 16,
            str(entry.get("content_hash")),
        )


def test_08_import_job_details():
    print("\n=== 测试8: 作业明细与快照 ===")
    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs/{JOB_ID}",
        headers={"X-User-Id": "auditor"},
    )
    _report("查作业详情-有权限", r.json().get("has_audit_permission") is True, r.text[:200])

    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs/{JOB_ID}/details",
        headers={"X-User-Id": "auditor"},
    )
    data = r.json()
    _report("作业明细查询成功", data.get("success") is True, str(data.get("success")))
    details = data.get("details", [])
    _report("作业明细2条", len(details) == 2, f"实际{len(details)}条")

    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs/{JOB_ID}/snapshots",
        params={"snapshot_type": "before_import"},
        headers={"X-User-Id": "auditor"},
    )
    data = r.json()
    _report("快照查询成功", data.get("success") is True)
    snapshots = data.get("snapshots", [])
    _report("before_import快照有1条(RULE_A)", len(snapshots) >= 1, f"实际{len(snapshots)}条")

    snap_rule_a = next((s for s in snapshots if s["rule_code"] == "RULE_A"), None)
    if snap_rule_a:
        _report("快照RULE_A名称=手工规则A", snap_rule_a["rule_json"]["name"] == "手工规则A", str(snap_rule_a["rule_json"].get("name")))
    else:
        _report("快照RULE_A名称=手工规则A", False, "RULE_A快照不存在")


def test_09_structured_audit_log():
    print("\n=== 测试9: 结构化审计日志 ===")
    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/audit-log",
        params={"rule_code": "RULE_A"},
        headers={"X-User-Id": "auditor"},
    )
    data = r.json()
    _report("审计日志查询成功", data.get("success") is True)
    _report("有审计权限", data.get("has_audit_permission") is True)
    logs = data.get("audit_logs", [])
    ops = [l["operation"] for l in logs]
    _report("审计日志含create", "create" in ops, str(ops))
    _report("审计日志含import_overwrite", "import_overwrite" in ops, str(ops))
    _report("审计日志含update", "update" in ops, str(ops))

    for l in logs:
        _report(
            f"审计日志条目有timestamp op={l['operation']}",
            l.get("timestamp") is not None,
            str(l.get("timestamp")),
        )
        _report(
            f"审计日志条目有has_diff op={l['operation']}",
            "has_diff" in l,
            str(l.get("has_diff")),
        )


def test_10_revoke_import():
    print("\n=== 测试10: 撤销导入作业 ===")
    r = requests.post(
        f"{BASE_URL}/api/admin/import-replay/jobs/{JOB_ID}/revoke",
        json={"reason": "测试撤销第一批导入"},
        headers={"X-Operator": "revoker", "X-User-Id": "revoker"},
    )
    _report("撤销请求成功", r.status_code == 200, r.text[:300])
    data = r.json()
    _report("撤销结果success", data.get("success") is True, str(data.get("success")))
    _report("撤销有revoke_results", "revoke_results" in data, str(data.keys()))

    r = requests.get(
        f"{BASE_URL}/api/admin/source-rules/RULE_A",
    )
    rule_a = r.json()
    _report("RULE_A恢复为手工规则A", rule_a.get("name") == "手工规则A", rule_a.get("name"))
    _report("RULE_A import_origin=manual", rule_a.get("import_origin") == "manual", str(rule_a.get("import_origin")))

    r = requests.get(
        f"{BASE_URL}/api/admin/source-rules/RULE_C",
    )
    _report("RULE_C已被删除(撤销新建)", r.status_code == 404, f"status={r.status_code}")


def test_11_replay_after_revoke():
    print("\n=== 测试11: 撤销后回看 ===")
    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs/{JOB_ID}/replay",
        headers={"X-User-Id": "revoker"},
    )
    data = r.json()
    _report("回放数据查询成功", data.get("success") is True)
    _report("is_revoked=1", data.get("is_revoked") == 1, str(data.get("is_revoked")))
    _report("撤销验证通过", data.get("revoke_verification_passed") is True, str(data.get("revoke_verification_passed")))

    replay_data = data.get("replay_data", [])
    for step in replay_data:
        _report(
            f"回放步骤 code={step['rule_code']} verify={step['verify_result']}",
            step["verify_result"] == "matched",
            step["verify_result"],
        )

    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs/{JOB_ID}/snapshots",
        params={"snapshot_type": "after_revoke"},
        headers={"X-User-Id": "revoker"},
    )
    data = r.json()
    _report("after_revoke快照存在", len(data.get("snapshots", [])) > 0, f"count={len(data.get('snapshots', []))}")


def test_12_restart_persistence():
    print("\n=== 测试12: 重启后查询 ===")
    restart_server()

    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs",
        headers={"X-User-Id": "auditor"},
    )
    data = r.json()
    _report("重启后查作业列表", data.get("success") is True)
    _report("重启后作业数>=2", data.get("total", 0) >= 2, f"total={data.get('total')}")

    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs/{JOB_ID}",
        headers={"X-User-Id": "auditor"},
    )
    data = r.json()
    _report("重启后查作业详情-有权限", data.get("has_audit_permission") is True)
    _report("重启后作业is_revoked=1", data.get("is_revoked") == 1, str(data.get("is_revoked")))

    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/lineage",
        params={"rule_code": "RULE_A"},
        headers={"X-User-Id": "auditor"},
    )
    data = r.json()
    _report("重启后溯源链可查", data.get("success") is True)
    lineage = data.get("lineage", [])
    source_types = [e["source_type"] for e in lineage]
    _report("重启后溯源链含revoke_restore", "revoke_restore" in source_types, str(source_types))

    r = requests.get(
        f"{BASE_URL}/api/admin/source-rules/RULE_A",
    )
    rule_a = r.json()
    _report("重启后RULE_A仍为手工规则A", rule_a.get("name") == "手工规则A", rule_a.get("name"))

    r = requests.get(
        f"{BASE_URL}/api/admin/source-rules/RULE_D",
    )
    rule_d = r.json()
    _report("重启后RULE_D存在", rule_d.get("name") == "第二次导入新建D", rule_d.get("name"))


def test_13_export_json():
    print("\n=== 测试13: JSON导出核对 ===")
    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs/{JOB_ID_2}/export/json",
        params={"export_type": "full"},
        headers={"X-User-Id": "auditor"},
    )
    _report("JSON导出请求成功", r.status_code == 200, r.text[:200])
    data = r.json()
    _report("JSON含job_id", data.get("job_id") == JOB_ID_2, str(data.get("job_id")))
    _report("JSON含details", "details" in data, str(data.keys()))
    _report("JSON含snapshots", "snapshots" in data, str(data.keys()))

    details = data.get("details", [])
    _report("JSON details有2条", len(details) == 2, f"实际{len(details)}条")

    overwrite_detail = next((d for d in details if d["rule_code"] == "RULE_A"), None)
    if overwrite_detail:
        _report("JSON含覆盖明细before", overwrite_detail.get("before") is not None, "before缺失")
        _report("JSON含覆盖明细after", overwrite_detail.get("after") is not None, "after缺失")
        _report("JSON含覆盖明细diff", overwrite_detail.get("diff") is not None, "diff缺失")
        before_name = overwrite_detail.get("before", {}).get("name")
        after_name = overwrite_detail.get("after", {}).get("name")
        _report("覆盖before=人工修改后的规则A", before_name == "人工修改后的规则A", str(before_name))
        _report("覆盖after=第二次导入覆盖A", after_name == "第二次导入覆盖A", str(after_name))
    else:
        _report("JSON含覆盖明细", False, "RULE_A覆盖明细不存在")


def test_14_export_csv():
    print("\n=== 测试14: CSV导出核对 ===")
    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs/{JOB_ID_2}/export/csv",
        params={"export_type": "details"},
        headers={"X-User-Id": "auditor"},
    )
    _report("CSV导出请求成功", r.status_code == 200)
    csv_text = r.text
    lines = csv_text.strip().split("\n")
    _report("CSV有表头+数据行", len(lines) >= 2, f"行数={len(lines)}")
    _report("CSV表头含rule_code", "rule_code" in lines[0], lines[0][:100])

    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs/{JOB_ID_2}/export/csv",
        params={"export_type": "diff"},
        headers={"X-User-Id": "auditor"},
    )
    _report("差异CSV导出成功", r.status_code == 200)
    diff_lines = r.text.strip().split("\n")
    _report("差异CSV有内容", len(diff_lines) >= 2, f"行数={len(diff_lines)}")
    _report("差异CSV表头含field", "field" in diff_lines[0], diff_lines[0][:100])


def test_15_sensitivity_masking():
    print("\n=== 测试15: 敏感数据脱敏 ===")
    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs/{JOB_ID_2}/details",
        headers={"X-User-Id": "nobody"},
    )
    data = r.json()
    _report("无权限用户查明细-脱敏", data.get("has_audit_permission") is False)

    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/audit-log",
        params={"rule_code": "RULE_A"},
        headers={"X-User-Id": "nobody"},
    )
    data = r.json()
    _report("无权限用户审计日志-脱敏", data.get("has_audit_permission") is False)

    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs/{JOB_ID_2}/export/json",
        headers={"X-User-Id": "nobody"},
    )
    _report("无权限用户导出JSON-被拦截", r.status_code == 403, f"status={r.status_code}")

    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs/{JOB_ID_2}/export/csv",
        headers={"X-User-Id": "nobody"},
    )
    _report("无权限用户导出CSV-被拦截", r.status_code == 403, f"status={r.status_code}")


def test_16_conflict_hash_verification():
    print("\n=== 测试16: 冲突记录可验证（content_hash）===")
    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs/{JOB_ID_2}/conflicts",
        headers={"X-User-Id": "auditor"},
    )
    data = r.json()
    _report("冲突记录查询成功", data.get("success") is True)
    conflicts = data.get("conflicts", [])
    if conflicts:
        for c in conflicts:
            _report(
                f"冲突记录有content_hash code={c.get('rule_code')}",
                c.get("content_hash") is not None,
                str(c.get("content_hash")),
            )
    else:
        _report("冲突记录无冲突(正常-第二次导入无并发)", True)


def test_17_default_admin_permission():
    print("\n=== 测试17: 默认管理员权限 ===")
    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/jobs",
        headers={"X-User-Id": "admin"},
    )
    data = r.json()
    _report("admin用户有审计权限", data.get("has_audit_permission") is True, str(data.get("has_audit_permission")))

    r = requests.post(
        f"{BASE_URL}/api/admin/source-rules/import",
        json={"rules": [{"code": "ADMIN_TEST", "name": "管理员导入测试"}], "conflict_strategy": "skip"},
        headers={"X-Operator": "admin", "X-User-Id": "admin"},
    )
    _report("admin用户可执行导入", r.status_code == 200, f"status={r.status_code}")


def test_18_second_import_revoke():
    print("\n=== 测试18: 撤销第二次导入 ===")
    r = requests.post(
        f"{BASE_URL}/api/admin/import-replay/jobs/{JOB_ID_2}/revoke",
        json={"reason": "撤销第二次导入"},
        headers={"X-Operator": "revoker", "X-User-Id": "revoker"},
    )
    _report("撤销第二次导入成功", r.status_code == 200, r.text[:200])
    data = r.json()
    _report("撤销结果success", data.get("success") is True)

    r = requests.get(
        f"{BASE_URL}/api/admin/source-rules/RULE_A",
    )
    rule_a = r.json()
    _report("RULE_A恢复为人工修改后", rule_a.get("name") == "人工修改后的规则A", rule_a.get("name"))

    r = requests.get(
        f"{BASE_URL}/api/admin/source-rules/RULE_D",
    )
    _report("RULE_D已被删除", r.status_code == 404, f"status={r.status_code}")

    r = requests.get(
        f"{BASE_URL}/api/admin/import-replay/lineage",
        params={"rule_code": "RULE_A", "source_type": "revoke_restore"},
        headers={"X-User-Id": "auditor"},
    )
    data = r.json()
    revoke_restores = [e for e in data.get("lineage", []) if e["source_type"] == "revoke_restore"]
    _report("RULE_A有2条revoke_restore(两次撤销)", len(revoke_restores) >= 2, f"实际{len(revoke_restores)}条")


def test_19_dry_run():
    print("\n=== 测试19: dry-run预检 ===")
    import_payload = {
        "rules": [
            {"code": "RULE_A", "name": "dry-run测试", "category": "general", "priority": 5},
            {"code": "RULE_E", "name": "dry-run新建", "category": "general", "priority": 5},
        ],
        "conflict_strategy": "overwrite",
        "dry_run": True,
    }
    r = requests.post(
        f"{BASE_URL}/api/admin/source-rules/import",
        json=import_payload,
        headers={"X-Operator": "importer", "X-User-Id": "importer"},
    )
    _report("dry-run请求成功", r.status_code == 200)
    data = r.json()
    _report("dry_run标记为true", data.get("dry_run") is True, str(data.get("dry_run")))

    r = requests.get(
        f"{BASE_URL}/api/admin/source-rules/RULE_A",
    )
    rule_a = r.json()
    _report("dry-run不实际修改", rule_a.get("name") == "人工修改后的规则A", rule_a.get("name"))


# ============================================================
# 主入口
# ============================================================

def main():
    global JOB_ID, JOB_ID_2
    JOB_ID = None
    JOB_ID_2 = None

    port = 18765
    print(f"导入作业审计模块 E2E 测试")
    print(f"=" * 60)

    try:
        start_server(port)

        test_01_grant_permission()
        test_02_permission_enforcement()
        test_03_manual_create_rule()
        test_04_first_import()
        test_05_manual_update_after_import()
        test_06_re_import_after_manual()
        test_07_lineage_tracking()
        test_08_import_job_details()
        test_09_structured_audit_log()
        test_10_revoke_import()
        test_11_replay_after_revoke()
        test_12_restart_persistence()
        test_13_export_json()
        test_14_export_csv()
        test_15_sensitivity_masking()
        test_16_conflict_hash_verification()
        test_17_default_admin_permission()
        test_18_second_import_revoke()
        test_19_dry_run()

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
