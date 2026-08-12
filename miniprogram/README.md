# Memory Agent 微信小程序客户端

这是原生微信小程序客户端。现有 `frontend/` Vue 工作台仍然保留，便于桌面调试；两端调用同一套 FastAPI 接口。

## 导入微信开发者工具

1. 打开微信开发者工具，选择“导入项目”。
2. 项目目录选择仓库根目录 `MemeryAgent`，不要只选择 `miniprogram/`。
3. 根目录 `project.config.json` 已将 `miniprogramRoot` 指向 `miniprogram/`。
4. 当前使用 `touristappid` 供本地预览；需要上传体验版时替换成自己的小程序 AppID。

## 本地联调

先启动后端：

```powershell
$env:CLI_PROXY_API_KEY="<local-proxy-key>"
docker compose up -d
```

默认请求地址在 `miniprogram/config.js`：

```js
apiBaseUrl: 'http://127.0.0.1:8000/api/v1'
```

微信开发者工具中已通过 `project.config.json` 关闭本地合法域名检查。该设置只适用于开发调试。

## 真机与上线

- 真机中的 `127.0.0.1` 指向手机自身，无法访问电脑后端。
- 局域网调试时需换成电脑局域网地址；正式上线必须使用 HTTPS 且配置微信 request 合法域名。
- `utils/api.js` 已预留 `wx.cloud.callContainer`。使用云托管时，在 `config.js` 中设置 `useCloud`、`cloudEnv` 和 `cloudService`。
- 不要把模型 Key、数据库密码、微信 AppSecret 写进 `config.js`；这些只应存在于后端环境变量或云端密钥配置。
- 当前后端使用单一本地身份。正式多人使用前，应接入微信登录，以 OpenID 映射后端用户并增加数据隔离。

## 当前页面

- 整理：快速输入、原文保存回执、Agent 轮询进度、知识草稿确认、长期记忆审批。
- 复习：到期队列、提示、开放题作答、答案与证据对照、FSRS 四档动态预览、下次到期和最近复习。
- 提醒：提醒开关、每日时间、数量上限、逾期策略与时区。

## 当前边界

- 微信订阅消息尚未接入；关闭小程序后不会主动推送。
- 当前复习调度使用官方 `py-fsrs 6.3.1` 的版本化默认配置；尚未训练个人参数。
- 当前回答评估依靠用户自评，AI 语义评估尚未接入。
