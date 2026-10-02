# AI_notice

AI 任务通知 → 手机 → 手表。一站式管理：截获 Windows 系统通知转发 ntfy、给 SSH 服务器批量部署 CodeBuddy 远程通知钩子、开机自启、自动更新。

## 功能

- **通知监听**：后台截获 Windows 系统通知，按关键词过滤后转发到 [ntfy](https://ntfy.sh)，手机/手表实时震动
- **服务器批量部署**：添加 SSH 服务器（支持从 `~/.ssh/config` 一键导入），给远程 CodeBuddy Code 安装/卸载/测试通知钩子（任务完成/等待审批时推送）
- **开机自启**：设置页一键开关
- **托盘常驻**：关闭窗口不退出，托盘图标右键可暂停监听/退出
- **自动更新**：启动时自动检查 GitHub Release，一键下载替换重启

## 使用

1. 从 [Releases](../../releases) 下载 `AI_notice.exe`，双击运行（免 Python）
2. 首次使用：在「设置」页填入你的 ntfy 频道名（手机装 ntfy app 订阅同一频道即可收到推送）
3. 「服务器」页：从 `~/.ssh/config` 导入或手动添加服务器 → 点「部署」
4. 建议：设置页勾选「开机自启」

## 打包（开发者）

```bash
pip install customtkinter pystray pyinstaller
pyinstaller --onefile --windowed --name AI_notice --collect-all customtkinter --collect-all pystray AI_notice_app.py
```

## 说明

- 通知数据只经 ntfy.sh 中转，频道名即订阅凭据，请使用不易猜测的随机名称
- SSH 部署要求目标服务器已配置免密登录（密钥认证）
- 本地数据（服务器列表/配置）保存在 exe 同目录的 `panel_data` 下，不会上传
