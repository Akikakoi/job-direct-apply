# 简历直达 · 一键启动三服务（PG/Redis 容器 + 后端 uvicorn + 前端 Next.js）
# 用法：
#   .\start-all.ps1              # 常规启动（前端 dev 模式；后端/前端各开一个
#                                #   日志窗口，实时滚动输出，同时落文件）
#   .\start-all.ps1 -Prod        # 前端走 build + start
#   .\start-all.ps1 -Stop        # 停止三服务（容器 stop，进程 kill）
#   .\start-all.ps1 -NoPause     # 自动化场景：跑完不停留（CI/会话内调用）
#   .\start-all.ps1 -Hidden      # 不弹日志窗口，输出只落 logs/（自动化用）
# 幂等：端口已在监听的组件自动跳过，可重复执行。
# 双击运行请用 start-all.cmd（跑完窗口停住，能看到结果）。

param(
    [switch]$Prod,
    [switch]$Stop,
    [switch]$NoPause,
    [switch]$Hidden   # 自动化场景：不弹服务窗口，日志只落文件（默认弹窗显示日志）
)

# 启动器不做全局 Stop：PS5.1 下原生命令（docker）往 stderr 写进度会被当作
# terminating error 掐断脚本；可靠性靠本脚本自己的端口/health 检查保证。
$ErrorActionPreference = "Continue"

$Root      = $PSScriptRoot
$Backend   = Join-Path $Root "backend"
$Frontend  = Join-Path $Root "frontend"
$LogDir    = Join-Path $Root "logs"
$PyExe     = Join-Path $Root ".venv\Scripts\python.exe"
$DockerExe = "C:\Program Files\Docker\Docker\Docker Desktop.exe"

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

# 验明正身：端口 UP 且 /health 是本项目的响应才算"后端在跑"
# （8000 曾被 stellar-mall 后端占用，纯端口检查会误报 SKIP）
function Test-Backend {
    if (-not (Test-Port 8001)) { return "down" }
    try {
        $h = Invoke-RestMethod -Uri "http://127.0.0.1:8001/health" -TimeoutSec 3
        if ($h.status -eq "ok") { return "ours" }
    } catch { }
    return "foreign"
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

# 可见窗口拉起长驻进程：日志实时滚动在窗口里 + 逐行追加 UTF-8 文件留档
# （PS5.1 的 Tee-Object 落 UTF-16，grep 不友好，故用 Add-Content -Encoding UTF8）。
# -NoExit：进程退出后窗口保留，方便看最后的报错。
function Start-ServiceWindow([string]$Title, [string]$WorkDir, [string]$Command, [string]$LogFile) {
    if ($Hidden) {
        Start-Hidden "cd /d `"$WorkDir`" && $Command > `"$LogFile`" 2>&1"
        return
    }
    $inner = "`$Host.UI.RawUI.WindowTitle='$Title'; " +
        "Set-Location -LiteralPath '$WorkDir'; " +
        "Write-Host '=== $Title ｜ 日志同时写入 $LogFile（Ctrl+C 停止服务）==='; " +
        "$Command *>&1 | ForEach-Object { Write-Host `$_; Add-Content -Path '$LogFile' -Value `$_ -Encoding UTF8 }"
    Start-Process powershell -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $inner
}

function Main {
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

    # ---------- Stop：停止三服务 ----------
    if ($Stop) {
        Write-Host "== 停止服务 =="
        # 只停本项目端口（8001）；8000 可能是 stellar-mall 等其他项目的服务，不碰
        foreach ($port in 8001, 3000) {
            Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty OwningProcess -Unique |
                ForEach-Object {
                    Write-Host "[STOP] kill port $port (pid $_)"
                    Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
                }
        }
        Push-Location $Backend
        cmd /c "docker compose stop >nul 2>&1"
        Pop-Location
        Write-Host "[OK] 容器已停止（数据保留；再次运行本脚本或 docker compose start 可恢复）"
        Write-Host "[提示] 后端/前端日志窗口若仍开着（-NoExit 保留），直接关闭即可"
        return
    }

    # ---------- 1. Docker 引擎 ----------
    Write-Host "== 1/3 Docker 引擎 =="
    $dockerOk = $false
    cmd /c "docker info >nul 2>&1"
    if ($LASTEXITCODE -eq 0) { $dockerOk = $true }

    if (-not $dockerOk) {
        if (Test-Path $DockerExe) {
            Write-Host "[..] 启动 Docker Desktop（首次冷启动约 30-90s）..."
            Start-Process $DockerExe -WindowStyle Hidden
            for ($i = 0; $i -lt 90; $i += 3) {
                Start-Sleep -Seconds 3
                cmd /c "docker info >nul 2>&1"
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
    cmd /c "cd /d `"$Backend`" && docker compose up -d >nul 2>&1"
    $dbCid = (cmd /c "cd /d `"$Backend`" && docker compose ps -q db 2>nul") -join ""

    if ($dbCid) {
        $st = ""
        for ($i = 0; $i -lt 60; $i += 3) {
            $st = (cmd /c "docker inspect -f {{.State.Health.Status}} $dbCid 2>nul") -join ""
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

    # ---------- 3a. 后端 uvicorn :8001（8000 让给 stellar-mall）----------
    Write-Host "== 3/3 后端 + 前端 =="
    $backendState = Test-Backend
    if ($backendState -eq "ours") {
        Write-Host "[SKIP] 后端已在运行 (port 8001)"
    } elseif ($backendState -eq "foreign") {
        Write-Host "[FAIL] 端口 8001 被其他服务占用且 /health 不是本项目响应，请排查"; return
    } else {
        $uvicornCmd = "`"$PyExe`" -m uvicorn app.main:app --host 0.0.0.0 --port 8001"
        Start-ServiceWindow -Title "简历直达-后端(8001)" -WorkDir $Backend `
            -Command $uvicornCmd -LogFile "$LogDir\uvicorn.log"
        $ready = $false
        for ($i = 0; $i -lt 30; $i += 2) {
            if ((Test-Backend) -eq "ours") { $ready = $true; break }
            Start-Sleep -Seconds 2
        }
        if ($ready) { Write-Host "[OK] 后端 uvicorn 已就绪 (port 8001)" }
        else { Write-Host "[FAIL] 后端启动失败，日志: $LogDir\uvicorn.log"; return }
    }

    # ---------- 3b. 前端 Next.js :3000 ----------
    if (Test-Port 3000) {
        Write-Host "[SKIP] 前端已在运行 (port 3000)"
    } else {
        if ($Prod) {
            Write-Host "[..] 前端 build + start（Prod 模式）..."
            Push-Location $Frontend
            cmd /c "npm run build >nul 2>&1"
            Pop-Location
            $feCmd = "npm run start"
        } else {
            $feCmd = "npm run dev"
        }
        Start-ServiceWindow -Title "简历直达-前端(3000)" -WorkDir $Frontend `
            -Command $feCmd -LogFile "$LogDir\next.log"
        if (-not (Wait-Port 3000 "前端 Next.js" 60)) {
            Write-Host "[FAIL] 前端启动失败，日志: $LogDir\next.log"; return
        }
    }

    # ---------- 冒烟 ----------
    $health = $null
    try { $health = Invoke-RestMethod -Uri "http://127.0.0.1:8001/health" -TimeoutSec 5 } catch { }

    Write-Host ""
    if ($health -and $health.status -eq "ok") {
        Write-Host "== 全部就绪 [OK]  后端 /health ok ｜ 前端 http://localhost:3000 =="
    } else {
        Write-Host "== 后端 /health 异常，请查看 $LogDir\uvicorn.log =="
    }
}

try {
    Main
} catch {
    Write-Host "[FAIL] 脚本异常: $_"
}

# 双击/右键运行时窗口停留，能看到结果；-NoPause 供自动化调用
if (-not $NoPause) {
    Write-Host ""
    Read-Host "按回车键关闭窗口" | Out-Null
}
