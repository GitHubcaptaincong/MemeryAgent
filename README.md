# Memory Agent

一个把学习材料转化为可验证知识单元，并通过主动回忆和间隔重复帮助用户持续复习的 AI 学习助手。

## 项目介绍

收藏文章、记录笔记并不等于真正掌握。很多学习工具停留在“保存内容”或“生成摘要”，用户仍然缺少可以主动回答的问题、可信的原文依据，以及明确的下一次复习时间。

Memory Agent 围绕这个问题构建了一条完整学习闭环：

```text
输入学习材料
  → Agent 拆分知识单元并定位原文证据
  → 用户审阅和确认
  → 开放题主动回忆
  → AI 提供覆盖点与缺失点建议
  → 用户最终自评
  → FSRS 安排下一次复习
```

项目不是通用聊天机器人。Agent 负责在受约束的工具流程中整理材料，生成结果必须经过结构校验并保留原文证据；未经用户确认的知识单元不会进入复习队列，AI 建议也不会覆盖用户对掌握程度的最终判断。

## 核心能力

### 材料整理与知识生成

- 支持文本和 Markdown 材料，单次最多 10,000 字。
- 原文先持久化，再启动后台 AI 整理，模型等待或重试不会丢失用户输入。
- 使用有预算的 Agent 流程完成材料读取、Skill 路由、证据定位、草稿生成和结构校验。
- 将材料拆分为 1–10 个可独立复习的开放问答知识单元。
- 由服务端 `source_locate_quotes` 工具计算权威字符区间，避免让模型自行猜测证据坐标。
- 600 字以内材料默认使用快速通道，减少不必要的多轮模型调用。
- 草稿确认与长期个性化记忆审批分离，Agent 只能读取已批准且仍有效的记忆。

### 主动回忆与复习调度

- 草稿确认后幂等创建复习卡，并按到期时间生成复习队列。
- 支持开放题作答、答案要点和原文证据对照，以及 1–4 档用户自评。
- 使用官方 `py-fsrs 6.3.1` 计算下一次复习时间，并在评分前展示四档调度预览。
- 回答和评分以不可变 `review_events` 保存，当前卡片状态可由历史事件重放。
- 旧调度状态迁移时保留原到期时间；历史不完整时拒绝静默重置。
- 提供自评趋势、可解释薄弱项和未来 14 天复习负载建议。

### AI 回答评估与提醒

- 回答提交后异步生成覆盖点、缺失点和评分建议，不阻塞用户继续评分。
- 用户评分是 FSRS 的唯一最终输入，AI 评估失败时可降级为纯自评流程。
- 支持提醒时间、每日建议上限、逾期策略和时区等偏好。
- 实现微信一次性订阅授权额度、提醒任务领取、发送去重和结果回写。

### 客户端

- 原生微信小程序：快速记录、Agent 进度、草稿确认、主动复习、学习统计和提醒设置。
- Vue 3 移动 H5：复用小程序的信息架构，便于浏览器调试和独立部署。
- Agent 运行期间展示可审计的计划、动作、工具结果和恢复原因，不展示或伪造模型隐式思维链。

### 可靠性与恢复

- 持久化 Agent Run、事件流、工具调用、后台任务、Checkpoint 和草稿证据。
- SSE 持续推送运行状态；没有新业务事件时发送临时进度脉冲。
- 仅对网络异常、超时、HTTP 408/409/425/429 和 5xx 执行自动重试。
- Worker 使用数据库租约领取任务，并可接管过期运行。
- 自动恢复采用 `fresh_context_replay`，从已持久化的业务输入、Skills、已批准记忆和预算重新执行。
- 工具调用和关键写入使用幂等键，避免重试产生重复副作用。

## 技术栈

| 层次 | 技术 |
| --- | --- |
| 微信客户端 | 原生微信小程序 |
| Web 客户端 | Vue 3、Vite |
| 后端 | Python 3.12、FastAPI、SQLAlchemy、Pydantic |
| 数据库 | PostgreSQL、pgvector、pg_trgm；测试可使用 SQLite |
| AI 接入 | OpenAI-compatible Responses API；当前实现包含 CLIProxy 适配器和 Fake Adapter |
| 复习调度 | py-fsrs 6.3.1 |
| 任务与事件 | PostgreSQL 任务租约、SSE、不可变业务事件 |
| 工程化 | Alembic、Pytest、Docker Compose |

## 快速启动

推荐使用 Docker Compose 启动 API、Worker、PostgreSQL 和移动 H5。

### 前置条件

- Docker Desktop
- 一个可访问的 OpenAI-compatible Responses API 服务

复制环境变量模板并填写本地模型服务的访问凭证：

```powershell
Copy-Item .env.example .env
# 编辑 .env 中的 CLI_PROXY_API_KEY，以及需要覆盖的模型配置。
```

默认 Compose 配置通过 `http://host.docker.internal:8317/v1` 从容器访问宿主机模型服务。可以在 `compose.yml` 或自己的部署环境中替换为其他可访问地址；不要提交真实密钥。

```powershell
docker compose up --build
```

启动后访问：

- 移动 H5：http://localhost:5173
- API 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/api/v1/health
- 就绪检查：http://localhost:8000/api/v1/ready

停止服务但保留数据库：

```powershell
docker compose stop
```

## 本地开发

### 后端

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".\backend[dev]"
$env:APP_DATABASE_URL="sqlite+pysqlite:///./memory_agent.db"
$env:APP_AUTO_CREATE_SCHEMA="true"
$env:APP_MODEL_PROVIDER="cli_proxy"
$env:APP_MODEL_BASE_URL="http://127.0.0.1:8317/v1"
$env:APP_MODEL_API_KEY="<local-proxy-key>"
$env:APP_MODEL_NAME="<model-name>"
.\.venv\Scripts\memory-agent-api.exe
```

不接入真实模型时，可以将 `APP_MODEL_PROVIDER` 设置为 `fake`，用于本地流程调试和确定性回归测试。

### Web 客户端

```powershell
Set-Location .\frontend
npm install
npm run dev
```

### 微信小程序

在微信开发者工具中导入仓库根目录。公开配置使用 `touristappid`，本地 AppID 和接口地址应保存在个人配置中，不要提交模型 Key、数据库密码或微信 AppSecret。

具体导入和联调步骤见 [微信小程序说明](./miniprogram/README.md)。

## 部署说明

项目不绑定特定云厂商，可以按照以下边界选择自己的部署方式：

- FastAPI API 和 Worker 可以部署为容器或常驻进程，并连接 PostgreSQL。
- 部署前执行 Alembic migration，不要依赖开发环境的自动建表。
- Vue H5 执行 `npm run build` 后可以部署到任意静态站点或 Web Server。
- 微信小程序可以通过公网 HTTPS API 或平台提供的容器调用能力访问后端。
- 微信订阅提醒需要外部定时任务触发，不能依赖可能缩容或重启的 API 进程内定时器。
- 数据库连接、模型凭证、微信密钥和提醒共享密钥应通过环境变量或密钥服务提供。

完整环境变量示例见 [.env.example](./.env.example)。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest .\backend\tests -q
npm --prefix .\frontend run build
```

后端测试覆盖主要 API 闭环、证据定位、模型协议、任务恢复、FSRS 调度、回答评估、学习统计、微信身份隔离和提醒幂等。

## 项目结构

```text
backend/src/memory_agent/   FastAPI、Agent Runtime、工具、记忆、复习与任务队列
backend/migrations/         Alembic 数据库迁移
backend/tests/              后端自动化测试
frontend/src/               Vue 3 移动 H5
miniprogram/                原生微信小程序客户端
cloudfunctions/             微信订阅消息定时发送桥接
.agents/skills/             Agent 只读 Skills
docs/                       架构、可靠性、复习与部署边界文档
infra/postgres/             PostgreSQL 扩展初始化
```

## 当前边界

- 当前材料输入只支持文本和 Markdown，尚未接入网页链接、图片、PDF、音视频或真实外部搜索。
- PostgreSQL 已包含 pgvector/pg_trgm 数据基础，但 Embedding 生成和完整混合检索尚未接入。
- FSRS 使用版本化的通用配置，尚未根据个人历史数据训练参数。
- 微信订阅消息代码已实现，但真实发送仍需要模板、字段映射、定时任务和真机授权配置。
- `agent_events` 只保存可审计事件和决策摘要，不保存模型隐式推理链。
- 模型请求使用 `store=false`；Provider 临时状态不作为业务记忆或长期记忆保存。

更详细的设计与验证记录见 [docs](./docs/)。
