import pytest

from app.core.files import safe_filename
from app.core.ratelimit import RateLimiter


@pytest.mark.parametrize("raw,expected", [
    ("invoice.pdf", "invoice.pdf"),
    ("../../etc/passwd.pdf", "passwd.pdf"),
    ("..\\..\\windows\\system32\\x.xlsx", "x.xlsx"),
    ("/abs/path/a.csv", "a.csv"),
    ("a\x00b.pdf", "ab.pdf"),
    ("line\nbreak\r.pdf", "linebreak.pdf"),
    ("  spaced   out  .pdf ", "spaced out .pdf"),
    ("...hidden", "hidden"),
    ("", "upload"), (None, "upload"), ("../", "upload"), ("\x00\x01", "upload"),
    ("চালান.pdf", "চালান.pdf"),
])
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


def test_long_names_keep_their_extension():
    out = safe_filename("a" * 500 + ".pdf")
    assert len(out) == 200 and out.endswith(".pdf")


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def test_rate_limiter_blocks_then_recovers():
    clock = Clock()
    rl = RateLimiter(lambda: 3, 60, clock)
    assert rl.check("a", 2) is None and rl.check("a") is None
    wait = rl.check("a")
    assert wait is not None and 0 < wait <= 60
    clock.t = 61
    assert rl.check("a", 3) is None  # old hits aged out


def test_rate_limiter_is_per_key_and_all_or_nothing():
    rl = RateLimiter(lambda: 2, 60, Clock())
    assert rl.check("a", 2) is None
    assert rl.check("b", 2) is None  # another client is unaffected
    assert rl.check("a") is not None
    assert rl.check("c", 3) is not None and rl.check("c", 2) is None  # too-big request is refused whole, nothing counted


def test_limit_is_read_live():
    limit = [1]
    rl = RateLimiter(lambda: limit[0], 60, Clock())
    assert rl.check("a") is None and rl.check("a") is not None
    limit[0] = 5
    assert rl.check("a") is None
