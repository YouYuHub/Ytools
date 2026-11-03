Set sh = CreateObject("WScript.Shell")
code = sh.Run("cmd /c ""C:\Users\Administrator\Desktop\python学习录\main_study\large_model\agent_tool_sse\rc_test_fdiwe1iv\fg_20261102_092339_0019_5712.cmd""", 0, True)
Set fso = CreateObject("Scripting.FileSystemObject")
Set f = fso.CreateTextFile("C:\Users\Administrator\Desktop\python学习录\main_study\large_model\agent_tool_sse\rc_test_fdiwe1iv\fg_20261102_092339_0019_5712.done.txt", True)
f.Write code
f.Close
