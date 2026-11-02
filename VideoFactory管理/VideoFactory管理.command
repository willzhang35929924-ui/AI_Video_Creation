#!/bin/bash
set -u
export PATH="/opt/local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PROJECT=$(cd "$SCRIPT_DIR/.." && pwd)
if [ -x /usr/local/bin/python3 ]; then PYTHON=/usr/local/bin/python3; else PYTHON=$(command -v python3 || true); fi
[ -n "$PYTHON" ] || { echo '请安装包含 Tk 的 Python 3。'; exit 1; }
bounded() {
  local limit="$1"; shift
  "$PYTHON" -c 'import subprocess,sys
try: sys.exit(subprocess.run(sys.argv[2:], timeout=int(sys.argv[1])).returncode)
except subprocess.TimeoutExpired: print("操作超时；结果可能不完整，请查看状态并重试。", file=sys.stderr); sys.exit(124)' "$limit" "$@"
}
cd "$PROJECT" || { osascript -e 'display alert "找不到 VideoFactory 项目目录" as critical'; exit 1; }
choice=$(osascript <<'APPLESCRIPT'
set options to {"开启项目", "关闭项目", "查看状态", "云端生成视频", "清空项目与任务", "取消"}
set picked to choose from list options with title "VideoFactory 管理" with prompt "选择操作：" default items {"查看状态"} OK button name "执行" cancel button name "退出"
if picked is false then return "取消"
return item 1 of picked
APPLESCRIPT
) || exit 0
case "$choice" in
  开启项目)
    if ! bounded 30 colima status >/dev/null 2>&1; then
      bounded 300 colima start --runtime docker --vm-type vz --mount-type virtiofs -c 2 --memory 2 --disk 30 --downloader curl || { echo 'Colima 启动失败'; exit 1; }
    fi
    bounded 300 docker --context colima compose up -d --pull never || { echo '项目启动失败，请查看上面的错误'; exit 1; }
    open 'http://127.0.0.1:8080'
    echo '项目启动完成。'
    ;;
  关闭项目)
    bounded 60 docker --context colima compose stop -t 10 || { echo '关闭失败，请查看上面的错误'; exit 1; }
    echo '项目容器已停止；Colima 保持运行。'
    ;;
  云端生成视频)
    SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
    exec "$PYTHON" -B "$SCRIPT_DIR/cloud_manager.py"
    ;;
  清空项目与任务)
    answer=$(osascript <<'APPLESCRIPT'
set answer to display dialog "将清除本机 VideoFactory Compose 项目的所有项目、任务、素材、生成媒体、日志、数据库备份、云端任务历史及管理程序保存的云端视频；保留全局设置、服务配置和加密密钥。不会清理云端、其他项目、外部导出、系统快照或备份。SSD 不保证物理不可恢复。请键入 清空 继续。" default answer "" buttons {"取消", "继续"} default button "取消" with icon caution
return text returned of answer
APPLESCRIPT
) || exit 0
    [ "$answer" = '清空' ] || { echo '确认文字不匹配，未清理。'; exit 1; }
    osascript -e 'display dialog "最后确认：本操作不可撤销。确认清理？" buttons {"取消", "确认清理"} default button "取消" with icon stop' >/dev/null || exit 0
    SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
    [ -f "$SCRIPT_DIR/cleanup.py" ] || { echo '缺少 cleanup.py，未清理。'; exit 1; }
    bounded 30 colima status >/dev/null 2>&1 || { echo 'Colima 未就绪，未清理；请先启动。'; exit 1; }
    bounded 60 docker --context colima compose stop -t 10 || { echo '无法停止项目，未清理。'; exit 1; }
    bounded 300 docker --context colima compose run --rm --no-deps --pull never -T --entrypoint python backend - < "$SCRIPT_DIR/cleanup.py" || { echo '清理未完全成功，请查看错误；项目保持关闭。'; exit 1; }
    if ! "$PYTHON" - "$SCRIPT_DIR" <<'PYCODE'
from pathlib import Path
import shutil, sys
base=Path(sys.argv[1]).resolve()
p=base/'云端视频'
if p.is_symlink(): raise RuntimeError('云端视频目录为符号链接，已拒绝清理')
if p.is_dir():
    for item in p.iterdir():
        if item.is_symlink() or not item.is_dir(): item.unlink()
        else: shutil.rmtree(item)
    if any(p.iterdir()): raise RuntimeError('云端视频目录仍有文件')
profiles=base/'云端配置'
if profiles.is_symlink(): raise RuntimeError('云端配置目录为符号链接，拒绝清理历史')
if profiles.is_dir():
    import re
    for d in profiles.iterdir():
        if d.is_symlink() or not d.is_dir() or not re.fullmatch(r'[0-9a-f]{32}',d.name):
            raise RuntimeError('云端配置存在未识别项目，拒绝清理历史：'+str(d))
        history=d/'history.json'
        if history.is_symlink(): raise RuntimeError('任务历史为符号链接：'+str(history))
        if history.exists(): history.unlink()
        if history.exists(): raise RuntimeError('任务历史未清除：'+str(history))
print('云端任务历史和管理程序保存的视频已清理；云端方案和密钥已保留。')
PYCODE
    then echo '项目数据已清理，但云端视频目录清理失败，请检查。'; exit 1; fi
    echo '清理完成；项目保持关闭，可手动重新启动。'
    ;;
  查看状态)
    colima status
    docker --context colima compose ps -a
    ;;
  *) exit 0 ;;
esac
printf '\n按回车关闭窗口。'
read -r _
