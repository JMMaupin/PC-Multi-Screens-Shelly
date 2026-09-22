@echo off
rem Lancement de diagnostic : la console reste ouverte et affiche les journaux.
rem Pour l'usage courant, utiliser « Shelly Screens.pyw » (aucune console).
cd /d "%~dp0"
python main.py --verbose
pause
