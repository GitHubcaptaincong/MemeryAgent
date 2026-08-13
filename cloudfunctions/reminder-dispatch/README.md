# 微信复习提醒定时桥接

这个云函数每 5 分钟执行一次，顺序如下：

1. 从 FastAPI 原子领取一批已经到发送时间的提醒任务；
2. 逐条调用 `cloud.openapi.subscribeMessage.send`；
3. 将 `sent`、`failed` 或 `uncertain` 结果回写 FastAPI。

它不读写 Neon，也不自行计算用户时区和复习计划。任务筛选、额度占用、租约与去重均由 FastAPI/PostgreSQL 负责。云函数只承担定时触发和微信云调用。

## 部署前提

- 当前 CloudBase 环境已经关联目标微信小程序；
- 小程序后台已经选用一个真实订阅消息模板；
- FastAPI 已经提供下文所述的内部 claim/result 接口；
- 云托管服务有可访问的 HTTPS 公网地址；
- 云函数运行时选择 Node.js 16.13 或更高版本，并安装 `package.json` 依赖。

`config.json` 已申请 `subscribeMessage.send` 云调用权限，并配置 7 字段 cron：

```text
0 */5 * * * * *
```

## 环境变量

必填：

```env
MEMORY_AGENT_BASE_URL=https://你的云托管公网域名
MEMORY_AGENT_REMINDER_TOKEN=与FastAPI相同的随机强密钥
WECHAT_TEMPLATE_FIELD_MAP_JSON={"due_count":"number1","reminder_time":"time2","summary":"thing3"}
```

上面的 `number1`、`time2`、`thing3` **只是格式示例，不能直接照抄**。必须打开“小程序后台 -> 功能 -> 订阅消息 -> 我的模板”，用该模板实际显示的字段名替换。

`WECHAT_TEMPLATE_FIELD_MAP_JSON` 的方向固定为：

```json
{
  "FastAPI 任务中的逻辑字段": "微信模板的真实字段名"
}
```

函数只发送映射中列出的字段；缺少任一逻辑字段时，该任务会以 `BRIDGE_INVALID_JOB` 失败，不会猜测模板字段，也不会把后端的任意字段直接透传给微信。

可选：

```env
WECHAT_MINIPROGRAM_STATE=developer
WECHAT_SUBSCRIBE_LANG=zh_CN
WECHAT_SUBSCRIBE_PAGE=pages/review/review
WECHAT_SEND_TIMEOUT_MS=10000
MEMORY_AGENT_HTTP_TIMEOUT_MS=10000
REMINDER_DISPATCH_BATCH_SIZE=10
REMINDER_DISPATCH_LEASE_SECONDS=120
```

联调、体验版、正式版分别使用 `developer`、`trial`、`formal`。上线前必须把 `WECHAT_MINIPROGRAM_STATE` 调整到目标版本。

不要在仓库、日志或小程序代码中写入 `MEMORY_AGENT_REMINDER_TOKEN`。`MEMORY_AGENT_BASE_URL` 填服务根地址，不要附加 `/api/v1`。

## FastAPI 接口契约

领取任务：

```http
POST /api/v1/internal/reminders/dispatch/claim
X-Reminder-Dispatch-Token: <secret>
Content-Type: application/json

{
  "batch_size": 10,
  "lease_seconds": 120
}
```

响应可以直接是数组，也可以使用 `{ "jobs": [...] }`。每个任务至少需要：

```json
{
  "id": "delivery UUID",
  "openid": "目标用户 OpenID",
  "template_id": "真实模板 ID",
  "page": "pages/review/review",
  "claim_token": "本次租约令牌，可选",
  "data": {
    "due_count": 5,
    "reminder_time": "2026年8月13日 20:00",
    "summary": "今天有复习任务"
  }
}
```

`data` 使用逻辑字段，而不是 `number1` 等微信字段。转换工作只由云函数中的环境变量映射完成。

结果回写：

```http
POST /api/v1/internal/reminders/dispatch/{job_id}/result
X-Reminder-Dispatch-Token: <secret>
Content-Type: application/json

{
  "status": "sent | failed | uncertain",
  "wechat_errcode": null,
  "wechat_errmsg": null,
  "response": null,
  "claim_token": "本次租约令牌，可选"
}
```

## 不确定结果与重试边界

- 微信明确返回错误码：记录 `failed`；
- 微信调用超时，或 SDK/网络错误没有微信错误码：记录 `uncertain`；
- 本函数不会重新发送 `uncertain` 任务，避免请求其实已经到达微信时产生重复消息；
- 如果消息已发送但 result 回写失败，函数只记录不含 OpenID 和模板内容的错误日志，不会再次发送。后端应在租约过期后把该任务转为 `uncertain`，由人工核查；
- claim 调用失败发生在发送之前，可以安全地让下一次定时触发重新领取。

## 日志与验收

日志只输出任务 ID、状态计数和截断后的错误，不输出 OpenID、订阅内容或共享密钥。

建议先把 cron 临时改为每分钟，在真机完成以下验证后恢复每 5 分钟：

1. 用户点击并同意一次性订阅；
2. FastAPI 成功创建并领取一条发送任务；
3. 微信收到消息，任务标记为 `sent`，授权额度被消费；
4. 再运行一次函数不会重复发送；
5. 人为制造微信超时后，任务变为 `uncertain` 且不会自动重发；
6. 删除或改错字段映射时任务明确失败，不会发送猜测字段组成的消息。

相关官方资料：

- [CloudBase：用云函数发送微信小程序订阅消息](https://docs.cloudbase.net/recipes/add-subscribe-message-cloud-function)
- [CloudBase：云函数定时触发器](https://docs.cloudbase.net/recipes/schedule-cloud-function-cron-job)
