@echo off
REM ============================================================
REM  一键构建 Windows 桌面 exe（需 Python 3.12）
REM  产物：desktop\dist\门店贴纸图片生成智能体\门店贴纸图片生成智能体.exe
REM ============================================================
cd /d "E:\1_Software\6_AI工具\deepseek\2_开发\图片生成"

where python >nul 2>nul || (
  echo 未找到 python，请先安装 Python 3.12 并加入 PATH。
  pause
  exit /b 1
)

python --version

REM 首次构建建立独立虚拟环境（与项目运行时隔离）
if not exist build_venv312 (
  python -m venv build_venv312
)
call build_venv312\Scripts\activate

python -m pip install --upgrade pip
pip install -r requirements.txt
pip install pyinstaller

python build_desktop.py

echo.
echo ============================================================
echo  构建完成。exe 位于：
echo  desktop\dist\门店贴纸图片生成智能体\门店贴纸图片生成智能体.exe
echo  双击即可运行（会自动开浏览器 + 弹出本地服务窗口）。
echo ============================================================
pause
