import pytest


def pytest_collection_modifyitems(items):
    for item in items:
        if "asyncio" not in item.keywords and getattr(item.obj, "__code__", None) and item.obj.__code__.co_flags & 0x80:
            item.add_marker(pytest.mark.asyncio)
