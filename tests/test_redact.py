import pytest

from brain.redact import MASK, redact


@pytest.mark.parametrize(
    "text",
    [
        "export GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "password: hunter2xyz",
        '"client_secret": "Abc.123~defGHI"',
        "postgres://admin:S3cretPw@db.host:5432/x",
        "AccountKey=abcDEF123+/xyz==;EndpointSuffix",
        "key sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA",
        "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.abcdefghijklmnop",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
    ],
)
def test_secrets_are_masked(text):
    out = redact(text)
    assert MASK in out
    for secret in ("hunter2xyz", "S3cretPw", "abcDEF123", "Abc.123", "MIIEow", "ghp_abc"):
        assert secret not in out


@pytest.mark.parametrize("text", ["token budget is fine", "pg_search key_field is deprecated"])
def test_prose_is_untouched(text):
    assert redact(text) == text
