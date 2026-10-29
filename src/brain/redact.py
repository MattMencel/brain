"""Strip secrets before anything is stored or sent to the summarizer."""

import re

MASK = "[REDACTED]"

_PATTERNS = [
    # PEM private keys
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    # Provider token formats
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    # Credentials embedded in URIs: scheme://user:pass@host
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),
]

# key=value / key: value / "key": "value" where the key names a secret
_KEYED = re.compile(
    r"""(?ix)
    (\b[\w.\-]*(?:password|passwd|pwd|secret|token|api[_\-]?key|access[_\-]?key|private[_\-]?key
       |accountkey|sharedaccesskey|client[_\-]?secret|sig)\b["']?\s*[:=]\s*)
    (["']?)([^\s"',;&]{6,})\2
    """
)


def redact(text: str) -> str:
    for pattern in _PATTERNS:
        text = pattern.sub(MASK, text)
    return _KEYED.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}{m.group(2)}", text)
