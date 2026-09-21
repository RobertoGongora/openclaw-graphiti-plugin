"""The default credentials start the stack; nothing touches the graph until they change."""

import pytest

from graph_memory.credentials import DEFAULT_PASSWORD, refuse_default_password


def test_default_credentials_are_refused_with_instructions(monkeypatch):
    monkeypatch.delenv("MEMORY_ALLOW_DEFAULT_PASSWORD", raising=False)
    monkeypatch.setenv("NEO4J_PASSWORD", DEFAULT_PASSWORD)
    monkeypatch.setenv("MEMORY_HTTP_TOKEN", "a-real-token")
    with pytest.raises(ValueError) as caught:
        refuse_default_password()
    message = str(caught.value)
    assert "NEO4J_PASSWORD" in message and "MEMORY_HTTP_TOKEN" not in message.split("\n")[0]
    assert (
        "ALTER CURRENT USER SET PASSWORD" in message and "MEMORY_ALLOW_DEFAULT_PASSWORD" in message
    )


def test_changed_or_explicitly_accepted_credentials_pass(monkeypatch):
    monkeypatch.setenv("NEO4J_PASSWORD", "something-else")
    monkeypatch.setenv("MEMORY_HTTP_TOKEN", "a-real-token")
    refuse_default_password()
    monkeypatch.setenv("MEMORY_HTTP_TOKEN", DEFAULT_PASSWORD)
    with pytest.raises(ValueError, match="MEMORY_HTTP_TOKEN"):
        refuse_default_password()
    monkeypatch.setenv("MEMORY_ALLOW_DEFAULT_PASSWORD", "1")
    refuse_default_password()
    monkeypatch.delenv("NEO4J_PASSWORD")
    monkeypatch.delenv("MEMORY_HTTP_TOKEN")
    monkeypatch.delenv("MEMORY_ALLOW_DEFAULT_PASSWORD")
    refuse_default_password()  # no password at all is a local, auth-less database
