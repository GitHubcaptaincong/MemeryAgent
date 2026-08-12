# 低成本公开演示部署

目标架构：

- GitHub Pages：公开、脱敏、只读的 H5 演示
- CloudBase 云托管：FastAPI 后端和微信小程序 API
- Neon Free：PostgreSQL 持久化数据库
- 本机 CLIProxy + cpolar：仅在需要真实生成时提供模型能力

## 1. GitHub Pages 公开演示

仓库已包含 `.github/workflows/pages.yml`。工作流在 `main` 分支的前端文件发生变化时：

1. 安装 `frontend` 依赖；
2. 根据仓库名生成 Vite `base`；
3. 使用 `VITE_PUBLIC_DEMO=true` 构建；
4. 将 `frontend/dist` 发布到 GitHub Pages。

首次启用：

1. 打开 GitHub 仓库的 `Settings -> Pages`；
2. 将 `Build and deployment -> Source` 选择为 `GitHub Actions`；
3. 推送工作流后，在 `Actions` 页面等待 `Deploy public demo to GitHub Pages` 完成；
4. 访问 `https://githubcaptaincong.github.io/MemeryAgent/`。

公开构建不会读取 `VITE_API_BASE_URL`，不会请求 CloudBase、Neon 或 CLIProxy。整理、复习和提醒页使用脱敏示例数据；复习操作只修改当前浏览器内存，刷新后恢复。

## 2. Neon PostgreSQL

1. 创建 Neon Free 项目，优先选择 Singapore 区域；
2. 使用 Neon 控制台生成独立的强密码；
3. 在 `Connect` 页面复制连接串；
4. 原始 `postgresql://` 连接串可以直接填写，后端会自动选择已经安装的 psycopg 3 驱动：

```env
APP_DATABASE_URL=postgresql://<user>:<url-encoded-password>@<host>/<database>?sslmode=require
```

不要把连接串写进 GitHub 仓库、小程序或前端环境变量。只在 CloudBase 服务环境变量中保存。

如果使用旧版本镜像，必须手动把协议写成 `postgresql+psycopg://`；否则 SQLAlchemy 会把未指定驱动的 `postgresql://` 解释为 psycopg2，并出现 `ModuleNotFoundError: No module named 'psycopg2'`。

## GitHub Pages 首次启用

`actions/configure-pages` 只能读取已经启用的 Pages 站点。仓库第一次发布前，必须在 GitHub 打开：

```text
Settings -> Pages -> Build and deployment -> Source -> GitHub Actions
```

如果工作流在 `Configure GitHub Pages` 步骤报告 `Get Pages site failed: Not Found`，说明这项一次性设置尚未完成。设置后在失败的 Action 页面选择 `Re-run all jobs` 即可，不需要创建 Personal Access Token，也不需要给工作流额外密钥。

当前 Alembic 首次迁移会创建 `vector` 和 `pg_trgm` 扩展以及业务表。首次部署必须检查容器日志，确认 `alembic upgrade head` 成功；然后依次验证：

```text
GET  /api/v1/health
POST /api/v1/sources
POST /api/v1/runs
GET  /api/v1/review/overview
```

MVP 阶段建议 CloudBase 最大实例数先设为 1，避免多个新实例同时执行数据库迁移。后续再把迁移从容器启动命令拆成独立发布步骤。

## 3. CloudBase 后端环境变量

基础配置：

```env
APP_ENV=production
APP_DATABASE_URL=<Neon connection string>
APP_AUTO_CREATE_SCHEMA=false
APP_INLINE_WORKER=true
APP_CORS_ORIGINS=https://githubcaptaincong.github.io

APP_MODEL_PROVIDER=cli_proxy
APP_MODEL_BASE_URL=https://<current-cpolar-host>/v1
APP_MODEL_API_KEY=<strong-secret>
APP_MODEL_NAME=gpt-5.4-mini
APP_MODEL_REASONING_EFFORT=low
APP_MODEL_VERIFY_SSL=true
APP_MODEL_TIMEOUT_SECONDS=180
APP_AGENT_MAX_SECONDS=600
APP_SKILL_ROOT=/skills
```

`APP_CORS_ORIGINS` 使用英文逗号分隔多个来源，不能填写路径：

```env
APP_CORS_ORIGINS=https://githubcaptaincong.github.io,https://example.com
```

目前 GitHub Pages 是纯演示模式，不依赖这个 CORS 配置；当 H5 增加登录并切换到真实 API 后才会使用。

## 4. 发布边界

- GitHub Pages：可以长期公开，只有示例数据。
- 微信小程序：在 OpenID 隔离完成前只供本人测试。
- CloudBase API：在访客鉴权和限流完成前不要直接提供给陌生人写入。
- 本机模型：本机离线不影响 GitHub Pages 演示；真实生成暂时不可用。
- Neon：定期执行 `pg_dump` 保存可恢复备份，免费服务不代替项目自己的备份策略。

## 5. 本地复现公开构建

PowerShell：

```powershell
$env:VITE_PUBLIC_DEMO='true'
$env:VITE_BASE_PATH='/MemeryAgent/'
npm --prefix .\frontend run build
```

构建产物位于 `frontend/dist`。
