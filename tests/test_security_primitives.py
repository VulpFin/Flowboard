# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest

from app.security.crypto import CredentialCipher, CredentialCryptoError, generate_key, mask_secret, _decode_key
from app.security.passwords import hash_password, verify_password, needs_rehash
from app.security.tokens import generate_token, hash_token, token_matches


def test_aes_gcm_roundtrip_and_aad_binding():
    c = CredentialCipher({1: b"k" * 32})
    blob, ver = c.encrypt_json({"api_key": "sk-secret"}, aad="ai-credential:u1:openai")
    assert ver == 1 and blob[:3] == b"FB1"
    assert c.decrypt_json(blob, "ai-credential:u1:openai") == {"api_key": "sk-secret"}
    # same blob, different owner => refuses
    with pytest.raises(CredentialCryptoError):
        c.decrypt_json(blob, "ai-credential:u2:openai")
    # tampering => refuses
    bad = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(CredentialCryptoError):
        c.decrypt_json(bad, "ai-credential:u1:openai")


def test_key_rotation():
    old = CredentialCipher({1: b"a" * 32})
    blob, _ = old.encrypt(b"hello", "x")
    both = CredentialCipher({1: b"a" * 32, 2: b"b" * 32})
    assert both.current_version == 2
    assert both.decrypt(blob, "x") == b"hello"
    assert both.needs_rotation(blob)
    new_blob, ver = both.encrypt(b"hello", "x")
    assert ver == 2 and not both.needs_rotation(new_blob)
    only_new = CredentialCipher({2: b"b" * 32})
    with pytest.raises(CredentialCryptoError):
        only_new.decrypt(blob, "x")


def test_generate_key_decodes():
    k = generate_key()
    assert len(_decode_key(k)) == 32


def test_mask_secret():
    assert mask_secret("sk-abcdefghijklmnop4X2q") == "sk-••••••••4X2q"
    assert mask_secret("") == ""
    assert "abcdefgh" not in mask_secret("sk-ant-abcdefgh")


def test_django_compatible_password_hash():
    h = hash_password("hunter22hunter")
    assert h.startswith("pbkdf2_sha256$870000$")
    assert verify_password("hunter22hunter", h)
    assert not verify_password("wrong", h)
    assert not verify_password("hunter22hunter", None)
    assert not needs_rehash(h)
    assert needs_rehash("pbkdf2_sha256$1000$salt$hash")


def test_token_hashing():
    t = generate_token()
    assert token_matches(t, hash_token(t))
    assert not token_matches(t + "x", hash_token(t))
