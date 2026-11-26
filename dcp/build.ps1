param()
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

python .\prepare_demo.py
if ($LASTEXITCODE -ne 0) { throw 'No se pudieron preparar los FMU para la demo.' }
$buildDir = Join-Path $root 'build\windows'

# Use the installed Visual Studio 2026 Build Tools explicitly.
python -m cmake --fresh -S . -B $buildDir -G 'Visual Studio 18 2026' -A x64
if ($LASTEXITCODE -ne 0) {
    throw 'CMake could not configure Visual Studio 2026. Check that Build Tools 2026 includes Desktop development with C++.'
}

python -m cmake --build $buildDir --config Release --parallel
if ($LASTEXITCODE -ne 0) { throw 'La compilación de la demo ha fallado; no se ha generado el ejecutable.' }

if (!(Test-Path (Join-Path $buildDir 'Release\dcp_master.exe')) -or !(Test-Path (Join-Path $buildDir 'Release\dcp_slave.exe'))) {
    throw 'La compilación terminó sin generar los ejecutables esperados.'
}
Write-Host 'Compilación completada: build\windows\Release\dcp_master.exe y build\windows\Release\dcp_slave.exe'
