import logging
from fastapi import FastAPI, HTTPException, Header, Query, UploadFile, File, Request, Depends
from fastapi.responses import PlainTextResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from datetime import datetime, timedelta
from typing import Optional, List

from database import init_db
from services import (
    EmployeeService,
    MenuService,
    OrderService,
    TransactionService,
    ReconciliationService,
    MenuImportExportService,
    ConfigService,
    SourceRuleService,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

logging.getLogger("services.source_rule_service").setLevel(logging.INFO)

app = FastAPI(
    title="食堂订餐扣费系统",
    description="本地食堂订餐扣费后端服务，支持菜单管理、下单冻结、取餐结算、取消退款和流水导出",
    version="1.0.0",
)


@app.on_event("startup")
def on_startup():
    init_db()


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = []
    missing_source = False
    for error in exc.errors():
        loc_parts = [str(loc) for loc in error.get("loc", [])]
        field = ".".join(loc_parts)
        if "source" in loc_parts and error.get("type") in ("missing", "string_type", "none_required"):
            missing_source = True
        errors.append(f"{field}: {error['msg']}")
    if missing_source and request.url.path.endswith("/orders/makeup"):
        return JSONResponse(
            status_code=400,
            content={
                "detail": {
                    "code": "MISSING_SOURCE",
                    "message": "补录来源不能为空",
                }
            },
        )
    return JSONResponse(
        status_code=400,
        content={
            "detail": {
                "code": "VALIDATION_ERROR",
                "message": "请求数据验证失败",
                "errors": errors,
            }
        },
    )


# ========== Pydantic Models ==========


class EmployeeCreate(BaseModel):
    id: str
    name: str
    initial_balance: float = 0


class EmployeeAdjust(BaseModel):
    amount: float
    description: str = "管理员调整"


class MenuCreate(BaseModel):
    name: str
    serving_date: str
    deadline: str


class MenuItemCreate(BaseModel):
    name: str
    price: float
    stock: int


class MenuItemUpdate(BaseModel):
    name: Optional[str] = None
    price: Optional[float] = None
    stock: Optional[int] = None


class OrderCreate(BaseModel):
    employee_id: str
    menu_item_id: int
    quantity: int


class MakeupOrderCreate(BaseModel):
    employee_id: str
    menu_item_id: int
    quantity: int
    serving_date: str
    source: str
    remark: Optional[str] = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "employee_id": "E001",
                "menu_item_id": 5,
                "quantity": 1,
                "serving_date": "2026-06-19",
                "source": "admin",
                "remark": "后台补录"
            }
        }
    }


class MakeupOrderErrorResponse(BaseModel):
    code: str
    message: str


class MakeupRevokeRequest(BaseModel):
    remark: Optional[str] = None


class ConfigUpdate(BaseModel):
    value: str
    description: Optional[str] = None


class SourceRuleCreate(BaseModel):
    code: str
    name: str
    description: Optional[str] = None
    category: str = "general"
    priority: int = 0
    is_enabled: bool = True
    match_pattern: Optional[str] = None


class SourceRuleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    priority: Optional[int] = None
    is_enabled: Optional[bool] = None
    match_pattern: Optional[str] = None


class SourceRulesImportRequest(BaseModel):
    rules: List[dict]
    conflict_strategy: str = "skip"
    dry_run: bool = False
    check_concurrent_modifications: bool = True


class ImportRevokeRequest(BaseModel):
    reason: Optional[str] = None


class GrantImportPermissionRequest(BaseModel):
    target_user_id: str
    permission_type: str
    expires_at: Optional[str] = None


# ========== Error Helper ==========


def error_response(message: str, code: str = "BAD_REQUEST", status_code: int = 400):
    raise HTTPException(status_code=status_code, detail={"code": code, "message": message})


# ========== 统一鉴权守卫 ==========


def get_permission_message(permission_type: str) -> str:
    permission_messages = {
        "import_manage": "没有导入管理权限，无法执行导入操作",
        "import_audit_view": "没有导入审计查看权限",
        "import_audit_export": "没有导入审计导出权限",
        "import_revoke": "没有撤销导入的权限",
    }
    return permission_messages.get(permission_type, "没有访问权限")


def require_import_permission(permission_type: str):
    def dependency(request: Request):
        x_user_id = request.headers.get("x-user-id") or request.headers.get("X-User-Id")
        if not x_user_id:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "UNAUTHORIZED",
                    "message": "缺少身份认证信息，请提供 X-User-Id 请求头",
                },
            )
        has_perm = SourceRuleService.check_import_audit_permission(
            x_user_id, permission_type
        )
        if not has_perm:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "PERMISSION_DENIED",
                    "message": get_permission_message(permission_type),
                },
            )
        return x_user_id
    return dependency


# ========== 管理员 - 员工管理 ==========


@app.post("/api/admin/employees", tags=["管理员"], summary="创建员工账户")
def admin_create_employee(data: EmployeeCreate):
    try:
        return EmployeeService.create_employee(data.id, data.name, data.initial_balance)
    except ValueError as e:
        error_response(str(e), "EMPLOYEE_EXISTS")


@app.get("/api/admin/employees", tags=["管理员"], summary="获取所有员工列表")
def admin_list_employees():
    return EmployeeService.list_employees()


@app.get("/api/admin/employees/{emp_id}", tags=["管理员"], summary="获取员工详情")
def admin_get_employee(emp_id: str):
    try:
        return EmployeeService.get_employee(emp_id)
    except ValueError as e:
        error_response(str(e), "EMPLOYEE_NOT_FOUND", 404)


@app.post("/api/admin/employees/{emp_id}/adjust", tags=["管理员"], summary="调整员工余额")
def admin_adjust_balance(emp_id: str, data: EmployeeAdjust):
    try:
        return EmployeeService.adjust_balance(emp_id, data.amount, data.description)
    except ValueError as e:
        error_response(str(e), "INSUFFICIENT_BALANCE")


# ========== 管理员 - 菜单管理 ==========


@app.post("/api/admin/menus", tags=["管理员"], summary="创建菜单")
def admin_create_menu(data: MenuCreate):
    try:
        return MenuService.create_menu(data.name, data.serving_date, data.deadline)
    except ValueError as e:
        error_response(str(e), "MENU_CREATE_ERROR")


@app.get("/api/admin/menus", tags=["管理员"], summary="获取所有菜单")
def admin_list_menus():
    return MenuService.list_menus()


@app.get("/api/admin/menus/{menu_id}", tags=["管理员"], summary="获取菜单详情")
def admin_get_menu(menu_id: int):
    try:
        return MenuService.get_menu(menu_id)
    except ValueError as e:
        error_response(str(e), "MENU_NOT_FOUND", 404)


@app.post("/api/admin/menus/{menu_id}/items", tags=["管理员"], summary="添加菜单项")
def admin_add_menu_item(menu_id: int, data: MenuItemCreate):
    try:
        return MenuService.add_menu_item(menu_id, data.name, data.price, data.stock)
    except ValueError as e:
        error_response(str(e), "MENU_ITEM_ERROR")


@app.patch("/api/admin/menu-items/{item_id}", tags=["管理员"], summary="更新菜单项")
def admin_update_menu_item(item_id: int, data: MenuItemUpdate):
    try:
        return MenuService.update_menu_item(item_id, data.name, data.price, data.stock)
    except ValueError as e:
        error_response(str(e), "MENU_ITEM_ERROR")


@app.post("/api/admin/menus/{menu_id}/publish", tags=["管理员"], summary="发布菜单")
def admin_publish_menu(menu_id: int):
    try:
        return MenuService.publish_menu(menu_id)
    except ValueError as e:
        error_response(str(e), "PUBLISH_ERROR")


@app.post("/api/admin/menus/import/json", tags=["管理员"], summary="批量导入菜单(JSON)")
def admin_import_menus_json(
    data: List[dict],
    conflict_strategy: str = Query("skip", description="冲突策略: skip=跳过, update_draft=更新草稿, report=仅报告冲突"),
):
    try:
        result = MenuImportExportService.import_menus_from_json(data, conflict_strategy)
        if not result["success"] and result["errors"]:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": {
                        "code": "IMPORT_VALIDATION_ERROR",
                        "message": "导入数据校验失败",
                        "errors": result["errors"],
                    }
                },
            )
        return result
    except ValueError as e:
        error_response(str(e), "IMPORT_ERROR")


@app.post("/api/admin/menus/import/csv", tags=["管理员"], summary="批量导入菜单(CSV文件)")
def admin_import_menus_csv(
    file: UploadFile = File(..., description="CSV文件"),
    conflict_strategy: str = Query("skip", description="冲突策略: skip=跳过, update_draft=更新草稿, report=仅报告冲突"),
):
    try:
        content = file.file.read().decode("utf-8-sig")
        result = MenuImportExportService.import_menus_from_csv(content, conflict_strategy)
        if not result["success"] and result["errors"]:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": {
                        "code": "IMPORT_VALIDATION_ERROR",
                        "message": "导入数据校验失败",
                        "errors": result["errors"],
                    }
                },
            )
        return result
    except ValueError as e:
        error_response(str(e), "IMPORT_ERROR")
    except UnicodeDecodeError:
        error_response("文件编码错误，请使用 UTF-8 编码", "IMPORT_ENCODING_ERROR", 400)


@app.get("/api/admin/menus/export/json", tags=["管理员"], summary="导出菜单(JSON)")
def admin_export_menus_json(
    start_date: Optional[str] = Query(None, description="起始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期 YYYY-MM-DD"),
):
    return MenuImportExportService.export_menus_json(start_date, end_date)


@app.get("/api/admin/menus/export/csv", tags=["管理员"], summary="导出菜单(CSV)")
def admin_export_menus_csv(
    start_date: Optional[str] = Query(None, description="起始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期 YYYY-MM-DD"),
):
    csv_content = MenuImportExportService.export_menus_csv(start_date, end_date)
    filename = f"menus_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return PlainTextResponse(
        content=csv_content,
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ========== 管理员 - 补录取餐 ==========


@app.post(
    "/api/admin/orders/makeup",
    tags=["管理员"],
    summary="补录取餐记录",
    responses={
        400: {
            "model": MakeupOrderErrorResponse,
            "description": "补录失败：来源缺失/未匹配规则/规则禁用/余额不足/库存不足等",
            "content": {
                "application/json": {
                    "example": {"detail": {"code": "MISSING_SOURCE", "message": "补录来源不能为空"}}
                }
            }
        },
        409: {
            "model": MakeupOrderErrorResponse,
            "description": "重复补录",
            "content": {
                "application/json": {
                    "example": {"detail": {"code": "DUPLICATE_MAKEUP", "message": "该员工当日已补录该菜品"}}
                }
            }
        }
    }
)
def admin_makeup_order(
    data: MakeupOrderCreate,
    x_idempotency_key: Optional[str] = Header(None),
):
    try:
        order = OrderService.makeup_order(
            employee_id=data.employee_id,
            menu_item_id=data.menu_item_id,
            quantity=data.quantity,
            serving_date=data.serving_date,
            source=data.source,
            remark=data.remark,
            idempotency_key=x_idempotency_key,
        )
        return order
    except ValueError as e:
        msg = str(e)
        if "重复补录" in msg or "已补录过" in msg:
            error_response(msg, "DUPLICATE_MAKEUP", 409)
        elif "余额不足" in msg:
            error_response(msg, "INSUFFICIENT_BALANCE", 400)
        elif "库存不足" in msg:
            error_response(msg, "OUT_OF_STOCK", 400)
        elif "菜单未发布" in msg:
            error_response(msg, "MENU_NOT_PUBLISHED", 400)
        elif "不匹配" in msg and "日期" in msg:
            error_response(msg, "DATE_MISMATCH", 400)
        elif "超出允许范围" in msg or "不能晚于今天" in msg or "超过" in msg and "天" in msg:
            error_response(msg, "DATE_OUT_OF_RANGE", 400)
        elif "来源不能为空" in msg:
            error_response(msg, "MISSING_SOURCE", 400)
        elif "未命中任何来源规则" in msg:
            error_response(msg, "UNMATCHED_SOURCE", 400)
        elif "已禁用" in msg and "来源规则" in msg:
            error_response(msg, "DISABLED_SOURCE_RULE", 400)
        elif "不合法" in msg and "来源" in msg:
            error_response(msg, "INVALID_SOURCE", 400)
        elif "格式错误" in msg:
            error_response(msg, "INVALID_DATE_FORMAT", 400)
        elif "必须大于0" in msg:
            error_response(msg, "INVALID_QUANTITY", 400)
        else:
            error_response(msg, "MAKEUP_ERROR", 400)


@app.get("/api/admin/orders/makeup", tags=["管理员"], summary="查询补录记录")
def admin_query_makeup_orders(
    employee_id: Optional[str] = Query(None, description="员工ID"),
    serving_date: Optional[str] = Query(None, description="供餐日期 YYYY-MM-DD"),
    source: Optional[str] = Query(None, description="补录来源"),
    operation_time_start: Optional[str] = Query(None, description="操作时间起 YYYY-MM-DD HH:MM:SS"),
    operation_time_end: Optional[str] = Query(None, description="操作时间止 YYYY-MM-DD HH:MM:SS"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
):
    try:
        return OrderService.query_makeup_orders(
            employee_id=employee_id,
            serving_date=serving_date,
            source=source,
            operation_time_start=operation_time_start,
            operation_time_end=operation_time_end,
            page=page,
            page_size=page_size,
        )
    except Exception as e:
        error_response(str(e), "MAKEUP_QUERY_ERROR", 400)


@app.post("/api/admin/orders/makeup/{order_id}/revoke", tags=["管理员"], summary="撤销补录")
def admin_revoke_makeup_order(order_id: str, data: MakeupRevokeRequest = None):
    remark = data.remark if data else None
    try:
        result = OrderService.revoke_makeup_order(order_id, remark=remark)
        return result
    except ValueError as e:
        msg = str(e)
        if "不存在" in msg:
            error_response(msg, "ORDER_NOT_FOUND", 404)
        elif "已被撤销" in msg or "重复操作" in msg:
            error_response(msg, "ALREADY_REVOKED", 409)
        elif "非补录来源" in msg:
            error_response(msg, "NOT_MAKEUP_ORDER", 400)
        elif "不允许撤销" in msg and "配置" in msg:
            error_response(msg, "REVOKE_NOT_ALLOWED", 403)
        elif "超过" in msg and "小时" in msg:
            error_response(msg, "REVOKE_DEADLINE_EXCEEDED", 400)
        elif "已处于取消状态" in msg:
            error_response(msg, "ORDER_ALREADY_CANCELLED", 409)
        elif "只能撤销已取餐" in msg or "当前状态为" in msg:
            error_response(msg, "ORDER_STATUS_ERROR", 400)
        else:
            error_response(msg, "REVOKE_ERROR", 400)


# ========== 管理员 - 配置管理 ==========


@app.get("/api/admin/config", tags=["管理员"], summary="获取所有配置")
def admin_list_config():
    return ConfigService.list_config()


@app.get("/api/admin/config/makeup", tags=["管理员"], summary="获取补录相关配置")
def admin_get_makeup_config():
    return ConfigService.get_makeup_config()


@app.put("/api/admin/config/{key}", tags=["管理员"], summary="更新配置")
def admin_update_config(key: str, data: ConfigUpdate):
    try:
        return ConfigService.set_config(key, data.value, data.description)
    except Exception as e:
        error_response(str(e), "CONFIG_ERROR", 400)


# ========== 管理员 - 来源规则管理 ==========


@app.get("/api/admin/source-rules", tags=["管理员"], summary="获取所有来源规则（含各层级）")
def admin_list_source_rules():
    try:
        return SourceRuleService.list_rules()
    except Exception as e:
        error_response(str(e), "SOURCE_RULE_ERROR", 400)


@app.post("/api/admin/source-rules", tags=["管理员"], summary="创建来源规则")
def admin_create_source_rule(
    data: SourceRuleCreate,
    x_operator: Optional[str] = Header(None, description="操作人标识，用于审计追踪"),
):
    try:
        rule = SourceRuleService.create_rule(
            code=data.code,
            name=data.name,
            description=data.description,
            category=data.category,
            priority=data.priority,
            is_enabled=data.is_enabled,
            match_pattern=data.match_pattern,
            operator=x_operator,
        )
        return rule.to_dict()
    except ValueError as e:
        msg = str(e)
        if "已存在" in msg or "已存在" in msg:
            error_response(msg, "SOURCE_RULE_EXISTS", 409)
        else:
            error_response(msg, "SOURCE_RULE_VALIDATION_ERROR", 400)


@app.post("/api/admin/source-rules/import", tags=["管理员"], summary="批量导入来源规则（支持dry-run预检、并发冲突检测）")
def admin_import_source_rules(
    data: SourceRulesImportRequest,
    x_operator: Optional[str] = Header(None, description="操作人标识，用于审计追踪"),
    current_user: str = Depends(require_import_permission("import_manage")),
):
    try:
        result = SourceRuleService.import_rules(
            rules_data=data.rules,
            conflict_strategy=data.conflict_strategy,
            dry_run=data.dry_run,
            operator=x_operator,
            check_concurrent_modifications=data.check_concurrent_modifications,
        )
        if not result["success"]:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": {
                        "code": "SOURCE_RULE_IMPORT_ERROR",
                        "message": "导入数据校验失败或存在冲突",
                        **result,
                    }
                },
            )
        return result
    except ValueError as e:
        error_response(str(e), "SOURCE_RULE_IMPORT_ERROR", 400)


@app.post("/api/admin/source-rules/import/dry-run", tags=["管理员"], summary="导入预检（dry-run专用）")
def admin_import_source_rules_dry_run(
    data: SourceRulesImportRequest,
    x_operator: Optional[str] = Header(None, description="操作人标识，用于审计追踪"),
    current_user: str = Depends(require_import_permission("import_manage")),
):
    try:
        result = SourceRuleService.import_rules(
            rules_data=data.rules,
            conflict_strategy=data.conflict_strategy,
            dry_run=True,
            operator=x_operator,
            check_concurrent_modifications=data.check_concurrent_modifications,
        )
        return result
    except ValueError as e:
        error_response(str(e), "SOURCE_RULE_IMPORT_ERROR", 400)


@app.get("/api/admin/source-rules/export/json", tags=["管理员"], summary="导出来源规则(JSON)")
def admin_export_source_rules_json(
    only_enabled: bool = Query(True, description="仅导出启用的规则"),
    include_all_layers: bool = Query(False, description="包含所有层级(default/environment/runtime)"),
    current_user: str = Depends(require_import_permission("import_audit_export")),
):
    return SourceRuleService.export_rules(only_enabled=only_enabled, include_all_layers=include_all_layers)


@app.get("/api/admin/source-rules/export/csv", tags=["管理员"], summary="导出来源规则(CSV)")
def admin_export_source_rules_csv(
    only_enabled: bool = Query(True, description="仅导出启用的规则"),
    current_user: str = Depends(require_import_permission("import_audit_export")),
):
    csv_content = SourceRuleService.export_rules_csv(only_enabled=only_enabled)
    filename = f"source_rules_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return PlainTextResponse(
        content=csv_content,
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/api/admin/source-rules/import-history/{import_id}", tags=["管理员"], summary="获取单条导入历史详情")
def admin_get_import_history_detail(
    import_id: int,
    current_user: str = Depends(require_import_permission("import_audit_view")),
):
    try:
        history = SourceRuleService.get_import_history(import_id=import_id)
        if not history:
            error_response(f"导入记录 {import_id} 不存在", "IMPORT_NOT_FOUND", 404)
        return history[0]
    except Exception as e:
        error_response(str(e), "SOURCE_RULE_ERROR", 400)


@app.get("/api/admin/source-rules/import-history", tags=["管理员"], summary="获取导入历史")
def admin_get_import_history(
    limit: int = Query(20, ge=1, le=100, description="返回条数"),
    operator: Optional[str] = Query(None, description="按操作人过滤"),
    current_user: str = Depends(require_import_permission("import_audit_view")),
):
    try:
        return SourceRuleService.get_import_history(limit=limit, operator=operator)
    except Exception as e:
        error_response(str(e), "SOURCE_RULE_ERROR", 400)


@app.get("/api/admin/source-rules/audit-log", tags=["管理员"], summary="获取来源规则审计日志")
def admin_get_source_rules_audit_log(
    rule_code: Optional[str] = Query(None, description="按规则code过滤"),
    import_id: Optional[int] = Query(None, description="按导入批次ID过滤"),
    limit: int = Query(50, ge=1, le=200, description="返回条数"),
    current_user: str = Depends(require_import_permission("import_audit_view")),
):
    try:
        return SourceRuleService.get_audit_log(rule_code=rule_code, import_id=import_id, limit=limit)
    except Exception as e:
        error_response(str(e), "SOURCE_RULE_ERROR", 400)


@app.get("/api/admin/source-rules/{code}", tags=["管理员"], summary="获取单个来源规则")
def admin_get_source_rule(code: str):
    try:
        rule = SourceRuleService.get_rule(code)
        if not rule:
            error_response(f"来源规则 {code} 不存在", "SOURCE_RULE_NOT_FOUND", 404)
        return rule.to_dict()
    except ValueError as e:
        error_response(str(e), "SOURCE_RULE_ERROR", 400)


@app.patch("/api/admin/source-rules/{code}", tags=["管理员"], summary="更新来源规则")
def admin_update_source_rule(
    code: str,
    data: SourceRuleUpdate,
    x_operator: Optional[str] = Header(None, description="操作人标识，用于审计追踪"),
):
    try:
        rule = SourceRuleService.update_rule(
            code=code,
            name=data.name,
            description=data.description,
            category=data.category,
            priority=data.priority,
            is_enabled=data.is_enabled,
            match_pattern=data.match_pattern,
            operator=x_operator,
        )
        return rule.to_dict()
    except ValueError as e:
        msg = str(e)
        if "不存在" in msg:
            error_response(msg, "SOURCE_RULE_NOT_FOUND", 404)
        else:
            error_response(msg, "SOURCE_RULE_VALIDATION_ERROR", 400)


@app.delete("/api/admin/source-rules/{code}", tags=["管理员"], summary="删除来源规则")
def admin_delete_source_rule(
    code: str,
    x_operator: Optional[str] = Header(None, description="操作人标识，用于审计追踪"),
):
    try:
        SourceRuleService.delete_rule(code, operator=x_operator)
        return {"success": True, "message": f"来源规则 {code} 已删除"}
    except ValueError as e:
        msg = str(e)
        if "不存在" in msg:
            error_response(msg, "SOURCE_RULE_NOT_FOUND", 404)
        else:
            error_response(msg, "SOURCE_RULE_ERROR", 400)


# ========== 管理员 - 导入回放中心 ==========


@app.get("/api/admin/import-replay/jobs", tags=["导入回放中心"], summary="查询导入作业列表")
def admin_list_import_jobs(
    status: Optional[str] = Query(None, description="按状态过滤: pending/processing/completed/completed_with_errors/failed"),
    operator: Optional[str] = Query(None, description="按操作人过滤"),
    is_revoked: Optional[bool] = Query(None, description="是否已撤销"),
    start_time: Optional[str] = Query(None, description="开始时间 YYYY-MM-DD HH:MM:SS"),
    end_time: Optional[str] = Query(None, description="结束时间 YYYY-MM-DD HH:MM:SS"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    current_user: str = Depends(require_import_permission("import_audit_view")),
):
    try:
        return SourceRuleService.list_import_jobs(
            user_id=current_user,
            status=status,
            operator=operator,
            is_revoked=is_revoked,
            start_time=start_time,
            end_time=end_time,
            page=page,
            page_size=page_size,
        )
    except Exception as e:
        error_response(str(e), "IMPORT_REPLAY_ERROR", 400)


@app.get("/api/admin/import-replay/jobs/{job_id}", tags=["导入回放中心"], summary="获取导入作业详情")
def admin_get_import_job(
    job_id: str,
    current_user: str = Depends(require_import_permission("import_audit_view")),
):
    try:
        result = SourceRuleService.get_import_job(job_id, user_id=current_user)
        return result
    except ValueError as e:
        error_response(str(e), "IMPORT_JOB_NOT_FOUND", 404)


@app.get("/api/admin/import-replay/jobs/{job_id}/details", tags=["导入回放中心"], summary="获取导入作业明细（逐条规则）")
def admin_get_import_job_details(
    job_id: str,
    status_filter: Optional[str] = Query(None, description="按状态过滤: success/skipped/error"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(50, ge=1, le=500, description="每页条数"),
    current_user: str = Depends(require_import_permission("import_audit_view")),
):
    try:
        result = SourceRuleService.get_import_job_details(
            job_id=job_id,
            user_id=current_user,
            status_filter=status_filter,
            page=page,
            page_size=page_size,
        )
        return result
    except ValueError as e:
        error_response(str(e), "IMPORT_JOB_NOT_FOUND", 404)


@app.get("/api/admin/import-replay/jobs/{job_id}/snapshots", tags=["导入回放中心"], summary="获取导入作业快照")
def admin_get_import_job_snapshots(
    job_id: str,
    snapshot_type: str = Query("before_import", description="快照类型: before_import/after_revoke"),
    current_user: str = Depends(require_import_permission("import_audit_view")),
):
    try:
        result = SourceRuleService.get_import_job_snapshots(
            job_id=job_id,
            user_id=current_user,
            snapshot_type=snapshot_type,
        )
        return result
    except ValueError as e:
        error_response(str(e), "IMPORT_JOB_NOT_FOUND", 404)


@app.get("/api/admin/import-replay/jobs/{job_id}/conflicts", tags=["导入回放中心"], summary="获取导入作业冲突记录")
def admin_get_import_job_conflicts(
    job_id: str,
    include_resolved: bool = Query(True, description="是否包含已解决的冲突"),
    current_user: str = Depends(require_import_permission("import_audit_view")),
):
    try:
        result = SourceRuleService.get_import_job_conflicts(
            job_id=job_id,
            user_id=current_user,
            include_resolved=include_resolved,
        )
        return result
    except ValueError as e:
        error_response(str(e), "IMPORT_JOB_NOT_FOUND", 404)


@app.post("/api/admin/import-replay/jobs/{job_id}/revoke", tags=["导入回放中心"], summary="撤销导入作业（恢复到导入前状态）")
def admin_revoke_import_job(
    job_id: str,
    data: ImportRevokeRequest = None,
    x_operator: Optional[str] = Header(None, description="操作人标识，用于审计追踪"),
    current_user: str = Depends(require_import_permission("import_revoke")),
):
    try:
        result = SourceRuleService.revoke_import_job(
            job_id=job_id,
            operator=x_operator,
            user_id=current_user,
            reason=data.reason if data else None,
        )
        if not result.get("success"):
            return JSONResponse(
                status_code=400,
                content={
                    "detail": {
                        "code": "REVOKE_ERROR",
                        "message": result.get("message", "撤销失败"),
                        **result,
                    }
                },
            )
        return result
    except ValueError as e:
        error_response(str(e), "REVOKE_ERROR", 400)


@app.get("/api/admin/import-replay/jobs/{job_id}/replay", tags=["导入回放中心"], summary="获取撤销后回放数据（验证撤销结果）")
def admin_get_import_replay_data(
    job_id: str,
    current_user: str = Depends(require_import_permission("import_audit_view")),
):
    try:
        result = SourceRuleService.get_import_replay_data(
            job_id=job_id,
            user_id=current_user,
        )
        return result
    except ValueError as e:
        error_response(str(e), "IMPORT_JOB_NOT_FOUND", 404)


@app.get("/api/admin/import-replay/jobs/{job_id}/export/json", tags=["导入回放中心"], summary="导出导入作业为JSON")
def admin_export_import_job_json(
    job_id: str,
    export_type: str = Query("full", description="导出类型: full/details/snapshots/conflicts/audit_log"),
    current_user: str = Depends(require_import_permission("import_audit_export")),
):
    try:
        result = SourceRuleService.export_import_job_json(
            job_id=job_id,
            user_id=current_user,
            export_type=export_type,
        )
        return JSONResponse(
            content=result["data"],
            headers={
                "Content-Disposition": f"attachment; filename={result['filename']}"
            },
        )
    except ValueError as e:
        error_response(str(e), "EXPORT_ERROR", 400)


@app.get("/api/admin/import-replay/jobs/{job_id}/export/csv", tags=["导入回放中心"], summary="导出导入作业为CSV")
def admin_export_import_job_csv(
    job_id: str,
    export_type: str = Query("details", description="导出类型: details/diff/conflicts"),
    current_user: str = Depends(require_import_permission("import_audit_export")),
):
    try:
        result = SourceRuleService.export_import_job_csv(
            job_id=job_id,
            user_id=current_user,
            export_type=export_type,
        )
        return PlainTextResponse(
            content=result["data"],
            media_type="text/csv; charset=utf-8-sig",
            headers={
                "Content-Disposition": f"attachment; filename={result['filename']}"
            },
        )
    except ValueError as e:
        error_response(str(e), "EXPORT_ERROR", 400)


@app.get("/api/admin/import-replay/audit-log", tags=["导入回放中心"], summary="获取结构化审计日志")
def admin_get_structured_audit_log(
    job_id: Optional[str] = Query(None, description="按导入作业ID过滤"),
    rule_code: Optional[str] = Query(None, description="按规则code过滤"),
    operation: Optional[str] = Query(None, description="按操作类型过滤: create/update/delete"),
    start_time: Optional[str] = Query(None, description="开始时间 YYYY-MM-DD HH:MM:SS"),
    end_time: Optional[str] = Query(None, description="结束时间 YYYY-MM-DD HH:MM:SS"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(50, ge=1, le=200, description="每页条数"),
    current_user: str = Depends(require_import_permission("import_audit_view")),
):
    try:
        result = SourceRuleService.get_structured_audit_log(
            user_id=current_user,
            job_id=job_id,
            rule_code=rule_code,
            operation=operation,
            start_time=start_time,
            end_time=end_time,
            page=page,
            page_size=page_size,
        )
        return result
    except Exception as e:
        error_response(str(e), "AUDIT_LOG_ERROR", 400)


@app.post("/api/admin/import-replay/permissions/grant", tags=["导入回放中心"], summary="授予导入审计权限")
def admin_grant_import_permission(
    data: GrantImportPermissionRequest,
    x_operator: Optional[str] = Header(None, description="授权人标识"),
):
    try:
        return SourceRuleService.grant_import_permission(
            target_user_id=data.target_user_id,
            permission_type=data.permission_type,
            granted_by=x_operator,
            expires_at=data.expires_at,
        )
    except ValueError as e:
        error_response(str(e), "PERMISSION_GRANT_ERROR", 400)


@app.get("/api/admin/import-replay/lineage", tags=["导入回放中心"], summary="查询规则来源溯源链")
def admin_get_rule_lineage(
    rule_code: Optional[str] = Query(None, description="按规则code过滤"),
    import_job_id: Optional[int] = Query(None, description="按导入作业ID过滤"),
    source_type: Optional[str] = Query(None, description="按来源类型过滤: manual_create/import_create/manual_update/import_overwrite/revoke_restore/revoke_delete"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(50, ge=1, le=200, description="每页条数"),
    current_user: str = Depends(require_import_permission("import_audit_view")),
):
    try:
        result = SourceRuleService.get_rule_lineage(
            rule_code=rule_code,
            import_job_id=import_job_id,
            source_type=source_type,
            user_id=current_user,
            page=page,
            page_size=page_size,
        )
        return result
    except Exception as e:
        error_response(str(e), "LINEAGE_ERROR", 400)


# ========== 员工 - 菜单浏览 ==========


@app.get("/api/menus", tags=["员工端"], summary="获取已发布的菜单列表")
def list_published_menus():
    menus = MenuService.list_menus(only_published=True)
    for m in menus:
        m.pop("is_published", None)
    return menus


@app.get("/api/menus/{menu_id}", tags=["员工端"], summary="获取菜单详情（含菜品）")
def get_menu_detail(menu_id: int):
    try:
        menu = MenuService.get_menu(menu_id)
        if not menu["is_published"]:
            error_response("菜单未发布", "MENU_NOT_PUBLISHED", 404)
        return menu
    except ValueError as e:
        error_response(str(e), "MENU_NOT_FOUND", 404)


# ========== 员工 - 订单管理 ==========


@app.post("/api/orders", tags=["员工端"], summary="下单（冻结余额）")
def place_order(data: OrderCreate, x_idempotency_key: Optional[str] = Header(None)):
    try:
        order = OrderService.place_order(
            employee_id=data.employee_id,
            menu_item_id=data.menu_item_id,
            quantity=data.quantity,
            idempotency_key=x_idempotency_key,
        )
        return order
    except ValueError as e:
        msg = str(e)
        if "已过订餐截止时间" in msg:
            error_response(msg, "DEADLINE_PASSED", 400)
        elif "库存不足" in msg:
            error_response(msg, "OUT_OF_STOCK", 400)
        elif "余额不足" in msg:
            error_response(msg, "INSUFFICIENT_BALANCE", 400)
        elif "菜单未发布" in msg:
            error_response(msg, "MENU_NOT_PUBLISHED", 400)
        else:
            error_response(msg, "ORDER_ERROR")


@app.get("/api/orders/{order_id}", tags=["员工端"], summary="获取订单详情")
def get_order(order_id: str):
    try:
        return OrderService.get_order(order_id)
    except ValueError as e:
        error_response(str(e), "ORDER_NOT_FOUND", 404)


@app.get("/api/orders", tags=["员工端"], summary="查询订单列表")
def list_orders(
    employee_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    menu_id: Optional[int] = Query(None),
):
    return OrderService.list_orders(employee_id=employee_id, status=status, menu_id=menu_id)


@app.post("/api/orders/{order_id}/take", tags=["员工端"], summary="取餐结算")
def take_meal(order_id: str):
    try:
        return OrderService.take_meal(order_id)
    except ValueError as e:
        msg = str(e)
        if "已取餐" in msg or "无法取餐" in msg:
            error_response(msg, "ORDER_STATUS_ERROR", 400)
        else:
            error_response(str(e), "TAKE_ERROR")


@app.post("/api/orders/{order_id}/cancel", tags=["员工端"], summary="取消订单（释放冻结）")
def cancel_order(order_id: str):
    try:
        return OrderService.cancel_order(order_id)
    except ValueError as e:
        msg = str(e)
        if "已取餐" in msg:
            error_response(msg, "ALREADY_TAKEN", 400)
        elif "已过订餐截止时间" in msg:
            error_response(msg, "DEADLINE_PASSED", 400)
        else:
            error_response(msg, "CANCEL_ERROR")


# ========== 流水与对账 ==========


@app.get("/api/transactions", tags=["流水"], summary="查询流水记录")
def list_transactions(
    employee_id: Optional[str] = Query(None),
    order_id: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    return TransactionService.list_transactions(
        employee_id=employee_id,
        order_id=order_id,
        txn_type=type,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        offset=offset,
    )


@app.get("/api/transactions/export", tags=["流水"], summary="导出流水CSV")
def export_transactions(
    employee_id: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
):
    csv_content = TransactionService.export_transactions_csv(
        employee_id=employee_id,
        start_date=start_date,
        end_date=end_date,
    )
    filename = f"transactions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return PlainTextResponse(
        content=csv_content,
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/api/admin/reconciliation", tags=["管理员"], summary="数据一致性对账检查")
def check_reconciliation():
    return ReconciliationService.check_consistency()


# ========== 健康检查 ==========


@app.get("/api/health", tags=["系统"], summary="健康检查")
def health_check():
    try:
        result = ReconciliationService.check_consistency()
        return {"status": "ok", "consistent": result["consistent"]}
    except Exception as e:
        raise HTTPException(status_code=500, detail={"status": "error", "message": str(e)})


# ========== 初始化示例数据 ==========


@app.post("/api/admin/init-sample", tags=["系统"], summary="初始化示例数据")
def init_sample_data():
    try:
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        deadline = (datetime.now() + timedelta(hours=12)).strftime("%Y-%m-%d %H:%M:%S")

        EmployeeService.create_employee("EMP001", "张三", 100.0)
        EmployeeService.create_employee("EMP002", "李四", 50.0)
        EmployeeService.create_employee("EMP003", "王五", 200.0)

        menu = MenuService.create_menu(f"午餐菜单-{tomorrow}", tomorrow, deadline)

        MenuService.add_menu_item(menu["id"], "红烧肉", 18.0, 50)
        MenuService.add_menu_item(menu["id"], "清蒸鱼", 25.0, 30)
        MenuService.add_menu_item(menu["id"], "番茄炒蛋", 12.0, 100)
        MenuService.add_menu_item(menu["id"], "米饭", 2.0, 200)
        MenuService.add_menu_item(menu["id"], "紫菜蛋花汤", 5.0, 80)

        MenuService.publish_menu(menu["id"])

        return {"message": "示例数据初始化成功", "menu_date": tomorrow}
    except Exception as e:
        error_response(f"初始化失败: {str(e)}", "INIT_ERROR")
