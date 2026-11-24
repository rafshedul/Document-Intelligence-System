import re
import unicodedata

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def safe_filename(name: str | None, max_length: int = 200) -> str:
    """A display-safe version of an uploaded filename.

    Keeps only the last path component (so `../../etc/passwd.pdf` becomes `passwd.pdf`), drops control
    characters, and caps the length while keeping the extension. The name is only for display: files are
    stored under a random name, so it never touches the filesystem path.
    """
    name = unicodedata.normalize("NFC", name or "")
    name = _CONTROL.sub("", name.replace("\\", "/")).split("/")[-1].strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    if not name:
        return "upload"
    if len(name) > max_length:
        stem, dot, ext = name.rpartition(".")
        keep = max_length - len(ext) - 1
        name = (stem[:keep] + "." + ext) if dot and len(ext) < 16 and keep > 0 else name[:max_length]
    return name
