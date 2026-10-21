$ErrorActionPreference='Stop'
$projectRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Set-Location -LiteralPath $projectRoot
New-Item -ItemType Directory -Force -Path resources,licenses | Out-Null
Copy-Item -LiteralPath '源码/CombinedRuntime.built.ps1' -Destination 'resources/runtime.ps1'
Copy-Item -LiteralPath '快橙一键启动.cmd' -Destination 'resources/快橙一键启动.cmd'
Copy-Item -LiteralPath 'tools/Start-ClaudeWithVpn.ps1' -Destination 'resources/Start-ClaudeWithVpn.ps1'
Copy-Item -LiteralPath '补充直连域名.txt' -Destination 'resources/补充直连域名.txt'
Copy-Item -LiteralPath '分流工具/域名规则-LICENSE.txt' -Destination 'licenses/china-domains-GPL-3.0.txt'
Copy-Item -LiteralPath '分流工具/china-domains.metadata.json' -Destination 'licenses/china-domains.metadata.json'
Copy-Item -LiteralPath '分流工具/china-ipv4.metadata.json' -Destination 'licenses/china-ipv4.metadata.json'
& '.build/venv/Scripts/python.exe' 'packaging/make_brand.py'
if ($LASTEXITCODE -ne 0) { throw 'Brand rendering failed' }
& '.build/venv/Scripts/python.exe' -c "from PIL import Image; Image.open('.build/brand/icon.png').save('.build/brand/icon.ico',sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"
if ($LASTEXITCODE -ne 0) { throw 'Icon generation failed' }
& '.build/venv/Scripts/python.exe' -m pip freeze | Set-Content -LiteralPath 'packaging/requirements.lock.txt' -Encoding utf8
