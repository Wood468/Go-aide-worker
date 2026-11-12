"""Shared byte-level checks for generated UTF-8 text; no execution or I/O."""


def unsafe_controls(data):
    # JSON can decode a model's mistaken regex escape into a literal backspace.
    # Only ordinary text whitespace is allowed among C0 controls; reject DEL.
    return any((byte < 32 and byte not in (9, 10, 13)) or byte == 127 for byte in data)
