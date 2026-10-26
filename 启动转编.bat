@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM 优先用 py launcher，其次 python
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
  echo [错误] 未找到 Python，请先安装并加入 PATH
  pause
  exit /b 1
)

if not exist "%~dp0chart_convert.py" (
  echo [错误] 同目录缺少 chart_convert.py
  pause
  exit /b 1
)

REM 拖放文件到本 bat：直接打开 GUI 并填入路径
if "%~1"=="" (
  %PY% -X utf8 "%~dp0launcher.py"
) else (
  %PY% -X utf8 "%~dp0launcher.py" "%~1"
)

if errorlevel 1 pause
exit /b %ERRORLEVEL%
