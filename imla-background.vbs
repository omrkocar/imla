' Start imla with no visible window. Output goes to imla.log.
' Use this for the Startup folder; run imla.cmd instead when debugging.
Option Explicit

Dim shell, fso, here, py, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

here = fso.GetParentFolderName(WScript.ScriptFullName)

' Resolve Python explicitly - "python" on a stale PATH hits the Store stub.
' python.exe rather than pythonw.exe: pythonw has no usable stdout, so the
' log redirect below would be unreliable. The window is hidden by Run's 0.
py = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & _
     "\Programs\Python\Python313\python.exe"
If Not fso.FileExists(py) Then py = "python"

shell.CurrentDirectory = here
cmd = "cmd /c """"" & py & """ """ & here & "\imla.py"" >> """ & _
      here & "\imla.log"" 2>&1"""
shell.Run cmd, 0, False
