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

## 菜单批量导入导出

管理员可以一次性导入一周的菜单，也可以将当前菜单导出备份。支持 JSON 和 CSV 两种格式。

### 文件格式说明

#### JSON 格式

根节点为菜单数组，每个菜单包含 `items` 菜品数组：

```json
[
  {
    "name": "周一午餐",
    "serving_date": "2026-07-01",
    "deadline": "2026-07-01 09:00:00",
    "is_published": false,
    "items": [
      {"name": "红烧肉", "price": 18.0, "stock": 50},
      {"name": "番茄炒蛋", "price": 12.0, "stock": 100},
      {"name": "米饭", "price": 2.0, "stock": 200}
    ]
  }
]
```

#### CSV 格式

每行一道菜，菜单级字段（日期、名称、截止时间、发布状态）每行重复：

```csv
serving_date,menu_name,deadline,is_published,item_name,price,stock
2026-07-01,周一午餐,2026-07-01 09:00:00,0,红烧肉,18.0,50
2026-07-01,周一午餐,2026-07-01 09:00:00,0,番茄炒蛋,12.0,100
2026-07-01,周一午餐,2026-07-01 09:00:00,0,米饭,2.0,200
2026-07-02,周二午餐,2026-07-02 09:00:00,0,清蒸鱼,25.0,30
```

CSV 必需列：`serving_date`, `menu_name`, `deadline`, `item_name`, `price`, `stock`
可选列：`is_published`（0=草稿，1=已发布，默认0）

### 字段校验规则

| 字段 | 规则 |
|------|------|
| serving_date | 格式 YYYY-MM-DD，不能为空 |
| menu_name / name | 菜单名称，不能为空 |
| deadline | 格式 YYYY-MM-DD HH:MM:SS，不能为空 |
| item_name / name | 菜品名称，不能为空 |
| price | 非负数字，不能为空 |
| stock | 非负整数，不能为空 |
| is_published | 0/false/no 为草稿，1/true/yes 为已发布 |

校验失败时，响应中 `errors` 数组会列出**所有错误行号及原因**。

### 冲突策略

当导入的菜单日期在系统中已存在时，通过 `conflict_strategy` 参数控制处理方式：

| 策略 | 说明 |
|------|------|
| `skip` | **跳过**冲突日期（默认），保留现有菜单 |
| `update_draft` | 仅更新**草稿状态**的菜单；**已发布菜单会报错跳过** |
| `report` | 仅检测并报告冲突，**不做任何修改** |

> ⚠️ **安全提醒**：已发布的菜单永远不会被导入操作静默覆盖。即使使用 `update_draft` 策略，已发布菜单也会被跳过并在 `errors` 中给出原因。

### 冲突结果查看

导入响应结构：

```json
{
  "success": true,
  "total": 5,
  "created": 3,
  "updated": 1,
  "skipped": 1,
  "errors": ["供餐日期 2026-07-01 的菜单已发布，无法修改"],
  "conflicts": [
    {
      "serving_date": "2026-07-01",
      "existing_menu_id": 1,
      "existing_menu_name": "周一午餐",
      "existing_is_published": true,
      "incoming_menu_name": "新周一午餐",
      "incoming_is_published": false
    }
  ]
}
```

#### 响应字段逐项解读：

| 字段 | 含义 | 管理员该怎么看 |
|------|------|----------------|
| `success` | 导入是否成功（校验通过为 true） | 只要不是 `false` 就说明导入流程走完了，即使有部分跳过/冲突也是成功的 |
| `total` | 导入文件中解析到的菜单总数 | 和你准备的菜单数量对比，确认没有漏读 |
| `created` | 成功新建的菜单数 | 这些是全新的日期，之前系统中没有 |
| `updated` | 成功更新的菜单数 | 只有 `update_draft` 策略下才会有值，表示草稿菜单被覆盖了 |
| `skipped` | 跳过的菜单总数 | 包括：冲突跳过 + 已发布保护跳过 + 错误跳过 |
| `errors` | 错误明细数组 | **逐条看**，每条都带行号/日期和具体原因 |
| `conflicts` | 冲突明细数组 | **逐条核对**，每个冲突日期的新旧菜单信息都在这里 |

#### `conflicts` 冲突明细逐项解读：

| 字段 | 含义 | 该怎么处理 |
|------|------|------------|
| `serving_date` | 冲突的供餐日期 | 先看是哪一天的冲突 |
| `existing_menu_id` | 系统中已有的菜单 ID | 可以调用 `/api/admin/menus/{id}` 查看现有详情 |
| `existing_menu_name` | 系统中已有的菜单名称 | 确认是不是你想要保留的 |
| `existing_is_published` | 现有菜单是否已发布 | `true`=已发布（受保护，无法修改），`false`=草稿（可更新） |
| `incoming_menu_name` | 你导入文件中的菜单名称 | 确认是不是你想要的新名称 |
| `incoming_is_published` | 你导入文件中的发布状态 | 确认你想设成什么状态 |

#### 典型冲突场景解读：

**场景 1：已发布菜单冲突**
```json
{
  "serving_date": "2026-07-01",
  "existing_menu_name": "周一午餐",
  "existing_is_published": true,
  "incoming_menu_name": "新周一午餐"
}
```
> **解读**：7月1日已有已发布的"周一午餐"，导入的是"新周一午餐"。由于已发布，**任何策略都不会修改**，会出现在 `errors` 中。
> **处理**：如果确实要改，先在系统里单独操作（不建议），或者确认导入文件里的日期是否正确。

**场景 2：草稿菜单冲突（skip 策略）
```json
{
  "serving_date": "2026-07-02",
  "existing_menu_name": "周二午餐",
  "existing_is_published": false,
  "incoming_menu_name": "新周二午餐"
}
```
> **解读**：7月2日有草稿状态的"周二午餐"，使用 `skip` 策略，所以**跳过不修改**，保留系统中的版本。
> **处理**：如果想更新，改用 `update_draft` 策略重新导入。

**场景 3：草稿菜单冲突（update_draft 策略）
> 同样的冲突数据，使用 `update_draft` 策略时，`updated` 会 +1，冲突仍然出现在 `conflicts` 中但不会被跳过。

### 服务端日志查看

导入导出的每一步操作都会在服务端输出详细日志，包括：
- 导入开始/结束、冲突检测结果、每一条菜单的处理动作（新建/更新/跳过）、字段校验错误详情

启动服务时可以看到类似日志：
```
2026-06-19 10:30:00 - services.menu_import_export_service - INFO - [JSON导入] 开始处理，冲突策略: skip
2026-06-19 10:30:00 - services.menu_import_export_service - INFO - [冲突检测] 发现 2 个日期冲突:
2026-06-19 10:30:00 - services.menu_import_export_service - INFO -   - 2026-07-01: 现有菜单='周一午餐'(已发布), 待导入='新周一午餐'
2026-06-19 10:30:00 - services.menu_import_export_service - INFO - [跳过] 供餐日期 2026-07-01 的菜单已发布，无法修改
2026-06-19 10:30:00 - services.menu_import_export_service - INFO - [新建] 2026-07-03: '周三午餐', 4 道菜品
2026-06-19 10:30:00 - services.menu_import_export_service - INFO - [JSON导入] 完成: 总计=3, 新建=1, 更新=0, 跳过=2, 冲突=2
```

### 使用示例

#### 1. 导入 JSON 菜单（skip 策略）

```bash
curl -X POST "http://127.0.0.1:8000/api/admin/menus/import/json?conflict_strategy=skip" \
  -H "Content-Type: application/json" \
  -d '[
    {
      "name": "周一午餐",
      "serving_date": "2026-07-01",
      "deadline": "2026-07-01 09:00:00",
      "items": [
        {"name": "红烧肉", "price": 18, "stock": 50},
        {"name": "米饭", "price": 2, "stock": 200}
      ]
    }
  ]'
```

#### 2. 导入 CSV 菜单文件（update_draft 策略）

```bash
curl -X POST "http://127.0.0.1:8000/api/admin/menus/import/csv?conflict_strategy=update_draft" \
  -F "file=@menus.csv"
```

#### 3. 预览冲突（不实际导入）

```bash
curl -X POST "http://127.0.0.1:8000/api/admin/menus/import/json?conflict_strategy=report" \
  -H "Content-Type: application/json" \
  -d '[{"name":"测试","serving_date":"2026-07-01","deadline":"2026-07-01 09:00:00","items":[{"name":"A","price":1,"stock":1}]}]'
```

#### 4. 导出全部菜单（JSON）

```bash
curl http://127.0.0.1:8000/api/admin/menus/export/json
```

#### 5. 导出指定日期范围菜单（CSV）

```bash
curl "http://127.0.0.1:8000/api/admin/menus/export/csv?start_date=2026-07-01&end_date=2026-07-07" \
  -o week_menus.csv
```

#### 6. 导出后重启服务再导入（备份恢复流程）

```bash
# 第一步：导出备份
curl http://127.0.0.1:8000/api/admin/menus/export/json > menu_backup.json

# 第二步：服务重启后（如数据库重置），重新导入
curl -X POST "http://127.0.0.1:8000/api/admin/menus/import/json?conflict_strategy=skip" \
  -H "Content-Type: application/json" \
  -d "$(cat menu_backup.json)"
```

### 对管理员操作的变化

新增能力后，管理员的实际操作变化：

1. **批量录入效率提升**：过去一周 5-7 天菜单需要逐天创建、逐个菜品添加，现在可以用 Excel/表格编辑好 CSV 一键导入，几分钟的工作缩短到几秒。
2. **菜单可备份**：导出功能提供 JSON/CSV 两种格式备份，误操作或数据库重置后可以快速恢复。
3. **冲突可控**：三种冲突策略（跳过/更新草稿/仅报告）满足不同场景需求，已发布菜单受保护不会被误改。
4. **错误定位清晰**：校验失败返回行号+原因，对照 Excel 即可快速修正，无需逐行排查。

### 运行导入导出测试

```bash
python test_menu_import_export.py
```

覆盖场景：
- JSON/CSV 成功导入
- 字段格式错误（含行号）
- 同日期冲突的三种策略
- 已发布菜单保护
- JSON/CSV 导出
- 导出-再导入数据一致性

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
