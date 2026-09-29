# deploy_frontend_s3.ps1
# Builds and deploys the SmallOps React frontend to AWS S3 Static Website Hosting.

[CmdletBinding()]
param(
    [string]$Region = "ap-south-1"
)

$ErrorActionPreference = "Stop"

Write-Host "=========================================" -ForegroundColor Cyan
Write-Host " SmallOps Frontend S3 Deployment" -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan

# 1. Check AWS Account
$callerIdentity = aws sts get-caller-identity --output json | ConvertFrom-Json
$accountId = $callerIdentity.Account
$bucketName = "smallops-frontend-$accountId"
Write-Host "  -> AWS Account: $accountId" -ForegroundColor Green
Write-Host "  -> Target S3 Bucket: $bucketName" -ForegroundColor Green

# 2. Get Live Backend URL from CloudFormation or .env
$apiUrl = aws cloudformation describe-stacks `
    --stack-name deployed-backend `
    --region $Region `
    --query "Stacks[0].Outputs[?OutputKey=='BackendApiUrl'].OutputValue" `
    --output text

if (-not $apiUrl -or $apiUrl -eq "None") {
    $apiUrl = "https://wawdoi6o2gmixk4uwy3aidzfle0lwnya.lambda-url.ap-south-1.on.aws"
}
$apiUrl = $apiUrl.TrimEnd('/')
Write-Host "  -> Backend API URL: $apiUrl" -ForegroundColor Green

# 3. Synchronize frontend/.env
$rootDir = $PSScriptRoot
$frontendDir = Join-Path $rootDir "frontend"
$frontendEnvPath = Join-Path $frontendDir ".env"
$rootEnvPath = Join-Path $rootDir ".env"

$userPoolId = "ap-south-1_txppSeJhO"
$clientId = "7eqigcajenecjughu2hg0dgn4q"

if (Test-Path $rootEnvPath) {
    Get-Content $rootEnvPath | ForEach-Object {
        $line = $_.Trim()
        if ($line.StartsWith("COGNITO_USER_POOL_ID=")) { $userPoolId = $line.Split("=")[1].Trim() }
        if ($line.StartsWith("COGNITO_APP_CLIENT_ID=")) { $clientId = $line.Split("=")[1].Trim() }
    }
}

$frontendEnvContent = @"
VITE_API_BASE_URL=$apiUrl
VITE_COGNITO_USER_POOL_ID=$userPoolId
VITE_COGNITO_APP_CLIENT_ID=$clientId
"@

Set-Content -Path $frontendEnvPath -Value $frontendEnvContent -Encoding UTF8
Write-Host "  -> Synchronized frontend/.env with live backend URL and Cognito IDs." -ForegroundColor Green

# 4. Build Frontend Bundle
Write-Host "`nBuilding React frontend..." -ForegroundColor Yellow
Set-Location $frontendDir
npm run build
if ($LASTEXITCODE -ne 0) {
    Write-Error "Frontend build failed."
    exit 1
}

# 5. Upload to S3
Write-Host "`nSyncing build artifacts to s3://$bucketName/..." -ForegroundColor Yellow
aws s3 sync dist/ "s3://$bucketName/" --delete --region $Region
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to sync to S3."
    exit 1
}

$websiteUrl = "http://$bucketName.s3-website.$Region.amazonaws.com"
Write-Host "`n=========================================" -ForegroundColor Green
Write-Host "FRONTEND DEPLOYMENT COMPLETE!" -ForegroundColor Green
Write-Host "=========================================" -ForegroundColor Green
Write-Host "Live App URL: $websiteUrl" -ForegroundColor Cyan
