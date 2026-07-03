@echo off
cd /d "%~dp0"
start "Avito Daemon" /min python avito_daemon.py
