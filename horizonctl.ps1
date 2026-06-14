param(
    [ValidateSet("start-backend", "start-ui", "start-redis", "start-local-marketdata", "start-local-cache", "local-ui", "health", "status", "performance", "performance-json", "validate-once", "market-check", "test-headless", "migrate-db", "stop")]
    [string]$Action = "health"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$UiHostPort = if ($env:UI_HOST_PORT) { $env:UI_HOST_PORT } else { "8501" }
$DockerBin = "C:\Program Files\Docker\Docker\resources\bin"
if ((Test-Path (Join-Path $DockerBin "docker.exe")) -and ($env:PATH -notlike "*$DockerBin*")) {
    $env:PATH = "$DockerBin;$env:PATH"
}

function Write-Check {
    param([string]$Name, [bool]$Ok, [string]$Detail = "")
    $status = if ($Ok) { "OK" } else { "FAIL" }
    $line = "{0,-28} {1,-5} {2}" -f $Name, $status, $Detail
    if ($Ok) { Write-Host $line -ForegroundColor Green } else { Write-Host $line -ForegroundColor Red }
}

function Test-Command {
    param([string]$Name)
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Invoke-NativeOk {
    param([scriptblock]$Command)
    $oldPreference = $ErrorActionPreference
    try {
        $script:ErrorActionPreference = "Continue"
        & $Command *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    } finally {
        $script:ErrorActionPreference = $oldPreference
    }
}

function Test-TcpPort {
    param([string]$HostName = "127.0.0.1", [int]$Port = 6379)
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $connect = $client.BeginConnect($HostName, $Port, $null, $null)
        $ok = $connect.AsyncWaitHandle.WaitOne(700, $false)
        if ($ok) { $client.EndConnect($connect) }
        $client.Close()
        return $ok
    } catch {
        return $false
    }
}

function Invoke-DockerReady {
    if (-not (Test-Command "docker")) {
        Write-Check "docker on PATH" $false
        return $false
    }
    if (-not (Invoke-NativeOk { docker info })) {
        Write-Check "docker daemon" $false "Docker Desktop engine is unavailable"
        return $false
    }
    return $true
}

function Test-RedisConnection {
    if (Invoke-NativeOk { docker compose exec -T redis redis-cli ping }) {
        Write-Check "redis connection" $true "redis-cli ping"
        return $true
    }
    Write-Check "redis connection" $false "redis-cli ping failed"
    return $false
}

function Test-MariaDbConnection {
    if (Invoke-NativeOk { docker compose exec -T mariadb healthcheck.sh --connect --innodb_initialized }) {
        Write-Check "mariadb connection" $true "healthcheck.sh --connect --innodb_initialized"
        return $true
    }
    Write-Check "mariadb connection" $false "MariaDB healthcheck failed"
    return $false
}

function Invoke-LocalDataLayerReady {
    $env:REDIS_HOST = "localhost"
    $env:MYSQL_HOST = "localhost"
    $redisPortOk = Test-TcpPort -Port 6379
    $mysqlPortOk = Test-TcpPort -Port 3306
    Write-Check "redis port 6379" $redisPortOk
    Write-Check "mariadb port 3306" $mysqlPortOk
    if (-not ($redisPortOk -and $mysqlPortOk)) {
        Write-Host "Local data layer is not reachable; startup skipped." -ForegroundColor Red
        return $false
    }
    $redisAppOk = Invoke-NativeOk { python -c "import sys, horizon_institutional_live_production_grade as h; s=h.RedisState(h.CFG); sys.exit(0 if s.ok else 1)" }
    $dbAppOk = Invoke-NativeOk { python -c "import sys, horizon_institutional_live_production_grade as h; e=h.db_engine(h.CFG); sys.exit(0 if e is not None else 1)" }
    Write-Check "redis app connection" $redisAppOk "REDIS_HOST=localhost"
    Write-Check "mariadb app connection" $dbAppOk "MYSQL_HOST=localhost"
    if (-not ($redisAppOk -and $dbAppOk)) {
        Write-Host "Application cannot connect to Redis/MariaDB; startup skipped." -ForegroundColor Red
        return $false
    }
    return $true
}

function Invoke-ComposeDataLayerReady {
    if (-not (Invoke-DockerReady)) { return $false }

    Write-Host "Starting data layer first: redis + mariadb" -ForegroundColor Cyan
    docker compose up -d mariadb redis

    $redisOk = $false
    $mariadbOk = $false
    for ($i = 1; $i -le 30; $i++) {
        $redisOk = Invoke-NativeOk { docker compose exec -T redis redis-cli ping }
        $mariadbOk = Invoke-NativeOk { docker compose exec -T mariadb healthcheck.sh --connect --innodb_initialized }
        if ($redisOk -and $mariadbOk) { break }
        Start-Sleep -Seconds 2
    }

    Write-Check "redis connection" $redisOk "container=redis"
    Write-Check "mariadb connection" $mariadbOk "container=mariadb"
    if (-not ($redisOk -and $mariadbOk)) {
        Write-Host "Data layer is not ready; backend/UI startup skipped." -ForegroundColor Red
        docker compose ps
        return $false
    }
    return $true
}

function Invoke-StartRedis {
    if (Test-TcpPort -Port 6379) {
        Write-Check "redis port 6379" $true "already listening"
        return $true
    }

    if (-not (Test-Command "docker")) {
        Write-Check "docker on PATH" $false "Docker is required for automatic local Redis"
        return $false
    }
    if (-not (Invoke-NativeOk { docker info })) {
        Write-Check "docker daemon" $false "Docker Desktop engine is unavailable. Start Docker Desktop/WSL, then rerun .\horizonctl.ps1 start-redis"
        return $false
    }

    $existing = docker ps -a --filter "name=^/superbot-local-redis$" --format "{{.Names}}"
    if ($existing -eq "superbot-local-redis") {
        docker start superbot-local-redis | Out-Null
    } else {
        docker run -d --name superbot-local-redis -p 6379:6379 redis:7-alpine | Out-Null
    }

    Start-Sleep -Seconds 3
    $ok = Test-TcpPort -Port 6379
    Write-Check "redis port 6379" $ok "container=superbot-local-redis"
    return $ok
}

function Invoke-StartLocalMarketdata {
    if (-not (Invoke-LocalDataLayerReady)) { return $false }
    $env:RUN_MODE = "marketdata"
    $env:REDIS_HOST = "localhost"
    $env:MYSQL_HOST = "localhost"
    $env:MARKET_DATA_SOURCE = "TESTNET_WS"
    $outLogPath = Join-Path $Root "marketdata-local.out.log"
    $errLogPath = Join-Path $Root "marketdata-local.err.log"
    Start-Process -FilePath "python" -ArgumentList "horizon_institutional_live_production_grade.py" -WorkingDirectory $Root -RedirectStandardOutput $outLogPath -RedirectStandardError $errLogPath -WindowStyle Hidden
    Start-Sleep -Seconds 5
    Write-Check "marketdata worker" $true "started hidden; logs=$outLogPath,$errLogPath"
    return $true
}

function Invoke-Health {
    $failed = $false

    Write-Host "Horizon health check" -ForegroundColor Cyan
    Write-Host "Root: $Root"

    $pythonOk = Test-Command "python"
    Write-Check "python on PATH" $pythonOk
    if ($pythonOk) {
        $env:PYTHONPYCACHEPREFIX = Join-Path $env:TEMP "horizon_pycache_check"
        python -m py_compile horizon_institutional_live_production_grade.py
        $compileOk = $LASTEXITCODE -eq 0
        Write-Check "python compile" $compileOk
        if (-not $compileOk) { $failed = $true }

        $creds = python -c "import horizon_institutional_live_production_grade as h; print('yes' if h.testnet_credentials_present() else 'no')"
        Write-Check "testnet credentials" ($creds -eq "yes") "present=$creds"
    } else {
        $failed = $true
    }

    $dockerOk = Test-Command "docker"
    Write-Check "docker on PATH" $dockerOk
    if ($dockerOk) {
        $composeOk = Invoke-NativeOk { docker compose config }
        Write-Check "compose config" $composeOk
        if (-not $composeOk) { $failed = $true }

        $daemonOk = Invoke-NativeOk { docker info }
        Write-Check "docker daemon" $daemonOk
        if (-not $daemonOk) { $failed = $true }

        $services = docker compose config --services
        $backendHasNoUi = ($services -contains "worker-signal") -and ($services -contains "worker-validation") -and -not ($services -contains "horizon-ui")
        Write-Check "headless backend default" $backendHasNoUi ("services=" + (($services -join ",")))

        $uiServices = docker compose --profile ui config --services
        Write-Check "ui profile available" ($uiServices -contains "horizon-ui")

        if ($daemonOk) {
            Write-Host ""
            Write-Host "Docker service status:" -ForegroundColor Cyan
            docker compose ps
        }
    } else {
        $failed = $true
    }

    $uiHealth = $false
    try {
        $response = Invoke-WebRequest "http://127.0.0.1:$UiHostPort/_stcore/health" -UseBasicParsing -TimeoutSec 3
        $uiHealth = $response.Content.Trim() -eq "ok"
    } catch {
        $uiHealth = $false
    }
    Write-Check "local UI health" $uiHealth "http://127.0.0.1:$UiHostPort/_stcore/health"

    if ($failed) { exit 1 }
}

switch ($Action) {
    "start-backend" {
        if (-not (Invoke-ComposeDataLayerReady)) { exit 1 }
        docker compose up -d --build worker-marketdata worker-validation worker-signal worker-risk worker-ml worker-order worker-pnl
        docker compose ps
    }
    "start-ui" {
        if (-not (Invoke-ComposeDataLayerReady)) { exit 1 }
        docker compose --profile ui up -d --build worker-marketdata worker-validation worker-signal worker-risk worker-ml worker-order worker-pnl horizon-ui
        docker compose --profile ui ps
    }
    "start-redis" {
        if (-not (Invoke-StartRedis)) { exit 1 }
    }
    "start-local-marketdata" {
        if (-not (Invoke-StartLocalMarketdata)) { exit 1 }
    }
    "start-local-cache" {
        if (-not (Invoke-StartRedis)) { exit 1 }
        if (-not (Invoke-StartLocalMarketdata)) { exit 1 }
    }
    "local-ui" {
        if (-not $env:REDIS_HOST) { $env:REDIS_HOST = "localhost" }
        if (-not (Invoke-LocalDataLayerReady)) { exit 1 }
        python -m streamlit run horizon_institutional_live_production_grade.py --server.address=127.0.0.1 --server.port=$UiHostPort
    }
    "health" {
        Invoke-Health
    }
    "status" {
        if (Invoke-NativeOk { docker info }) {
            docker compose ps
        } else {
            Write-Host "Docker daemon: unavailable"
        }
        Write-Check "redis port 6379" (Test-TcpPort -Port 6379)
        try {
            $response = Invoke-WebRequest "http://127.0.0.1:$UiHostPort/_stcore/health" -UseBasicParsing -TimeoutSec 3
            Write-Host "UI health: $($response.Content.Trim())"
        } catch {
            Write-Host "UI health: unavailable"
        }
    }
    "performance" {
        if (Invoke-NativeOk { docker info }) {
            docker compose run --rm --no-deps worker-signal python scripts/performance_report.py --env-file .env.example
        } else {
            python scripts/performance_report.py --env-file .env.example
        }
    }
    "performance-json" {
        if (Invoke-NativeOk { docker info }) {
            docker compose run --rm --no-deps worker-signal python scripts/performance_report.py --env-file .env.example --json
        } else {
            python scripts/performance_report.py --env-file .env.example --json
        }
    }
    "validate-once" {
        if (Invoke-NativeOk { docker info }) {
            docker compose run --rm --no-deps worker-validation python horizon_institutional_live_production_grade.py validation-once
        } else {
            $env:RUN_MODE = "validation-once"
            python horizon_institutional_live_production_grade.py
        }
    }
    "market-check" {
        if (Invoke-NativeOk { docker info }) {
            docker compose run --rm --no-deps worker-marketdata python horizon_institutional_live_production_grade.py market-check
        } else {
            $env:RUN_MODE = "market-check"
            python horizon_institutional_live_production_grade.py
        }
    }
    "test-headless" {
        if (Invoke-NativeOk { docker info }) {
            docker compose run --rm --no-deps worker-validation python scripts/headless_functional_test.py
        } else {
            python scripts/headless_functional_test.py
        }
    }
    "migrate-db" {
        if (Invoke-NativeOk { docker info }) {
            docker compose up -d mariadb redis
            docker compose build worker-signal
            docker compose run --rm --no-deps worker-signal python horizon_institutional_live_production_grade.py migrate-db
        } else {
            python horizon_institutional_live_production_grade.py migrate-db
        }
    }
    "stop" {
        docker compose --profile ui down
    }
}
