from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    admin_ids: str = ""
    database_url: str = "sqlite+aiosqlite:///./shop.db"

    public_base_url: str = "https://example.com"
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

    @property
    def admins(self) -> set[int]:
        result: set[int] = set()
        for value in self.admin_ids.split(","):
            value = value.strip()
            if value:
                result.add(int(value))
        return result


@lru_cache
def get_settings() -> Settings:
    return Settings()
