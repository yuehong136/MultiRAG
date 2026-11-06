"""Display names shared by registration and profile updates.

Length counts Unicode code points, matching Array.from(name).length in Web.
The trim set matches ECMAScript String.trim rather than Python's wider strip.
"""

from api.constants import NICKNAME_MAX_LENGTH

_NICKNAME_TRIM = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"


def normalize_nickname(value: object) -> str:
    """Return the trimmed name or reject it before persistence or password IO."""
    if not isinstance(value, str):
        raise ValueError("Nickname must be a string.")
    nickname = value.strip(_NICKNAME_TRIM)
    if not nickname:
        raise ValueError("Nickname cannot be empty.")
    if len(nickname) > NICKNAME_MAX_LENGTH:
        raise ValueError(f"Nickname must be at most {NICKNAME_MAX_LENGTH} characters.")
    if any(not (character.isalnum() or character in " ._'-") for character in nickname):
        raise ValueError("Nickname contains invalid characters.")
    return nickname
