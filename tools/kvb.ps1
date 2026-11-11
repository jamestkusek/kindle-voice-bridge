#Requires -Version 5.1
<#
.SYNOPSIS
    Host-side helper for Kindle Voice Bridge.

.DESCRIPTION
    One entry point for the operations that run on the computer:
    start the receiver, start the transcription worker, copy the device
    files to a mounted Kindle, and run the test suite.

    The script never prints the bearer token or the OpenRouter key.
    It reads the token from .data\receiver-config.json, or from the
    Kindle upload.json when the local file is absent.

    The script does not start the Bluetooth handoff and does not run
    anything on the Kindle. The handoff stops the stock Bluetooth daemon
    and can leave Bluetooth unusable.

.EXAMPLE
    .\tools\kvb.ps1 receiver

.EXAMPLE
    .\tools\kvb.ps1 deploy -KindleRoot D:\ -ServerUrl http://192.168.1.20:18767
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Command = 'help',

    [Alias('h')]
    [switch]$Help,

    [string]$KindleRoot = 'D:\',

    [string]$ServerUrl,

    [string]$Python,

    [int]$Port = 0,

    [switch]$LocalOnly,

    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DataDir = Join-Path $ProjectRoot '.data'
$LocalConfig = Join-Path $DataDir 'receiver-config.json'
$KeyFile = Join-Path $ProjectRoot 'server\voice_bridge_server\voxtral-openrouter-key.txt'
$StorageDir = Join-Path $env:LOCALAPPDATA 'KindleVoiceBridge\recordings'
$ReceiverLog = Join-Path $DataDir 'receiver.log'
$TranscribeLog = Join-Path $DataDir 'transcribe.log'
$DefaultPort = 18767

function Show-Usage {
    Write-Output @'
Kindle Voice Bridge host helper.

Usage:
  tools\kvb.ps1 receiver  [-Port N] [-LocalOnly] [-Python PATH]
  tools\kvb.ps1 transcriber [-Python PATH]
  tools\kvb.ps1 deploy    [-KindleRoot D:\] [-ServerUrl URL] [-Force]
  tools\kvb.ps1 test      [-Python PATH]
  tools\kvb.ps1 help

Commands:
  receiver     Start the FastAPI receiver in the background.
  transcriber  Start the OpenRouter transcription worker in the background.
  deploy       Copy the device files to a mounted Kindle.
  test         Run the test suite with pytest.
  help         Print this text and exit.

Options:
  -Port N          Receiver port. The default comes from the config file.
  -LocalOnly       Bind the receiver to 127.0.0.1 only.
  -Python PATH     Interpreter to use. The default is .venv, then python.
  -KindleRoot D:\  Mount point of the Kindle USB storage.
  -ServerUrl URL   Write upload.json on the Kindle with this server URL.
  -Force           Replace an existing upload.json.

The receiver reads the token from .data\receiver-config.json. When that
file is absent, the receiver reads the token from the Kindle upload.json.
When both files are absent, the receiver writes a new token to
.data\receiver-config.json. The script never prints the token.

The Bluetooth handoff is not automated. It stops the stock Bluetooth
daemon and can leave Bluetooth unusable. Recovery is not guaranteed.
'@
}

function Write-WarningLine {
    param([string]$Message)
    Write-Output "warning: $Message"
}

function Read-JsonFile {
    param([string]$Path)
    $text = [System.IO.File]::ReadAllText($Path)
    return $text | ConvertFrom-Json
}

function Get-KindleUploadPath {
    return (Join-Path (Join-Path $KindleRoot 'kindle-voice-bridge') 'upload.json')
}

function Get-ReceiverConfig {
    if (Test-Path -LiteralPath $LocalConfig) {
        $config = Read-JsonFile $LocalConfig
        $token = [string]$config.token
        $configPort = 0
        if ($null -ne $config.port) {
            $configPort = [int]$config.port
        }
        if ($token) {
            return [pscustomobject]@{
                Token = $token
                Port = $configPort
                Source = $LocalConfig
            }
        }
    }

    $kindleConfig = Get-KindleUploadPath
    if (Test-Path -LiteralPath $kindleConfig) {
        $config = Read-JsonFile $kindleConfig
        $token = [string]$config.token
        $configPort = 0
        if ($config.server_url) {
            $configPort = ([uri]$config.server_url).Port
        }
        if ($token) {
            return [pscustomobject]@{
                Token = $token
                Port = $configPort
                Source = $kindleConfig
            }
        }
    }

    return $null
}

function New-ReceiverConfig {
    $bytes = New-Object 'System.Byte[]' 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    } finally {
        $rng.Dispose()
    }
    $token = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')

    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    $payload = [ordered]@{
        token = $token
        port = $DefaultPort
    }
    [System.IO.File]::WriteAllText($LocalConfig, ($payload | ConvertTo-Json))
    Write-Host "created $LocalConfig with a new token (token not shown)"
    return [pscustomobject]@{
        Token = $token
        Port = $DefaultPort
        Source = $LocalConfig
    }
}

function Test-PythonProbe {
    param(
        [string]$Exe,
        [string]$Modules
    )
    try {
        $null = & $Exe -c "import $Modules" 2>&1
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Resolve-Python {
    param([string]$Modules = 'sys')

    if ($Python) {
        if (-not (Test-Path -LiteralPath $Python)) {
            throw "Python interpreter not found: $Python"
        }
        $resolved = (Resolve-Path -LiteralPath $Python).Path
        if (-not (Test-PythonProbe $resolved $Modules)) {
            throw "The interpreter cannot import $Modules. Install the test extras or pass -Python."
        }
        return $resolved
    }

    $candidates = @()
    $venvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython) {
        $candidates += $venvPython
    }
    $candidates += 'python'

    foreach ($candidate in $candidates) {
        if (Test-PythonProbe $candidate $Modules) {
            return $candidate
        }
    }

    throw "No Python interpreter can import $Modules. Install Python 3.11 or later and the project dependencies, or pass -Python."
}

function Test-PortListening {
    param([int]$Number)
    try {
        $existing = Get-NetTCPConnection -LocalPort $Number -State Listen -ErrorAction SilentlyContinue
        return $existing
    } catch {
        return $null
    }
}

function Start-Receiver {
    $config = Get-ReceiverConfig
    if ($null -eq $config) {
        $config = New-ReceiverConfig
    }

    $listenPort = $config.Port
    if ($Port -gt 0) {
        $listenPort = $Port
    }
    if ($listenPort -le 0) {
        $listenPort = $DefaultPort
    }

    $existing = Test-PortListening $listenPort
    if ($existing) {
        $pids = ($existing | ForEach-Object { $_.OwningProcess }) -join ','
        Write-Output "already listening on port $listenPort (pid $pids)"
        return
    }

    $python = Resolve-Python 'uvicorn, fastapi'

    New-Item -ItemType Directory -Force -Path $StorageDir | Out-Null
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

    $env:KVB_TOKEN = $config.Token
    $env:KVB_STORAGE_DIR = $StorageDir
    $env:KVB_ALLOW_INSECURE_LAN = '1'

    if (Test-Path -LiteralPath $KeyFile) {
        $env:OPENROUTER_API_KEY = [System.IO.File]::ReadAllText($KeyFile).Trim()
        Write-Output 'transcription: on'
    } else {
        Write-Output 'transcription: off (no key file)'
    }

    $bindAddress = '0.0.0.0'
    if ($LocalOnly) {
        $bindAddress = '127.0.0.1'
    } else {
        Write-WarningLine 'the receiver listens on all interfaces. HTTP exposes the bearer token and the audio on the network. Use a trusted network only.'
    }

    $arguments = @(
        '-m', 'uvicorn', 'voice_bridge_server.app:create_app_from_env',
        '--factory', '--host', $bindAddress, '--port', "$listenPort"
    )

    $proc = Start-Process -FilePath $python `
        -ArgumentList $arguments `
        -WorkingDirectory (Join-Path $ProjectRoot 'server') `
        -WindowStyle Hidden `
        -RedirectStandardOutput $ReceiverLog `
        -RedirectStandardError "$ReceiverLog.err" `
        -PassThru

    Start-Sleep -Seconds 3
    $listening = Test-PortListening $listenPort
    if ($listening) {
        Write-Output "started pid $($proc.Id) on port $listenPort, storage $StorageDir"
        Write-Output "review page: http://127.0.0.1:$listenPort/"
        Write-Output "health check: http://127.0.0.1:$listenPort/healthz"
    } else {
        Write-Output "FAILED to listen on port $listenPort. See $ReceiverLog.err"
        if (Test-Path -LiteralPath "$ReceiverLog.err") {
            Get-Content -LiteralPath "$ReceiverLog.err" -Tail 20
        }
        exit 1
    }
}

function Start-Transcriber {
    if (-not (Test-Path -LiteralPath $KeyFile)) {
        Write-Output "missing key file: $KeyFile"
        exit 1
    }

    $python = Resolve-Python 'sys'

    New-Item -ItemType Directory -Force -Path $StorageDir | Out-Null
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

    $env:OPENROUTER_API_KEY = [System.IO.File]::ReadAllText($KeyFile).Trim()
    $env:KVB_STORAGE_DIR = $StorageDir

    $running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*transcribe_worker.py*' }
    if ($running) {
        $pids = ($running | ForEach-Object { $_.ProcessId }) -join ','
        Write-Output "already running (pid $pids)"
        return
    }

    $arguments = @('tools\transcribe_worker.py', '--interval', '5')

    $proc = Start-Process -FilePath $python `
        -ArgumentList $arguments `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $TranscribeLog `
        -RedirectStandardError "$TranscribeLog.err" `
        -PassThru

    Write-Output "started pid $($proc.Id), log $TranscribeLog"
}

function Invoke-Deploy {
    if (-not (Test-Path -LiteralPath $KindleRoot)) {
        throw "Kindle root not found: $KindleRoot. Connect the USB cable and pass -KindleRoot."
    }

    $rootPath = (Resolve-Path -LiteralPath $KindleRoot).Path
    if ($rootPath.TrimEnd('\') -eq $ProjectRoot.TrimEnd('\')) {
        throw "The Kindle root is the project directory. Pass the USB mount point."
    }

    $deviceDir = Join-Path $rootPath 'kindle-voice-bridge'
    New-Item -ItemType Directory -Force -Path $deviceDir | Out-Null

    $files = @(
        'capture_local.py',
        'bt_session.py',
        'pw4_supervisor.py',
        'uploader.py',
        'hci_relay.py',
        'launch_hci.py'
    )

    foreach ($name in $files) {
        $source = Join-Path $ProjectRoot (Join-Path 'kindle\voice_bridge' $name)
        Copy-Item -LiteralPath $source -Destination (Join-Path $deviceDir $name) -Force
        Write-Output "copied $name"
    }

    $pluginSource = Join-Path $ProjectRoot 'koreader\voicebridge.koplugin'
    $pluginTarget = Join-Path $rootPath 'koreader\plugins\voicebridge.koplugin'
    New-Item -ItemType Directory -Force -Path $pluginTarget | Out-Null
    Copy-Item -Path (Join-Path $pluginSource '*') -Destination $pluginTarget -Recurse -Force
    Write-Output "copied the KOReader plugin to $pluginTarget"

    $uploadPath = Join-Path $deviceDir 'upload.json'
    if ($ServerUrl) {
        if ((Test-Path -LiteralPath $uploadPath) -and (-not $Force)) {
            Write-Output 'kept the existing upload.json. Use -Force to replace it.'
        } else {
            $config = Get-ReceiverConfig
            if ($null -eq $config) {
                $config = New-ReceiverConfig
            }
            $payload = [ordered]@{
                server_url = $ServerUrl
                token = $config.Token
            }
            [System.IO.File]::WriteAllText($uploadPath, ($payload | ConvertTo-Json))
            Write-Output 'wrote upload.json with the local token (token not shown)'
        }
    } elseif (Test-Path -LiteralPath $uploadPath) {
        Write-Output 'upload.json is present'
    } else {
        Write-Output 'upload.json is missing. Run deploy with -ServerUrl http://<laptop-ip>:<port> to write it.'
    }

    Write-Output 'the Python runtime and the Bumble bundle are not copied. They stay at /mnt/us/kindle-voice-bridge/.'
    Write-WarningLine 'the device files perform a Bluetooth handoff. The handoff stops the stock Bluetooth daemon and can leave Bluetooth unusable. Recovery is not guaranteed.'
}

function Invoke-Tests {
    $python = Resolve-Python 'pytest, httpx, fastapi'
    Push-Location $ProjectRoot
    try {
        & $python -m pytest -q
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    exit $code
}

if ($Help) {
    Show-Usage
    exit 0
}

switch ($Command.ToLowerInvariant()) {
    'receiver' { Start-Receiver }
    'transcriber' { Start-Transcriber }
    'deploy' { Invoke-Deploy }
    'test' { Invoke-Tests }
    'help' { Show-Usage }
    default {
        Write-Output "unknown command: $Command"
        Write-Output ''
        Show-Usage
        exit 2
    }
}
