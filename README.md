# zai2api

呜，笨蛋哥哥先别乱点啦，妹妹把这份说明书重新写好了。

这是一个把 `chat.z.ai` 网页能力包装成常见 API 的项目。你可以把它理解成：

- 上游是 `chat.z.ai`
- 中间是 `zai2api`
- 下游是各种 OpenAI / Claude 兼容客户端

也就是说，很多原本只能填 OpenAI 或 Claude 接口的工具，现在都可以绕过来接这个项目。

## 妹妹先讲重点

- 支持 `OpenAI` 风格接口
- 支持 `Claude Messages` 风格接口
- 支持 `glm-5-think` / `glm-5-nothink`
- 支持 Windows 部署
- 支持 Linux / Ubuntu 24.04 部署
- 带 `/admin` 管理页面
- 支持账号池、API Key、失败重建策略管理

默认服务地址：

- `http://127.0.0.1:30016`

常用接口：

- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/messages`
- `GET /admin`

## 快速开始

笨蛋哥哥如果只想先跑起来，不想看长篇，那就先抄这一段。

### Windows 最短版

1. 安装 Python 3
2. 解压项目到本地
3. 双击 `启动服务.bat`
4. 等浏览器自动打开 `/admin`

### Ubuntu 24.04 最短版

```bash
apt update && apt install -y python3 python3-venv python3-pip
cd /root/zai2_api
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python openai.py
```

然后打开：

- `http://127.0.0.1:30016/admin`

## 这个项目主要文件是做什么的

- `main.py`
  - 负责和 `chat.z.ai` 直接通信
  - 包括游客登录、创建聊天、获取模型等
- `openai.py`
  - 负责启动 API 服务
  - 提供 OpenAI / Claude 兼容接口
  - 提供 `/admin` 管理页
- `claude_compat.py`
  - 负责 Claude 格式转换
- `web/`
  - 管理后台前端页面

## 模型怎么填

妹妹建议你直接记这两个：

- `glm-5-think` = 开启思考
- `glm-5-nothink` = 关闭思考

如果你什么都不传，默认是开思考。

## /admin 管理页有什么

登录管理页后可以做这些：

- 查看总请求、总成功、总失败
- 查看每个 free 账号的调用情况
- 增加 / 删除账号
- 创建 / 删除 API Key
- 随机生成 Key 或自定义 Key
- 修改 admin 密码
- 修改失败重建冷却时间和重试上限

默认管理页地址：

- `http://127.0.0.1:30016/admin`

默认初始密码：

- `zai2api`

建议你登录后马上改掉，笨蛋哥哥不要偷懒。

## Windows 部署

### 方法 1：最省心的办法

如果你是 Windows 用户，最简单就是直接双击：

- `启动服务.bat`

这个脚本现在会自动做这些事：

- 检查端口占用
- 自动创建 `.venv`
- 自动安装依赖
- 自动启动服务
- 等服务真的 ready 后再打开浏览器到 `/admin`

所以大多数情况下，你只要：

1. 安装 Python 3
2. 把项目解压到本地
3. 双击 `启动服务.bat`

就差不多能用了。

### 方法 2：手动启动

如果你不想双击 bat，也可以自己手动来。

先进入项目目录：

```bat
cd /d D:\zai2api
```

创建虚拟环境：

```bat
python -m venv .venv
```

激活虚拟环境：

```bat
.venv\Scripts\activate
```

安装依赖：

```bat
python -m pip install --upgrade pip
pip install -r requirements.txt
```

启动服务：

```bat
python openai.py
```

启动后打开：

```text
http://127.0.0.1:30016/admin
```

### Windows 常见问题

#### 1. 双击 bat 后浏览器打不开

先别慌，先看控制台有没有红字报错。

最常见原因：

- Python 没装
- 依赖没装成功
- 端口被占用

#### 2. 提示缺少模块，比如 `httpcore`

那就是依赖没装好，执行：

```bat
.venv\Scripts\activate
pip install -r requirements.txt
```

#### 3. 127.0.0.1 拒绝连接

这通常说明服务没真正启动起来。

别只看浏览器，要看启动窗口最后有没有报错。

## Linux / Ubuntu 24.04 部署

下面妹妹按 Ubuntu 24.04 来写，别的 Linux 也可以参考。

假设项目路径是：

- `/root/zai2_api`

### 第 1 步：安装基础环境

```bash
apt update && apt upgrade -y
apt install -y python3 python3-venv python3-pip nginx apache2-utils curl
```

### 第 2 步：把项目放到服务器

比如放到：

- `/root/zai2_api`

检查一下：

```bash
cd /root/zai2_api
ls -lah
```

你至少应该能看到：

- `main.py`
- `openai.py`
- `claude_compat.py`
- `requirements.txt`
- `web/`

### 第 3 步：创建虚拟环境

```bash
cd /root/zai2_api
python3 -m venv .venv
source .venv/bin/activate
```

### 第 4 步：安装依赖

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

测试依赖：

```bash
python -c "import fastapi,uvicorn,httpx,httpcore; print('ok')"
```

如果输出 `ok`，说明环境好了。

### 第 5 步：手动启动测试

```bash
cd /root/zai2_api
source .venv/bin/activate
python openai.py
```

再开一个终端测试：

```bash
curl http://127.0.0.1:30016/v1/models
```

如果能返回 JSON，说明服务已经起来啦。

管理页测试：

```bash
curl http://127.0.0.1:30016/admin
```

### 第 6 步：配置 systemd 开机自启

创建服务文件：

```bash
nano /etc/systemd/system/zai2api.service
```

填入：

```ini
[Unit]
Description=zai2api service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/zai2_api
Environment=LOG_LEVEL=INFO
Environment=POOL_SIZE=3
Environment=TOKEN_MAX_AGE=480
ExecStart=/root/zai2_api/.venv/bin/python /root/zai2_api/openai.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启用并启动：

```bash
systemctl daemon-reload
systemctl enable zai2api
systemctl start zai2api
systemctl status zai2api
```

看日志：

```bash
journalctl -u zai2api -f
```

### 第 7 步：配置 Nginx 反代

先创建一个访问密码：

```bash
htpasswd -c /etc/nginx/.htpasswd admin
```

创建配置：

```bash
nano /etc/nginx/sites-available/zai2api
```

内容如下：

```nginx
server {
    listen 80;
    server_name _;

    client_max_body_size 20m;

    auth_basic "Restricted";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass http://127.0.0.1:30016;
        proxy_http_version 1.1;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

启用配置：

```bash
ln -s /etc/nginx/sites-available/zai2api /etc/nginx/sites-enabled/zai2api
nginx -t
systemctl restart nginx
```

### 第 8 步：放行端口

如果你启用了 UFW：

```bash
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw enable
ufw status
```

如果你是云服务器，还要记得开放：

- `80`
- `443`（如果以后上 HTTPS）

### 第 9 步：测试接口

模型列表：

```bash
curl -u admin:你的密码 http://你的服务器IP/v1/models
```

OpenAI 风格：

```bash
curl -u admin:你的密码 http://你的服务器IP/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5-think",
    "messages": [
      {"role": "user", "content": "你好"}
    ],
    "stream": false
  }'
```

Claude 风格：

```bash
curl -u admin:你的密码 http://你的服务器IP/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5-nothink",
    "messages": [
      {"role": "user", "content": "请简单介绍你自己"}
    ],
    "stream": false
  }'
```

## 客户端怎么填

如果你用的是 OpenAI 兼容客户端，一般这样填：

- Base URL: `http://你的地址/v1`
- Model: `glm-5-think` 或 `glm-5-nothink`

### API Key 怎么填

现在这个项目支持在 `/admin` 里创建 API Key。

如果你已经创建了 key，请在客户端里带上：

```text
Authorization: Bearer 你的key
```

如果你还没创建 key，那就先打开 `/admin` 自己生成一个，笨蛋哥哥不要忘记这一步。

## 常见问题

### 1. 为什么访问 `/` 是 404

因为这个项目没有首页。

你应该访问：

- `/admin`
- `/v1/models`
- `/v1/chat/completions`
- `/v1/messages`

### 2. 为什么服务起不来

最常见原因：

- Python 环境没装好
- `.venv` 没创建
- 依赖没安装
- 端口被占用
- 上游异常

Windows 先看 bat 窗口输出，Linux 先看：

```bash
journalctl -u zai2api -f
```

### 3. 为什么成功率不变

现在新版已经修过统计逻辑了。

如果你发现还是不对，先刷新 `/admin` 再观察；如果还是奇怪，把复现步骤贴出来，妹妹陪你看。

### 4. 账号池是不是越大越好

不是。

建议：

- 自己用：`POOL_SIZE=1` 到 `3`
- 少量人一起用：`POOL_SIZE=3` 到 `5`
- 别一上来就开太大

### 5. 这个项目稳定吗

能用，但它不是官方商用稳定 API。

因为它本质上还是网页协议代理：

- 上游可能改
- 游客号可能失效
- 并发太高可能不稳

所以更适合：

- 个人使用
- 小规模共享
- 开发测试

## 项目更新办法

笨蛋哥哥如果后面想更新项目，别直接乱覆盖，按下面来比较稳。

### Windows 更新

如果你是手动下载压缩包那种方式：

1. 先备份你现在正在用的目录
2. 下载新版本代码
3. 把你自己的配置文件保留好
4. 再重新运行 `启动服务.bat`

如果你是 git 拉下来的：

```bat
cd /d D:\zai2api
git pull
```

如果更新后依赖有变化，再执行：

```bat
.venv\Scripts\activate
pip install -r requirements.txt
```

### Ubuntu 更新

如果服务器目录本身是 git 仓库：

```bash
cd /root/zai2_api
git fetch origin
git pull
source .venv/bin/activate
pip install -r requirements.txt
systemctl restart zai2api
systemctl status zai2api
```

如果你用的是指定分支，比如：

```bash
cd /root/zai2_api
git fetch origin
git checkout hongyue0721-patch-1
git pull origin hongyue0721-patch-1
source .venv/bin/activate
pip install -r requirements.txt
systemctl restart zai2api
```

更新完建议再看一眼日志：

```bash
journalctl -u zai2api -n 100 --no-pager
```

### 更新时要特别注意什么

- `webui_state.json` 里有你管理页的密码、API Key 和部分配置
- 如果你不想丢这些数据，更新时记得保留这个文件
- 如果你改过 `openai.py`、`web/` 目录、或者 `启动服务.bat`，更新时也要注意别被新文件覆盖掉

## 最后，妹妹再帮你总结一下

如果你很懒，不想看长篇，就记住这些：

- Windows 直接双击 `启动服务.bat`
- Linux 推荐 `systemd + nginx`
- 管理页是 `/admin`
- 初始密码是 `zai2api`
- 模型常用 `glm-5-think` 和 `glm-5-nothink`
- 建议先小规模使用，不要一开始就高并发乱冲

如果你照着做还是不会，那就把报错贴出来。

笨蛋哥哥别硬撑，妹妹会继续陪你修。
