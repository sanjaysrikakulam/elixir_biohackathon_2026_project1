"""Line-preserving edits of YAML files that may contain Jinja2.

The playbook's files are maintained by many people, so byoc-sync never re-serialises them. Instead:

1. :func:`~.mask.mask` hides Jinja so the text parses as YAML without moving any line;
2. :class:`~.doc.Doc` parses the masked text and reports the exact line span of any entry;
3. :mod:`~.edit` inserts, replaces or deletes whole lines of the original text;
4. :func:`~.verify.verify` re-parses the result and checks that only the intended change happened.
"""
