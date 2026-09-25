$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$apiPort = if ($env:API_PORT) { $env:API_PORT } else { "8000" }
$webPort = if ($env:WEB_PORT) { $env:WEB_PORT } else { "3000" }

Push-Location $projectRoot
try {
    Write-Host "==> Python-зависимости (uv sync)"
    uv sync --extra dev

    Write-Host "==> Node-зависимости (pnpm install, web/)"
    Push-Location (Join-Path $projectRoot "web")
    try { pnpm install --frozen-lockfile } finally { Pop-Location }

    $env:NEXT_PUBLIC_API_BASE = "http://127.0.0.1:$apiPort"
    Write-Host "==> API: http://127.0.0.1:$apiPort"
    $api = Start-Process -FilePath "uv" -ArgumentList @(
        "run", "uvicorn", "deckforge.api.app:app", "--host", "127.0.0.1",
        "--port", $apiPort, "--reload", "--reload-dir", "src"
    ) -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru

    Write-Host "==> Веб-интерфейс: http://127.0.0.1:$webPort"
    $web = Start-Process -FilePath "pnpm.cmd" -ArgumentList @("dev", "--port", $webPort) `
        -WorkingDirectory (Join-Path $projectRoot "web") -WindowStyle Hidden -PassThru

    Write-Host "Нажмите Ctrl+C, чтобы остановить оба процесса."
    Wait-Process -Id $api.Id, $web.Id
}
finally {
    if ($api -and -not $api.HasExited) { Stop-Process -Id $api.Id -Force }
    if ($web -and -not $web.HasExited) { Stop-Process -Id $web.Id -Force }
    Pop-Location
}
