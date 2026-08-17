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

H5 与小程序整理页均增加输入模式切换：

- 文本模式：长文本框与 50,000 字计数。
- 链接模式：URL 输入、能力边界提示和更长请求超时。

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
