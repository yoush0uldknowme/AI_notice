# -*- coding: utf-8 -*-
"""
AI_notice —— AI 任务通知一体化应用（单文件版）
= 后台监听器(截获Windows通知转手表) + 托盘图标 + 管理窗口

- 双击打开: 窗口出现, 同时后台监听已启动
- 关闭窗口: 缩到系统托盘(铃铛图标), 监听继续
- 托盘右键: 打开管理窗口 / 暂停监听 / 开机自启 / 退出
- [服务器] 页: SSH 服务器一键部署/卸载/测试 CodeBuddy 远程钩子
- [设置] 页: 开机自启 / ntfy频道 / 监听关键词 / GitHub仓库(更新检查)
"""
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
import urllib.request
import winreg

import customtkinter as ctk
from PIL import Image, ImageDraw
import pystray

import server_panel as core

APP_NAME = "AI_notice"

C_MUT = "#6B7280"
C_OK = "#16A34A"
C_BAD = "#DC2626"
C_WORK = "#D97706"

BADGE = {
    "deployed": ("● 已部署", C_OK),
    "undeployed": ("● 未部署", C_MUT),
    "failed": ("● 部署失败", C_BAD),
    "working": ("● 处理中…", C_WORK),
}

DEFAULT_FILTER = ["workbuddy", "claude", "cursor", "codebuddy", "copilot",
                  "chatgpt", "gpt", "ollama", "lm studio", "openai",
                  "kimi", "豆包", "deepseek", "tr",
                  "vscode", "visualstudiocode", "codex", "opencode"]

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "AINotice"
LEGACY_RUN_VALUE = "AINotifyWatch"


# ---------------- 开机自启 ----------------
def autostart_enabled():
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY)
        try:
            winreg.QueryValueEx(k, RUN_VALUE)
            return True
        finally:
            winreg.CloseKey(k)
    except OSError:
        return False


def set_autostart(enable):
    exe = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
    if enable:
        winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, '"%s"' % exe)
    else:
        try:
            winreg.DeleteValue(k, RUN_VALUE)
        except FileNotFoundError:
            pass
    winreg.CloseKey(k)


def cleanup_legacy_autostart():
    """清理旧版遗留的自启动项(改名/迁移前产生的)"""
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        try:
            winreg.DeleteValue(k, LEGACY_RUN_VALUE)
        except FileNotFoundError:
            pass
        winreg.CloseKey(k)
    except OSError:
        pass


# ---------------- 监听器 ----------------
class Watcher:
    """后台监听 Windows 通知数据库 -> ntfy"""

    def __init__(self, topic, keywords, log_fn):
        self.topic = topic
        self.keywords = [k.lower() for k in keywords]
        self.log_fn = log_fn
        self.paused = threading.Event()
        self.db_dir = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Notifications")
        self.tmp = os.path.join(core.DATA, "_watch_tmp")
        os.makedirs(self.tmp, exist_ok=True)
        # 全局复制锁: 监听线程/扫描线程共享同一临时目录, 任何时刻只允许一个复制
        self._copy_lock = threading.Lock()

    def _copy_db(self):
        with self._copy_lock:
            for f in ("wpndatabase.db", "wpndatabase.db-wal", "wpndatabase.db-shm"):
                try:
                    shutil.copy2(os.path.join(self.db_dir, f), os.path.join(self.tmp, f))
                except Exception as e:
                    self.log_fn(f"复制通知数据库失败({f}): {e}")
            return os.path.exists(os.path.join(self.tmp, "wpndatabase.db"))

    def _read_new(self, last_order):
        import sqlite3
        con = sqlite3.connect(os.path.join(self.tmp, "wpndatabase.db"))
        rows = con.execute(
            'SELECT n.[Order], h.PrimaryId, n.Payload FROM [Notification] n '
            'JOIN [NotificationHandler] h ON n.HandlerId = h.RecordId '
            "WHERE n.Type = 'toast' AND n.[Order] > ? ORDER BY n.[Order]", (last_order,)).fetchall()
        con.close()
        result, mo = [], last_order
        for (o, app, payload) in rows:
            mo = max(mo, o)
            try:
                texts = re.findall(r"<text[^>]*>([^<]+)</text>",
                                   payload.decode("utf-8", errors="replace"))
            except Exception:
                texts = []
            result.append((app, texts[0] if texts else "", " ".join(texts[1:]) if len(texts) > 1 else ""))
        return result, mo

    def _send(self, app, title, body):
        if not self.topic:
            self.log_fn("未配置 ntfy 频道, 跳过发送 (请在设置页填写)")
            return False
        payload = json.dumps({"topic": self.topic,
                              "title": f"{socket.gethostname()} | {app}",
                              "message": (body or title or "").strip()[:200],
                              "priority": 3, "tags": ["bell"]}).encode("utf-8")
        req = urllib.request.Request("https://ntfy.sh", data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=15).read()
        return True

    def list_known_apps(self):
        """列出所有曾发过通知的应用。
        直接查询监听线程每 5 秒维护的最新副本(加锁防撞车), 不再自行复制。
        副本不存在时(刚启动未满一个周期)才触发一次加锁复制。"""
        import sqlite3
        db = os.path.join(self.tmp, "wpndatabase.db")
        if not os.path.exists(db) and not self._copy_db():
            raise RuntimeError("无法读取系统通知数据库")
        con = sqlite3.connect(db)
        try:
            rows = con.execute(
                'SELECT DISTINCT PrimaryId FROM NotificationHandler ORDER BY PrimaryId').fetchall()
        finally:
            con.close()
        return [r[0] for r in rows if r[0]]

    def loop(self):
        self.log_fn("后台监听线程已启动")
        last_order = None
        while True:
            try:
                if self._copy_db():
                    import sqlite3
                    con = sqlite3.connect(os.path.join(self.tmp, "wpndatabase.db"))
                    cur_max = con.execute(
                        'SELECT COALESCE(MAX([Order]),0) FROM [Notification]').fetchone()[0]
                    if last_order is None:
                        last_order = cur_max  # 首次启动以当前位置为基线, 不补发历史
                        self.log_fn("后台监听已启动")
                    rows = []
                    if cur_max > (last_order or 0):
                        rows = con.execute(
                            'SELECT n.[Order], h.PrimaryId, n.Payload FROM [Notification] n '
                            'JOIN [NotificationHandler] h ON n.HandlerId = h.RecordId '
                            "WHERE n.Type = 'toast' AND n.[Order] > ? ORDER BY n.[Order]",
                            (last_order,)).fetchall()
                    con.close()
                    max_seen = max([r[0] for r in rows], default=last_order)
                    for (o, app, payload) in rows:
                        try:
                            texts = re.findall(r"<text[^>]*>([^<]+)</text>",
                                               payload.decode("utf-8", errors="replace"))
                        except Exception:
                            texts = []
                        title = texts[0] if texts else ""
                        body = " ".join(texts[1:]) if len(texts) > 1 else ""
                        a = app.lower()
                        if "*" in self.keywords or any(k in a for k in self.keywords):
                            try:
                                if self._send(app, title, body):
                                    self.log_fn(f"已转发: {app} | {title}")
                            except Exception as e:
                                self.log_fn(f"转发失败({app}): {e}")
                    last_order = max(last_order or 0, max_seen)
            except Exception as e:
                self.log_fn(f"监听出错(继续): {e}")
            time.sleep(5)


# ---------------- 托盘图标 ----------------
def make_icon_image(paused):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(22, 119, 255, 255))
    d.polygon([(32, 14), (18, 30), (18, 44), (46, 44), (46, 30)], fill="white")
    d.ellipse((28, 10, 36, 18), fill="white")
    d.arc((22, 42, 42, 56), start=0, end=180, fill="white", width=4)
    if paused:
        d.rectangle((14, 14, 50, 50), fill=(0, 0, 0, 140))
        d.text((24, 24), "II", fill="white")
    return img


class Tray:
    def __init__(self, app):
        self.app = app
        self.icon = pystray.Icon(APP_NAME, make_icon_image(False),
                                 f"{APP_NAME} (运行中)", self.build_menu())

    def build_menu(self):
        return pystray.Menu(
            pystray.MenuItem("打开管理窗口", self.on_show, default=True),
            pystray.MenuItem(lambda item: "恢复监听" if self.app.watcher.paused.is_set()
                             else "暂停监听", self.on_pause),
            pystray.MenuItem("退出", self.on_exit),
        )

    def on_show(self, icon, item):
        self.app.after(0, lambda: (self.app.deiconify(), self.app.focus_force()))

    def on_pause(self, icon, item):
        w = self.app.watcher
        if w.paused.is_set():
            w.paused.clear()
        else:
            w.paused.set()
        self.refresh_icon()

    def on_exit(self, icon, item):
        # 先隐藏图标给出"正在退出"的视觉反馈, 再秒退
        try:
            icon.visible = False
        except Exception:
            pass
        try:
            self.app.watcher.paused.set()
        except Exception:
            pass
        os._exit(0)

    def refresh_icon(self):
        self.icon.icon = make_icon_image(self.app.watcher.paused.is_set())
        st = "已暂停" if self.app.watcher.paused.is_set() else "运行中"
        self.icon.title = f"{APP_NAME} ({st})"

    def run(self):
        # pystray 的 run_detached 不会自动显示图标, 通过 setup 回调在循环就绪时点亮
        self.icon.run_detached(setup=self._tray_ready)

    def _tray_ready(self, icon):
        icon.visible = True


# ---------------- 服务器卡片 ----------------
class ServerCard(ctk.CTkFrame):
    def __init__(self, master, info, app):
        super().__init__(master, fg_color="#FFFFFF", corner_radius=10,
                         border_width=1, border_color="#E5E7EB")
        self.info = info
        self.app = app
        self.pack(fill="x", padx=2, pady=5)

        left = ctk.CTkFrame(self, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True, padx=14, pady=10)

        name_row = ctk.CTkFrame(left, fg_color="transparent")
        name_row.pack(anchor="w")
        ctk.CTkLabel(name_row, text=info["name"], font=("Microsoft YaHei UI", 13, "bold"),
                     text_color="#111827").pack(side="left")
        txt, color = BADGE.get(info.get("status", "undeployed"),
                               ("● " + info.get("status", ""), C_MUT))
        ctk.CTkLabel(name_row, text=txt, font=("Microsoft YaHei UI", 11),
                     text_color=color).pack(side="left", padx=10)

        last = f"　上次部署: {info['last_deploy']}" if info.get("last_deploy") else ""
        ctk.CTkLabel(left, text=f"主机: {info['host']}{last}",
                     font=("Microsoft YaHei UI", 11), text_color=C_MUT,
                     anchor="w").pack(anchor="w")

        right = ctk.CTkFrame(self, fg_color="transparent")
        right.pack(side="right", padx=12)
        ctk.CTkButton(right, text="部署", width=72,
                      command=lambda: app.run_op("deploy", info["name"])).pack(pady=2)
        ctk.CTkButton(right, text="测试", width=72, fg_color="#F3F4F6", text_color="#111827",
                      hover_color="#E5E7EB",
                      command=lambda: app.run_op("test", info["name"])).pack(pady=2)
        ctk.CTkButton(right, text="卸载", width=72, fg_color="#F3F4F6", text_color="#111827",
                      hover_color="#E5E7EB",
                      command=lambda: app.run_op("uninstall", info["name"])).pack(pady=2)
        ctk.CTkButton(right, text="移除记录", width=72, fg_color="#F3F4F6", text_color="#DC2626",
                      hover_color="#FDE8E8",
                      command=lambda: app.remove_server(info["name"])).pack(pady=2)


# ---------------- GUI ----------------
class App(ctk.CTk):
    def __init__(self, watcher):
        super().__init__()
        self.watcher = watcher
        self.title(APP_NAME)
        self.geometry("760x640")
        self.minsize(660, 540)
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(16, 6))
        ctk.CTkLabel(head, text=APP_NAME, font=("Microsoft YaHei UI", 22, "bold")).pack(side="left")
        ctk.CTkLabel(head, text="AI 任务通知 → 手表", font=("Microsoft YaHei UI", 12),
                     text_color=C_MUT).pack(side="left", padx=10, pady=(8, 0))
        ctk.CTkButton(head, text="检查更新", width=100, command=self.check_update).pack(side="right")

        tabs = ctk.CTkTabview(self, corner_radius=12)
        tabs.pack(fill="both", expand=True, padx=16, pady=(4, 8))
        tabs.add("服务器")
        tabs.add("设置")
        self.tab_srv = tabs.tab("服务器")
        self.tab_set = tabs.tab("设置")

        bar = ctk.CTkFrame(self.tab_srv, fg_color="transparent")
        bar.pack(fill="x", pady=(2, 8))
        ctk.CTkButton(bar, text="从 ~/.ssh/config 导入", command=self.import_ssh).pack(side="left")
        ctk.CTkButton(bar, text="手动添加…", fg_color="#F3F4F6", text_color="#111827",
                      hover_color="#E5E7EB", command=self.add_dialog).pack(side="left", padx=8)
        ctk.CTkButton(bar, text="刷新", width=70, fg_color="#F3F4F6", text_color="#111827",
                      hover_color="#E5E7EB", command=self.refresh).pack(side="left")

        self.cards_frame = ctk.CTkScrollableFrame(self.tab_srv, fg_color="transparent")
        self.cards_frame.pack(fill="both", expand=True)

        card = ctk.CTkFrame(self.tab_set, fg_color="#FFFFFF", corner_radius=12,
                            border_width=1, border_color="#E5E7EB")
        card.pack(fill="x", padx=2, pady=(2, 10))

        ctk.CTkLabel(card, text="监听", font=("Microsoft YaHei UI", 13, "bold"),
                     text_color="#111827").pack(anchor="w", padx=18, pady=(14, 2))
        self.sw_pause = ctk.CTkSwitch(card, text="暂停监听", command=self.toggle_pause,
                                      progress_color="#D97706")
        self.sw_pause.pack(anchor="w", padx=18, pady=4)
        row0 = ctk.CTkFrame(card, fg_color="transparent")
        row0.pack(fill="x", padx=18, pady=4)
        ctk.CTkLabel(row0, text="监听关键词", width=90, anchor="w",
                     text_color=C_MUT).pack(side="left")
        self.e_filter = ctk.CTkEntry(row0, width=320)
        self.e_filter.pack(side="left", padx=6)
        ctk.CTkButton(row0, text="保存关键词", width=90, command=self.save_filter).pack(side="left", padx=6)
        ctk.CTkButton(row0, text="勾选应用…", width=90, command=self.pick_apps_dialog).pack(side="left")
        ctk.CTkLabel(card, text="应用名含关键词才转发；填 * 转发全部应用。也可点「勾选应用」从电脑通知记录里直接挑。保存后立即生效。",
                     text_color=C_MUT).pack(anchor="w", padx=18)

        ctk.CTkLabel(card, text="常规", font=("Microsoft YaHei UI", 13, "bold"),
                     text_color="#111827").pack(anchor="w", padx=18, pady=(14, 2))
        self.sw_autostart = ctk.CTkSwitch(card, text="开机自启（登录 Windows 后自动运行）",
                                          command=self.toggle_autostart, progress_color="#1677FF")
        self.sw_autostart.pack(anchor="w", padx=18, pady=6)
        if autostart_enabled():
            self.sw_autostart.select()

        row1 = ctk.CTkFrame(card, fg_color="transparent")
        row1.pack(fill="x", padx=18, pady=4)
        ctk.CTkLabel(row1, text="ntfy 频道", width=90, anchor="w", text_color=C_MUT).pack(side="left")
        self.e_topic = ctk.CTkEntry(row1, width=260)
        self.e_topic.pack(side="left", padx=6)
        ctk.CTkLabel(row1, text="保存后立即生效", text_color=C_MUT).pack(side="left", padx=6)

        ctk.CTkButton(card, text="保存配置", command=self.save_config).pack(anchor="w", padx=18,
                                                                           pady=(8, 16))

        self.status = ctk.StringVar(value="就绪")
        ctk.CTkLabel(self, textvariable=self.status, anchor="w", height=26,
                     text_color=C_MUT).pack(fill="x", side="bottom", padx=20, pady=(0, 6))

        self.load_config_fields()
        self.refresh()
        # 每次启动自动检查更新(延迟2秒等界面就绪; 无新版则静默, 不弹任何窗)
        self.after(2000, self.check_update)

    # ---------- 窗口/托盘 ----------
    def hide_to_tray(self):
        self.withdraw()
        self.set_status("已最小化到托盘, 监听继续运行 (托盘图标右键可退出)")
        # 系统级气泡提示, 明确告诉用户"没有退出, 只是看不见了"
        try:
            self.tray.icon.notify("已最小化到托盘, 监听继续运行。退出请右键托盘图标。", APP_NAME)
        except Exception:
            pass

    def show_window(self):
        self.deiconify()
        self.focus_force()

    def toggle_pause(self):
        if self.sw_pause.get():
            self.watcher.paused.set()
            self.tray.refresh_icon()
            self.set_status("监听已暂停")
        else:
            self.watcher.paused.clear()
            self.tray.refresh_icon()
            self.set_status("监听已恢复")

    def on_tray_exit(self):
        try:
            self.tray.icon.stop()
        except Exception:
            pass
        os._exit(0)

    # ---------- 工具 ----------
    def set_status(self, s):
        self.status.set(s)

    def run_async(self, fn, on_done):
        def worker():
            try:
                result = fn()
            except Exception as e:
                result = {"ok": False, "error": str(e), "output": ""}
            self.after(0, lambda: on_done(result))
        threading.Thread(target=worker, daemon=True).start()

    # ---------- 数据 ----------
    def load_config_fields(self):
        cfg = core.load_config()
        self.e_topic.delete(0, "end"); self.e_topic.insert(0, cfg.get("topic", ""))
        flt = cfg.get("filter") or DEFAULT_FILTER
        self.e_filter.delete(0, "end"); self.e_filter.insert(0, ",".join(flt))
        self.watcher.keywords = [k.strip().lower() for k in flt if k.strip()]

    def save_config(self):
        cfg = core.api_config({"topic": self.e_topic.get().strip()})
        if cfg.get("config"):
            # 频道名热更新: 保存后监听器立即改用新频道, 无需重启
            self.watcher.topic = cfg["config"].get("topic", self.watcher.topic)
        self.save_filter(silent=True)
        self.set_status("配置已保存, 监听频道已立即生效（服务器钩子需重新部署才会跟随改动）")

    def save_filter(self, silent=False):
        try:
            raw = self.e_filter.get().strip()
            flt = [k.strip() for k in raw.replace("，", ",").split(",") if k.strip()]
            if not flt:
                flt = ["*"]
            core.cfg_set("filter", flt)
            self.watcher.keywords = [k.lower() for k in flt]
            if not silent:
                self.set_status(f"监听关键词已更新: {flt}")
        except Exception as e:
            # 配置写失败绝不拖垮界面: 提示并保留原值
            self.set_status(f"保存关键词失败: {e}")

    def pick_apps_dialog(self):
        """从电脑通知记录里勾选要监听的应用"""
        from tkinter import messagebox
        self.set_status("正在扫描电脑通知应用列表…")
        def worker():
            try:
                return {"ok": True, "apps": self.watcher.list_known_apps()}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        def done(r):
            if not r.get("ok"):
                self.set_status(f"扫描失败: {r.get('error', '')}（需要电脑上有过通知记录）")
                return
            self._build_picker(r["apps"])
        self.run_async(worker, done)

    def _build_picker(self, apps):
        # 用原生 Listbox 渲染 (426 个 CTk 复选框会卡死界面线程)
        import tkinter as tk

        def pretty(appid):
            """把系统长 ID 显示成人类可读名: xxx_yyy!Snipaste -> Snipaste"""
            tail = appid.split("!")[-1] if "!" in appid else appid
            tail = re.sub(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}_", "", tail)
            tail = re.sub(r"^[^_]+_", "", tail) if "_" in tail else tail
            tail = re.sub(r"_\w+$", "", tail) if tail.count("_") else tail
            return tail or appid

        # 按显示名去重(保留首个 appid), 排序
        seen, items = set(), []
        for appid in sorted(apps):
            name = pretty(appid)
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            items.append((name, appid))
        items.sort(key=lambda x: x[0].lower())

        current = set()
        raw = self.e_filter.get().strip()
        if raw and "*" not in raw:
            current = set(k.strip().lower() for k in raw.replace("，", ",").split(",") if k.strip())

        win = ctk.CTkToplevel(self)
        win.title("勾选要监听的应用")
        win.geometry("560x640")
        win.grab_set()
        top = ctk.CTkFrame(win, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(10, 2))
        ctk.CTkLabel(top, text=f"共发现 {len(items)} 个应用（已合并重名），勾选后点确定：",
                     text_color="#111827", anchor="w").pack(side="left")
        search_var = ctk.StringVar()
        ctk.CTkEntry(top, width=160, placeholder_text="搜索…",
                     textvariable=search_var).pack(side="right")

        listbox = tk.Listbox(win, selectmode="multiple", exportselection=False,
                             font=("Microsoft YaHei UI", 10), activestyle="none",
                             bg="#FFFFFF", fg="#111827", highlightthickness=1,
                             highlightbackground="#D9DDE3", relief="flat")
        sb = tk.Scrollbar(win, command=listbox.yview)
        listbox.configure(yscrollcommand=sb.set)
        listbox.pack(fill="both", expand=True, padx=(14, 2), pady=4)
        sb.pack(side="right", fill="y", padx=(0, 14))

        # 勾选状态记录在 selected_names 集合(显示名 -> appid 的映射在 items 里)
        selected_names = set()

        def toggle(event):
            sel_idx = listbox.nearest(event.y)
            if not (0 <= sel_idx < listbox.size()):
                return
            line = listbox.get(sel_idx)
            name = line[2:] if line.startswith(("☐ ", "☑ ")) else line
            if name in selected_names:
                selected_names.discard(name)
            else:
                selected_names.add(name)
            repaint()

        def repaint():
            kw = search_var.get().strip().lower()
            listbox.delete(0, "end")
            for name, appid in items:
                if kw and kw not in name.lower() and kw not in appid.lower():
                    continue
                mark = "☑" if name in selected_names else "☐"
                listbox.insert("end", f"{mark} {name}")

        search_var.trace_add("write", lambda *_: repaint())
        listbox.bind("<Button-1>", toggle)
        # 预选当前已监听的
        for name, appid in items:
            if appid.lower() in current:
                selected_names.add(name)
        repaint()

        def apply():
            # 把勾选的显示名映射回 appid
            sel_ids = [appid for name, appid in items if name in selected_names]
            if not sel_ids:
                from tkinter import messagebox
                messagebox.showinfo("提示", "一个都没勾 = 不监听任何应用。", parent=win)
                return
            self.e_filter.delete(0, "end")
            self.e_filter.insert(0, ",".join(sel_ids))
            self.save_filter()
            win.destroy()
        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkButton(btns, text="全选", width=70, fg_color="#F3F4F6", text_color="#111827",
                      hover_color="#E5E7EB", command=lambda: (selected_names.update(n for n, _ in items), repaint())).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="清空", width=70, fg_color="#F3F4F6", text_color="#111827",
                      hover_color="#E5E7EB", command=lambda: (selected_names.clear(), repaint())).pack(side="left")
        ctk.CTkButton(btns, text="确定", width=90, command=apply).pack(side="right", padx=4)
        self.set_status(f"应用列表已就绪, 共 {len(items)} 个 (搜索框可过滤)")

    def refresh(self):
        """重建服务器卡片列表"""
        for w in self.cards_frame.winfo_children():
            w.destroy()
        servers = core.load_servers()["servers"]
        if not servers:
            ctk.CTkLabel(self.cards_frame,
                         text="还没有服务器。\n点上方「从 ~/.ssh/config 导入」或「手动添加…」开始。",
                         text_color=C_MUT, font=("Microsoft YaHei UI", 13),
                         justify="center").pack(pady=40)
            return
        for s in servers:
            ServerCard(self.cards_frame, s, self)

    # ---------- 服务器操作 ----------
    def import_ssh(self):
        self.set_status("正在读取 ~/.ssh/config …")
        def worker(): return core.api_import_ssh()
        def done(r):
            added = r.get("added", [])
            self.set_status(f"发现 {len(r['found'])} 个主机, 新导入 {len(added)} 个" +
                            ((": " + ", ".join(added)) if added else ""))
            self.refresh()
        self.run_async(worker, done)

    def add_dialog(self):
        from tkinter import messagebox
        win = ctk.CTkToplevel(self)
        win.title("添加服务器")
        win.geometry("420x200")
        win.resizable(False, False)
        win.grab_set()
        ctk.CTkLabel(win, text="名称:", text_color="#111827").grid(row=0, column=0,
                                                                   padx=16, pady=(20, 8), sticky="w")
        e1 = ctk.CTkEntry(win, width=250); e1.grid(row=0, column=1, pady=(20, 8))
        ctk.CTkLabel(win, text="SSH地址:", text_color="#111827").grid(row=1, column=0,
                                                                      padx=16, sticky="w")
        e2 = ctk.CTkEntry(win, width=250); e2.grid(row=1, column=1)
        def ok():
            n, h = e1.get().strip(), e2.get().strip()
            if not n or not h:
                messagebox.showinfo("提示", "名称和地址都要填", parent=win); return
            r = core.api_add({"name": n, "host": h})
            if r.get("ok"):
                win.destroy(); self.refresh()
            else:
                messagebox.showinfo("提示", r.get("error", "失败"), parent=win)
        ctk.CTkButton(win, text="添加", command=ok).grid(row=2, column=1, pady=16, sticky="e")

    def run_op(self, action, name):
        from tkinter import messagebox
        self.set_status(f"{name}: {action} 处理中…")
        def worker():
            if action == "deploy":
                return core.api_deploy({"name": name})
            return {"test": core.api_test, "uninstall": core.api_uninstall}[action]({"name": name})
        def done(r):
            ok = r.get("ok")
            self.set_status(f"{name} {action}: " + ("成功" if ok else "失败"))
            if action == "test" and ok:
                messagebox.showinfo("测试通知", r.get("output", "已发出, 请看手机/手表"))
            if not ok and r.get("output"):
                messagebox.showerror("失败详情", r["output"][-800:])
            self.refresh()
        self.run_async(worker, done)

    def remove_server(self, name):
        from tkinter import messagebox
        if not messagebox.askyesno("确认", f"移除 {name} 的部署记录？\n（服务器上的钩子不受影响, 可先点\"卸载\"）"):
            return
        core.api_remove({"name": name})
        self.refresh()
        self.set_status(f"已移除 {name} 的记录")

    # ---------- 自启 ----------
    def toggle_autostart(self):
        enable = bool(self.sw_autostart.get())
        try:
            set_autostart(enable)
            self.set_status(f"开机自启已{'开启' if enable else '关闭'}")
        except Exception as e:
            self.set_status(f"修改开机自启失败: {e}")
            if enable:
                self.sw_autostart.deselect()
            else:
                self.sw_autostart.select()

    # ---------- 更新 ----------
    def check_update(self):
        from tkinter import messagebox
        self.set_status("正在检查更新…")
        def worker(): return core.api_check_update()
        def done(r):
            if not r.get("configured"):
                self.set_status("未配置 GitHub 仓库, 在设置页填入后保存")
                return
            if r.get("updateAvailable"):
                if messagebox.askyesno("有更新",
                        f"发现新版本 {r['latest']}（当前 {r['current']}）。\n现在更新吗？（自动下载新版并替换, 完成后自动重启）"):
                    def worker2(): return core.api_update()
                    def done2(r2):
                        self.set_status("更新完成, 请重启 AI_notice 生效")
                        messagebox.showinfo("更新", "更新完成, 请重启 AI_notice 生效。")
                    self.run_async(worker2, done2)
            else:
                self.set_status(f"已是最新版本 {r.get('current', '?')}" if r.get("latest")
                                else r.get("msg", "检查失败（GitHub 连接不了?）"))
        self.run_async(worker, done)


# ---------------- 入口 ----------------
ACTIVATION_PORT = 8531  # 本机回环端口: 第二实例通知第一实例弹出窗口


def _activate_running():
    """通知已运行的实例弹出窗口。返回 True=已有实例接管(本实例应退出)"""
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", ACTIVATION_PORT), timeout=1.5)
        s.sendall(b"AI_NOTICE_SHOW")
        data = s.recv(64)
        s.close()
        return data == b"OK"
    except Exception:
        return False


def _start_activation_listener(app, log):
    """主实例监听激活请求: 第二实例启动时唤起本窗口"""
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", ACTIVATION_PORT))
    srv.listen(4)
    def loop():
        while True:
            try:
                conn, _ = srv.accept()
                data = conn.recv(64)
                if data == b"AI_NOTICE_SHOW":
                    conn.sendall(b"OK")
                    app.after(0, app.show_window)
                conn.close()
            except Exception as e:
                log(f"激活监听出错(继续): {e}")
                time.sleep(1)
    threading.Thread(target=loop, daemon=True).start()


def main():
    # 单实例(唤起式): 已有实例 -> 让它弹出窗口, 本实例退出。互杀设计已废弃。
    if _activate_running():
        return

    cleanup_legacy_autostart()

    cfg = core.load_config()
    topic = cfg.get("topic") or ""
    flt = cfg.get("filter") or DEFAULT_FILTER

    app_log = os.path.join(core.DATA, "ai_notice.log")
    def log(msg):
        try:
            with open(app_log, "a", encoding="utf-8") as f:
                f.write(time.strftime("[%m-%d %H:%M:%S] ") + msg + "\n")
        except Exception:
            pass

    # 全局异常钩子: 任何未处理异常都写进日志, 界面保持可用, 不再"黑箱卡死"
    def _unhandled(t, v, tb):
        log(f"未处理异常 {t.__name__}: {v}")
    sys.excepthook = _unhandled

    watcher = Watcher(topic, flt, log)
    threading.Thread(target=watcher.loop, daemon=True).start()

    app = App(watcher)
    # tkinter 回调异常同样落日志(默认行为会静默或弹 PyInstaller 错误框)
    app.report_callback_exception = lambda et, ev, tb: log(f"UI回调异常 {et.__name__}: {ev}")

    _start_activation_listener(app, log)

    tray = Tray(app)
    threading.Thread(target=tray.run, daemon=True).start()

    app.mainloop()


if __name__ == "__main__":
    main()
