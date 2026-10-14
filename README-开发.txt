快橙伴航 1.2.1 — 自编源码与重建

Windows x64 / Python 3.12 / PySide6 6.12.0 / PyInstaller 6.22.3 / Inno Setup 6.3+
本项目使用 Qt Widgets；UI、CLI 和控制后台共用业务状态。
源码入口：python -m app.main；命令行：python -m app.cli。
只有显式 start 才会开启保活和临时代理。

单文件重建
完成 Python 构建环境和依赖安装后，执行 ./packaging/build-single.ps1。
输出为 发布/快橙伴航.exe；同一个 EXE 可直接运行界面或接受 CLI 参数。
窗口恢复守护和后台分别拥有独立的临时运行目录，退出 UI 不会删掉后台依赖。

重建
1. 安装 Python 3.12 x64，创建 .build/venv，按 packaging/requirements.txt 安装依赖。
2. resources 已附带可运行网络脚本和官方公共部署文件；licenses 已附带组件说明。
3. 安装官方 Inno Setup，在 PowerShell 中执行：
   ./packaging/build.ps1 -Compiler '你的 Inno Setup/ISCC.exe'
4. 输出在 dist/OrangeDesk 和 发布 中。
   较新的 Inno Setup 同样支持本脚本；随附的简体中文翻译保留原维护者说明。

数据与来源
源码/ 中是自编 PowerShell 和 C#，分流工具/ 中是国内规则快照及来源许可。
resources/runtime.ps1 是当前源码构建后的独立运行文件。
resources/vendor 中是从原厂公开部署地址取得的应用文件，未修改或解包账号缓存。
离线文件不替代原厂登录、网络驱动与服务部署。首次使用需要联网。
Qt 动态库在安装目录 _internal 中；可重新链接、替换兼容版本并自行重建应用。
组件许可、Qt 对应源码地址及其第三方归属说明在 licenses 中。

验证
python -m unittest tests.test_desktop_engine tests.test_install_support tests.test_vendor_ui tests.test_whitelist tests.test_whitelist_cli tests.test_whitelist_ui tests.test_whitelist_integration
python -m unittest tests.test_vendor_host
Windows PowerShell 5.1 中执行 tests/Test-WhitelistBridge.ps1；只使用自建 loopback 服务，不改系统代理。
上述测试使用模拟会话、模拟厂商窗口或自建原生窗口，不改真实代理，也不操作真实快橙。
tests 内其他网络测试用于实际运行检查，请阅读其参数后按需使用。

项目不包含使用者的 AppData 缓存、账号、日志和诊断记录。
