@echo off
setlocal
chcp 65001 >nul
rem ============================================================
rem  Hermes-SuperAgent 便携版 · 一键唤醒（无需 DSH/宿主）
rem  双击本文件：自动补装依赖 → 起服务 → 弹原生桌面窗口（大屏画面）
rem  停止：关闭弹出窗口，或在本黑窗按 Ctrl+C
rem ============================================================
cd /d "%~dp0core"

echo.
echo   Hermes-SuperAgent 双端工作台（便携版）启动中...
echo.

rem 若依赖缺失，尝试一次性补装（已有则秒过）
python -c "import aiohttp, yaml, httpx, PIL, playwright" 2>nul
if errorlevel 1 (
  echo   正在安装核心依赖（首次需要）...
  python -m pip install aiohttp pyyaml httpx Pillow playwright 2>nul
)

python -m hermes_superagent panel --port 8766
pause
