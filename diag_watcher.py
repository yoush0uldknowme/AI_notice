# -*- coding: utf-8 -*-
"""最小化诊断: Watcher 线程单独拉起, 观察日志写入"""
import sys, os, threading, time
sys.path.insert(0, r'E:\project\notice\panel')
import server_panel as core
import AI_notice_app as app_mod

print('core.DATA =', core.DATA)
app_log = os.path.join(core.DATA, 'ai_notice.log')
print('日志路径 =', app_log, '| 存在:', os.path.exists(app_log))

# 测试直接写日志
try:
    with open(app_log, 'a', encoding='utf-8') as f:
        f.write('[诊断] 直接写入测试 ' + time.strftime('%H:%M:%S') + '\n')
    print('直接写日志: 成功')
except Exception as e:
    print('直接写日志: 失败 ->', e)

cfg = core.load_config()
topic = cfg.get('topic') or ''
flt = cfg.get('filter') or app_mod.DEFAULT_FILTER
print('topic =', repr(topic), '| filter 数量 =', len(flt))

def log(msg):
    try:
        with open(app_log, 'a', encoding='utf-8') as f:
            f.write(time.strftime('[%H:%M:%S] ') + msg + '\n')
    except Exception as e:
        print('log_fn 写入失败:', e)

w = app_mod.Watcher(topic, flt, log)
t = threading.Thread(target=w.loop, daemon=True)
t.start()
time.sleep(8)
print('8秒后线程存活:', t.is_alive())
print('=== 日志尾部 ===')
if os.path.exists(app_log):
    print('\n'.join(open(app_log, encoding='utf-8').read().splitlines()[-4:]))
