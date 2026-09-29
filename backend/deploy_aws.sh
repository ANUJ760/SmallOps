#!/bin/bash
set -e

REGION="ap-south-1"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REPO_NAME="smallops-backend"
IMAGE_TAG="latest"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPO_NAME}:${IMAGE_TAG}"

echo "🚀 Deploying SmallOps Backend to AWS..."

# 1. Authenticate with ECR
echo "🔑 Logging into Amazon ECR..."
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com

# 2. Create ECR repository if it doesn't exist
echo "📦 Checking ECR repository..."
aws ecr describe-repositories --repository-names ${REPO_NAME} --region $REGION || aws ecr create-repository --repository-name ${REPO_NAME} --region $REGION

# 3. Build and Push Docker image
echo "🔨 Building Docker image..."
docker build --provenance=false --platform linux/amd64 -t ${REPO_NAME}:${IMAGE_TAG} .
docker tag ${REPO_NAME}:${IMAGE_TAG} ${ECR_URI}

echo "⬆️ Pushing Docker image to ECR..."
docker push ${ECR_URI}

echo "✅ Image pushed successfully to: ${ECR_URI}"
echo ""
echo "🎉 DEPLOYMENT NEXT STEPS:"
echo "To serve this container, go to the AWS App Runner console:"
echo "1. Click 'Create an App Runner service'"
echo "2. Select 'Container registry' -> 'Amazon ECR'"
echo "3. Choose the '${REPO_NAME}' image."
echo "4. Under Environment variables, add your BEDROCK_MODEL_ID, AWS_ACCESS_KEY_ID, and other .env keys."
echo "5. Set Port to 8000 and click Deploy!"
