' Starts the tunnel with no console window. Used by the autostart task.
CreateObject("Wscript.Shell").Run """" & CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName) & "\radar-tunnel.bat""", 0, False
