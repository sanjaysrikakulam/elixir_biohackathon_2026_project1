"""Edit the Ansible vault that holds the RabbitMQ passwords.

The vault is decrypted in memory with ansible-core's own VaultLib, its plaintext is edited with the same
line-preserving engine as the other files (only our keys change, everything else stays byte for byte), and
it is re-encrypted with the same vault id. Every new ciphertext is decrypted again and compared before it is
handed out. Ciphertext is salted, so "did anything change" is always decided on the plaintext.

The password comes from ``--vault-password-file`` or the ``BYOC_SYNC_VAULT_PASSWORD`` environment variable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ansible.errors import AnsibleError
from ansible.parsing.vault import VaultLib, VaultSecret, is_encrypted, parse_vaulttext_envelope

from .models import MASK
from .yamledit.doc import Doc, LocateError
from .yamledit.edit import Edit, apply, delete_edit, insert_after_edit

PASSWORD_ENV = "BYOC_SYNC_VAULT_PASSWORD"


class VaultError(RuntimeError):
    pass


def read_password(path: str | None) -> bytes:
    """The vault password: from ``path`` if given, else from ``$BYOC_SYNC_VAULT_PASSWORD``."""
    if path:
        try:
            password = Path(path).read_text().strip()
        except OSError as exc:
            raise VaultError(f"cannot read vault password file {path}: {exc.strerror}") from None
        if not password:
            raise VaultError(f"vault password file {path} is empty")
        return password.encode()
    password = os.environ.get(PASSWORD_ENV, "")
    if not password:
        raise VaultError(f"no vault password: use --vault-password-file or set {PASSWORD_ENV}")
    return password.encode()


def _quote(value: str) -> str:
    # Always a quoted string: an all-digit password would otherwise turn into a YAML integer.
    return "'" + value.replace("'", "''") + "'"


@dataclass(frozen=True)
class VaultChange:
    key: str
    action: Literal["create", "update", "delete"]

    def describe(self) -> str:
        return f"{self.action} {self.key}" + ("" if self.action == "delete" else f": {MASK}")


@dataclass
class VaultPlan:
    path: Path
    before: str
    plaintext: str
    changes: list[VaultChange] = field(default_factory=list)
    _lib: VaultLib | None = field(default=None, repr=False)
    _secret: VaultSecret | None = field(default=None, repr=False)
    _vault_id: str | None = field(default=None, repr=False)

    @property
    def changed(self) -> bool:
        return self.plaintext != self.before

    def ciphertext(self) -> bytes:
        """Encrypt the new plaintext and prove it decrypts back to exactly that text."""
        assert self._lib is not None and self._secret is not None
        blob = self._lib.encrypt(self.plaintext.encode(), self._secret, vault_id=self._vault_id)
        if self._lib.decrypt(blob).decode() != self.plaintext:
            raise VaultError(f"{self.path}: re-encrypted vault does not decrypt to the planned content")
        return blob

    def __repr__(self) -> str:
        return f"VaultPlan(path={str(self.path)!r}, changes={[c.describe() for c in self.changes]!r})"

    __str__ = __repr__


class VaultFile:
    def __init__(self, path: str | Path, password: bytes, anchor: str = r"^rabbitmq_password_"):
        self.path = Path(path)
        self.anchor = anchor
        if not self.path.is_file():
            raise VaultError(f"vault file {self.path} not found")
        blob = self.path.read_bytes()
        if not is_encrypted(blob):
            raise VaultError(f"{self.path} is not an Ansible vault")
        _, _, _, vault_id = parse_vaulttext_envelope(blob)
        self._vault_id = vault_id if vault_id and vault_id != "default" else None
        self._secret = VaultSecret(password)
        self._lib = VaultLib([(self._vault_id or "default", self._secret)])
        try:
            self.plaintext = self._lib.decrypt(blob).decode()
        except AnsibleError:
            raise VaultError(f"cannot decrypt {self.path}: wrong vault password?") from None
        self._doc = self._parse(self.plaintext)
        if not isinstance(self._doc.data, dict):
            raise VaultError(f"{self.path}: the vault must contain a mapping of variables")

    def _parse(self, plaintext: str) -> Doc:
        """Parse decrypted content without caching it and without echoing it in errors (YAML error messages
        quote the offending lines and duplicate values)."""
        try:
            return Doc(plaintext, name=str(self.path), cache=False)
        except LocateError as exc:
            mark = getattr(exc.__cause__, "problem_mark", None) or getattr(exc.__cause__, "context_mark", None)
            where = f" near line {mark.line + 1}" if mark is not None else ""
            kind = type(exc.__cause__).__name__ if exc.__cause__ else "error"
            raise VaultError(f"{self.path}: decrypted vault is not valid YAML{where} ({kind})") from None

    @staticmethod
    def parse(plaintext: str) -> dict:
        return Doc(plaintext, cache=False).data

    def keys(self) -> set[str]:
        return set(self._doc.data)

    def get(self, key: str) -> str | None:
        value = self._doc.data.get(key)
        return None if value is None else str(value)

    def plan(self, wanted: dict[str, str | None]) -> VaultPlan:
        """Set (value) or remove (None) top-level variables."""
        edits: list[Edit] = []
        inserts: list[str] = []
        changes: list[VaultChange] = []
        for key, value in wanted.items():
            span = self._doc.map_child([], key)
            if value is None:
                if span is not None:
                    edits.append(delete_edit(self._doc, span))
                    changes.append(VaultChange(key, "delete"))
            elif span is None:
                inserts.append(f"{key}: {_quote(value)}")
                changes.append(VaultChange(key, "create"))
            elif self.get(key) != value:
                edits.append(Edit("replace", span.start, span.end, [f"{key}: {_quote(value)}"]))
                changes.append(VaultChange(key, "update"))
        if inserts:
            edits.append(insert_after_edit(self._doc, [], anchor=self.anchor, lines=inserts))
        plaintext = apply(self.plaintext, edits) if edits else self.plaintext
        if edits:
            self._check(plaintext, wanted)
        return VaultPlan(self.path, self.plaintext, plaintext, changes, self._lib, self._secret, self._vault_id)

    def _check(self, plaintext: str, wanted: dict[str, str | None]) -> None:
        expected = dict(self._doc.data)
        for key, value in wanted.items():
            if value is None:
                expected.pop(key, None)
            else:
                expected[key] = value
        if self._parse(plaintext).data != expected:
            raise VaultError(f"{self.path}: edited vault content is not as planned")
