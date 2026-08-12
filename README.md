# Memory Agent MVP

当前 MVP 已跑通“学习材料 → Agent 整理 → 用户确认 → 主动回忆 → 用户自评 → 下次复习”的纵向闭环。

## 已实现

- 文本/Markdown 材料输入，单次最多 10,000 字。
- Python 3.12 + FastAPI + SQLAlchemy 后端。
- PostgreSQL + pgvector + pg_trgm 数据模型；SQLite 可用于本地无依赖测试。
- 持久化 Agent Run、事件流、工具调用、后台任务和草稿证据。
- 有预算的 Agent 循环：读取材料 → 定位逐字证据 → 生成草稿 → 结构校验。
- 原文引用由 `source_locate_quotes` 返回权威字符区间，模型不再自行数字符。
- 模型超时、限流和 5xx 自动进入 `retry_wait`；Worker 也可接管过期租约。
- 600 字以内默认走短内容快速通道：一次紧凑模型生成，由代码补全字段、定位证据并校验。
- 原文先持久化再启动 AI 整理；图形界面会立即显示“原文已记录”，模型等待不会阻塞记录结果。
- SSE 每 2 秒发送一次临时进度脉冲，界面展示当前动作、耗时、工具结果和重[.env](.env)试原因。
- 只读 Skill 路由与三个初始 Skills。
- SSE 实时运行轨迹。
- 1–10 个开放问答知识单元及来源字符区间。
- 草稿确认与长期记忆审批分离；只有批准后的候选才写入长期记忆。
- 草稿确认后幂等创建复习卡，到期队列按 `due_at` 拉取。
- 开放题作答、答案要点与原文证据对照、1–4 档用户自评。
- 回答与评分以不可变 `review_events` 保存，复习卡只作为当前调度投影。
- 已接入官方 `py-fsrs 6.3.1`；使用 1/10 分钟学习步骤、10 分钟重学步骤、0.9 目标记忆率，并关闭随机抖动以支持确定性重放。
- 评分前由服务端返回四档真实调度预览；评分后可查看最近复习、下一次到期和当前到期数量。
- 旧 MVP 卡片保留原到期时间，并在下一次评分时从不可变 `review_rated` 事件重放迁移到 FSRS；历史不完整时拒绝静默重置。
- 提醒偏好持久化：启用状态、每日时间、数量上限、逾期策略和时区。
- Vue 3 三页工作台：整理、复习、提醒。
- 原生微信小程序客户端：iOS 风格移动界面、快速记录、Agent 进度、复习作答与提醒设置；现有 Vue 端继续保留。

当前已支持通过 OpenAI-compatible Responses API 接入 CLIProxy，并保留 `FakeModelAdapter` 作为确定性回归基线。外部网页搜索、Embedding 生成、AI 语义判分、FSRS 个性化参数训练和浏览器/系统通知尚未接入。

## 一键启动（推荐）

前提：Docker Desktop 已启动。

先创建本地环境文件并填写代理 Key。`.env` 已被 Git 忽略，不要提交：

```powershell
Copy-Item .env.example .env
# 编辑 .env 中的 CLI_PROXY_API_KEY；不要把真实值提交到仓库
```

CLIProxy 的访问地址按运行位置区分：

- 后端直接运行在 Windows 主机时使用 `http://127.0.0.1:8317/v1`。
- 后端运行在 Docker 容器时使用 `http://host.docker.internal:8317/v1`，Compose 已内置该配置。
- 当前默认模型是 `gpt-5.4-mini`；可通过 `.env` 中的 `CLI_PROXY_MODEL` 切换为 `gpt-5.4` 或 `gpt-5.5`。

本机验证时请按运行位置选择上面的回环或 Docker 主机地址；不要把个人网络地址写入仓库。

```powershell
docker compose up --build
```

启动后访问：

- 工作台：http://localhost:5173
- API 文档：http://localhost:8000/docs
- API 健康检查：http://localhost:8000/api/v1/health

微信小程序请在微信开发者工具中导入仓库根目录。配置与真机联调说明见 [miniprogram/README.md](./miniprogram/README.md)。

公开仓库中的 `project.config.json` 使用 `touristappid`。首次导入后，请在微信开发者工具“详情 -> 基本信息”中填写自己的 AppID；开发者工具会将它保存到已被 Git 忽略的 `project.private.config.json`。

停止服务但保留数据库：

```powershell
docker compose stop
```

## 本地开发

后端：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".\backend[dev]"
$env:APP_DATABASE_URL="sqlite+pysqlite:///./memory_agent.db"
$env:APP_AUTO_CREATE_SCHEMA="true"
$env:APP_MODEL_PROVIDER="cli_proxy"
$env:APP_MODEL_BASE_URL="http://127.0.0.1:8317/v1"
$env:APP_MODEL_API_KEY="<local-proxy-key>"
$env:APP_MODEL_NAME="gpt-5.4-mini"
.\.venv\Scripts\memory-agent-api.exe
```

前端（另一个终端）：

```powershell
Set-Location .\frontend
npm install
npm run dev
```

## CloudBase 云托管部署

仓库根目录提供了云托管专用 `Dockerfile`。在 CloudBase 控制台通过本地代码部署时：

1. 代码包类型选择“文件夹”，上传整个项目根目录。
2. Dockerfile 目录留空或填写 `.`。
3. Dockerfile 名称填写 `Dockerfile`。
4. 服务端口填写 `8000`。
5. 健康检查路径填写 `/api/v1/health`。

必须在云托管服务中配置环境变量，不要写入代码：

```text
APP_DATABASE_URL=postgresql+psycopg://<user>:<password>@<host>:5432/<database>
APP_MODEL_PROVIDER=cli_proxy
APP_MODEL_BASE_URL=https://<cloud-accessible-model-service>/v1
APP_MODEL_API_KEY=<secret>
APP_MODEL_NAME=gpt-5.4-mini
APP_INLINE_WORKER=true
APP_AUTO_CREATE_SCHEMA=false
APP_SKILL_ROOT=/skills
```

当前本机 CLIProxy 地址不能从 CloudBase 容器访问。云端必须使用公网或同 VPC 可访问的模型服务地址；只想先验证部署时，可以临时设置 `APP_MODEL_PROVIDER=fake`，但这不会调用真实模型。

根目录 Dockerfile 会在启动时执行 `alembic upgrade head`，因此目标 PostgreSQL 必须在容器启动前可连接。`frontend/` 和 `miniprogram/` 不需要由这个容器提供；小程序只调用云托管暴露的 FastAPI 地址。

## GitHub Pages 公开演示与 Neon

仓库包含 GitHub Pages 自动部署工作流。`main` 分支更新前端后，会构建一个完全使用脱敏示例数据的只读演示版；该版本不会连接后端、数据库或模型服务。

首次发布时，在 GitHub 仓库 `Settings -> Pages` 中将 Source 选择为 `GitHub Actions`。本仓库的默认访问地址为：

```text
https://githubcaptaincong.github.io/MemeryAgent/
```

CloudBase 后端继续使用 PostgreSQL。低频个人展示可以使用 Neon Free，将 Neon 连接串仅保存到 CloudBase 的 `APP_DATABASE_URL` 环境变量。完整步骤、CORS 配置及上线边界见 [低成本公开演示部署](./docs/deployment-github-neon.md)。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest .\backend\tests -q
Set-Location .\frontend
npm run build
```

## 关键目录

```text
backend/src/memory_agent/   FastAPI、Agent Runtime、工具、记忆与任务队列
backend/migrations/         Alembic 数据库迁移
frontend/src/               Vue 整理、复习与提醒工作台
miniprogram/                原生微信小程序客户端
project.config.json         微信开发者工具项目配置
.agents/skills/             Agent 只读 Skills
docs/                       阶段一技术设计
infra/postgres/             PostgreSQL 扩展初始化
```

## 重要边界

- `agent_events` 保存可审计事件和决策摘要，不保存模型隐式思维链。
- “Agent 在做什么”展示的是可验证的计划、动作和工具摘要，不展示或伪造模型原始思维链。
- 用户业务知识、Agent 运行事件、长期个性化记忆是三类不同数据。
- Agent 只能读取已批准且有效的长期记忆。
- 草稿确认不会自动授权写长期记忆；记忆候选必须再次批准。
- Agent 运行时不能修改 Skills。
- 用户自评是当前复习调度的最终输入；AI 只可在后续提供建议，不能静默覆盖用户选择。
- 提醒页目前保存节奏偏好并展示到期队列，不代表关闭网页后会发送系统通知。
- 当前调度器是 `fsrs-6.3.1-v1`；调度配置单独版本化，后续升级必须通过历史事件重放，不能静默套用新参数。
- 模型请求使用 `store=false`；Provider 回放项只存在于本次运行的工作内存，原始推理项不写入事件、Checkpoint 或长期记忆。
- 自动恢复使用 `fresh_context_replay`：从已持久化的业务输入重新执行，保留事件、工具调用和 Token 预算，但不恢复模型隐式推理链。
- 只有网络异常、超时、HTTP 408/409/425/429 和 5xx 会自动重试；协议错误及普通 4xx 直接失败，避免无意义重放。
