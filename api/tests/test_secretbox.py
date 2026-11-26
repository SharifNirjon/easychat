"""secretbox.py: sealed tenant secrets and keyed one-time-code hashes."""
import pytest

from secretbox import SealError, code_digest, open_sealed, same_digest, seal


def test_a_sealed_value_opens_in_its_own_context():
    """Round trip: what goes in comes out, and the ciphertext doesn't contain the plain text."""
    blob = seal({"secret": "shpat_abc123"}, "order-credentials:tenant-1")
    assert b"shpat_abc123" not in blob
    assert open_sealed(blob, "order-credentials:tenant-1") == {"secret": "shpat_abc123"}


def test_a_sealed_value_does_not_open_for_another_tenant():
    """Copying one tenant's ciphertext into another tenant's row gets nothing."""
    blob = seal({"secret": "x"}, "order-credentials:tenant-1")
    with pytest.raises(SealError):
        open_sealed(blob, "order-credentials:tenant-2")


def test_tampered_or_garbage_values_are_refused():
    """GCM authenticates: one flipped byte, or random bytes, fail."""
    blob = bytearray(seal({"secret": "x"}, "ctx"))
    blob[-1] ^= 1
    for bad in (bytes(blob), b"", b"\x01short", b"\x02" + bytes(40)):
        with pytest.raises(SealError):
            open_sealed(bad, "ctx")


def test_each_seal_is_different():
    """A random nonce per seal: the same secret never gives the same ciphertext."""
    assert seal({"a": 1}, "ctx") != seal({"a": 1}, "ctx")


def test_code_digests_are_bound_to_their_context():
    """The same code hashes differently per tenant/chat/order, and compares in constant time."""
    a = code_digest("123456", "tenant:conv:MR-1")
    assert same_digest(a, code_digest("123456", "tenant:conv:MR-1"))
    assert not same_digest(a, code_digest("123456", "tenant:conv:MR-2"))
    assert not same_digest(a, code_digest("123457", "tenant:conv:MR-1"))
