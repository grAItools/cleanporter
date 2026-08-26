"""Classifier behaviour against the real standard library."""

from __future__ import annotations

from cleanporter._probe import classify


def test_submodule_of_package_is_module():
    # os.path is a module attribute of the (non-package) os module
    assert classify("os", "path") is True
    # collections.abc is a real submodule of the collections package
    assert classify("collections", "abc") is True


def test_object_from_module_is_violation():
    assert classify("functools", "partial") is False
    assert classify("collections", "OrderedDict") is False
    assert classify("os", "getcwd") is False
    assert classify("os.path", "join") is False


def test_unimportable_parent_is_unknown():
    assert classify("this_module_does_not_exist_xyz", "thing") is None
