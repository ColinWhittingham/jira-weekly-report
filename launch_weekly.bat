@echo off
cd /d "%~dp0"
python generate_report.py --period weekly >> logs\weekly.log 2>&1
