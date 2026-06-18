"""ARTIFACT_STORE config validation."""
import pytest
from pydantic import ValidationError

from src.backend.core.config import Settings


def test_artifact_store_defaults_to_local():
    assert Settings().ARTIFACT_STORE == "local"


def test_artifact_store_accepts_s3_case_insensitively():
    assert Settings(ARTIFACT_STORE="S3").ARTIFACT_STORE == "s3"


def test_artifact_store_rejects_unknown_backend():
    # A @validator raising ValueError surfaces as pydantic's ValidationError,
    # which does NOT subclass ValueError in pydantic v2.
    with pytest.raises(ValidationError):
        Settings(ARTIFACT_STORE="gcs")


def test_s3_settings_have_defaults():
    s = Settings()
    assert s.ARTIFACT_S3_BUCKET == ""
    assert s.ARTIFACT_S3_PREFIX == "runs"
