' Helm autostart: at logon, run scripts/autostart.bat hidden (damselfish + SearXNG).
' ASCII-only content; all paths are derived at runtime from WScript.ScriptFullName
' (a Unicode string), so no non-ASCII literals -> immune to .vbs codepage issues, and
' the HKCU Run value is written via WScript.Shell.RegWrite (Unicode, no admin, no reg.exe).
'
'   wscript autostart.vbs            -> logon action (run autostart.bat hidden)
'   wscript autostart.vbs install    -> register the HKCU Run key (run once)
Option Explicit
Dim fso, sh, self, here, autostartBat, isInstall
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
self = WScript.ScriptFullName
here = fso.GetParentFolderName(self)                  ' .../Helm/scripts
autostartBat = here & "\autostart.bat"

isInstall = False
If WScript.Arguments.Count > 0 Then
  If WScript.Arguments(0) = "install" Then isInstall = True
End If

If isInstall Then
  ' Write HKCU Run key directly (Unicode; no admin, no reg.exe, no codepage issue).
  sh.RegWrite "HKCU\Software\Microsoft\Windows\CurrentVersion\Run\Helm-AutoStart", _
              "wscript.exe " & self, "REG_SZ"
  WScript.Echo "registered Helm-AutoStart -> wscript.exe " & self
  WScript.Quit 0
End If

' Logon action: run autostart.bat hidden (0 = no window, False = no wait).
If Not fso.FileExists(autostartBat) Then WScript.Quit 1
sh.Run "cmd /c """ & autostartBat & """", 0, False
