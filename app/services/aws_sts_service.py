"""AWS STS token exchange service for WebSockets SigV4 IoT connectivity."""

import json
import logging
import re
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException, status

from app.core.config import Settings, settings

logger = logging.getLogger(__name__)


class AWSSTSService:
    """Service encapsulating AWS STS assume_role calls with dynamic IoT session policies."""

    def __init__(self, client: Any | None = None, config: Settings = settings) -> None:
        self._client = client
        self._config = config

    def _get_client(self) -> Any:
        if self._client is None:
            return boto3.client("sts", region_name=self._config.aws_region)
        return self._client

    def build_session_policy(self, user_id: str | int, device_ids: list[str]) -> str:
        """Construct a least-privilege scoped session policy for AWS IoT Core.

        The policy dynamically restricts:
        - iot:Connect to client IDs matching the user or associated device(s).
        - iot:Publish & iot:Receive to healthkicks/v1/{device_id}/* topics.
        - iot:Subscribe to healthkicks/v1/{device_id}/* topic filters.
        """
        role_arn = self._config.aws_iot_role_arn
        arn_parts = role_arn.split(":") if role_arn else []
        account_id = arn_parts[4] if len(arn_parts) >= 5 and arn_parts[4] else "*"
        region = self._config.aws_region or "*"

        clean_user_id = re.sub(r"[^\w+=,.@-]", "-", str(user_id))

        if "*" in device_ids:
            client_resources = [f"arn:aws:iot:{region}:{account_id}:client/*"]
            topic_resources = [f"arn:aws:iot:{region}:{account_id}:topic/healthkicks/v1/*"]
            topicfilter_resources = [f"arn:aws:iot:{region}:{account_id}:topicfilter/healthkicks/v1/*"]
        else:
            client_resources = [
                f"arn:aws:iot:{region}:{account_id}:client/healthkicks-mobile-{clean_user_id}",
                f"arn:aws:iot:{region}:{account_id}:client/healthkicks-session-{clean_user_id}",
            ]
            topic_resources = [
                f"arn:aws:iot:{region}:{account_id}:topic/healthkicks/v1/{dev_id}/*" for dev_id in device_ids
            ] + [
                f"arn:aws:iot:{region}:{account_id}:topic/healthkicks/v1/users/{clean_user_id}/*",
            ]
            topicfilter_resources = [
                f"arn:aws:iot:{region}:{account_id}:topicfilter/healthkicks/v1/{dev_id}/*" for dev_id in device_ids
            ] + [
                f"arn:aws:iot:{region}:{account_id}:topicfilter/healthkicks/v1/users/{clean_user_id}/*",
            ]

        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "AllowIoTConnect",
                    "Effect": "Allow",
                    "Action": ["iot:Connect"],
                    "Resource": client_resources,
                },
                {
                    "Sid": "AllowIoTPublishAndReceive",
                    "Effect": "Allow",
                    "Action": ["iot:Publish", "iot:Receive"],
                    "Resource": topic_resources,
                },
                {
                    "Sid": "AllowIoTSubscribe",
                    "Effect": "Allow",
                    "Action": ["iot:Subscribe"],
                    "Resource": topicfilter_resources,
                },
            ],
        }
        return json.dumps(policy)

    def generate_iot_credentials(
        self, user_id: str | int, device_ids: list[str]
    ) -> dict[str, Any]:
        """Assume the configured IoT role and generate temporary scoped credentials."""
        if not self._config.aws_iot_role_arn:
            logger.error("AWS IoT Role ARN is not configured in settings")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="AWS IoT Role ARN is not configured on the server",
            )

        if not device_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="At least one device ID must be specified to scope IoT credentials",
            )

        clean_user_id = re.sub(r"[^\w+=,.@-]", "-", str(user_id))
        session_name = f"healthkicks-session-{clean_user_id}"[:64]
        if len(session_name) < 2:
            session_name = "healthkicks-session"

        policy_json = self.build_session_policy(clean_user_id, device_ids)
        duration = min(max(self._config.aws_sts_session_duration, 900), 3600)

        client = self._get_client()
        try:
            logger.info(
                "Assuming IoT role %s for user %s (devices: %s, duration: %ss)",
                self._config.aws_iot_role_arn,
                user_id,
                device_ids,
                duration,
            )
            response = client.assume_role(
                RoleArn=self._config.aws_iot_role_arn,
                RoleSessionName=session_name,
                Policy=policy_json,
                DurationSeconds=duration,
            )
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "ClientError")
            error_message = exc.response.get("Error", {}).get("Message", str(exc))
            logger.error("AWS STS AssumeRole ClientError [%s]: %s", error_code, error_message)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"AWS STS AssumeRole failed ({error_code}): {error_message}",
            ) from exc
        except BotoCoreError as exc:
            logger.error("AWS STS AssumeRole BotoCoreError: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"AWS STS communication failure: {exc}",
            ) from exc
        except Exception as exc:
            logger.exception("Unexpected error during AWS STS AssumeRole: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal server error during STS token exchange",
            ) from exc

        creds = response["Credentials"]
        return {
            "access_key_id": creds["AccessKeyId"],
            "secret_access_key": creds["SecretAccessKey"],
            "session_token": creds["SessionToken"],
            "expiration": creds["Expiration"],
            "iot_endpoint": self._config.aws_iot_endpoint,
            "region": self._config.aws_region,
            "user_id": str(user_id),
        }

