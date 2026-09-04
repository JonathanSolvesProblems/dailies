# Deploy to Cloud Run, then prove the deployed URL actually changed.
#
# This script exists because "committed" and "deployed" drifted apart three separate times
# in this project, and each time the gap was invisible from the code. Once the live feature
# sat in the repo for four commits while the hosted URL returned 404 for it. A green build
# log is not evidence; the only evidence is fetching the running URL and finding the change.
#
#   .\deploy.ps1

# Deliberately NOT "Stop". gcloud writes its normal build progress to stderr, and with
# ErrorActionPreference=Stop PowerShell turns that into a terminating error and kills the
# deploy mid-flight. Native exit codes are checked explicitly instead, which is the only
# thing that actually indicates failure here.
$ErrorActionPreference = "Continue"
$SERVICE = "dailies"
$REGION  = "us-east1"
$PROJECT = "warden-agent-supervisor"
$URL     = "https://dailies-564641829203.us-east1.run.app"

# A short, unique string from the newest change. If it is not on the live URL afterwards,
# the deploy did not take effect no matter what the build said.
#
# Update this whenever you ship something whose absence would be invisible. A marker left
# pointing at an old change still passes while the new one is missing, which is the exact
# failure this script exists to catch.
$MARKER = "odd-state"
$MARKER_PATH = "/"

Write-Host "`n=== uncommitted work? ===" -ForegroundColor Cyan
$dirty = git status --porcelain
if ($dirty) {
  Write-Host $dirty
  Write-Host "Refusing to deploy: commit first, or the deployed build will not match the repo." -ForegroundColor Red
  exit 1
}
Write-Host "  clean at $(git rev-parse --short HEAD)"

Write-Host "`n=== deploying ===" -ForegroundColor Cyan
$env:CLOUDSDK_CORE_DISABLE_PROMPTS = "1"

# NO `2>&1` on gcloud, and the exit code is read on the very next line.
#
# This script twice lied about what happened, in opposite directions, and both faults came
# from the same place.
#
# It originally piped `gcloud ... 2>&1 | Select-Object -Last 3`. In Windows PowerShell 5.1,
# redirecting a NATIVE command's stderr wraps each line in an ErrorRecord (NativeCommandError)
# rather than passing text through, and gcloud writes all of its ordinary progress to stderr.
# The result was a deploy that reported "gcloud exited 1. Deploy did not complete." while the
# revision had in fact gone live and was serving 100 percent of traffic. Before that, the same
# `-Last 3` threw away a genuine build error and left only gcloud's generic
# "run --run-diagnostics" footer.
#
# So: let stderr go to the console untouched, keep stdout for the log, and read $LASTEXITCODE
# immediately, because any cmdlet in between can overwrite it.
$log = Join-Path $env:TEMP "dailies-deploy.log"
gcloud run deploy $SERVICE --source . --region $REGION --project $PROJECT `
  --allow-unauthenticated --memory 1Gi --cpu 2 --timeout 300 `
  --min-instances 1 --max-instances 3 | Tee-Object -FilePath $log
$deployExit = $LASTEXITCODE

if ($deployExit -ne 0) {
  Write-Host "`n  gcloud exited $deployExit. Deploy did not complete." -ForegroundColor Red
  if (Test-Path $log) {
    Write-Host "  Last 40 lines of stdout:" -ForegroundColor Red
    Get-Content $log -Tail 40 | ForEach-Object { Write-Host "    $_" }
    Write-Host "`n  Full log: $log" -ForegroundColor Red
  }
  Write-Host "  gcloud's own progress and errors are on the console above." -ForegroundColor Red
  exit 1
}

Write-Host "`n=== verifying the RUNNING url, not the build log ===" -ForegroundColor Cyan
foreach ($p in @("/", "/live", "/api/health", "/api/capabilities")) {
  $code = (Invoke-WebRequest -Uri "$URL$p" -UseBasicParsing -TimeoutSec 90).StatusCode
  Write-Host ("  {0,-20} {1}" -f $p, $code)
}

$body = (Invoke-WebRequest -Uri "$URL$MARKER_PATH" -UseBasicParsing -TimeoutSec 90).Content
if ($body -match [regex]::Escape($MARKER)) {
  Write-Host "`n  marker present: the deployed page is current." -ForegroundColor Green
} else {
  Write-Host "`n  MARKER MISSING: '$MARKER' is not on $URL$MARKER_PATH" -ForegroundColor Red
  Write-Host "  The build may have succeeded while serving an older revision." -ForegroundColor Red
  exit 1
}

Write-Host "`n  $URL`n"
