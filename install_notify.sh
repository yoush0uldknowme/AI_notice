#!/bin/bash
# ============================================================
#  CodeBuddy 通知一键安装器（幂等版）
#  用法（在目标服务器上执行）:
#    bash install_notify.sh            # 安装/更新钩子
#    bash install_notify.sh uninstall  # 卸载钩子
#  依赖: python3（服务器上一般都有）
#  说明: __TOPIC__ 和 __HOSTTAG__ 由管理面板在部署时替换
# ============================================================
DIR="$HOME/.codebuddy"
ACTION="${1:-install}"
mkdir -p "$DIR"
SETTINGS="$DIR/settings.json"
MARK="notify_hook.sh"

# ---------- 卸载 ----------
if [ "$ACTION" = "uninstall" ]; then
  python3 - "$SETTINGS" "$MARK" << 'PYEOF'
import json, sys, os
p, mark = sys.argv[1], sys.argv[2]
if not os.path.exists(p):
    print("无配置文件，跳过"); sys.exit(0)
try:
    cfg = json.load(open(p, encoding="utf-8"))
except Exception:
    cfg = {}
hooks = cfg.get("hooks", {})
removed = []
for ev in list(hooks.keys()):
    lst = hooks[ev]
    new = []
    changed = False
    for m in lst:
        hs = [h for h in m.get("hooks", []) if mark not in str(h.get("command", ""))]
        if len(hs) != len(m.get("hooks", [])):
            changed = True
        if hs:
            m["hooks"] = hs
            new.append(m)
    if changed:
        removed.append(ev)
    if new:
        hooks[ev] = new
    else:
        del hooks[ev]
json.dump(cfg, open(p, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
print("已移除钩子的事件:", sorted(set(removed)) if removed else "无")
PYEOF
  rm -f "$HOOK_SH" "$SEND_PY" 2>/dev/null
  rm -f "$DIR/notify_hook.sh" "$DIR/notify_send.py"
  echo "✅ 卸载完成"
  exit 0
fi

# ---------- 安装/更新 ----------
cat > "$DIR/notify_send.py" << 'PYEOF'
# -*- coding: utf-8 -*-
import sys, json, urllib.request
event = sys.argv[1] if len(sys.argv) > 1 else "Stop"
raw = sys.stdin.read()
cwd = ""; msg = ""
try:
    j = json.loads(raw)
    cwd = (j.get("cwd") or "").replace("\\", "/").rstrip("/").split("/")[-1]
    msg = j.get("message") or ""
except Exception:
    pass
title = "__HOSTTAG__ | CodeBuddy"
if event in ("Notification", "PermissionRequest"):
    pri = 4; tag = "question"; title += "-审批"
    if not msg: msg = "需要你确认后才能继续"
else:
    pri = 3; tag = "alarm_clock"
    if not msg: msg = "任务回合已结束"
if cwd:
    parts = title.split(" | ", 1)
    title = cwd + (" | " + parts[1] if len(parts) > 1 else "")
body = json.dumps({"topic": "__TOPIC__", "title": title[:100], "message": msg[:150],
                   "priority": pri, "tags": [tag]}).encode("utf-8")
try:
    urllib.request.urlopen(urllib.request.Request(
        "https://ntfy.sh", data=body,
        headers={"Content-Type": "application/json"}, method="POST"), timeout=10).read()
except Exception:
    pass
PYEOF

cat > "$DIR/notify_hook.sh" << 'SHEOF'
#!/bin/bash
EVENT="${1:-Stop}"
cat | python3 "$HOME/.codebuddy/notify_send.py" "$EVENT"
exit 0
SHEOF
chmod +x "$DIR/notify_hook.sh"

python3 - "$SETTINGS" << 'PYEOF'
import json, sys, os
p = sys.argv[1]
cfg = {}
if os.path.exists(p):
    try:
        cfg = json.load(open(p, encoding="utf-8"))
    except Exception:
        cfg = {}
hooks = cfg.setdefault("hooks", {})
def wire(event):
    lst = hooks.setdefault(event, [])
    cmd = "bash $HOME/.codebuddy/notify_hook.sh " + event
    for m in lst:
        for h in m.get("hooks", []):
            if h.get("command") == cmd:
                return
    lst.append({"hooks": [{"type": "command", "command": cmd}]})
for ev in ("Notification", "PermissionRequest", "Stop"):
    wire(ev)
json.dump(cfg, open(p, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
print("钩子已写入", p)
PYEOF
echo "✅ 安装完成: __HOSTTAG__"
