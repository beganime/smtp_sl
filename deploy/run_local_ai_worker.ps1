$ErrorActionPreference = 'Continue'
$projectPath = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectPath 'venv\Scripts\python.exe'
$managePath = Join-Path $projectPath 'manage.py'
$logDirectory = Join-Path $projectPath 'logs'
$logPath = Join-Path $logDirectory 'ai-worker.log'
$createdNew = $false
$workerMutex = New-Object System.Threading.Mutex($true, 'SMTP_SL_AI_Worker', [ref]$createdNew)

if (-not $createdNew) {
    exit 0
}

$env:AI_WORKER_TOKEN = [Environment]::GetEnvironmentVariable('AI_WORKER_TOKEN', 'User')
$env:PYTHONUNBUFFERED = '1'
$env:PYTHONIOENCODING = 'utf-8'
$configuredServerUrl = [Environment]::GetEnvironmentVariable('AI_WORKER_SERVER_URL', 'User')
if ($configuredServerUrl) {
    $env:AI_WORKER_SERVER_URL = $configuredServerUrl
}

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
Set-Location $projectPath

try {
    while ($true) {
        $analysisJob = Start-Job -ScriptBlock {
            param($python, $manage)
            [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
            $env:PYTHONIOENCODING = 'utf-8'
            & $python $manage run_remote_ai_worker --once 2>&1
        } -ArgumentList $pythonPath, $managePath

        $finished = Wait-Job -Job $analysisJob -Timeout 300
        if ($null -eq $finished) {
            Stop-Job -Job $analysisJob
            Add-Content -Path $logPath -Value "$(Get-Date -Format o) Worker timed out after 300 seconds; restarting."
        }
        $jobOutput = @(Receive-Job -Job $analysisJob)
        $jobOutput | Out-File -FilePath $logPath -Append -Encoding utf8
        Remove-Job -Job $analysisJob -Force
        if (($jobOutput | Out-String) -match 'BILLING_CREDITS_DEPLETED') {
            Start-Sleep -Seconds 3600
        }
        else {
            Start-Sleep -Seconds 15
        }
    }
}
finally {
    $workerMutex.ReleaseMutex()
    $workerMutex.Dispose()
}
