import base64
import hashlib

from django.contrib.auth.hashers import (
    BasePasswordHasher,
    PBKDF2PasswordHasher,
    mask_hash,
)
from django.utils.crypto import constant_time_compare
from django.utils.encoding import force_bytes
from django.utils.translation import gettext_noop


class SHA512Base64PBKDF2PasswordHasher(PBKDF2PasswordHasher):
    """Hasher for Symfony migrated password tokens.

    5000 iterations of sha512 plus base64 encoder, with an extra
    PBKDF2 round.
    """

    algorithm = "sha512b64pbkdf2"
    iterations = 690000

    def encode(self, password, salt, iterations=None):
        symfony_iterations = 5000
        digest = hashlib.sha512(force_bytes(password, encoding="ISO-8859-1")).digest()
        for _i in range(symfony_iterations - 1):
            digest = hashlib.sha512(
                digest + force_bytes(password, encoding="ISO-8859-1")
            ).digest()
        iterations = iterations or self.iterations
        return self.pbkdf2_round(
            base64.b64encode(digest).decode("ISO-8859-1"), salt, iterations
        )

    def pbkdf2_round(self, password, salt, iterations=None):
        """PBKDF2 round (salt + secure hash function added)."""
        return super().encode(password, salt, iterations)


def php_strtolower(password: str) -> bytes:
    """Lower-case exactly the way PHP's ``strtolower()`` does.

    PHP folds only ASCII ``A-Z`` (byte-wise, C locale); Python's ``str.lower()``
    also folds non-ASCII, so ``"Ä"`` would diverge and every password holding one
    would fail to verify. Fold the UTF-8 bytes instead of the string.
    """
    return password.encode("utf-8").translate(
        bytes.maketrans(
            bytes(range(ord("A"), ord("Z") + 1)),
            bytes(range(ord("a"), ord("z") + 1)),
        )
    )


class WSMLegacyPasswordHasher(BasePasswordHasher):
    """Base for the two WSM5 (``core``) customer password schemes.

    Verify-only: these exist so customers migrated off the legacy PHP platform can
    log in once with the credentials they already had. Django then re-hashes them
    with the preferred hasher, so nothing is ever *written* in these formats --
    hence ``salt()`` refuses and ``must_update()`` is always True.
    """

    def salt(self):
        raise NotImplementedError(
            f"{type(self).__name__} is verify-only; it must never hash a new password"
        )

    def must_update(self, encoded):
        return True

    def harden_runtime(self, password, encoded):
        # Never reached: Django short-circuits on `hasher.algorithm != preferred`
        # before it would harden, because these are never the preferred hasher.
        pass

    def safe_summary(self, encoded):
        decoded = self.decode(encoded)
        summary = {
            gettext_noop("algorithm"): decoded["algorithm"],
            gettext_noop("hash"): mask_hash(decoded["hash"]),
        }
        if decoded["salt"]:
            summary[gettext_noop("salt")] = mask_hash(decoded["salt"], show=2)
        return summary


class WSMSHA256PasswordHasher(WSMLegacyPasswordHasher):
    """WSM5 salted scheme: ``sha256(strtolower(password) + "-wsm-" + salt)``.

    Mirrors ``Model_Customer::validatePassword()`` in the legacy PHP platform
    (``app/obj/customer.obj.php``) for rows where ``customer.salt`` is set.
    Encoded as ``wsm_sha256$<salt>$<sha256 hex>``.

    Note these passwords are effectively case-insensitive, because ``core``
    lower-cased before hashing.
    """

    algorithm = "wsm_sha256"

    def encode(self, password, salt):
        self._check_encode_args(password, salt)
        digest = hashlib.sha256(
            php_strtolower(password) + b"-wsm-" + salt.encode("utf-8")
        ).hexdigest()
        return f"{self.algorithm}${salt}${digest}"

    def decode(self, encoded):
        algorithm, salt, digest = encoded.split("$", 2)
        assert algorithm == self.algorithm
        return {"algorithm": algorithm, "salt": salt, "hash": digest}

    def verify(self, password, encoded):
        salt = self.decode(encoded)["salt"]
        return constant_time_compare(self.encode(password, salt), encoded)


class WSMMD5PasswordHasher(WSMLegacyPasswordHasher):
    """WSM5 pre-salt scheme: a bare MD5, for customers who never logged in again.

    ``core`` accepted either ``md5(password)`` or ``md5(strtolower(password))``,
    so both are checked here. Encoded as ``wsm_md5$$<md5 hex>`` -- the empty salt
    field keeps ``$``-splitting uniform, and the resulting 41-char string avoids
    Django's ancient bare-MD5 special cases in ``identify_hasher()``.
    """

    algorithm = "wsm_md5"

    def encode(self, password, salt=""):
        if not password:
            raise ValueError("password must be provided.")
        if salt:
            raise ValueError(f"salt must be empty for {self.algorithm}.")
        return f"{self.algorithm}$${hashlib.md5(password.encode('utf-8')).hexdigest()}"

    def decode(self, encoded):
        algorithm, salt, digest = encoded.split("$", 2)
        assert algorithm == self.algorithm
        assert salt == ""
        return {"algorithm": algorithm, "salt": "", "hash": digest}

    def verify(self, password, encoded):
        expected = self.decode(encoded)["hash"]
        as_typed = hashlib.md5(password.encode("utf-8")).hexdigest()
        lowered = hashlib.md5(php_strtolower(password)).hexdigest()
        # Both compared unconditionally -- no early return -- to keep the
        # comparison time independent of which variant matched.
        return constant_time_compare(as_typed, expected) | constant_time_compare(
            lowered, expected
        )
