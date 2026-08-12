# 微信 OpenID 用户隔离

## 设计边界

- 小程序继续通过 `wx.cloud.callContainer` 访问云托管，不新增用户名、密码登录页。
- CloudBase 自动注入 `X-WX-OPENID` 与 `X-WX-APPID`；后端不接受客户端 JSON 中自行提交的 OpenID。
- `wechat_identities` 只负责微信身份到内部 `users.id` 的映射。Source、Run、草稿、复习、提醒和长期记忆继续按内部 `user_id` 查询。
- `/api/v1/health` 与 `/api/v1/ready` 不要求用户身份，便于平台探活；其他业务接口要求当前用户身份。
- OpenID 不返回客户端、不写入 Agent 事件，也不作为模型上下文。

## 第一次云端切换

当前数据库中的历史数据属于：

```text
00000000-0000-0000-0000-000000000001
```

为了保留这些数据，第一次部署使用一次性认领：

```env
APP_AUTH_MODE=wechat
APP_WECHAT_APP_ID=<微信小程序 AppID>
APP_WECHAT_CLAIM_LOCAL_USER=true
```

按以下顺序执行：

1. 在小程序尚未公开时部署以上配置。
2. 使用你自己的微信账号登录微信开发者工具，打开“复习”或“整理”页面，触发第一条业务请求。
3. 请求成功后，在 Neon SQL Editor 验证映射已经指向历史用户：

   ```sql
   SELECT user_id, app_id, created_at, last_seen_at
   FROM wechat_identities;
   ```

   预期只有一行，并且 `user_id` 是上面的全零结尾 UUID。
4. 立即把 `APP_WECHAT_CLAIM_LOCAL_USER` 改为 `false`，重新部署。
5. 再次使用开发者工具访问，原有记忆和复习数据应保持可见。

一次性开关开启期间，第一个成功访问业务接口的微信身份会认领历史数据，因此不要在这段时间发布体验版或把入口交给其他人。

如果已经知道自己的 OpenID，可以使用更严格的配置替代首次访问认领：

```env
APP_WECHAT_CLAIM_LOCAL_USER=false
APP_WECHAT_LEGACY_OWNER_OPENID=<你的 OpenID>
```

`APP_WECHAT_LEGACY_OWNER_OPENID` 应放在云托管密钥/环境变量中，不写入仓库或小程序。

## 日常配置

完成旧数据认领后保持：

```env
APP_AUTH_MODE=wechat
APP_WECHAT_APP_ID=<微信小程序 AppID>
APP_WECHAT_CLAIM_LOCAL_USER=false
```

同一微信身份会稳定映射到同一内部用户；新的微信身份首次访问时会创建新的内部用户和默认 Agent Profile。微信开发者工具与真机都走相同规则。

本地直接运行 FastAPI 或移动 H5 时使用：

```env
APP_AUTH_MODE=local
```

本地模式会继续使用 `APP_LOCAL_USER_ID`，不要求微信 Header。

## 常见响应

- `401 missing or invalid x-wx-openid header`：请求没有经过微信云托管原生调用，或开发者工具未获得微信身份。
- `401 missing or invalid x-wx-appid header`：CloudBase 未注入小程序 AppID。
- `403 WeChat AppID is not allowed`：请求的 AppID 与 `APP_WECHAT_APP_ID` 不一致。
- `404 run/source/draft not found`：资源存在但属于另一个用户时也返回 404，避免泄露资源是否存在。
