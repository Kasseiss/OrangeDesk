# OrangeDesk · 快橙伴航

Windows 上的 Qt 伴航工具，为快橙提供自动重连、国内网站直连、海外访问、网址白名单和本机网络诊断。界面与命令行共用配置。

**[下载单文件 EXE](https://github.com/Kasseiss/OrangeDesk/releases/latest/download/OrangeDesk.exe)** · **[查看 Release](https://github.com/Kasseiss/OrangeDesk/releases/latest)**

## 1.3.3

新版使用深浅双主题、侧栏导航和状态卡片，重新整理主页、运行记录、设置、关于及白名单界面。保留原有白名单、主题设置、后台运行与命令行入口。

本版同时包含 1.3 系列的网络监测、诊断 ZIP 导出、可选 Claude 出口保护和微信直连。微信直连默认勾选，但首次必须安装组件并确认“已生效”；已保存的关闭选择会保留。

已有安装版可下载 Release 中的 `OrangeDesk-1.3.3-update.zip`，解压后运行“更新并打开伴航.cmd”。更新器会正常关闭伴航、备份旧程序并重新打开新版；分流会短暂停止，快橙节点由原客户端管理。

## 使用

下载 `OrangeDesk.exe`，双击打开，点击「启动伴航」。白名单、保活、分流、Qt 界面及快橙官方安装组件都在这个文件中，无需另外打开脚本或安装 Python。

首次运行会在临时目录释放依赖；白名单和外观设置保存在 %USERPROFILE%\.OrangeDesk。新电脑需要通过随附的官方安装程序完成快橙及网络驱动安装，并自行登录。

要求 Windows 10 22H2 / Windows 11 x64。OrangeDesk 是伴航工具，快橙的账号、线路选择和服务仍由原厂客户端处理。

## 快速添加白名单

点击右上角「白名单」，或者在伴航窗口按 **Ctrl+L**。可直接粘贴完整网址，也可一次添加多个地址。

| 操作 | 效果 |
| --- | --- |
| Enter | 固定直连 |
| Ctrl+Enter | 固定走 VPN |
| Shift+Enter | 换行，批量添加 |
| Ctrl+Shift+D | 将剪贴板网址直接加入直连白名单 |
| Ctrl+Shift+P | 将剪贴板网址直接加入 VPN 白名单 |
| 最近连接旁的「直连」「VPN」 | 一键添加该域名 |
| 运行记录中右键域名 | 固定访问路线 |

只保存域名或 IPv4，不保存网址中的账号、密码、路径和查询参数。默认包含子域名，也可选择仅匹配当前域名。

白名单优先于自动分流判断；更具体的规则优先。运行中的新连接约 1 秒内采用新规则，已经建立的下载或长连接保持原路线。刷新页面或重新连接后即可采用新规则。

## 保活与分流

快橙已登录并具备连接条件时，伴航可在断线后重新连接。正常关闭自己启动的伴航会话会恢复启动前的系统代理设置，快橙本体保留运行；选择「后台运行」可继续保活。

分流通过临时系统代理处理 HTTP/HTTPS 请求，国内域名和国内 IPv4 通常走物理网卡，海外访问走快橙。白名单可明确指定路线。HTTPS 内容不解密，也不安装根证书。

适用于遵循 Windows 系统代理的软件；忽略系统代理的 SSH、游戏 UDP 或软件自己的独立代理不在覆盖范围内。

## 微信直连与 Claude 出口保护

微信直连是独立的可选应用分流功能。首次点击“安装 / 修复”需要管理员授权及下载组件；驱动安装可能短暂中断网络。核验生效后，识别到的微信程序的常用 IPv4 TCP/UDP 连接经物理网络发出。当前不支持 IPv6，不能作为覆盖所有 IP 报文的隔离保证。关闭伴航窗口不会自动停止这些独立组件，删除程序前应关闭本功能。详见[微信直连说明](发布/微信直连使用说明.txt)。

Claude 出口保护仅限制识别到的原生 `claude.exe` 路径的新建外部连接使用指定 VPN 网卡。本机代理、Node 脚本版及未识别的程序路径不在自动覆盖范围内。它不会启动 Claude、修改 PATH 或项目目录，也不能保证账号风控结果或核验节点国家。保护规则独立持续生效，删除程序前应关闭。详见[出口保护说明](发布/Claude出口保护使用说明.txt)。

## 网络诊断

启动伴航时自动开始本机普通监测；发生故障后尽量等待约两分钟，在“运行记录”导出诊断 ZIP。可选的深度记录需要管理员授权。记录用于关联网卡、VPN 和进程状态，不能仅凭连接超时断定 Wi-Fi 或路由器故障。

诊断文件保存在本机，可能包含网络名称、IP、设备标识、目标域名与本机路径；不自动上传。分享前请检查内容。详见[网络监测说明](发布/网络监测使用说明.txt)。

## 命令行

同一个 EXE 也能接受命令行参数：

```powershell
.\OrangeDesk.exe ui
.\OrangeDesk.exe start
.\OrangeDesk.exe status
.\OrangeDesk.exe whitelist add example.com --mode direct
.\OrangeDesk.exe whitelist add example.com --mode vpn --exact
.\OrangeDesk.exe whitelist list
.\OrangeDesk.exe stop
```

安装版保留 `orange` 命令，并加入当前用户 PATH。安装后重新打开终端即可使用。不要同时启动来自不同文件夹的多个伴航入口。

详细使用说明见 [安装与使用](发布/安装与使用.txt)。

## 从源码构建

使用 Python 3.12 x64，在项目根目录创建构建环境：

```powershell
py -3.12 -m venv .build/venv
.build/venv/Scripts/python.exe -m pip install -r packaging/requirements.txt
./packaging/build-single.ps1
```

单文件结果在 `发布/快橙伴航.exe`，中间结果在 `dist/single/OrangeDesk.exe`。构建安装版需另外安装 Inno Setup，再运行 `packaging/build.ps1`。

源码界面入口是 `python -m app.main`，命令行入口是 `python -m app.cli`。开发环境和目录说明见 [README-开发.txt](README-开发.txt)。

## 验证

1.3.3 的新版界面已检查主页、运行记录、设置、关于、白名单、深浅主题、920×600 最小窗口与 150% 缩放。单文件已验证 Qt 运行环境、界面启动、命令行版本、保护与诊断组件打包，以及安装后设置保留。具体测试结果见 Release 说明。

窗口生命周期测试使用自建窗口；真实快橙的登录、线路菜单和使用效果由使用者检查。仓库不包含使用者的登录缓存、个人配置、网络日志或诊断记录。

## 第三方组件

组件来源和许可保存在 [licenses](licenses/THIRD-PARTY-NOTICES.txt)。Qt/PySide6、Python、PyInstaller、国内规则数据和快橙原厂组件各自遵循对应条款。快橙原厂组件保留原始签名和权利归属。
