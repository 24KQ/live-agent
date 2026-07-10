# Phase 6A 前端一键启动脚本
# 用法: .scriptsun_frontend.ps1

Write-Host "=== LiveAgent Frontend Startup ===" -ForegroundColor Cyan

# Step 1: 种子数据
Write-Host "[1/2] Seeding frontend data..." -ForegroundColor Yellow
python scripts/seed_frontend_data.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "Seed failed, but continuing..." -ForegroundColor Red
}

# Step 2: 启动 API Server
Write-Host "[2/2] Starting API server on http://localhost:8100" -ForegroundColor Yellow
python -m uvicorn src.gateway.api_server:app --port 8100
