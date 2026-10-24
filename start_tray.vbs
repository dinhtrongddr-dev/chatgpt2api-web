' ChatGPT2API (Web Edition) - Silent Tray Launcher
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "D:\chatgpt2api-web"
shell.Run """D:\chatgpt2api\backend\python\pythonw.exe"" -X utf8 ""D:\chatgpt2api-web\tray.py""", 0, False
