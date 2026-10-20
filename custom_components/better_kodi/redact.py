"""Keep secrets out of logs: links may carry API keys or passwords."""

import re

SECRET_PARAMS = re.compile(
    r"(?i)([?&](?:api_key|apikey|x-emby-token|x-mediabrowser-token|token|access_token)=)[^&#\s]*"
)
USER_INFO = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/@\s]+@")


def redact_url(text: str | None) -> str:
    """Return the text with API keys and user:password@ in links replaced."""
    if not text:
        return text or ""
    return USER_INFO.sub(r"\1***@", SECRET_PARAMS.sub(r"\1***", text))
