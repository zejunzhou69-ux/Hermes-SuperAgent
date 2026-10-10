@echo off
setlocal enabledelayedexpansion
rem ============================================================
rem  Hermes-SuperAgent 便携版 · 一键部署（Windows）
rem  双击运行：检查 Python、装依赖、装 Chromium、建密钥模板、跑自检
rem ============================================================
echo.
echo  ============================================================
echo   Hermes-SuperAgent 便携版 · 一键部署
echo  ============================================================
echo.

rem ---------- 1) Python 检查 ----------
where python >nul 2>nul
if errorlevel 1 (
  echo  [错误] 未检测到 Python，请先安装 Python 3.10+
  echo  下载地址：https://www.python.org/downloads/
  pause
  exit /b 1
)
for /f "tokens=2 delims=: " %%v in ('python -c "import sys; print(sys.version.split()[0])"') do set PYVER=%%v
echo  [1/6] Python: !PYVER!

rem ---------- 2) 进入 core 目录装依赖 ----------
cd /d "%~dp0core" || goto :err
echo  [2/6] 安装 Python 依赖...
python -m pip install --upgrade pip >nul 2>&1
python -m pip install httpx aiohttp Pillow pyyaml 2>nul || python -m pip install httpx aiohttp Pillow pyyaml
if errorlevel 1 goto :err

rem ---------- 3) Playwright 内置 Chromium ----------
echo  [3/6] 安装内置 Chromium（浏览器渲染引擎）...
python -m pip install playwright >nul 2>&1
python -m playwright install chromium 2>nul || python -m playwright install chromium

rem ---------- 4) pywebview（原生桌面窗口，可选） ----------
echo  [4/6] 安装 pywebview（原生窗口外壳，失败不影响主流程）...
python -m pip install pywebview >nul 2>&1 || echo   （pywebview 安装失败，将自动回落到浏览器标签页）

rem ---------- 5) 密钥模板 ----------
echo  [5/6] 创建密钥模板 ~/.hermes-superagent/secrets.json ...
set HOME_DIR=%USERPROFILE%
if not exist "%HOME_DIR%\.hermes-superagent" mkdir "%HOME_DIR%\.hermes-superagent"
if not exist "%HOME_DIR%\.hermes-superagent\secrets.json" (
  echo { "agnes.api_key": "YOUR_AGNES_API_KEY", "agnes.base_url": "https://apihub.agnes-ai.cn/v1" } > "%HOME_DIR%\.hermes-superagent\secrets.json"
  echo   已创建模板，请填入你的 agnes API key 再调真实模型
)

rem ---------- 6) 跑测试确认 ----------
echo  [6/6] 跑快速自检测试...
python -m pytest tests -q 2>&1 | findstr /C:"passed" /C:"failed"

echo.
echo  ============================================================
echo   部署完成！
echo  ============================================================
echo.
echo   启动双端工作台（左侧栏+对话+大屏内嵌浏览器）：
echo       双击 启动工作台.bat
echo   或命令行：
echo       cd core && python -m hermes_superagent panel
echo.
pause
exit /b 0

:err
echo.
echo  [错误] 部署失败，请检查上方日志
pause
exit /b 1
