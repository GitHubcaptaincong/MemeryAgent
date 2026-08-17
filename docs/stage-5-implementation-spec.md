# 阶段五实现说明：长文本与公开链接

状态：已完成本地实现与验证，尚未验证云端部署（2026-08-17）

## API

### 长文本

`POST /api/v1/sources`

- `content` 上限从 10,000 调整为 50,000 字。
- 仍支持 `text | markdown`。
- 成功后继续通过 `POST /api/v1/runs` 创建整理任务。

### 公开链接

`POST /api/v1/sources/from-url`

请求字段：

- `url`：必填，完整 HTTP/HTTPS 地址，最长 2,048 字符。
- `title`：可选；为空时优先使用网页标题。
- `learning_goal`：必填；客户端可提供默认值。
- `web_access_allowed`：是否允许额外外部检索，与读取该 URL 的权限分离。

成功后返回现有 `SourceRead`，新增：

- `origin_type`: `text | url`
- `origin_url`: 最终 URL
- `retrieved_at`: 抓取时间
- `origin_content_hash`: 原始 HTTP 响应 SHA-256

### 统一识别入口

`POST /api/v1/sources/resolve`

- `input` 是单个 HTTP/HTTPS URL 时，进入上述公开链接抓取流程。
- 其他输入均按长文本保存；包含在说明文字中的 URL 不会被自动打开。
- H5 与小程序只调用这个统一入口，旧的文本和 URL 接口继续保留兼容性。

## 抓取与解析

服务端模块 `source_ingestion.py` 负责：

1. URL 结构和账号信息校验。
2. DNS 解析与公网 IP 校验。
3. 固定到已校验 IP 建立 HTTP/TLS 连接。
4. 最多 5 次跳转，每次重新校验。
5. 默认 12 秒超时、2 MB 响应体上限。
6. HTML/纯文本正文提取和空白规范化。
7. 解析正文 50,000 字上限校验；不静默截断。

当前正文提取是服务端静态 HTML 解析，不执行 JavaScript，不发送 Cookie，也不绕过登录或访问限制。

## 数据变更

迁移 `0006_public_url_sources` 为 `sources` 增加：

- `origin_type`
- `origin_url`
- `retrieved_at`
- `origin_content_hash`

`draft_source_spans` 已有 `url` 和 `retrieved_at` 字段，不新增表。Agent 保存 URL Source 草稿时，将 Source 的最终 URL 与抓取时间写入每条证据。

第一版不保存完整原始 HTML 文件；`raw_content` 保存解析后、实际参与整理与证据定位的文本，`origin_content_hash` 用于识别抓取响应是否变化。

## 客户端

H5 与小程序整理页使用同一个输入框，并显示 50,000 字计数和自动识别提示。尚未接入的“允许外部检索”开关不再展示；定向读取用户提交的 URL 不依赖搜索工具。

Agent 任务轨迹在 H5 和小程序中均可滚动，最多保留客户端近期 50 条；进入等待确认、完成或失败状态时自动折叠，点击摘要可再次展开或收起。

公开只读演示仍不提交真实链接或用户数据。

## 错误模型

链接接口错误详情包含稳定 `code` 和用户可读 `message`。主要错误：

- `invalid_url`
- `url_credentials_not_allowed`
- `private_target_blocked`
- `dns_resolution_failed`
- `fetch_timeout`
- `fetch_failed`
- `too_many_redirects`
- `unsupported_content_type`
- `empty_extracted_content`
- `extracted_content_too_long`

网络瞬时错误返回 502，其余输入或能力边界错误返回 422。

## 验证要求

- 单元测试覆盖 HTML 提取、私网阻断、重定向复检。
- API 测试覆盖长文本保存、URL Source 元数据和草稿证据。
- 全量后端测试、Alembic 升级和前端构建必须通过。
- 这些本地测试不等于已验证任意真实网站、云端部署或微信真机网络白名单。
