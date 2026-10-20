# Memory Agent 项目说明与面试准备材料

整理日期：2026-10-12。用途：上传到 ChatGPT，提供项目事实、证据与待补充问题；暂不生成标准面试答案。

## 1. 材料范围与可信度

- 本文依据当前工作区源码、测试和项目文档。Git 基准为 `eed1213f7b48d3a317275b13fbf7af7437ba1a2d`，但工作区包含未提交修改（复习、统计、鉴权、数据库、前端、性能埋点等），所以本文并非该提交的纯净快照。
- 已收到用户粘贴的简历和“Agent 相关 AI 应用工程师”岗位方向，未收到具体公司 JD。用户说明本项目全程使用 Codex 实现。个人设计决策、协作范围及运营成果仍需补充，不能从项目存在反推由候选人独立编写或主导全部架构。
- “已实现”表示在源码中找到执行路径；“本地已验证”仅指本次实际执行通过的检查；“测试覆盖”表示存在测试用例；“待验证”表示缺少对应环境证据；“计划”不算实现。
- 不使用虚构用户数、准确率、成本降幅、性能提升或上线成果。配置上限、测试夹具和设计目标均不能写成生产指标。
- README 有过时信息：其 10,000 字上限、未支持公开链接的描述与当前源码冲突；当前接口是 50,000 字并有公开 URL 抓取。依据 E01、E02 和 `docs/stage-5-implementation-spec.md`。

## 2. 项目背景

项目面向“收藏了材料，但缺少主动回忆、证据对照和后续复习安排”的学习场景。基本闭环是：输入文本或公开链接 → 生成有原文证据的知识问答草稿 → 用户确认 → 主动回忆作答 → AI 给出建议 → 用户自评 → FSRS 计算下次复习时间。

这是项目的产品定位，不代表已通过用户调研证明学习效果。实际目标用户、立项原因、使用人数、是否上线和使用反馈需要候选人补充。

## 3. 架构与目录

```text
Vue 3 H5 / 原生微信小程序
        │ HTTP + SSE（客户端能力需分别验收）
FastAPI API ── 身份解析 / Source / Run / Draft / Review / Reminder
        │ SQLAlchemy
数据库：业务对象 + BackgroundJob + AgentEvent + ToolInvocation
        │ 任务领取与租约
Python Worker ── AgentRuntime ── ModelAdapter / CLIProxy Responses
        │                  ├─ ToolRegistry：读取、证据定位、校验
        │                  ├─ SkillRegistry：只读本地技能
        │                  └─ Memory：已批准且 active 的记忆
        ├─ 回答评估任务
        └─ 微信订阅提醒调度
复习领域：ReviewEvent 历史 + ReviewCard 当前状态 + FSRS
```

目录对应：`backend/src/memory_agent/` 是服务端领域与运行时；`backend/migrations/` 是数据库演进；`backend/tests/` 是回归测试；`frontend/src/` 是 H5；`miniprogram/` 是微信客户端；`.agents/skills/` 是 Agent 技能；`scripts/` 有诊断和性能基准脚本。

这套架构包含一个受约束的 Agent Runtime，不是多 Agent 协作系统。长期记忆检索目前是词项重合加权排序，不是完整向量 RAG。依据 E03、E09、E10。

## 4. 核心流程

### 材料到知识草稿

1. API 保存 Source；会话轮次可将材料、运行和聊天记录关联起来。Source 与 Run 分开，使模型失败不必导致原文丢失。见 `services.py:create_source/create_run`、`api.py:post_conversation_turn`。
2. Worker 从 BackgroundJob 领取任务，记录 owner、attempt 和过期时间；PostgreSQL 路径使用行锁与 `SKIP LOCKED`。见 E05。
3. Runtime 加载原文、用户 Profile、有效记忆和匹配技能，再建立模型上下文。技能路由为关键词规则，最多选择三个，不是模型自主安装技能。见 E03、E10。
4. 模型提出工具调用；工具读取材料、定位逐字引文并做草稿校验。唯一匹配引文才返回权威字符范围；找不到或重复出现时要求修正。见 E04。
5. 草稿必须通过结构和原文证据校验。Runtime 校验已持久化的草稿哈希，防止“校验的是 A，保存的是 B”。见 E03、E11。
6. 保存草稿并等待用户确认。确认后创建复习卡，同时创建待批准记忆候选；确认知识和批准长期记忆是两个动作。见 E06。

短材料在 CLIProxy 配置下可走快速路径，默认阈值 600 字。测试中分别存在“一次模型调用”和“三次调用后本地完成”的场景；这是受控测试行为，不能推导所有真实输入的调用次数或延迟。见 E12。

### 回答到下一次复习

1. 提交回答，写入事件并创建异步评估任务。评估结果包含覆盖点、缺失点与建议评分，客户端可以继续走用户自评流程。见 `review.py:submit_review_answer`、`evaluation_jobs.py:AnswerEvaluationProcessor`。
2. 用户提交 1–4 档评分；服务检查幂等键、回答轮次是否已评分、提交时 review_count 是否过期，并锁定卡片。
3. FSRS 接收用户评分，更新 due_at 和调度状态，保存 review_rated 事件及前后状态。AI 评分仅用于记录对照。见 E07。
4. 老卡片通过评分事件重放迁移；历史不足时拒绝静默重置。见 E08。

### 失败恢复与提醒

模型可重试异常进入 retry_wait，JobRunner 指数退避（最多 30 秒），受 max_attempts 限制。过期租约可被接管。恢复方式是 fresh_context_replay：重新组装业务上下文执行，不恢复 Provider 隐式推理链，也不能描述成精确从任意断点继续。见 E03、E05、E13。

微信提醒预留一次性订阅额度并写发送状态。发送超时可能形成 uncertain；对应测试要求过期 sending 不重发，以减少重复发送，代价是可能漏提醒。真实发送还依赖有效凭证、模板、用户授权和部署配置。见 E14。

## 5. 技术选型与取舍

以下“理由”是由实现推断出的工程权衡；候选人需确认哪些确实是自己的决策。

| 选择 | 实现依据 | 权衡与局限 |
| --- | --- | --- |
| FastAPI + Pydantic + SQLAlchemy | `backend/pyproject.toml`、`api.py`、`schemas.py` | Python 生态便于 AI 与 FSRS 集成；不能仅凭框架选择宣称高并发能力 |
| PostgreSQL 业务与任务共用数据库 | E05、`models.py:BackgroundJob` | 少引入独立队列，业务和任务可持久化；锁竞争、轮询和扩容需实测 |
| SSE 事件反馈 | `api.py:_event_stream`、`events.py:append_event` | 便于推送运行事件与按 seq 续读；代理超时、断线与小程序传输需单独验证 |
| 服务端证据定位 + 草稿哈希 | E04、E11 | 增加工具与校验成本，降低坐标错配和校验后替换风险；不能证明知识解释语义正确 |
| 只读 Skill + 已批准记忆 | E06、E09、E10 | 限制模型权限，保留用户控制；规则路由和简单检索的召回能力有限 |
| 官方 fsrs 6.3.1 | E08、`review_config.py`、依赖文件 | 复用调度算法并版本化状态；尚未证明对本项目用户的效果，也未训练个人参数 |
| CLIProxy Responses + Fake Adapter | `cli_proxy_adapter.py`、`model_adapters.py` | 协议适配和测试可分离；Fake 通过不代表真实 Provider 兼容性和内容质量 |
| 公开链接静态抓取 | E02 | 控制 SSRF 风险与请求规模；不执行 JS、不携带登录态，覆盖不了所有网页 |

## 6. 难点、已有处理与追问

| 难点 | 已有处理 | 面试可能追问 |
| --- | --- | --- |
| 模型证据坐标不可信 | 唯一引文定位、原文切片校验、草稿哈希（E04/E11） | 重复引文怎么办？字符坐标是否对应 Python 字符而非 JS UTF-16？证据正确是否等于答案正确？ |
| 模型失败和 Worker 崩溃 | 持久化任务、租约、退避、业务上下文重放（E05/E13） | 超过租约但旧 Worker 还在执行怎么办？是否有续租或 fencing token？ |
| 幂等与重复副作用 | 工具缓存、评分幂等键、卡片锁（E04/E07） | 工具键含 call_id 和参数字符串，重放后换 call_id 是否仍能去重？不能宣称全局 exactly-once |
| AI 建议不能替用户评分 | 异步建议与最终用户评分分离（E07） | 建议晚到、失败和用户改分怎么处理？旧回答为什么要拒绝？ |
| 调度版本迁移 | 状态版本与历史重放（E08） | 历史缺失为何不重置？如何验证迁移前后 due_at？ |
| 多用户隔离 | 内部 user_id 查询、微信身份映射（E15） | X-WX-* Header 谁注入？公网请求是否可能伪造？数据库唯一约束如何处理首次请求竞争？ |
| 外链抓取安全 | 公网 IP 检查、固定连接 IP、每次重定向复检（E02） | DNS rebinding、TLS hostname 校验、响应体超限和静态正文质量怎么处理？ |
| 通知超时的不确定性 | uncertain 状态避免盲目重发（E14） | 为什么接受漏发？微信有没有服务端查询或发送幂等支持？ |

一个值得如实讨论的风险：配置中有 agent_max_seconds，但当前 AgentRuntime 中未找到硬截止时间检查；JobRunner 用它计算租约，而模型请求默认超时是 180 秒、Agent 配置默认 120 秒。不要声称“已完整实现时间预算和严格互斥恢复”；需审查超时与租约关系，补真实数据库多 Worker 测试。见 `config.py`、E03、E05。

## 7. 实现与验证状态

| 能力 | 源码状态 | 验证边界 |
| --- | --- | --- |
| Source → Run → Draft → Confirm → Review | 已实现 | API 回归测试；不证明真实模型生成质量 |
| 50,000 字和公开 URL | 已实现 | 提取、安全边界与元数据模拟测试；未证明任意网站可用 |
| 草稿证据与哈希约束 | 已实现 | 定位歧义、证据不匹配、哈希不匹配测试 |
| 重试与租约接管 | 已实现 | 受控恢复测试；未验证生产多进程竞争与长任务重叠 |
| FSRS 状态、迁移、自评分离 | 已实现 | 本地调度与流程回归；无学习效果实证 |
| 微信身份隔离与提醒 | 已实现 | 模拟 Header/HTTP 和状态测试；真机、可信网关与真实发送待补证据 |
| H5 | 已实现 | 本次生产构建通过；未进行浏览器交互验收 |
| 微信小程序 | 客户端源码存在 | 本次未运行开发者工具或操作手机 |
| pgvector / pg_trgm | 有数据与部署基础 | 不等于已接入 Embedding 或完整混合检索 |
| 外部搜索、PDF/图片/音视频、个性化 FSRS 参数训练 | 未见完整接入 | 可列候选后续方向，不能写成已有成果或已承诺路线 |

本次验证结果见 `interview-verification.txt`。后端测试使用 `backend/tests/conftest.py` 指定的 SQLite 与 Fake 默认配置，部分模型协议和微信接口测试为 Stub/Mock。没有执行生产基准、真实模型评测、PostgreSQL 并发验证、云端部署、微信发送或学习收益实验。

具体结果：H5 构建成功；15 个不依赖 TestClient 的模型协议、调度和抓取用例通过。全量测试未完成，诊断运行在 Windows asyncio/TestClient 初始化 socketpair 处停滞。任务恢复测试独立运行有 2 个失败，原因为测试数据库缺少 users 表；属于初始化/隔离缺口，本次未验证其业务断言。因此本材料只称任务恢复“已实现、存在测试覆盖”，不称其本次验证通过。

`scripts/benchmark_review_endpoints.py` 提供合成 SQLite 工作负载与可选查询 RTT 模拟，本次未运行。不能把其中默认卡片数或模拟 RTT 写成线上用户规模和线上延迟；没有前后基线，不报性能提升百分比。若以后需要示例数字，必须放入单独的“假设场景”，并禁止用于简历真实成果。

## 8. 简历核对、个人贡献与岗位匹配

简历背景：计算机科学与技术本科；2021–2025 年主要为 Java/游戏后端经历；2025.04–2026.03 列高级 AI 工程师，涉及政务、企业知识库与智能复习助手。上述经历来自用户自述，本仓库不能验证雇佣、职级、交付或其他项目成果，不在本文复制联系方式。

| 简历表述 | 当前源码核对 | 材料处理 |
| --- | --- | --- |
| 有界、可审计知识生成 Agent | 工具次数限制、事件、证据定位和校验可对应 E03/E04/E11 | 保留能力事实；“有界”需限定为已找到的工具/循环预算，不能包含已完整落实的时间/Token 预算 |
| 状态与 Context 解耦 | 业务状态落库、执行时创建上下文、失败后重放可对应 E03/E05 | 保留这一层设计事实，个人是否提出该方案待确认 |
| Token 接近预算时压缩历史，动态组装近期交互和工作集 | 未找到 Token 阈值触发压缩、工作集裁剪和中途压缩后续跑链路；适配器会累计 provider_items | 从“已实现亮点”移到候选改进；Checkpoint 摘要与 tokens_before/after 字段不等于完整压缩机制 |
| 可恢复长任务、异步执行、SSE、幂等与重试 | 相关路径和回归测试存在 | 说明 fresh_context_replay，不宣称 Provider 推理状态续传或全局 exactly-once |
| 需求、方案、联调、交付由本人负责 | 本仓库无法确认个人责任与真实客户交付 | 需要实际需求记录、取舍理由、验收/部署记录补充 |

面向 Agent AI 应用工程师的暂定匹配：工具调用协议、输出校验、运行状态、任务恢复、用户审批和评估降级是较强项目证据。Token 压缩、Embedding/混合检索、系统化模型评测、MCP 服务接入、多 Agent 协作和生产规模验证没有在本项目找到完整证据。简历中的 MinerU、Ragas、Weaviate、Dify、LangChain、Hermes 与 Java 能力需要对应其他项目材料，不应嫁接到这个仓库。

AI 使用方式按真实情况描述：Codex 承担代码生成与实现。可准备本人如何提出需求、判断方案、读代码、发现问题和验收的具体例子，但只有实际做过才能归为贡献。面试前现在补做的源码阅读和实验可作为当前理解证据，不能倒写成历史主导经历。

源码证明功能存在，不能证明谁设计、谁实现、是否使用 AI 辅助或是否独立完成。请补充：

1. 具体目标岗位 JD；当前仅收到岗位方向。
2. 项目时间、团队规模、本人角色、负责模块与协作者分工。
3. 哪些设计和取舍由本人主导，哪些来自参考项目、依赖库或 AI 辅助。
4. 两个实际处理过的困难：触发条件、排查过程、本人改动、可提供的 PR/提交/日志。
5. 是否上线，何时、部署环境、真实用户范围、模型与微信验收记录。
6. 数字的来源、样本量和测试方法；无法复核的数字先保留“待核实”。

贡献填写模板：`模块：____；本人动作：____；合作边界：____；代码/提交：____；验证记录：____；真实结果：____`。

JD 映射应在收到原文后填写：`岗位要求 → 项目证据 → 本人贡献证据 → 匹配程度 → 能力缺口 → 待追问`。暂时只能列候选主题：AI 应用岗位可看受约束工具循环与证据校验；后端岗位可看事务、幂等、任务恢复与身份隔离；全栈岗位可看双客户端闭环。这里不是对实际 JD 的匹配结论。

额外需要确认时间归属：简历中的工作经历截至 2026.03，而阶段五文档标注 2026.08 本地实现。需说明哪些版本在任职期间完成，哪些是离职后个人迭代，不能将后续功能自动计为当时的公司交付。

## 9. 给 ChatGPT 的使用说明

请将本文件与简历、目标岗位 JD 一起读取。先核对事实、证据和个人贡献，再组织面试准备。当前仅需项目背景梳理、岗位映射、证据缺口与追问清单，不生成可背诵的标准面试答案。

本文件已包含验证摘要与关键证据索引，可以单独上传；若要核查精确引文，同时上传 `interview-evidence.json` 和 `interview-verification.txt`。

必须保留已实现、本地已验证、待验证与候选计划的区别；不要把 Mock 测试改写成线上验收，不要把配置目标改写成性能成果，不要把仓库功能自动归为本人独立贡献。个人贡献缺证据时先提问。源码证据的路径/行号/字符区间与短引文见同目录 `interview-evidence.json`；文件修改后位置可能变化，应按符号重新核查。

重点准备追问：为何这是 Agent 而不只是固定工作流？模型权限由谁控制？校验能保证什么？恢复是否精确续跑？幂等覆盖哪些边界？租约到期能否双重执行？SQLite 测试缺什么？为什么用 FSRS？用户评分与模型建议为何分离？微信网关信任如何建立？外链权限与外部搜索权限如何区分？如何补真实质量和性能评测？

## 10. 证据索引

行号与字符区间依据本次读取的 UTF-8 源码，字符区间为 Python 解码字符串的零基半开区间。源码路径用于定位，不代表已确认个人作者。

- E01: `backend/src/memory_agent/schemas.py:14`，符号/短引文 `class SourceCreate`。

- E02: `backend/src/memory_agent/source_ingestion.py:224`，符号/短引文 `def resolve_public_ips`。

- E03: `backend/src/memory_agent/agent.py:34`，符号/短引文 `class AgentRuntime`。

- E04: `backend/src/memory_agent/tools.py:43`，符号/短引文 `class ToolRegistry`。

- E05: `backend/src/memory_agent/jobs.py:170`，符号/短引文 `def claim_next`。

- E06: `backend/src/memory_agent/services.py:179`，符号/短引文 `def confirm_draft`。

- E07: `backend/src/memory_agent/review.py:335`，符号/短引文 `def rate_review_answer`。

- E08: `backend/src/memory_agent/scheduler.py:49`，符号/短引文 `class FsrsReviewScheduler`。

- E09: `backend/src/memory_agent/memory.py:31`，符号/短引文 `def retrieve_memories`。

- E10: `backend/src/memory_agent/skills.py:66`，符号/短引文 `def route`。

- E11: `backend/tests/test_cli_proxy_adapter.py:230`，符号/短引文 `def test_runtime_rejects_persisted_validation_hash_mismatch`。

- E12: `backend/tests/test_cli_proxy_adapter.py:321`，符号/短引文 `def test_short_source_uses_one_model_call_and_normalizes_evidence_ranges`。

- E13: `backend/tests/test_job_recovery.py:92`，符号/短引文 `def test_claim_next_recovers_an_expired_running_lease`。

- E14: `backend/tests/test_reminders.py:325`，符号/短引文 `def test_expired_sending_lease_becomes_uncertain_and_is_never_resent`。

- E15: `backend/tests/test_wechat_auth.py:54`，符号/短引文 `def test_first_wechat_user_claims_legacy_data_and_users_are_isolated`。

- E16: `backend/src/memory_agent/agent.py:541`，符号/短引文 `tokens_before=max(1, len(serialized) // 4)`。

- E17: `backend/src/memory_agent/cli_proxy_adapter.py:141`，符号/短引文 `provider_items.extend(copy.deepcopy(output_items))`。

- E18: `backend/tests/test_api_flow.py:31`，符号/短引文 `def test_source_to_confirmed_draft_and_approved_memory`。

- E19: `backend/tests/test_source_ingestion.py:83`，符号/短引文 `def test_long_text_and_public_url_sources_keep_origin_evidence`。

- E20: `backend/tests/test_scheduler.py:88`，符号/短引文 `def test_legacy_card_with_incomplete_history_is_not_silently_reset`。
