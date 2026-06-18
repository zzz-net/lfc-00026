# 食堂订餐扣费后端服务

本地轻量级食堂订餐扣费系统，使用 FastAPI + SQLite 构建。

## 功能特性

- **菜单管理**：管理员配置菜单、菜品、库存、订餐截止时间
- **账户管理**：员工初始余额、余额调整
- **下单冻结**：员工下单时冻结对应金额余额
- **取餐结算**：取餐后扣减余额，完成结算
- **取消退款**：截止时间前可取消订单，释放冻结金额
- **流水记录**：每一步操作都写入流水，可追溯、可导出CSV
- **幂等性保证**：支持幂等键，重复请求返回相同结果
- **并发安全**：基于数据库事务，避免并发重复扣款
- **数据一致性**：服务重启后自动对账，订单/余额/库存/流水一致

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 启动服务

```bash
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

服务启动后访问：
- API 文档（Swagger UI）：http://127.0.0.1:8000/docs
- 健康检查：http://127.0.0.1:8000/api/health

### 3. 初始化示例数据

调用初始化接口，会创建3个员工、1个含5道菜品的菜单并发布：

```bash
curl -X POST http://127.0.0.1:8000/api/admin/init-sample
```

或者运行完整测试脚本：

```bash
python test_api.py
```

## 数据模型

### 员工表 (employees)
- `id`：员工ID（主键）
- `name`：姓名
- `balance`：账户余额
- `frozen_balance`：冻结余额（已下单未取餐）

### 菜单表 (menus)
- `id`：菜单ID
- `name`：菜单名称
- `serving_date`：供餐日期
- `deadline`：订餐截止时间
- `is_published`：是否发布

### 菜单项表 (menu_items)
- `id`：菜品ID
- `menu_id`：所属菜单ID
- `name`：菜品名称
- `price`：价格
- `stock`：库存
- `sold_count`：已售数量

### 订单表 (orders)
- `id`：订单号
- `idempotency_key`：幂等键（唯一）
- `employee_id`：员工ID
- `menu_item_id`：菜品ID
- `status`：状态（pending/taken/cancelled）
- `total_amount`：订单金额

### 流水表 (transactions)
- `id`：流水ID
- `type`：类型（INITIAL/ADJUST/FREEZE/SETTLE/UNFREEZE）
- `employee_id`：员工ID
- `order_id`：订单ID
- `amount`：金额
- `balance_before/after`：变动前后余额
- `frozen_before/after`：变动前后冻结金额
- `idempotency_key`：幂等键

## API 列表

### 管理员接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /api/admin/employees | 创建员工 |
| GET | /api/admin/employees | 员工列表 |
| GET | /api/admin/employees/{id} | 员工详情 |
| POST | /api/admin/employees/{id}/adjust | 调整余额 |
| POST | /api/admin/menus | 创建菜单 |
| GET | /api/admin/menus | 菜单列表 |
| GET | /api/admin/menus/{id} | 菜单详情 |
| POST | /api/admin/menus/{id}/items | 添加菜品 |
| PATCH | /api/admin/menu-items/{id} | 更新菜品 |
| POST | /api/admin/menus/{id}/publish | 发布菜单 |
| POST | /api/admin/menus/import/json | 批量导入菜单(JSON) |
| POST | /api/admin/menus/import/csv | 批量导入菜单(CSV文件) |
| GET | /api/admin/menus/export/json | 导出菜单(JSON) |
| GET | /api/admin/menus/export/csv | 导出菜单(CSV) |
| GET | /api/admin/reconciliation | 对账检查 |

### 员工端接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/menus | 已发布菜单列表 |
| GET | /api/menus/{id} | 菜单详情 |
| POST | /api/orders | 下单（冻结余额） |
| GET | /api/orders | 订单列表 |
| GET | /api/orders/{id} | 订单详情 |
| POST | /api/orders/{id}/take | 取餐结算 |
| POST | /api/orders/{id}/cancel | 取消订单 |

### 流水接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/transactions | 查询流水 |
| GET | /api/transactions/export | 导出CSV |

## 错误码说明

| 错误码 | 说明 |
|--------|------|
| EMPLOYEE_EXISTS | 员工ID已存在 |
| EMPLOYEE_NOT_FOUND | 员工不存在 |
| INSUFFICIENT_BALANCE | 余额不足 |
| MENU_NOT_FOUND | 菜单不存在 |
| MENU_NOT_PUBLISHED | 菜单未发布 |
| OUT_OF_STOCK | 库存不足 |
| DEADLINE_PASSED | 已过截止时间 |
| ORDER_NOT_FOUND | 订单不存在 |
| ORDER_STATUS_ERROR | 订单状态不允许操作 |
| ALREADY_TAKEN | 已取餐，无法取消 |

## 核心流程示例

### 1. 发布菜单流程

```bash
# 创建菜单
curl -X POST http://127.0.0.1:8000/api/admin/menus \
  -H "Content-Type: application/json" \
  -d '{
    "name": "周一午餐",
    "serving_date": "2026-06-20",
    "deadline": "2026-06-19 18:00:00"
  }'

# 添加菜品
curl -X POST http://127.0.0.1:8000/api/admin/menus/1/items \
  -H "Content-Type: application/json" \
  -d '{"name": "红烧肉", "price": 18.0, "stock": 50}'

# 发布菜单
curl -X POST http://127.0.0.1:8000/api/admin/menus/1/publish
```

### 2. 员工下单流程

```bash
# 下单（带幂等键）
curl -X POST http://127.0.0.1:8000/api/orders \
  -H "Content-Type: application/json" \
  -H "X-Idempotency-Key: my-unique-key-001" \
  -d '{
    "employee_id": "EMP001",
    "menu_item_id": 1,
    "quantity": 2
  }'
```

### 3. 取餐结算

```bash
curl -X POST http://127.0.0.1:8000/api/orders/ORD12345678/take
```

### 4. 取消订单

```bash
curl -X POST http://127.0.0.1:8000/api/orders/ORD12345678/cancel
```

### 5. 流水导出

```bash
curl http://127.0.0.1:8000/api/transactions/export -o transactions.csv
```

## 对账与一致性

服务启动后可随时调用对账接口检查数据一致性：

```bash
curl http://127.0.0.1:8000/api/admin/reconciliation
```

对账检查项：
- 员工余额与流水末尾是否一致
- 员工冻结金额与待结算订单总额是否一致
- 菜品已售数量是否在合理范围内（不超过库存、不为负）
- 订单对应流水是否完整

## 并发安全

- 使用 SQLite 的 `BEGIN IMMEDIATE` 事务保证写操作串行化
- 幂等键唯一索引防止重复下单
- 余额冻结/扣减在同一事务中完成
- 库存扣减与订单创建原子化

## 运行时生成文件与提交隔离

服务运行后，项目根目录会产生以下本地文件。**它们全部被 `.gitignore` 忽略，不会进入版本提交。**

### SQLite 数据库文件

| 文件 | 何时产生 | 服务停止后 | 删除后果 | `.gitignore` 规则 |
|------|----------|-----------|----------|-------------------|
| `canteen.db` | 首次启动时自动创建 | **保留**，含全部业务数据 | 丢失所有本地数据，重启后从空库开始 | `*.db` |
| `canteen.db-wal` | WAL 模式下有写操作时产生 | 正常关闭后自动回收为空或消失；异常退出可能残留 | 无影响，内容已合并到主库 | `*.db-wal` |
| `canteen.db-shm` | WAL 模式下有并发读时产生 | 正常关闭后自动删除；异常退出可能残留 | 无影响，仅索引辅助 | `*.db-shm` |

> SQLite 启用了 WAL（Write-Ahead Logging）模式（见 [database.py](file:///d:/workSpace/AI__SPACE/lfc-00026/database.py#L17)）。`-wal` 和 `-shm` 是该模式的运行时分片，正常关闭时 SQLite 会自动清理；若服务被强杀，残留的 `-wal` / `-shm` 文件会在下次启动时被 SQLite 自行处理，无需手动干预。

### 其他运行时产物

| 文件/目录 | 何时产生 | `.gitignore` 规则 |
|-----------|----------|-------------------|
| `__pycache__/` | Python 解释器导入模块时自动生成 | `__pycache__/` |
| `*.log` | 本服务默认不写日志文件；若用户重定向输出则可能产生 | `*.log` |

### 清理方式

删除数据库文件将**丢失所有本地测试数据**（员工余额、订单、流水等），重启后从空库开始。如需重置：

```bash
# 先停止服务，再删除数据库文件
# Linux / macOS
rm -f canteen.db canteen.db-wal canteen.db-shm

# Windows PowerShell
Remove-Item canteen.db, canteen.db-wal, canteen.db-shm -ErrorAction SilentlyContinue

# 重新启动后会自动创建空库
uvicorn main:app --host 127.0.0.1 --port 8000
```

仅清理缓存，不影响数据：

```bash
# Linux / macOS
rm -rf __pycache__ services/__pycache__

# Windows PowerShell
Remove-Item __pycache__, services\__pycache__ -Recurse -ErrorAction SilentlyContinue
```

### 确认运行态产物不会误入提交

```bash
# 查看被忽略的文件（!! 标记）
git status --short --ignored

# 逐一验证忽略规则命中
git check-ignore -v canteen.db canteen.db-wal canteen.db-shm
```

## 项目结构

```
.
├── main.py                  # 主应用和API层
├── database.py              # 数据库连接和表结构
├── services/
│   ├── __init__.py
│   ├── employee_service.py  # 员工账户服务
│   ├── menu_service.py      # 菜单服务
│   ├── order_service.py     # 订单服务
│   └── transaction_service.py # 流水和对账服务
├── test_api.py              # API测试脚本
├── verify_reboot.py         # 重启一致性验证脚本
├── requirements.txt         # 依赖列表
├── .gitignore               # 忽略规则（含数据库和缓存）
│
│   # 以下为运行时产物，不进入提交
├── canteen.db               # SQLite 主库（首次启动生成）
├── canteen.db-wal           # WAL 写入日志（运行中生成，正常关闭后回收）
├── canteen.db-shm           # WAL 共享内存（运行中生成，正常关闭后删除）
└── __pycache__/             # Python 字节码缓存
```
