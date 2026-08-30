from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_URL = f"sqlite+aiosqlite:///{(BASE_DIR / 'data' / 'shop.db').as_posix()}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str
    admin_ids: str = ""
    database_url: str = DEFAULT_DATABASE_URL

    public_base_url: str = ""
    web_host: str = "0.0.0.0"
    web_port: int = 8080

    rollypay_api_base: str = "https://api.rollypay.io"
    rollypay_terminal_id: str = ""
    rollypay_api_key: str = ""
    rollypay_signing_secret: str = ""
    rollypay_test_mode: bool = True

    cryptopay_api_base: str = "https://pay.crypt.bot"
    cryptopay_token: str = ""
    cryptopay_webhook_secret: str = ""

    @field_validator("public_base_url", "rollypay_api_base", "cryptopay_api_base")
    @classmethod
    def normalize_urls(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        return value

    @property
    def admins(self) -> set[int]:
        result: set[int] = set()
        for value in self.admin_ids.split(","):
            value = value.strip()
            if value:
                result.add(int(value))
        return result

    @property
    def rollypay_enabled(self) -> bool:
        return _is_configured(self.rollypay_api_key) and _is_configured(self.rollypay_signing_secret)

    @property
    def cryptopay_enabled(self) -> bool:
        return _is_configured(self.cryptopay_token)


def _is_configured(value: str) -> bool:
    return bool(value and value.strip() and value.strip().upper() not in {"CHANGE_ME", "YOUR_TOKEN"})


@lru_cache
def get_settings() -> Settings:
    return Settings()
