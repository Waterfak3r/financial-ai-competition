[CmdletBinding()]
param(
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$backendSource = Join-Path $projectRoot 'backend\src'
$frontendRoot = Join-Path $projectRoot 'frontend'
$apiModule = Join-Path $backendSource 'finagent\api\app.py'
$viteEntry = Join-Path $frontendRoot 'node_modules\vite\bin\vite.js'
$apiUrl = 'http://127.0.0.1:8000'
$frontendUrl = 'http://127.0.0.1:5173'
$startupId = [Guid]::NewGuid().ToString('N')
$logDirectory = Join-Path $projectRoot "artifacts\tmp\startup-$startupId"
$startedServices = @()

function Get-ListeningPids {
    param([Parameter(Mandatory = $true)][int]$Port)

    $listenerPids = @()
    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        $connections = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
        if ($connections.Count -gt 0) {
            $listenerPids = @($connections | ForEach-Object { [int]$_.OwningProcess } | Sort-Object -Unique)
        }
        return $listenerPids
    }

    $netstatPath = Join-Path $env:SystemRoot 'System32\netstat.exe'
    if (-not (Test-Path -LiteralPath $netstatPath)) {
        throw '无法确认本机端口占用状态（缺少 Get-NetTCPConnection 和 netstat.exe）。'
    }
    $lines = & $netstatPath -ano -p tcp 2>$null
    foreach ($line in $lines) {
        if ($line -match '^\s*TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$' -and [int]$Matches[1] -eq $Port) {
            $listenerPids += [int]$Matches[2]
        }
    }
    return @($listenerPids | Sort-Object -Unique)
}

function Test-FintraceApi {
    try {
        $response = Invoke-WebRequest -Uri "$apiUrl/openapi.json" -UseBasicParsing -TimeoutSec 2
        if ([int]$response.StatusCode -ne 200) { return $null }
        $schema = $response.Content | ConvertFrom-Json
        if ($schema.info.title -ne 'finagent annual precheck') { return $null }
        $paths = @($schema.paths.PSObject.Properties | ForEach-Object { $_.Name })
        if ($paths -notcontains '/v1/annual-analysis-jobs' -or $paths -notcontains '/v1/model-settings') { return $null }
        return $true
    }
    catch {
        return $null
    }
}

function Test-FintraceVite {
    try {
        $response = Invoke-WebRequest -Uri "$frontendUrl/src/main.tsx" -UseBasicParsing -TimeoutSec 2
        if ([int]$response.StatusCode -ne 200) { return $null }
        if ($response.Content.IndexOf('AnnualPrecheckPage', [System.StringComparison]::Ordinal) -lt 0) { return $null }
        if ($response.Content.IndexOf('styles/app.css', [System.StringComparison]::Ordinal) -lt 0) { return $null }
        return $true
    }
    catch {
        return $null
    }
}

function Stop-StartedServices {
    param([object[]]$Services)

    $orderedServices = @($Services)
    [System.Array]::Reverse($orderedServices)
    foreach ($service in $orderedServices) {
        try {
            $current = Get-Process -Id $service.Id -ErrorAction SilentlyContinue
            if ($null -ne $current -and $current.StartTime.ToUniversalTime().Ticks -eq $service.StartTimeTicks) {
                Stop-Process -Id $service.Id -Force -ErrorAction Stop
            }
        }
        catch {
            Write-Host "回滚本次创建的 $($service.Name) 进程时遇到问题（PID $($service.Id)）：$($_.Exception.Message)" -ForegroundColor Yellow
        }
    }
}

try {
    if (-not (Test-Path -LiteralPath $apiModule -PathType Leaf)) {
        throw "找不到后端入口：$apiModule"
    }
    if (-not (Test-Path -LiteralPath $viteEntry -PathType Leaf)) {
        throw "前端 Vite 未安装：$viteEntry。请先按 README.md 安装项目依赖；启动脚本不会安装依赖。"
    }

    $pythonCandidates = @()
    if (-not [string]::IsNullOrWhiteSpace($env:VIRTUAL_ENV)) {
        $pythonCandidates += Join-Path $env:VIRTUAL_ENV 'Scripts\python.exe'
    }
    $pythonCandidates += Join-Path $projectRoot '.venv\Scripts\python.exe'
    $pathPython = Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $pathPython) { $pythonCandidates += $pathPython.Source }
    $pythonCandidates = @($pythonCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -Unique)
    if ($pythonCandidates.Count -eq 0) {
        throw '未找到 Python。请激活项目虚拟环境、在项目根目录创建 .venv，或将 python.exe 加入 PATH。'
    }

    $pythonCheck = 'import sys; import fastapi, uvicorn, multipart, fitz; assert sys.version_info >= (3, 11)'
    $pythonPath = $null
    $pythonFailure = $null
    foreach ($candidate in $pythonCandidates) {
        try {
            $checkOutput = & $candidate -c $pythonCheck 2>&1
            if ($LASTEXITCODE -eq 0) {
                $pythonPath = $candidate
                break
            }
            $pythonFailure = "未通过 Python 版本或依赖检查：$candidate"
        }
        catch {
            $pythonFailure = "无法完成 Python 版本或依赖检查：$candidate"
        }
    }
    if ($null -eq $pythonPath) {
        $message = 'Python 需要 3.11 或更高版本，并应已安装 fastapi、uvicorn、python-multipart 和 PyMuPDF。请按 README.md 安装 backend/pyproject.toml 中的依赖；启动脚本不会安装依赖。'
        if (-not [string]::IsNullOrWhiteSpace($pythonFailure)) { $message += "`n$pythonFailure" }
        throw $message
    }
    $nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $nodeCommand) {
        throw '未找到 node.exe。请安装 Node.js 并将其加入 PATH；启动脚本不会安装依赖。'
    }
    $nodeVersion = & $nodeCommand.Source --version 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Node.js 无法启动：$nodeVersion" }

    $apiPids = @(Get-ListeningPids -Port 8000)
    $apiRecognized = [bool](Test-FintraceApi)
    if ($apiRecognized -and $apiPids.Count -eq 0) {
        throw '8000 端口响应为本项目 API，但无法读取其进程 PID；为避免不完整复用，已停止启动。'
    }
    if (-not $apiRecognized -and $apiPids.Count -gt 0) {
        throw "8000 端口已被未知服务占用（PID $($apiPids -join ', ')），未启动或停止任何服务。"
    }

    $frontendPids = @(Get-ListeningPids -Port 5173)
    $frontendRecognized = [bool](Test-FintraceVite)
    if ($frontendRecognized -and $frontendPids.Count -eq 0) {
        throw '5173 端口响应为本项目 Vite 页面，但无法读取其进程 PID；为避免不完整复用，已停止启动。'
    }
    if (-not $frontendRecognized -and $frontendPids.Count -gt 0) {
        throw "5173 端口已被未知服务占用（PID $($frontendPids -join ', ')），未启动或停止任何服务。"
    }

    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    Write-Host "项目目录：$projectRoot"
    Write-Host "Python：$pythonPath"
    Write-Host "Node.js：$($nodeCommand.Source) $nodeVersion"
    Write-Host '未读取模型凭据；启动过程不会调用模型。'

    if (-not $apiRecognized) {
        $apiStdout = Join-Path $logDirectory 'api.stdout.log'
        $apiStderr = Join-Path $logDirectory 'api.stderr.log'
        $apiArguments = '-m uvicorn finagent.api.app:app --app-dir "' + $backendSource + '" --host 127.0.0.1 --port 8000'
        $process = Start-Process -FilePath $pythonPath -ArgumentList $apiArguments -WorkingDirectory $projectRoot -RedirectStandardOutput $apiStdout -RedirectStandardError $apiStderr -PassThru -WindowStyle Hidden
        $process.Refresh()
        $startedServices += [pscustomobject]@{ Name = 'API'; Id = [int]$process.Id; StartTimeTicks = $process.StartTime.ToUniversalTime().Ticks; Process = $process }
    }

    if (-not $frontendRecognized) {
        $viteStdout = Join-Path $logDirectory 'vite.stdout.log'
        $viteStderr = Join-Path $logDirectory 'vite.stderr.log'
        $viteArguments = '"' + $viteEntry + '"'
        $process = Start-Process -FilePath $nodeCommand.Source -ArgumentList $viteArguments -WorkingDirectory $frontendRoot -RedirectStandardOutput $viteStdout -RedirectStandardError $viteStderr -PassThru -WindowStyle Hidden
        $process.Refresh()
        $startedServices += [pscustomobject]@{ Name = 'Vite'; Id = [int]$process.Id; StartTimeTicks = $process.StartTime.ToUniversalTime().Ticks; Process = $process }
    }

    $deadline = (Get-Date).AddSeconds(45)
    $apiReady = $apiRecognized
    $frontendReady = $frontendRecognized
    while ((-not $apiReady -or -not $frontendReady) -and (Get-Date) -lt $deadline) {
        if (-not $apiReady) { $apiReady = [bool](Test-FintraceApi) }
        if (-not $frontendReady) { $frontendReady = [bool](Test-FintraceVite) }
        if ($apiReady -and $frontendReady) { break }
        foreach ($service in $startedServices) {
            $service.Process.Refresh()
            if ($service.Process.HasExited) {
                throw "$($service.Name) 进程已退出；请查看日志目录：$logDirectory"
            }
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $apiReady -or -not $frontendReady) {
        throw "服务未在 45 秒内就绪；请查看日志目录：$logDirectory"
    }

    if (-not $apiRecognized) { $apiPids = @(Get-ListeningPids -Port 8000) }
    if (-not $frontendRecognized) { $frontendPids = @(Get-ListeningPids -Port 5173) }
    $apiPid = [int]$apiPids[0]
    $frontendPid = [int]$frontendPids[0]

    Write-Host ''
    Write-Host 'FINTRACE 已就绪。'
    Write-Host "API：$apiUrl（PID $apiPid）"
    Write-Host "前端：$frontendUrl（PID $frontendPid）"
    Write-Host "日志目录：$logDirectory"
    Write-Host "停止这两个服务：Stop-Process -Id $apiPid,$frontendPid"
    Write-Host '关闭启动窗口后，服务仍在后台运行。'
    if (-not $NoBrowser) {
        try {
            Start-Process -FilePath $frontendUrl -ErrorAction Stop
        }
        catch {
            Write-Host "浏览器未能自动打开，请手动访问：$frontendUrl" -ForegroundColor Yellow
        }
    }
}
catch {
    Stop-StartedServices -Services $startedServices
    Write-Host "启动失败：$($_.Exception.Message)" -ForegroundColor Red
    if (Test-Path -LiteralPath $logDirectory -PathType Container) {
        Write-Host "本次启动日志：$logDirectory"
    }
    exit 1
}
