from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent


@lru_cache(maxsize=None)
def load_prompt(name: str) -> tuple[str, str]:
    """Return (system, user_template) from prompts/<name>.txt.

    The file has two sections introduced by `### SYSTEM` and `### USER`.
    The user section contains the placeholder {{DOCUMENT_TEXT}}.
    """
    raw = (_DIR / f"{name}.txt").read_text(encoding="utf-8")
    _, _, rest = raw.partition("### SYSTEM")
    system, _, user = rest.partition("### USER")
    system, user = system.strip(), user.strip()
    if not system or "{{DOCUMENT_TEXT}}" not in user:
        raise ValueError(f"prompt {name!r} must have ### SYSTEM and ### USER sections with {{DOCUMENT_TEXT}}")
    return system, user
