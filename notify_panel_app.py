# -*- coding: utf-8 -*-
"""
CodeBuddy 通知管理面板 · 桌面版（Tkinter 窗口）
双击/运行后打开窗口, 对服务器列表一键部署/卸载/测试通知钩子。
复用 server_panel.py 里的核心逻辑。
"""
import json
import threading
import tkinter as tk
from tkinter import ttk, messagebox

import server_panel as core


class App:
    def __init__(self, root):
        self.root = root
        root.title("CodeBuddy 通知管理面板")
        root.geometry("620x520")
        root.minsize(560, 460)

        # ---- 顶部操作条 ----
        top = ttk.Frame(root, padding=(10, 8, 10, 4))
        top.pack(fill="x")
        ttk.Button(top, text="从 ~/.ssh/config 导入", command=self.import_ssh).pack(side="left")
        ttk.Button(top, text="手动添加…", command=self.add_dialog).pack(side="left", padx=6)
        ttk.Button(top, text="检查更新", command=self.check_update).pack(side="right")

        # ---- 服务器列表 ----
        mid = ttk.Frame(root, padding=(10, 4))
        mid.pack(fill="both", expand=True)
        cols = ("status", "last")
        self.tree = ttk.Treeview(mid, columns=cols, show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="服务器")
        self.tree.heading("status", text="状态")
        self.tree.heading("last", text="上次部署")
        self.tree.column("#0", width=220)
        self.tree.column("status", width=140, anchor="center")
        self.tree.column("last", width=160, anchor="center")
        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        # 状态文本 -> 图标
        self.badge = {"deployed": "✅ 已部署", "undeployed": "⬜ 未部署",
                      "failed": "❌ 失败", "working": "⏳ 处理中…"}

        # ---- 操作条 ----
        ops = ttk.Frame(root, padding=(10, 4))
        ops.pack(fill="x")
        ttk.Button(ops, text="部署", command=lambda: self.run_op("deploy")).pack(side="left")
        ttk.Button(ops, text="测试通知", command=lambda: self.run_op("test")).pack(side="left", padx=6)
        ttk.Button(ops, text="卸载钩子", command=lambda: self.run_op("uninstall")).pack(side="left")
        ttk.Button(ops, text="移除记录", command=self.remove_server).pack(side="left", padx=6)

        # ---- 配置区 ----
        cfgf = ttk.LabelFrame(root, text="配置", padding=(10, 6))
        cfgf.pack(fill="x", padx=10, pady=(2, 8))
        ttk.Label(cfgf, text="ntfy频道:").grid(row=0, column=0, sticky="w")
        self.e_topic = ttk.Entry(cfgf, width=28)
        self.e_topic.grid(row=0, column=1, padx=6, pady=2)
        ttk.Label(cfgf, text="GitHub仓库:").grid(row=1, column=0, sticky="w")
        self.e_repo = ttk.Entry(cfgf, width=28)
        self.e_repo.grid(row=1, column=1, padx=6, pady=2)
        ttk.Button(cfgf, text="保存配置", command=self.save_config).grid(row=0, column=2, rowspan=2, padx=6)

        # ---- 状态栏 ----
        self.status = tk.StringVar(value="就绪")
        ttk.Label(root, textvariable=self.status, relief="sunken", anchor="w",
                  padding=(10, 4)).pack(fill="x", side="bottom")

        self.load_config_fields()
        self.refresh()

    # ---------- 工具 ----------
    def set_status(self, s):
        self.status.set(s)

    def selected_name(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在列表中选择一台服务器")
            return None
        return self.tree.item(sel[0])["text"]

    def run_async(self, fn, on_done):
        """后台线程执行耗时操作, 完成后回主线程刷新"""
        def worker():
            try:
                result = fn()
            except Exception as e:
                result = {"ok": False, "error": str(e), "output": ""}
            self.root.after(0, lambda: on_done(result))
        threading.Thread(target=worker, daemon=True).start()

    # ---------- 数据 ----------
    def load_config_fields(self):
        cfg = core.load_config()
        self.e_topic.delete(0, "end"); self.e_topic.insert(0, cfg.get("topic", ""))
        self.e_repo.delete(0, "end"); self.e_repo.insert(0, cfg.get("github_repo", ""))

    def save_config(self):
        core.api_config({"topic": self.e_topic.get().strip(),
                         "github_repo": self.e_repo.get().strip()})
        self.set_status("配置已保存（频道名改动后需重新部署才生效）")

    def refresh(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        for s in core.load_servers()["servers"]:
            badge = self.badge.get(s.get("status", "undeployed"), s.get("status"))
            self.tree.insert("", "end", text=s["name"],
                             values=(badge, s.get("last_deploy", "")))

    # ---------- 操作 ----------
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
        win = tk.Toplevel(self.root)
        win.title("添加服务器")
        win.geometry("360x150")
        win.resizable(False, False)
        ttk.Label(win, text="名称:").grid(row=0, column=0, padx=10, pady=8, sticky="w")
        e1 = ttk.Entry(win, width=30); e1.grid(row=0, column=1, pady=8)
        ttk.Label(win, text="SSH地址:").grid(row=1, column=0, padx=10, sticky="w")
        e2 = ttk.Entry(win, width=30); e2.grid(row=1, column=1)
        def ok():
            n, h = e1.get().strip(), e2.get().strip()
            if not n or not h:
                messagebox.showinfo("提示", "名称和地址都要填", parent=win); return
            r = core.api_add({"name": n, "host": h})
            if r.get("ok"):
                win.destroy(); self.refresh()
            else:
                messagebox.showinfo("提示", r.get("error", "失败"), parent=win)
        ttk.Button(win, text="添加", command=ok).grid(row=2, column=1, pady=10, sticky="e")

    def run_op(self, action):
        name = self.selected_name()
        if not name:
            return
        # 列表里先标成处理中
        for i in self.tree.get_children():
            if self.tree.item(i)["text"] == name:
                self.tree.set(i, "status", self.badge["working"])
        self.set_status(f"{name}: {action} 处理中…")
        def worker():
            if action == "deploy":
                s = core.get_server(name)
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

    def remove_server(self):
        name = self.selected_name()
        if not name:
            return
        if not messagebox.askyesno("确认", f"移除 {name} 的部署记录？\n（服务器上的钩子不受影响, 可先点\"卸载钩子\"）"):
            return
        core.api_remove({"name": name})
        self.refresh()

    def check_update(self):
        self.set_status("正在检查更新…")
        def worker(): return core.api_check_update()
        def done(r):
            if not r.get("configured"):
                self.set_status("未配置 GitHub 仓库, 在下方配置框填入后保存")
                return
            if r.get("updateAvailable"):
                if messagebox.askyesno("有更新",
                        f"发现新版本 {r['latest']}（当前 {r['current']}）。\n现在更新吗？将执行 git pull 并提示重启。"):
                    def worker2(): return core.api_update()
                    def done2(r2):
                        self.set_status("更新完成, 请重启软件生效: " + (r2.get("output", "") or "")[-100:])
                    self.run_async(worker2, done2)
            else:
                self.set_status(f"已是最新版本 {r.get('current', '?')}" if r.get("latest")
                                else r.get("msg", "检查失败"))
        self.run_async(worker, done)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
