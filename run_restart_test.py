"""Restart persistence test - verify data survives server restart"""
import requests, sys

BASE = "http://127.0.0.1:18765"
P = 0; F = 0; ERR = []

def chk(n, ok, d=""):
    global P, F
    if ok:
        P += 1
        print(f"[PASS] {n}")
    else:
        F += 1
        ERR.append(f"{n}: {d}")
        print(f"[FAIL] {n} -- {d}")

print("=== Restart persistence test ===")
r = requests.get(f"{BASE}/api/health")
chk("health OK", r.status_code == 200)

r = requests.get(f"{BASE}/api/admin/import-replay/jobs", headers={"X-User-Id": "auditor"})
chk("jobs list OK", r.status_code == 200)
d = r.json()
chk("jobs has audit perm", d.get("has_audit_permission") is True)
total = d.get("total", 0)
chk("jobs total >= 2", total >= 2, f"total={total}")

r = requests.get(f"{BASE}/api/admin/import-replay/lineage", params={"rule_code": "RULE_A"}, headers={"X-User-Id": "auditor"})
chk("lineage OK", r.status_code == 200)
lin = r.json().get("lineage", [])
stypes = [e["source_type"] for e in lin]
chk("lineage has revoke_restore after restart", "revoke_restore" in stypes, str(stypes))
chk("lineage has manual_create", "manual_create" in stypes)
chk("lineage has manual_update", "manual_update" in stypes)

r = requests.get(f"{BASE}/api/admin/source-rules/RULE_A")
chk("RULE_A persists", r.json().get("name") == "人工修改后的规则A", r.json().get("name"))
chk("RULE_A origin=manual", r.json().get("import_origin") == "manual")

r = requests.get(f"{BASE}/api/admin/source-rules/RULE_D")
chk("RULE_D deleted persists", r.status_code == 404, f"status={r.status_code}")

r = requests.get(f"{BASE}/api/admin/source-rules/RULE_B")
chk("RULE_B persists", r.status_code == 200)

r = requests.get(f"{BASE}/api/admin/import-replay/audit-log", params={"rule_code": "RULE_A"}, headers={"X-User-Id": "auditor"})
chk("audit log persists", r.status_code == 200)
ops = [l["operation"] for l in r.json().get("audit_logs", [])]
chk("audit log has create", "create" in ops, str(ops))

# Test export after restart
r = requests.get(f"{BASE}/api/admin/source-rules/export/json", headers={"X-User-Id": "auditor"})
chk("export JSON after restart OK", r.status_code == 200)

r = requests.get(f"{BASE}/api/admin/source-rules/export/csv", headers={"X-User-Id": "auditor"})
chk("export CSV after restart OK", r.status_code == 200)

print(f"\nRestart test: PASS={P} FAIL={F}")
if ERR:
    for e in ERR:
        print(f"  - {e}")
sys.exit(1 if F > 0 else 0)
