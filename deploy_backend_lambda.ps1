# deploy_backend_lambda.ps1
# Deploys SmallOps backend FastAPI application to AWS Lambda via Amazon ECR.

[CmdletBinding()]
param(
    [string]$Region = "ap-south-1",
    [string]$FunctionName = "deployed-backend-SmallOpsBackendFunction-8eszVaco3RkA",
    [string]$RepoName = "deployedbackendde08ec99/smallopsbackendfunction7579f802repo",
    [string]$Tag = "v$(Get-Date -Format 'yyyyMMdd-HHmmss')"
)

$ErrorActionPreference = "Continue"

Write-Host "=========================================" -ForegroundColor Cyan
Write-Host " SmallOps Backend AWS Lambda Deployment" -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan

# 1. Check AWS Credentials
Write-Host "`n[1/6] Checking AWS credentials..." -ForegroundColor Yellow
try {
    $callerIdentity = aws sts get-caller-identity --output json | ConvertFrom-Json
    $accountId = $callerIdentity.Account
    Write-Host "  -> Authenticated as AWS Account: $accountId (User: $($callerIdentity.Arn))" -ForegroundColor Green
} catch {
    Write-Error "AWS CLI is not authenticated or not found. Please verify your AWS credentials."
    exit 1
}

# 2. Check Docker Daemon
Write-Host "`n[2/6] Checking Docker status..." -ForegroundColor Yellow
$dockerWorking = $false
try {
    $dockerVersion = & docker version --format '{{.Server.Version}}' 2>$null
    if ($dockerVersion -and $LASTEXITCODE -eq 0) {
        $dockerWorking = $true
        Write-Host "  -> Docker daemon is running (Server version: $dockerVersion)." -ForegroundColor Green
    }
} catch {
    # ignore
}

if (-not $dockerWorking) {
    Write-Host "  Docker daemon is not running or not responding!" -ForegroundColor Red
    Write-Host "  Please open Docker Desktop and wait until it finishes starting." -ForegroundColor Yellow
    exit 1
}

# 3. Log into Amazon ECR
Write-Host "`n[3/6] Logging into Amazon ECR ($Region)..." -ForegroundColor Yellow
$ecrRegistry = "$accountId.dkr.ecr.$Region.amazonaws.com"
$ecrImageUri = "$ecrRegistry/${RepoName}:$Tag"
$ecrLatestUri = "$ecrRegistry/${RepoName}:latest"

$loginPassword = aws ecr get-login-password --region $Region
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to retrieve ECR login password."
    exit 1
}
$loginPassword | docker login --username AWS --password-stdin $ecrRegistry
if ($LASTEXITCODE -ne 0) {
    Write-Error "Docker login to ECR failed."
    exit 1
}
Write-Host "  -> Successfully logged into ECR." -ForegroundColor Green

# 4. Build and Tag Docker Image
Write-Host "`n[4/6] Building Docker image (linux/amd64)..." -ForegroundColor Yellow
Write-Host "  Target Image: $ecrImageUri" -ForegroundColor Gray

# Ensure we run from workspace root
$rootDir = $PSScriptRoot
Set-Location $rootDir

docker build --provenance=false --platform linux/amd64 -t "smallops-backend:$Tag" -t "smallops-backend:latest" -f backend/Dockerfile backend
if ($LASTEXITCODE -ne 0) {
    Write-Error "Docker build failed."
    exit 1
}

docker tag "smallops-backend:$Tag" $ecrImageUri
docker tag "smallops-backend:latest" $ecrLatestUri

Write-Host "  Pushing image to ECR..." -ForegroundColor Yellow
docker push $ecrImageUri
docker push $ecrLatestUri
if ($LASTEXITCODE -ne 0) {
    Write-Error "Docker push to ECR failed."
    exit 1
}
Write-Host "  -> Pushed $ecrImageUri successfully." -ForegroundColor Green

# 5. Update AWS Lambda Function Code
Write-Host "`n[5/6] Updating AWS Lambda Function Code..." -ForegroundColor Yellow
Write-Host "  Function: $FunctionName" -ForegroundColor Gray

aws lambda update-function-code `
    --function-name $FunctionName `
    --image-uri $ecrImageUri `
    --region $Region | Out-Null

if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to update Lambda function code."
    exit 1
}

Write-Host "  Waiting for Lambda code update to complete..." -ForegroundColor Yellow
aws lambda wait function-updated --function-name $FunctionName --region $Region
Write-Host "  -> Lambda function code updated." -ForegroundColor Green

# 6. Update Environment Variables from .env
Write-Host "`n[6/6] Synchronizing Environment Variables from .env..." -ForegroundColor Yellow
$envPath = Join-Path $rootDir ".env"
$envVars = @{}

if (Test-Path $envPath) {
    Get-Content $envPath | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $parts = $line.Split("=", 2)
            $key = $parts[0].Trim()
            $val = $parts[1].Trim()
            $reservedKeys = @("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
            if ($reservedKeys -notcontains $key) {
                $envVars[$key] = $val
            }
        }
    }
}

# Ensure PORT is 8000 for Lambda Web Adapter
$envVars["PORT"] = "8000"

$tempEnvFile = Join-Path $rootDir "scratch_lambda_env.json"
try {
    $envObj = @{ Variables = $envVars }
    $jsonContent = $envObj | ConvertTo-Json -Depth 5
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($tempEnvFile, $jsonContent, $utf8NoBom)
    
    aws lambda update-function-configuration `
        --function-name $FunctionName `
        --environment "file://$tempEnvFile" `
        --region $Region | Out-Null

    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Could not update environment variables via CLI."
    } else {
        Write-Host "  Waiting for configuration update to complete..." -ForegroundColor Yellow
        aws lambda wait function-updated --function-name $FunctionName --region $Region
        Write-Host "  -> Environment variables synchronized successfully." -ForegroundColor Green
    }
} finally {
    if (Test-Path $tempEnvFile) {
        Remove-Item -Force $tempEnvFile
    }
}

# 7. Verification & Health Check
Write-Host "`n=========================================" -ForegroundColor Green
Write-Host "DEPLOYMENT FINISHED!" -ForegroundColor Green
Write-Host "=========================================" -ForegroundColor Green
try {
    $apiUrl = aws cloudformation describe-stacks `
        --stack-name deployed-backend `
        --region $Region `
        --query "Stacks[0].Outputs[?OutputKey=='BackendApiUrl'].OutputValue" `
        --output text
    
    if ($apiUrl) {
        Write-Host "Backend API URL: $apiUrl" -ForegroundColor Cyan
        Write-Host "Testing /health endpoint..." -ForegroundColor Yellow
        Start-Sleep -Seconds 3
        $healthUrl = "$($apiUrl.TrimEnd('/'))/health"
        $healthResponse = curl.exe -s -m 15 $healthUrl
        Write-Host "Response: $healthResponse" -ForegroundColor Green
    }
} catch {
    Write-Host "Live endpoint verification skipped." -ForegroundColor Gray
}

Write-Host "`nSmallOps Backend is live!" -ForegroundColor Green
