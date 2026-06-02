@echo off
chcp 65001 >nul
set PYTHONUTF8=1
title CodeAgentBot
cd /d "f:\cardnews_bot\cardnews_bot"
echo Starting code agent bot...
python code_agent_bot.py
echo.
echo === Bot stopped. Check the messages above. Press any key to close. ===
pause >nul
