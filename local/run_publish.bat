@echo off
rem 一键发布：在项目根目录下运行 local/publish.py（自动读取 local/config.env）
cd /d "%~dp0.."
python local/publish.py
if errorlevel 1 (
  echo.
  echo [publish] 本轮未全部成功，请查看上方日志。
  echo 按任意键退出...
  pause >nul
)