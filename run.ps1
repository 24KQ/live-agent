# Phase 7C: LiveAgent 快捷启动入口
param(
    [string]$Command = "help",
    [switch]
)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Info { param([string]$Message) Write-Host ">>> $Message" -ForegroundColor Cyan }
function Write-OK { param([string]$Message) Write-Host "[OK] $Message" -ForegroundColor Green }
function Write-Fail { param([string]$Message) Write-Host "[FAIL] $Message" -ForegroundColor Red }

switch ($Command.ToLower()) {
    "up" {
        Write-Info "LiveAgent Quick Start: migrate + seed + server"
        if ($DryRun) { & python -x "$ProjectRoot\\scripts\\run_all.py" migrate --dry-run }
        else { & python -x "$ProjectRoot\\scripts\\run_all.py" up }
        break
    }
    "demo" {
        Write-Info "LiveAgent End-to-End Demo"
        & python -x "$ProjectRoot\\scripts\\run_all.py" demo
        break
    }
    "docker" {
        Write-Info "Starting Docker infrastructure"
        docker compose -f "$ProjectRoot\\docker-compose.yml" up -d
        if ($LASTEXITCODE -eq 0) {
            Write-OK "Docker services started"
            Write-Info "PostgreSQL: localhost:5432"
            Write-Info "Kafka: localhost:9092"
            Write-Info "MinIO: http://localhost:8900"
        } else { Write-Fail "Docker start failed" }
        break
    }
    "migrate" {
        $dryArg = if ($DryRun) { "--dry-run" } else { "" }
        & python -x "$ProjectRoot\\scripts\\run_all.py" migrate $dryArg
        break
    }
    "seed" { & python -x "$ProjectRoot\\scripts\\run_all.py" seed; break }
    "server" { & python -x "$ProjectRoot\\scripts\\run_all.py" server; break }
    default {
        Write-Host @"
LiveAgent 快捷启动入口

用法:
  .\\run.ps1 up       一键启动 (migrate + seed + server)
  .\\run.ps1 demo     端到端全链路演示
  .\\run.ps1 docker   docker compose up -d
  .\\run.ps1 migrate  仅执行数据库迁移
  .\\run.ps1 seed     仅填充种子数据
  .\\run.ps1 server   仅启动 API 服务
  .\\run.ps1 help     显示本帮助
"@
        break
    }
}
