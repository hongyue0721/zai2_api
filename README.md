# ZAI2API

呜呜，哥哥姐姐好，这是一份尽量写得很傻瓜、很容易照着做的说明书。

这是一个把 `chat.z.ai` 网页能力包装成常见 API 的小项目。

你可以把它理解成：

- 上游是 `chat.z.ai`
- 这个项目是中间代理
- 下游是各种 OpenAI / Claude 兼容客户端

只要接上它，很多原本只能接 OpenAI 或 Claude 的工具，就可以绕过来调用 Z.ai 啦。

## 一眼看懂

- 支持 `OpenAI` 风格接口
- 支持 `Claude Messages` 风格接口
- 支持 `思考 / 不思考` 两种模型后缀
- 支持 Ubuntu 24.04 部署
- 支持 `systemd` 开机自启
- 支持 `nginx` 对外访问

## 项目作用

这个项目的作用很简单：

- 它会把 `chat.z.ai` 的网页能力包装成常见的 API
- 对外提供 `OpenAI` 风格接口
- 也提供 `Claude Messages` 风格接口
- 这样很多支持 OpenAI/Claude 的客户端，就可以直接接它来用啦

如果你不是很懂代码，也没关系，照着下面一步一步抄就好，我尽量写得像笨蛋妹妹教哥哥装软件一样简单一点。

## 这个项目是干什么的

这个项目主要有 3 个核心文件：

- `main.py`
  - 负责直接和 `chat.z.ai` 通信
  - 包括游客登录、获取模型、创建聊天、流式返回内容
- `openai.py`
  - 负责启动 API 服务
  - 提供 `/v1/models`
  - 提供 `/v1/chat/completions`
  - 提供 `/v1/messages`
- `claude_compat.py`
  - 负责把 Claude 格式和内部格式互相转换

简单理解就是：

- 上游是 `chat.z.ai`
- 这个项目是中间代理
- 你的客户端调用的是这个项目暴露出来的接口

## 支持什么接口

启动后默认监听：

- `http://127.0.0.1:30016`

可用接口：

- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/messages`

## 支持“思考 / 不思考”吗

支持呀，已经加好了，笨蛋妹妹这次没有偷懒。

现在推荐用模型后缀来区分：

- `glm-5-think` = 开启思考
- `glm-5-nothink` = 关闭思考

比如：

- `glm-5-think`
- `glm-5-nothink`

另外，这个项目也兼容请求体里的 `enable_thinking` 参数。

但是更推荐你直接用模型名后缀，因为更直观，不容易搞混。

优先级大概可以这样理解：

- 如果模型名带 `-think`，那就开思考
- 如果模型名带 `-nothink`，那就关思考
- 如果模型名不带后缀，就看 `enable_thinking`
- 如果你啥都不传，默认是开思考

## 能不能部署到 Ubuntu 24.04

可以。

这个项目是 Python 写的，在 Ubuntu 24.04 上可以跑。

### 推荐的服务器配置

- 最低可以用 `1C2G`
- 适合自己用、小流量调用
- 不太适合高并发商用场景

### 为什么不是很适合超高并发

因为它本质上不是官方稳定 API，而是网页协议代理：

- 上游规则可能变化
- 游客账号可能失效
- 并发太高可能被限制
- 1C2G 机器本身资源也比较紧

所以建议拿来：

- 自己用
- 给少量朋友用
- 接第三方面板测试

## Ubuntu 24.04 全新服务器部署教程

下面这段是从一台全新 Ubuntu 24.04 服务器开始教。

假设你的项目目录最后放在：

- `/root/zai2_api`

### 第 1 步：安装基础环境

先更新系统并安装软件：

```bash
apt update && apt upgrade -y
apt install -y python3 python3-venv python3-pip nginx apache2-utils curl
```

## 第 2 步：上传项目文件

把这些文件放到服务器目录里：

- `main.py`
- `openai.py`
- `claude_compat.py`
- `README.md`

假设你最后放到了：

- `/root/zai2_api`

检查一下：

```bash
cd /root/zai2_api
ls -lah
```

## 第 3 步：创建虚拟环境

Ubuntu 24.04 不建议直接往系统 Python 里乱装包，不然会报 `externally-managed-environment`。

所以乖乖用虚拟环境就好：

```bash
cd /root/zai2_api
python3 -m venv .venv
source .venv/bin/activate
```

## 第 4 步：安装依赖

激活虚拟环境后再安装：

```bash
pip install --upgrade pip
pip install fastapi uvicorn httpx httpcore
```

验证一下依赖有没有装好：

```bash
python -c "import fastapi,uvicorn,httpx,httpcore; print('ok')"
```

如果输出 `ok`，就说明没问题啦。

## 第 5 步：手动启动测试

先手动跑一次，看看会不会炸。

```bash
cd /root/zai2_api
source .venv/bin/activate
python openai.py
```

如果正常，你会看到类似：

- `Application startup complete`
- `Uvicorn running on http://0.0.0.0:30016`

这时候别关掉这个终端，再开一个终端测试：

```bash
curl http://127.0.0.1:30016/v1/models
```

如果能返回 JSON，说明服务已经活过来了。

## 第 6 步：配置 systemd 开机自启

这样就不用每次手动开终端啦。

创建服务文件：

```bash
nano /etc/systemd/system/zai2api.service
```

粘贴下面内容：

```ini
[Unit]
Description=Z.ai OpenAI Proxy
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/zai2_api
Environment=LOG_LEVEL=INFO
Environment=POOL_SIZE=9
Environment=TOKEN_MAX_AGE=480
ExecStart=/root/zai2_api/.venv/bin/python /root/zai2_api/openai.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

然后执行：

```bash
systemctl daemon-reload
systemctl enable zai2api
systemctl start zai2api
systemctl status zai2api
```

看实时日志：

```bash
journalctl -u zai2api -f
```

### 关于 `POOL_SIZE=9`

这个参数表示预创建 9 个游客账号池。

如果你的服务器只有 `1C2G`，9 个账号池可能有点猛：

- 能跑，不一定最稳
- 如果后面发现内存高、卡顿、掉线多
- 可以改成 `3` 或 `5`

比如改成 3：

```ini
Environment=POOL_SIZE=3
```

改完后重载：

```bash
systemctl daemon-reload
systemctl restart zai2api
```

## 第 7 步：配置 Nginx 让外部访问

虽然你可以直接开放 `30016` 端口，但不太优雅，也不太安全。

更推荐用 Nginx 反代到 `80` 端口。

### 先创建一个访问密码

```bash
htpasswd -c /etc/nginx/.htpasswd admin
```

它会让你输入密码。

### 创建 Nginx 配置

```bash
nano /etc/nginx/sites-available/zai2api
```

粘贴下面内容：

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

## 第 8 步：开放防火墙和安全组

如果你开启了 UFW：

```bash
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw enable
ufw status
```

如果你是云服务器，还要记得去云厂商控制台开放端口：

- `80`
- 如果以后要 HTTPS，再开放 `443`

## 第 9 步：测试外部访问

### 测模型列表

```bash
curl -u admin:你的密码 http://你的服务器IP/v1/models
```

### 测不思考

```bash
curl -u admin:你的密码 http://你的服务器IP/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5-nothink",
    "messages": [
      {"role": "user", "content": "你好"}
    ],
    "stream": false
  }'
```

### 测思考

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

## OpenAI 客户端怎么填

如果你用的是支持 OpenAI 兼容接口的客户端，一般这样填：

- Base URL: `http://你的服务器IP/v1`
- Model: `glm-5-think` 或 `glm-5-nothink`

### API Key 怎么办

这个项目本身默认没有真的校验 `Authorization: Bearer sk-xxx` 那种 API Key。

如果客户端强制要填一个 key，你可以先随便填：

- `sk-test`
- `123456`
- `anything`

真正拦截访问的，是上面 Nginx 的用户名密码。

## Claude 接口怎么调用

Claude 风格接口地址是：

- `POST /v1/messages`

示例：

```bash
curl -u admin:你的密码 http://你的服务器IP/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5-think",
    "messages": [
      {"role": "user", "content": "请简单介绍你自己"}
    ],
    "stream": false
  }'
```

## 常见问题

### 1. 为什么访问 `/` 是 404

因为这个项目没有首页。

你应该访问的是：

- `/v1/models`
- `/v1/chat/completions`
- `/v1/messages`

### 2. 为什么 `curl http://127.0.0.1:30016/v1/models` 连不上

一般是因为服务没启动，或者被你 `Ctrl+C` 关掉了。

先看：

```bash
systemctl status zai2api
```

再看日志：

```bash
journalctl -u zai2api -f
```

### 3. Ubuntu 24.04 为什么 pip 报 `externally-managed-environment`

因为系统不让你直接乱装到系统 Python。

解决方法就是：

- 创建虚拟环境
- 激活虚拟环境
- 再用 `pip install`

也就是：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn httpx httpcore
```

### 4. 9 个账号是不是越多越好

不是哦，笨蛋妹妹认真提醒你：

- 账号池越多，不一定越稳
- 你的机器只有 `1C2G`
- 开太大可能更吃资源

建议：

- 自己一个人用：`POOL_SIZE=1` 到 `3`
- 少量朋友一起用：`POOL_SIZE=3` 到 `5`
- 非要试试：`POOL_SIZE=9`

### 5. 这个项目稳定吗

能用，但不是官方稳定商用 API 那种稳定。

因为它依赖网页协议：

- 上游改接口可能失效
- 游客账号可能被限制
- 并发大时可能不稳定

所以更适合：

- 个人玩
- 小规模使用
- 开发测试

## 建议再做的一件事：加 swap

如果你的机器是 `1C2G`，建议加 2G swap，能稳一点：

```bash
fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
free -h
```

## 最后说人话总结

如果你懒得看太多，就记住这几件事：

- 这是一个 Z.ai 的代理 API
- Ubuntu 24.04 可以部署
- 推荐用 `systemd + nginx`
- 思考模型用 `glm-5-think`
- 非思考模型用 `glm-5-nothink`
- 1C2G 能跑，但别指望它扛很大并发

如果你照着做还是不会，呜呜，那就把报错贴出来，我陪你继续一点一点修。
