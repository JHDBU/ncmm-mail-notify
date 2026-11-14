# 云村邮件通 · NCMM 邮箱通知插件

给 [ncmm](https://github.com/3899/ncmm) 补一个邮箱通道：在插件中心可视化设置**发件人名字、发件邮箱、收件人**，把每日任务汇总和失败告警自动发到邮箱。

ncmm 原生通知支持 Webhook / Bark / Server酱 / Telegram / 钉钉 / PushPlus / 企业微信，**唯独没有邮件**。本插件补上 SMTP，并且提供两个配置入口，不需要手动改文件。

- 零第三方依赖，只用 Python 3 标准库（`smtplib` / `http.server`）
- 同时支持 SSL(465)、STARTTLS(587)、明文(25)
- 纯文本 + HTML 双格式邮件，中文不乱码
- 内置独立 Web 配置面板，可在线改配置并发测试邮件
- 只监听 `127.0.0.1`，不对外暴露端口

## 安装

**方式一：WebUI 插件中心在线安装（推荐）**

打开 `ncmm web` → 插件中心 → 在线安装插件，填入：

```
JHDBU/ncmm-mail-notify
```

**方式二：命令行**

```bash
ncmm plugin install JHDBU/ncmm-mail-notify
```

**方式三：手动安装到现有 home**

```bash
git clone https://github.com/JHDBU/ncmm-mail-notify.git
cd ncmm-mail-notify
NCMM_HOME_DIR=/opt/ncmm/home bash install.sh
```

脚本会把插件装成 `{home}/plugins/ncmm-mail/` 子目录形态（与 ncmm-qqbot 一致），包含 `plugin.json`、`config.yaml.example`、`source.json`。已有的 `config.yaml` 不会被覆盖。

## 配置

装好后有两种可视化方式，改的是同一份 `config.yaml`：

1. **WebUI 插件中心** → 找到「云村邮件通」→ 点【在线配置】。表单字段由 `plugin.json` 的 `params` 生成。
2. **独立 Web 面板** → 启动常驻服务后访问 `http://127.0.0.1:8787/`，可改配置并点「发送测试邮件」。

| 字段 | 说明 | 默认值 |
| --- | --- | --- |
| `sender_name` | 发件人显示名字 | 云村邮件通 |
| `mail_from` | 发件邮箱 | 空（必填） |
| `mail_to` | 收件人，多个用逗号分隔 | 空（必填） |
| `smtp_host` / `smtp_port` | SMTP 服务器与端口 | smtp.qq.com / 465 |
| `smtp_security` | `ssl` / `starttls` / `none` | ssl |
| `smtp_user` / `smtp_password` | 账号与授权码 | 空（必填） |
| `subject_prefix` | 主题前缀 | [ncmm] |
| `webhook_port` | 面板与桥接端口 | 8787 |
| `webhook_token` | 非空时通知路径为 `/notify/<token>` | 空 |
| `dry_run` | 只打印预览不发信 | true |

密码也可以完全不落盘，用环境变量（或 systemd `EnvironmentFile`）：

```bash
NCMM_MAIL_SMTP_HOST / PORT / SECURITY / USER / PASSWORD
NCMM_MAIL_FROM / NCMM_MAIL_TO / NCMM_MAIL_SENDER_NAME
NCMM_MAIL_WEBHOOK_TOKEN / NCMM_MAIL_SUBJECT_PREFIX
```

配置齐全后把 `dry_run` 改成 `false`。

## 使用

**插件调用 / 定时任务**

```bash
ncmm plugin run mail --test                       # 预览，不真发
ncmm plugin run mail --title 每日状态 --level info
ncmm plugin run mail --check                     # 检查配置完整性
```

WebUI 定时任务里新建规则：命令 `plugin`、参数 `run mail --title 每日状态`、cron `0 8 * * *`。

插件结束时按 ncmm 规范输出一行 `PluginResult` JSON；`notify.should_notify` 固定为 `false`，避免与宿主通知形成循环。

**把 ncmm 自己的失败通知也发到邮箱**

宿主在任务失败时汇总推一条 `notify.webhook`。把 Web 面板/桥接常驻起来，再把 webhook 指过去即可：

```bash
systemctl enable --now ncmm-mail-bridge
```

`{home}/notify.yaml`（**只改 webhook 段，保留其它通道**）：

```yaml
webhook:
  enabled: true
  url: "http://127.0.0.1:8787/notify"
  method: POST
  headers: {}
  body_template: ""
```

`{home}/config.yaml` 里 `notify.enabled: true`。健康检查：`curl -sS http://127.0.0.1:8787/`。

> 桥接服务随 systemd 常驻，没有声明 `type: daemon`（那是 ncmm 伴生插件的生命周期约定）。这样 `plugin run` 与常驻服务互不干扰，也不会被宿主无参拉起。

## 安全说明

- `config.yaml` 含 SMTP 凭据，已在 `.gitignore` 排除；文件权限 `0600`。
- 面板与桥接只绑 `127.0.0.1`；若要经公网访问，请自行套 nginx + HTTPS + 认证。
- 设置 `webhook_token` 后，通知路径变为 `/notify/<token>`，可避免本机其它进程冒用。
- Web 面板读取配置时对密码和令牌做脱敏；密码框留空表示沿用已保存值。
- 插件不会修改宿主的 `config.yaml` 与 `notify.yaml`，这两处需要显式配置。

## 兼容性

按 ncmm `docs/plugin.md` 的官方契约实现，并在 v1.2.10 上实测。实测发现与文档有出入的地方（路径解析时已规避）：

- `NCMM_CONFIG_PATH` 可能是 `"default"`、`NCMM_ACCOUNT_COOKIE` 可能是 `"./cookie.json"`，都不是绝对路径；
- 子进程 `cwd` 是调用者目录而非 `NCMM_HOME`；
- stdin `PluginContext.version` 是 ncmm 版本号，不是 `"1.0.0"`。

## License

MIT
