# zai2_api

一个将 `chat.z.ai` 封装为 **OpenAI / Claude 兼容 API** 的本地代理服务，附带简易管理后台，方便统一接入第三方客户端、脚本和工具链。

## 功能特性

- 提供 **OpenAI 兼容接口**
  - `GET /v1/models`
  - `POST /v1/chat/completions`
- 提供 **Claude Messages 兼容接口**
  - `POST /v1/messages`
- 支持 **流式响应**
- 支持 **工具 / function calling 转换**
- 内置 **管理后台**
  - 管理员登录
  - API Key 管理
  - 账号池管理
  - 仪表盘 / 统计信息
  - 重建冷却与重试配置
- 支持 **Windows / Linux** 部署

---

## 项目结构

```text
zai2_api/
├── main.py             # 上游 chat.z.ai 交互逻辑：鉴权、模型获取、创建会话、流式对话、清理会话
├── openai.py           # FastAPI 服务入口，提供 OpenAI/Claude 兼容接口与管理后台
├── claude_compat.py    # Claude Messages API 与 OpenAI 消息格式的转换逻辑
├── web/                # 管理后台静态资源（admin.html / admin.css / admin.js）
├── tools/              # 辅助工具目录
├── requirements.txt    # Python 依赖
├── webui_state.json    # 持久化状态：API Key、后台密码哈希、池配置等
└── 启动服务.bat         # Windows 一键启动脚本
```

---

## 工作原理

该项目本身不是模型服务，而是一个代理层：

1. 与 `chat.z.ai` 建立访客/账号会话
2. 获取模型信息
3. 创建聊天上下文
4. 将请求转发到上游
5. 将响应转换为 OpenAI / Claude 兼容格式
6. 视情况清理聊天会话

因此，下游客户端只需要对接本项目暴露的本地 API。

---

## 已实现接口

### OpenAI 兼容接口

#### `GET /v1/models`

获取可用模型列表。

#### `POST /v1/chat/completions`

OpenAI Chat Completions 兼容接口。

支持能力：

- `messages`
- `model`
- `stream`
- tools / function calling
- 与上游会话代理联动

---

### Claude 兼容接口

#### `POST /v1/messages`

Claude Messages API 兼容接口。

支持能力：

- 顶层 `system`
- `messages`
- 文本内容块
- `tools`
- `tool_choice`
- `tool_use / tool_result`
- 流式 / 非流式响应转换

---

## 管理后台

### 入口

```text
GET /admin
```

静态资源：

- `GET /admin/assets/admin.css`
- `GET /admin/assets/admin.js`

后台 API：

- `POST /admin/api/login`
- `POST /admin/api/logout`
- `POST /admin/api/change-password`
- `POST /admin/api/rebuild-settings`
- `GET /admin/api/dashboard`
- `GET /admin/api/keys`
- `POST /admin/api/keys`
- `DELETE /admin/api/keys/{key_id}`
- `POST /admin/api/accounts`
- `DELETE /admin/api/accounts/{user_id}`

### 默认管理密码

默认后台密码为：

```text
zai2api
```

**首次启动后请立即修改默认密码。**

后台认证使用 Cookie，会写入 `admin_session`，有效期约 7 天。

---

## 状态与配置存储

项目运行状态保存在：

```text
webui_state.json
```

已知包含字段：

- `api_keys`
- `target_pool_size`
- `admin_password_hash`
- `rebuild_cooldown`
- `rebuild_max_retries`

当前仓库默认状态中可见的配置值包括：

- `target_pool_size`: `6`
- `rebuild_cooldown`: `30`
- `rebuild_max_retries`: `3`

说明：

- 后台密码以 **哈希** 形式保存，不保存明文
- API Key 会记录创建时间、最后使用时间和请求次数

---

## 账号池与运行机制

服务启动后会通过生命周期钩子自动初始化账号池，并在后台周期性维护。

已知行为包括：

- 启动时初始化 guest account pool
- 若无可用账号，会按需创建
- 后台维护任务约每 30 秒执行一次
- 会清理失效 / 过期 / 空闲无效账号
- 会尝试补齐账号池到目标大小

这意味着项目更适合在**轻量、自用、受控环境**中运行。

---

## 快速开始

## 环境要求

- Python 3.10+
- 可正常访问 `chat.z.ai`
- Windows 或 Linux

安装依赖：

```bash
pip install -r requirements.txt
```

---

## 启动方式

### Windows

可直接双击：

```text
启动服务.bat
```

或命令行运行：

```bash
python openai.py
```

### Linux

建议使用虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 openai.py
```

> `openai.py` 中已引入 `uvicorn` 与 `FastAPI`，实际监听地址与端口请以代码运行参数为准。
> README 历史说明中常见默认地址为 `http://127.0.0.1:30016`。

---

## 常用访问地址

如果按默认方式启动，通常使用：

```text
http://127.0.0.1:30016
```

常用入口：

- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/messages`
- `GET /admin`

---

## 模型名称

README 历史内容中提到的常用模型：

- `glm-5-think`
- `glm-5-nothink`

如果你的客户端支持自定义模型名，可直接填写这些值。

---

## 调用示例

## 1. 获取模型列表

```bash
curl http://127.0.0.1:30016/v1/models \
  -H "Authorization: Bearer YOUR_API_KEY"
```

---

## 2. OpenAI Chat Completions

```bash
curl http://127.0.0.1:30016/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "model": "glm-5-think",
    "messages": [
      { "role": "user", "content": "你好，介绍一下你自己" }
    ],
    "stream": false
  }'
```

---

## 3. Claude Messages

```bash
curl http://127.0.0.1:30016/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: YOUR_API_KEY" \
  -d '{
    "model": "glm-5-think",
    "max_tokens": 1024,
    "messages": [
      { "role": "user", "content": "你好" }
    ]
  }'
```

---

## API Key 鉴权说明

项目支持 API Key 机制。

已知行为：

- 如果状态中已存在 API Key，API 请求需要携带有效 Key
- 否则相关接口会返回 `401`

接入时建议统一使用后台创建的 API Key。

---

## Claude 兼容层说明

`claude_compat.py` 已实现以下转换能力：

- `system` 字段转换
- 文本消息块提取
- Claude `tools` 转 OpenAI function schema
- `tool_choice` 规则转换
- `tool_use / tool_result` 转换
- SSE 流式事件转换
- 非流式结果封装

也就是说，该项目不仅是简单转发，还包含一层协议适配。

---

## 第三方客户端接入

对于支持 OpenAI 协议的客户端，可按以下方式接入：

- **Base URL**：`http://127.0.0.1:30016/v1`
- **API Key**：后台创建的 Key
- **Model**：`glm-5-think` 或 `glm-5-nothink`

典型场景：

- OpenAI SDK
- 支持自定义 Base URL 的桌面客户端
- 各类自动化脚本
- 多模型聚合工具

---

## 部署建议

### 本地试用

直接运行：

```bash
python openai.py
```

### 长期运行

建议配合：

- `systemd`
- `nginx`
- HTTPS
- 内网访问控制
- API Key 管理
- 定期备份状态文件

---

## 常见问题

### 1. 访问 `/` 没有页面？

这是 API 服务，不保证提供首页。请直接访问：

- `/admin`
- `/v1/models`

---

### 2. 为什么接口返回 401？

优先检查：

- 是否已在后台创建 API Key
- 是否正确携带 `Authorization: Bearer ...`
- Claude 调用时是否正确传递 `x-api-key`

---

### 3. 启动后不能正常调用？

请检查：

- Python 版本
- 依赖是否安装完整
- 本机是否能访问 `chat.z.ai`
- 端口是否被占用
- 账号池是否初始化成功

---

### 4. 后台无法登录？

如果未修改过密码，默认值为：

```text
zai2api
```

如果已修改，请检查 `webui_state.json` 中保存的密码哈希对应的实际密码。

---

## 安全提醒

该项目默认更适合：

- 自用
- 测试环境
- 内网环境
- 受控访问环境

不建议直接裸露在公网。

至少应做到：

- 立即修改默认后台密码
- 为 API 设置独立 Key
- 限制来源 IP
- 通过反向代理增加访问控制
- 启用 HTTPS
- 妥善保护 `webui_state.json`

---

## 更新建议

如果你通过 Git 获取代码：

```bash
git pull
pip install -r requirements.txt
```

更新前建议备份：

- `webui_state.json`
- 反向代理配置
- 本地自定义启动方式
- 任何你修改过的代码文件

---

## 协议 / License

当前仓库页面**未见公开 License 文件**，也未显示 GitHub 识别的开源协议。

这意味着：

- 当前项目**默认不等同于开源授权**
- 除非作者后续补充 `LICENSE`，否则他人通常**不应默认拥有复制、修改、再分发权限**

如果你是仓库维护者，建议按你的发布意图补充其一：

- `MIT`
- `Apache-2.0`
- `GPL-3.0`
- `BSD-3-Clause`

补充后 GitHub 才会正确识别项目协议。

---

## 免责声明

本项目依赖上游站点的接口行为与可用性。

上游一旦变更：

- 接口结构
- 鉴权逻辑
- 风控策略
- 页面交互参数

都可能导致兼容失效。

请仅在你有权使用的环境中部署和测试，并自行承担相关使用风险。
