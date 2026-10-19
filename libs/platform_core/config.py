"""Environment profile. UAT and production refuse unverified regulatory rule packs."""

from __future__ import annotations

from enum import StrEnum

from pydantic_settings import BaseSettings, SettingsConfigDict


class Profile(StrEnum):
    DEV = "dev"
    TEST = "test"
    UAT = "uat"
    PROD = "prod"


class PlatformSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CP_", env_file=".env", extra="ignore")

    profile: Profile = Profile.DEV
    allow_unverified_rules: bool = False  # override; every use is logged to the audit chain
    data_residency_region: str = "IN"

    @property
    def strict(self) -> bool:
        return self.profile in (Profile.UAT, Profile.PROD)
