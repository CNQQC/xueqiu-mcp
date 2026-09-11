# 部署

服务器主动拉取，GitHub 不需要任何进入你机器的凭证 —— 不开入站端口、不存私钥。
代价是部署有最多 5 分钟延迟。

## 布局

```
/opt/xueqiu-mcp/
├── .src/              仓库检出（只读来源，定时 reset 到 origin/main）
├── xueqiu_mcp/        真正被服务加载的包（由 .src/src/xueqiu_mcp/ 同步而来）
├── serve_http.py      服务器独有：以 streamable-http 启动，不在仓库里
├── .env               服务器独有：环境变量
├── .venv/             虚拟环境
└── .deployed-sha      当前已部署的 commit
```

`serve_http.py` 和 `.env` 只在服务器上。所以同步的是**包目录**而不是整个应用目录 ——
`rsync --delete` 的作用范围被限制在 `xueqiu_mcp/` 里，碰不到它们。

## 扫码登录网页

启用 `get_login_qrcode` 需要在服务的虚拟环境安装本项目的 `[browser]` 依赖和 Chromium
（`playwright install chromium`）。MCP SDK 需 `>=1.30,<3`；仅同步 Python 包目录不会更新依赖。

在服务环境中设置 `XUEQIU_TRANSPORT=streamable-http` 和
`XUEQIU_PUBLIC_BASE_URL=https://你的域名/服务前缀`，后者不含 `/mcp`。
`serve_http.py` 应使用 `xueqiu_mcp.server.mcp.streamable_http_app()`，以包含登录网页路由。

反向代理将 `GET /服务前缀/login/<token>`、`GET /服务前缀/login/<token>/state` 转给同一
MCP 进程的 `/login/...`。这两条路径使用 5 分钟随机 token，浏览器访问无需长期 Bearer；
保留 `/mcp` 的原有鉴权，并避免记录登录 URL 的访问日志。不要放行其他管理接口。
使用单 worker，或将登录网页和发起取码的 MCP 请求固定路由到同一进程。

没有配置对外地址时仍能返回 MCP 二维码图片和网页相对路径。修改后验证工具列表含
`get_login_qrcode`、调用返回 `text` + `image`，以及临时链接能读取 `pending` 状态；
由账号本人扫码后检查 `login_status` 和凭证保存结果。

## 一次部署做了什么

1. `git fetch && reset --hard origin/main`；HEAD 与 `.deployed-sha` 相同就安静退出；
2. 备份当前 `xueqiu_mcp/`；
3. rsync 新代码；
4. **先旁路 import 一遍**。语法错、缺依赖在这里就被拦下，服务连停都不用停，
   坏提交零停机；
5. 重启服务，然后发一次真正的 MCP `initialize` 握手做健康检查 —— 只看
   `systemctl is-active` 不够，进程起着但端口没监听时它照样报 active；
6. 不健康就还原备份并重启，只保留最近 3 份备份。

## 安装（在服务器上执行一次）

```bash
git clone https://github.com/CNQQC/xueqiu-mcp.git /opt/xueqiu-mcp/.src
install -m 755 /opt/xueqiu-mcp/.src/deploy/xueqiu-deploy /usr/local/bin/xueqiu-deploy
install -m 644 /opt/xueqiu-mcp/.src/deploy/xueqiu-deploy.{service,timer} /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now xueqiu-deploy.timer
```

## 运维

```bash
xueqiu-deploy                              # 立刻部署一次，不等定时器
systemctl list-timers xueqiu-deploy.timer  # 下次什么时候跑
journalctl -u xueqiu-deploy -n 50          # 部署日志
cat /opt/xueqiu-mcp/.deployed-sha          # 当前跑的是哪个提交
```

回滚到某个旧版本：把那个 commit 推成 main 的 HEAD 即可，定时器会自己跟上。
紧急情况直接 `mv /opt/xueqiu-mcp/xueqiu_mcp.bak-<时间戳> /opt/xueqiu-mcp/xueqiu_mcp`
再 `systemctl restart xueqiu-mcp`。
