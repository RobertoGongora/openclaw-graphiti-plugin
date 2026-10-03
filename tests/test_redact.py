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
    for shown in (
        "**Device code:** **WDJB-MJHT**",
        '{"user_code":"WDJB-MJHT"}',
        "open github.com/login/device and enter WDJB-MJHT",
    ):
        assert "WDJB-MJHT" not in redact(shown)


def test_identifiers_shaped_like_device_codes_stay():
    text = (
        "PLAN-1234 GRAF-0199 INV-2026-0001 READ-ONLY AWS4-HMAC-SHA256 ports 7473-7474 "
        "1999-2016 550E8400-E29B-41D4-A716-446655440000 xai-grok-2-vision-1212-latest"
    )
    assert redact(text) == text


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


def test_quoted_and_prefixed_keys_are_redacted():
    cleaned = redact(
        '{"api_key": "value-one", "password":"value-two"} '
        "{'secret': 'value-three'} OPENAI_API_KEY=value-four GITHUB_TOKEN=value-five "
        "Authorization: Bearer value-six-long-enough Authorization: Basic dmFsdWUtc2V2ZW4= "
        'password: "value eight words" '
        "https://x.example/cb#access_token=value-nine&x=1 ?client_secret=value-ten"
    )
    for n in ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"):
        assert f"value-{n}" not in cleaned and "dmFsdWUtc2V2ZW4" not in cleaned
    assert '"api_key": "[REDACTED]"' in cleaned
    assert "Authorization: Bearer [REDACTED]" in cleaned
    assert "&x=1" in cleaned
    assert redact("max_tokens: 4096 max_token=5 secret sauce") == (
        "max_tokens: 4096 max_token=5 secret sauce"
    )


def test_short_signature_jwt_and_unterminated_pem_are_redacted():
    cleaned = redact(
        "eyJhbGciOiJub25lIn0.eyJzdWIiOiJhdGxhcyJ9. and -----BEGIN PRIVATE KEY-----\nabc"
    )
    assert "eyJ" not in cleaned
    assert "abc" not in cleaned


def test_redaction_is_idempotent():
    # A cursor hash covers redacted text; running redact again must not change it.
    text = "\n".join(
        [
            PEM,
            JWT,
            '"api_key": "x"',
            "Authorization: Bearer abcdefghijklmnop",
            "?token=abc&key=def",
            "Enter device code ABCD-EFGH",
            "xai-" + "a1" * 20,
            "password=hunter2",
        ]
    )
    once = redact(text)
    assert redact(once) == once


def test_long_identifier_runs_do_not_backtrack():
    import time

    for text in ("A_" * 50_000, "A_B_" * 25_000, "snake_case_identifier_" * 5_000):
        started = time.monotonic()
        redact(text)
        assert time.monotonic() - started < 1


def test_escaped_quotes_in_json_encoded_tool_arguments_are_redacted():
    import json

    for command in (
        'export DB_PASSWORD="value-one"',
        'curl -d \'{"api_key":"value-two"}\'',
    ):
        cleaned = redact(json.dumps({"command": command}))
        assert "value-" not in cleaned
        assert redact(cleaned) == cleaned
    assert "value-three" not in redact(json.dumps(json.dumps({"api_key": "value-three"})))


def test_every_device_code_after_a_prompt_is_redacted_once():
    cleaned = redact("device codes: ABCD-EFGH and WXYZ-2345")
    assert "ABCD-EFGH" not in cleaned and "WXYZ-2345" not in cleaned
    assert redact(cleaned) == cleaned
    assert redact("device code ABCD-EFGH\nticket PLAN-1234").endswith("PLAN-1234")


def test_values_that_carry_a_literal_are_redacted():
    for text in (
        "password=true-secret-99",
        "password: SecretStr('value-one')",
        'password=str("value-two")',
        "api_key=process.env.X||'value-three'",
        "password=${VAR}value-four",
        "password={{a}}value-five",
        'password=abc"value-six"',
    ):
        cleaned = redact(text)
        assert "value-" not in cleaned and "true-secret" not in cleaned, text
        assert redact(cleaned) == cleaned
    assert redact("?apikey=#{api_key}") == redact(redact("?apikey=#{api_key}"))


def test_a_quote_or_bracket_inside_a_secret_does_not_end_it():
    import json
    import random
    import string

    from graph_memory.importers import redact_v1

    for text in (
        "password=pa]ss99word",
        'secret: Xk"9mQ!2zP',
        'password="abc"def123',
        json.dumps({"command": 'export API_KEY=cec!3bg"hc2Z'}),
    ):
        cleaned = redact(text)
        assert not any(part in cleaned for part in ("ss99", "9mQ", "def123", "hc2Z")), text
    rng = random.Random(1)
    chars = string.ascii_letters + string.digits + "!@#$%^&*()[]{}\"'<>/\\|-_+=.:"
    for _ in range(2_000):
        secret = "".join(rng.choice(chars) for _ in range(rng.randint(6, 16)))
        text = f"password={secret}"
        # Nothing the earlier redaction removed may come back.
        assert secret in redact_v1(text) or secret not in redact(text)
