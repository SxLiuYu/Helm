@echo off
rem 开机/登录时自启 damselfish (经 Git Bash 跑 start.sh)
rem 部署时复制到: C:\Users\<user>\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\
rem 并把下面的 Helm 路径改成你的实际路径
"D:\APP\Git\bin\bash.exe" -l -c "/d/AI项目/Helm/scripts/start.sh" > "C:\Users\mi\orca\Helm\data\autostart.log" 2>&1
