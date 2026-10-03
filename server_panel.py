#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CodeBuddy 通知管理面板 · 后端
零第三方依赖（Python 标准库）。运行: python server_panel.py
浏览器打开: http://localhost:8530
"""
import json
import os
import re
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 数据目录: 打包成 exe 后 __file__ 指向临时解压目录,
# 必须改用 exe 所在目录, 否则配置/日志/缓存会随临时目录一起丢失
if getattr(sys, "frozen", False):
    BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "panel_data")
WEB = os.path.join(BASE, "web")
os.makedirs(DATA, exist_ok=True)

SERVERS_FILE = os.path.join(DATA, "servers.json")
CONFIG_FILE = os.path.join(DATA, "config.json")
INSTALLER = os.path.join(BASE, "install_notify.sh")
VERSION_FILE = os.path.join(BASE, "version.json")
SSH_CONFIG = os.path.expanduser("~/.ssh/config")
PORT = 8530

# 当前版本号(单一来源, exe 内嵌; version.json 仅用于 git 仓库用户的对照)
APP_VERSION = "1.6.2"

# 配置/服务器列表文件读写锁: 多线程(GUI+监听+托盘)共享, 防止读半截写坏
_io_lock = threading.Lock()


# ---------------- 数据读写 ----------------
def load_config():
    default = {
        "topic": "",   # 首次使用请在设置页填入自己的 ntfy 频道名
        "host_tag": socket.gethostname(),
        "github_repo": "yoush0uldknowme/AI_notice",  # 默认检查上游仓库更新
    }
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            cfg = json.load(open(CONFIG_FILE, encoding="utf-8"))
        except Exception:
            cfg = {}
    changed = False
    for k, v in default.items():
        if k not in cfg:
            cfg[k] = v
            changed = True
    if changed:
        json.dump(cfg, open(CONFIG_FILE, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    return cfg


def load_servers():
    with _io_lock:
        if os.path.exists(SERVERS_FILE):
            try:
                return json.load(open(SERVERS_FILE, encoding="utf-8"))
            except Exception:
                pass
    return {"servers": []}


def save_servers(data):
    with _io_lock:
        tmp = SERVERS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, SERVERS_FILE)


def cfg_set(key, value):
    """写入单个配置项到 config.json (加锁+先写临时文件再替换, 崩溃也不会写坏配置)"""
    with _io_lock:
        cfg = load_config()
        cfg[key] = value
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_FILE)


def get_server(name):
    for s in load_servers()["servers"]:
        if s["name"] == name:
            return s
    return None


# ---------------- SSH 相关 ----------------
def ssh_run(host, script=None, args=None, timeout=120, remote_cmd=None, stdin_data=None):
    """两种模式:
    1) script 模式: 把脚本内容经 stdin 传给远程 bash -s 执行(args 为位置参数)
    2) remote_cmd 模式: 在远程执行一条命令, stdin_data 作为它的标准输入
    """
    if script is not None:
        script = script.replace("\r\n", "\n").encode("utf-8")  # 统一 LF; 用字节流避免 Windows 文本模式再转回 CRLF
        cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", host, "bash -s"] + (args or [])
        input_data = script
    else:
        cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", host, remote_cmd]
        input_data = (stdin_data or "").encode("utf-8")
    r = subprocess.run(cmd, input=input_data, capture_output=True, timeout=timeout)
    out = (r.stdout or b"").decode("utf-8", errors="replace")
    err = (r.stderr or b"").decode("utf-8", errors="replace")
    return r.returncode, out + (("\n[stderr] " + err) if err else "")


def build_script(action, cfg, name):
    """读取安装器模板并替换占位符。action: install / uninstall"""
    tpl = open(INSTALLER, encoding="utf-8").read()
    tpl = tpl.replace("__TOPIC__", cfg.get("topic") or "")
    tpl = tpl.replace("__HOSTTAG__", f"{cfg.get('host_tag', 'PC')}@{name}")
    return tpl


# ---------------- API 处理 ----------------
def api_import_ssh():
    """从 ~/.ssh/config 导入主机名（跳过通配符）"""
    names = []
    if os.path.exists(SSH_CONFIG):
        for line in open(SSH_CONFIG, encoding="utf-8", errors="replace"):
            m = re.match(r"\s*Host\s+(.+)\s*$", line, re.IGNORECASE)
            if m:
                for n in m.group(1).split():
                    if "*" not in n and n not in names:
                        names.append(n)
    data = load_servers()
    existing = {s["name"] for s in data["servers"]}
    added = []
    for n in names:
        if n not in existing:
            data["servers"].append({"name": n, "host": n, "status": "undeployed",
                                    "last_deploy": "", "detail": ""})
            added.append(n)
    save_servers(data)
    return {"ok": True, "found": names, "added": added}


def api_add(body):
    name = (body.get("name") or "").strip()
    host = (body.get("host") or "").strip()
    if not name or not host:
        return {"ok": False, "error": "name 和 host 不能为空"}
    data = load_servers()
    if any(s["name"] == name for s in data["servers"]):
        return {"ok": False, "error": "同名服务器已存在"}
    data["servers"].append({"name": name, "host": host, "status": "undeployed",
                            "last_deploy": "", "detail": ""})
    save_servers(data)
    return {"ok": True}


def api_remove(body):
    name = body.get("name")
    data = load_servers()
    data["servers"] = [s for s in data["servers"] if s["name"] != name]
    save_servers(data)
    return {"ok": True}


def api_deploy(body):
    name = body.get("name")
    s = get_server(name)
    if not s:
        return {"ok": False, "error": "服务器不存在"}
    cfg = load_config()
    script = build_script("install", cfg, name)
    try:
        code, output = ssh_run(s["host"], script, timeout=180)
    except subprocess.TimeoutExpired:
        code, output = 1, "连接超时"
    except Exception as e:
        code, output = 1, f"SSH 执行失败: {e}"
    data = load_servers()
    for sv in data["servers"]:
        if sv["name"] == name:
            sv["status"] = "deployed" if code == 0 else "failed"
            sv["last_deploy"] = time.strftime("%Y-%m-%d %H:%M")
            sv["detail"] = output.strip()[-500:]
    save_servers(data)
    return {"ok": code == 0, "output": output.strip()[-800:]}


def api_uninstall(body):
    name = body.get("name")
    s = get_server(name)
    if not s:
        return {"ok": False, "error": "服务器不存在"}
    cfg = load_config()
    script = build_script("uninstall", cfg, name)
    try:
        code, output = ssh_run(s["host"], script, timeout=120)
    except Exception as e:
        code, output = 1, f"SSH 执行失败: {e}"
    if code == 0:
        data = load_servers()
        for sv in data["servers"]:
            if sv["name"] == name:
                sv["status"] = "undeployed"
                sv["last_deploy"] = ""
                sv["detail"] = output.strip()[-300:]
        save_servers(data)
    return {"ok": code == 0, "output": output.strip()[-500:]}


def api_test(body):
    name = body.get("name")
    s = get_server(name)
    if not s:
        return {"ok": False, "error": "服务器不存在"}
    payload = json.dumps({"cwd": s["host"], "message": "面板测试通知（说明这台机器的钩子链路通畅）"})
    try:
        code, output = ssh_run(s["host"], timeout=60,
                               remote_cmd="bash $HOME/.codebuddy/notify_hook.sh Test",
                               stdin_data=payload)
    except Exception as e:
        code, output = 1, str(e)
    # Test 事件不在钩子映射里, 会走默认 Stop 分支(优先级3), 消息里带"测试"字样
    return {"ok": code == 0, "output": ("测试通知已从远程机发出, 请看手机/手表" if code == 0 else output[-300:])}


def normalize_repo(repo):
    """容忍各种填法: 带不带 https://、带不带 github.com/ 前缀, 统一成 用户名/仓库名"""
    r = (repo or "").strip()
    r = re.sub(r"^https?://", "", r, flags=re.IGNORECASE)
    r = re.sub(r"^www\.", "", r, flags=re.IGNORECASE)
    r = re.sub(r"^github\.com/", "", r, flags=re.IGNORECASE)
    return r.strip("/").strip()


def api_check_update():
    cfg = load_config()
    repo = normalize_repo(cfg.get("github_repo", ""))
    if not repo:
        return {"ok": True, "configured": False, "msg": "未配置 GitHub 仓库 (config.json -> github_repo)"}
    cur = APP_VERSION
    url = f"https://raw.githubusercontent.com/{repo}/main/version.json"
    try:
        latest = json.loads(urllib.request.urlopen(url, timeout=8).read().decode("utf-8")).get("version", "?")
    except Exception as e:
        return {"ok": True, "configured": True, "current": cur, "latest": None,
                "updateAvailable": False, "msg": f"检查失败(GitHub 连不上?): {e}"}
    # 真正的版本大小比较: 只有远端比当前"新"才提示, 防止远端是旧版本号时误报
    def vt(v):
        try:
            return tuple(int(x) for x in str(v).split("."))
        except Exception:
            return (0,)
    newer = latest != "?" and vt(latest) > vt(cur)
    return {"ok": True, "configured": True, "current": cur, "latest": latest,
            "updateAvailable": newer, "msg": ""}


def _self_update_from_release(repo):
    """独立 exe 的自更新: 从 GitHub Releases 下载最新 AI_notice.exe, 写替换脚本后退出"""
    api_url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(api_url, headers={"User-Agent": "AI_notice"})
    rel = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
    asset_url = None
    for a in rel.get("assets", []):
        if a.get("name") == "AI_notice.exe":
            asset_url = a.get("browser_download_url")
            break
    if not asset_url:
        return {"ok": False, "output": "最新 Release 里没有找到 AI_notice.exe 附件"}

    tmp_new = os.path.join(BASE, "AI_notice_update.exe")
    # 分块下载 + 超时保护(urlretrieve 无超时, GitHub 慢时会无限卡死)
    req = urllib.request.Request(asset_url, headers={"User-Agent": "AI_notice"})
    resp = urllib.request.urlopen(req, timeout=30)
    total = int(resp.headers.get("Content-Length", 0) or 0)
    done = 0
    with open(tmp_new, "wb") as f:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total and time.time() % 5 < 0.1:  # 低频心跳, 避免界面无反馈
                pass
    if total and done < total * 0.95:
        return {"ok": False, "output": f"下载不完整({done}/{total} 字节), 已取消更新"}
    if os.path.getsize(tmp_new) < 1024 * 1024:
        return {"ok": False, "output": "下载的文件不完整, 已取消更新"}

    bat = os.path.join(BASE, "update_ai_notice.bat")
    with open(bat, "w", encoding="gbk") as f:
        f.write("@echo off\r\n")
        f.write("timeout /t 2 /nobreak >nul\r\n")
        f.write('taskkill /IM AI_notice.exe /F >nul 2>&1\r\n')
        f.write('copy /Y "%s" "%s" >nul\r\n' % (tmp_new, os.path.join(BASE, "AI_notice.exe")))
        f.write('del "%s"\r\n' % tmp_new)
        f.write('start "" "%s"\r\n' % os.path.join(BASE, "AI_notice.exe"))
        f.write('del "%s"\r\n' % bat)
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([bat], creationflags=flags, close_fds=True)
    return {"ok": True, "output": f"已下载 {rel.get('tag_name','新版')}, 软件将自动替换并重启…", "restart": True}


def api_update():
    # 路径一: exe 在 git 仓库里 → git pull
    if os.path.isdir(os.path.join(BASE, ".git")):
        try:
            r = subprocess.run(["git", "-C", BASE, "pull"], capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=120)
            return {"ok": r.returncode == 0, "output": (r.stdout or "") + (r.stderr or "")}
        except Exception as e:
            return {"ok": False, "output": str(e)}
    # 路径二: 独立下载的 exe → GitHub Releases 自更新
    cfg = load_config()
    repo = normalize_repo(cfg.get("github_repo", ""))
    if not repo:
        return {"ok": False, "output": "未配置 GitHub 仓库, 无法自动更新"}
    return _self_update_from_release(repo)


def api_config(body):
    """更新本地配置(主题频道/仓库地址)"""
    with _io_lock:
        cfg = load_config()
        for k in ("topic", "host_tag", "github_repo"):
            if body.get(k) is not None and str(body[k]).strip():
                val = str(body[k]).strip()
                if k == "github_repo":
                    val = normalize_repo(val)
                cfg[k] = val
        json.dump(cfg, open(CONFIG_FILE, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    return {"ok": True, "config": cfg}


ROUTES = {
    "/api/servers": lambda b: load_servers(),
    "/api/import_ssh": lambda b: api_import_ssh(),
    "/api/add": api_add,
    "/api/remove": api_remove,
    "/api/deploy": api_deploy,
    "/api/uninstall": api_uninstall,
    "/api/test": api_test,
    "/api/check_update": lambda b: api_check_update(),
    "/api/update": lambda b: api_update(),
    "/api/config": api_config,
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/" or path == "/index.html":
            p = os.path.join(WEB, "index.html")
            if os.path.exists(p):
                self._send(200, open(p, "rb").read(), "text/html; charset=utf-8")
            else:
                self._send(404, {"error": "index.html 缺失"})
        elif path == "/api/servers":
            self._send(200, load_servers())
        elif path == "/api/config":
            self._send(200, load_config())
        elif path == "/api/check_update":
            self._send(200, api_check_update())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        fn = ROUTES.get(path)
        if not fn:
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = {}
        if length:
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                body = {}
        try:
            self._send(200, fn(body))
        except Exception as e:
            self._send(200, {"ok": False, "error": str(e)})

    def log_message(self, *a):
        pass  # 静默访问日志


if __name__ == "__main__":
    load_config()  # 首次生成默认配置
    print(f"面板已启动: http://localhost:{PORT}  (Ctrl+C 停止)")
    socketserver.TCPServer.allow_reuse_address = True
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
