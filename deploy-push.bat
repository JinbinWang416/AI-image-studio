@echo off
REM ============================================================
REM  一键提交并推送到 GitHub —— Render 会自动重新构建部署
REM  用法：
REM    双击本文件            -> 交互输入本次更新说明
REM    命令行 deploy-push.bat "修复去背bug"   -> 直接带提交说明
REM ============================================================
cd /d "E:\1_Software\6_AI工具\deepseek\2_开发\图片生成" || (
  echo 无法进入项目目录，请检查路径是否正确。
  pause
  exit /b 1
)

REM 没有 git 命令时提示
where git >nul 2>nul || (
  echo 未找到 git 命令。请先安装 Git for Windows 并确保加入 PATH，
  echo 或用 Git Bash / GitHub Desktop 自带的终端执行下面的命令。
  pause
  exit /b 1
)

if "%~1"=="" (
  set /p MSG=请输入本次更新说明(commit message):
) else (
  set "MSG=%~1"
)

git add -A
git diff --cached --quiet
if %errorlevel%==0 (
  echo 没有检测到改动，无需提交。
  goto :end
)

git commit -m "%MSG%"
git push origin main

:end
echo.
echo ============================================================
echo  推送完成。Render 会自动检测到新提交并重新构建部署，
echo  通常需要 5~15 分钟（重依赖）。可在 Render 控制台 Events/Logs 查看进度。
echo  免费层冷启动首次访问需等 30~60 秒，刷新即可。
echo ============================================================
pause
