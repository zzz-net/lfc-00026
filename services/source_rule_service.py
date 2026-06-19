import logging
import json
import os
import re
import hashlib
import time
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass

from database import get_db, transaction, now_str, SOURCE_RULES_VERSION

logger = logging.getLogger(__name__)

VALID_CATEGORIES = {"onsite", "system", "import", "remote", "general"}


@dataclass
class SourceRule:
    id: Optional[int]
    code: str
    name: str
    description: Optional[str]
    category: str
    priority: int
    is_enabled: bool
    match_pattern: Optional[str]
    version: str
    created_at: Optional[str]
    updated_at: Optional[str]
    source_layer: str = "runtime"
    import_origin: Optional[str] = None
    import_job_id: Optional[int] = None
    last_manual_modified_at: Optional[str] = None
    last_manual_modified_by: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "code": self.code,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "priority": self.priority,
            "is_enabled": self.is_enabled,
            "match_pattern": self.match_pattern,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source_layer": self.source_layer,
            "import_origin": self.import_origin,
            "import_job_id": self.import_job_id,
            "last_manual_modified_at": self.last_manual_modified_at,
            "last_manual_modified_by": self.last_manual_modified_by,
        }


class SourceRuleService:
    RULES_VERSION = SOURCE_RULES_VERSION
    SUPPORTED_VERSIONS = {"1.0"}
    CONFLICT_STRATEGIES = {"skip", "overwrite", "report"}

    _cache: Dict[str, Any] = {}
    _cache_timestamp: float = 0
    _CACHE_TTL: float = 5.0

    @staticmethod
    def _invalidate_cache():
        SourceRuleService._cache = {}
        SourceRuleService._cache_timestamp = 0
        logger.info(json.dumps({
            "event": "source_rule_cache_invalidated",
            "timestamp": now_str(),
        }, ensure_ascii=False))

    @staticmethod
    def ensure_cache_fresh():
        SourceRuleService._invalidate_cache()
        SourceRuleService.get_merged_rules()
        logger.info(json.dumps({
            "event": "source_rule_cache_freshened",
            "timestamp": now_str(),
        }, ensure_ascii=False))

    DEFAULT_RULES: List[Dict[str, Any]] = [
        {
            "code": "window",
            "name": "线下窗口",
            "description": "食堂窗口人工补录",
            "category": "onsite",
            "priority": 100,
            "is_enabled": True,
            "match_pattern": None,
            "version": RULES_VERSION,
            "source_layer": "default",
        },
        {
            "code": "admin",
            "name": "管理员后台",
            "description": "管理员在后台系统补录",
            "category": "system",
            "priority": 90,
            "is_enabled": True,
            "match_pattern": None,
            "version": RULES_VERSION,
            "source_layer": "default",
        },
        {
            "code": "manual",
            "name": "手动导入",
            "description": "通过批量导入方式补录",
            "category": "import",
            "priority": 80,
            "is_enabled": True,
            "match_pattern": None,
            "version": RULES_VERSION,
            "source_layer": "default",
        },
        {
            "code": "phone_order",
            "name": "电话订餐",
            "description": "通过电话方式补录的订单",
            "category": "remote",
            "priority": 70,
            "is_enabled": True,
            "match_pattern": "^phone_.*",
            "version": RULES_VERSION,
            "source_layer": "default",
        },
        {
            "code": "pos",
            "name": "POS终端",
            "description": "POS终端设备自动补录",
            "category": "system",
            "priority": 85,
            "is_enabled": True,
            "match_pattern": "^pos_.*",
            "version": RULES_VERSION,
            "source_layer": "default",
        },
    ]

    @staticmethod
    def _parse_env_rules(env_value: str) -> List[Dict[str, Any]]:
        if not env_value:
            return []
        try:
            rules = json.loads(env_value)
            if not isinstance(rules, list):
                logger.warning(json.dumps({
                    "event": "env_rules_parse_error",
                    "reason": "not_array",
                    "raw": env_value[:200],
                }, ensure_ascii=False))
                return []
            parsed_rules = []
            for r in rules:
                parsed_rules.append({
                    **r,
                    "source_layer": "environment",
                    "version": r.get("version", SourceRuleService.RULES_VERSION),
                    "is_enabled": r.get("is_enabled", True),
                    "priority": r.get("priority", 0),
                    "category": r.get("category", "general"),
                })
            return parsed_rules
        except json.JSONDecodeError as e:
            logger.warning(json.dumps({
                "event": "env_rules_json_error",
                "error": str(e),
            }, ensure_ascii=False))
            return []

    @staticmethod
    def get_default_rules() -> List[SourceRule]:
        return [
            SourceRule(
                id=None,
                code=r["code"],
                name=r["name"],
                description=r["description"],
                category=r.get("category", "general"),
                priority=r["priority"],
                is_enabled=r["is_enabled"],
                match_pattern=r["match_pattern"],
                version=r["version"],
                created_at=None,
                updated_at=None,
                source_layer="default",
            )
            for r in SourceRuleService.DEFAULT_RULES
        ]

    @staticmethod
    def get_environment_rules() -> List[SourceRule]:
        env_value = os.environ.get("CANTEEN_SOURCE_RULES")
        rules_data = SourceRuleService._parse_env_rules(env_value)
        return [
            SourceRule(
                id=None,
                code=r["code"],
                name=r.get("name", r["code"]),
                description=r.get("description"),
                category=r.get("category", "general"),
                priority=r.get("priority", 0),
                is_enabled=r.get("is_enabled", True),
                match_pattern=r.get("match_pattern"),
                version=r.get("version", SourceRuleService.RULES_VERSION),
                created_at=None,
                updated_at=None,
                source_layer="environment",
            )
            for r in rules_data
        ]

    @staticmethod
    def get_runtime_rules(conn=None) -> List[SourceRule]:
        if conn is None:
            conn = get_db()
        rows = conn.execute(
            "SELECT * FROM source_rules ORDER BY priority DESC, code ASC"
        ).fetchall()

        def _get_field(row, field, default=None):
            try:
                return row[field]
            except (KeyError, IndexError):
                return default

        return [
            SourceRule(
                id=row["id"],
                code=row["code"],
                name=row["name"],
                description=row["description"],
                category=row["category"] if "category" in row.keys() else "general",
                priority=row["priority"],
                is_enabled=bool(row["is_enabled"]),
                match_pattern=row["match_pattern"],
                version=row["version"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                source_layer="runtime",
                import_origin=_get_field(row, "import_origin"),
                import_job_id=_get_field(row, "import_job_id"),
                last_manual_modified_at=_get_field(row, "last_manual_modified_at"),
                last_manual_modified_by=_get_field(row, "last_manual_modified_by"),
            )
            for row in rows
        ]

    @staticmethod
    def get_merged_rules(conn=None) -> List[SourceRule]:
        now = time.time()
        if (now - SourceRuleService._cache_timestamp) < SourceRuleService._CACHE_TTL:
            cached = SourceRuleService._cache.get("merged_rules")
            if cached is not None:
                return cached

        default_rules = SourceRuleService.get_default_rules()
        env_rules = SourceRuleService.get_environment_rules()
        runtime_rules = SourceRuleService.get_runtime_rules(conn)

        merged: Dict[str, SourceRule] = {}
        merge_log = []

        for rule in default_rules:
            merged[rule.code] = rule

        for rule in env_rules:
            if rule.code in merged:
                existing = merged[rule.code]
                if rule.priority >= existing.priority:
                    merge_log.append({
                        "code": rule.code,
                        "action": "env_override",
                        "from_layer": existing.source_layer,
                        "to_layer": "environment",
                        "from_priority": existing.priority,
                        "to_priority": rule.priority,
                    })
                    merged[rule.code] = rule
                else:
                    merge_log.append({
                        "code": rule.code,
                        "action": "env_kept_default",
                        "reason": "env_priority_lower",
                        "env_priority": rule.priority,
                        "default_priority": existing.priority,
                    })
            else:
                merge_log.append({
                    "code": rule.code,
                    "action": "env_added",
                })
                merged[rule.code] = rule

        for rule in runtime_rules:
            if rule.code in merged:
                existing = merged[rule.code]
                if rule.priority >= existing.priority:
                    merge_log.append({
                        "code": rule.code,
                        "action": "runtime_override",
                        "from_layer": existing.source_layer,
                        "to_layer": "runtime",
                        "from_priority": existing.priority,
                        "to_priority": rule.priority,
                    })
                    merged[rule.code] = rule
                else:
                    merge_log.append({
                        "code": rule.code,
                        "action": "runtime_kept_existing",
                        "reason": "runtime_priority_lower",
                        "runtime_priority": rule.priority,
                        "existing_priority": existing.priority,
                        "existing_layer": existing.source_layer,
                    })
            else:
                merge_log.append({
                    "code": rule.code,
                    "action": "runtime_added",
                })
                merged[rule.code] = rule

        result = sorted(
            [r for r in merged.values() if r.is_enabled],
            key=lambda r: (-r.priority, r.code),
        )

        logger.info(json.dumps({
            "event": "source_rules_merged",
            "total_enabled": len(result),
            "default_count": len(default_rules),
            "environment_count": len(env_rules),
            "runtime_count": len(runtime_rules),
            "merge_details": merge_log,
        }, ensure_ascii=False))

        SourceRuleService._cache["merged_rules"] = result
        SourceRuleService._cache["merge_log"] = merge_log
        SourceRuleService._cache_timestamp = now

        return result

    @staticmethod
    def get_rule_by_code(code: str, conn=None) -> Optional[SourceRule]:
        if conn is None:
            conn = get_db()
        row = conn.execute(
            "SELECT * FROM source_rules WHERE code = ?", (code,)
        ).fetchone()
        if row:
            def _get_field(row, field, default=None):
                try:
                    return row[field]
                except (KeyError, IndexError):
                    return default
            return SourceRule(
                id=row["id"],
                code=row["code"],
                name=row["name"],
                description=row["description"],
                category=row["category"] if "category" in row.keys() else "general",
                priority=row["priority"],
                is_enabled=bool(row["is_enabled"]),
                match_pattern=row["match_pattern"],
                version=row["version"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                source_layer="runtime",
                import_origin=_get_field(row, "import_origin"),
                import_job_id=_get_field(row, "import_job_id"),
                last_manual_modified_at=_get_field(row, "last_manual_modified_at"),
                last_manual_modified_by=_get_field(row, "last_manual_modified_by"),
            )
        for r in SourceRuleService.get_default_rules():
            if r.code == code:
                return r
        for r in SourceRuleService.get_environment_rules():
            if r.code == code:
                return r
        return None

    @staticmethod
    def get_allowed_sources(conn=None) -> List[str]:
        rules = SourceRuleService.get_merged_rules(conn)
        return [r.code for r in rules]

    @staticmethod
    def match_source(source_code: str, conn=None) -> Optional[SourceRule]:
        rules = SourceRuleService.get_merged_rules(conn)

        for rule in rules:
            if rule.code == source_code:
                logger.info(json.dumps({
                    "event": "source_rule_exact_match",
                    "input_source": source_code,
                    "matched_code": rule.code,
                    "matched_name": rule.name,
                    "matched_priority": rule.priority,
                    "matched_category": rule.category,
                    "matched_layer": rule.source_layer,
                    "match_type": "exact",
                }, ensure_ascii=False))
                return rule

            if rule.match_pattern:
                try:
                    if re.match(rule.match_pattern, source_code):
                        logger.info(json.dumps({
                            "event": "source_rule_pattern_match",
                            "input_source": source_code,
                            "matched_code": rule.code,
                            "matched_name": rule.name,
                            "matched_priority": rule.priority,
                            "matched_category": rule.category,
                            "matched_layer": rule.source_layer,
                            "match_pattern": rule.match_pattern,
                            "match_type": "pattern",
                        }, ensure_ascii=False))
                        return rule
                except re.error as e:
                    logger.warning(json.dumps({
                        "event": "source_rule_pattern_error",
                        "rule_code": rule.code,
                        "pattern": rule.match_pattern,
                        "error": str(e),
                    }, ensure_ascii=False))

        logger.warning(json.dumps({
            "event": "source_rule_no_match",
            "input_source": source_code,
            "allowed_sources": [r.code for r in rules],
        }, ensure_ascii=False))
        return None

    @staticmethod
    def detect_source(source_code: Optional[str], remark: str = None, conn=None) -> Tuple[str, Optional[SourceRule]]:
        if source_code:
            rule = SourceRuleService.match_source(source_code, conn)
            if rule:
                return source_code, rule
            rules = SourceRuleService.get_merged_rules(conn)
            for r in rules:
                if r.match_pattern:
                    try:
                        if re.match(r.match_pattern, source_code):
                            logger.info(json.dumps({
                                "event": "source_auto_detected_from_code_pattern",
                                "source_code": source_code,
                                "detected_code": r.code,
                                "matched_pattern": r.match_pattern,
                            }, ensure_ascii=False))
                            return source_code, r
                    except re.error:
                        pass
            return source_code, None

        if remark:
            rules = SourceRuleService.get_merged_rules(conn)
            for rule in rules:
                if rule.match_pattern:
                    try:
                        if re.match(rule.match_pattern, remark):
                            logger.info(json.dumps({
                                "event": "source_auto_detected_from_remark",
                                "remark": remark,
                                "detected_code": rule.code,
                                "matched_pattern": rule.match_pattern,
                            }, ensure_ascii=False))
                            return rule.code, rule
                    except re.error:
                        pass

        makeup_cfg_default = "window"
        try:
            from services.config_service import ConfigService
            makeup_cfg_default = ConfigService.get_config("makeup_default_source", conn) or "window"
        except Exception:
            pass

        default_rule = SourceRuleService.match_source(makeup_cfg_default, conn)
        logger.info(json.dumps({
            "event": "source_fallback_to_default",
            "default_source": makeup_cfg_default,
            "input_source": source_code,
            "remark": remark,
            "matched_rule": default_rule.code if default_rule else None,
        }, ensure_ascii=False))
        return makeup_cfg_default, default_rule

    @staticmethod
    def validate_source(source_code: str, conn=None) -> Tuple[bool, Optional[str], Optional[SourceRule]]:
        if not source_code:
            return False, "来源不能为空", None

        rule = SourceRuleService.match_source(source_code, conn)
        if rule:
            if not rule.is_enabled:
                return True, None, rule
            return True, None, rule

        rule = SourceRuleService.get_rule_by_code(source_code, conn)
        if rule:
            return True, None, rule

        allowed = SourceRuleService.get_allowed_sources(conn)
        return (
            False,
            f"补录来源 '{source_code}' 不合法，允许的来源: {', '.join(allowed)}",
            None,
        )

    @staticmethod
    def validate_rule_data(rule_data: Dict[str, Any], check_merged: bool = False, conn=None) -> Tuple[bool, List[str]]:
        errors = []

        if not isinstance(rule_data, dict):
            errors.append("规则数据必须是对象")
            return False, errors

        if "code" not in rule_data or not rule_data["code"]:
            errors.append("缺少必填字段: code")
        elif not isinstance(rule_data["code"], str):
            errors.append("code 必须是字符串")
        elif len(rule_data["code"]) > 50:
            errors.append("code 长度不能超过50字符")

        if "name" not in rule_data or not rule_data["name"]:
            errors.append("缺少必填字段: name")
        elif not isinstance(rule_data["name"], str):
            errors.append("name 必须是字符串")
        elif len(rule_data["name"]) > 100:
            errors.append("name 长度不能超过100字符")

        if "version" in rule_data:
            if rule_data["version"] not in SourceRuleService.SUPPORTED_VERSIONS:
                errors.append(
                    f"版本 {rule_data['version']} 不支持，支持的版本: "
                    f"{', '.join(SourceRuleService.SUPPORTED_VERSIONS)}"
                )

        if "category" in rule_data:
            if rule_data["category"] not in VALID_CATEGORIES:
                errors.append(
                    f"category '{rule_data['category']}' 不合法，允许的值: "
                    f"{', '.join(sorted(VALID_CATEGORIES))}"
                )

        if "priority" in rule_data:
            if not isinstance(rule_data["priority"], int):
                errors.append("priority 必须是整数")
            elif rule_data["priority"] < 0 or rule_data["priority"] > 1000:
                errors.append("priority 必须在 0-1000 之间")

        if "is_enabled" in rule_data:
            if not isinstance(rule_data["is_enabled"], bool):
                errors.append("is_enabled 必须是布尔值")

        if "match_pattern" in rule_data and rule_data["match_pattern"]:
            try:
                re.compile(rule_data["match_pattern"])
            except re.error as e:
                errors.append(f"match_pattern 不是有效的正则表达式: {e}")

        if check_merged and "code" in rule_data and isinstance(rule_data["code"], str):
            merged_rules = SourceRuleService.get_merged_rules(conn)
            matched = next((r for r in merged_rules if r.code == rule_data["code"]), None)
            if matched:
                existing_info = {
                    "code": matched.code,
                    "name": matched.name,
                    "priority": matched.priority,
                    "category": matched.category,
                    "is_enabled": matched.is_enabled,
                    "source_layer": matched.source_layer,
                }
                errors.append(
                    f"来源 code='{rule_data['code']}' 在合并规则中已存在"
                    f" (层级: {matched.source_layer}, 名称: {matched.name}, "
                    f"优先级: {matched.priority}, 类别: {matched.category}); "
                    f"现有规则: {json.dumps(existing_info, ensure_ascii=False)}"
                )

        return len(errors) == 0, errors

    @staticmethod
    def _write_audit_log(rule_code: str, operation: str, before_data: Dict = None,
                         after_data: Dict = None, operator: str = None,
                         remark: str = None, import_id: int = None, conn=None):
        if conn is None:
            conn = get_db()
        now = now_str()
        conn.execute(
            """INSERT INTO source_rules_audit_log
               (rule_code, operation, operator, before_json, after_json, remark, import_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rule_code,
                operation,
                operator,
                json.dumps(before_data, ensure_ascii=False) if before_data else None,
                json.dumps(after_data, ensure_ascii=False) if after_data else None,
                remark,
                import_id,
                now,
            ),
        )

    @staticmethod
    def get_audit_log(rule_code: str = None, import_id: int = None, limit: int = 50, conn=None) -> List[Dict[str, Any]]:
        if conn is None:
            conn = get_db()
        conditions = []
        params = []
        if rule_code:
            conditions.append("rule_code = ?")
            params.append(rule_code)
        if import_id:
            conditions.append("import_id = ?")
            params.append(import_id)
        where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = conn.execute(
            f"SELECT * FROM source_rules_audit_log{where_clause} ORDER BY id DESC LIMIT ?",
            params + [limit],
        ).fetchall()

        result = []
        for row in rows:
            entry = dict(row)
            try:
                entry["before"] = json.loads(entry["before_json"]) if entry["before_json"] else None
            except json.JSONDecodeError:
                entry["before"] = None
            try:
                entry["after"] = json.loads(entry["after_json"]) if entry["after_json"] else None
            except json.JSONDecodeError:
                entry["after"] = None
            entry.pop("before_json", None)
            entry.pop("after_json", None)
            result.append(entry)
        return result

    @staticmethod
    def list_rules(conn=None) -> Dict[str, Any]:
        merged_rules = SourceRuleService.get_merged_rules(conn)
        runtime_rules = SourceRuleService.get_runtime_rules(conn)
        default_rules = SourceRuleService.get_default_rules()
        env_rules = SourceRuleService.get_environment_rules()

        categories = {}
        for r in merged_rules:
            cat = r.category
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(r.code)

        return {
            "version": SourceRuleService.RULES_VERSION,
            "total": len(merged_rules),
            "rules": [r.to_dict() for r in merged_rules],
            "categories": categories,
            "layers": {
                "default": [r.to_dict() for r in default_rules],
                "environment": [r.to_dict() for r in env_rules],
                "runtime": [r.to_dict() for r in runtime_rules],
            },
            "allowed_sources": SourceRuleService.get_allowed_sources(conn),
        }

    @staticmethod
    def get_rule(code: str, conn=None) -> Optional[SourceRule]:
        for rule in SourceRuleService.get_merged_rules(conn):
            if rule.code == code:
                return rule
        return None

    @staticmethod
    def create_rule(
        code: str,
        name: str,
        description: str = None,
        category: str = "general",
        priority: int = 0,
        is_enabled: bool = True,
        match_pattern: str = None,
        operator: str = None,
        is_import_operation: bool = False,
        import_job_id: int = None,
        conn=None,
    ) -> SourceRule:
        rule_data = {
            "code": code,
            "name": name,
            "description": description,
            "category": category,
            "priority": priority,
            "is_enabled": is_enabled,
            "match_pattern": match_pattern,
            "version": SourceRuleService.RULES_VERSION,
        }

        is_valid, errors = SourceRuleService.validate_rule_data(rule_data, check_merged=True, conn=conn)
        if not is_valid:
            raise ValueError(f"规则数据验证失败: {'; '.join(errors)}")

        def _execute(conn_inner):
            existing = conn_inner.execute(
                "SELECT * FROM source_rules WHERE code = ?", (code,)
            ).fetchone()
            if existing:
                raise ValueError(f"来源规则 code={code} 已存在")

            now = now_str()
            import_origin = "import" if is_import_operation else "manual"
            last_manual_modified_at = None if is_import_operation else now
            last_manual_modified_by = None if is_import_operation else operator

            insert_fields = [
                "code", "name", "description", "category", "priority", "is_enabled",
                "match_pattern", "version", "created_at", "updated_at", "import_origin"
            ]
            insert_values = [
                code, name, description, category, priority,
                1 if is_enabled else 0, match_pattern,
                SourceRuleService.RULES_VERSION, now, now, import_origin
            ]

            if import_job_id:
                insert_fields.append("import_job_id")
                insert_values.append(import_job_id)

            if not is_import_operation:
                insert_fields.append("last_manual_modified_at")
                insert_fields.append("last_manual_modified_by")
                insert_values.append(now)
                insert_values.append(operator)

            placeholders = ", ".join(["?"] * len(insert_fields))
            field_names = ", ".join(insert_fields)

            conn_inner.execute(
                f"INSERT INTO source_rules ({field_names}) VALUES ({placeholders})",
                tuple(insert_values),
            )

            row = conn_inner.execute(
                "SELECT * FROM source_rules WHERE code = ?", (code,)
            ).fetchone()

            def _get_field(row, field, default=None):
                try:
                    return row[field]
                except (KeyError, IndexError):
                    return default

            created_rule = SourceRule(
                id=row["id"],
                code=row["code"],
                name=row["name"],
                description=row["description"],
                category=row["category"] if "category" in row.keys() else "general",
                priority=row["priority"],
                is_enabled=bool(row["is_enabled"]),
                match_pattern=row["match_pattern"],
                version=row["version"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                source_layer="runtime",
                import_origin=_get_field(row, "import_origin", import_origin),
                import_job_id=_get_field(row, "import_job_id"),
                last_manual_modified_at=_get_field(row, "last_manual_modified_at"),
                last_manual_modified_by=_get_field(row, "last_manual_modified_by"),
            )

            operation = "import_create" if is_import_operation else "create"
            SourceRuleService._write_audit_log(
                rule_code=code,
                operation=operation,
                after_data=created_rule.to_dict(),
                operator=operator,
                import_id=import_job_id if is_import_operation else None,
                conn=conn_inner,
            )

            SourceRuleService._write_lineage(
                rule_code=code,
                source_type="manual_create" if not is_import_operation else "import_create",
                snapshot_data=created_rule.to_dict(),
                operator=operator,
                import_job_id=import_job_id if is_import_operation else None,
                conn=conn_inner,
            )

            logger.info(json.dumps({
                "event": "source_rule_created",
                "code": code,
                "name": name,
                "category": category,
                "priority": priority,
                "operator": operator,
            }, ensure_ascii=False))

            SourceRuleService._invalidate_cache()

            return created_rule

        if conn is None:
            with transaction() as conn:
                return _execute(conn)
        else:
            return _execute(conn)

    @staticmethod
    def update_rule(
        code: str,
        name: str = None,
        description: str = None,
        category: str = None,
        priority: int = None,
        is_enabled: bool = None,
        match_pattern: str = None,
        operator: str = None,
        is_import_operation: bool = False,
        import_job_id: int = None,
        expected_before: Dict = None,
        conn=None,
    ) -> SourceRule:
        def _execute(conn_inner):
            existing = conn_inner.execute(
                "SELECT * FROM source_rules WHERE code = ?", (code,)
            ).fetchone()
            if not existing:
                raise ValueError(f"来源规则 code={code} 不存在")

            def _get_field(row, field, default=None):
                try:
                    return row[field]
                except (KeyError, IndexError):
                    return default

            before_data = dict(existing)
            before_data["is_enabled"] = bool(before_data["is_enabled"])
            if "category" not in before_data:
                before_data["category"] = "general"

            if expected_before and not is_import_operation:
                conflict = SourceRuleService._check_conflict(expected_before, before_data)
                if conflict:
                    raise ValueError(
                        f"检测到冲突: 规则 {code} 在读取后被修改。"
                        f" 期望: {json.dumps(expected_before, ensure_ascii=False)}"
                        f" 实际: {json.dumps(before_data, ensure_ascii=False)}"
                    )

            update_data = {}
            if name is not None:
                update_data["name"] = name
            if description is not None:
                update_data["description"] = description
            if category is not None:
                update_data["category"] = category
            if priority is not None:
                update_data["priority"] = priority
            if is_enabled is not None:
                update_data["is_enabled"] = 1 if is_enabled else 0
            if match_pattern is not None:
                update_data["match_pattern"] = match_pattern

            if not update_data:
                raise ValueError("没有需要更新的字段")

            validate_data = {**before_data, **update_data}
            if "is_enabled" in update_data:
                validate_data["is_enabled"] = bool(update_data["is_enabled"])
            is_valid, errors = SourceRuleService.validate_rule_data(validate_data)
            if not is_valid:
                raise ValueError(f"规则数据验证失败: {'; '.join(errors)}")

            now = now_str()
            update_data["updated_at"] = now

            if not is_import_operation:
                update_data["last_manual_modified_at"] = now
                update_data["last_manual_modified_by"] = operator
                update_data["import_origin"] = "manual"

            if is_import_operation and import_job_id:
                update_data["import_job_id"] = import_job_id
                update_data["import_origin"] = "import"

            set_clause = ", ".join(f"{k} = ?" for k in update_data.keys())
            values = list(update_data.values()) + [code]

            conn_inner.execute(
                f"UPDATE source_rules SET {set_clause} WHERE code = ?",
                values,
            )

            row = conn_inner.execute(
                "SELECT * FROM source_rules WHERE code = ?", (code,)
            ).fetchone()

            updated_rule = SourceRule(
                id=row["id"],
                code=row["code"],
                name=row["name"],
                description=row["description"],
                category=row["category"] if "category" in row.keys() else "general",
                priority=row["priority"],
                is_enabled=bool(row["is_enabled"]),
                match_pattern=row["match_pattern"],
                version=row["version"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                source_layer="runtime",
                import_origin=_get_field(row, "import_origin"),
                import_job_id=_get_field(row, "import_job_id"),
                last_manual_modified_at=_get_field(row, "last_manual_modified_at"),
                last_manual_modified_by=_get_field(row, "last_manual_modified_by"),
            )

            after_data = updated_rule.to_dict()

            operation = "import_overwrite" if is_import_operation else "update"
            SourceRuleService._write_audit_log(
                rule_code=code,
                operation=operation,
                before_data=before_data,
                after_data=after_data,
                operator=operator,
                import_id=import_job_id if is_import_operation else None,
                conn=conn_inner,
            )

            parent_lid = SourceRuleService._get_last_lineage_id(code, conn_inner)
            SourceRuleService._write_lineage(
                rule_code=code,
                source_type="manual_update" if not is_import_operation else "import_overwrite",
                snapshot_data=after_data,
                operator=operator,
                import_job_id=import_job_id if is_import_operation else None,
                parent_lineage_id=parent_lid,
                conn=conn_inner,
            )

            logger.info(json.dumps({
                "event": "source_rule_updated",
                "code": code,
                "updated_fields": list(update_data.keys()),
                "before": before_data,
                "after": after_data,
                "operator": operator,
                "is_import_operation": is_import_operation,
            }, ensure_ascii=False))

            SourceRuleService._invalidate_cache()

            return updated_rule

        if conn is None:
            with transaction() as conn:
                return _execute(conn)
        else:
            return _execute(conn)

    @staticmethod
    def _check_conflict(expected: Dict, actual: Dict) -> Optional[Dict]:
        compare_fields = ["name", "description", "category", "priority", "is_enabled", "match_pattern", "version", "updated_at"]
        diff = {}
        for field in compare_fields:
            exp_val = expected.get(field)
            act_val = actual.get(field)
            if exp_val != act_val:
                diff[field] = {"expected": exp_val, "actual": act_val}
        return diff if diff else None

    @staticmethod
    def _compute_diff(before: Dict, after: Dict) -> Dict:
        all_fields = set(list(before.keys()) + list(after.keys()))
        changed_fields = []
        field_changes = {}
        for field in all_fields:
            if field in ["id", "created_at", "updated_at", "source_layer"]:
                continue
            b_val = before.get(field)
            a_val = after.get(field)
            if b_val != a_val:
                change = {"field": field, "before": b_val, "after": a_val}
                changed_fields.append(change)
                field_changes[field] = {"before": b_val, "after": a_val}
        return {
            "changed_fields": changed_fields,
            "field_changes": field_changes,
            "changed_count": len(changed_fields),
        }

    @staticmethod
    def _compute_content_hash(data: Dict) -> str:
        canonical = json.dumps(data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _write_lineage(rule_code: str, source_type: str, snapshot_data: Dict,
                       operator: str = None, import_job_id: int = None,
                       parent_lineage_id: int = None, remark: str = None,
                       conn=None):
        if conn is None:
            conn = get_db()
        now = now_str()
        content_hash = SourceRuleService._compute_content_hash(snapshot_data)
        conn.execute(
            """INSERT INTO source_rules_lineage
               (rule_code, source_type, operator, import_job_id, snapshot_json,
                content_hash, parent_lineage_id, remark, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rule_code, source_type, operator, import_job_id,
                json.dumps(snapshot_data, ensure_ascii=False),
                content_hash, parent_lineage_id, remark, now,
            ),
        )

    @staticmethod
    def _get_last_lineage_id(rule_code: str, conn=None) -> Optional[int]:
        if conn is None:
            conn = get_db()
        row = conn.execute(
            "SELECT id FROM source_rules_lineage WHERE rule_code = ? ORDER BY id DESC LIMIT 1",
            (rule_code,),
        ).fetchone()
        return row["id"] if row else None

    @staticmethod
    def _generate_job_id() -> str:
        import uuid
        return f"IMP-{int(time.time())}-{uuid.uuid4().hex[:8].upper()}"

    @staticmethod
    def delete_rule(code: str, operator: str = None, conn=None) -> bool:
        def _execute(conn_inner):
            existing = conn_inner.execute(
                "SELECT * FROM source_rules WHERE code = ?", (code,)
            ).fetchone()
            if not existing:
                raise ValueError(f"来源规则 code={code} 不存在")

            before_data = dict(existing)
            before_data["is_enabled"] = bool(before_data["is_enabled"])
            if "category" not in before_data:
                before_data["category"] = "general"

            conn_inner.execute("DELETE FROM source_rules WHERE code = ?", (code,))

            SourceRuleService._write_audit_log(
                rule_code=code,
                operation="delete",
                before_data=before_data,
                operator=operator,
                conn=conn_inner,
            )

            logger.info(json.dumps({
                "event": "source_rule_deleted",
                "code": code,
                "deleted_rule": before_data,
                "operator": operator,
            }, ensure_ascii=False))

            SourceRuleService._invalidate_cache()
            return True

        if conn is None:
            with transaction() as conn:
                return _execute(conn)
        else:
            return _execute(conn)

    @staticmethod
    def import_rules(
        rules_data: List[Dict[str, Any]],
        conflict_strategy: str = "skip",
        dry_run: bool = False,
        operator: str = None,
        check_concurrent_modifications: bool = True,
        conn=None,
    ) -> Dict[str, Any]:
        if conflict_strategy not in SourceRuleService.CONFLICT_STRATEGIES:
            raise ValueError(
                f"冲突策略不支持: {conflict_strategy}, "
                f"支持的策略: {', '.join(SourceRuleService.CONFLICT_STRATEGIES)}"
            )

        results = {
            "success": True,
            "version": SourceRuleService.RULES_VERSION,
            "job_id": None,
            "rules_count": len(rules_data),
            "success_count": 0,
            "skipped_count": 0,
            "error_count": 0,
            "new_count": 0,
            "overwritten_count": 0,
            "conflict_count": 0,
            "disabled_blocked_count": 0,
            "summary": {
                "total": len(rules_data),
                "success": 0,
                "created": 0,
                "overwritten": 0,
                "skipped": 0,
                "failed": 0,
                "conflicts_detected": 0,
            },
            "new_rules": [],
            "overwritten_rules": [],
            "skipped_rules": [],
            "conflict_rules": [],
            "disabled_blocked_rules": [],
            "invalid_rules": [],
            "errors": [],
            "imported_rules": [],
            "dry_run": dry_run,
            "operator": operator,
        }

        if not isinstance(rules_data, list):
            results["success"] = False
            results["error_count"] = 1
            results["errors"].append("导入数据必须是规则数组")
            return results

        job_id = SourceRuleService._generate_job_id()
        results["job_id"] = job_id

        def _execute(conn_inner):
            now = now_str()
            import_job_pk = None

            if not dry_run:
                conn_inner.execute(
                    """INSERT INTO source_rules_import_jobs
                       (job_id, status, operator, conflict_strategy, rules_count, dry_run, created_at, updated_at)
                       VALUES (?, 'processing', ?, ?, ?, ?, ?, ?)""",
                    (job_id, operator, conflict_strategy, len(rules_data), 1 if dry_run else 0, now, now),
                )
                job_row = conn_inner.execute(
                    "SELECT last_insert_rowid() as id"
                ).fetchone()
                import_job_pk = job_row["id"] if job_row else None
                results["import_job_id"] = import_job_pk

            all_rules_rows = conn_inner.execute(
                "SELECT * FROM source_rules"
            ).fetchall()

            def _row_to_dict(row):
                d = dict(row)
                d["is_enabled"] = bool(d["is_enabled"])
                if "category" not in d:
                    d["category"] = "general"
                return d

            existing_db_codes = {row["code"]: _row_to_dict(row) for row in all_rules_rows}

            if not dry_run and import_job_pk:
                import_codes = {r.get("code") for r in rules_data if isinstance(r, dict) and r.get("code")}
                for code, rule_dict in existing_db_codes.items():
                    if code in import_codes:
                        conn_inner.execute(
                            """INSERT INTO source_rules_import_snapshots
                               (import_job_id, rule_code, rule_json, snapshot_type, created_at)
                               VALUES (?, ?, ?, 'before_import', ?)""",
                            (import_job_pk, code, json.dumps(rule_dict, ensure_ascii=False), now),
                        )

            merged_rules = SourceRuleService.get_merged_rules(conn_inner)
            merged_codes = {r.code: r for r in merged_rules}

            if conflict_strategy == "report":
                report_conflicts = []
                for idx, rule_data in enumerate(rules_data):
                    if not isinstance(rule_data, dict):
                        continue
                    code = rule_data.get("code", f"index={idx}")
                    if code in merged_codes or code in existing_db_codes:
                        existing = merged_codes.get(code)
                        existing_dict = existing.to_dict() if existing else existing_db_codes.get(code, {})
                        report_conflicts.append({
                            "code": code,
                            "index": idx,
                            "existing_rule": existing_dict,
                            "action": "reported",
                            "reason": f"来源 code='{code}' 已存在，冲突策略为 report，不执行导入",
                        })
                if report_conflicts:
                    results["success"] = False
                    results["error_count"] = len(report_conflicts)
                    results["conflict_count"] = len(report_conflicts)
                    results["conflict_rules"] = report_conflicts
                    results["errors"] = [c["reason"] for c in report_conflicts]
                    results["result_summary"] = f"导入失败: 发现{len(report_conflicts)}个冲突 (report策略，不做任何修改)"

                    if not dry_run and import_job_pk:
                        conn_inner.execute(
                            """UPDATE source_rules_import_jobs SET
                               status = 'failed',
                               error_count = ?,
                               conflict_count = ?,
                               result_summary = ?,
                               updated_at = ?
                               WHERE id = ?""",
                            (len(report_conflicts), len(report_conflicts), results["result_summary"], now, import_job_pk),
                        )
                        for conflict in report_conflicts:
                            code = conflict["code"]
                            expected = existing_db_codes.get(code, {})
                            actual_row = conn_inner.execute(
                                "SELECT * FROM source_rules WHERE code = ?", (code,)
                            ).fetchone()
                            actual = _row_to_dict(actual_row) if actual_row else {}
                            diff = SourceRuleService._compute_diff(expected, actual)
                            expected_hash = SourceRuleService._compute_content_hash(expected)
                            actual_hash = SourceRuleService._compute_content_hash(actual)
                            conflict_content_hash = SourceRuleService._compute_content_hash({"expected": expected, "actual": actual})
                            conn_inner.execute(
                                """INSERT INTO source_rules_import_conflicts
                                   (import_job_id, rule_code, conflict_type, detected_at,
                                    expected_before_json, actual_before_json, diff_json, content_hash, created_at)
                                   VALUES (?, ?, 'pre_import_report', ?, ?, ?, ?, ?, ?)""",
                                (
                                    import_job_pk, code, now,
                                    json.dumps(expected, ensure_ascii=False),
                                    json.dumps(actual, ensure_ascii=False),
                                    json.dumps(diff, ensure_ascii=False),
                                    conflict_content_hash,
                                    now,
                                ),
                            )
                    return results

            for idx, rule_data in enumerate(rules_data):
                rule_identifier = rule_data.get("code", f"index={idx}")

                version = rule_data.get("version", SourceRuleService.RULES_VERSION)
                if version not in SourceRuleService.SUPPORTED_VERSIONS:
                    results["error_count"] += 1
                    results["errors"].append(
                        f"规则 {rule_identifier}: 版本 {version} 不支持，"
                        f"支持的版本: {', '.join(SourceRuleService.SUPPORTED_VERSIONS)}"
                    )
                    results["invalid_rules"].append({
                        "code": rule_identifier,
                        "index": idx,
                        "reason": f"版本 {version} 不支持",
                        "incoming_rule": rule_data,
                    })
                    if not dry_run and import_job_pk:
                        conn_inner.execute(
                            """INSERT INTO source_rules_import_details
                               (import_job_id, rule_code, rule_index, action, status,
                                incoming_rule_json, error_message, created_at)
                               VALUES (?, ?, ?, 'invalid', 'error', ?, ?, ?)""",
                            (
                                import_job_pk, rule_identifier, idx,
                                json.dumps(rule_data, ensure_ascii=False),
                                f"版本 {version} 不支持",
                                now,
                            ),
                        )
                    continue

                is_valid, validation_errors = SourceRuleService.validate_rule_data(rule_data)
                if not is_valid:
                    results["error_count"] += 1
                    results["errors"].append(
                        f"规则 {rule_identifier} 验证失败: {'; '.join(validation_errors)}"
                    )
                    results["invalid_rules"].append({
                        "code": rule_identifier,
                        "index": idx,
                        "reason": f"验证失败: {'; '.join(validation_errors)}",
                        "incoming_rule": rule_data,
                    })
                    if not dry_run and import_job_pk:
                        conn_inner.execute(
                            """INSERT INTO source_rules_import_details
                               (import_job_id, rule_code, rule_index, action, status,
                                incoming_rule_json, error_message, created_at)
                               VALUES (?, ?, ?, 'invalid', 'error', ?, ?, ?)""",
                            (
                                import_job_pk, rule_identifier, idx,
                                json.dumps(rule_data, ensure_ascii=False),
                                f"验证失败: {'; '.join(validation_errors)}",
                                now,
                            ),
                        )
                    continue

                code = rule_data["code"]
                is_enabled_incoming = rule_data.get("is_enabled", True)

                detected_conflict = None
                if check_concurrent_modifications and code in existing_db_codes and not dry_run:
                    current_row = conn_inner.execute(
                        "SELECT * FROM source_rules WHERE code = ?", (code,)
                    ).fetchone()
                    current_dict = _row_to_dict(current_row) if current_row else {}
                    expected_dict = existing_db_codes.get(code, {})
                    conflict = SourceRuleService._check_conflict(expected_dict, current_dict)
                    if conflict:
                        results["conflict_count"] += 1
                        conflict_entry = {
                            "code": code,
                            "index": idx,
                            "incoming_rule": rule_data,
                            "existing_rule": expected_dict,
                            "actual_rule": current_dict,
                            "diff": conflict,
                            "action": "concurrent_modification_conflict",
                            "reason": f"规则 {code} 在导入开始后被人工修改，存在并发冲突",
                        }
                        results["conflict_rules"].append(conflict_entry)

                        if import_job_pk:
                            conflict_content_hash = SourceRuleService._compute_content_hash({
                                "expected": expected_dict, "actual": current_dict
                            })
                            logger.info(json.dumps({
                                "event": "source_rule_import_concurrent_conflict",
                                "job_id": job_id,
                                "rule_code": code,
                                "conflict_type": "concurrent_modification",
                                "content_hash": conflict_content_hash,
                                "expected_hash": SourceRuleService._compute_content_hash(expected_dict),
                                "actual_hash": SourceRuleService._compute_content_hash(current_dict),
                                "diff_fields": list(conflict.keys()) if conflict else [],
                                "conflict_strategy": conflict_strategy,
                                "operator": operator,
                            }, ensure_ascii=False))
                            conn_inner.execute(
                                """INSERT INTO source_rules_import_conflicts
                                   (import_job_id, rule_code, conflict_type, detected_at,
                                    expected_before_json, actual_before_json, diff_json,
                                    content_hash, resolver, resolved_at, resolution, created_at)
                                   VALUES (?, ?, 'concurrent_modification', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    import_job_pk, code, now,
                                    json.dumps(expected_dict, ensure_ascii=False),
                                    json.dumps(current_dict, ensure_ascii=False),
                                    json.dumps(conflict, ensure_ascii=False),
                                    conflict_content_hash,
                                    operator, now,
                                    f"按{conflict_strategy}策略继续执行导入",
                                    now,
                                ),
                            )
                        detected_conflict = conflict

                conflict_with_merged = None
                if code in merged_codes:
                    existing_rule = merged_codes[code]
                    conflict_with_merged = existing_rule.to_dict()

                conflict_with_db = code in existing_db_codes
                existing_db_rule = existing_db_codes.get(code)

                disabled_blocked = False
                if conflict_with_db and existing_db_rule:
                    if existing_db_rule["is_enabled"] and not is_enabled_incoming:
                        disabled_blocked = True
                        block_reason = (
                            f"规则 {code} 当前已启用，导入后将被禁用，"
                            f"这将阻止使用该来源的新补录请求"
                        )
                        block_entry = {
                            "code": code,
                            "index": idx,
                            "reason": block_reason,
                            "incoming_rule": rule_data,
                            "existing_rule": existing_db_rule,
                            "impact": "导入后该来源将被禁用，新的补录请求会被拒绝，历史记录不受影响",
                        }
                        results["disabled_blocked_rules"].append(block_entry)
                        results["disabled_blocked_count"] += 1
                        logger.warning(json.dumps({
                            "event": "source_rule_import_disabled_blocked",
                            "code": code,
                            "reason": block_reason,
                            "operator": operator,
                            "job_id": job_id,
                        }, ensure_ascii=False))

                detail_entry = {
                    "code": code,
                    "index": idx,
                    "strategy": conflict_strategy,
                    "incoming_rule": rule_data,
                    "existing_rule": conflict_with_merged or existing_db_rule,
                    "disabled_blocked": disabled_blocked,
                }

                if conflict_with_merged or conflict_with_db:
                    results["conflict_rules"].append(detail_entry)

                    if conflict_strategy == "skip":
                        results["skipped_count"] += 1
                        detail_entry["action"] = "skipped"
                        detail_entry["reason"] = (
                            f"来源 code='{code}' 已存在"
                            f" (层级: {conflict_with_merged['source_layer'] if conflict_with_merged else 'runtime'}"
                            f", 名称: {conflict_with_merged['name'] if conflict_with_merged else code})"
                            f"，跳过导入"
                        )
                        results["skipped_rules"].append(detail_entry)
                        if not dry_run and import_job_pk:
                            conn_inner.execute(
                                """INSERT INTO source_rules_import_details
                                   (import_job_id, rule_code, rule_index, action, status,
                                    incoming_rule_json, before_rule_json, disabled_blocked, created_at)
                                   VALUES (?, ?, ?, 'skip', 'skipped', ?, ?, ?, ?)""",
                                (
                                    import_job_pk, code, idx,
                                    json.dumps(rule_data, ensure_ascii=False),
                                    json.dumps(existing_db_rule, ensure_ascii=False) if existing_db_rule else None,
                                    1 if disabled_blocked else 0,
                                    now,
                                ),
                            )
                        logger.info(json.dumps({
                            "event": "source_rule_import_skip",
                            "code": code,
                            "existing_layer": conflict_with_merged["source_layer"] if conflict_with_merged else "runtime",
                            "operator": operator,
                            "dry_run": dry_run,
                            "job_id": job_id,
                        }, ensure_ascii=False))
                        continue

                    elif conflict_strategy == "overwrite":
                        if not conflict_with_db:
                            results["skipped_count"] += 1
                            detail_entry["action"] = "skipped_not_runtime"
                            detail_entry["reason"] = (
                                f"来源 code='{code}' 存在于 {conflict_with_merged['source_layer']} 层"
                                f" 但不在 runtime 层，无法覆盖，跳过"
                            )
                            results["skipped_rules"].append(detail_entry)
                            results["errors"].append(
                                f"规则 {code} 存在于 {conflict_with_merged['source_layer']} 层"
                                f" 但不在 runtime 数据库中，无法覆盖"
                            )
                            if not dry_run and import_job_pk:
                                conn_inner.execute(
                                    """INSERT INTO source_rules_import_details
                                       (import_job_id, rule_code, rule_index, action, status,
                                        incoming_rule_json, error_message, created_at)
                                       VALUES (?, ?, ?, 'skip_non_runtime', 'skipped', ?, ?, ?)""",
                                    (
                                        import_job_pk, code, idx,
                                        json.dumps(rule_data, ensure_ascii=False),
                                        f"存在于 {conflict_with_merged['source_layer']} 层但不在 runtime 数据库中，无法覆盖",
                                        now,
                                    ),
                                )
                            logger.warning(json.dumps({
                                "event": "source_rule_import_skip_not_runtime",
                                "code": code,
                                "existing_layer": conflict_with_merged["source_layer"],
                                "operator": operator,
                                "dry_run": dry_run,
                                "job_id": job_id,
                            }, ensure_ascii=False))
                            continue

                        if dry_run:
                            detail_entry["action"] = "would_overwrite"
                            detail_entry["reason"] = "dry_run模式，仅预览不会实际覆盖"
                            detail_entry["before"] = existing_db_rule
                            detail_entry["after"] = {**existing_db_rule, **rule_data}
                            detail_entry["diff"] = SourceRuleService._compute_diff(existing_db_rule, {**existing_db_rule, **rule_data})
                            results["overwritten_rules"].append(detail_entry)
                            results["overwritten_count"] += 1
                            results["success_count"] += 1
                            continue

                        try:
                            before_data = existing_db_rule
                            updated = SourceRuleService.update_rule(
                                code=code,
                                name=rule_data.get("name"),
                                description=rule_data.get("description"),
                                category=rule_data.get("category"),
                                priority=rule_data.get("priority", 0),
                                is_enabled=is_enabled_incoming,
                                match_pattern=rule_data.get("match_pattern"),
                                operator=operator,
                                is_import_operation=True,
                                import_job_id=import_job_pk,
                                conn=conn_inner,
                            )
                            after_data = updated.to_dict()
                            diff = SourceRuleService._compute_diff(before_data, after_data)

                            results["success_count"] += 1
                            results["overwritten_count"] += 1
                            detail_entry["action"] = "overwritten"
                            detail_entry["reason"] = (
                                f"已存在(层级: {conflict_with_merged['source_layer'] if conflict_with_merged else 'runtime'})，"
                                f"覆盖更新"
                            )
                            detail_entry["before"] = before_data
                            detail_entry["after"] = after_data
                            detail_entry["diff"] = diff
                            results["overwritten_rules"].append(detail_entry)
                            results["imported_rules"].append(after_data)
                            existing_db_codes[code] = after_data

                            if import_job_pk:
                                conn_inner.execute(
                                    """INSERT INTO source_rules_import_details
                                       (import_job_id, rule_code, rule_index, action, status,
                                        incoming_rule_json, before_rule_json, after_rule_json,
                                        diff_json, disabled_blocked, created_at)
                                       VALUES (?, ?, ?, 'overwrite', 'success', ?, ?, ?, ?, ?, ?)""",
                                    (
                                        import_job_pk, code, idx,
                                        json.dumps(rule_data, ensure_ascii=False),
                                        json.dumps(before_data, ensure_ascii=False),
                                        json.dumps(after_data, ensure_ascii=False),
                                        json.dumps(diff, ensure_ascii=False),
                                        1 if disabled_blocked else 0,
                                        now,
                                    ),
                                )

                            logger.info(json.dumps({
                                "event": "source_rule_import_overwrite",
                                "code": code,
                                "before": before_data,
                                "after": after_data,
                                "diff": diff,
                                "operator": operator,
                                "dry_run": dry_run,
                                "job_id": job_id,
                            }, ensure_ascii=False))
                            continue
                        except ValueError as e:
                            results["error_count"] += 1
                            results["errors"].append(
                                f"规则 {code} 更新失败: {str(e)}"
                            )
                            if not dry_run and import_job_pk:
                                conn_inner.execute(
                                    """INSERT INTO source_rules_import_details
                                       (import_job_id, rule_code, rule_index, action, status,
                                        incoming_rule_json, before_rule_json, error_message, created_at)
                                       VALUES (?, ?, ?, 'overwrite', 'error', ?, ?, ?, ?)""",
                                    (
                                        import_job_pk, code, idx,
                                        json.dumps(rule_data, ensure_ascii=False),
                                        json.dumps(existing_db_rule, ensure_ascii=False),
                                        str(e),
                                        now,
                                    ),
                                )
                            continue

                    elif conflict_strategy == "report":
                        results["error_count"] += 1
                        detail_entry["action"] = "reported"
                        detail_entry["reason"] = (
                            f"来源 code='{code}' 已存在"
                            f" (层级: {conflict_with_merged['source_layer'] if conflict_with_merged else 'runtime'}"
                            f", 名称: {conflict_with_merged['name'] if conflict_with_merged else code})"
                            f"，冲突策略为 report，导入失败"
                        )
                        results["conflict_rules"][-1]["action"] = "reported"
                        results["errors"].append(
                            f"规则 {code} 已存在 (层级: {conflict_with_merged['source_layer'] if conflict_with_merged else 'runtime'})，"
                            f"冲突策略为 report，导入失败"
                        )
                        continue

                if dry_run:
                    results["success_count"] += 1
                    results["new_count"] += 1
                    after_data = {**rule_data, "code": code}
                    new_entry = {
                        **detail_entry,
                        "action": "would_create",
                        "reason": "dry_run模式，仅预览不会实际创建",
                        "dry_run": True,
                        "after": after_data,
                        "diff": SourceRuleService._compute_diff({}, after_data),
                    }
                    results["new_rules"].append(new_entry)
                    results["imported_rules"].append(after_data)
                    continue

                try:
                    created = SourceRuleService.create_rule(
                        code=code,
                        name=rule_data["name"],
                        description=rule_data.get("description"),
                        category=rule_data.get("category", "general"),
                        priority=rule_data.get("priority", 0),
                        is_enabled=is_enabled_incoming,
                        match_pattern=rule_data.get("match_pattern"),
                        operator=operator,
                        is_import_operation=True,
                        import_job_id=import_job_pk,
                        conn=conn_inner,
                    )

                    if import_job_pk:
                        created_row = conn_inner.execute(
                            "SELECT * FROM source_rules WHERE code = ?", (code,)
                        ).fetchone()
                        created_dict = _row_to_dict(created_row)
                        created = SourceRule(
                            id=created_dict["id"],
                            code=created_dict["code"],
                            name=created_dict["name"],
                            description=created_dict["description"],
                            category=created_dict.get("category", "general"),
                            priority=created_dict["priority"],
                            is_enabled=bool(created_dict["is_enabled"]),
                            match_pattern=created_dict["match_pattern"],
                            version=created_dict["version"],
                            created_at=created_dict["created_at"],
                            updated_at=created_dict["updated_at"],
                            source_layer="runtime",
                            import_origin=created_dict.get("import_origin"),
                            import_job_id=created_dict.get("import_job_id"),
                            last_manual_modified_at=created_dict.get("last_manual_modified_at"),
                            last_manual_modified_by=created_dict.get("last_manual_modified_by"),
                        )

                    after_data = created.to_dict()
                    diff = SourceRuleService._compute_diff({}, after_data)

                    results["success_count"] += 1
                    results["new_count"] += 1
                    new_entry = {
                        **detail_entry,
                        "action": "created",
                        "reason": "成功创建新规则",
                        "after": after_data,
                        "diff": diff,
                    }
                    results["new_rules"].append(new_entry)
                    results["imported_rules"].append(after_data)
                    existing_db_codes[code] = after_data

                    if import_job_pk:
                        conn_inner.execute(
                            """INSERT INTO source_rules_import_details
                               (import_job_id, rule_code, rule_index, action, status,
                                incoming_rule_json, after_rule_json, diff_json,
                                disabled_blocked, created_at)
                               VALUES (?, ?, ?, 'create', 'success', ?, ?, ?, ?, ?)""",
                            (
                                import_job_pk, code, idx,
                                json.dumps(rule_data, ensure_ascii=False),
                                json.dumps(after_data, ensure_ascii=False),
                                json.dumps(diff, ensure_ascii=False),
                                1 if disabled_blocked else 0,
                                now,
                            ),
                        )

                    logger.info(json.dumps({
                        "event": "source_rule_import_created",
                        "code": code,
                        "rule": after_data,
                        "operator": operator,
                        "dry_run": dry_run,
                        "job_id": job_id,
                    }, ensure_ascii=False))
                except ValueError as e:
                    results["error_count"] += 1
                    results["errors"].append(
                        f"规则 {code} 创建失败: {str(e)}"
                    )
                    if not dry_run and import_job_pk:
                        conn_inner.execute(
                            """INSERT INTO source_rules_import_details
                               (import_job_id, rule_code, rule_index, action, status,
                                incoming_rule_json, error_message, created_at)
                               VALUES (?, ?, ?, 'create', 'error', ?, ?, ?)""",
                            (
                                import_job_pk, code, idx,
                                json.dumps(rule_data, ensure_ascii=False),
                                str(e),
                                now,
                            ),
                        )

            results["success"] = results["error_count"] == 0

            result_summary = (
                f"导入完成: 成功{results['success_count']}条 "
                f"(新增{results['new_count']}条, 覆盖{results['overwritten_count']}条), "
                f"跳过{results['skipped_count']}条, "
                f"失败{results['error_count']}条, "
                f"冲突{results['conflict_count']}条, "
                f"禁用拦截{results['disabled_blocked_count']}条"
                f"{' (dry_run预览)' if dry_run else ''}"
            )
            results["result_summary"] = result_summary

            if not dry_run and import_job_pk:
                final_status = "completed" if results["success"] else "completed_with_errors"
                conn_inner.execute(
                    """UPDATE source_rules_import_jobs SET
                       status = ?,
                       success_count = ?,
                       skipped_count = ?,
                       error_count = ?,
                       new_count = ?,
                       overwritten_count = ?,
                       conflict_count = ?,
                       result_summary = ?,
                       updated_at = ?
                       WHERE id = ?""",
                    (
                        final_status,
                        results["success_count"],
                        results["skipped_count"],
                        results["error_count"],
                        results["new_count"],
                        results["overwritten_count"],
                        results["conflict_count"],
                        result_summary,
                        now,
                        import_job_pk,
                    ),
                )

                effective_rules = [r["code"] for r in results["new_rules"] + results["overwritten_rules"]]
                details_json = json.dumps({
                    "new_rules": results["new_rules"],
                    "overwritten_rules": results["overwritten_rules"],
                    "skipped_rules": results["skipped_rules"],
                    "conflict_rules": results["conflict_rules"],
                    "disabled_blocked_rules": results["disabled_blocked_rules"],
                    "invalid_rules": results["invalid_rules"],
                    "errors": results["errors"],
                    "effective_rules": effective_rules,
                }, ensure_ascii=False)

                conn_inner.execute(
                    """INSERT INTO source_rules_import_log
                       (import_version, rules_count, success_count, skipped_count, 
                        error_count, new_count, overwritten_count, disabled_blocked_count,
                        conflict_strategy, result_summary, details_json, operator, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        SourceRuleService.RULES_VERSION,
                        results["rules_count"],
                        results["success_count"],
                        results["skipped_count"],
                        results["error_count"],
                        results["new_count"],
                        results["overwritten_count"],
                        results["disabled_blocked_count"],
                        conflict_strategy,
                        result_summary,
                        details_json,
                        operator,
                        now,
                    ),
                )

                import_id_row = conn_inner.execute(
                    "SELECT last_insert_rowid() as id"
                ).fetchone()
                import_id = import_id_row["id"] if import_id_row else None
                results["import_id"] = import_id

                if import_id:
                    effective_codes = [r.get("code") for r in results["new_rules"] + results["overwritten_rules"] if r.get("code")]
                    if effective_codes:
                        placeholders = ",".join("?" for _ in effective_codes)
                        conn_inner.execute(
                            f"UPDATE source_rules_audit_log SET import_id = ? "
                            f"WHERE rule_code IN ({placeholders}) AND import_id IS NULL AND operation IN ('create', 'update')",
                            [import_id] + effective_codes,
                        )

                logger.info(json.dumps({
                    "event": "source_rules_import_completed",
                    "summary": result_summary,
                    "import_id": import_id,
                    "import_job_id": import_job_pk,
                    "job_id": job_id,
                    "success_count": results["success_count"],
                    "new_count": results["new_count"],
                    "overwritten_count": results["overwritten_count"],
                    "skipped_count": results["skipped_count"],
                    "error_count": results["error_count"],
                    "conflict_count": results["conflict_count"],
                    "disabled_blocked_count": results["disabled_blocked_count"],
                    "conflict_strategy": conflict_strategy,
                    "effective_rules": effective_rules,
                    "operator": operator,
                    "dry_run": dry_run,
                }, ensure_ascii=False))

                if results["success_count"] > 0 or results["skipped_count"] > 0:
                    SourceRuleService._invalidate_cache()
                    SourceRuleService.ensure_cache_fresh()

            results["summary"] = {
                "total": results["rules_count"],
                "success": results["success_count"],
                "created": results["new_count"],
                "overwritten": results["overwritten_count"],
                "skipped": results["skipped_count"],
                "failed": results["error_count"],
                "conflicts_detected": results["conflict_count"],
            }

            return results

        if conn is None:
            with transaction() as conn:
                return _execute(conn)
        else:
            return _execute(conn)

    @staticmethod
    def export_rules(only_enabled: bool = True, include_all_layers: bool = False, conn=None) -> Dict[str, Any]:
        if include_all_layers:
            runtime_rules = SourceRuleService.get_runtime_rules(conn)
            default_rules = SourceRuleService.get_default_rules()
            env_rules = SourceRuleService.get_environment_rules()

            if only_enabled:
                runtime_rules = [r for r in runtime_rules if r.is_enabled]
                default_rules = [r for r in default_rules if r.is_enabled]
                env_rules = [r for r in env_rules if r.is_enabled]

            def _rule_export(r: SourceRule) -> Dict[str, Any]:
                return {
                    "code": r.code,
                    "name": r.name,
                    "description": r.description,
                    "category": r.category,
                    "priority": r.priority,
                    "is_enabled": r.is_enabled,
                    "match_pattern": r.match_pattern,
                    "version": r.version,
                    "source_layer": r.source_layer,
                    "created_at": r.created_at,
                    "updated_at": r.updated_at,
                }

            export_data = {
                "version": SourceRuleService.RULES_VERSION,
                "exported_at": now_str(),
                "include_all_layers": True,
                "layers": {
                    "default": [_rule_export(r) for r in default_rules],
                    "environment": [_rule_export(r) for r in env_rules],
                    "runtime": [_rule_export(r) for r in runtime_rules],
                },
                "count": len(runtime_rules) + len(default_rules) + len(env_rules),
                "rules": [_rule_export(r) for r in runtime_rules],
            }

            logger.info(json.dumps({
                "event": "source_rules_exported_all_layers",
                "default_count": len(default_rules),
                "environment_count": len(env_rules),
                "runtime_count": len(runtime_rules),
            }, ensure_ascii=False))
            return export_data

        rules = SourceRuleService.get_runtime_rules(conn)

        if only_enabled:
            rules = [r for r in rules if r.is_enabled]

        export_data = {
            "version": SourceRuleService.RULES_VERSION,
            "exported_at": now_str(),
            "include_all_layers": False,
            "only_enabled": only_enabled,
            "count": len(rules),
            "rules": [
                {
                    "code": r.code,
                    "name": r.name,
                    "description": r.description,
                    "category": r.category,
                    "priority": r.priority,
                    "is_enabled": r.is_enabled,
                    "match_pattern": r.match_pattern,
                    "version": r.version,
                    "created_at": r.created_at,
                    "updated_at": r.updated_at,
                }
                for r in rules
            ],
        }

        logger.info(json.dumps({
            "event": "source_rules_exported",
            "count": len(rules),
            "only_enabled": only_enabled,
        }, ensure_ascii=False))
        return export_data

    @staticmethod
    def export_rules_csv(only_enabled: bool = True, conn=None) -> str:
        rules = SourceRuleService.get_runtime_rules(conn)

        if only_enabled:
            rules = [r for r in rules if r.is_enabled]

        headers = [
            "code", "name", "description", "category", "priority",
            "is_enabled", "match_pattern", "version", "created_at", "updated_at"
        ]
        header_line = ",".join(headers)

        lines = [header_line]
        for r in rules:
            row = [
                f'"{r.code}"',
                f'"{r.name}"',
                f'"{r.description or ""}"',
                f'"{r.category}"',
                str(r.priority),
                str(r.is_enabled).lower(),
                f'"{r.match_pattern or ""}"',
                f'"{r.version}"',
                f'"{r.created_at or ""}"',
                f'"{r.updated_at or ""}"',
            ]
            lines.append(",".join(row))

        csv_content = "\n".join(lines)

        logger.info(json.dumps({
            "event": "source_rules_exported_csv",
            "count": len(rules),
            "only_enabled": only_enabled,
        }, ensure_ascii=False))

        return csv_content

    @staticmethod
    def get_import_history(limit: int = 20, import_id: int = None, operator: str = None, conn=None) -> List[Dict[str, Any]]:
        if conn is None:
            conn = get_db()

        if import_id:
            rows = conn.execute(
                "SELECT * FROM source_rules_import_log WHERE id = ? ORDER BY id DESC",
                (import_id,),
            ).fetchall()
        elif operator:
            rows = conn.execute(
                "SELECT * FROM source_rules_import_log WHERE operator = ? ORDER BY id DESC LIMIT ?",
                (operator, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM source_rules_import_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()

        history = []
        for row in rows:
            entry = dict(row)
            try:
                entry["details"] = json.loads(entry["details_json"]) if entry["details_json"] else None
            except json.JSONDecodeError:
                entry["details"] = None
            entry.pop("details_json", None)

            if entry.get("details"):
                details = entry["details"]
                summary = {
                    "new_count": entry.get("new_count", 0),
                    "overwritten_count": entry.get("overwritten_count", 0),
                    "skipped_count": entry.get("skipped_count", 0),
                    "error_count": entry.get("error_count", 0),
                    "disabled_blocked_count": entry.get("disabled_blocked_count", 0),
                    "effective_rules": details.get("effective_rules", []),
                    "new_rules": [
                        {"code": r.get("code"), "name": r.get("incoming_rule", {}).get("name")}
                        for r in details.get("new_rules", [])
                    ],
                    "overwritten_rules": [
                        {"code": r.get("code"), "name": r.get("incoming_rule", {}).get("name")}
                        for r in details.get("overwritten_rules", [])
                    ],
                }
                entry["summary"] = summary

            history.append(entry)

        logger.info(json.dumps({
            "event": "source_rules_import_history_query",
            "count": len(history),
            "limit": limit,
            "import_id": import_id,
        }, ensure_ascii=False))

        return history

    # ================================================
    # 导入回放中心 - 权限控制
    # ================================================

    @staticmethod
    def _get_permission_type_name(permission_type: str) -> str:
        permission_map = {
            "import_audit_view": "导入审计查看",
            "import_audit_export": "导入审计导出",
            "import_revoke": "导入撤销",
            "import_manage": "导入管理",
        }
        return permission_map.get(permission_type, permission_type)

    @staticmethod
    def check_import_audit_permission(user_id: str, permission_type: str = "import_audit_view", conn=None) -> bool:
        if conn is None:
            conn = get_db()
        try:
            from services.config_service import ConfigService
            default_admin = ConfigService.get_config("import_audit_default_admin", conn)
            if default_admin and user_id == default_admin:
                return True
        except Exception:
            pass
        if not user_id:
            logger.warning(json.dumps({
                "event": "import_audit_permission_denied",
                "reason": "user_id_empty",
                "user_id": user_id,
                "permission_type": permission_type,
            }, ensure_ascii=False))
            return False

        row = conn.execute(
            """SELECT * FROM source_rules_import_permissions 
               WHERE user_id = ? AND permission_type = ? AND is_active = 1
               ORDER BY granted_at DESC LIMIT 1""",
            (user_id, permission_type),
        ).fetchone()

        if not row:
            logger.warning(json.dumps({
                "event": "import_audit_permission_denied",
                "reason": "no_permission_record",
                "user_id": user_id,
                "permission_type": permission_type,
            }, ensure_ascii=False))
            return False

        perm = dict(row)
        if perm.get("expires_at"):
            try:
                from datetime import datetime
                exp_time = datetime.strptime(perm["expires_at"], "%Y-%m-%d %H:%M:%S")
                now = datetime.now()
                if now > exp_time:
                    logger.warning(json.dumps({
                        "event": "import_audit_permission_denied",
                        "reason": "permission_expired",
                        "user_id": user_id,
                        "permission_type": permission_type,
                        "expires_at": perm["expires_at"],
                    }, ensure_ascii=False))
                    return False
            except Exception:
                pass

        logger.info(json.dumps({
            "event": "import_audit_permission_granted",
            "user_id": user_id,
            "permission_type": permission_type,
            "granted_by": perm.get("granted_by"),
        }, ensure_ascii=False))
        return True

    @staticmethod
    def _mask_sensitive_data(data: Dict[str, Any]) -> Dict[str, Any]:
        masked = dict(data)
        sensitive_patterns = ["remark", "description", "match_pattern"]
        for key in masked:
            if isinstance(key, str) and any(p in key.lower() for p in sensitive_patterns):
                if masked[key] and isinstance(masked[key], str) and len(masked[key]) > 10:
                    masked[key] = masked[key][:5] + "***" + masked[key][-3:] if len(masked[key]) > 8 else "***"
        return masked

    # ================================================
    # 导入回放中心 - 作业查询
    # ================================================

    @staticmethod
    def list_import_jobs(
        user_id: str = None,
        status: str = None,
        operator: str = None,
        is_revoked: bool = None,
        start_time: str = None,
        end_time: str = None,
        page: int = 1,
        page_size: int = 20,
        conn=None,
    ) -> Dict[str, Any]:
        if conn is None:
            conn = get_db()

        has_audit_permission = SourceRuleService.check_import_audit_permission(
            user_id, "import_audit_view", conn
        ) if user_id else False

        conditions = []
        params = []

        if status:
            conditions.append("status = ?")
            params.append(status)
        if operator:
            conditions.append("operator = ?")
            params.append(operator)
        if is_revoked is not None:
            conditions.append("is_revoked = ?")
            params.append(1 if is_revoked else 0)
        if start_time:
            conditions.append("created_at >= ?")
            params.append(start_time)
        if end_time:
            conditions.append("created_at <= ?")
            params.append(end_time)

        where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

        count_row = conn.execute(
            f"SELECT COUNT(*) as total FROM source_rules_import_jobs{where_clause}",
            params,
        ).fetchone()
        total = count_row["total"] if count_row else 0

        offset = (page - 1) * page_size
        rows = conn.execute(
            f"""SELECT * FROM source_rules_import_jobs{where_clause}
               ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            params + [page_size, offset],
        ).fetchall()

        jobs = []
        for row in rows:
            job = dict(row)
            job["is_revoked"] = bool(job["is_revoked"])

            if not has_audit_permission:
                job = SourceRuleService._mask_sensitive_data(job)
                if "result_summary" in job:
                    job["result_summary"] = "*** 需导入审计权限查看 ***"

            jobs.append(job)

        logger.info(json.dumps({
            "event": "import_jobs_list_query",
            "count": len(jobs),
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_audit_permission": has_audit_permission,
            "user_id": user_id,
        }, ensure_ascii=False))

        return {
            "success": True,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_audit_permission": has_audit_permission,
            "jobs": jobs,
        }

    @staticmethod
    def get_import_job(
        job_id: str,
        user_id: str = None,
        conn=None,
    ) -> Dict[str, Any]:
        if conn is None:
            conn = get_db()

        has_audit_permission = SourceRuleService.check_import_audit_permission(
            user_id, "import_audit_view", conn
        ) if user_id else False

        row = conn.execute(
            "SELECT * FROM source_rules_import_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()

        if not row:
            raise ValueError(f"导入作业 {job_id} 不存在")

        job = dict(row)
        job["is_revoked"] = bool(job["is_revoked"])

        if not has_audit_permission:
            job = SourceRuleService._mask_sensitive_data(job)
            job["result_summary"] = "*** 需导入审计权限查看详细信息 ***"

        logger.info(json.dumps({
            "event": "import_job_detail_query",
            "job_id": job_id,
            "has_audit_permission": has_audit_permission,
            "user_id": user_id,
        }, ensure_ascii=False))

        return {
            "success": True,
            "has_audit_permission": has_audit_permission,
            "job_id": job.get("job_id"),
            "operator": job.get("operator"),
            "conflict_strategy": job.get("conflict_strategy"),
            "is_revoked": job.get("is_revoked"),
            "revoked_at": job.get("revoked_at"),
            "revoked_by": job.get("revoked_by"),
            "revoked_reason": job.get("revoked_reason"),
            "status": job.get("status"),
            "rules_count": job.get("rules_count"),
            "success_count": job.get("success_count"),
            "overwritten_count": job.get("overwritten_count"),
            "new_count": job.get("new_count"),
            "skipped_count": job.get("skipped_count"),
            "error_count": job.get("error_count"),
            "conflict_count": job.get("conflict_count"),
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
            "result_summary": job.get("result_summary"),
            "job": job,
        }

    # ================================================
    # 导入回放中心 - 明细与差异
    # ================================================

    @staticmethod
    def get_import_job_details(
        job_id: str,
        user_id: str = None,
        status_filter: str = None,
        page: int = 1,
        page_size: int = 50,
        conn=None,
    ) -> Dict[str, Any]:
        if conn is None:
            conn = get_db()

        has_audit_permission = SourceRuleService.check_import_audit_permission(
            user_id, "import_audit_view", conn
        ) if user_id else False

        job_row = conn.execute(
            "SELECT id FROM source_rules_import_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if not job_row:
            raise ValueError(f"导入作业 {job_id} 不存在")

        import_job_pk = job_row["id"]

        conditions = ["import_job_id = ?"]
        params = [import_job_pk]

        if status_filter:
            conditions.append("status = ?")
            params.append(status_filter)

        where_clause = " AND ".join(conditions)

        count_row = conn.execute(
            f"SELECT COUNT(*) as total FROM source_rules_import_details WHERE {where_clause}",
            params,
        ).fetchone()
        total = count_row["total"] if count_row else 0

        offset = (page - 1) * page_size
        rows = conn.execute(
            f"""SELECT * FROM source_rules_import_details 
               WHERE {where_clause}
               ORDER BY rule_index ASC LIMIT ? OFFSET ?""",
            params + [page_size, offset],
        ).fetchall()

        details = []
        for row in rows:
            detail = dict(row)

            field_mapping = {
                "incoming_rule_json": "incoming",
                "before_rule_json": "before",
                "after_rule_json": "after",
                "diff_json": "diff",
            }
            for json_field, key in field_mapping.items():
                if detail.get(json_field):
                    try:
                        detail[key] = json.loads(detail[json_field])
                        if not has_audit_permission:
                            detail[key] = SourceRuleService._mask_sensitive_data(detail[key])
                    except json.JSONDecodeError:
                        detail[key] = None
                else:
                    detail[key] = None
                detail.pop(json_field, None)

            detail["disabled_blocked"] = bool(detail["disabled_blocked"])

            if not has_audit_permission and "error_message" in detail:
                detail["error_message"] = "*** 需导入审计权限查看 ***"

            details.append(detail)

        logger.info(json.dumps({
            "event": "import_job_details_query",
            "job_id": job_id,
            "count": len(details),
            "total": total,
            "has_audit_permission": has_audit_permission,
            "user_id": user_id,
        }, ensure_ascii=False))

        return {
            "success": True,
            "job_id": job_id,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_audit_permission": has_audit_permission,
            "details": details,
        }

    @staticmethod
    def get_import_job_snapshots(
        job_id: str,
        user_id: str = None,
        snapshot_type: str = "before_import",
        conn=None,
    ) -> Dict[str, Any]:
        if conn is None:
            conn = get_db()

        has_audit_permission = SourceRuleService.check_import_audit_permission(
            user_id, "import_audit_view", conn
        ) if user_id else False

        job_row = conn.execute(
            "SELECT id FROM source_rules_import_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if not job_row:
            raise ValueError(f"导入作业 {job_id} 不存在")

        import_job_pk = job_row["id"]

        rows = conn.execute(
            """SELECT * FROM source_rules_import_snapshots 
               WHERE import_job_id = ? AND snapshot_type = ?
               ORDER BY rule_code ASC""",
            (import_job_pk, snapshot_type),
        ).fetchall()

        snapshots = []
        for row in rows:
            snap = dict(row)
            try:
                snap["rule_json"] = json.loads(snap["rule_json"]) if snap["rule_json"] else None
                if snap["rule_json"] and not has_audit_permission:
                    snap["rule_json"] = SourceRuleService._mask_sensitive_data(snap["rule_json"])
            except json.JSONDecodeError:
                snap["rule_json"] = None
            snapshots.append(snap)

        logger.info(json.dumps({
            "event": "import_job_snapshots_query",
            "job_id": job_id,
            "snapshot_type": snapshot_type,
            "count": len(snapshots),
            "has_audit_permission": has_audit_permission,
            "user_id": user_id,
        }, ensure_ascii=False))

        return {
            "success": True,
            "job_id": job_id,
            "snapshot_type": snapshot_type,
            "has_audit_permission": has_audit_permission,
            "snapshots": snapshots,
        }

    @staticmethod
    def get_import_job_conflicts(
        job_id: str,
        user_id: str = None,
        include_resolved: bool = True,
        conn=None,
    ) -> Dict[str, Any]:
        if conn is None:
            conn = get_db()

        has_audit_permission = SourceRuleService.check_import_audit_permission(
            user_id, "import_audit_view", conn
        ) if user_id else False

        job_row = conn.execute(
            "SELECT id FROM source_rules_import_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if not job_row:
            raise ValueError(f"导入作业 {job_id} 不存在")

        import_job_pk = job_row["id"]

        conditions = ["import_job_id = ?"]
        params = [import_job_pk]

        if not include_resolved:
            conditions.append("resolved_at IS NULL")

        where_clause = " AND ".join(conditions)

        rows = conn.execute(
            f"""SELECT * FROM source_rules_import_conflicts 
               WHERE {where_clause}
               ORDER BY detected_at DESC""",
            params,
        ).fetchall()

        conflicts = []
        for row in rows:
            conflict = dict(row)

            for json_field in ["expected_before_json", "actual_before_json", "diff_json"]:
                if conflict.get(json_field):
                    try:
                        key = json_field.replace("_json", "")
                        conflict[key] = json.loads(conflict[json_field])
                        if conflict[key] and not has_audit_permission:
                            conflict[key] = SourceRuleService._mask_sensitive_data(conflict[key])
                    except json.JSONDecodeError:
                        key = json_field.replace("_json", "")
                        conflict[key] = None
                conflict.pop(json_field, None)

            conflict["is_resolved"] = 1 if conflict.get("resolved_at") else 0

            if conflict.get("diff") and isinstance(conflict["diff"], dict):
                detected_changes = []
                for field, values in conflict["diff"].items():
                    if isinstance(values, dict):
                        detected_changes.append({
                            "field": field,
                            "expected": values.get("expected"),
                            "actual": values.get("actual"),
                        })
                conflict["detected_changes"] = detected_changes
            else:
                conflict["detected_changes"] = []

            if not has_audit_permission:
                if "resolution" in conflict:
                    conflict["resolution"] = "*** 需导入审计权限查看 ***"

            conflicts.append(conflict)

        logger.info(json.dumps({
            "event": "import_job_conflicts_query",
            "job_id": job_id,
            "count": len(conflicts),
            "has_audit_permission": has_audit_permission,
            "user_id": user_id,
        }, ensure_ascii=False))

        return {
            "success": True,
            "job_id": job_id,
            "has_audit_permission": has_audit_permission,
            "conflicts": conflicts,
        }

    # ================================================
    # 导入回放中心 - 撤销功能
    # ================================================

    @staticmethod
    def revoke_import_job(
        job_id: str,
        operator: str = None,
        user_id: str = None,
        reason: str = None,
        conn=None,
    ) -> Dict[str, Any]:
        if conn is None:
            conn = get_db()

        has_revoke_permission = SourceRuleService.check_import_audit_permission(
            user_id, "import_revoke", conn
        ) if user_id else False

        if not has_revoke_permission:
            return {
                "success": False,
                "has_revoke_permission": False,
                "message": "没有撤销导入的权限，请联系管理员授权",
            }

        def _execute(conn_inner):
            now = now_str()

            job_row = conn_inner.execute(
                "SELECT * FROM source_rules_import_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()

            if not job_row:
                raise ValueError(f"导入作业 {job_id} 不存在")

            if job_row["is_revoked"]:
                raise ValueError(f"导入作业 {job_id} 已被撤销")

            import_job_pk = job_row["id"]

            snapshot_rows = conn_inner.execute(
                """SELECT * FROM source_rules_import_snapshots 
                   WHERE import_job_id = ? AND snapshot_type = 'before_import'""",
                (import_job_pk,),
            ).fetchall()

            snapshot_rules = {}
            for snap in snapshot_rows:
                try:
                    rule_data = json.loads(snap["rule_json"])
                    snapshot_rules[snap["rule_code"]] = rule_data
                except json.JSONDecodeError:
                    continue

            detail_rows = conn_inner.execute(
                """SELECT * FROM source_rules_import_details 
                   WHERE import_job_id = ? AND status = 'success'
                   ORDER BY rule_index ASC""",
                (import_job_pk,),
            ).fetchall()

            revoke_results = {
                "restored": [],
                "deleted": [],
                "skipped": [],
                "errors": [],
            }

            for detail in detail_rows:
                rule_code = detail["rule_code"]
                action = detail["action"]

                try:
                    if action == "create":
                        conn_inner.execute(
                            "DELETE FROM source_rules WHERE code = ?",
                            (rule_code,),
                        )
                        revoke_results["deleted"].append({
                            "code": rule_code,
                            "action": "deleted",
                            "reason": "撤销导入时删除导入创建的规则",
                        })

                        SourceRuleService._write_audit_log(
                            rule_code=rule_code,
                            operation="delete",
                            before_data=json.loads(detail["after_rule_json"]) if detail["after_rule_json"] else None,
                            operator=operator,
                            remark=f"撤销导入作业 {job_id}",
                            conn=conn_inner,
                        )

                        after_data_for_lineage = json.loads(detail["after_rule_json"]) if detail["after_rule_json"] else {}
                        parent_lid = SourceRuleService._get_last_lineage_id(rule_code, conn_inner)
                        SourceRuleService._write_lineage(
                            rule_code=rule_code,
                            source_type="revoke_delete",
                            snapshot_data=after_data_for_lineage,
                            operator=operator,
                            import_job_id=import_job_pk,
                            parent_lineage_id=parent_lid,
                            remark=f"撤销导入作业 {job_id} 删除规则",
                            conn=conn_inner,
                        )

                    elif action == "overwrite":
                        before_data = snapshot_rules.get(rule_code)
                        if before_data:
                            update_fields = {}
                            for field in ["name", "description", "category", "priority", "is_enabled", "match_pattern", "version"]:
                                if field in before_data:
                                    val = before_data[field]
                                    if field == "is_enabled":
                                        update_fields[field] = 1 if val else 0
                                    else:
                                        update_fields[field] = val

                            update_fields["updated_at"] = now
                            update_fields["import_origin"] = before_data.get("import_origin", "manual")
                            update_fields["import_job_id"] = before_data.get("import_job_id")
                            update_fields["last_manual_modified_at"] = before_data.get("last_manual_modified_at")
                            update_fields["last_manual_modified_by"] = before_data.get("last_manual_modified_by")

                            set_clause = ", ".join(f"{k} = ?" for k in update_fields.keys())
                            values = list(update_fields.values()) + [rule_code]

                            conn_inner.execute(
                                f"UPDATE source_rules SET {set_clause} WHERE code = ?",
                                values,
                            )

                            revoke_results["restored"].append({
                                "code": rule_code,
                                "action": "restored",
                                "reason": "撤销导入时恢复导入覆盖前的规则",
                            })

                            SourceRuleService._write_audit_log(
                                rule_code=rule_code,
                                operation="update",
                                before_data=json.loads(detail["after_rule_json"]) if detail["after_rule_json"] else None,
                                after_data=before_data,
                                operator=operator,
                                remark=f"撤销导入作业 {job_id}",
                                conn=conn_inner,
                            )

                            parent_lid = SourceRuleService._get_last_lineage_id(rule_code, conn_inner)
                            SourceRuleService._write_lineage(
                                rule_code=rule_code,
                                source_type="revoke_restore",
                                snapshot_data=before_data,
                                operator=operator,
                                import_job_id=import_job_pk,
                                parent_lineage_id=parent_lid,
                                remark=f"撤销导入作业 {job_id} 恢复规则",
                                conn=conn_inner,
                            )
                        else:
                            revoke_results["skipped"].append({
                                "code": rule_code,
                                "action": "skipped",
                                "reason": "未找到导入前的快照数据，无法恢复",
                            })

                    else:
                        revoke_results["skipped"].append({
                            "code": rule_code,
                            "action": "skipped",
                            "reason": f"不支持撤销操作类型: {action}",
                        })

                except Exception as e:
                    revoke_results["errors"].append({
                        "code": rule_code,
                        "error": str(e),
                    })

            after_snapshots = {}
            current_rows = conn_inner.execute(
                "SELECT * FROM source_rules"
            ).fetchall()
            for r in current_rows:
                d = dict(r)
                d["is_enabled"] = bool(d["is_enabled"])
                if "category" not in d:
                    d["category"] = "general"
                after_snapshots[d["code"]] = d
                conn_inner.execute(
                    """INSERT INTO source_rules_import_snapshots
                       (import_job_id, rule_code, rule_json, snapshot_type, created_at)
                       VALUES (?, ?, ?, 'after_revoke', ?)""",
                    (import_job_pk, d["code"], json.dumps(d, ensure_ascii=False), now),
                )

            conn_inner.execute(
                """UPDATE source_rules_import_jobs SET
                   is_revoked = 1,
                   revoked_at = ?,
                   revoked_by = ?,
                   revoked_reason = ?,
                   updated_at = ?
                   WHERE id = ?""",
                (now, operator, reason, now, import_job_pk),
            )

            SourceRuleService._invalidate_cache()
            SourceRuleService.ensure_cache_fresh()

            result_summary = (
                f"撤销完成: 恢复{len(revoke_results['restored'])}条, "
                f"删除{len(revoke_results['deleted'])}条, "
                f"跳过{len(revoke_results['skipped'])}条, "
                f"失败{len(revoke_results['errors'])}条"
            )

            logger.info(json.dumps({
                "event": "import_job_revoked",
                "job_id": job_id,
                "import_job_pk": import_job_pk,
                "operator": operator,
                "user_id": user_id,
                "reason": reason,
                "result_summary": result_summary,
                "revoke_results": revoke_results,
            }, ensure_ascii=False))

            return {
                "success": True,
                "has_revoke_permission": True,
                "job_id": job_id,
                "revoked_at": now,
                "revoked_by": operator,
                "reason": reason,
                "result_summary": result_summary,
                "revoke_results": revoke_results,
            }

        try:
            if conn is None:
                with transaction() as conn:
                    return _execute(conn)
            else:
                return _execute(conn)
        except ValueError as e:
            return {
                "success": False,
                "has_revoke_permission": True,
                "message": str(e),
            }

    @staticmethod
    def get_import_replay_data(
        job_id: str,
        user_id: str = None,
        conn=None,
    ) -> Dict[str, Any]:
        if conn is None:
            conn = get_db()

        has_audit_permission = SourceRuleService.check_import_audit_permission(
            user_id, "import_audit_view", conn
        ) if user_id else False

        job_row = conn.execute(
            "SELECT * FROM source_rules_import_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if not job_row:
            raise ValueError(f"导入作业 {job_id} 不存在")

        if not job_row["is_revoked"]:
            return {
                "success": True,
                "job_id": job_id,
                "is_revoked": False,
                "message": "该作业尚未撤销，无可回放数据",
            }

        import_job_pk = job_row["id"]

        before_rows = conn.execute(
            """SELECT * FROM source_rules_import_snapshots 
               WHERE import_job_id = ? AND snapshot_type = 'before_import'""",
            (import_job_pk,),
        ).fetchall()

        after_revoke_rows = conn.execute(
            """SELECT * FROM source_rules_import_snapshots 
               WHERE import_job_id = ? AND snapshot_type = 'after_revoke'""",
            (import_job_pk,),
        ).fetchall()

        detail_rows = conn.execute(
            """SELECT * FROM source_rules_import_details 
               WHERE import_job_id = ? AND status = 'success'
               ORDER BY rule_index ASC""",
            (import_job_pk,),
        ).fetchall()

        def _parse_snapshot(rows):
            result = {}
            for r in rows:
                try:
                    rule_data = json.loads(r["rule_json"])
                    if not has_audit_permission:
                        rule_data = SourceRuleService._mask_sensitive_data(rule_data)
                    result[r["rule_code"]] = rule_data
                except json.JSONDecodeError:
                    continue
            return result

        def _parse_details(rows):
            result = []
            for r in rows:
                d = dict(r)
                field_mapping = {
                    "incoming_rule_json": "incoming",
                    "before_rule_json": "before",
                    "after_rule_json": "after",
                    "diff_json": "diff",
                }
                for jf, key in field_mapping.items():
                    if d.get(jf):
                        try:
                            d[key] = json.loads(d[jf])
                            if not has_audit_permission:
                                d[key] = SourceRuleService._mask_sensitive_data(d[key])
                        except json.JSONDecodeError:
                            d[key] = None
                    else:
                        d[key] = None
                    d.pop(jf, None)
                d["disabled_blocked"] = bool(d["disabled_blocked"])
                result.append(d)
            return result

        before_snapshot = _parse_snapshot(before_rows)
        after_revoke_snapshot = _parse_snapshot(after_revoke_rows)
        details = _parse_details(detail_rows)

        replay_steps = []
        all_verified = True
        for detail in details:
            code = detail["rule_code"]
            step = {
                "code": code,
                "rule_code": code,
                "action": detail["action"],
                "import_action": detail["action"],
                "revoke_action": "restored" if detail["action"] == "overwrite" else "deleted",
                "before_import": before_snapshot.get(code),
                "after_import": detail.get("after"),
                "after_revoke": after_revoke_snapshot.get(code),
                "import_diff": detail.get("diff"),
                "diff": detail.get("diff"),
            }

            if before_snapshot.get(code) and after_revoke_snapshot.get(code):
                before_clean = {k: v for k, v in before_snapshot[code].items() if k != "updated_at"}
                after_clean = {k: v for k, v in after_revoke_snapshot[code].items() if k != "updated_at"}
                step["verify_result"] = "matched" if before_clean == after_clean else "mismatched"
                if step["verify_result"] == "mismatched":
                    all_verified = False
                    step["verify_diff"] = SourceRuleService._compute_diff(
                        before_snapshot[code], after_revoke_snapshot[code]
                    )
                else:
                    step["verify_diff"] = None
            else:
                step["verify_result"] = "no_snapshot"
                step["verify_diff"] = None
                if detail["action"] == "create" and after_revoke_snapshot.get(code) is None:
                    step["verify_result"] = "matched"
                else:
                    all_verified = False

            replay_steps.append(step)

        logger.info(json.dumps({
            "event": "import_replay_data_query",
            "job_id": job_id,
            "has_audit_permission": has_audit_permission,
            "steps_count": len(replay_steps),
            "user_id": user_id,
        }, ensure_ascii=False))

        return {
            "success": True,
            "job_id": job_id,
            "is_revoked": 1,
            "has_audit_permission": has_audit_permission,
            "revoke_verification_passed": all_verified,
            "revoked_at": job_row["revoked_at"],
            "revoked_by": job_row["revoked_by"],
            "revoked_reason": job_row["revoked_reason"],
            "before_snapshot": before_snapshot,
            "after_revoke_snapshot": after_revoke_snapshot,
            "import_details": details,
            "replay_steps": replay_steps,
            "replay_data": replay_steps,
        }

    # ================================================
    # 导入回放中心 - 导出功能
    # ================================================

    @staticmethod
    def export_import_job_json(
        job_id: str,
        user_id: str = None,
        export_type: str = "full",
        conn=None,
    ) -> Dict[str, Any]:
        if conn is None:
            conn = get_db()

        has_export_permission = SourceRuleService.check_import_audit_permission(
            user_id, "import_audit_export", conn
        ) if user_id else False

        if not has_export_permission:
            return {
                "success": False,
                "has_export_permission": False,
                "message": "没有导出导入审计数据的权限",
            }

        job_info = SourceRuleService.get_import_job(job_id, user_id, conn)
        details = SourceRuleService.get_import_job_details(job_id, user_id, page_size=10000, conn=conn)
        snapshots = SourceRuleService.get_import_job_snapshots(job_id, user_id, conn=conn)
        conflicts = SourceRuleService.get_import_job_conflicts(job_id, user_id, conn=conn)
        audit_log = SourceRuleService.get_audit_log(import_id=job_info.get("job", {}).get("id"), limit=10000, conn=conn)

        export_data = {
            "export_version": "1.0",
            "exported_at": now_str(),
            "export_type": export_type,
            "job_id": job_id,
            "job": job_info.get("job", {}),
            "summary": {
                "total": job_info.get("rules_count", 0),
                "success": job_info.get("success_count", 0),
                "created": job_info.get("new_count", 0),
                "overwritten": job_info.get("overwritten_count", 0),
                "skipped": job_info.get("skipped_count", 0),
                "failed": job_info.get("error_count", 0),
                "conflicts_detected": job_info.get("conflict_count", 0),
            },
        }

        if export_type in ["full", "details"]:
            export_data["details"] = details.get("details", [])
        if export_type in ["full", "snapshots"]:
            export_data["snapshots"] = snapshots.get("snapshots", [])
        if export_type in ["full", "conflicts"]:
            export_data["conflicts"] = conflicts.get("conflicts", [])
        if export_type in ["full", "audit_log"]:
            export_data["audit_log"] = audit_log

        logger.info(json.dumps({
            "event": "import_job_exported_json",
            "job_id": job_id,
            "export_type": export_type,
            "records_count": len(details.get("details", [])),
            "user_id": user_id,
        }, ensure_ascii=False))

        return {
            "success": True,
            "has_export_permission": True,
            "content_type": "application/json",
            "filename": f"import_job_{job_id}_{export_type}_{now_str().replace(':', '-')}.json",
            "data": export_data,
        }

    @staticmethod
    def export_import_job_csv(
        job_id: str,
        user_id: str = None,
        export_type: str = "details",
        conn=None,
    ) -> Dict[str, Any]:
        if conn is None:
            conn = get_db()

        has_export_permission = SourceRuleService.check_import_audit_permission(
            user_id, "import_audit_export", conn
        ) if user_id else False

        if not has_export_permission:
            return {
                "success": False,
                "has_export_permission": False,
                "message": "没有导出导入审计数据的权限",
            }

        csv_lines = []

        if export_type == "details":
            details = SourceRuleService.get_import_job_details(job_id, user_id, page_size=10000, conn=conn)
            headers = [
                "rule_code", "action", "status", "rule_index",
                "incoming_rule_code", "incoming_rule_name",
                "before_rule_code", "before_rule_name",
                "after_rule_code", "after_rule_name",
                "diff_summary", "error_message", "created_at"
            ]
            csv_lines.append(",".join(headers))

            for d in details.get("details", []):
                inc = d.get("incoming", {}) or {}
                bef = d.get("before", {}) or {}
                aft = d.get("after", {}) or {}
                diff = d.get("diff", {}) or {}
                diff_fields = [cf.get("field", "") for cf in diff.get("changed_fields", [])][:5]
                row = [
                    f'"{d.get("rule_code", "")}"',
                    f'"{d.get("action", "")}"',
                    f'"{d.get("status", "")}"',
                    str(d.get("rule_index", "")),
                    f'"{inc.get("code", "")}"',
                    f'"{inc.get("name", "")}"',
                    f'"{bef.get("code", "")}"',
                    f'"{bef.get("name", "")}"',
                    f'"{aft.get("code", "")}"',
                    f'"{aft.get("name", "")}"',
                    f'"{", ".join(diff_fields)}"',
                    f'"{(d.get("error_message") or "").replace('"', '""')}"',
                    f'"{d.get("created_at", "")}"',
                ]
                csv_lines.append(",".join(row))

        elif export_type == "diff":
            details = SourceRuleService.get_import_job_details(job_id, user_id, page_size=10000, conn=conn)
            headers = ["rule_code", "field", "old_value", "new_value"]
            csv_lines.append(",".join(headers))

            for d in details.get("details", []):
                diff = d.get("diff", {}) or {}
                for change in diff.get("changed_fields", []):
                    field = change.get("field", "")
                    old_value = str(change.get("before", ""))
                    new_value = str(change.get("after", ""))
                    row = [
                        f'"{d.get("rule_code", "")}"',
                        f'"{field}"',
                        f'"{old_value.replace('"', '""')}"',
                        f'"{new_value.replace('"', '""')}"',
                    ]
                    csv_lines.append(",".join(row))

        elif export_type == "conflicts":
            conflicts = SourceRuleService.get_import_job_conflicts(job_id, user_id, conn=conn)
            headers = [
                "rule_code", "conflict_type", "detected_at",
                "expected_value", "actual_value", "diff_fields",
                "resolver", "resolved_at", "resolution"
            ]
            csv_lines.append(",".join(headers))

            for c in conflicts.get("conflicts", []):
                diff = c.get("diff", {}) or {}
                diff_fields = list(diff.keys())
                row = [
                    f'"{c.get("rule_code", "")}"',
                    f'"{c.get("conflict_type", "")}"',
                    f'"{c.get("detected_at", "")}"',
                    f'"{json.dumps(c.get("expected_before", {}), ensure_ascii=False).replace('"', '""')}"',
                    f'"{json.dumps(c.get("actual_before", {}), ensure_ascii=False).replace('"', '""')}"',
                    f'"{", ".join(diff_fields)}"',
                    f'"{c.get("resolver", "")}"',
                    f'"{c.get("resolved_at", "")}"',
                    f'"{(c.get("resolution") or "").replace('"', '""')}"',
                ]
                csv_lines.append(",".join(row))

        csv_content = "\n".join(csv_lines)

        logger.info(json.dumps({
            "event": "import_job_exported_csv",
            "job_id": job_id,
            "export_type": export_type,
            "lines_count": len(csv_lines),
            "user_id": user_id,
        }, ensure_ascii=False))

        return {
            "success": True,
            "has_export_permission": True,
            "content_type": "text/csv",
            "filename": f"import_job_{job_id}_{export_type}_{now_str().replace(':', '-')}.csv",
            "data": csv_content,
        }

    @staticmethod
    def get_structured_audit_log(
        user_id: str = None,
        job_id: str = None,
        rule_code: str = None,
        operation: str = None,
        start_time: str = None,
        end_time: str = None,
        page: int = 1,
        page_size: int = 50,
        conn=None,
    ) -> Dict[str, Any]:
        if conn is None:
            conn = get_db()

        has_audit_permission = SourceRuleService.check_import_audit_permission(
            user_id, "import_audit_view", conn
        ) if user_id else False

        conditions = []
        params = []

        if job_id:
            job_row = conn.execute(
                "SELECT id FROM source_rules_import_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if job_row:
                conditions.append("import_id = ?")
                params.append(job_row["id"])

        if rule_code:
            conditions.append("rule_code = ?")
            params.append(rule_code)
        if operation:
            conditions.append("operation = ?")
            params.append(operation)
        if start_time:
            conditions.append("created_at >= ?")
            params.append(start_time)
        if end_time:
            conditions.append("created_at <= ?")
            params.append(end_time)

        where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

        count_row = conn.execute(
            f"SELECT COUNT(*) as total FROM source_rules_audit_log{where_clause}",
            params,
        ).fetchone()
        total = count_row["total"] if count_row else 0

        offset = (page - 1) * page_size
        rows = conn.execute(
            f"""SELECT * FROM source_rules_audit_log{where_clause}
               ORDER BY id DESC LIMIT ? OFFSET ?""",
            params + [page_size, offset],
        ).fetchall()

        entries = []
        for row in rows:
            entry = dict(row)

            for json_field in ["before_json", "after_json"]:
                if entry.get(json_field):
                    try:
                        key = json_field.replace("_json", "")
                        entry[key] = json.loads(entry[json_field])
                        if entry[key] and not has_audit_permission:
                            entry[key] = SourceRuleService._mask_sensitive_data(entry[key])
                    except json.JSONDecodeError:
                        key = json_field.replace("_json", "")
                        entry[key] = None
                entry.pop(json_field, None)

            if entry.get("before") and entry.get("after"):
                entry["diff"] = SourceRuleService._compute_diff(entry["before"], entry["after"])
            else:
                entry["diff"] = None

            entry["has_diff"] = 1 if entry.get("diff") and entry["diff"].get("changed_count", 0) > 0 else 0
            entry["timestamp"] = entry.get("created_at")

            if entry.get("import_id"):
                entry["import_job_id"] = entry["import_id"]

            if entry.get("before") and not has_audit_permission:
                entry["before"] = SourceRuleService._mask_sensitive_data(entry["before"])
            if entry.get("after") and not has_audit_permission:
                entry["after"] = SourceRuleService._mask_sensitive_data(entry["after"])
            if entry.get("diff") and not has_audit_permission:
                entry["diff"] = SourceRuleService._mask_sensitive_data(entry["diff"])
            if entry.get("remark") and not has_audit_permission:
                entry["remark"] = "*** 需导入审计权限查看 ***"

            entries.append(entry)

        logger.info(json.dumps({
            "event": "structured_audit_log_query",
            "count": len(entries),
            "total": total,
            "has_audit_permission": has_audit_permission,
            "user_id": user_id,
        }, ensure_ascii=False))

        return {
            "success": True,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_audit_permission": has_audit_permission,
            "audit_logs": entries,
        }

    # ================================================
    # 导入回放中心 - 权限管理
    # ================================================

    @staticmethod
    def grant_import_permission(
        target_user_id: str,
        permission_type: str,
        granted_by: str = None,
        expires_at: str = None,
        conn=None,
    ) -> Dict[str, Any]:
        def _execute(conn_inner):
            valid_permissions = ["import_audit_view", "import_audit_export", "import_revoke", "import_manage"]
            if permission_type not in valid_permissions:
                raise ValueError(
                    f"无效的权限类型: {permission_type}，支持的权限: {', '.join(valid_permissions)}"
                )

            now = now_str()
            conn_inner.execute(
                """INSERT INTO source_rules_import_permissions
                   (user_id, permission_type, granted_by, granted_at, expires_at, is_active)
                   VALUES (?, ?, ?, ?, ?, 1)""",
                (target_user_id, permission_type, granted_by, now, expires_at),
            )

            logger.info(json.dumps({
                "event": "import_permission_granted",
                "target_user_id": target_user_id,
                "permission_type": permission_type,
                "permission_name": SourceRuleService._get_permission_type_name(permission_type),
                "granted_by": granted_by,
                "expires_at": expires_at,
            }, ensure_ascii=False))

            return {
                "success": True,
                "message": f"已为用户 {target_user_id} 授予权限: {SourceRuleService._get_permission_type_name(permission_type)}",
                "permission_type": permission_type,
                "permission_name": SourceRuleService._get_permission_type_name(permission_type),
                "target_user_id": target_user_id,
                "granted_at": now,
                "expires_at": expires_at,
            }

        if conn is None:
            with transaction() as conn:
                return _execute(conn)
        else:
            return _execute(conn)

    @staticmethod
    def get_rule_lineage(
        rule_code: str = None,
        import_job_id: int = None,
        source_type: str = None,
        user_id: str = None,
        page: int = 1,
        page_size: int = 50,
        conn=None,
    ) -> Dict[str, Any]:
        if conn is None:
            conn = get_db()

        has_audit_permission = SourceRuleService.check_import_audit_permission(
            user_id, "import_audit_view", conn
        ) if user_id else False

        conditions = []
        params = []
        if rule_code:
            conditions.append("rule_code = ?")
            params.append(rule_code)
        if import_job_id:
            conditions.append("import_job_id = ?")
            params.append(import_job_id)
        if source_type:
            conditions.append("source_type = ?")
            params.append(source_type)

        where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

        count_row = conn.execute(
            f"SELECT COUNT(*) as total FROM source_rules_lineage{where_clause}",
            params,
        ).fetchone()
        total = count_row["total"] if count_row else 0

        offset = (page - 1) * page_size
        rows = conn.execute(
            f"""SELECT * FROM source_rules_lineage{where_clause}
               ORDER BY id ASC LIMIT ? OFFSET ?""",
            params + [page_size, offset],
        ).fetchall()

        entries = []
        for row in rows:
            entry = dict(row)
            try:
                entry["snapshot"] = json.loads(entry["snapshot_json"]) if entry["snapshot_json"] else None
                if entry["snapshot"] and not has_audit_permission:
                    entry["snapshot"] = SourceRuleService._mask_sensitive_data(entry["snapshot"])
            except json.JSONDecodeError:
                entry["snapshot"] = None
            entry.pop("snapshot_json", None)
            entries.append(entry)

        logger.info(json.dumps({
            "event": "rule_lineage_query",
            "rule_code": rule_code,
            "import_job_id": import_job_id,
            "source_type": source_type,
            "count": len(entries),
            "total": total,
            "has_audit_permission": has_audit_permission,
            "user_id": user_id,
        }, ensure_ascii=False))

        return {
            "success": True,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_audit_permission": has_audit_permission,
            "lineage": entries,
        }
