"""Inactive bounded-section editing primitive; no file, shell or credential access.

Queue/publisher grants deliberately remain unchanged. Callers must bind this
contract to the approved task and independently check syntax before publication.
"""
import hashlib
import re

from direct_editor import generate
from text_safety import unsafe_controls

MAX_SOURCE_BYTES = 250_000
MAX_SECTION_BYTES = 40_000
SECRET_MARKERS = (b'github_pat_', b'ghp_', b'gho_', b'AIza',
                  b'-----BEGIN PRIVATE KEY-----')


class SectionEditError(ValueError):
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
        # bytes.count skips overlapping matches; every possible start must be unique.
        first = original.find(anchor)
        if first < 0 or original.find(anchor, first + 1) >= 0:
            raise SectionEditError('Section anchor missing or ambiguous')
        anchors.append(anchor)
    start = original.index(anchors[0])
    end_start = original.index(anchors[1])
    end = end_start + len(anchors[1])
    if end_start < start + len(anchors[0]) or end - start > MAX_SECTION_BYTES:
        raise SectionEditError('Invalid section bounds')
    if unsafe_controls(original[start:end]) or any(marker in original[start:end] for marker in SECRET_MARKERS):
        raise SectionEditError('Unsafe section context')
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
    if not replacement or len(replacement) > MAX_SECTION_BYTES or unsafe_controls(replacement):
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


def task_contract(contract, expected_path):
    """Validate an inactive v1 section request; live queue accepts no such schema."""
    from queue_scope import SOURCE_TEST_PAIRS, SECTION_SOURCE_TEST_PAIRS
    fields = {'version', 'path', 'instruction', 'sha256', 'start', 'end'}
    if (not isinstance(contract, dict) or set(contract) != fields
            or type(contract['version']) is not int or contract['version'] != 1
            or not isinstance(expected_path, str)
            or expected_path not in {pair[0] for pair in SOURCE_TEST_PAIRS | SECTION_SOURCE_TEST_PAIRS}
            or contract['path'] != expected_path):
        raise SectionEditError('Invalid section task contract')
    if (not isinstance(contract['sha256'], str)
            or not re.fullmatch('[a-f0-9]{64}', contract['sha256'])
            or any(not isinstance(contract[k], str) or not 12 <= len(contract[k]) <= 512
                   or not contract[k].strip() for k in ('start', 'end'))):
        raise SectionEditError('Invalid section task bounds')
    instruction = contract['instruction']
    if not isinstance(instruction, str) or not 20 <= len(instruction) <= 1500:
        raise SectionEditError('Invalid section instruction')
    try:
        raw = instruction.encode('utf-8')
    except UnicodeError:
        raise SectionEditError('Invalid section instruction') from None
    if (not instruction.strip() or unsafe_controls(raw)
            or any(marker in raw for marker in SECRET_MARKERS)):
        raise SectionEditError('Unsafe section instruction')
    return {key: contract[key] for key in ('sha256', 'start', 'end')}


def propose_task(key, expected_path, original, contract, generator=generate):
    """Use the approved instruction and path, with no caller prompt override."""
    bounds = task_contract(contract, expected_path)
    return propose(key, contract['instruction'], expected_path, original, bounds, generator)


def verify_task(original, updated, contract, expected_path):
    """Independent publication-side byte check; never invokes the generator."""
    return verify(original, updated, task_contract(contract, expected_path))
