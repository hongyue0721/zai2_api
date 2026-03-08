@echo off
CHCP 65001
title GLM 官方渠道代理服务
echo 正在启动 OpenAI 代理服务...
echo.

:: 1. 切换到脚本所在的目录 (使用 /d 确保跨盘符切换成功)
cd /d "C:\Users\琚然\Desktop\PPT\逆向\GLM官方渠道"

:: 2. 运行服务
:: 这里直接使用 python 命令，因为你的系统已经识别了 python 命令
:: 如果报错，再手动指定绝对路径
python openai.py

echo.
if %errorlevel% neq 0 (
    echo [错误] 程序异常退出，请检查上方报错信息。
) else (
    echo 服务已正常停止。
)
pause
