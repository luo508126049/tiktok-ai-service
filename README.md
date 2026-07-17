# tiktok-ai-service

抖音百应选品采集系统。系统通过 Flask 提供 Web 控制台，通过 Playwright 驱动服务器端浏览器访问百应选品页面，完成扫码登录、商品筛选、商品详情读取、联系方式读取和历史数据管理。

## 1. 系统能力

### 1.1 系统账号

- 所有页面和业务接口都需要先登录系统。
- 内置最高权限账号 `admin`，首次初始化密码为 `123456`。
- 只有 `admin` 可以访问采集控制台、执行采集、控制百应浏览器和管理账号。
- 普通账号只能访问历史数据页面和历史数据读取、导出接口，不能进入采集控制台。
- 管理员可以创建带中文名的普通账号、修改账号密码和删除普通账号。
- `admin` 同时只允许一个登录会话。
- `admin` 执行采集任务时不能被新的管理员登录顶替；任务完成并释放任务锁后，新的管理员登录才可以接管旧会话。
- 账号密码使用 Werkzeug 哈希保存，不保存明文密码。

### 1.2 百应登录和扫码

- 管理员在采集控制台点击“打开登录页”。
- Playwright 在服务器端打开无头百应浏览器，不要求 Linux 服务器具备桌面环境。
- 前端定时请求 `/login-qr`，后端只返回登录页面中的二维码区域图片，管理员用手机扫码。
- 登录成功后，登录二维码自动隐藏，百应浏览器会话保存在持久化 Profile 中。
- 百应登录态与系统登录态是两套独立状态：系统账号登录不代表百应已经授权。

### 1.3 商品采集

采集流程如下：

1. 使用 `admin` 登录系统。
2. 打开百应登录页并扫码授权。
3. 进入选品页。
4. 设置本次采集数量，默认 90，允许范围为 1 到 10000。
5. 点击“开始采集”。

采集逻辑包括：

- 通过“月销”筛选项选择销量大于等于 5000 的商品作为候选。
- 捕获或复用百应商品列表请求，支持直接请求和页面滚动翻页两种方式。
- 跳过历史数据库中已经出现过的店铺。
- 打开商品详情页，读取商品、店铺、评分、月销等信息。
- 尝试读取商家商品 ID、微信号和手机号等公开联系方式。
- 对联系方式接口做请求间隔控制，检测百应的频繁访问限制。
- 任务完成或失败后清理临时筛选并刷新选品页，避免筛选条件影响下一次任务。
- 每个任务结束后，前端采集按钮会恢复为可点击的“开始采集”。

### 1.4 历史数据

历史数据保存在 SQLite 数据库中，支持：

- 按商品名称、店铺名称、商品 ID、店铺 ID 和商家商品 ID搜索。
- 按采集批次 `collection_id` 筛选。
- 按店铺评分小于某值筛选。
- 按月销大于某值筛选。
- 分页浏览。
- 管理员批量删除历史记录。
- 导出 Excel 文件。

## 2. 项目结构

```text
tiktok-ai-service/
├── python_login_app.py   # Flask 应用、页面模板、Playwright 和采集主流程
├── auth_store.py         # 系统账号、密码哈希和管理员单会话存储
├── history_store.py      # SQLite 历史数据存储
├── requirements.txt      # Python 依赖
├── install.ps1           # Windows 环境初始化脚本
├── manage.ps1            # Windows 启停和状态管理脚本
├── start.bat             # Windows 启动入口
├── stop.bat              # Windows 停止入口
├── restart.bat           # Windows 重启入口
└── data/                 # 运行时数据库、浏览器 Profile、日志和缓存
```

页面当前使用 Python 字符串模板定义在 `python_login_app.py` 中，没有独立的前端构建工程。

## 3. 环境要求

- Python 3.10 或更高版本。
- Chromium 浏览器，由 Playwright 安装或使用系统已有 Chrome/Edge。
- Windows 可以使用项目内的 PowerShell 和批处理脚本。
- Linux 可以手动创建虚拟环境并使用 Python 直接启动；项目中的 `.ps1` 和 `.bat` 文件主要面向 Windows。
- 服务器需要能够访问 `buyin.jinritemai.com`。

## 4. 安装和启动

### 4.1 Windows 初始化

在项目根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

脚本会完成以下工作：

- 创建 `.venv` 虚拟环境。
- 安装 `requirements.txt` 中的依赖。
- 安装 Playwright Chromium。

### 4.2 Windows 启停

```powershell
.\manage.ps1 start
.\manage.ps1 status
.\manage.ps1 restart
.\manage.ps1 stop
```

也可以直接双击 `start.bat`、`stop.bat` 和 `restart.bat`。

默认控制台地址：

```text
http://127.0.0.1:8000
```

### 4.3 直接启动

```powershell
.\.venv\Scripts\python.exe python_login_app.py
```

如果没有虚拟环境，也可以使用当前 Python：

```powershell
python python_login_app.py
```

应用当前使用单进程、单线程 Flask 模式，因为 Playwright 的同步 API 和全局浏览器上下文绑定在创建线程上。

## 5. 首次使用

1. 打开 `http://127.0.0.1:8000/login`。
2. 使用 `admin / 123456` 登录系统。
3. 进入“账号管理”，立即修改管理员密码。
4. 按需创建普通账号，创建时填写登录账号、中文名和初始密码。
5. 返回采集控制台，打开百应登录页并扫码。
6. 登录百应后进入选品页，再开始采集。

管理员密码只会在账号数据库中不存在 `admin` 时使用默认值初始化。管理员修改密码后，程序重启不会覆盖新密码。

## 6. 页面和权限

| 页面 | 地址 | 管理员 | 普通用户 |
| --- | --- | --- | --- |
| 系统登录 | `/login` | 可访问 | 可访问 |
| 采集控制台 | `/` | 可访问 | 自动跳转到历史数据 |
| 历史数据 | `/results` | 可访问 | 可访问 |
| 账号管理 | `/admin/accounts` | 可访问 | 403 |

普通用户的历史数据页面不显示采集控制台和账号管理导航。权限仍由后端接口强制校验，不能仅依靠前端隐藏按钮来限制访问。

## 7. HTTP API

除登录接口外，所有接口都要求带有系统 Session Cookie。

### 7.1 系统账号接口

#### `GET /login`

返回系统登录页。

#### `POST /auth/login`

表单字段：

```text
username=admin
password=123456
```

登录成功后：

- 管理员跳转到 `/`。
- 普通用户跳转到 `/results`。
- 如果已有管理员正在执行采集任务，新的管理员登录返回 HTTP 409，不能顶替当前会话。

#### `GET|POST /auth/logout`

退出系统账号登录，清除当前系统 Session。该接口不会退出百应浏览器账号。

#### `POST /admin/accounts/create`

管理员创建普通账号，表单字段：

```text
username=operator01
display_name=张三
password=12345678
```

账号只能包含字母、数字、下划线和短横线；中文名不能为空且不超过 32 个字符；密码至少 6 位。

#### `POST /admin/accounts/password`

管理员修改账号密码，表单字段：

```text
username=operator01
password=new-password
```

#### `POST /admin/accounts/delete`

管理员删除普通账号，表单字段：

```text
username=operator01
```

`admin` 是系统保留账号，不能删除。被删除普通账号的已有 Session 会在下一次请求时失效。

### 7.2 历史数据接口

#### `GET /api/results`

分页查询历史记录。

参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `limit` | `100` | 返回数量，最大由存储层限制为 1000 |
| `offset` | `0` | 偏移量 |
| `q` | 空 | 搜索名称、店铺或 ID |
| `collection_id` | 空 | 指定采集批次 |
| `shop_score_lt` | 空 | 店铺评分小于该值 |
| `month_sale_gt` | 空 | 月销大于该值 |

示例：

```text
GET /api/results?limit=100&offset=0&q=商品
GET /api/results?shop_score_lt=4.5&month_sale_gt=1000
```

管理员和普通用户均可访问。

#### `GET /api/results/export`

按与 `/api/results` 相同的查询参数导出 Excel：

```text
GET /api/results/export?q=商品&month_sale_gt=1000
```

管理员和普通用户均可访问。

#### `POST /api/results/delete`

管理员批量删除历史记录。请求体：

```json
{
  "ids": [1, 2, 3]
}
```

普通用户调用返回 HTTP 403。

#### `GET /api/history`

返回历史记录总数：

```json
{
  "count": 97,
  "database": "data/history.db"
}
```

管理员和普通用户均可访问。

### 7.3 采集和百应浏览器接口

以下接口全部只允许管理员调用。

| 方法 | 地址 | 说明 |
| --- | --- | --- |
| `POST` | `/open-login` | 打开或重置百应登录页 |
| `GET` | `/login-qr` | 返回当前百应登录二维码 PNG |
| `POST` | `/go-selection` | 进入百应选品页 |
| `POST` | `/collect-selection` | 执行采集任务，JSON 可传 `limit` |
| `GET` | `/status` | 返回浏览器连接、百应登录和当前页面状态 |
| `POST` | `/close` | 关闭 Playwright 浏览器会话 |
| `POST` | `/toggle-browser` | 在前台和后台浏览器模式之间切换 |
| `POST` | `/logout` | 退出百应账号并清理百应会话 |

采集请求示例：

```json
{
  "limit": 90
}
```

注意：`/logout` 是退出百应，不是退出系统。退出系统使用 `/auth/logout`。

## 8. 配置项

配置通过环境变量设置。未设置时使用下表默认值。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_SESSION_SECRET` | 自动生成并保存 | Flask Session 签名密钥，生产环境建议显式设置强随机值 |
| `SESSION_COOKIE_SECURE` | `0` | 使用 HTTPS 时设置为 `1` |
| `BUYIN_HEADLESS` | `1` | 百应浏览器默认后台运行 |
| `BUYIN_DIRECT_MATERIAL` | `1` | 优先复用已捕获的商品列表请求直接获取数据 |
| `CAPTURE_NETWORK` | `0` | 设置为 `1` 后保存指定网络响应调试数据 |
| `COLLECT_LIMIT` | `90` | 未从请求传入采集数量时的默认值 |
| `MATERIAL_RESPONSE_TIMEOUT_MS` | `15000` | 商品列表请求超时时间 |
| `DETAIL_PAGE_TIMEOUT_MS` | `15000` | 商品详情页超时时间 |
| `DETAIL_SETTLE_MS` | `4000` | 详情页加载后的稳定等待时间 |
| `CONTACT_HOVER_SETTLE_MS` | `1000` | 联系商家区域悬停后的等待时间 |
| `CONTACT_RESPONSE_TIMEOUT_MS` | `2000` | 联系方式接口超时时间 |
| `CONTACT_REQUEST_GAP_MS` | `1200` | 联系方式请求最小间隔 |
| `DETAIL_CONCURRENCY` | `1` | 同时处理的详情页数量，最小值为 1 |

示例：

```powershell
$env:APP_SESSION_SECRET = "replace-with-a-long-random-secret"
$env:SESSION_COOKIE_SECURE = "1"
$env:DETAIL_CONCURRENCY = "2"
python python_login_app.py
```

## 9. 运行时数据

`data/` 已加入 `.gitignore`，以下文件只应保存在服务器本地：

| 路径 | 说明 |
| --- | --- |
| `data/auth.db` | 系统账号、密码哈希和管理员会话锁 |
| `data/history.db` | SQLite 历史采集数据，使用 WAL 模式 |
| `data/.session-secret` | Flask Session 密钥，未配置环境变量时自动生成 |
| `data/buyin-browser-profile/` | Playwright 持久化浏览器 Profile 和百应登录态 |
| `data/buyin-authenticated.marker` | 百应登录成功标记 |
| `data/material_list_request.json` | 捕获的商品列表请求模板 |
| `data/material_list_response.json` | 最近一次商品列表摘要 |
| `data/material_list_pages.json` | 最近一次采集使用的商品列表分页数据 |
| `data/selection_results.json` | 最近一次采集结果快照 |
| `data/selection_skipped.json` | 被跳过商品及原因 |
| `data/network_capture.jsonl` | 开启网络捕获后保存的调试响应 |
| `data/debug_detail_first_skipped.png` | 详情页调试截图 |
| `data/app.log` | Windows 管理脚本的标准输出日志 |
| `data/app-error.log` | Windows 管理脚本的错误日志 |

`history_store.py` 会在历史数据库为空且 `data/selection_results.json` 存在时执行一次迁移。

## 10. 数据库表结构

系统使用两个 SQLite 数据库，均位于 `data/` 目录，数据库之间没有外键关系：

- `data/history.db`：采集历史数据。
- `data/auth.db`：系统账号和管理员单会话锁。

### 10.1 `history_records`

历史记录表由 `history_store.py` 创建，当前结构如下：

```sql
CREATE TABLE history_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    commodity_id TEXT,
    product_id TEXT,
    shop_id TEXT,
    merchant_product_id TEXT,
    mobile TEXT,
    name TEXT,
    image_url TEXT,
    shop_name TEXT,
    shop_score TEXT,
    month_sale TEXT,
    detail_url TEXT,
    raw_json TEXT NOT NULL
);
```

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | 主键、自增 | 历史记录内部 ID，删除接口使用该字段 |
| `collection_id` | `TEXT` | `NOT NULL` | 采集批次 ID，同一任务的记录共用一个值 |
| `collected_at` | `TEXT` | `NOT NULL` | 采集时间，保存为 ISO 8601 字符串 |
| `commodity_id` | `TEXT` | 可空 | 百应商品/商品实体 ID |
| `product_id` | `TEXT` | 可空 | 商品 ID |
| `shop_id` | `TEXT` | 可空 | 店铺 ID，用于跳过历史店铺 |
| `merchant_product_id` | `TEXT` | 可空 | 商家商品 ID |
| `mobile` | `TEXT` | 可空 | 商家手机号 |
| `name` | `TEXT` | 可空 | 商品名称 |
| `image_url` | `TEXT` | 可空 | 商品图片地址 |
| `shop_name` | `TEXT` | 可空 | 店铺名称 |
| `shop_score` | `TEXT` | 可空 | 店铺评分，查询时转换为数字比较 |
| `month_sale` | `TEXT` | 可空 | 月销，查询时转换为数字比较 |
| `detail_url` | `TEXT` | 可空 | 商品详情页地址 |
| `raw_json` | `TEXT` | `NOT NULL` | 商品原始结构化数据 JSON |

当前索引：

```sql
CREATE INDEX idx_history_collected_at
    ON history_records(collected_at DESC);
CREATE INDEX idx_history_collection_id
    ON history_records(collection_id);
CREATE INDEX idx_history_product_id
    ON history_records(product_id);
CREATE INDEX idx_history_shop_id
    ON history_records(shop_id);
CREATE INDEX idx_history_merchant_product_id
    ON history_records(merchant_product_id);
```

说明：

- `shop_score` 和 `month_sale` 为文本字段，以兼容百应返回的原始格式；筛选时使用 `CAST(... AS REAL)` 转换。
- `raw_json` 保存完整采集对象，列表接口会用结构化字段覆盖或补充展示字段。
- `collection_id` 由 `HistoryStore.start_collection()` 使用 UUID 生成，不建立数据库外键。
- 老版本数据库启动时会自动补齐缺失的 `shop_score` 和 `mobile` 字段。

### 10.2 `users`

账号表由 `auth_store.py` 创建：

```sql
CREATE TABLE users (
    username TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `username` | `TEXT` | 主键 | 登录账号，`admin` 为保留管理员账号 |
| `display_name` | `TEXT` | `NOT NULL`，默认空字符串 | 用户中文名，用于账号列表和页面展示 |
| `password_hash` | `TEXT` | `NOT NULL` | Werkzeug 生成的密码哈希，不保存明文密码 |
| `role` | `TEXT` | `NOT NULL`，默认 `user` | 当前使用 `admin` 或 `user` |
| `active` | `INTEGER` | `NOT NULL`，默认 `1` | 是否允许登录，`1` 表示启用 |
| `created_at` | `TEXT` | `NOT NULL` | 创建时间，ISO 8601 字符串 |
| `updated_at` | `TEXT` | `NOT NULL` | 最近一次密码修改时间 |

当前实现只允许管理员创建普通用户、修改密码和删除普通用户，不能删除 `admin`。旧版 `users` 表启动时会自动补充 `display_name`，已有账号缺少中文名时使用登录账号作为展示名。

### 10.3 `admin_session`

管理员单会话和采集任务锁使用单行表：

```sql
CREATE TABLE admin_session (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    session_id TEXT NOT NULL,
    task_active INTEGER NOT NULL DEFAULT 0,
    task_session_id TEXT,
    updated_at TEXT NOT NULL
);
```

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | 主键，必须为 `1` | 保证表中只有一个管理员会话槽位 |
| `session_id` | `TEXT` | `NOT NULL` | 当前管理员的系统 Session ID |
| `task_active` | `INTEGER` | `NOT NULL`，默认 `0` | 是否正在执行采集任务 |
| `task_session_id` | `TEXT` | 可空 | 发起当前采集任务的管理员 Session ID |
| `updated_at` | `TEXT` | `NOT NULL` | 会话或任务状态最近更新时间 |

管理员登录时会覆盖空闲的会话槽位；如果 `task_active=1`，新的管理员登录会被拒绝。应用启动时会把遗留的 `task_active` 重置为 `0`，避免进程异常退出后永久锁住管理员账号。

## 11. Linux 公网部署

应用当前默认监听 `127.0.0.1:8000`，推荐使用 Nginx 或其他反向代理对外提供 HTTPS，而不是直接暴露 Flask 开发服务器。

基本部署步骤：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python python_login_app.py
```

生产环境建议：

- 使用 systemd、Supervisor 或容器管理进程。
- 反向代理启用 HTTPS，并设置 `SESSION_COOKIE_SECURE=1`。
- 使用强随机 `APP_SESSION_SECRET`。
- 首次登录后立即修改 `admin` 默认密码。
- 限制 `data/` 目录权限，尤其是 `auth.db`、`.session-secret` 和浏览器 Profile。
- 反向代理层增加访问认证、IP 白名单或 VPN，避免未授权访问系统控制台。
- 不要启动多个应用进程。当前 Playwright 浏览器上下文、采集任务锁和页面状态属于单进程状态。
- 定期备份 `data/auth.db` 和 `data/history.db`，备份时保留 SQLite WAL 文件一致性。

## 12. 开发说明

### 12.1 主要模块

- `python_login_app.py`
  - Flask 路由和权限拦截。
  - 系统登录、管理员会话接管和采集任务锁。
  - Playwright 浏览器生命周期。
  - 百应扫码登录、二维码截图和选品页控制。
  - 商品列表请求捕获、直请求、翻页和详情页采集。
  - 控制台和历史数据页面模板。
- `auth_store.py`
  - 用户表和管理员单会话表。
  - 密码哈希校验、创建用户、修改密码。
- `history_store.py`
  - SQLite 初始化、历史记录写入、查询、分页、统计和删除。

### 12.2 采集线程约束

Playwright 使用同步 API，并在主程序初始化的线程中创建浏览器上下文。Flask 启动时使用单线程模式，修改为多线程或多进程时必须重新设计：

- 浏览器上下文的线程归属。
- 管理员任务锁的一致性。
- 多实例之间的百应登录态。
- 多实例之间的账号会话接管。

### 12.3 静态检查

项目没有独立的自动化测试套件。修改 Python 代码后可执行：

```powershell
python -m py_compile python_login_app.py auth_store.py history_store.py
git diff --check
```

按照项目约定，开发修改完成后不要求启动服务或执行 Maven 编译。

## 13. 常见问题

### 点击登录按钮没有弹出服务器浏览器

这是预期行为。公网部署时浏览器在服务器端无头运行，登录二维码通过 `/login-qr` 截取后显示在前端，用户使用手机扫码即可。

### 二维码无法识别

确认：

- 管理员已经登录系统。
- `/status` 显示百应登录页已打开。
- 浏览器能够访问 `buyin.jinritemai.com`。
- 反向代理没有缓存 `/login-qr`，接口已返回 `Cache-Control: no-store`。
- Playwright Chromium 已安装。

### 第二次采集提示没有捕获商品列表请求

程序会在采集任务前后清理销量筛选并刷新选品页。若仍失败，先确认当前页面是百应选品页，并查看 `data/app-error.log` 或程序标准输出中的 Playwright 错误。

### 管理员无法被新的管理员登录顶替

管理员执行采集任务时禁止顶替，这是设计行为。任务结束并完成选品页清理后，新的管理员登录才可以接管旧会话。程序异常退出后重新启动会清理遗留的任务锁。
