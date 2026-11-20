# 环评公示每日归档 · 续采与跨站复核 API

按 UTC 分钟区间对公示源做**有界续采**，把「已收齐的范围 + 公告」和「仍需续查的前沿区间」一起存进 SQLite。
每轮先核对日版号、最多检索 6 次，结果少于 5 条才算该段收齐；满额就二分缩区间，单分钟仍满则转人工补查。
发现更高版号会撤下当天旧版公告与覆盖结论，迟到的旧响应无法把它们恢复。

此外提供**跨站复核供数 API**（`/api/review`）：三个固定归档点按交件号提交按日查档 JSON，服务逐分钟
比对同一 UTC 日版，至少两站确认收齐且公告编号、标题、发布时间清单相同才签发复核号；复核号一旦失去
互证或清单变更立即失效，恢复互证重新发号，同一清单持续获两站支持时仅换支持站点可沿用原号。

## 快速开始

```bash
docker compose up --build
```

- 归档服务（含 Swagger）：http://localhost:8080/docs
- 本地公示源（含 Swagger）：http://localhost:8081/docs
- 续采演示脚本：`./scripts/demo.sh`
- 跨站复核演示脚本：`./scripts/demo_review.sh`

切换公示源数据版本（无需重建镜像）：

```bash
SOURCE_DATASET=v2 docker compose up --build
# 或对运行中的源热切换：
curl -X POST localhost:8081/v1/admin/version -H 'content-type: application/json' -d '{"version":"v2"}'
```

本地不用 Docker 时：

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
SOURCE_DATA_DIR=./datasets .venv/bin/python -m uvicorn source.main:app --port 8081 &
SOURCE_BASE_URL=http://localhost:8081 ARCHIVE_DB_PATH=./archive.sqlite3 \
  .venv/bin/python -m uvicorn app.main:app --port 8080
```

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/collection/rounds` | 跑一轮续采：核对版号 → 检索至多 6 次 |
| GET | `/api/days/{day}` | 按日查询公告、已收齐范围、待查范围、需人工补查范围 |
| GET | `/api/days/{day}/version` | 归档中该日的日版号 |
| POST | `/api/review/days/{day}/submissions` | 复核供数：站点按交件号提交按日查档 JSON |
| POST | `/api/review/days/{day}/evaluation` | 把逐分钟复核推进到指定分钟（缺省按服务时钟） |
| GET | `/api/review/days/{day}/minutes` | 按日版查各分钟复核号与支持站点（时间线） |
| GET | `/api/review/days/{day}/minutes/{minute}` | 查某一分钟的复核结论 |
| GET | `/api/review/numbers/{review_no}` | 凭有效复核号取交叉核实过的公告 |
| GET | `/health` | 健康检查 |

公示源（模拟上游）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/days/{day}/version` | 当日版号 |
| GET | `/v1/days/{day}/notices?start_minute=&end_minute=` | 分钟区间检索，最多 5 条，**无总数** |
| GET/POST | `/v1/admin/version` | 查看/切换数据集版本 |
| GET/POST | `/v1/admin/delay` | 注入响应延迟，用于演示迟到响应 |

### 一轮续采的返回

```json
{
  "day": "2026-11-20",
  "status": "partial",
  "day_version": "2026-11-20.v1",
  "queries_used": 2,
  "version_reset": false,
  "collected_ranges": [{"start_minute": 540, "end_minute": 584, "from_time": "09:00", "to_time": "09:44"}],
  "manual_ranges": [],
  "pending_ranges": [{"start_minute": 585, "end_minute": 1439, "from_time": "09:45", "to_time": "23:59"}],
  "notices_collected": 4,
  "detail": null
}
```

`status` 取值：`collected`（当天已全部收齐）、`partial`（本轮预算用尽，仍有待查区间）、
`needs_manual_review`（只剩人工补查时段）、`source_error`（源不可用，可原样重试）、
`stale_response`（收到旧版迟到响应，已丢弃）、`version_changed`（检索中发现更高版号，已撤旧版）。

`POST /api/collection/rounds` 的 `max_queries` 可把单轮预算压到 1..6；服务上限固定为 6。

### 按日取数

`GET /api/days/2026-11-20` 返回 `day_version`、`complete`、`notices`、`completed_ranges`、
`pending_ranges`（待查）、`manual_ranges`（需人工补查，带 `reason`）。
`complete=true` 要求既无待查区间、也无人工补查区间。

## 跨站复核供数

三个固定归档点各自提交同一 UTC 日的按日查档 JSON，分析库只凭**复核号**取交叉核实过的公告。

### 交件受理规则（`POST /api/review/days/{day}/submissions`）

请求体：`station`（站点）、`submission_no`（交件号）、`complete`（是否确认当日清单收齐，
空清单收齐也传 `true`）、`notices`（现有按日查档 JSON 的公告数组，字段为
`announcement_no` / `title` / `published_at`）。

- **同站同日同版，按交件号整份更新**：更新交件号自然序更大（数字按数值比较，如 `S-10` 新于 `S-9`）时，
  用新 JSON 整份替换该站当日交付。
- **拒收旧号**：交件号小于该站已受理号 → `409 {"kind":"stale_no"}`，原状不变。
- **拒收同号改件**：交件号相同但内容变了 → `409 {"kind":"changed_payload"}`。
- **原样重送不重复受理**：交件号与内容（行序无关）都相同 → 返回 `accepted=false`，不重放、不重复受理。
- 受理与随后的逐分钟评估在**同一 SQLite 事务**内生效；失败回滚，保留原状。

### 逐分钟复核与复核号

每分钟是一份**不可变快照**：交件只落定「已结束」的分钟（真实时钟下为当前分钟之前），
当前开放分钟通过 `POST .../evaluation`（带 `minute`）显式落定；历史分钟不会被后来的交件改写。

- 至少两个 `complete=true` 的站点，其公告**清单集合**（编号、标题、发布时间三元组，行序不影响）相同，
  该分钟记为 `active` 并签发/沿用复核号，响应给出 `review_no` 与 `supporting_stations`。
- 两站都报收齐的**空清单**同样签发复核号。
- 收齐站点不足两个 → `insufficient_evidence`（证据不足）；都收齐但清单两两不一致 →
  `disagreement`（有分歧），两者在分钟视图与时间线中明确区分。
- 复核号形如 `RV-20261119-001`，按日顺序编号；同一清单持续获两站支持、仅支持站点变化时沿用原号。

### 取数与失效

- `GET /api/review/numbers/{review_no}`：仅当最新评估分钟仍以该号支持同一清单时返回核实后的公告；
  否则 `409` 并在 `latest_status` 标明当前是 `insufficient_evidence` 还是 `disagreement`。
- **失去互证或清单变更，旧号立即不能取数**：历史分钟仍保留当时的号与支持站点，但号已不可取数。
- **恢复互证要发新号**：中断后即便清单与之前完全相同，也签发新号；只有支持连续未中断、仅换支持站点
  才沿用原号。
- 所有交件、分钟结论、复核号与游标都在 SQLite（与续采库同文件、独立表），**重启后仍能凭有效复核号
  取数**，并从上次游标继续逐分钟评估。

### 分钟时间线

`GET /api/review/days/{day}/minutes` 返回每个已评估分钟的 `status`、`review_no`、
`supporting_stations` 与 `detail`；单分钟用 `GET .../minutes/{minute}`，未评估的分钟返回 404。

## 规则与状态机

1. **先核对版号**：每轮开始先查源上的日版号；与归档不同则先处理版本（见下），再决定检索范围。
2. **有界检索**：每轮最多 6 次检索，不论待查区间有多少。
3. **少于 5 条即收齐**：整个被检索区间（可能跨多分钟）一次性标记为收齐并落库。
4. **满额缩区间**：返回 5 条时把区间二分，两个半段进「前沿」队列；下一轮从队列头部继续。
5. **单分钟仍满 → 人工**：区间已是一分钟且返回 5 条，记入 `manual_ranges`，不再重复检索。
6. **失败可重试**：版号查询或检索失败时，已完成的进度保留，未完成区间仍在队列里，重跑即可。
7. **重启接着查**：前沿队列与已收齐范围都在 SQLite，进程重启后从队首继续，不会整天重扫。

### 版本升级与迟到响应

日版号按「后缀数字优先，否则字符串序」比较（本地源形如 `2026-11-20.1`）。

- 预检发现更高版号：删除当天 `notices` / `completed_ranges` / `manual_ranges` / `frontier_ranges`，换新版号后重新排空整天。
- 检索响应自带更高版号：同一次事务内撤下旧数据，结束本轮（`version_changed`）。
- 检索响应带更低版号（迟到旧响应）：整页丢弃（`stale_response`），不写公告、不改覆盖范围。
- 写入事务内还会再核对一次版号，所以「取数后、落库前」被别的轮次抢先升级也不会写脏数据。

## 目录结构

```
app/          归档服务：collector 状态机、SQLite 存储、源客户端、FastAPI 路由
app/review/   跨站复核：交件受理规则、逐分钟评估状态机、复核号签发/失效、复核路由
common/       版本比较、UTC 时间解析等共享工具
source/       本地可换版公示源（内存数据集 + 管理端点）
datasets/     v1.json / v2.json 演示数据
tests/        单元 + 端到端 + 迟到响应竞态 + 复核状态机/API 测试
scripts/      demo.sh 续采演示、demo_review.sh 复核演示
```

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

覆盖：稀疏日一轮收齐、全天空日、密集日二分收敛、单分钟转人工、每轮预算上限、
版号预检/检索中发现升级、迟到旧响应丢弃、事务内版号竞态、源故障后重试、
进程重启续查、API 契约与 Swagger、真实延迟下的迟到响应竞态；
以及复核侧的清单规范化/哈希、交件号受理规则、逐分钟两站共识（含空清单与行序无关）、
证据不足与分歧区分、复核号失效与重新签发、换支持站点沿用原号、失败原子性与重启取数。
