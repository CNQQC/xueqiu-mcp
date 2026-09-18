# 部署

main 更新后先运行 CI；全部通过后，GitHub Actions 的 `Deploy VPS` 工作流通过专用
SSH key 调用 VPS 上的部署入口，拉取已验证的提交并重启服务。失败会显示在 Actions。
原有的 5 分钟定时拉取在启用 Actions 后应停用，避免绕过 CI。

## GitHub Actions 配置

仓库 Actions secret：`VPS_DEPLOY_KEY`（专用 SSH 私钥）。仓库 Actions variables：
`VPS_HOST`、`VPS_USER`、`VPS_KNOWN_HOSTS`（经可信 SSH 连接核验的主机公钥记录）。

专用公钥在 VPS 的 `authorized_keys` 中使用如下限制：

```
restrict,command="/usr/local/bin/xueqiu-deploy" ssh-ed25519 <专用公钥> xueqiu-github-actions
```

该 key 只能触发部署；入口仅接受完整的 40 位提交 SHA。部署前获取最新 main，若 CI
对应的提交已过时则跳过，等待最新提交通过 CI。并发部署通过文件锁串行执行。
需要重试时，在 GitHub Actions 重新运行该提交的 CI，或手动运行 main 分支的 CI。

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

1. 获取部署锁，`git fetch` 并确认 CI 提交仍是最新 main，再 `reset --hard origin/main`；
   HEAD 与 `.deployed-sha` 相同就安静退出；
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
systemctl daemon-reload
# 使用 Actions 自动部署时，停用旧定时器。
systemctl disable --now xueqiu-deploy.timer
```

## 运维

```bash
xueqiu-deploy                              # 运维手动部署最新 main（不等待 CI）
# Actions 的部署日志在 GitHub Actions → Deploy VPS 中。
journalctl -u xueqiu-mcp -n 50             # 应用日志
cat /opt/xueqiu-mcp/.deployed-sha          # 当前跑的是哪个提交
```

回滚到某个旧版本：在 main 提交对应的 revert，CI 通过后 Actions 会自动部署。
紧急情况直接 `mv /opt/xueqiu-mcp/xueqiu_mcp.bak-<时间戳> /opt/xueqiu-mcp/xueqiu_mcp`
再 `systemctl restart xueqiu-mcp`。
