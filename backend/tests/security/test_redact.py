import pytest

from orchestrator.security.redact import MASK, redact, redact_obj, register_secret

SECRETS = [
    "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "AIzaSyA1234567890abcdefghijklmnopqrstuv",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz",
    "xoxb-1234567890-abcdefghij",
    "AKIAABCDEFGHIJKLMNOP",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
]


@pytest.mark.parametrize("secret", SECRETS)
def test_known_formats_masked(secret):
    out = redact(f"value is {secret} end")
    assert secret not in out
    assert MASK in out


@pytest.mark.parametrize(
    "text,leak",
    [
        ("Authorization: Bearer abcdef1234567890", "abcdef1234567890"),
        ("curl -H 'authorization: token zzzzyyyyxxxx1111'", "zzzzyyyyxxxx1111"),
        ('{"api_key": "hunter2hunter2"}', "hunter2hunter2"),
        ("OPENAI_API_KEY=abc123secretvalue", "abc123secretvalue"),
        ("password: s3cr3tP@ss", "s3cr3tP@ss"),
        ("https://user:p4ssw0rd@example.com/repo.git", "p4ssw0rd"),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----", "MIIabc"),
    ],
)
def test_contextual_secrets_masked(text, leak):
    assert leak not in redact(text)


def test_plain_text_untouched():
    text = "Ran 12 tests in 0.3s. OK. token count: 42, see docs/ARCHITECTURE.md"
    assert redact(text) == text


def test_registered_secret_and_nested():
    register_secret("my-very-own-api-token-value")
    data = {"a": ["x my-very-own-api-token-value y", {"b": "ok"}], "n": 3}
    out = redact_obj(data)
    assert "my-very-own-api-token-value" not in str(out)
    assert out["a"][1] == {"b": "ok"} and out["n"] == 3


async def test_bus_redacts_payloads(h):
    from orchestrator.domain import events as ev

    e = await h.ctx.bus.emit(ev.SYSTEM_NOTICE, payload={"message": "key sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWx"})
    stored = (await h.ctx.bus.replay(e.seq - 1, limit=1))[0]
    assert "sk-ant" not in stored.payload["message"]
