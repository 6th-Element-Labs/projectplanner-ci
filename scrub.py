"""Secret scrubbing for the Live Inbox (fail-and-fix-early — see docs/AGENT_ROADMAP.md).

Inbound email/transcript bodies can carry credentials in the clear (a zip password, an
EntraID client secret, an API/subscription key). Those must NEVER land in the persistent
RAG corpus or be handed back to the LLM as a value it could echo. `redact()` masks the
VALUE while keeping the LABEL, so the agent still knows "a password was provided" and can
act on it (route it, ask for a secure channel) without ever seeing or storing the secret.

Conservative by design: we only redact a value that sits right after a secret-indicating
label (password / secret / client secret / api-key / subscription-key / token / credential)
and is long enough (>=6 chars) to be a real secret — so ordinary prose ("the secret sauce")
AND non-secret identifiers the agent legitimately needs (a tenant GUID, an asset id, a URL)
are left untouched. Over-redacting real content would itself be a masked-placeholder bug.
"""
import re

# label  [up to 40 non-newline chars, e.g. "for protected file "]  separator(: = -)  VALUE
_LABELED = re.compile(
    r"(?im)\b(passwords?|passwd|pwd|secrets?(?:\s+values?)?|client\s+secret|"
    r"api[-_ ]?keys?|subscription[-_ ]?keys?|access[-_ ]?tokens?|tokens?|credentials?)\b"
    r"([^\n]{0,40}?)"          # optional words between label and separator
    r"([:=\-]\s*)"             # separator
    r"([^\s,;]{6,})"           # the secret value (>=6 non-space chars)
)

REDACTED = "[REDACTED]"


def redact(text):
    """Return (scrubbed_text, count). Masks secret VALUES, keeps labels for context."""
    if not text:
        return text, 0
    n = [0]

    def _labeled(m):
        n[0] += 1
        return f"{m.group(1)}{m.group(2)}{m.group(3)}{REDACTED}"

    out = _LABELED.sub(_labeled, text)
    return out, n[0]
