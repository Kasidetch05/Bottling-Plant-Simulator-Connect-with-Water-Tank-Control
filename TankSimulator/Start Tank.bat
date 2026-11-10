@echo off
rem Double-click to open the virtual water tank (Modbus TCP port 5020).
python "%~dp0tank_sim.py" %*
if errorlevel 1 pause
