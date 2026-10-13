"""Application settings loaded from the environment and the .env file."""

from typing import Literal, Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    jwt_secret: str
    # Fernet key that encrypts camera passwords. Generate one with:
    # python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    camera_credentials_key: str
    # Where uploaded snapshots and clips are kept, and for how long.
    media_storage_dir: str = "media_store"
    media_retention_days: int = 30
    # How long audit log entries are kept.
    audit_retention_days: int = 365
    # How long in-app notifications are kept.
    notification_retention_days: int = 90
    # How long detection events are kept.
    event_retention_days: int = 90
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30

    # Email: "console" prints to the server terminal, "smtp" sends for real.
    email_mode: Literal["console", "smtp"] = "console"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "VisionPulse <no-reply@visionpulse.local>"
    # "starttls" (port 587), "ssl" (port 465) or "none" (local relay only).
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_timeout_s: float = 10

    # Push: "console" prints to the server terminal, "fcm" sends through
    # Firebase Cloud Messaging with a service account key file (JSON).
    push_mode: Literal["console", "fcm"] = "console"
    fcm_credentials_file: str | None = None
    push_timeout_s: float = 5

    sms_mode: str = "console"

    @model_validator(mode="after")
    def check_transports(self) -> Self:
        if self.email_mode == "smtp" and not self.smtp_host:
            raise ValueError("EMAIL_MODE=smtp needs SMTP_HOST")
        if self.push_mode == "fcm" and not self.fcm_credentials_file:
            raise ValueError("PUSH_MODE=fcm needs FCM_CREDENTIALS_FILE")
        return self


settings = Settings()
