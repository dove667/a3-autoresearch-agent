"""Repair tool-call JSON that leaked protocol delimiter fragments.

A model occasionally writes an adjacent protocol marker into the JSON string it
sends as tool arguments, for example an object whose array is followed by a stray
end tag and a leftover ": 1800}". Those markers are not JSON, so the call is
rejected and the same request tends to reproduce the same mistake. The markers
are unambiguous, so removing them before parsing recovers the intended object; a
genuine syntax error still fails and is reported exactly as before.
"""

import json
import re

PIPE = "\uff5c"
# Provider protocol delimiters, including the pipe-wrapped DSML form.
DELIMITERS = re.compile(
    r"</?parameter[^>]*>" +
    r"|<" + PIPE + r"/?DSML" + PIPE + r"[^>]*>" +
    r"|<" + PIPE + r"DSML" + PIPE,

)
TRAILING = re.compile(r'"\s*:\s*\d+\s*}\s*$')


def cleaned(text):
    """Drop protocol markers that may sit inside the JSON text."""
    return DELIMITERS.sub("", text)


def parsed(text):
    """Return whatever the text encodes, exactly like json.loads."""
    return json.loads(text)


def loads(text):
    """Parse tool arguments, repairing leaked delimiters once before failing."""
    try:
        return parsed(text)
    except (ValueError, TypeError):
        repaired = cleaned(text)
        if repaired == text:
            raise
        return parsed(repaired)
