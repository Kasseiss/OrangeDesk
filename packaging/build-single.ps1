$ErrorActionPreference='Stop'
$projectRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Set-Location -LiteralPath $projectRoot
if (-not (Test-Path -LiteralPath '.build/venv/Scripts/python.exe')) {
    throw 'Create .build/venv with Python 3.12 x64 and install packaging/requirements.txt first.'
}
& './源码/Build-Bundle.ps1'
& './packaging/prepare.ps1'
& '.build/venv/Scripts/python.exe' -m PyInstaller --noconfirm --distpath dist/single --workpath .build/pyinstaller-single packaging/OrangeDesk-single.spec
if ($LASTEXITCODE -ne 0) { throw 'Single EXE build failed.' }
Copy-Item -LiteralPath 'dist/single/OrangeDesk.exe' -Destination '发布/快橙伴航.exe'
