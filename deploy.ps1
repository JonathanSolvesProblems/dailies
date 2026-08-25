# Deploy to Cloud Run, then prove the deployed URL actually changed.
#
# This script exists because "committed" and "deployed" drifted apart three separate times
# in this project, and each time the gap was invisible from the code. Once the live feature
# sat in the repo for four commits while the hosted URL returned 404 for it. A green build
# log is not evidence; the only evidence is fetching the running URL and finding the change.
#
#   .\deploy.ps1

$ErrorActionPreference = "Stop"
$SERVICE = "dailies"
$REGION  = "us-east1"
$PROJECT = "warden-agent-supervisor"
$URL     = "https://dailies-564641829203.us-east1.run.app"

# A short, unique string from the newest change. If it is not on the live URL afterwards,
# the deploy did not take effect no matter what the build said.
$MARKER = "minmax(0, 1fr)"
$MARKER_PATH = "/live"

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
gcloud run deploy $SERVICE --source . --region $REGION --project $PROJECT `
  --allow-unauthenticated --memory 1Gi --cpu 2 --timeout 300 `
  --min-instances 1 --max-instances 3 2>&1 | Select-Object -Last 3

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
