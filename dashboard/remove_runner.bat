@echo off
schtasks /delete /tn "AgentRunner" /f
echo Runner removed. Tasks will no longer fire on schedule.
pause
