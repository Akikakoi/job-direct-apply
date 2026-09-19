# 简历直达 · 一键启动三服务（PG/Redis 容器 + 后端 uvicorn + 前端 Next.js）
# 用法：
#   .\start-all.ps1              # 常规启动（前端 dev 模式）
#   .\start-all.ps1 -Prod        # 前端走 build + start
#   .\start-all.ps1 -Stop        # 停止三服务（容器 stop，进程 kill）
# 幂等：端口已在监听的组件自动跳过，可重复执行。

param(
    [switch]$Prod,
    [switch]$Stop
)

# 启动器不做全局 Stop：PS5.1 下原生命令（docker）往 stderr 写进度会被当作
# terminating error 掐断脚本；可靠性靠本脚本自己的端口/health 检查保证。
$ErrorActionPreference = "Continue"
$Root     = $PSScriptRoot
$Backend  = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$LogDir   = Join-Path $Root "logs"
$PyExe    = Join-Path $Root ".venv\Scripts\python.exe"
$DockerExe = "C:\Program Files\Docker\Docker\Docker Desktop.exe"

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

function Test-Port([int]$Port) {
    $c = New-Object Net.Sockets.TcpClient
    try {
        $c.Connect("127.0.0.1", $Port)
        return $c.Connected
    } catch {
        return $false
    } finally {
        $c.Close()
    }
}

function Wait-Port([int]$Port, [string]$Name, [int]$Seconds) {
    for ($i = 0; $i -lt $Seconds; $i += 2) {
        if (Test-Port $Port) {
            Write-Host "[OK] $Name 已就绪 (port $Port, 等待 $([int]$i)s)"
            return $true
        }
        Start-Sleep -Seconds 2
    }
    Write-Host "[FAIL] $Name 等待超时（${Seconds}s）"
    return $false
}

# 隐藏窗口拉起长驻进程，输出落文件（cmd /c 负责重定向，规避 Start-Process 重定向限制）
function Start-Hidden([string]$Cmd) {
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c", $Cmd -WindowStyle Hidden
}

# ---------- Stop：停止三服务 ----------
if ($Stop) {
    Write-Host "== 停止服务 =="
    foreach ($port in 8000, 3000) {
        try {
            Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty OwningProcess -Unique |
                ForEach-Object {
                    Write-Host "[STOP] kill port $port (pid $_)"
                    Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
                }
        } catch { }
    }
    Push-Location $Backend
    docker compose stop 2>&1 | Out-Null
    Pop-Location
    Write-Host "[OK] 容器已停止（数据保留；docker compose start 可恢复）"
    return
}

# ---------- 1. Docker 引擎 ----------
Write-Host "== 1/3 Docker 引擎 =="
$dockerOk = $false
try {
    docker info 2>$null | Out-Null
    $dockerOk = ($LASTEXITCODE -eq 0)
} catch { }

if (-not $dockerOk) {
    if (Test-Path $DockerExe) {
        Write-Host "[..] 启动 Docker Desktop..."
        Start-Process $DockerExe -WindowStyle Hidden
        for ($i = 0; $i -lt 90; $i += 3) {
            Start-Sleep -Seconds 3
            docker info 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { $dockerOk = $true; break }
        }
    } else {
        Write-Host "[FAIL] 未找到 Docker Desktop（$DockerExe）"
    }
}
if (-not $dockerOk) { Write-Host "[FAIL] Docker 引擎不可用，无法启动数据库"; return }
Write-Host "[OK] Docker 引擎在线"

# ---------- 2. PG / Redis 容器 ----------
Write-Host "== 2/3 数据库容器（PG + Redis）=="
Push-Location $Backend
docker compose up -d 2>&1 | Out-Null
$dbCid = docker compose ps -q db
Pop-Location

if ($dbCid) {
    for ($i = 0; $i -lt 60; $i += 3) {
        $st = docker inspect -f "{{.State.Health.Status}}" $dbCid 2>$null
        if ($st -eq "healthy") { break }
        Start-Sleep -Seconds 3
    }
    if ($st -eq "healthy") { Write-Host "[OK] PG healthy" }
    else { Write-Host "[FAIL] PG 未 healthy（当前: $st）"; return }
} else {
    Write-Host "[FAIL] 未找到 db 容器（docker compose ps -q db 为空）"; return
}
if (Test-Port 6379) { Write-Host "[OK] Redis 6379 在线" }
else { Write-Host "[FAIL] Redis 6379 未监听"; return }

# ---------- 3a. 后端 uvicorn :8000 ----------
Write-Host "== 3/3 后端 + 前端 =="
if (Test-Port 8000) {
    Write-Host "[SKIP] 后端已在运行 (port 8000)"
} else {
    $uvicornCmd = "cd /d `"$Backend`" && `"$PyExe`" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 > `"$LogDir\uvicorn.log`" 2>&1"
    Start-Hidden $uvicornCmd
    if (-not (Wait-Port 8000 "后端 uvicorn" 30)) {
        Write-Host "[FAIL] 后端启动失败，日志: $LogDir\uvicorn.log"; return
    }
}

# ---------- 3b. 前端 Next.js :3000 ----------
if (Test-Port 3000) {
    Write-Host "[SKIP] 前端已在运行 (port 3000)"
} else {
    if ($Prod) {
        Write-Host "[..] 前端 build + start（Prod 模式）..."
        Push-Location $Frontend
        npm run build 2>&1 | Out-Null
        Pop-Location
        $feCmd = "cd /d `"$Frontend`" && npm run start > `"$LogDir\next.log`" 2>&1"
    } else {
        $feCmd = "cd /d `"$Frontend`" && npm run dev > `"$LogDir\next.log`" 2>&1"
    }
    Start-Hidden $feCmd
    if (-not (Wait-Port 3000 "前端 Next.js" 60)) {
        Write-Host "[FAIL] 前端启动失败，日志: $LogDir\next.log"; return
    }
}

# ---------- 冒烟 ----------
$health = $null
try { $health = Invoke-RestMethod -Uri "http://localhost:8000/health" -TimeoutSec 5 } catch { }

Write-Host ""
if ($health -and $health.status -eq "ok") {
    Write-Host "== 全部就绪 [OK]  后端 /health ok ｜ 前端 http://localhost:3000 =="
} else {
    Write-Host "== 后端 /health 异常，请查看 $LogDir\uvicorn.log =="
}
