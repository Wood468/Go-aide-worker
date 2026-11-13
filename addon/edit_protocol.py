"""Finite, content-free editor receipts shared across privilege boundaries."""
CORRECTABLE = frozenset({'format-envelope', 'format-text', 'format-json',
                         'format-path', 'format-shape', 'format-patch', 'anchor'})
TERMINAL = frozenset({'provider-block', 'provider-error', 'output-limit'})
SUCCESS = b'Direct Gemini selected-file edit completed'
PREFIX = b'WORKER_EDITOR_CORRECTION:'
ERROR_PREFIX = b'Direct Gemini edit rejected: '


def marker(category):
    if category not in CORRECTABLE:
        raise ValueError('Invalid correction category')
    return PREFIX + category.encode('ascii')


def parse_output(raw, failures, success=False):
    """Exactly one final line and optionally one trusted correction marker."""
    if not isinstance(raw, bytes):
        raise ValueError('Invalid editor receipt')
    lines = raw.removesuffix(b'\n').split(b'\n')
    correction = None
    if len(lines) == 2:
        correction = next((c for c in CORRECTABLE if lines[0] == marker(c)), None)
        if correction is None:
            raise ValueError('Invalid editor receipt')
        lines = lines[1:]
    if len(lines) != 1 or (success and lines[0] != SUCCESS) or (not success and lines[0] not in failures):
        raise ValueError('Invalid editor receipt')
    return correction


def validate_history(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 2:
        raise ValueError('Invalid correction history')
    stages = []
    for entry in value:
        if (not isinstance(entry, dict) or set(entry) != {'stage', 'category'}
                or not isinstance(entry['stage'], str) or entry['stage'] not in ('edit-source', 'edit-test')
                or not isinstance(entry['category'], str) or entry['category'] not in CORRECTABLE):
            raise ValueError('Invalid correction history')
        stages.append(entry['stage'])
    if stages not in (['edit-source'], ['edit-test'], ['edit-source', 'edit-test']):
        raise ValueError('Invalid correction history')
    return value
