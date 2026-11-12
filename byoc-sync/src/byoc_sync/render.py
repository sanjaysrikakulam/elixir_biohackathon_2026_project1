"""Render the YAML blocks byoc-sync writes, from the site's templates.

Templates use ``[[ ]]`` / ``[% %]`` delimiters so that Ansible's own ``{{ ... }}`` passes through literally.
They never receive the password: it only ever goes into the vault.
"""

from __future__ import annotations

import itertools
import json
import re
from dataclasses import asdict, dataclass, replace
from typing import Any

import jinja2

from .config import SiteConfig
from .models import ByocRequest
from .naming import Names
from .yamledit.doc import Doc

# E-mails end up in an Ansible-templated file (the destination's owner guard) and as TPV users keys, which TPV
# reads as regular expressions (prefix match). Only plain ASCII addresses without braces, whitespace or regex
# operators are accepted: "bob+lab@x.org" would not even match itself. A key that also matches look-alike
# addresses ("alice@example.org" matches "alice@example.org.evil") is harmless: the destination's owner guard
# compares the exact e-mail.
SAFE_EMAIL_RE = re.compile(r"[A-Za-z0-9.!#%&'/=_`~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")


def is_safe_email(email: str) -> bool:
    return isinstance(email, str) and len(email) <= 255 and SAFE_EMAIL_RE.fullmatch(email) is not None


def email_key(email: str) -> str:
    """The TPV ``users:`` key for this e-mail: the address itself."""
    if not is_safe_email(email):
        raise ValueError(f"cannot write e-mail {email!r} into the configuration")
    return email


def email_from_key(key: str) -> str | None:
    """The e-mail a ``users:`` key stands for, or None if byoc-sync would not write such a key."""
    return key if isinstance(key, str) and is_safe_email(key) else None


# Galaxy user ids as the sources report them: encoded (API, hex) or the database id (gxadmin, digits).
SAFE_GALAXY_ID_RE = re.compile(r"[0-9a-f]{1,64}")


def is_safe_galaxy_id(galaxy_id: str) -> bool:
    return isinstance(galaxy_id, str) and SAFE_GALAXY_ID_RE.fullmatch(galaxy_id) is not None


def owner_key(owner: str, mode: str) -> str:
    """The key that records who owns an endpoint: the e-mail (``email``) or the Galaxy user id (``galaxy_id``)."""
    if mode == "galaxy_id":
        if not is_safe_galaxy_id(owner):
            raise ValueError(f"cannot write user id {owner!r} into the configuration")
        return owner
    return email_key(owner)


def owner_from_key(key: str, mode: str) -> str | None:
    """The owner a key in the ownership file stands for, or None if byoc-sync would not write such a key."""
    if mode == "galaxy_id":
        return key if is_safe_galaxy_id(key) else None
    return email_from_key(key)


def _yaml_key(value: str) -> str:
    # Plain like the hand-written entries when that is unambiguous YAML, single-quoted otherwise.
    return value if re.fullmatch(r"[A-Za-z0-9._@-]+", value) else _yaml_single(value)


def _yaml_single(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


@dataclass(frozen=True)
class DesiredObject:
    """One rendered entry: its id in the collection, its text (indent 0) and its parsed data."""

    id: str
    text: str
    data: Any
    universe: Any  # union of everything the template can produce for this entry


def deep_union(a: Any, b: Any) -> Any:
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for key, value in b.items():
            out[key] = deep_union(a[key], value) if key in a else value
        return out
    if isinstance(a, list) and isinstance(b, list):
        if all(isinstance(i, dict) and "id" in i for i in a + b):
            by_id = {i["id"]: i for i in a}
            for item in b:
                by_id[item["id"]] = deep_union(by_id[item["id"]], item) if item["id"] in by_id else item
            return list(by_id.values())
        return a + [i for i in b if i not in a]
    return b


class Renderer:
    def __init__(self, site: SiteConfig):
        self.site = site
        self.env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(site.templates_dir)),
            variable_start_string="[[",
            variable_end_string="]]",
            block_start_string="[%",
            block_end_string="%]",
            comment_start_string="[#",
            comment_end_string="#]",
            undefined=jinja2.StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=False,
            autoescape=False,
        )
        self.env.filters["q"] = lambda v: json.dumps(str(v))
        self.env.filters["sq"] = _yaml_single
        self.env.filters["yaml_key"] = _yaml_key
        self.env.filters["pystr"] = lambda v: json.dumps(str(v))

    def render(self, key: str, req: ByocRequest, names: Names, email: str, *, strict: bool = False) -> str:
        """``email`` is the owner key: the e-mail, or with ``owner_key: galaxy_id`` the Galaxy user id."""
        spec = self.site.targets[key]
        values = {k: v for k, v in asdict(req).items() if k != "password"}
        by_id = self.site.owner_key == "galaxy_id"
        context = {
            "req": values,
            "names": names,
            # with owner_key: galaxy_id no e-mail is known to the templates, so none can end up in a file
            "email": None if by_id else email,
            "email_key": None if by_id else email_key(email),
            "owner_key": owner_key(email, self.site.owner_key),
            "galaxy_id": email if by_id else None,
            "site": self.site.site,
            "strict": strict,
        }
        return self.env.get_template(spec.template).render(**context).rstrip("\n")

    def object_id(self, key: str, names: Names, email: str) -> str:
        attribute = self.site.targets[key].id
        if attribute in ("email_key", "owner_key"):
            return owner_key(email, self.site.owner_key)
        return getattr(names, attribute)

    @staticmethod
    def _entry(text: str) -> Any:
        # Parse the block the way it will sit in a file: followed by a newline. Without it, a block scalar
        # at the very end ("execute: |") would lose its final newline and never compare equal.
        data = Doc(text + "\n").data
        return data[0] if isinstance(data, list) else next(iter(data.values()))

    def universe(self, key: str, names: Names, email: str) -> Any:
        """Everything the template can produce for this entry (all optional parts, both routing modes)."""
        sample = ByocRequest(
            name=names.name, password="", max_cores=1, max_mem=1, min_gpus=0, max_gpus=0, nfs_path=None
        )
        union: Any = None
        for nfs, strict in itertools.product((None, "/sample"), (False, True)):
            text = self.render(key, replace(sample, nfs_path=nfs), names, email, strict=strict)
            entry = self._entry(text)
            union = entry if union is None else deep_union(union, entry)
        return union

    def desired(self, key: str, req: ByocRequest, names: Names, email: str, *, strict: bool = False) -> DesiredObject:
        text = self.render(key, req, names, email, strict=strict)
        return DesiredObject(
            id=self.object_id(key, names, email),
            text=text,
            data=self._entry(text),
            universe=self.universe(key, names, email),
        )

    def removal(self, key: str, names: Names, email: str) -> Removal:
        return Removal(id=self.object_id(key, names, email), universe=self.universe(key, names, email))


@dataclass(frozen=True)
class Removal:
    """An entry to delete, with everything byoc-sync could have written into it."""

    id: str
    universe: Any
