import pytest

from manyeyes.updates import is_newer, parse


@pytest.mark.parametrize("a,b,newer", [
    ("0.2.0", "0.1.0", True), ("0.1.0", "0.1.0.dev0", True), ("0.1.0rc1", "0.1.0", False),
    ("v1.0.0", "0.9.9", True), ("0.1.0", "0.1.1", False), ("garbage", "0.1.0", False),
])
def test_is_newer(a, b, newer):
    assert is_newer(a, b) is newer


def test_parse_orders_prereleases():
    assert parse("1.0.0.dev0") < parse("1.0.0a1") < parse("1.0.0rc2") < parse("1.0.0")
