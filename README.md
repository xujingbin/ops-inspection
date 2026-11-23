# MVP-01 主机巡检最小闭环（CPU / 内存 / 磁盘）

只做一条能真正跑起来的巡检链路：登记主机 → 手动执行三条内置规则 → 结果落库 → 结果页与历史记录。

技术栈：Python 3.11 标准库（`http.server` + `sqlite3`），**零第三方依赖**。

## 启动

环境要求：Python 3.11+，零第三方依赖（不需要 pip install）。

推荐用脚本启动，脚本会先检查 Python 版本和端口占用：

```bash
./start.sh                  # 前台运行，Ctrl+C 退出
./start.sh --port 8001      # 换端口
./start.sh --db /tmp/x.db   # 指定数据库
./start.sh --daemon         # 后台运行：日志 server.log，PID server.pid
```

也可以直接运行：

```bash
python3 app.py              # 默认端口：优先取环境变量 PORT，未设置时 8000
python3 app.py --port 8000  # 显式指定端口，优先级高于 PORT
```

启动后打开 http://localhost:8000 （端口按上一条规则确认），用健康检查验证：

```bash
curl http://localhost:8000/health
# {"status": "ok", "hosts": 0}
```

端口约定与常见问题：

- 默认监听 `0.0.0.0:8000`；如果运行环境设置了 `PORT`（云工作区、容器平台常见），不传 `--port` 时使用 `PORT`，因为平台通常只转发这个端口。显式传 `--port` 以参数为准。
- 配置优先级：命令行参数 > 环境变量 > `.env` > 默认值；`.env` 里的 `HOST` / `PORT` / `DB` 同样生效（同名环境变量已存在时以环境变量为准）。
- 端口被占用时程序会直接提示换端口，不再抛 traceback。
- 页面打不开时先看启动日志里的「主机巡检已启动」端口，再用上面的 `curl` 验证；后台模式日志在 `server.log`。
- 停止后台服务：`kill $(cat server.pid)`。

数据库默认落在 `inspection.db`，可用 `--db /path/to.db` 指定。

### 鉴权与数据源配置

部分版本开启了启动鉴权：缺少 `OPS_AUTH_PASSWORD`（HTTP Basic）或 `OPS_AUTH_TOKEN`（Bearer）时会直接拒绝启动，提示：

```
error: 必须配置 OPS_AUTH_PASSWORD（HTTP Basic）或 OPS_AUTH_TOKEN（Bearer），服务拒绝在未鉴权状态下启动
```

用环境变量或 `.env` 提供（`start.sh` 默认加载仓库下的 `.env`，也支持 `--env-file PATH`）：

```bash
cp .env.example .env    # 编辑 .env 填入真实值，.env 已被 git 忽略
./start.sh              # 或临时: OPS_AUTH_PASSWORD=xxx ./start.sh
```

- 不要把密码写成命令行参数（`ps` 可见），也不要提交进仓库。
- `warning: OPS_DATASOURCE_KEY is not set` 只影响「保存数据源密码」，不影响服务启动；需要该能力时在 `.env` 里补上。
- 鉴权开启后，浏览器访问会要求输入账号密码，`curl` 需带 `-u user:pass` 或 `-H "Authorization: Bearer <token>"`。

## 功能范围

| 能力 | 说明 |
|---|---|
| 主机对象 | 新增 / 列表：名称、hostname、环境、团队 |
| 内置规则 | CPU 使用率、内存使用率、磁盘使用率，各三档阈值 |
| 规则阈值配置 | `/rules` 页面在线改正常上限/异常下界与启停，存在 `rules` 表，改完立即生效 |
| 手动执行 | 逐项采集 → 判定 → 落库，单项失败只影响该项 |
| 结果页 | 检查项数、正常/警告/异常/未知数量、通过率、逐项实际值与阈值、说明 |
| 历史记录 | 最近 20 次执行，可回看每次逐项结果 |

## 阈值（左闭右开）

| 规则 | 正常 | 警告 | 异常 |
|---|---|---|---|
| CPU 使用率 `R-INFRA-001` | < 75% | 75% ~ 90% | ≥ 90% |
| 内存使用率 `R-INFRA-002` | < 80% | 80% ~ 90% | ≥ 90% |
| 磁盘使用率 `R-INFRA-003` | < 80% | 80% ~ 90% | ≥ 90% |

边界语义有单测覆盖：`74.9 → NORMAL`、`75 → WARNING`、`89.9 → WARNING`、`90 → CRITICAL`。

阈值默认值随建库写入 `rules` 表（`INSERT OR IGNORE`，可重复执行），之后以表里的值为准，可在 `/rules` 页面直接修改；约束 `0 <= 正常上限 < 异常下界 <= 100`，非法值返回 400 并提示原因。

## 结果状态语义

- `NORMAL` 正常 / `WARNING` 警告 / `CRITICAL` 异常：实际值落在对应区间。
- `UNKNOWN` 未知：采集抛异常、返回空值、返回值不是数值、未注册采集器。**记录原始原因**，绝不默认正常。
- 单个检查项失败不影响整次任务：任务仍会完成，其它项照常判定与落库。
- 通过率 = 正常 / (正常 + 警告 + 异常)。`UNKNOWN` 不进分子也不进分母；全部未知时通过率为 `—`。

## 采集实现

只读本机，不依赖 agent：

- CPU：读 `/proc/stat` 两次，按区间增量算忙碌占比。
- 内存：`/proc/meminfo` 的 `MemTotal` 与 `MemAvailable`。
- 磁盘：`os.statvfs("/")` 的可用块占比。

采集器通过 `core.execute_run(..., collectors={...})` 注入，测试里用假采集器构造异常与空值场景。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖三块：

- `tests/test_core.py`：三档阈值边界、采集异常/空值/非数值/缺采集器 → UNKNOWN、UNKNOWN 不计入通过率、真实采集器取值区间。
- 规则配置：建库种子幂等、阈值更新与启停、非法阈值（正常上限 ≥ 异常下界、超出 0~100、非数字、规则不存在）→ 400、执行时使用库里的阈值、全部停用时拒绝执行。
- `tests/test_store.py`：主机增查与必填校验、执行记录与逐项结果的 SQLite 往返、按时间倒序、按主机过滤、全 UNKNOWN 时通过率落 NULL。
- `tests/test_app.py`：真实起 HTTP 服务，覆盖首页、新增主机（含 400 校验）、执行巡检跳转与详情页内容、`/health`、404。

## 目录

```
app.py                 HTTP 路由与页面渲染（/、/hosts、/rules、/runs、/runs/{id}、/health）
core.py                规则、三档阈值判定、采集器、单次执行
store.py               SQLite 表结构（hosts / rules / runs / run_items）与读写
templates/index.html   首页（主机 / 执行 / 历史）
templates/detail.html  执行结果页
templates/rules.html   规则阈值配置页
tests/                 单元测试与 HTTP 测试
```

## 不在本版范围

定时调度、报告生成与导出、消息通知、整改闭环、K8s/中间件/接口等多类型巡检对象。这些属于后续需求，本版只保证主机巡检这条主链路端到端可用。
