import logging
from fastapi import FastAPI, HTTPException, Header, Query, UploadFile, File
from fastapi.responses import PlainTextResponse, JSONResponse
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
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="食堂订餐扣费系统",
    description="本地食堂订餐扣费后端服务，支持菜单管理、下单冻结、取餐结算、取消退款和流水导出",
    version="1.0.0",
)


@app.on_event("startup")
def on_startup():
    init_db()


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
    source: Optional[str] = None
    remark: Optional[str] = None


class MakeupRevokeRequest(BaseModel):
    remark: Optional[str] = None


class ConfigUpdate(BaseModel):
    value: str
    description: Optional[str] = None


# ========== Error Helper ==========


def error_response(message: str, code: str = "BAD_REQUEST", status_code: int = 400):
    raise HTTPException(status_code=status_code, detail={"code": code, "message": message})


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


@app.post("/api/admin/orders/makeup", tags=["管理员"], summary="补录取餐记录")
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
