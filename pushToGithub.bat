@echo off
cd /d "%~dp0"
git add .
git commit -m "update notes"
git push origin main
echo Done!
pause