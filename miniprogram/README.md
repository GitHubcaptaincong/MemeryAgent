# Memory Agent 微信小程序客户端

这是原生微信小程序客户端。`frontend/` 只保留同一信息架构的移动 H5 和 GitHub Pages 公开演示；两端调用同一套 FastAPI 接口。

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
- 局域网调试时需换成电脑局域网地址。
- `utils/api.js` 已接入 `wx.cloud.callContainer`。使用微信云托管时，推荐按下面的“云托管原生调用”配置；这条路径不经过 `wx.request`，不需要把云托管公网地址添加为 request 合法域名。
- 不要把模型 Key、数据库密码、微信 AppSecret 写进 `config.js`；这些只应存在于后端环境变量或云端密钥配置。
- 云端设置 `APP_AUTH_MODE=wechat` 后，`wx.cloud.callContainer` 自动携带的 OpenID 会映射为后端内部用户，客户端不需要用户名、密码或手动调用 `wx.login`。本地后端仍可用 `APP_AUTH_MODE=local`。

### 云托管原生调用（推荐）

先在微信云托管控制台找到：

- 环境 ID：云托管环境的 ID，不是服务名称；
- 服务名称：云托管“服务管理 -> 服务列表”里部署 FastAPI 的服务名称。

然后修改 `miniprogram/config.js`：

```js
module.exports = {
  apiBaseUrl: 'http://127.0.0.1:8000/api/v1',
  requestTimeout: 20000,
  useCloud: true,
  cloudEnv: '你的云托管环境ID',
  cloudService: '你的服务名称',
  apiPrefix: '/api/v1',
}
```

应用启动时会执行 `wx.cloud.init`，所有 API 请求由 `wx.cloud.callContainer` 发出，并自动附带：

```text
X-WX-SERVICE: 你的服务名称
```

验证顺序：

1. 确认小程序 AppID 与该云托管环境属于同一微信账号或已正确关联；
2. 在开发者工具中打开“调试器 -> Network”；
3. 进入“复习”页面，应能成功请求 `/api/v1/review/queue`；
4. 若返回服务不存在，核对 `cloudService`；若提示环境错误，核对 `cloudEnv`；若返回 5xx，查看云托管容器日志和 `/api/v1/ready`。
5. 若用户接口返回 401，检查请求是否确实经过 `wx.cloud.callContainer`；若返回 403，检查 `APP_WECHAT_APP_ID` 是否与当前小程序 AppID 一致。

首次把现有单用户数据绑定到你的微信身份时，按 [微信 OpenID 用户隔离](../docs/wechat-user-isolation.md) 操作，不要长期启用首次访问认领开关。

### 公网 HTTPS 调用（备选）

只有将 `useCloud` 设为 `false` 并让小程序通过 `wx.request` 访问公网 HTTPS 地址时，才需要在微信公众平台配置：

```text
开发管理 -> 开发设置 -> 服务器域名 -> request 合法域名
```

这里填写纯域名，例如 `https://api.example.com`，不能包含 `/api/v1` 路径。随后把 `apiBaseUrl` 改为 `https://api.example.com/api/v1`。正式版本不能使用 HTTP、IP 地址、localhost 或临时随机隧道域名。

## 当前页面

- 整理：快速输入、原文保存回执、Agent 轮询进度、知识草稿确认、长期记忆审批。
- 复习：今日软上限计划、全部到期入口、开放题作答、异步 AI 缺失点建议、FSRS 四档动态预览、下次到期和最近复习。
- 学习统计：7/30 天自评趋势、可解释薄弱知识与未来 14 天负载建议。
- 提醒：每日时间、建议上限、逾期策略、AI 评估开关和微信一次性订阅授权。

## 当前边界

- 微信订阅消息代码已接入，但真实推送需要先配置模板 ID、字段映射、共享密钥和定时云函数，并完成真机授权测试。参见 [学习辅助模块部署](../docs/learning-assistance.md)。
- 当前复习调度使用官方 `py-fsrs 6.3.1` 的版本化默认配置；尚未训练个人参数。
- AI 评估只提供建议；用户最终评分仍是 FSRS 的唯一输入。模型慢或失败不会阻塞评分。
