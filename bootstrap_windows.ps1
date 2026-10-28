$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($Root)) { throw 'Cannot determine the project directory.' }
$Venv = Join-Path $Root '.venv'
$Python = Join-Path $Venv 'Scripts\python.exe'
$Logs = Join-Path $Root 'logs'
$Dataset = Join-Path $Root 'data\issues_demo.json'
Set-Location $Root
New-Item -ItemType Directory -Force -Path $Logs | Out-Null

foreach ($Required in @($Dataset, (Join-Path $Root 'requirements.txt'), (Join-Path $Root 'issuepilot\api.py'))) {
    if (-not (Test-Path -LiteralPath $Required)) { throw "Missing prerequisite: $Required" }
}
Write-Host "`n===== 1. LOCATE UV =====" -ForegroundColor Cyan
$Candidates = @(
    (Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
    (Join-Path $env:APPDATA 'Python\Scripts\uv.exe')
)
$Uv = $null
foreach ($Item in $Candidates) { if (Test-Path $Item) { $Uv=$Item; break } }
if (-not $Uv) {
    $Found=Get-Command uv -ErrorAction SilentlyContinue
    if ($Found) { $Uv=$Found.Source }
}
if (-not $Uv) { throw 'uv executable not found; install uv or create the project .venv manually.' }
& $Uv --version
if ($LASTEXITCODE -ne 0) {throw 'uv failed to run'}

Write-Host "`n===== 2. CREATE INDEPENDENT ENV =====" -ForegroundColor Cyan
if (-not (Test-Path $Python)) {
    if (Test-Path $Venv) {throw 'Found incomplete .venv; not overwriting'}
    $BasePython = $env:ISSUEPILOT_BOOTSTRAP_PYTHON
    if ([string]::IsNullOrWhiteSpace($BasePython)) {
        $FoundPython = Get-Command python -ErrorAction SilentlyContinue
        if ($FoundPython) { $BasePython = $FoundPython.Source }
    }
    if ([string]::IsNullOrWhiteSpace($BasePython) -or -not (Test-Path -LiteralPath $BasePython)) {
        throw 'Python was not found. Set ISSUEPILOT_BOOTSTRAP_PYTHON to an existing python.exe.'
    }
    & $Uv venv --python $BasePython $Venv
    if ($LASTEXITCODE -ne 0) {throw 'uv venv failed'}
}
& $Uv pip install --python $Python -r (Join-Path $Root 'requirements.txt')
if ($LASTEXITCODE -ne 0) {throw 'Dependency installation failed'}
& $Uv pip check --python $Python
if ($LASTEXITCODE -ne 0) {throw 'Dependency consistency failed'}
Write-Host 'INDEPENDENT_ENV=PASS' -ForegroundColor Green

Write-Host "`n===== 3. CONFIGURE LOCAL SECRETS =====" -ForegroundColor Cyan
$EnvFile=Join-Path $Root '.env'
if (-not (Test-Path $EnvFile)) {
    $bytes=New-Object byte[] 32
    $rng=[Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $rng.Dispose()
    $secret=[Convert]::ToBase64String($bytes)
    Write-Host 'API auth secret generated locally. It will not be displayed.'
    Write-Host 'Paste Bailian / Qwen API key if you want to test real triage later.'
    $s=Read-Host 'Qwen API key (press Enter to skip)' -AsSecureString
    $Bstr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { $Key=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($Bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Bstr) }
    if ([string]::IsNullOrWhiteSpace($Key)) { $Key='replace-with-your-api-key' }
    if ($Key -match "[`r`n]") { throw 'Invalid API key line break' }
    $EnvContent="ISSUEPILOT_AUTH_SECRET=$secret`nQWEN_API_KEY=$Key`nQWEN_MODEL=qwen3.8-flash`nQWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1`n"
    [IO.File]::WriteAllText($EnvFile, $EnvContent, (New-Object Text.UTF8Encoding($false)))
    Remove-Variable Key -ErrorAction SilentlyContinue
    Write-Host 'LOCAL_ENV=CREATED'
} else {
    Write-Host 'LOCAL_ENV=PRESERVED'
}

Write-Host "`n===== 4. RUN HTTP + DB TESTS (NO LLM COST) =====" -ForegroundColor Cyan
& $Python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) {throw 'Contract tests failed'}
Write-Host 'BACKEND_TESTS=PASS' -ForegroundColor Green

Write-Host "`n===== 5. START API =====" -ForegroundColor Cyan
$Existing=@(Get-NetTCPConnection -LocalPort 8790 -State Listen -ErrorAction SilentlyContinue)
if ($Existing.Count -gt 0) {throw 'Port 8790 already in use; will not stop an unknown process'}
$Stdout=Join-Path $Logs 'api.stdout.log'
$Stderr=Join-Path $Logs 'api.stderr.log'
$Process=Start-Process -FilePath $Python -ArgumentList @('-m','uvicorn','issuepilot.api:app','--host','127.0.0.1','--port','8790') -WorkingDirectory $Root -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr -PassThru
$Ready=$false
for ($i=0; $i -lt 35; $i++) {
    try {
        $Health=Invoke-RestMethod -Uri 'http://127.0.0.1:8790/api/health' -TimeoutSec 3
        if ($Health.status -eq 'ok') { $Ready=$true; break }
    } catch {}
    $Process.Refresh()
    if ($Process.HasExited) {break}
    Start-Sleep -Seconds 2
}
if (-not $Ready) {
    Get-Content -LiteralPath $Stderr -Tail 40
    throw 'FastAPI did not become healthy'
}

Write-Host "`n===== FINAL STATUS =====" -ForegroundColor Green
Write-Host 'ISSUEPILOT_BACKEND=PASS'
Write-Host 'INDEPENDENT_ENV=PASS'
Write-Host 'BACKEND_TESTS=PASS'
Write-Host 'API_READY=True'
Write-Host 'GITHUB_WRITE_OPERATIONS=0'
Write-Host 'LLM_API_CALLS=0'
Write-Host 'API_DOCS=http://127.0.0.1:8790/docs'
Write-Host "DATASET=$Dataset"
Write-Host 'The API is running. Start the Worker separately only after reviewing .env and the README safety notes.'
