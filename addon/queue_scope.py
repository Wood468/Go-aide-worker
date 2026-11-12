"""Explicit queue file allowlists, shared by startup, dispatch and publication."""
import json
import re

TEST_PATH = re.compile(r'test/[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\.test\.mjs')
DOC_PATH = re.compile(r'docs/worker-task-[a-z0-9-]{1,50}\.md')
SOURCE_TEST_PAIRS = {
    ('public/event-components.js', 'test/event-components-crowned-alias.test.mjs'),
    ('public/today.js', 'test/today-logic.test.mjs'),
    ('public/detail.js', 'test/event-detail-source-confidence.test.mjs'),
    ('public/app.js', 'test/universal-event.test.mjs'),
}
SOURCE_PATHS = {pair[0] for pair in SOURCE_TEST_PAIRS} | {'public/index.html', 'public/sw.js'}


class ScopeError(ValueError):
    pass


def validate(paths, unattended=False):
    if (not isinstance(paths, list) or not 1 <= len(paths) <= 5
            or not all(isinstance(p, str) and len(p) <= 180 for p in paths)
            or len(set(paths)) != len(paths)):
        raise ScopeError('Invalid queue file allowlist')
    for path in paths:
        if any(term in path.lower() for term in ("token", "password", "secret", "credential", "private_key")):
            raise ScopeError("Disallowed queue allowlist path")
        if TEST_PATH.fullmatch(path):
            continue
        if unattended or not (DOC_PATH.fullmatch(path) or path in SOURCE_PATHS):
            raise ScopeError('Disallowed queue allowlist path')
    return list(paths)


def parse(raw, unattended=False):
    if not isinstance(raw, str) or len(raw) > 1200:
        raise ScopeError('Missing queue file allowlist')
    try:
        paths = json.loads(raw)
    except ValueError:
        raise ScopeError('Invalid queue file allowlist') from None
    return validate(paths, unattended)


def paths_of(task):
    return list(task['paths']) if 'paths' in task else [task['path']]


def permits(task, paths, unattended=False):
    return (not unattended or 'version' not in task) and set(paths_of(task)).issubset(paths)
