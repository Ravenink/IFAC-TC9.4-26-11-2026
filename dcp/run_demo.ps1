param(
    [Alias('Steps')][ValidateRange(1, 120)][int]$Duration = 120,
    [double]$Reference = 0.43,
    [string]$OutputFolder = 'outputs',
    [Alias('Delays')][int[]]$DelaySeconds = @(0, 2, 5, 10)
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
function Get-ParticipantLog([string[]]$Paths) {
    $parts = @()
    foreach ($path in $Paths) {
        if (Test-Path -LiteralPath $path) {
            $content = Get-Content -LiteralPath $path -Raw
            if ($null -ne $content) { $parts += $content.Trim() }
        }
    }
    return $parts -join [Environment]::NewLine
}

$DelaySeconds = @($DelaySeconds | Sort-Object -Unique)
if ($DelaySeconds.Count -eq 0) { throw 'Select at least one DCP delay with -DelaySeconds.' }
foreach ($delay in $DelaySeconds) {
    if ($delay -lt 0 -or $delay -gt 30) {
        throw 'Each DCP one-way signal delay must be between 0 and 30 seconds.'
    }
}

$master = Join-Path $root 'build\windows\Release\dcp_master.exe'
$slave = Join-Path $root 'build\windows\Release\dcp_slave.exe'
$sourcePaths = @(
    (Join-Path $root 'CMakeLists.txt'),
    (Join-Path $root 'src\dcp_master.cpp'),
    (Join-Path $root 'src\dcp_slave.cpp'),
    (Join-Path $root 'src\fmi2_runtime.hpp')
)
$latestSourceWrite = ($sourcePaths | ForEach-Object { (Get-Item -LiteralPath $_).LastWriteTimeUtc } |
    Measure-Object -Maximum).Maximum
$needsBuild = !(Test-Path $master) -or !(Test-Path $slave)
if (!$needsBuild) {
    $needsBuild = (Get-Item -LiteralPath $master).LastWriteTimeUtc -lt $latestSourceWrite -or
                  (Get-Item -LiteralPath $slave).LastWriteTimeUtc -lt $latestSourceWrite
}
if ($needsBuild) {
    & (Join-Path $root 'build.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'The DCP executables could not be built.' }
}

python .\prepare_demo.py
if ($LASTEXITCODE -ne 0) { throw 'The FMU files could not be prepared.' }

$output = if ([System.IO.Path]::IsPathRooted($OutputFolder)) { $OutputFolder } else { Join-Path $root $OutputFolder }
New-Item -ItemType Directory -Force -Path $output | Out-Null
$output = (Resolve-Path -LiteralPath $output).Path
$controllerFmu = Join-Path $root 'build\fmus\PIController'
$tankFmu = Join-Path $root 'build\fmus\Tank'
$referenceText = $Reference.ToString('R', [Globalization.CultureInfo]::InvariantCulture)
$runRecords = @()

foreach ($delay in $DelaySeconds) {
    $runFolder = Join-Path $output "dcp_delay_${delay}s"
    New-Item -ItemType Directory -Force -Path $runFolder | Out-Null
    $runFolder = (Resolve-Path -LiteralPath $runFolder).Path
    $controllerCsv = Join-Path $runFolder 'controller.csv'
    $tankCsv = Join-Path $runFolder 'tank.csv'
    $controllerArgs = 'controller {0} "{1}" "{2}"' -f $delay, $controllerFmu, $controllerCsv
    $tankArgs = 'tank {0} "{1}" "{2}"' -f $delay, $tankFmu, $tankCsv
    $controllerLog = Join-Path $runFolder 'controller.log'
    $tankLog = Join-Path $runFolder 'tank.log'
    $controllerErrorLog = Join-Path $runFolder 'controller-error.log'
    $tankErrorLog = Join-Path $runFolder 'tank-error.log'
    $masterLog = Join-Path $runFolder 'master.log'
    $masterErrorLog = Join-Path $runFolder 'master-error.log'
    $controllerProcess = $null
    $tankProcess = $null
    $masterProcess = $null

    try {
        $controllerProcess = Start-Process -FilePath $slave -ArgumentList $controllerArgs -PassThru `
            -WindowStyle Hidden -RedirectStandardOutput $controllerLog -RedirectStandardError $controllerErrorLog
        Start-Sleep -Milliseconds 700
        $controllerProcess.Refresh()
        if ($controllerProcess.HasExited) {
            $detail = Get-ParticipantLog @($controllerLog, $controllerErrorLog)
            throw "DCP controller exited before the master started: $detail"
        }
        $tankProcess = Start-Process -FilePath $slave -ArgumentList $tankArgs -PassThru `
            -WindowStyle Hidden -RedirectStandardOutput $tankLog -RedirectStandardError $tankErrorLog
        Start-Sleep -Milliseconds 700
        $tankProcess.Refresh()
        if ($tankProcess.HasExited) {
            $detail = Get-ParticipantLog @($tankLog, $tankErrorLog)
            throw "DCP tank exited before the master started: $detail"
        }
        $masterArgs = '--duration {0} --delay-seconds {1} --reference {2}' -f $Duration, $delay, $referenceText
        $masterProcess = Start-Process -FilePath $master -ArgumentList $masterArgs -PassThru `
            -WindowStyle Hidden -RedirectStandardOutput $masterLog -RedirectStandardError $masterErrorLog

        $runRecords += [ordered]@{
            delaySeconds = [int]$delay
            oneWayDelaySeconds = [int]$delay
            roundTripSignalDelaySeconds = [int](2 * $delay)
            communicationStepSeconds = 1
            exchanges = [int]$Duration
            masterPid = $masterProcess.Id
            controllerPid = $controllerProcess.Id
            tankPid = $tankProcess.Id
        }
        Write-Host "DCP delay=$delay s each way: master PID $($masterProcess.Id), controller PID $($controllerProcess.Id), plant PID $($tankProcess.Id)."

        $deadline = [DateTime]::UtcNow.AddSeconds(180)
        while (!$masterProcess.HasExited) {
            foreach ($participant in @(@{ process = $controllerProcess; log = $controllerLog; errorLog = $controllerErrorLog; name = 'controller' },
                                       @{ process = $tankProcess; log = $tankLog; errorLog = $tankErrorLog; name = 'tank' })) {
                $participant.process.Refresh()
                if ($participant.process.HasExited) {
                    $detail = Get-ParticipantLog @($participant.log, $participant.errorLog)
                    if ($detail -match '(?i)(failed|timed out|exception|NACK|error)') {
                        throw "DCP $($participant.name) stopped with an error: $detail"
                    }
                }
            }
            if ([DateTime]::UtcNow -ge $deadline) { throw "DCP run with $delay s delay timed out." }
            Start-Sleep -Milliseconds 100
        }
        $masterProcess.WaitForExit()
        $masterOutput = Get-ParticipantLog @($masterLog, $masterErrorLog)
        if ($masterOutput -notmatch 'DCP master: stopped cleanly') {
            throw "DCP run with $delay s delay did not stop cleanly: $masterOutput"
        }
        foreach ($participant in @(@{ process = $controllerProcess; log = $controllerLog; errorLog = $controllerErrorLog; name = 'controller' },
                                   @{ process = $tankProcess; log = $tankLog; errorLog = $tankErrorLog; name = 'tank' })) {
            if (!$participant.process.WaitForExit(10000)) {
                throw "DCP $($participant.name) did not stop after the master completed."
            }
            if ($null -ne $participant.process.ExitCode -and $participant.process.ExitCode -ne 0) {
                $detail = Get-ParticipantLog @($participant.log, $participant.errorLog)
                throw "DCP $($participant.name) exited with code $($participant.process.ExitCode): $detail"
            }
        }
        foreach ($csvPath in @($controllerCsv, $tankCsv)) {
            if (!(Test-Path -LiteralPath $csvPath)) { throw "DCP run with $delay s delay did not create $csvPath." }
        }
        $controllerRows = @(Import-Csv -LiteralPath $controllerCsv).Count
        $tankRows = @(Import-Csv -LiteralPath $tankCsv).Count
        $expectedRows = $Duration + 1
        if ($controllerRows -ne $expectedRows -or $tankRows -ne $expectedRows) {
            throw "DCP delay=$delay s run ended before writing all $Duration exchanges."
        }
        Write-Host "DCP delay=$delay s completed: $controllerRows samples, master PID $($masterProcess.Id)."
    } finally {
        foreach ($process in @($controllerProcess, $tankProcess, $masterProcess)) {
            if ($process -and !$process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
        }
    }
}

$runInfo = [ordered]@{
    host = '127.0.0.1'
    duration_s = $Duration
    reference_m = $Reference
    controllerFmu = '../PIController.fmu'
    dcpCommunicationStep_s = 1
    dcpDelayMeaning = 'Simulated one-way signal delay, applied to feedback and command at each exchange.'
    dcpRuns = $runRecords
}
$runInfo | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $output 'run_info.json') -Encoding ASCII
python .\plot_results.py --folder "$output"
if ($LASTEXITCODE -ne 0) { throw 'The interactive DCP results could not be generated.' }
