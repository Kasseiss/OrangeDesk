param([string]$Compiler)
$ErrorActionPreference='Stop'
$projectRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Set-Location -LiteralPath $projectRoot
if (-not (Test-Path -LiteralPath '.build/venv/Scripts/python.exe')) {
    python -m venv .build/venv
    if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.12 x64 and run again.' }
}
& '.build/venv/Scripts/python.exe' -m pip install -r packaging/requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& './packaging/prepare.ps1'
& '.build/venv/Scripts/python.exe' -m PyInstaller --noconfirm --distpath dist --workpath .build/pyinstaller packaging/OrangeDesk.spec
if ($LASTEXITCODE -ne 0) { throw 'Application build failed.' }
if (-not $Compiler) {
    $candidates=@('.build/inno-registry/package/bin/ISCC.exe',"${env:ProgramFiles(x86)}/Inno Setup 6/ISCC.exe", "$env:ProgramFiles/Inno Setup 6/ISCC.exe")
    $Compiler=$candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $Compiler) { throw 'Install Inno Setup, or pass -Compiler path-to-ISCC.exe.' }
& $Compiler packaging/OrangeDesk.iss
if ($LASTEXITCODE -ne 0) { throw 'Installer build failed.' }
