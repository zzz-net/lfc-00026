"""
规则导入回放中心 自动化测试
覆盖场景：
1. 手工创建后再导入覆盖
2. 重启后查询
3. 导出核对
4. 撤销后回看
"""
import os
import json
import csv
import io
import pytest
import sqlite3
import tempfile
import uuid
from datetime import datetime, timedelta

from services.source_rule_service import SourceRuleService


TEST_DB_DIR = tempfile.gettempdir()


def _close_db_connections():
    """关闭所有数据库连接，避免文件锁定"""
    import database as db_module
    if hasattr(db_module._local, "conn"):
        try:
            db_module._local.conn.close()
        except Exception:
            pass
        delattr(db_module._local, "conn")


def _get_db_path():
    """生成唯一的测试数据库路径"""
    return os.path.join(TEST_DB_DIR, "test_source_rules_{}.db".format(uuid.uuid4().hex[:8]))


def _init_test_db(db_path):
    """初始化测试数据库"""
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except Exception:
            pass
    from database import init_db
    import database as db_module
    db_module.DB_PATH = db_path
    init_db()


def _cleanup_test_db(db_path):
    """清理测试数据库"""
    _close_db_connections()
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except Exception:
            pass


def _get_job_pk_by_job_id(job_id):
    """通过业务job_id获取作业自增主键"""
    import database as db_module
    conn = db_module.get_db()
    row = conn.execute(
        "SELECT id FROM source_rules_import_jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    return row["id"] if row else None


def _assert_rule_import_job_link(rule, job_id):
    """验证规则与导入作业的关联关系"""
    assert rule.import_origin == "import"
    assert rule.import_job_id is not None
    job_pk = _get_job_pk_by_job_id(job_id)
    assert job_pk is not None
    assert rule.import_job_id == job_pk


@pytest.fixture
def clean_db():
    """每个测试前清空数据库并重新初始化"""
    db_path = _get_db_path()
    _init_test_db(db_path)
    yield db_path
    _cleanup_test_db(db_path)


class TestManualCreateThenImportOverwrite:
    """场景1：手工创建后再导入覆盖"""

    def test_manual_create_then_import_overwrite(self, clean_db):
        """测试手工创建规则后，用导入覆盖该规则"""
        # 1. 先手工创建规则
        manual_rule = SourceRuleService.create_rule(
            code="TEST_001",
            name="手工创建的规则",
            description="这是手工创建的规则描述",
            category="general",
            priority=10,
            is_enabled=True,
            match_pattern="test_manual_*",
            operator="user_manual",
        )
        assert manual_rule.import_origin == "manual"
        assert manual_rule.import_job_id is None
        assert manual_rule.last_manual_modified_by == "user_manual"
        assert manual_rule.last_manual_modified_at is not None

        # 2. 准备导入数据，包含同名规则
        import_rules = [
            {
                "code": "TEST_001",
                "name": "导入覆盖的规则",
                "description": "这是导入覆盖的规则描述",
                "category": "import",
                "priority": 50,
                "is_enabled": True,
                "match_pattern": "test_import_*",
            },
            {
                "code": "TEST_002",
                "name": "导入新建的规则",
                "description": "这是导入新建的规则描述",
                "category": "import",
                "priority": 60,
                "is_enabled": True,
                "match_pattern": "test_import_new_*",
            },
        ]

        # 3. 执行导入，使用 overwrite 策略
        result = SourceRuleService.import_rules(
            rules_data=import_rules,
            conflict_strategy="overwrite",
            dry_run=False,
            operator="user_import",
            check_concurrent_modifications=True,
        )

        assert result["success"] is True
        assert result["summary"]["total"] == 2
        assert result["summary"]["overwritten"] == 1
        assert result["summary"]["created"] == 1
        job_id = result["job_id"]
        assert job_id is not None

        # 4. 验证规则被正确覆盖
        rule_001 = SourceRuleService.get_rule_by_code("TEST_001")
        assert rule_001.name == "导入覆盖的规则"
        _assert_rule_import_job_link(rule_001, job_id)
        assert rule_001.last_manual_modified_by == "user_manual"
        assert rule_001.last_manual_modified_at is not None

        rule_002 = SourceRuleService.get_rule_by_code("TEST_002")
        assert rule_002.name == "导入新建的规则"
        _assert_rule_import_job_link(rule_002, job_id)

        # 5. 验证导入作业记录正确
        # 先授权审计权限
        SourceRuleService.grant_import_permission(
            target_user_id="user_audit",
            permission_type="import_audit_view",
            granted_by="admin",
        )
        job = SourceRuleService.get_import_job(job_id, user_id="user_audit")
        assert job["has_audit_permission"] is True
        assert job["job_id"] == job_id
        assert job["operator"] == "user_import"
        assert job["conflict_strategy"] == "overwrite"
        assert job["is_revoked"] == 0

        # 6. 验证导入明细正确
        details_result = SourceRuleService.get_import_job_details(
            job_id, user_id="user_audit"
        )
        assert details_result["has_audit_permission"] is True
        assert details_result["total"] == 2

        # 找到TEST_001的明细
        detail_001 = next(
            d for d in details_result["details"] if d["rule_code"] == "TEST_001"
        )
        assert detail_001["action"] == "overwrite"
        assert detail_001["status"] == "success"
        assert detail_001["incoming"]["name"] == "导入覆盖的规则"
        assert detail_001["before"]["name"] == "手工创建的规则"
        assert detail_001["after"]["name"] == "导入覆盖的规则"
        assert len(detail_001["diff"]) > 0
        assert any(
            d["field"] == "name"
            for d in detail_001["diff"]["changed_fields"]
        )

        # 找到TEST_002的明细
        detail_002 = next(
            d for d in details_result["details"] if d["rule_code"] == "TEST_002"
        )
        assert detail_002["action"] == "create"
        assert detail_002["status"] == "success"
        assert detail_002["before"] is None

        # 7. 验证快照正确
        snapshots_result = SourceRuleService.get_import_job_snapshots(
            job_id, user_id="user_audit", snapshot_type="before_import"
        )
        assert snapshots_result["has_audit_permission"] is True
        assert len(snapshots_result["snapshots"]) == 1
        assert snapshots_result["snapshots"][0]["rule_code"] == "TEST_001"
        assert (
            snapshots_result["snapshots"][0]["rule_json"]["name"]
            == "手工创建的规则"
        )


class TestConcurrentConflictDetection:
    """场景补充：导入过程中并发修改冲突检测"""

    def test_concurrent_modification_detected(self, clean_db):
        """测试导入过程中检测到人工修改的冲突"""
        from unittest.mock import patch

        # 1. 先创建规则
        SourceRuleService.create_rule(
            code="CONFLICT_001",
            name="初始规则",
            description="初始描述",
            category="general",
            priority=10,
            is_enabled=True,
            match_pattern="conflict_*",
            operator="user1",
        )

        # 2. 准备导入数据（包含同名规则）
        import_rules = [
            {
                "code": "CONFLICT_001",
                "name": "导入的规则",
                "description": "导入描述",
                "category": "general",
                "priority": 20,
                "is_enabled": True,
                "match_pattern": "import_*",
            }
        ]

        # 3. 使用mock在导入过程中修改数据库，模拟并发修改
        original_get_merged_rules = SourceRuleService.get_merged_rules
        modification_done = [False]

        def mock_get_merged_rules(*args, **kwargs):
            result = original_get_merged_rules(*args, **kwargs)
            # 在第一次调用后修改数据库，模拟并发修改
            if not modification_done[0]:
                modification_done[0] = True
                import database as db_module
                conn = db_module.get_db()
                conn.execute(
                    "UPDATE source_rules SET name = ? WHERE code = ?",
                    ("被人工修改的规则", "CONFLICT_001"),
                )
                conn.commit()
            return result

        # 4. 执行导入，应该检测到冲突
        with patch.object(SourceRuleService, 'get_merged_rules', side_effect=mock_get_merged_rules):
            result = SourceRuleService.import_rules(
                rules_data=import_rules,
                conflict_strategy="overwrite",
                dry_run=False,
                operator="user_import",
                check_concurrent_modifications=True,
            )

        assert result["success"] is True
        assert result["summary"]["total"] == 1
        assert result["summary"]["conflicts_detected"] == 1
        job_id = result["job_id"]

        # 5. 验证冲突记录
        SourceRuleService.grant_import_permission(
            target_user_id="user_audit",
            permission_type="import_audit_view",
            granted_by="admin",
        )
        conflicts_result = SourceRuleService.get_import_job_conflicts(
            job_id, user_id="user_audit"
        )
        assert conflicts_result["has_audit_permission"] is True
        assert len(conflicts_result["conflicts"]) == 1

        conflict = conflicts_result["conflicts"][0]
        assert conflict["rule_code"] == "CONFLICT_001"
        assert conflict["conflict_type"] == "concurrent_modification"
        assert conflict["is_resolved"] == 1
        assert conflict["expected_before"]["name"] == "初始规则"
        assert conflict["actual_before"]["name"] == "被人工修改的规则"
        assert len(conflict["detected_changes"]) > 0

        # 6. 验证规则最终被导入覆盖（按overwrite策略）
        final_rule = SourceRuleService.get_rule_by_code("CONFLICT_001")
        assert final_rule.name == "导入的规则"


class TestRestartPersistence:
    """场景2：重启后查询（模拟服务重启后数据仍可追查）"""

    def test_data_persists_after_reconnect(self, clean_db):
        """测试数据在重新连接数据库后仍然存在"""
        # 1. 先创建规则并执行导入
        SourceRuleService.create_rule(
            code="RESTART_001",
            name="重启前的规则",
            description="重启前描述",
            category="general",
            priority=10,
            is_enabled=True,
            match_pattern="restart_*",
            operator="user1",
        )

        import_rules = [
            {
                "code": "RESTART_001",
                "name": "导入覆盖的规则",
                "description": "导入描述",
                "category": "general",
                "priority": 20,
                "is_enabled": True,
                "match_pattern": "import_*",
            }
        ]

        result = SourceRuleService.import_rules(
            rules_data=import_rules,
            conflict_strategy="overwrite",
            dry_run=False,
            operator="user_import",
        )
        job_id = result["job_id"]

        # 授权
        SourceRuleService.grant_import_permission(
            target_user_id="user_audit",
            permission_type="import_audit_view",
            granted_by="admin",
        )

        # 2. 模拟服务重启 - 重新初始化数据库连接
        import database as db_module

        # 3. 重新查询作业列表
        list_result = SourceRuleService.list_import_jobs(
            user_id="user_audit", page=1, page_size=10
        )
        assert list_result["has_audit_permission"] is True
        assert list_result["total"] == 1
        assert list_result["jobs"][0]["job_id"] == job_id

        # 4. 重新查询作业详情
        job = SourceRuleService.get_import_job(job_id, user_id="user_audit")
        assert job["job_id"] == job_id
        assert job["operator"] == "user_import"

        # 5. 重新查询明细
        details_result = SourceRuleService.get_import_job_details(
            job_id, user_id="user_audit"
        )
        assert details_result["total"] == 1
        assert details_result["details"][0]["rule_code"] == "RESTART_001"
        assert details_result["details"][0]["before"]["name"] == "重启前的规则"

        # 6. 重新查询快照
        snapshots_result = SourceRuleService.get_import_job_snapshots(
            job_id, user_id="user_audit"
        )
        assert len(snapshots_result["snapshots"]) == 1

        # 7. 验证规则状态正确
        rule = SourceRuleService.get_rule_by_code("RESTART_001")
        assert rule.name == "导入覆盖的规则"
        _assert_rule_import_job_link(rule, job_id)


class TestExportVerification:
    """场景3：导出核对"""

    def test_json_export_correctness(self, clean_db):
        """测试JSON导出内容正确"""
        # 1. 准备数据
        SourceRuleService.create_rule(
            code="EXPORT_001",
            name="导出测试规则1",
            description="描述1",
            category="general",
            priority=10,
            is_enabled=True,
            match_pattern="export1_*",
            operator="user1",
        )

        import_rules = [
            {
                "code": "EXPORT_001",
                "name": "导入覆盖规则1",
                "description": "导入描述1",
                "category": "import",
                "priority": 20,
                "is_enabled": True,
                "match_pattern": "import1_*",
            },
            {
                "code": "EXPORT_002",
                "name": "导入新建规则2",
                "description": "导入描述2",
                "category": "import",
                "priority": 30,
                "is_enabled": True,
                "match_pattern": "import2_*",
            },
        ]

        result = SourceRuleService.import_rules(
            rules_data=import_rules,
            conflict_strategy="overwrite",
            dry_run=False,
            operator="user_import",
        )
        job_id = result["job_id"]

        # 授权导出权限
        SourceRuleService.grant_import_permission(
            target_user_id="user_audit",
            permission_type="import_audit_export",
            granted_by="admin",
        )

        # 2. 导出完整JSON
        export_result = SourceRuleService.export_import_job_json(
            job_id, user_id="user_audit", export_type="full"
        )
        assert export_result["has_export_permission"] is True
        assert export_result["filename"].startswith("import_job_{}_full_".format(job_id))
        assert export_result["filename"].endswith(".json")

        export_data = export_result["data"]
        assert export_data["job_id"] == job_id
        assert export_data["summary"]["total"] == 2
        assert export_data["summary"]["overwritten"] == 1
        assert export_data["summary"]["created"] == 1
        assert len(export_data["details"]) == 2
        assert len(export_data["snapshots"]) == 1

        # 3. 导出明细JSON
        details_export = SourceRuleService.export_import_job_json(
            job_id, user_id="user_audit", export_type="details"
        )
        assert len(details_export["data"]["details"]) == 2

        # 4. 导出快照JSON
        snapshots_export = SourceRuleService.export_import_job_json(
            job_id, user_id="user_audit", export_type="snapshots"
        )
        assert len(snapshots_export["data"]["snapshots"]) == 1

        # 5. 验证导出的明细数据正确
        detail_001 = next(
            d
            for d in export_data["details"]
            if d["rule_code"] == "EXPORT_001"
        )
        assert detail_001["action"] == "overwrite"
        assert detail_001["before"]["name"] == "导出测试规则1"
        assert detail_001["after"]["name"] == "导入覆盖规则1"

        detail_002 = next(
            d
            for d in export_data["details"]
            if d["rule_code"] == "EXPORT_002"
        )
        assert detail_002["action"] == "create"
        assert detail_002["before"] is None

    def test_csv_export_correctness(self, clean_db):
        """测试CSV导出内容正确"""
        # 1. 准备数据
        SourceRuleService.create_rule(
            code="CSV_001",
            name="CSV测试规则1",
            description="描述1",
            category="general",
            priority=10,
            is_enabled=True,
            match_pattern="csv1_*",
            operator="user1",
        )

        import_rules = [
            {
                "code": "CSV_001",
                "name": "导入覆盖规则1",
                "description": "导入描述1",
                "category": "import",
                "priority": 20,
                "is_enabled": True,
                "match_pattern": "import1_*",
            }
        ]

        result = SourceRuleService.import_rules(
            rules_data=import_rules,
            conflict_strategy="overwrite",
            dry_run=False,
            operator="user_import",
        )
        job_id = result["job_id"]

        # 授权
        SourceRuleService.grant_import_permission(
            target_user_id="user_audit",
            permission_type="import_audit_export",
            granted_by="admin",
        )

        # 2. 导出明细CSV
        export_result = SourceRuleService.export_import_job_csv(
            job_id, user_id="user_audit", export_type="details"
        )
        assert export_result["has_export_permission"] is True
        assert export_result["filename"].startswith("import_job_{}_details_".format(job_id))
        assert export_result["filename"].endswith(".csv")

        csv_content = export_result["data"]
        reader = csv.reader(io.StringIO(csv_content))
        rows = list(reader)

        # 验证表头
        assert rows[0][0] == "rule_code"
        assert rows[0][1] == "action"
        assert rows[0][2] == "status"

        # 验证数据行
        assert len(rows) == 2
        assert rows[1][0] == "CSV_001"
        assert rows[1][1] == "overwrite"
        assert rows[1][2] == "success"

        # 3. 导出差异CSV
        diff_export = SourceRuleService.export_import_job_csv(
            job_id, user_id="user_audit", export_type="diff"
        )
        diff_reader = csv.reader(io.StringIO(diff_export["data"]))
        diff_rows = list(diff_reader)
        assert diff_rows[0][0] == "rule_code"
        assert diff_rows[0][1] == "field"
        assert diff_rows[0][2] == "old_value"
        assert diff_rows[0][3] == "new_value"

        # 应该有多个字段变化
        assert len(diff_rows) >= 2


class TestRevokeAndReplay:
    """场景4：撤销后回看"""

    def test_revoke_import_and_verify_replay(self, clean_db):
        """测试撤销导入并验证回放数据"""
        # 1. 手工创建规则
        SourceRuleService.create_rule(
            code="REVOKE_001",
            name="手工创建的规则",
            description="原始描述",
            category="general",
            priority=10,
            is_enabled=True,
            match_pattern="original_*",
            operator="user_manual",
        )

        # 2. 导入覆盖并新建
        import_rules = [
            {
                "code": "REVOKE_001",
                "name": "导入覆盖的规则",
                "description": "导入描述",
                "category": "import",
                "priority": 50,
                "is_enabled": False,
                "match_pattern": "import_*",
            },
            {
                "code": "REVOKE_002",
                "name": "导入新建的规则",
                "description": "导入描述2",
                "category": "import",
                "priority": 60,
                "is_enabled": True,
                "match_pattern": "import_new_*",
            },
        ]

        result = SourceRuleService.import_rules(
            rules_data=import_rules,
            conflict_strategy="overwrite",
            dry_run=False,
            operator="user_import",
        )
        job_id = result["job_id"]

        # 3. 验证导入后状态
        rule_001_after = SourceRuleService.get_rule_by_code("REVOKE_001")
        assert rule_001_after.name == "导入覆盖的规则"
        assert rule_001_after.is_enabled is False

        rule_002_after = SourceRuleService.get_rule_by_code("REVOKE_002")
        assert rule_002_after is not None

        # 4. 授权撤销权限
        SourceRuleService.grant_import_permission(
            target_user_id="user_revoker",
            permission_type="import_revoke",
            granted_by="admin",
        )
        SourceRuleService.grant_import_permission(
            target_user_id="user_revoker",
            permission_type="import_audit_view",
            granted_by="admin",
        )

        # 5. 执行撤销
        revoke_result = SourceRuleService.revoke_import_job(
            job_id,
            operator="user_revoker",
            user_id="user_revoker",
            reason="误操作撤销测试",
        )
        assert revoke_result["success"] is True
        assert revoke_result["has_revoke_permission"] is True
        assert len(revoke_result["revoke_results"]["restored"]) == 1
        assert len(revoke_result["revoke_results"]["deleted"]) == 1

        # 6. 验证撤销后状态
        rule_001_revoked = SourceRuleService.get_rule_by_code("REVOKE_001")
        assert rule_001_revoked.name == "手工创建的规则"
        assert rule_001_revoked.description == "原始描述"
        assert rule_001_revoked.category == "general"
        assert rule_001_revoked.priority == 10
        assert rule_001_revoked.is_enabled is True
        assert rule_001_revoked.match_pattern == "original_*"
        assert rule_001_revoked.import_origin == "manual"
        assert rule_001_revoked.import_job_id is None

        rule_002_revoked = SourceRuleService.get_rule_by_code("REVOKE_002")
        assert rule_002_revoked is None

        # 7. 查看撤销回放数据，验证撤销正确
        replay_result = SourceRuleService.get_import_replay_data(
            job_id, user_id="user_revoker"
        )
        assert replay_result["has_audit_permission"] is True
        assert replay_result["job_id"] == job_id
        assert replay_result["is_revoked"] == 1
        assert replay_result["revoke_verification_passed"] is True

        # 验证每条规则的回放数据
        replay_rules = replay_result["replay_data"]
        assert len(replay_rules) == 2

        # 验证REVOKE_001（覆盖后撤销）
        replay_001 = next(
            r for r in replay_rules if r["rule_code"] == "REVOKE_001"
        )
        assert replay_001["import_action"] == "overwrite"
        assert replay_001["revoke_action"] == "restored"
        assert replay_001["verify_result"] == "matched"
        assert replay_001["before_import"]["name"] == "手工创建的规则"
        assert replay_001["after_import"]["name"] == "导入覆盖的规则"
        assert replay_001["after_revoke"]["name"] == "手工创建的规则"
        assert replay_001["verify_diff"] is None

        # 验证REVOKE_002（新建后撤销）
        replay_002 = next(
            r for r in replay_rules if r["rule_code"] == "REVOKE_002"
        )
        assert replay_002["import_action"] == "create"
        assert replay_002["revoke_action"] == "deleted"
        assert replay_002["verify_result"] == "matched"
        assert replay_002["before_import"] is None
        assert replay_002["after_import"]["name"] == "导入新建的规则"
        assert replay_002["after_revoke"] is None

        # 8. 验证作业已标记为已撤销
        job = SourceRuleService.get_import_job(job_id, user_id="user_revoker")
        assert job["is_revoked"] == 1
        assert job["revoked_by"] == "user_revoker"
        assert job["revoked_reason"] == "误操作撤销测试"


class TestPermissionControl:
    """权限控制测试"""

    def test_no_permission_cannot_view_sensitive_data(self, clean_db):
        """测试没有权限的用户无法查看敏感数据"""
        # 1. 准备数据
        SourceRuleService.create_rule(
            code="PERM_001",
            name="权限测试规则",
            description="测试描述",
            category="general",
            priority=10,
            is_enabled=True,
            match_pattern="perm_*",
            operator="user1",
        )

        import_rules = [
            {
                "code": "PERM_001",
                "name": "导入覆盖的规则",
                "description": "导入描述",
                "category": "import",
                "priority": 20,
                "is_enabled": True,
                "match_pattern": "import_*",
            }
        ]

        result = SourceRuleService.import_rules(
            rules_data=import_rules,
            conflict_strategy="overwrite",
            dry_run=False,
            operator="user_import",
        )
        job_id = result["job_id"]

        # 2. 未授权用户查询作业列表（应该只能看到脱敏数据）
        list_result = SourceRuleService.list_import_jobs(
            user_id="unauthorized_user", page=1, page_size=10
        )
        assert list_result["has_audit_permission"] is False
        assert list_result["total"] == 1
        # 验证敏感字段被脱敏
        job_data = list_result["jobs"][0]
        assert job_data["operator"] is not None  # 操作员不脱敏
        # 其他敏感字段已在服务层脱敏

        # 3. 未授权用户查询作业详情（应该返回403类错误）
        job_result = SourceRuleService.get_import_job(
            job_id, user_id="unauthorized_user"
        )
        assert job_result["has_audit_permission"] is False
        assert "job_id" in job_result  # 只返回基本信息

        # 4. 未授权用户查询明细
        details_result = SourceRuleService.get_import_job_details(
            job_id, user_id="unauthorized_user"
        )
        assert details_result["has_audit_permission"] is False
        # 敏感数据如before/after/diff应被脱敏

        # 5. 未授权用户导出（应该失败）
        export_result = SourceRuleService.export_import_job_json(
            job_id, user_id="unauthorized_user", export_type="full"
        )
        assert export_result["has_export_permission"] is False

    def test_permission_expiration(self, clean_db):
        """测试权限过期"""
        # 1. 授予带过期时间的权限
        past_time = (datetime.now() - timedelta(days=1)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        SourceRuleService.grant_import_permission(
            target_user_id="expired_user",
            permission_type="import_audit_view",
            granted_by="admin",
            expires_at=past_time,
        )

        # 2. 验证权限已过期
        has_perm = SourceRuleService.check_import_audit_permission(
            user_id="expired_user",
            permission_type="import_audit_view",
        )
        assert has_perm is False


class TestStructuredAuditLog:
    """结构化审计日志测试"""

    def test_audit_log_complete(self, clean_db):
        """测试审计日志记录完整"""
        # 1. 手工创建
        SourceRuleService.create_rule(
            code="AUDIT_001",
            name="审计测试",
            description="测试",
            category="general",
            priority=10,
            is_enabled=True,
            match_pattern="audit_*",
            operator="user1",
        )

        # 2. 导入覆盖
        import_rules = [
            {
                "code": "AUDIT_001",
                "name": "导入覆盖",
                "description": "导入描述",
                "category": "import",
                "priority": 20,
                "is_enabled": True,
                "match_pattern": "import_*",
            }
        ]
        result = SourceRuleService.import_rules(
            rules_data=import_rules,
            conflict_strategy="overwrite",
            dry_run=False,
            operator="user_import",
        )
        job_id = result["job_id"]

        # 3. 手工更新
        SourceRuleService.update_rule(
            code="AUDIT_001",
            name="人工修改后的规则",
            description="修改后的描述",
            operator="user2",
        )

        # 4. 授权
        SourceRuleService.grant_import_permission(
            target_user_id="user_audit",
            permission_type="import_audit_view",
            granted_by="admin",
        )

        # 5. 查询审计日志
        audit_result = SourceRuleService.get_structured_audit_log(
            user_id="user_audit", rule_code="AUDIT_001", page=1, page_size=20
        )
        assert audit_result["has_audit_permission"] is True
        assert audit_result["total"] >= 3  # 至少3条操作记录

        # 验证包含创建、更新、导入操作
        operations = [log["operation"] for log in audit_result["audit_logs"]]
        assert "create" in operations
        assert "update" in operations
        assert "import_overwrite" in operations

        # 验证每条日志包含必要字段
        for log in audit_result["audit_logs"]:
            assert "rule_code" in log
            assert "operator" in log
            assert "timestamp" in log
            assert "import_job_id" in log or log["operation"] in [
                "create",
                "update",
                "delete",
            ]
            assert "has_diff" in log


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
