' yt-bridge watchdog — uruchamia launcher.py i co 2 minuty sprawdza czy dziala.
' launcher.py ma blokade jednej instancji (_acquire_lock) wiec jesli juz dziala,
' nowa proba po prostu wychodzi. Jesli padl — startuje od nowa.
Dim WshShell
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\owoca\yt-bridge"

Const PYTHON = "C:\Users\owoca\AppData\Local\Programs\Python\Python313\pythonw.exe"
Const SCRIPT = "C:\Users\owoca\yt-bridge\launcher.py"
Const INTERVAL_MS = 120000

Do While True
    WshShell.Run """" & PYTHON & """ """ & SCRIPT & """", 0, False
    WScript.Sleep INTERVAL_MS
Loop
