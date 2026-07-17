# tiktok-ai-service

## 一键启动和停止

首次使用先运行 `install.ps1` 安装 Python 环境和 Playwright 浏览器。之后直接双击 `start.bat` 启动，程序会在后台启动前后端并自动打开 `http://127.0.0.1:8000`。

双击 `stop.bat` 停止程序，双击 `restart.bat` 重启程序。也可以在 PowerShell 中执行：

`powershell -ExecutionPolicy Bypass -File .\manage.ps1 status`

运行日志在 `data\app.log`，错误日志在 `data\app-error.log`。

## 历史数据存储

采集结果现在会追加保存到 `data/history.db`（SQLite），不会随着历史记录增长而每次读写整个 JSON 文件。数据库使用 WAL 模式，并为采集时间、商品、店铺和商家商品 ID 建立索引，适合存储至少十万条结构化记录。

现有的 `data/selection_results.json` 会在数据库为空时自动迁移一次；该 JSON 文件仍作为当前批次的快照保留。结果接口默认分页返回 100 条：

`GET /api/results?limit=100&offset=0`

也支持通过 `q` 搜索名称、店铺名或 ID，以及通过 `collection_id` 筛选某次采集。`GET /api/history` 可查看数据库中的记录总数。
抖音评价有礼ai邀评程序
