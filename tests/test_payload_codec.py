import pytest

from worker.payload_codec import (
    PayloadProtectionError,
    WindowsCurrentUserDPAPICodec,
)


@pytest.mark.skipif(__import__("os").name != "nt", reason="production codec is Windows-only")
def test_windows_current_user_codec_roundtrip_and_entropy_binding():
    codec = WindowsCurrentUserDPAPICodec()
    plaintext = b"harmless Atlas protected-payload test"
    entropy = b"atlas-test-entropy-v1"
    ciphertext = codec.protect(plaintext, entropy=entropy)
    assert ciphertext != plaintext
    assert codec.unprotect(ciphertext, entropy=entropy) == plaintext
    with pytest.raises(PayloadProtectionError):
        codec.unprotect(ciphertext, entropy=b"different-atlas-test-entropy")


def test_production_codec_has_no_plaintext_fallback_on_empty_input():
    if __import__("os").name != "nt":
        with pytest.raises(PayloadProtectionError):
            WindowsCurrentUserDPAPICodec()
        return
    codec = WindowsCurrentUserDPAPICodec()
    with pytest.raises(PayloadProtectionError):
        codec.protect(b"", entropy=b"atlas-test-entropy-v1")
