import logging
import json
import os
import re
import time
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass

from database import get_db, transaction, now_str, SOURCE_RULES_VERSION

logger = logging.getLogger(__name__)


@dataclass
class SourceRule:
    id: Optional[int]
    code: str
    name: str
    description: Optional[str]
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
        logger.info("[来源规则] 缓存已失效")

    DEFAULT_RULES: List[Dict[str, Any]] = [
        {
            "code": "window",
            "name": "线下窗口",
            "description": "食堂窗口人工补录",
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
            "priority": 80,
            "is_enabled": True,
            "match_pattern": None,
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
                logger.warning(f"[来源规则] 环境变量规则格式错误，应为数组: {env_value}")
                return []
            parsed_rules = []
            for r in rules:
                parsed_rules.append({
                    **r,
                    "source_layer": "environment",
                    "version": r.get("version", SourceRuleService.RULES_VERSION),
                    "is_enabled": r.get("is_enabled", True),
                    "priority": r.get("priority", 0),
                })
            return parsed_rules
        except json.JSONDecodeError as e:
            logger.warning(f"[来源规则] 环境变量规则JSON解析失败: {e}")
            return []

    @staticmethod
    def get_default_rules() -> List[SourceRule]:
        return [
            SourceRule(
                id=None,
                code=r["code"],
                name=r["name"],
                description=r["description"],
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

        for rule in default_rules:
            merged[rule.code] = rule

        for rule in env_rules:
            if rule.code in merged:
                if rule.priority >= merged[rule.code].priority:
                    logger.info(
                        f"[来源规则] 环境变量规则覆盖默认规则: {rule.code} "
                        f"(优先级: {merged[rule.code].priority} -> {rule.priority})"
                    )
                    merged[rule.code] = rule
                else:
                    logger.info(
                        f"[来源规则] 环境变量规则优先级低于默认规则，保留默认: {rule.code}"
                    )
            else:
                merged[rule.code] = rule

        for rule in runtime_rules:
            if rule.code in merged:
                if rule.priority >= merged[rule.code].priority:
                    logger.info(
                        f"[来源规则] 运行时规则覆盖已有规则: {rule.code} "
                        f"(优先级: {merged[rule.code].priority} -> {rule.priority}, "
                        f"来源: {merged[rule.code].source_layer} -> runtime)"
                    )
                    merged[rule.code] = rule
                else:
                    logger.info(
                        f"[来源规则] 运行时规则优先级低于已有规则，保留原有: {rule.code}"
                    )
            else:
                merged[rule.code] = rule

        result = sorted(
            [r for r in merged.values() if r.is_enabled],
            key=lambda r: (-r.priority, r.code),
        )

        logger.info(
            f"[来源规则] 合并完成，共 {len(result)} 条有效规则 "
            f"(默认: {len(default_rules)}, 环境: {len(env_rules)}, 运行时: {len(runtime_rules)})"
        )

        SourceRuleService._cache["merged_rules"] = result
        SourceRuleService._cache_timestamp = now

        return result

    @staticmethod
    def get_allowed_sources(conn=None) -> List[str]:
        rules = SourceRuleService.get_merged_rules(conn)
        return [r.code for r in rules]

    @staticmethod
    def match_source(source_code: str, conn=None) -> Optional[SourceRule]:
        rules = SourceRuleService.get_merged_rules(conn)

        for rule in rules:
            if rule.code == source_code:
                logger.info(
                    f"[来源规则] 命中规则: code={rule.code}, name={rule.name}, "
                    f"priority={rule.priority}, layer={rule.source_layer}"
                )
                return rule

            if rule.match_pattern:
                try:
                    if re.match(rule.match_pattern, source_code):
                        logger.info(
                            f"[来源规则] 模式匹配命中: code={rule.code}, "
                            f"pattern={rule.match_pattern}, input={source_code}, "
                            f"layer={rule.source_layer}"
                        )
                        return rule
                except re.error as e:
                    logger.warning(
                        f"[来源规则] 规则 {rule.code} 的匹配模式无效: {e}"
                    )

        logger.warning(f"[来源规则] 未找到匹配的来源规则: {source_code}")
        return None

    @staticmethod
    def validate_source(source_code: str, conn=None) -> Tuple[bool, Optional[str], Optional[SourceRule]]:
        if not source_code:
            return False, "来源不能为空", None

        rule = SourceRuleService.match_source(source_code, conn)
        if rule:
            return True, None, rule

        allowed = SourceRuleService.get_allowed_sources(conn)
        return (
            False,
            f"补录来源 '{source_code}' 不合法，允许的来源: {', '.join(allowed)}",
            None,
        )

    @staticmethod
    def validate_rule_data(rule_data: Dict[str, Any]) -> Tuple[bool, List[str]]:
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

        return len(errors) == 0, errors

    @staticmethod
    def list_rules(conn=None) -> Dict[str, Any]:
        merged_rules = SourceRuleService.get_merged_rules(conn)
        runtime_rules = SourceRuleService.get_runtime_rules(conn)
        default_rules = SourceRuleService.get_default_rules()
        env_rules = SourceRuleService.get_environment_rules()

        return {
            "version": SourceRuleService.RULES_VERSION,
            "total": len(merged_rules),
            "rules": [r.to_dict() for r in merged_rules],
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
        priority: int = 0,
        is_enabled: bool = True,
        match_pattern: str = None,
        conn=None,
    ) -> SourceRule:
        rule_data = {
            "code": code,
            "name": name,
            "description": description,
            "priority": priority,
            "is_enabled": is_enabled,
            "match_pattern": match_pattern,
            "version": SourceRuleService.RULES_VERSION,
        }

        is_valid, errors = SourceRuleService.validate_rule_data(rule_data)
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
                   (code, name, description, priority, is_enabled, match_pattern, version, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    code,
                    name,
                    description,
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

            logger.info(
                f"[来源规则] 创建成功: code={code}, name={name}, priority={priority}"
            )

            SourceRuleService._invalidate_cache()

            return SourceRule(
                id=row["id"],
                code=row["code"],
                name=row["name"],
                description=row["description"],
                priority=row["priority"],
                is_enabled=bool(row["is_enabled"]),
                match_pattern=row["match_pattern"],
                version=row["version"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                source_layer="runtime",
            )

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

            update_data = {}
            if name is not None:
                update_data["name"] = name
            if description is not None:
                update_data["description"] = description
            if priority is not None:
                update_data["priority"] = priority
            if is_enabled is not None:
                update_data["is_enabled"] = 1 if is_enabled else 0
            if match_pattern is not None:
                update_data["match_pattern"] = match_pattern

            if not update_data:
                raise ValueError("没有需要更新的字段")

            validate_data = {**dict(existing), **update_data}
            validate_data["is_enabled"] = bool(validate_data.get("is_enabled", existing["is_enabled"]))
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

            logger.info(
                f"[来源规则] 更新成功: code={code}, updates={list(update_data.keys())}"
            )

            SourceRuleService._invalidate_cache()

            return SourceRule(
                id=row["id"],
                code=row["code"],
                name=row["name"],
                description=row["description"],
                priority=row["priority"],
                is_enabled=bool(row["is_enabled"]),
                match_pattern=row["match_pattern"],
                version=row["version"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                source_layer="runtime",
            )

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

            conn_inner.execute("DELETE FROM source_rules WHERE code = ?", (code,))

            logger.info(f"[来源规则] 删除成功: code={code}")
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
            "conflicts": [],
            "errors": [],
            "imported_rules": [],
        }

        if not isinstance(rules_data, list):
            results["success"] = False
            results["error_count"] = 1
            results["errors"].append("导入数据必须是规则数组")
            return results

        def _execute(conn_inner):
            now = now_str()
            existing_codes = {
                row["code"] for row in conn_inner.execute(
                    "SELECT code FROM source_rules"
                ).fetchall()
            }

            for idx, rule_data in enumerate(rules_data):
                rule_identifier = rule_data.get("code", f"index={idx}")

                version = rule_data.get("version", SourceRuleService.RULES_VERSION)
                if version not in SourceRuleService.SUPPORTED_VERSIONS:
                    results["error_count"] += 1
                    results["errors"].append(
                        f"规则 {rule_identifier}: 版本 {version} 不支持，"
                        f"支持的版本: {', '.join(SourceRuleService.SUPPORTED_VERSIONS)}"
                    )
                    continue

                is_valid, validation_errors = SourceRuleService.validate_rule_data(rule_data)
                if not is_valid:
                    results["error_count"] += 1
                    results["errors"].append(
                        f"规则 {rule_identifier} 验证失败: {'; '.join(validation_errors)}"
                    )
                    continue

                code = rule_data["code"]
                if code in existing_codes:
                    conflict_info = {
                        "code": code,
                        "index": idx,
                        "strategy": conflict_strategy,
                    }

                    if conflict_strategy == "skip":
                        results["skipped_count"] += 1
                        conflict_info["action"] = "skipped"
                        conflict_info["reason"] = "已存在，跳过"
                        results["conflicts"].append(conflict_info)
                        logger.info(f"[来源规则导入] 跳过已存在规则: {code}")
                        continue

                    elif conflict_strategy == "overwrite":
                        try:
                            updated = SourceRuleService.update_rule(
                                code=code,
                                name=rule_data.get("name"),
                                description=rule_data.get("description"),
                                priority=rule_data.get("priority", 0),
                                is_enabled=rule_data.get("is_enabled", True),
                                match_pattern=rule_data.get("match_pattern"),
                                conn=conn_inner,
                            )
                            results["success_count"] += 1
                            conflict_info["action"] = "overwritten"
                            conflict_info["reason"] = "已存在，覆盖更新"
                            results["conflicts"].append(conflict_info)
                            results["imported_rules"].append(updated.to_dict())
                            existing_codes.add(code)
                            logger.info(f"[来源规则导入] 覆盖已存在规则: {code}")
                            continue
                        except ValueError as e:
                            results["error_count"] += 1
                            results["errors"].append(
                                f"规则 {code} 更新失败: {str(e)}"
                            )
                            continue

                    elif conflict_strategy == "report":
                        results["error_count"] += 1
                        conflict_info["action"] = "reported"
                        conflict_info["reason"] = "已存在，报告冲突"
                        results["conflicts"].append(conflict_info)
                        results["errors"].append(
                            f"规则 {code} 已存在，冲突策略为 report，导入失败"
                        )
                        continue

                try:
                    created = SourceRuleService.create_rule(
                        code=code,
                        name=rule_data["name"],
                        description=rule_data.get("description"),
                        priority=rule_data.get("priority", 0),
                        is_enabled=rule_data.get("is_enabled", True),
                        match_pattern=rule_data.get("match_pattern"),
                        conn=conn_inner,
                    )
                    results["success_count"] += 1
                    results["imported_rules"].append(created.to_dict())
                    existing_codes.add(code)
                    logger.info(f"[来源规则导入] 新增规则成功: {code}")
                except ValueError as e:
                    results["error_count"] += 1
                    results["errors"].append(
                        f"规则 {code} 创建失败: {str(e)}"
                    )

            results["success"] = results["error_count"] == 0

            details_json = json.dumps({
                "conflicts": results["conflicts"],
                "errors": results["errors"],
            }, ensure_ascii=False)

            result_summary = (
                f"导入完成: 成功{results['success_count']}条, "
                f"跳过{results['skipped_count']}条, "
                f"失败{results['error_count']}条"
            )

            conn_inner.execute(
                """INSERT INTO source_rules_import_log
                   (import_version, rules_count, success_count, skipped_count, 
                    error_count, conflict_strategy, result_summary, details_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    SourceRuleService.RULES_VERSION,
                    results["rules_count"],
                    results["success_count"],
                    results["skipped_count"],
                    results["error_count"],
                    conflict_strategy,
                    result_summary,
                    details_json,
                    now,
                ),
            )

            logger.info(f"[来源规则导入] {result_summary}")

            if results["success_count"] > 0:
                SourceRuleService._invalidate_cache()

            return results

        if conn is None:
            with transaction() as conn:
                return _execute(conn)
        else:
            return _execute(conn)

    @staticmethod
    def export_rules(only_enabled: bool = True, conn=None) -> Dict[str, Any]:
        rules = SourceRuleService.get_runtime_rules(conn)

        if only_enabled:
            rules = [r for r in rules if r.is_enabled]

        export_data = {
            "version": SourceRuleService.RULES_VERSION,
            "exported_at": now_str(),
            "count": len(rules),
            "rules": [
                {
                    "code": r.code,
                    "name": r.name,
                    "description": r.description,
                    "priority": r.priority,
                    "is_enabled": r.is_enabled,
                    "match_pattern": r.match_pattern,
                    "version": r.version,
                }
                for r in rules
            ],
        }

        logger.info(f"[来源规则导出] 共导出 {len(rules)} 条规则")
        return export_data

    @staticmethod
    def get_import_history(limit: int = 20, conn=None) -> List[Dict[str, Any]]:
        if conn is None:
            conn = get_db()
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
            history.append(entry)

        return history
