# Open LOCODE from PowerShell. Starts the engine only when nothing answers
# /api/health, then opens the shell in the default browser; the engine keeps
# running after this exits, so running it again reuses it instead of starting a
# second one.
#
#   .\scripts\locode-run.ps1
#   .\scripts\locode-run.ps1 -Port 8080
#   .\scripts\locode-run.ps1 -NoBrowser      # start the engine only
#
# The Unix path is scripts/locode-run.sh; both do the same thing, and both pick
# up .venv created by install.sh (Git Bash) because a venv is a venv.
[CmdletBinding()]
param(
    [int]$Port = $(if ($env:LOCODE_PORT) { [int]$env:LOCODE_PORT } else { 3000 }),
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$url = "http://127.0.0.1:$Port/"

function Test-Engine {
    # /api/health is the endpoint that proves the engine is the engine: a port
    # squatter that refuses the path is not reused.
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 -Uri "${url}api/health"
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

if (-not (Test-Engine)) {
    $engine = Join-Path $here '.venv\Scripts\python.exe'
    $argv = @('-m', 'uvicorn', 'server:app', '--host', '127.0.0.1', '--port', "$Port")
    if (-not (Test-Path $engine)) {
        $py = Get-Command py -ErrorAction SilentlyContinue
        if (-not $py) {
            throw "LOCODE: no .venv and no py launcher. Run install.sh in Git Bash, or 'py -3 -m venv .venv' + pip install -r requirements.txt first."
        }
        $engine = $py.Source
        $argv = @('-3') + $argv
    }

    $state = Join-Path $env:LOCALAPPDATA 'locode'
    New-Item -ItemType Directory -Force -Path $state | Out-Null
    $log = Join-Path $state "engine-$Port.log"
    $err = Join-Path $state "engine-$Port.err.log"

    # WORKSPACE is dropped on purpose: the launcher inherits the session's
    # environment, and a stale WORKSPACE would open a different project.
    Remove-Item Env:\WORKSPACE -ErrorAction SilentlyContinue
    $proc = Start-Process -FilePath $engine -ArgumentList $argv -WorkingDirectory $here `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput $log -RedirectStandardError $err
    Set-Content -Path (Join-Path $state "engine-$Port.pid") -Value $proc.Id

    for ($i = 0; $i -lt 75; $i++) {
        if (Test-Engine) { break }
        Start-Sleep -Milliseconds 200
    }
    if (-not (Test-Engine)) {
        Write-Host "LOCODE did not answer on $url - last lines of ${log} and ${err}:" -ForegroundColor Red
        if (Test-Path $log) { Get-Content $log -Tail 20 }
        if (Test-Path $err) { Get-Content $err -Tail 20 }
        exit 1
    }
    Write-Host "engine started (pid $($proc.Id))"
    Write-Host "engine log: $log"
} else {
    Write-Host "engine already running"
}

Write-Host "LOCODE: $url"
if (-not $NoBrowser) {
    Start-Process $url
}
