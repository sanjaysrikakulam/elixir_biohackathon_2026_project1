"""Create, update and delete BYOC entries in one playbook file.

A :class:`Target` knows where its collection lives (site config) and plans whole-line edits:

* create: the rendered block goes after the last BYOC entry, or after the last sibling matching the site's
  anchor (for example the last ``pulsar_*`` destination);
* update: value by value. Changed values are replaced, missing ones inserted, list items byoc-sync manages
  are added (lists only have to *contain* them), and values the template no longer produces are removed.
  Keys and list items someone added by hand stay. A managed value whose YAML type changed is a conflict;
* delete: the whole entry, unless it contains something byoc-sync did not write (then: conflict).

Every plan is verified by re-parsing the result: our entries must be exactly as expected and everything
else in the file must be unchanged.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from ..config import SiteConfig
from ..render import DesiredObject, Removal, email_from_key
from ..yamledit.doc import Doc, plain
from ..yamledit.edit import Edit, apply, delete_edit, insert_after_edit
from ..yamledit.verify import VerificationError, _differences


@dataclass(frozen=True)
class ObjectChange:
    object_id: str
    action: Literal["create", "update", "delete"]
    fields: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Conflict:
    object_id: str
    reason: str


@dataclass
class FilePlan:
    file: str
    before: str
    text: str
    changes: list[ObjectChange] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.text != self.before


class _Conflict(Exception):
    pass


def _is_id_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(i, dict) and "id" in i for i in value)


def residual(current: Any, universe: Any) -> Any:
    """What in ``current`` byoc-sync did not (and could not) write. Empty/None means: all ours."""
    if isinstance(current, dict):
        if not isinstance(universe, dict):
            return current
        out = {}
        for key, value in current.items():
            if key not in universe:
                out[key] = value
            else:
                rest = residual(value, universe[key])
                if rest not in (None, {}, []):
                    out[key] = rest
        return out
    if isinstance(current, list):
        if not isinstance(universe, list):
            return current
        if _is_id_list(current) or _is_id_list(universe):
            ours = {i.get("id"): i for i in universe if isinstance(i, dict)}
            out = []
            for item in current:
                if isinstance(item, dict) and item.get("id") in ours:
                    rest = residual(
                        {k: v for k, v in item.items() if k != "id"},
                        {k: v for k, v in ours[item["id"]].items() if k != "id"},
                    )
                    if rest:
                        out.append(item)
                else:
                    out.append(item)
            return out
        return [i for i in current if i not in universe]
    return None


def merge(current: Any, desired: Any, universe: Any) -> Any:
    """The data an entry must have after an update (see module docstring)."""
    if isinstance(desired, dict):
        if not isinstance(current, dict):
            raise _Conflict("is not a mapping any more")
        out = copy.deepcopy(current)
        for key, value in desired.items():
            out[key] = merge(current[key], value, (universe or {}).get(key)) if key in current else value
        for key in list(out):
            if key in desired or not isinstance(universe, dict) or key not in universe:
                continue
            rest = residual(out[key], universe[key])
            if rest in (None, {}, []):
                del out[key]
            else:
                out[key] = rest
        return out
    if isinstance(desired, list):
        if not isinstance(current, list):
            raise _Conflict("is not a list any more")
        if _is_id_list(desired):
            u = {i["id"]: i for i in (universe or []) if isinstance(i, dict) and "id" in i}
            want = {i["id"]: i for i in desired}
            out = []
            for item in current:
                item_id = item.get("id") if isinstance(item, dict) else None
                if item_id in want:
                    out.append(merge(item, want[item_id], u.get(item_id)))
                elif item_id in u and not residual(item, u[item_id]):
                    continue
                else:
                    out.append(item)
            have = {i.get("id") for i in out if isinstance(i, dict)}
            out += [copy.deepcopy(i) for i in desired if i["id"] not in have]
            return out
        managed = [i for i in (universe or []) if i not in desired]
        out = [i for i in current if i not in managed]
        return out + [i for i in desired if i not in out]
    if isinstance(current, (dict, list)):
        raise _Conflict("changed from a single value to a mapping or list")
    return desired


def _reindent(lines: list[str], source_indent: int, target_indent: int) -> list[str]:
    delta = target_indent - source_indent
    if delta >= 0:
        return [(" " * delta + line) if line.strip() else line for line in lines]
    return [line[-delta:] if line[:-delta].strip() == "" else line for line in lines]


class Target:
    def __init__(self, site: SiteConfig, key: str):
        self.site = site
        self.spec = site.targets[key]
        self.key = key

    # -- locating ---------------------------------------------------------------------------------------------

    def _entry_path(self, doc: Doc, object_id: str) -> list[Any] | None:
        node = doc.node(self.spec.path)
        if self.spec.field:
            if not isinstance(node, CommentedSeq):
                doc.seq_item(self.spec.path, self.spec.field, object_id)  # raises a LocateError
            for index, item in enumerate(node):
                if isinstance(item, dict) and item.get(self.spec.field) == object_id:
                    return [*self.spec.path, index]
            return None
        if doc.map_child(self.spec.path, object_id) is None:
            return None
        return [*self.spec.path, object_id]

    def _ids(self, doc: Doc) -> list[str]:
        node = doc.node(self.spec.path)
        if self.spec.field:
            return [i.get(self.spec.field) for i in node if isinstance(i, dict)] if isinstance(node, list) else []
        return list(node) if isinstance(node, dict) else []

    def names(self, text: str) -> set[str]:
        """BYOC names that have an entry in this file (by naming convention)."""
        if self.spec.id == "email_key":
            return set(self.owners(text))
        doc = Doc(text, self.spec.file)
        found = (self.site.naming.name_from(self.spec.id, str(i)) for i in self._ids(doc))
        return {name for name in found if name}

    def owners(self, text: str) -> dict[str, str]:
        """For the users file: BYOC name -> owner e-mail, from the keys byoc-sync generates."""
        doc = Doc(text, self.spec.file)
        owners: dict[str, str] = {}
        users = doc.node(self.spec.path)
        if not isinstance(users, dict):
            return owners
        for key, value in users.items():
            email = email_from_key(key)
            rules = value.get("rules") if isinstance(value, dict) else None
            if not email or not isinstance(rules, list):
                continue
            for rule in rules:
                name = self.site.naming.name_from("rule_id", str(rule.get("id"))) if isinstance(rule, dict) else None
                if name:
                    owners[name] = email
        return owners

    # -- planning ---------------------------------------------------------------------------------------------

    def plan(self, text: str, objects: dict[str, DesiredObject | Removal], file: str | None = None) -> FilePlan:
        file = file or self.spec.file
        doc = Doc(text, file)
        doc.node(self.spec.path)  # the collection must exist
        result = FilePlan(file=file, before=text, text=text)
        edits: list[Edit] = []
        inserts: list[str] = []
        expected: dict[str, Any] = {}

        for object_id, obj in objects.items():
            path = self._entry_path(doc, object_id)
            current = doc.node(path) if path else None
            if isinstance(obj, DesiredObject):
                if path is None:
                    inserts.append(obj.text)
                    expected[object_id] = obj.data
                    result.changes.append(ObjectChange(object_id, "create"))
                    continue
                try:
                    entry_edits, fields = self._update_edits(
                        doc, path, Doc(obj.text + "\n"), [0 if self.spec.field else object_id], obj.universe
                    )
                    want = merge(_plain(current), obj.data, obj.universe)
                except _Conflict as exc:
                    result.conflicts.append(Conflict(object_id, str(exc)))
                    continue
                if entry_edits:
                    edits += entry_edits
                    expected[object_id] = want
                    result.changes.append(ObjectChange(object_id, "update", sorted(set(fields))))
            elif isinstance(obj, Removal):
                if path is None:
                    continue
                rest = residual(_plain(current), obj.universe)
                if rest not in (None, {}, []) and self.spec.id == "email_key":
                    # The users entry is shared with whatever admins wrote for the same e-mail:
                    # remove only byoc-sync's rule and keep the rest.
                    partial: list[Edit] = []
                    removed: list[str] = []
                    self._remove_managed(doc, path, obj.universe, partial, removed)
                    if partial:
                        edits += partial
                        expected[object_id] = rest
                        result.changes.append(ObjectChange(object_id, "delete", sorted(set(removed))))
                    continue
                if rest not in (None, {}, []):
                    keys = ", ".join(sorted(map(str, rest))) if isinstance(rest, dict) else "list items"
                    result.conflicts.append(
                        Conflict(object_id, f"contains entries byoc-sync did not write ({keys}); remove it by hand")
                    )
                    continue
                edits.append(delete_edit(doc, doc.span_of(path)))
                expected[object_id] = None
                result.changes.append(ObjectChange(object_id, "delete"))
            else:
                raise TypeError(f"{object_id}: expected DesiredObject or Removal, got {type(obj).__name__}")

        if inserts:
            edits.append(self._insert_edit(doc, inserts))
        if not edits:
            return result
        result.text = apply(text, edits)
        self._verify(text, result.text, expected, file)
        return result

    def _insert_edit(self, doc: Doc, texts: list[str]) -> Edit:
        """One insertion holding every new entry, in the given order, spaced like the existing entries."""
        children = doc.children(self.spec.path)
        indent = children[0][1].indent if children else 0
        blocks = [_reindent(text.split("\n"), 0, indent) for text in texts]
        first = insert_after_edit(doc, self.spec.path, anchor=self._anchor(doc), lines=blocks[0], field=self.spec.field)
        separator = first.lines[: len(first.lines) - len(blocks[0])]
        lines = first.lines + [line for block in blocks[1:] for line in separator + block]
        if first.start >= len(doc.lines):
            # Inserting after the last line of a file without a final newline: add one, or the new block's last
            # line (often a block scalar) would end the file without its newline.
            lines.append("")
        return Edit("insert", first.start, first.end, lines)

    def _anchor(self, doc: Doc) -> str:
        """Prefer placing new entries after existing BYOC entries; otherwise use the site's anchor."""
        ours = [str(i) for i in self._ids(doc) if i is not None and self._is_ours(str(i))]
        if ours:
            return "^(" + "|".join(re.escape(i) for i in ours) + ")$"
        return self.spec.anchor

    def _is_ours(self, object_id: str) -> bool:
        if self.spec.id == "email_key":
            return email_from_key(object_id) is not None
        return self.site.naming.name_from(self.spec.id, object_id) is not None

    def _update_edits(
        self, doc: Doc, cpath: list[Any], ddoc: Doc, dpath: list[Any], universe: Any
    ) -> tuple[list[Edit], list[str]]:
        edits: list[Edit] = []
        fields: list[str] = []
        self._walk(doc, cpath, ddoc, dpath, universe, edits, fields)
        return edits, fields

    def _walk(
        self,
        doc: Doc,
        cpath: list[Any],
        ddoc: Doc,
        dpath: list[Any],
        universe: Any,
        edits: list[Edit],
        fields: list[str],
    ) -> None:
        current = doc.node(cpath)
        desired = ddoc.node(dpath)
        label = str(cpath[-1])
        if isinstance(current, (CommentedMap, CommentedSeq)) and doc.is_flow(cpath):
            # Flow style ("{a: 1}") cannot be edited line by line. Fine if nothing needs to change.
            if merge(_plain(current), _plain(desired), universe) != _plain(current):
                raise _Conflict(f"'{label}' is written in flow style; update it by hand")
            return
        if not isinstance(current, (CommentedMap, CommentedSeq)) and doc.is_flow(cpath[:-1]):
            if _plain(current) != _plain(desired):
                raise _Conflict(f"'{cpath[-2]}' is written in flow style; update it by hand")
            return
        if isinstance(desired, dict):
            if not isinstance(current, CommentedMap):
                raise _Conflict(f"'{label}' is not a mapping any more")
            new_lines: list[str] = []
            for key in desired:
                if key in current:
                    self._walk(
                        doc,
                        [*cpath, key],
                        ddoc,
                        [*dpath, key],
                        (universe or {}).get(key) if isinstance(universe, dict) else None,
                        edits,
                        fields,
                    )
                else:
                    new_lines += self._lines_for(doc, cpath, ddoc, [*dpath, key])
                    fields.append(str(key))
            for key in current:
                if key in desired or not isinstance(universe, dict) or key not in universe:
                    continue
                rest = residual(_plain(current[key]), universe[key])
                if rest in (None, {}, []):
                    span = doc.span_of([*cpath, key])
                    edits.append(Edit("delete", span.start, span.end))
                    fields.append(str(key))
                else:
                    self._remove_managed(doc, [*cpath, key], universe[key], edits, fields)
            if new_lines:
                end = self._content_end(doc, cpath)
                edits.append(Edit("insert", end, end, new_lines))
            return
        if isinstance(desired, list):
            if not isinstance(current, CommentedSeq):
                raise _Conflict(f"'{label}' is not a list any more")
            new_lines = []
            if _is_id_list(_plain(desired)):
                cur_ids = {item.get("id"): i for i, item in enumerate(current) if isinstance(item, dict)}
                u = {i["id"]: i for i in (universe or []) if isinstance(i, dict) and "id" in i}
                want = set()
                for index, item in enumerate(desired):
                    want.add(item["id"])
                    if item["id"] in cur_ids:
                        self._walk(
                            doc, [*cpath, cur_ids[item["id"]]], ddoc, [*dpath, index], u.get(item["id"]), edits, fields
                        )
                    else:
                        new_lines += self._item_lines(doc, cpath, ddoc, [*dpath, index])
                        fields.append(f"{label}[{item['id']}]")
                for item_id, index in cur_ids.items():
                    if item_id not in want and item_id in u and not residual(_plain(current[index]), u[item_id]):
                        span = doc.span_of([*cpath, index])
                        edits.append(Edit("delete", span.start, span.end))
                        fields.append(f"{label}[{item_id}]")
            else:
                plain_current = _plain(current)
                for index, item in enumerate(desired):
                    if _plain(item) not in plain_current:
                        new_lines += self._item_lines(doc, cpath, ddoc, [*dpath, index])
                        fields.append(label)
                managed = [i for i in (universe or []) if i not in _plain(desired)]
                for index, item in enumerate(plain_current):
                    if item in managed:
                        span = doc.span_of([*cpath, index])
                        edits.append(Edit("delete", span.start, span.end))
                        fields.append(label)
            if new_lines:
                end = self._content_end(doc, cpath)
                edits.append(Edit("insert", end, end, new_lines))
            return
        if isinstance(current, (CommentedMap, CommentedSeq)):
            raise _Conflict(f"'{label}' changed from a single value to a mapping or list")
        if _plain(current) != _plain(desired):
            span = doc.span_of(cpath)
            dspan = ddoc.span_of(dpath)
            lines = _reindent(ddoc.lines[dspan.start : dspan.end], dspan.indent, span.indent)
            edits.append(Edit("replace", span.start, span.end, lines))
            fields.append(label)

    def _remove_managed(self, doc: Doc, cpath: list[Any], universe: Any, edits: list[Edit], fields: list[str]) -> None:
        """Delete what byoc-sync wrote below ``cpath``, keeping everything else."""
        current = doc.node(cpath)
        if isinstance(current, CommentedMap) and isinstance(universe, dict):
            for key in current:
                if key in universe:
                    rest = residual(_plain(current[key]), universe[key])
                    if rest in (None, {}, []):
                        span = doc.span_of([*cpath, key])
                        edits.append(Edit("delete", span.start, span.end))
                        fields.append(str(key))
                    else:
                        self._remove_managed(doc, [*cpath, key], universe[key], edits, fields)
        elif isinstance(current, CommentedSeq) and isinstance(universe, list):
            ours = {i["id"]: i for i in universe if isinstance(i, dict) and "id" in i}
            for index, item in enumerate(current):
                plain_item = _plain(item)
                if isinstance(plain_item, dict):
                    mine = plain_item.get("id") in ours and not residual(plain_item, ours[plain_item["id"]])
                else:
                    mine = plain_item in universe
                if mine:
                    span = doc.span_of([*cpath, index])
                    edits.append(Edit("delete", span.start, span.end))
                    fields.append(
                        f"{cpath[-1]}[{plain_item.get('id') if isinstance(plain_item, dict) else plain_item}]"
                    )

    def _content_end(self, doc: Doc, cpath: list[Any]) -> int:
        node = doc.node(cpath)
        children = doc.children(cpath) if len(node) else []
        return children[-1][1].end if children else doc.span_of(cpath).end

    def _lines_for(self, doc: Doc, cpath: list[Any], ddoc: Doc, dpath: list[Any]) -> list[str]:
        children = doc.children(cpath)
        indent = children[0][1].indent if children else doc.span_of(cpath).indent + 2
        dspan = ddoc.span_of(dpath)
        return _reindent(ddoc.lines[dspan.start : dspan.end], dspan.indent, indent)

    def _item_lines(self, doc: Doc, cpath: list[Any], ddoc: Doc, dpath: list[Any]) -> list[str]:
        return self._lines_for(doc, cpath, ddoc, dpath)

    def _verify(self, before: str, after: str, expected: dict[str, Any], file: str) -> None:
        old = _without(Doc(before, file).data, self.spec, expected)
        new_doc = Doc(after, file)
        new = _without(new_doc.data, self.spec, expected)
        if old != new:
            where = ", ".join(_differences(old, new)[:5])
            raise VerificationError(f"{file}: content outside the BYOC entries changed ({where})")
        for object_id, want in expected.items():
            path = self._entry_path(new_doc, object_id)
            got = new_doc.node(path) if path else None
            if _plain(got) != want:
                raise VerificationError(f"{file}: entry {object_id} is not as planned after editing")


def _plain(value: Any) -> Any:
    return plain(value)


def _without(data: Any, spec: Any, ids: dict[str, Any]) -> Any:
    data = copy.deepcopy(data)
    node = data
    for part in spec.path:
        node = node[part]
    if spec.field:
        node[:] = [i for i in node if not (isinstance(i, dict) and i.get(spec.field) in ids)]
    else:
        for object_id in ids:
            node.pop(object_id, None)
    return data
