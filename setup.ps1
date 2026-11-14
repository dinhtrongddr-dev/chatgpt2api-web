# ChatGPT2API (Web Edition) - one-command setup for a new Windows machine.
#   powershell -ExecutionPolicy Bypass -File setup.ps1
# Checks Python (>= 3.13, or the bundled runtime\python), creates .venv, installs or repairs
# the required packages, and creates .env with a random local auth key (never hard-coded).
param([switch]$Force)
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
Set-Location $Root

function Find-Python {
    $bundled = Join-Path $Root 'runtime\python\python.exe'
    if (Test-Path $bundled) { return $bundled }
    foreach ($cmd in @(@('py', '-3.13'), @('py', '-3'), @('python'))) {
        try {
            $exe = $cmd[0]; $pre = @($cmd | Select-Object -Skip 1)
            $ver = & $exe @pre -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
            if ($ver -and [version]$ver -ge [version]'3.13') {
                return (& $exe @pre -c "import sys;print(sys.executable)")
            }
        } catch {}
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Host 'Python 3.13+ chua co. Dang cai bang winget...' -ForegroundColor Yellow
    winget install -e --id Python.Python.3.13 --accept-package-agreements --accept-source-agreements
    $py = Find-Python
    if (-not $py) { throw 'Khong tim thay Python 3.13 sau khi cai. Mo lai PowerShell roi chay lai setup.ps1.' }
}
Write-Host "Python: $py"

$venvPy = Join-Path $Root '.venv\Scripts\python.exe'
$bundled = Join-Path $Root 'runtime\python\python.exe'
if ($py -eq $bundled) {
    $target = $bundled
} else {
    if ($Force -or -not (Test-Path $venvPy)) {
        & $py -m venv (Join-Path $Root '.venv')
    }
    $target = $venvPy
}

# Install only what is missing or broken (pip check repairs version conflicts).
& $target -m pip install --upgrade pip --quiet
& $target -m pip install -r (Join-Path $Root 'requirements.txt') --quiet
& $target -m pip check
if ($LASTEXITCODE -ne 0) {
    & $target -m pip install -r (Join-Path $Root 'requirements.txt') --force-reinstall --quiet
}
& $target -c "import fastapi, uvicorn, curl_cffi, sqlalchemy, tiktoken, PIL; print('Thu vien: OK')"

$envFile = Join-Path $Root '.env'
if (-not (Test-Path $envFile)) {
    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $key = 'sk-' + ([Convert]::ToBase64String($bytes) -replace '[+/=]', 'x')
    @(
        "CHATGPT2API_AUTH_KEY=$key",
        'CHATGPT2API_PORT=13000',
        'CHATGPT2API_HOST=127.0.0.1',
        'TZ=Asia/Ho_Chi_Minh'
    ) | Set-Content -Encoding UTF8 $envFile
    Write-Host 'Da tao .env voi auth key ngau nhien (127.0.0.1:13000).'
} else {
    Write-Host '.env da co: giu nguyen.'
}
Write-Host ''
Write-Host 'Xong. Chay start.cmd de mo server + icon khay. Dang nhap tai khoan ChatGPT trong giao dien web.' -ForegroundColor Green
