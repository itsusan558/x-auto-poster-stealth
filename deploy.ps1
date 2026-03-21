# X Auto Poster - Deploy Script
# First time: .\deploy.ps1 -FirstDeploy
# Update:     .\deploy.ps1

param(
    [switch]$FirstDeploy
)

$PROJECT_ID     = "alien-craft-490615-v5"
$REGION         = "asia-northeast1"
$SERVICE_NAME   = "x-auto-poster"
$IMAGE          = "gcr.io/$PROJECT_ID/$SERVICE_NAME"
$GCS_BUCKET     = "x-auto-poster-$PROJECT_ID"
$SCHEDULER_JOB  = "x-auto-poster-job"
$SA_NAME        = "x-auto-poster"
$SA_EMAIL       = "$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"

$X_USERNAME = $env:X_USERNAME
$X_PASSWORD = $env:X_PASSWORD

if (-not $X_USERNAME -or -not $X_PASSWORD) {
    Write-Host "Set X_USERNAME and X_PASSWORD in your environment before deploy." -ForegroundColor Red
    exit 1
}

Write-Host "=== X Auto Poster Deploy ===" -ForegroundColor Cyan

if ($FirstDeploy) {
    Write-Host "[1/6] Creating GCS bucket..." -ForegroundColor Yellow
    gcloud storage buckets create "gs://$GCS_BUCKET" `
        --project=$PROJECT_ID `
        --location=$REGION `
        --uniform-bucket-level-access 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  Bucket already exists, skipping." -ForegroundColor Gray
    }
}

if ($FirstDeploy) {
    Write-Host "[2/6] Creating service account..." -ForegroundColor Yellow
    gcloud iam service-accounts create $SA_NAME `
        --project=$PROJECT_ID `
        --display-name="X Auto Poster" 2>$null

    gcloud storage buckets add-iam-policy-binding "gs://$GCS_BUCKET" `
        --member="serviceAccount:$SA_EMAIL" `
        --role="roles/storage.objectAdmin"

    gcloud projects add-iam-policy-binding $PROJECT_ID `
        --member="serviceAccount:$SA_EMAIL" `
        --role="roles/cloudscheduler.admin"
}

Write-Host "[3/6] Building Docker image..." -ForegroundColor Yellow
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir
gcloud builds submit --tag $IMAGE --project=$PROJECT_ID
if ($LASTEXITCODE -ne 0) {
    Write-Host "Build failed." -ForegroundColor Red
    exit 1
}

Write-Host "[4/6] Deploying to Cloud Run..." -ForegroundColor Yellow
gcloud run deploy $SERVICE_NAME `
    --image=$IMAGE `
    --project=$PROJECT_ID `
    --region=$REGION `
    --platform=managed `
    --memory=2Gi `
    --cpu=1 `
    --timeout=120 `
    --min-instances=0 `
    --max-instances=1 `
    --service-account=$SA_EMAIL `
    --allow-unauthenticated `
    --set-env-vars="GCS_BUCKET=$GCS_BUCKET,GCP_PROJECT=$PROJECT_ID,SCHEDULER_JOB_NAME=$SCHEDULER_JOB,SCHEDULER_LOCATION=$REGION,X_USERNAME=$X_USERNAME,X_PASSWORD=$X_PASSWORD"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Deploy failed." -ForegroundColor Red
    exit 1
}

Write-Host "[5/6] Setting SERVICE_URL..." -ForegroundColor Yellow
$SERVICE_URL = gcloud run services describe $SERVICE_NAME `
    --project=$PROJECT_ID `
    --region=$REGION `
    --format="value(status.url)"

gcloud run services update $SERVICE_NAME `
    --project=$PROJECT_ID `
    --region=$REGION `
    --update-env-vars="SERVICE_URL=$SERVICE_URL"

Write-Host "[6/6] Setting up Cloud Scheduler (daily 7:00 JST)..." -ForegroundColor Yellow
if ($FirstDeploy) {
    gcloud scheduler jobs create http $SCHEDULER_JOB `
        --project=$PROJECT_ID `
        --location=$REGION `
        --schedule="0 7 * * *" `
        --time-zone="Asia/Tokyo" `
        --uri="$SERVICE_URL/post" `
        --http-method=POST `
        --oidc-service-account-email=$SA_EMAIL `
        --oidc-token-audience=$SERVICE_URL 2>$null

    if ($LASTEXITCODE -ne 0) {
        gcloud scheduler jobs update http $SCHEDULER_JOB `
            --project=$PROJECT_ID `
            --location=$REGION `
            --schedule="0 7 * * *" `
            --time-zone="Asia/Tokyo" `
            --uri="$SERVICE_URL/post" `
            --http-method=POST `
            --oidc-service-account-email=$SA_EMAIL `
            --oidc-token-audience=$SERVICE_URL
    }
}

Write-Host ""
Write-Host "=== Deploy Complete ===" -ForegroundColor Green
Write-Host "URL: $SERVICE_URL" -ForegroundColor Cyan
Write-Host "1. Open the URL above to configure settings"
Write-Host "2. Click '今すぐ投稿' to test"
Write-Host "3. Schedule and content can be changed from the UI"
