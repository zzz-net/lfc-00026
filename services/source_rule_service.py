import logging
import json
import os
import re
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
            conn_inner.execute(
                """INSERT INTO source_rules 
                   (code, name, description, category, priority, is_enabled, match_pattern, version, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    code,
                    name,
                    description,
                    category,
                    priority,
                    1 if is_enabled else 0,
                    match_pattern,
                    SourceRuleService.RULES_VERSION,
                    now,
                    now,
                ),
            )

            row = conn_inner.execute(
                "SELECT * FROM source_rules WHERE code = ?", (code,)
            ).fetchone()

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
            )

            SourceRuleService._write_audit_log(
                rule_code=code,
                operation="create",
                after_data=created_rule.to_dict(),
                conn=conn_inner,
            )

            logger.info(json.dumps({
                "event": "source_rule_created",
                "code": code,
                "name": name,
                "category": category,
                "priority": priority,
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
        conn=None,
    ) -> SourceRule:
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
            )

            after_data = updated_rule.to_dict()

            SourceRuleService._write_audit_log(
                rule_code=code,
                operation="update",
                before_data=before_data,
                after_data=after_data,
                conn=conn_inner,
            )

            logger.info(json.dumps({
                "event": "source_rule_updated",
                "code": code,
                "updated_fields": list(update_data.keys()),
                "before": before_data,
                "after": after_data,
            }, ensure_ascii=False))

            SourceRuleService._invalidate_cache()

            return updated_rule

        if conn is None:
            with transaction() as conn:
                return _execute(conn)
        else:
            return _execute(conn)

    @staticmethod
    def delete_rule(code: str, conn=None) -> bool:
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
                conn=conn_inner,
            )

            logger.info(json.dumps({
                "event": "source_rule_deleted",
                "code": code,
                "deleted_rule": before_data,
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
            "rules_count": len(rules_data),
            "success_count": 0,
            "skipped_count": 0,
            "error_count": 0,
            "new_count": 0,
            "overwritten_count": 0,
            "disabled_blocked_count": 0,
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

        def _execute(conn_inner):
            now = now_str()
            existing_db_codes = {
                row["code"]: dict(row) for row in conn_inner.execute(
                    "SELECT * FROM source_rules"
                ).fetchall()
            }
            for code in existing_db_codes:
                existing_db_codes[code]["is_enabled"] = bool(existing_db_codes[code]["is_enabled"])

            merged_rules = SourceRuleService.get_merged_rules(conn_inner)
            merged_codes = {r.code: r for r in merged_rules}

            all_runtime_rules = SourceRuleService.get_runtime_rules(conn_inner)
            all_runtime_dict = {r.code: r for r in all_runtime_rules}

            if conflict_strategy == "report":
                report_conflicts = []
                for idx, rule_data in enumerate(rules_data):
                    if not isinstance(rule_data, dict):
                        continue
                    code = rule_data.get("code", f"index={idx}")
                    if code in merged_codes or code in existing_db_codes:
                        existing = merged_codes.get(code)
                        report_conflicts.append({
                            "code": code,
                            "index": idx,
                            "existing_rule": {
                                "code": existing.code if existing else code,
                                "name": existing.name if existing else existing_db_codes.get(code, {}).get("name"),
                                "priority": existing.priority if existing else existing_db_codes.get(code, {}).get("priority"),
                                "category": existing.category if existing else existing_db_codes.get(code, {}).get("category"),
                                "is_enabled": existing.is_enabled if existing else existing_db_codes.get(code, {}).get("is_enabled"),
                                "source_layer": existing.source_layer if existing else "runtime",
                            } if code in merged_codes else {
                                "code": code,
                                "source_layer": "runtime",
                                **existing_db_codes.get(code, {}),
                            },
                            "action": "reported",
                            "reason": f"来源 code='{code}' 已存在，冲突策略为 report，不执行导入",
                        })
                if report_conflicts:
                    results["success"] = False
                    results["error_count"] = len(report_conflicts)
                    results["conflict_rules"] = report_conflicts
                    results["errors"] = [c["reason"] for c in report_conflicts]
                    results["result_summary"] = f"导入失败: 发现{len(report_conflicts)}个冲突 (report策略，不做任何修改)"
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
                    continue

                code = rule_data["code"]
                is_enabled_incoming = rule_data.get("is_enabled", True)

                conflict_with_merged = None
                if code in merged_codes:
                    existing_rule = merged_codes[code]
                    conflict_with_merged = {
                        "code": existing_rule.code,
                        "name": existing_rule.name,
                        "priority": existing_rule.priority,
                        "category": existing_rule.category,
                        "is_enabled": existing_rule.is_enabled,
                        "source_layer": existing_rule.source_layer,
                        "match_pattern": existing_rule.match_pattern,
                    }

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
                            "incoming_rule": {
                                "code": code,
                                "name": rule_data.get("name"),
                                "is_enabled": is_enabled_incoming,
                            },
                            "existing_rule": {
                                "code": code,
                                "name": existing_db_rule.get("name"),
                                "is_enabled": existing_db_rule["is_enabled"],
                                "source_layer": "runtime",
                            },
                            "impact": "导入后该来源将被禁用，新的补录请求会被拒绝，历史记录不受影响",
                        }
                        results["disabled_blocked_rules"].append(block_entry)
                        results["disabled_blocked_count"] += 1
                        logger.warning(json.dumps({
                            "event": "source_rule_import_disabled_blocked",
                            "code": code,
                            "reason": block_reason,
                            "operator": operator,
                        }, ensure_ascii=False))

                detail_entry = {
                    "code": code,
                    "index": idx,
                    "strategy": conflict_strategy,
                    "incoming_rule": {
                        "code": code,
                        "name": rule_data.get("name"),
                        "priority": rule_data.get("priority", 0),
                        "category": rule_data.get("category", "general"),
                        "is_enabled": is_enabled_incoming,
                        "match_pattern": rule_data.get("match_pattern"),
                        "description": rule_data.get("description"),
                    },
                    "existing_rule": conflict_with_merged or (
                        {"code": code, "source_layer": "runtime", **existing_db_rule}
                        if existing_db_rule else None
                    ),
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
                        logger.info(json.dumps({
                            "event": "source_rule_import_skip",
                            "code": code,
                            "existing_layer": conflict_with_merged["source_layer"] if conflict_with_merged else "runtime",
                            "operator": operator,
                            "dry_run": dry_run,
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
                            logger.warning(json.dumps({
                                "event": "source_rule_import_skip_not_runtime",
                                "code": code,
                                "existing_layer": conflict_with_merged["source_layer"],
                                "operator": operator,
                                "dry_run": dry_run,
                            }, ensure_ascii=False))
                            continue

                        if dry_run:
                            detail_entry["action"] = "would_overwrite"
                            detail_entry["reason"] = "dry_run模式，仅预览不会实际覆盖"
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
                                conn=conn_inner,
                            )
                            results["success_count"] += 1
                            results["overwritten_count"] += 1
                            detail_entry["action"] = "overwritten"
                            detail_entry["reason"] = (
                                f"已存在(层级: {conflict_with_merged['source_layer'] if conflict_with_merged else 'runtime'})，"
                                f"覆盖更新; 变更: "
                                f"name={before_data.get('name')}->{rule_data.get('name')}, "
                                f"priority={before_data.get('priority')}->{rule_data.get('priority', 0)}, "
                                f"category={before_data.get('category')}->{rule_data.get('category', 'general')}, "
                                f"is_enabled={before_data.get('is_enabled')}->{is_enabled_incoming}"
                            )
                            detail_entry["before"] = before_data
                            detail_entry["after"] = updated.to_dict()
                            results["overwritten_rules"].append(detail_entry)
                            results["imported_rules"].append(updated.to_dict())
                            existing_db_codes[code] = updated.to_dict()
                            logger.info(json.dumps({
                                "event": "source_rule_import_overwrite",
                                "code": code,
                                "before": before_data,
                                "after": updated.to_dict(),
                                "operator": operator,
                                "dry_run": dry_run,
                            }, ensure_ascii=False))
                            continue
                        except ValueError as e:
                            results["error_count"] += 1
                            results["errors"].append(
                                f"规则 {code} 更新失败: {str(e)}"
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
                    new_entry = {
                        **detail_entry,
                        "action": "would_create",
                        "reason": "dry_run模式，仅预览不会实际创建",
                        "dry_run": True,
                    }
                    results["new_rules"].append(new_entry)
                    results["imported_rules"].append({
                        "code": code,
                        "name": rule_data.get("name"),
                        "category": rule_data.get("category", "general"),
                        "priority": rule_data.get("priority", 0),
                        "is_enabled": is_enabled_incoming,
                        "match_pattern": rule_data.get("match_pattern"),
                        "dry_run": True,
                    })
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
                        conn=conn_inner,
                    )
                    results["success_count"] += 1
                    results["new_count"] += 1
                    new_entry = {
                        **detail_entry,
                        "action": "created",
                        "reason": "成功创建新规则",
                        "after": created.to_dict(),
                    }
                    results["new_rules"].append(new_entry)
                    results["imported_rules"].append(created.to_dict())
                    existing_db_codes[code] = created.to_dict()
                    logger.info(json.dumps({
                        "event": "source_rule_import_created",
                        "code": code,
                        "rule": created.to_dict(),
                        "operator": operator,
                        "dry_run": dry_run,
                    }, ensure_ascii=False))
                except ValueError as e:
                    results["error_count"] += 1
                    results["errors"].append(
                        f"规则 {code} 创建失败: {str(e)}"
                    )

            results["success"] = results["error_count"] == 0

            result_summary = (
                f"导入完成: 成功{results['success_count']}条 "
                f"(新增{results['new_count']}条, 覆盖{results['overwritten_count']}条), "
                f"跳过{results['skipped_count']}条, "
                f"失败{results['error_count']}条, "
                f"禁用拦截{results['disabled_blocked_count']}条"
                f"{' (dry_run预览)' if dry_run else ''}"
            )
            results["result_summary"] = result_summary

            if not dry_run:
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
                    "success_count": results["success_count"],
                    "new_count": results["new_count"],
                    "overwritten_count": results["overwritten_count"],
                    "skipped_count": results["skipped_count"],
                    "error_count": results["error_count"],
                    "disabled_blocked_count": results["disabled_blocked_count"],
                    "conflict_strategy": conflict_strategy,
                    "effective_rules": effective_rules,
                    "operator": operator,
                    "dry_run": dry_run,
                }, ensure_ascii=False))

                if results["success_count"] > 0 or results["skipped_count"] > 0:
                    SourceRuleService._invalidate_cache()
                    SourceRuleService.ensure_cache_fresh()

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
