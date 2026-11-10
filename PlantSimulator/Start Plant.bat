@echo off
rem Double-click to start the bottling plant simulator.
rem Modbus ports 5020 (tank) and 5021 (line), picture at http://localhost:8090
cd /d "%~dp0"
python plant_server.py %*
if errorlevel 1 pause
