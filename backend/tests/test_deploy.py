"""Tests for Lambda deployer — mocked via moto and unittest.mock."""

from __future__ import annotations

import io
import json
import zipfile
from unittest.mock import MagicMock, patch

import boto3
import pytest

from backend.deploy.lambda_deployer import _package_code, deploy_to_lambda

TEST_REGION = "ap-south-1"
TEST_FUNCTION = "test-bharatbuilds-deploy"

SAMPLE_CODE = """\
def render():
    return "<h1>Hello from BharatBuilds!</h1>"
"""


class TestPackageCode:
    """Test the code packaging logic."""

    def test_creates_valid_zip(self):
        zip_bytes = _package_code(SAMPLE_CODE)
        buf = io.BytesIO(zip_bytes)
        with zipfile.ZipFile(buf, "r") as zf:
            names = zf.namelist()
            assert "index.html" in names
            assert "lambda_function.py" in names
            # Verify the app code content
            assert zf.read("index.html").decode() == SAMPLE_CODE

    def test_handler_is_valid_python(self):
        zip_bytes = _package_code(SAMPLE_CODE)
        buf = io.BytesIO(zip_bytes)
        with zipfile.ZipFile(buf, "r") as zf:
            handler_code = zf.read("lambda_function.py").decode()
            # Should compile without errors
            compile(handler_code, "lambda_function.py", "exec")


class TestDeployToLambda:
    """deploy_to_lambda tests with mocked Lambda client."""

    @pytest.mark.asyncio
    @patch("backend.deploy.lambda_deployer._get_client")
    async def test_deploy_success(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        # Mock update_function_code
        mock_client.update_function_code.return_value = {
            "FunctionArn": f"arn:aws:lambda:{TEST_REGION}:123456789012:function:{TEST_FUNCTION}",
        }

        # Mock get_function_url_config
        mock_client.get_function_url_config.return_value = {
            "FunctionUrl": f"https://{TEST_FUNCTION}.lambda-url.{TEST_REGION}.on.aws/",
        }

        url = await deploy_to_lambda(
            "app-1",
            SAMPLE_CODE,
            function_name=TEST_FUNCTION,
            region=TEST_REGION,
        )

        assert "lambda-url" in url
        mock_client.update_function_code.assert_called_once()
        call_kwargs = mock_client.update_function_code.call_args[1]
        assert call_kwargs["FunctionName"] == TEST_FUNCTION
        assert call_kwargs["Publish"] is True
        assert isinstance(call_kwargs["ZipFile"], bytes)

    @pytest.mark.asyncio
    @patch("backend.deploy.lambda_deployer._get_client")
    async def test_deploy_creates_url_if_missing(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mock_client.update_function_code.return_value = {}

        # Simulate ResourceNotFoundException on get_function_url_config
        mock_client.exceptions.ResourceNotFoundException = type(
            "ResourceNotFoundException", (Exception,), {}
        )
        mock_client.get_function_url_config.side_effect = (
            mock_client.exceptions.ResourceNotFoundException()
        )
        mock_client.create_function_url_config.return_value = {
            "FunctionUrl": f"https://{TEST_FUNCTION}.lambda-url.{TEST_REGION}.on.aws/",
        }

        url = await deploy_to_lambda(
            "app-2",
            SAMPLE_CODE,
            function_name=TEST_FUNCTION,
            region=TEST_REGION,
        )

        assert "lambda-url" in url
        mock_client.create_function_url_config.assert_called_once()
