"""E2E test runner - talks to the already-running server at 127.0.0.1:18765"""
import requests, json, sys

BASE = "http://127.0.0.1:18765"
PASS = 0
FAIL = 0
ERR = []

def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        ERR.append(f"{name}: {detail}")
        print(f"  [FAIL] {name} -- {detail}")

# T1: Grant permissions
print("=== T1: Grant permissions ===")
for uid, ptype in [("auditor","import_audit_view"),("auditor","import_audit_export"),("revoker","import_revoke"),("revoker","import_audit_view"),("importer","import_manage")]:
    r = requests.post(f"{BASE}/api/admin/import-replay/permissions/grant", json={"target_user_id":uid,"permission_type":ptype}, headers={"X-Operator":"admin"})
    check(f"grant {uid}/{ptype}", r.status_code==200, r.text[:100])

# T2: Permission enforcement
print("=== T2: Permission enforcement ===")
r = requests.get(f"{BASE}/api/admin/import-replay/jobs", headers={"X-User-Id":"nobody"})
check("nobody list jobs - no perm", r.json().get("has_audit_permission") is False)
r = requests.post(f"{BASE}/api/admin/source-rules/import", json={"rules":[{"code":"X","name":"X"}],"conflict_strategy":"skip"}, headers={"X-User-Id":"nobody"})
check("nobody import - 403", r.status_code==403, f"status={r.status_code}")
r = requests.get(f"{BASE}/api/admin/source-rules/export/json", headers={"X-User-Id":"nobody"})
check("nobody export json - 403", r.status_code==403)
r = requests.get(f"{BASE}/api/admin/source-rules/export/csv", headers={"X-User-Id":"nobody"})
check("nobody export csv - 403", r.status_code==403)
r = requests.get(f"{BASE}/api/admin/import-replay/lineage", headers={"X-User-Id":"nobody"})
check("nobody lineage - 403", r.status_code==403)

# T3: Manual create rules
print("=== T3: Manual create rules ===")
r = requests.post(f"{BASE}/api/admin/source-rules", json={"code":"RULE_A","name":"手工规则A","description":"手工创建","category":"general","priority":10,"is_enabled":True,"match_pattern":"rule_a_*"}, headers={"X-Operator":"admin"})
check("create RULE_A", r.status_code==200, r.text[:200])
check("RULE_A import_origin=manual", r.json().get("import_origin")=="manual")
r = requests.post(f"{BASE}/api/admin/source-rules", json={"code":"RULE_B","name":"手工规则B","category":"system","priority":20,"is_enabled":True}, headers={"X-Operator":"admin"})
check("create RULE_B", r.status_code==200)

# T4: First import
print("=== T4: First import ===")
r = requests.post(f"{BASE}/api/admin/source-rules/import", json={"rules":[{"code":"RULE_A","name":"导入覆盖规则A","description":"导入覆盖","category":"import","priority":50,"is_enabled":True,"match_pattern":"import_a_*"},{"code":"RULE_C","name":"导入新建规则C","description":"导入新建","category":"import","priority":60,"is_enabled":True}],"conflict_strategy":"overwrite","dry_run":False,"check_concurrent_modifications":True}, headers={"X-Operator":"importer","X-User-Id":"importer"})
check("import request OK", r.status_code==200, r.text[:300])
d = r.json()
check("import success", d.get("success") is True)
check("created 1", d.get("summary",{}).get("created")==1, str(d.get("summary")))
check("overwritten 1", d.get("summary",{}).get("overwritten")==1)
JOB_ID = d.get("job_id")
check("has job_id", JOB_ID is not None)
r = requests.get(f"{BASE}/api/admin/source-rules/RULE_A")
check("RULE_A overwritten", r.json().get("name")=="导入覆盖规则A", r.json().get("name"))
check("RULE_A origin=import", r.json().get("import_origin")=="import")

# T5: Manual update after import
print("=== T5: Manual update after import ===")
r = requests.patch(f"{BASE}/api/admin/source-rules/RULE_A", json={"name":"人工修改后的规则A","description":"人工修改描述","priority":99}, headers={"X-Operator":"admin"})
check("manual update RULE_A", r.status_code==200)
check("RULE_A name changed", r.json().get("name")=="人工修改后的规则A")
check("RULE_A origin=manual", r.json().get("import_origin")=="manual")

# T6: Re-import after manual
print("=== T6: Re-import ===")
r = requests.post(f"{BASE}/api/admin/source-rules/import", json={"rules":[{"code":"RULE_A","name":"第二次导入覆盖A","description":"第二次导入","category":"import","priority":70,"is_enabled":True},{"code":"RULE_D","name":"第二次导入新建D","description":"新建D","category":"general","priority":30,"is_enabled":True}],"conflict_strategy":"overwrite","dry_run":False}, headers={"X-Operator":"importer","X-User-Id":"importer"})
check("re-import OK", r.status_code==200, r.text[:300])
d = r.json()
JOB_ID_2 = d.get("job_id")
check("re-import overwritten 1", d.get("summary",{}).get("overwritten")==1)
check("re-import created 1", d.get("summary",{}).get("created")==1)
r = requests.get(f"{BASE}/api/admin/source-rules/RULE_A")
check("RULE_A re-overwritten", r.json().get("name")=="第二次导入覆盖A")
check("RULE_A origin=import again", r.json().get("import_origin")=="import")

# T7: Lineage tracking
print("=== T7: Lineage ===")
r = requests.get(f"{BASE}/api/admin/import-replay/lineage", params={"rule_code":"RULE_A"}, headers={"X-User-Id":"auditor"})
check("lineage query OK", r.status_code==200)
d = r.json()
check("lineage has audit perm", d.get("has_audit_permission") is True)
lin = d.get("lineage",[])
stypes = [e["source_type"] for e in lin]
check("lineage has manual_create", "manual_create" in stypes, str(stypes))
check("lineage has import_overwrite", "import_overwrite" in stypes, str(stypes))
check("lineage has manual_update", "manual_update" in stypes, str(stypes))
check("lineage >= 4 entries", len(lin)>=4, f"actual={len(lin)}")
for e in lin:
    h = e.get("content_hash")
    check(f"lineage entry has content_hash ({e['source_type']})", h is not None and len(h)==16, str(h))

# T8: Job details
print("=== T8: Job details ===")
r = requests.get(f"{BASE}/api/admin/import-replay/jobs/{JOB_ID}", headers={"X-User-Id":"auditor"})
check("job detail has perm", r.json().get("has_audit_permission") is True)
r = requests.get(f"{BASE}/api/admin/import-replay/jobs/{JOB_ID}/details", headers={"X-User-Id":"auditor"})
check("job details OK", r.json().get("success") is True)
check("job details 2", len(r.json().get("details",[]))==2, f"actual={len(r.json().get('details',[]))}")
r = requests.get(f"{BASE}/api/admin/import-replay/jobs/{JOB_ID}/snapshots", params={"snapshot_type":"before_import"}, headers={"X-User-Id":"auditor"})
snaps = r.json().get("snapshots",[])
check("snapshots >= 1", len(snaps)>=1, f"actual={len(snaps)}")
sa = next((s for s in snaps if s["rule_code"]=="RULE_A"), None)
check("RULE_A snapshot name=手工规则A", sa is not None and sa["rule_json"]["name"]=="手工规则A", str(sa["rule_json"]["name"] if sa else None))

# T9: Structured audit log
print("=== T9: Audit log ===")
r = requests.get(f"{BASE}/api/admin/import-replay/audit-log", params={"rule_code":"RULE_A"}, headers={"X-User-Id":"auditor"})
d = r.json()
check("audit log OK", d.get("success") is True)
ops = [l["operation"] for l in d.get("audit_logs",[])]
check("audit has create", "create" in ops, str(ops))
check("audit has import_overwrite", "import_overwrite" in ops, str(ops))
check("audit has update", "update" in ops, str(ops))

# T10: Revoke
print("=== T10: Revoke ===")
r = requests.post(f"{BASE}/api/admin/import-replay/jobs/{JOB_ID}/revoke", json={"reason":"测试撤销"}, headers={"X-Operator":"revoker","X-User-Id":"revoker"})
check("revoke OK", r.status_code==200, r.text[:200])
d = r.json()
check("revoke success", d.get("success") is True)
r = requests.get(f"{BASE}/api/admin/source-rules/RULE_A")
check("RULE_A restored to 手工规则A", r.json().get("name")=="手工规则A", r.json().get("name"))
check("RULE_A origin=manual after revoke", r.json().get("import_origin")=="manual")
r = requests.get(f"{BASE}/api/admin/source-rules/RULE_C")
check("RULE_C deleted after revoke", r.status_code==404, f"status={r.status_code}")

# T11: Replay
print("=== T11: Replay ===")
r = requests.get(f"{BASE}/api/admin/import-replay/jobs/{JOB_ID}/replay", headers={"X-User-Id":"revoker"})
d = r.json()
check("replay OK", d.get("success") is True)
check("replay is_revoked=1", d.get("is_revoked")==1)
check("replay verification passed", d.get("revoke_verification_passed") is True, str(d.get("revoke_verification_passed")))
for s in d.get("replay_data",[]):
    check(f"replay step {s['rule_code']} matched", s["verify_result"]=="matched", s["verify_result"])

# T12: Default admin
print("=== T12: Default admin ===")
r = requests.get(f"{BASE}/api/admin/import-replay/jobs", headers={"X-User-Id":"admin"})
check("admin has audit perm", r.json().get("has_audit_permission") is True)

# T13: Export JSON
print("=== T13: Export JSON ===")
r = requests.get(f"{BASE}/api/admin/import-replay/jobs/{JOB_ID_2}/export/json", params={"export_type":"full"}, headers={"X-User-Id":"auditor"})
check("JSON export OK", r.status_code==200, r.text[:200])
d = r.json()
check("JSON has job_id", d.get("job_id")==JOB_ID_2)
check("JSON has details", "details" in d)
check("JSON has snapshots", "snapshots" in d)
details = d.get("details",[])
check("JSON details 2", len(details)==2, f"actual={len(details)}")
ov = next((x for x in details if x["rule_code"]=="RULE_A"), None)
if ov:
    check("JSON overwrite has before", ov.get("before") is not None)
    check("JSON overwrite has after", ov.get("after") is not None)
    check("JSON overwrite has diff", ov.get("diff") is not None)
    check("JSON before=人工修改后的规则A", ov.get("before",{}).get("name")=="人工修改后的规则A")
    check("JSON after=第二次导入覆盖A", ov.get("after",{}).get("name")=="第二次导入覆盖A")
else:
    check("JSON overwrite detail exists", False, "RULE_A detail not found")

# T14: Export CSV
print("=== T14: Export CSV ===")
r = requests.get(f"{BASE}/api/admin/import-replay/jobs/{JOB_ID_2}/export/csv", params={"export_type":"details"}, headers={"X-User-Id":"auditor"})
check("CSV details export OK", r.status_code==200)
lines = r.text.strip().split("\n")
check("CSV has header+data", len(lines)>=2, f"lines={len(lines)}")
check("CSV header has rule_code", "rule_code" in lines[0])
r = requests.get(f"{BASE}/api/admin/import-replay/jobs/{JOB_ID_2}/export/csv", params={"export_type":"diff"}, headers={"X-User-Id":"auditor"})
check("CSV diff export OK", r.status_code==200)
diff_lines = r.text.strip().split("\n")
check("CSV diff has content", len(diff_lines)>=2, f"lines={len(diff_lines)}")
check("CSV diff header has field", "field" in diff_lines[0])

# T15: Revoke second import
print("=== T15: Revoke second import ===")
r = requests.post(f"{BASE}/api/admin/import-replay/jobs/{JOB_ID_2}/revoke", json={"reason":"撤销第二次导入"}, headers={"X-Operator":"revoker","X-User-Id":"revoker"})
check("revoke 2 OK", r.status_code==200)
r = requests.get(f"{BASE}/api/admin/source-rules/RULE_A")
check("RULE_A back to 人工修改后", r.json().get("name")=="人工修改后的规则A")
r = requests.get(f"{BASE}/api/admin/source-rules/RULE_D")
check("RULE_D deleted", r.status_code==404, f"status={r.status_code}")
r = requests.get(f"{BASE}/api/admin/import-replay/lineage", params={"rule_code":"RULE_A","source_type":"revoke_restore"}, headers={"X-User-Id":"auditor"})
rr = [e for e in r.json().get("lineage",[]) if e["source_type"]=="revoke_restore"]
check("RULE_A has 2 revoke_restore entries", len(rr)>=2, f"actual={len(rr)}")

# T16: Dry-run
print("=== T16: Dry-run ===")
r = requests.post(f"{BASE}/api/admin/source-rules/import", json={"rules":[{"code":"RULE_A","name":"dry-run测试","category":"general","priority":5},{"code":"RULE_E","name":"dry-run新建","category":"general","priority":5}],"conflict_strategy":"overwrite","dry_run":True}, headers={"X-Operator":"importer","X-User-Id":"importer"})
check("dry-run OK", r.status_code==200)
check("dry_run flag true", r.json().get("dry_run") is True)
r = requests.get(f"{BASE}/api/admin/source-rules/RULE_A")
check("dry-run no side effect", r.json().get("name")=="人工修改后的规则A")

# T17: Conflicts content hash
print("=== T17: Conflict hash ===")
r = requests.get(f"{BASE}/api/admin/import-replay/jobs/{JOB_ID_2}/conflicts", headers={"X-User-Id":"auditor"})
d = r.json()
check("conflicts query OK", d.get("success") is True)
for c in d.get("conflicts",[]):
    check(f"conflict has content_hash ({c.get('rule_code')})", c.get("content_hash") is not None, str(c.get("content_hash")))

# Summary
print(f"\n{'='*60}")
print(f"RESULTS: PASS={PASS}  FAIL={FAIL}")
if ERR:
    print("Failures:")
    for e in ERR:
        print(f"  - {e}")
print(f"{'='*60}")
sys.exit(1 if FAIL > 0 else 0)
