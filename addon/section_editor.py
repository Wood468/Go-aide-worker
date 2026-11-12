"""Inactive bounded-section editing primitive; no file, shell or credential access.

Queue/publisher grants deliberately remain unchanged. Callers must bind this
contract to the approved task and independently check syntax before publication.
"""
import hashlib
import re

from direct_editor import generate

MAX_SOURCE_BYTES = 250_000
MAX_SECTION_BYTES = 40_000
SECRET_MARKERS = (b'github_pat_', b'ghp_', b'gho_', b'AIza',
                  b'-----BEGIN PRIVATE KEY-----')


class SectionEditError(Exception):
    pass


def locate(original, contract):
    """Resolve two explicit inclusive anchors against immutable UTF-8 bytes."""
    if (not isinstance(original, bytes) or not original
            or len(original) > MAX_SOURCE_BYTES or b'\0' in original):
        raise SectionEditError('Invalid section source')
    try:
        original.decode('utf-8')
    except UnicodeError:
        raise SectionEditError('Invalid section source') from None
    if not isinstance(contract, dict) or set(contract) != {'sha256', 'start', 'end'}:
        raise SectionEditError('Invalid section contract')
    digest = contract['sha256']
    if (not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest)
            or hashlib.sha256(original).hexdigest() != digest):
        raise SectionEditError('Section source changed')
    anchors = []
    for key in ('start', 'end'):
        text = contract[key]
        if not isinstance(text, str) or not 12 <= len(text) <= 512 or not text.strip():
            raise SectionEditError('Invalid section anchor')
        try:
            anchor = text.encode('utf-8')
        except UnicodeError:
            raise SectionEditError('Invalid section anchor') from None
        if original.count(anchor) != 1:
            raise SectionEditError('Section anchor missing or ambiguous')
        anchors.append(anchor)
    start = original.index(anchors[0])
    end_start = original.index(anchors[1])
    end = end_start + len(anchors[1])
    if end_start < start + len(anchors[0]) or end - start > MAX_SECTION_BYTES:
        raise SectionEditError('Invalid section bounds')
    return start, end


def verify(original, updated, contract):
    """Independently prove bytes outside the approved section are identical."""
    start, end = locate(original, contract)
    if (not isinstance(updated, bytes) or updated == original
            or len(updated) > MAX_SOURCE_BYTES or b'\0' in updated
            or not updated.startswith(original[:start])
            or not updated.endswith(original[end:])):
        raise SectionEditError('Section edit escaped bounds')
    tail = len(original) - end
    replacement = updated[start:len(updated) - tail if tail else len(updated)]
    if not replacement or len(replacement) > MAX_SECTION_BYTES:
        raise SectionEditError('Invalid section replacement')
    # Compare exact composition too: prefix/suffix must not overlap.
    if updated != original[:start] + replacement + original[end:]:
        raise SectionEditError('Section edit escaped bounds')
    try:
        replacement.decode('utf-8')
    except UnicodeError:
        raise SectionEditError('Invalid section replacement') from None
    if any(marker in replacement for marker in SECRET_MARKERS):
        raise SectionEditError('Unsafe section replacement')
    return True


def propose(key, prompt, path, original, contract, generator=generate):
    """Send only the selected section to the existing exact-patch generator."""
    start, end = locate(original, contract)
    section = original[start:end].decode('utf-8')
    replacement = generator(key, prompt, path, section)
    if not isinstance(replacement, str):
        raise SectionEditError('Invalid section replacement')
    try:
        raw = replacement.encode('utf-8')
    except UnicodeError:
        raise SectionEditError('Invalid section replacement') from None
    updated = original[:start] + raw + original[end:]
    verify(original, updated, contract)
    return updated
