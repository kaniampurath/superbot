param(
    [ValidateSet("start-backend", "start-ui", "local-ui", "health", "status", "performance", "performance-json", "validate-once", "test-headless", "migrate-db", "stop")]
    [string]$Action = "health"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$UiHostPort = if ($env:UI_HOST_PORT) { $env:UI_HOST_PORT } else { "8501" }

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
        docker compose up -d --build
        docker compose ps
    }
    "start-ui" {
        docker compose --profile ui up -d --build
        docker compose --profile ui ps
    }
    "local-ui" {
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
