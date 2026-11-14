#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NCMM email notification plugin (ncmm-mail).

A zero-dependency, single-file Python 3 plugin for ncmm
(https://github.com/3899/ncmm).

Three ways to use it:
  1. WebUI plugin centre -> "online config" writes config.yaml; ncmm reads
     plugin.json params and renders the form. No manual file editing needed.
  2. Built-in web console: run --serve and open http://127.0.0.1:8787/
     to edit sender name / sender address / recipients / SMTP settings.
  3. CLI: `ncmm plugin run mail --test`, or scheduled via WebUI cron.

Observed ncmm behaviour on 2026-11-14 that differs from docs/plugin.md:
  - NCMM_CONFIG_PATH may be "default" (not absolute)
  - NCMM_ACCOUNT_COOKIE may be "./cookie.json" (not absolute)
  - cwd is the caller's cwd, NOT NCMM_HOME
  - PluginContext.version carries the ncmm version, not "1.0.0"
All paths are therefore resolved against NCMM_HOME / the plugin directory.

License: MIT
"""

import argparse
import html
import json
import os
import re
import smtplib
import socket
import ssl
import sys
import urllib.parse
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.0"
PLUGIN_NAME = "mail"

DEFAULTS = {
    "sender_name": "NCMM 通知",
    "mail_from": "",
    "mail_to": [],
    "smtp_host": "smtp.qq.com",
    "smtp_port": 465,
    "smtp_security": "ssl",          # ssl | starttls | none
    "smtp_user": "",
    "smtp_password": "",
    "subject_prefix": "[ncmm]",
    "timeout": 15,
    "webhook_host": "127.0.0.1",
    "webhook_port": 8787,
    "webhook_token": "",
    "auto_notify": False,
    "dry_run": True,
}

# Fields the web console / WebUI online-config are allowed to write.
EDITABLE = [
    "sender_name", "mail_from", "mail_to", "smtp_host", "smtp_port",
    "smtp_security", "smtp_user", "smtp_password", "subject_prefix",
    "webhook_port", "webhook_token", "auto_notify", "dry_run",
]
SECRET_KEYS = {"smtp_password", "webhook_token"}

ENV_MAP = {
    "NCMM_MAIL_SENDER_NAME": ("sender_name", str),
    "NCMM_MAIL_FROM": ("mail_from", str),
    "NCMM_MAIL_TO": ("mail_to", "list"),
    "NCMM_MAIL_SMTP_HOST": ("smtp_host", str),
    "NCMM_MAIL_SMTP_PORT": ("smtp_port", int),
    "NCMM_MAIL_SMTP_SECURITY": ("smtp_security", str),
    "NCMM_MAIL_SMTP_USER": ("smtp_user", str),
    "NCMM_MAIL_SMTP_PASSWORD": ("smtp_password", str),
    "NCMM_MAIL_SUBJECT_PREFIX": ("subject_prefix", str),
    "NCMM_MAIL_WEBHOOK_PORT": ("webhook_port", int),
    "NCMM_MAIL_WEBHOOK_TOKEN": ("webhook_token", str),
}


# --------------------------------------------------------------------------
# paths & config
# --------------------------------------------------------------------------

def plugin_dir():
    return os.path.dirname(os.path.abspath(__file__))


def resolve_home(explicit=None):
    if explicit:
        return os.path.abspath(explicit)
    env = os.environ.get("NCMM_HOME")
    if env:
        return os.path.abspath(env)
    here = plugin_dir()
    parent = os.path.dirname(here)
    # subdirectory layout: {home}/plugins/ncmm-mail/ncmm-mail.py
    if os.path.basename(parent) == "plugins":
        return os.path.dirname(parent)
    # single-file layout: {home}/plugins/ncmm-mail.py
    return parent


def yaml_scalar(val):
    val = val.strip()
    if not val or val == "[]":
        return [] if val == "[]" else ""
    if val[0] in "\"'" and val[-1] == val[0] and len(val) >= 2:
        return val[1:-1]
    low = val.lower()
    if low in ("true", "false", "yes", "no", "on", "off"):
        return low in ("true", "yes", "on")
    if re.fullmatch(r"-?\d+", val):
        return int(val)
    return val.split(" #", 1)[0].strip()


def parse_simple_yaml(text):
    """Tiny flat YAML reader: key: value, inline lists, and - item lists.
    Enough for plugin config.yaml; avoids a PyYAML dependency."""
    out, list_key = {}, None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        if stripped.startswith("- ") and list_key:
            out[list_key].append(yaml_scalar(stripped[2:]))
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if not key:
            continue
        if val == "":
            out[key] = []
            list_key = key
        else:
            out[key] = yaml_scalar(val)
            list_key = None
    return out


def dump_simple_yaml(cfg):
    lines = [
        "# NCMM 邮箱通知插件配置 - 由 WebUI 在线配置或内置面板写入",
        "# 也可手动编辑。密码建议改用环境变量，不要提交到版本库。",
        "",
    ]
    for key in EDITABLE:
        val = cfg.get(key, DEFAULTS.get(key, ""))
        if key == "mail_to":
            lines.append("mail_to:")
            for item in (val or []):
                lines.append("  - %s" % item)
        elif isinstance(val, bool):
            lines.append("%s: %s" % (key, "true" if val else "false"))
        elif isinstance(val, int):
            lines.append("%s: %d" % (key, val))
        else:
            text = str(val or "")
            # Quote values a standard YAML parser could misread as flow
            # collections, tags, aliases or containing comment markers.
            if text == "" or text[0] in "[{&*!|>%@`" or any(c in text for c in "#:'\"\n"):
                text = '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
            lines.append("%s: %s" % (key, text))
    return "\n".join(lines) + "\n"


def normalise(cfg):
    to = cfg.get("mail_to")
    if isinstance(to, str):
        cfg["mail_to"] = [x.strip() for x in to.split(",") if x.strip()]
    elif isinstance(to, list):
        cfg["mail_to"] = [str(x).strip() for x in to if str(x).strip()]
    else:
        cfg["mail_to"] = []
    for key in ("smtp_port", "webhook_port", "timeout"):
        try:
            cfg[key] = int(cfg[key])
        except (ValueError, TypeError, KeyError):
            cfg[key] = DEFAULTS[key]
    for key in ("dry_run", "auto_notify"):
        v = cfg.get(key)
        cfg[key] = v if isinstance(v, bool) else str(v).lower() in ("true", "1", "yes")
    return cfg


def load_config(home, config_path=None):
    cfg = dict(DEFAULTS)
    path = config_path or os.path.join(plugin_dir(), "config.yaml")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg.update(parse_simple_yaml(f.read()))
            cfg["_source"] = path
        except OSError as e:
            sys.stderr.write("[ncmm-mail] config read failed: %s\n" % e)
    else:
        cfg["_source"] = path + " (missing, using defaults)"
    # legacy JSON config, kept for older single-file installs
    legacy = os.path.join(resolve_home(home), "plugins", "mail.config.json")
    if os.path.isfile(legacy):
        try:
            with open(legacy, "r", encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old, dict):
                for k, v in old.items():
                    if k in DEFAULTS and not cfg.get(k):
                        cfg[k] = v
        except (OSError, ValueError):
            pass
    for env_name, (key, caster) in ENV_MAP.items():
        raw = os.environ.get(env_name)
        if not raw:
            continue
        if caster == "list":
            cfg[key] = [x.strip() for x in raw.split(",") if x.strip()]
        else:
            try:
                cfg[key] = caster(raw)
            except ValueError:
                pass
    # ncmm daemon params use `port` / `host` (same names as ncmm-qqbot).
    # Accept them as aliases so online-config changes actually take effect.
    if cfg.get("port"):
        cfg["webhook_port"] = cfg["port"]
    if cfg.get("host"):
        cfg["webhook_host"] = cfg["host"]
    return normalise(cfg)


def save_config(cfg, path=None):
    path = path or os.path.join(plugin_dir(), "config.yaml")
    clean = {k: cfg.get(k, DEFAULTS.get(k)) for k in EDITABLE}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(dump_simple_yaml(clean))
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def config_problems(cfg):
    missing = []
    for key in ("mail_from", "smtp_host", "smtp_user", "smtp_password"):
        if not cfg.get(key):
            missing.append(key)
    if not cfg.get("mail_to"):
        missing.append("mail_to")
    if cfg.get("smtp_security") not in ("ssl", "starttls", "none"):
        missing.append("smtp_security")
    return missing


# --------------------------------------------------------------------------
# mail
# --------------------------------------------------------------------------

def render_email(cfg, title, content, level="info", extra=None):
    prefix = cfg.get("subject_prefix") or "[ncmm]"
    subject = "%s [%s] %s" % (prefix, str(level).upper(), title)
    lines = [content or "(no content)", "", "---", "level   : %s" % level]
    for k, v in (extra or {}).items():
        lines.append("%-7s : %s" % (k, v))
    text = "\n".join(lines)

    esc = lambda s: html.escape(str(s), quote=False)
    rows = "".join(
        "<tr><td style='padding:4px 10px;color:#666'>%s</td><td style='padding:4px 10px'>%s</td></tr>"
        % (esc(k), esc(v)) for k, v in (extra or {}).items()
    )
    html_body = (
        "<div style='font:14px/1.6 -apple-system,Segoe UI,Arial,sans-serif;color:#222'>"
        "<h2 style='font-size:16px;margin:0 0 12px'>%s</h2>"
        "<div style='white-space:pre-wrap;background:#f6f7f9;padding:12px;border-radius:6px'>%s</div>"
        "<table style='margin-top:14px;border-collapse:collapse;font-size:12px'>%s</table>"
        "</div>" % (esc(title), esc(content or "(no content)"), rows)
    )
    return subject, text, html_body


def send_mail(cfg, subject, text, html_body):
    if cfg.get("dry_run"):
        sys.stdout.write("[ncmm-mail] DRY-RUN, would send:\n")
        sys.stdout.write("  from    : %s <%s>\n"
                         % (cfg.get("sender_name") or "", cfg.get("mail_from") or ""))
        sys.stdout.write("  to      : %s\n" % ", ".join(cfg.get("mail_to", [])))
        sys.stdout.write("  subject : %s\n  smtp    : %s:%s (%s)\n"
                         % (subject, cfg.get("smtp_host"), cfg.get("smtp_port"), cfg.get("smtp_security")))
        sys.stdout.write("  -------- body --------\n%s\n  -----------------------\n" % text)
        return True, "dry-run"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg.get("sender_name") or "", cfg["mail_from"]))
    msg["To"] = ", ".join(cfg["mail_to"])
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="ncmm.local")
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")

    host, port = cfg["smtp_host"], int(cfg["smtp_port"])
    security, timeout = cfg["smtp_security"], float(cfg.get("timeout") or 15)
    ctx = ssl.create_default_context()
    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx)
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
            if security == "starttls":
                server.starttls(context=ctx)
        with server:
            if cfg.get("smtp_user"):
                server.login(cfg["smtp_user"], cfg["smtp_password"])
            server.send_message(msg)
        return True, "sent"
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def plugin_result(ok, message, data=None):
    # should_notify=false avoids a loop: host notify -> webhook -> bridge -> notify
    result = {
        "success": bool(ok), "message": message, "data": data or {},
        "notify": {"should_notify": False, "title": "ncmm 邮箱通知",
                   "content": message, "level": "info" if ok else "error"},
    }
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")


def read_context():
    """Read PluginContext from stdin without ever blocking.

    `ncmm plugin run` writes a JSON document to stdin (verified). A daemon
    launch normally gives /dev/null or an idle pipe, so an empty/short read
    means "act as the long-running service". A 0.3s select keeps an idle
    pipe from hanging the process.
    """
    if sys.stdin.isatty():
        return {}
    try:
        if os.name != "nt":
            import select
            if not select.select([sys.stdin], [], [], 0.3)[0]:
                return {}
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except ValueError:
        return {"_raw": raw[:2000]}


# --------------------------------------------------------------------------
# web console + notify bridge
# --------------------------------------------------------------------------

CONSOLE_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NCMM 邮箱通知 - 配置</title>
<style>
body{font:14px/1.6 -apple-system,Segoe UI,Arial,sans-serif;color:#222;background:#f7f8fa;margin:0;padding:24px}
.wrap{max-width:720px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:24px}
h1{font-size:18px;margin:0 0 4px}p.sub{color:#666;margin:0 0 20px;font-size:13px}
label{display:block;font-weight:600;margin:12px 0 4px;font-size:13px}
input,select{width:100%;box-sizing:border-box;padding:8px 10px;border:1px solid #d1d5db;border-radius:5px;font:14px inherit}
.row{display:flex;gap:12px}.row>div{flex:1}
button{margin-top:18px;padding:9px 18px;border:0;border-radius:5px;background:#2563eb;color:#fff;cursor:pointer;font-size:14px}
button.sec{background:#6b7280;margin-left:8px}
#msg{margin-top:14px;font-size:13px;min-height:20px}
.hint{font-size:12px;color:#888;font-weight:400}
</style></head><body><div class="wrap">
<h1>NCMM 邮箱通知插件</h1>
<p class="sub">配置保存在插件目录 config.yaml，与 WebUI「在线配置」共用同一份文件。</p>
<form id="f">
<label>发件人名字</label><input name="sender_name" placeholder="NCMM 通知">
<label>发件邮箱</label><input name="mail_from" placeholder="bot@example.com">
<label>收件人 <span class="hint">多个用逗号分隔</span></label><input name="mail_to" placeholder="you@example.com">
<div class="row"><div><label>SMTP 服务器</label><input name="smtp_host"></div>
<div><label>端口</label><input name="smtp_port" type="number"></div></div>
<div class="row"><div><label>加密方式</label><select name="smtp_security"><option value="ssl">SSL (465)</option><option value="starttls">STARTTLS (587)</option><option value="none">无</option></select></div>
<div><label>主题前缀</label><input name="subject_prefix"></div></div>
<label>SMTP 账号</label><input name="smtp_user">
<label>SMTP 密码 / 授权码 <span class="hint">留空表示沿用已保存值</span></label><input name="smtp_password" type="password" autocomplete="new-password">
<label>桥接端口</label><input name="webhook_port" type="number">
<label>桥接令牌 <span class="hint">可选，非空时路径为 /notify/&lt;token&gt;</span></label><input name="webhook_token">
<label><input type="checkbox" name="dry_run" style="width:auto"> 仅预览不发信（调试）</label>
<button type="submit">保存配置</button><button type="button" class="sec" id="test">发送测试邮件</button>
</form><div id="msg"></div></div>
<script>
const $=s=>document.querySelector(s), msg=$('#msg');
function fill(d){const c=d.config||{};for(const k of ['sender_name','mail_from','smtp_host','smtp_port','smtp_security','smtp_user','subject_prefix','webhook_port','webhook_token'])if(c[k]!==undefined)$('[name='+k+']').value=c[k];
$('[name=mail_to]').value=(c.mail_to||[]).join(',');$('[name=dry_run]').checked=!!c.dry_run;$('[name=smtp_password]').value='';}
fetch('/api/config').then(r=>r.json()).then(fill).catch(e=>msg.textContent='读取配置失败: '+e);
$('#f').addEventListener('submit',async e=>{e.preventDefault();const d={};new FormData($('#f')).forEach((v,k)=>d[k]=v);d.dry_run=$('[name=dry_run]').checked;d.mail_to=d.mail_to.split(',').map(x=>x.trim()).filter(Boolean);
const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});const j=await r.json();msg.textContent=j.message||(j.ok?'已保存':'保存失败');});
$('#test').addEventListener('click',async()=>{const r=await fetch('/api/test',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const j=await r.json();msg.textContent=j.message||(j.ok?'测试邮件已提交':'失败');});
</script></body></html>"""


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "ncmm-mail/" + VERSION
    cfg = None
    cfg_path = None

    def _refresh(self):
        """Reload config.yaml so changes saved in the WebUI online-config
        take effect without restarting the daemon."""
        try:
            BridgeHandler.cfg = load_config(resolve_home(), BridgeHandler.cfg_path)
        except Exception as e:
            sys.stderr.write("[ncmm-mail] config reload failed: %s\n" % e)

    def _reply(self, code, payload, ctype="application/json; charset=utf-8"):
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        else:
            body = str(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _token_ok(self):
        token = (self.cfg.get("webhook_token") or "").strip()
        if not token or self.path.startswith("/api/") or self.path == "/":
            return True
        return self.path.split("?", 1)[0].rstrip("/") == "/notify/" + token

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._reply(200, CONSOLE_HTML, "text/html; charset=utf-8")
            return
        if path == "/api/config":
            safe = {k: self.cfg.get(k) for k in EDITABLE}
            for k in SECRET_KEYS:
                if safe.get(k):
                    safe[k] = "******"
            self._reply(200, {"ok": True, "config": safe,
                              "problems": config_problems(self.cfg)})
            return
        if path.startswith("/notify"):
            self._reply(200, {"ok": True, "service": "ncmm-mail", "version": VERSION})
            return
        self._reply(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        self._refresh()
        path = self.path.split("?", 1)[0]
        if not self._token_ok():
            self._reply(403, {"ok": False, "error": "bad token"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, 65536)).decode("utf-8", "replace")
        except (ValueError, OSError):
            raw = ""
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                payload = {}
        except ValueError:
            payload = {}

        if path == "/api/config":
            updates = {}
            for key in EDITABLE:
                if key not in payload:
                    continue
                val = payload[key]
                if key in SECRET_KEYS and val == "******":
                    continue
                if key in ("dry_run", "auto_notify"):
                    val = bool(val)
                updates[key] = val
            new_cfg = normalise(dict(self.cfg, **updates))
            try:
                saved = save_config(new_cfg, self.cfg_path)
                BridgeHandler.cfg = load_config(resolve_home(), self.cfg_path)
                self._reply(200, {"ok": True, "message": "已保存 " + saved,
                                  "problems": config_problems(BridgeHandler.cfg)})
            except OSError as e:
                self._reply(500, {"ok": False, "message": "保存失败: %s" % e})
            return

        if path == "/api/test":
            cfg = self.cfg
            subject, text, html_body = render_email(
                cfg, "邮箱通知测试", "这是一封来自 NCMM 邮箱通知插件的测试邮件。", "info",
                {"time": formatdate(localtime=True), "host": socket.gethostname(),
                 "source": "web console"})
            ok, detail = send_mail(cfg, subject, text, html_body)
            self._reply(200 if ok else 502,
                        {"ok": ok, "message": ("测试邮件已发送" if ok else "发送失败") + "：" + detail})
            return

        if path.startswith("/notify"):
            title = str(payload.get("title") or "ncmm 通知")
            content = str(payload.get("content") or payload.get("text") or raw or "(no content)")
            level = str(payload.get("level") or "warn")
            extra = {"time": payload.get("time") or formatdate(localtime=True),
                     "host": payload.get("host") or socket.gethostname(),
                     "source": "notify.webhook"}
            subject, text, html_body = render_email(self.cfg, title, content, level, extra)
            ok, detail = send_mail(self.cfg, subject, text, html_body)
            self._reply(200 if ok else 502, {"ok": ok, "detail": detail, "subject": subject})
            return
        self._reply(404, {"ok": False, "error": "not found"})

    def log_message(self, fmt, *a):
        sys.stderr.write("[ncmm-mail] %s\n" % (fmt % a))


def run_bridge_mode(cfg, cfg_path):
    host = cfg.get("webhook_host") or "127.0.0.1"
    port = int(cfg.get("webhook_port") or 8787)
    BridgeHandler.cfg, BridgeHandler.cfg_path = cfg, cfg_path
    sys.stderr.write("[ncmm-mail] console: http://%s:%d/  bridge: http://%s:%d/notify\n"
                     % (host, port, host, port))
    httpd = ThreadingHTTPServer((host, port), BridgeHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def run_plugin_mode(cfg, args, ctx):
    problems = config_problems(cfg)
    if args.check:
        plugin_result(not problems,
                      "配置完整" if not problems else "配置不完整: " + ", ".join(problems),
                      {"config": cfg["_source"], "missing": problems,
                       "sender_name": cfg.get("sender_name"),
                       "mail_from": cfg.get("mail_from"), "mail_to": cfg.get("mail_to")})
        return 0 if not problems else 1

    level = args.level or "info"
    title = args.title or "ncmm 插件运行报告"
    content = args.content
    if content is None:
        acc = ctx.get("account", {}) if isinstance(ctx.get("account"), dict) else {}
        content = "\n".join([
            "ncmm 插件已执行。", "",
            "版本    : %s" % ctx.get("version", "unknown"),
            "命令    : %s" % ctx.get("command", "plugin run"),
            "home    : %s" % ctx.get("home", os.environ.get("NCMM_HOME", "")),
            "账号    : %s%s" % (acc.get("filepath", "-"),
                                " (主账号)" if acc.get("is_main") else ""),
            "参数    : %s" % " ".join(args.forward or []),
        ])
    extra = {"time": formatdate(localtime=True), "host": socket.gethostname(),
             "plugin": "mail v" + VERSION, "ncmm": ctx.get("version", "-")}
    if problems and not cfg.get("dry_run"):
        plugin_result(False, "配置不完整: " + ", ".join(problems), {"missing": problems})
        return 1
    subject, text, html_body = render_email(cfg, title, content, level, extra)
    ok, detail = send_mail(cfg, subject, text, html_body)
    plugin_result(ok, "mail %s" % detail,
                  {"subject": subject, "to": cfg.get("mail_to", []),
                   "dry_run": bool(cfg.get("dry_run")), "detail": detail})
    return 0 if ok else 1


def main():
    # Launch diagnostics: the host starts daemon plugins with its own argv.
    # Keeping this tiny record makes future integration issues diagnosable.
    try:
        with open("/tmp/ncmm_mail_launch.txt", "w", encoding="utf-8") as _f:
            _f.write("argv=%r\n" % (sys.argv,))
            _f.write("ncmm_env=%r\n" % ({k: v for k, v in os.environ.items()
                                         if k.startswith("NCMM_")},))
            _f.write("cwd=%s\n" % os.getcwd())
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="NCMM 邮箱通知插件")
    ap.add_argument("--serve", action="store_true", help="启动内置 Web 配置面板 + notify.webhook 桥接")
    ap.add_argument("--test", action="store_true", help="仅预览不发信")
    ap.add_argument("--check", action="store_true", help="只校验配置")
    ap.add_argument("--title")
    ap.add_argument("--content")
    ap.add_argument("--level", default="info", choices=["info", "warn", "error"])
    ap.add_argument("--home", help="覆盖 ncmm home")
    ap.add_argument("--config", help="覆盖配置文件路径")
    ap.add_argument("--to", help="覆盖收件人，逗号分隔")
    ap.add_argument("--dry-run", action="store_true")
    args, forward = ap.parse_known_args()
    args.forward = forward

    home = resolve_home(args.home)
    cfg_path = os.path.abspath(args.config) if args.config else os.path.join(plugin_dir(), "config.yaml")
    cfg = load_config(home, cfg_path)
    if args.to:
        cfg["mail_to"] = [x.strip() for x in args.to.split(",") if x.strip()]
    if args.test or args.dry_run:
        cfg["dry_run"] = True

    # ncmm starts daemon plugins with its own flag (strings show
    # "plugin-daemon"). Only match FLAG names, never argument values:
    # a mail title containing the word "daemon" must not trigger this.
    host_daemon = any(
        x.startswith("--") and ("daemon" in x.lower() or x == "--background")
        for x in sys.argv[1:]
    )
    args.forward = [x for x in args.forward
                    if not (x.startswith("--") and ("daemon" in x.lower()
                                                     or x == "--background"))]
    ctx = {} if host_daemon else read_context()  # non-blocking
    # Only flags a human/task would pass count as a one-shot command.
    # Unknown argv (host daemon flags, --port, etc.) must NOT flip the mode.
    business = bool(args.test or args.check or args.title or args.content
                    or args.to or args.dry_run)
    if args.serve or host_daemon or (not ctx and not business):
        # daemon launch: no PluginContext + no explicit command -> stay resident
        return run_bridge_mode(cfg, cfg_path)
    return run_plugin_mode(cfg, args, ctx)


if __name__ == "__main__":
    sys.exit(main())
