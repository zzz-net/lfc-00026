import logging
from typing import Optional, Dict, Any

from database import get_db, transaction, now_str
from services.source_rule_service import SourceRuleService

logger = logging.getLogger(__name__)


class ConfigService:
    @staticmethod
    def get_config(key: str, conn=None) -> Optional[str]:
        if conn is None:
            conn = get_db()
        row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    @staticmethod
    def get_config_int(key: str, default: int = 0, conn=None) -> int:
        value = ConfigService.get_config(key, conn)
        if value is None:
            return default
        try:
            return int(value)
        except (ValueError, TypeError):
            logger.warning(f"[配置] 键 {key} 的值 '{value}' 不是有效整数，使用默认值 {default}")
            return default

    @staticmethod
    def get_config_list(key: str, separator: str = ",", conn=None) -> list:
        value = ConfigService.get_config(key, conn)
        if value is None:
            return []
        return [item.strip() for item in value.split(separator) if item.strip()]

    @staticmethod
    def set_config(key: str, value: str, description: str = None, conn=None) -> Dict[str, Any]:
        def _execute(conn_inner):
            now = now_str()
            existing = conn_inner.execute(
                "SELECT * FROM config WHERE key = ?", (key,)
            ).fetchone()

            if existing:
                desc = description if description is not None else existing["description"]
                conn_inner.execute(
                    "UPDATE config SET value = ?, description = ?, updated_at = ? WHERE key = ?",
                    (value, desc, now, key),
                )
                logger.info(f"[配置] 更新: {key} = {value}")
            else:
                desc = description or ""
                conn_inner.execute(
                    "INSERT INTO config (key, value, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (key, value, desc, now, now),
                )
                logger.info(f"[配置] 新增: {key} = {value}")

            row = conn_inner.execute("SELECT * FROM config WHERE key = ?", (key,)).fetchone()
            return dict(row)

        if conn is None:
            with transaction() as conn:
                return _execute(conn)
        else:
            return _execute(conn)

    @staticmethod
    def list_config(conn=None) -> Dict[str, Dict[str, Any]]:
        if conn is None:
            conn = get_db()
        rows = conn.execute("SELECT * FROM config ORDER BY key").fetchall()
        result = {}
        for row in rows:
            result[row["key"]] = {
                "value": row["value"],
                "description": row["description"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        return result

    @staticmethod
    def get_makeup_config(conn=None) -> Dict[str, Any]:
        allow_revoke_val = ConfigService.get_config("makeup_allow_revoke", conn) or "true"
        allowed_sources = SourceRuleService.get_allowed_sources(conn)
        if not allowed_sources:
            allowed_sources = ConfigService.get_config_list("makeup_allowed_sources", ",", conn)
        return {
            "days_limit": ConfigService.get_config_int("makeup_days_limit", 7, conn),
            "default_source": ConfigService.get_config("makeup_default_source", conn) or "window",
            "allowed_sources": allowed_sources,
            "default_remark": ConfigService.get_config("makeup_default_remark", conn) or "线下窗口补录",
            "allow_revoke": allow_revoke_val.lower() in ("true", "1", "yes"),
            "revoke_deadline_hours": ConfigService.get_config_int("makeup_revoke_deadline_hours", 24, conn),
        }
