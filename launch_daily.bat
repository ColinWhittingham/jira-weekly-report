@echo off
cd /d "%~dp0"
python generate_report.py --period daily >> logs\daily.log 2>&1
