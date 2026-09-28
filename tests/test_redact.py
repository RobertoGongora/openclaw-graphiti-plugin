from graph_memory.importers import redact

PEM = """-----BEGIN PRIVATE KEY-----
not-a-real-key
-----END PRIVATE KEY-----"""
JWT = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJhdGxhcyJ9.signaturevalue"


def test_existing_secret_patterns_still_redact():
    text = "\n".join(
        [
            PEM,
            "sk-abcdefghijklmnopqrstuvwxyz",
            "ghp_abcdefghijklmnopqrstuvwxyz",
            "xoxb-1234-abcdefghij",
            "password=hunter2",
            "api_key=private-value",
            "Bearer abcdefghijklmnop",
            "Atlas uses MySQL.",
        ]
    )
    cleaned = redact(text)
    for secret in (
        "not-a-real-key",
        "sk-abcdefghijklmnopqrstuvwxyz",
        "ghp_abcdefghijklmnopqrstuvwxyz",
        "xoxb-1234-abcdefghij",
        "hunter2",
        "private-value",
        "abcdefghijklmnop",
    ):
        assert secret not in cleaned
    assert "[REDACTED PRIVATE KEY]" in cleaned
    assert "Atlas uses MySQL." in cleaned


def test_github_device_code_is_redacted():
    cleaned = redact("Enter device code ABCD-EFGH then continue. Atlas uses MySQL.")
    assert "ABCD-EFGH" not in cleaned
    assert "[REDACTED DEVICE CODE]" in cleaned
    assert "Atlas uses MySQL." in cleaned
    assert "well-known" in redact("a well-known path")


def test_tskey_and_xai_tokens_are_redacted():
    cleaned = redact("tskey-auth-kabcdefghijklmnopqrstuvwxyz and xai-abcdefghijklmnopqrstuvwxyz")
    assert "tskey-auth-kabcdefghijklmnopqrstuvwxyz" not in cleaned
    assert "xai-abcdefghijklmnopqrstuvwxyz" not in cleaned
    assert redact("the xai-grok model") == "the xai-grok model"


def test_jwt_is_redacted():
    cleaned = redact(f"header {JWT} trailer")
    assert JWT not in cleaned
    assert "eyJhbGciOiJub25lIn0" not in cleaned
    assert "[REDACTED TOKEN]" in cleaned


def test_token_and_key_query_params_are_redacted():
    cleaned = redact(
        "https://hooks.example/listen?token=supersecretvalue&ok=1 "
        "https://hooks.example/listen?key=anothersecret "
        "https://hooks.example/listen?ok=1&token=zzzsecret"
    )
    assert "supersecretvalue" not in cleaned
    assert "anothersecret" not in cleaned
    assert "zzzsecret" not in cleaned
    assert "?token=[REDACTED]" in cleaned
    assert "?key=[REDACTED]" in cleaned
    assert "&token=[REDACTED]" in cleaned
    assert "ok=1" in cleaned


def test_cursor_webhook_key_style_secrets_are_redacted():
    cleaned = redact(
        "crsr_abcdefghijklmnopqrstuvwxyz webhook_key=cursorsecretvalue "
        "webhook-key: cursor-other-secret Bearer crsr_bearersecretvalue"
    )
    assert "crsr_abcdefghijklmnopqrstuvwxyz" not in cleaned
    assert "cursorsecretvalue" not in cleaned
    assert "cursor-other-secret" not in cleaned
    assert "crsr_bearersecretvalue" not in cleaned
    assert "webhook_key=[REDACTED]" in cleaned
    assert "webhook-key: [REDACTED]" in cleaned
