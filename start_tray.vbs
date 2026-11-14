' ChatGPT2API (Web Edition) - Silent Tray Launcher
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = scriptDir

pywPath = scriptDir & "\runtime\python\pythonw.exe"
If Not fso.FileExists(pywPath) Then
    pywPath = scriptDir & "\.venv\Scripts\pythonw.exe"
    If Not fso.FileExists(pywPath) Then
        pywPath = "pythonw.exe"
    End If
End If

trayPath = scriptDir & "\tray.py"
shell.Run """" & pywPath & """ -X utf8 """ & trayPath & """", 0, False
