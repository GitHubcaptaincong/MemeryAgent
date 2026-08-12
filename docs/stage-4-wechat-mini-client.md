# 阶段四：微信小程序移动客户端

## 目标

把客户端统一为适合微信和手机单手操作的移动端形态：`miniprogram/` 提供原生微信客户端，`frontend/` 只保留同版式的移动 H5 和 GitHub Pages 公开演示，不再维护旧桌面工作台。

## 信息架构

```text
整理
  快速记录 → Agent 进度 → 知识草稿 → 用户确认

复习
  到期队列 → 独立作答 → 对照答案与证据 → 用户自评

提醒
  每日时间 → 数量上限 → 逾期策略 → 保存偏好
```

三个页面使用原生自定义底部标签栏，并为复习页保留到期数量角标。

## 移动端设计原则

- iOS 式大标题、分组设置、圆角卡片、系统蓝主操作和系统绿成功状态。
- 主要操作保持大触控区域，输入、提交和评分适合单手完成。
- 信息采用手机端纵向渐进披露；知识单元默认只展开一张。
- Agent 等待期间持续展示当前业务动作、进度、耗时和最近事件，不展示模型隐式思维链。
- 先保存原文再启动 AI，模型变慢不会让用户误以为记录丢失。

## 工程结构

```text
project.config.json
miniprogram/
  app.js / app.json / app.wxss
  custom-tab-bar/
  pages/capture/
  pages/review/
  pages/reminders/
  utils/api.js
  utils/format.js
  utils/ids.js
```

`utils/api.js` 支持两种请求路径：

1. 开发者工具通过 `wx.request` 访问本机 FastAPI。
2. 上线后通过 `wx.cloud.callContainer` 访问云托管服务。

## 已验证

- 全部小程序 JavaScript 文件通过 `node --check`。
- 全部 JSON 配置可以解析。
- 三个页面均具备 `js/json/wxml/wxss` 完整文件集合。
- WXML 未出现实体化运算符或可选链表达式。
- 移动 H5 的 Vue 生产构建通过。
- 本地 FastAPI 健康检查与复习队列接口可访问。

## 尚未验证与边界

- 本机微信开发者工具已经安装，但 CLI 服务端口关闭，因此本轮没有完成真实 IDE 编译；需要在“设置 → 安全设置”开启服务端口后再运行 CLI。
- 真机不能访问 `127.0.0.1`；需改为局域网地址或部署到 HTTPS/云托管。
- 订阅消息、微信登录和 OpenID 用户隔离尚未实现。
- `config.js` 不保存模型 Key、数据库密码或 AppSecret。

## CloudBase 上传目录

根目录已经增加云托管专用 `Dockerfile` 和 `.dockerignore`。通过控制台上传整个仓库时，Dockerfile 目录使用 `.`、名称使用 `Dockerfile`、端口使用 `8000`。

原来的 `backend/Dockerfile` 继续服务于本地 Compose 构建；根目录 Dockerfile 使用仓库根目录作为构建上下文，并把 `.agents/skills` 一起复制到 `/skills`。
