# tests/test_notebook_utils.py
import json
import os
import pytest
from colab_automation.notebook_utils import apply_patches_to_notebook
from colab_automation.config import CellPatch
from colab_automation.session import CellPatchError


def _make_nb(cells: list) -> dict:
    """Build minimal notebook JSON with given cell sources (str or list[str])."""
    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {},
        "cells": [
            {"cell_type": "code", "source": src, "metadata": {}, "outputs": []}
            for src in cells
        ],
    }


@pytest.fixture
def nb_file(tmp_path):
    """Factory: write a notebook JSON to a temp file, return its path."""
    def _make(cells):
        path = tmp_path / "test.ipynb"
        path.write_text(json.dumps(_make_nb(cells)), encoding="utf-8")
        return str(path)
    return _make


def test_patch_string_source(nb_file):
    path = nb_file(["x = 1\ny = 2"])
    tmp = apply_patches_to_notebook(path, [CellPatch(pattern=r"x = 1", replace="x = 99")])
    try:
        nb = json.loads(open(tmp, encoding="utf-8").read())
        assert "x = 99" in nb["cells"][0]["source"]
        assert "y = 2" in nb["cells"][0]["source"]
    finally:
        os.unlink(tmp)


def test_patch_list_source_preserves_format(nb_file):
    path = nb_file([["x = 1\n", "y = 2"]])
    tmp = apply_patches_to_notebook(path, [CellPatch(pattern=r"x = 1", replace="x = 99")])
    try:
        nb = json.loads(open(tmp, encoding="utf-8").read())
        src = nb["cells"][0]["source"]
        assert isinstance(src, list), "list source should stay as list"
        assert "x = 99" in "".join(src)
    finally:
        os.unlink(tmp)


def test_no_match_raises_cell_patch_error(nb_file):
    path = nb_file(["hello = 1"])
    with pytest.raises(CellPatchError, match="matched no cell"):
        apply_patches_to_notebook(path, [CellPatch(pattern=r"not_there", replace="x")])


def test_multiple_patches_applied_in_order(nb_file):
    path = nb_file(["a = 1", "b = 2"])
    tmp = apply_patches_to_notebook(path, [
        CellPatch(pattern=r"a = 1", replace="a = 10"),
        CellPatch(pattern=r"b = 2", replace="b = 20"),
    ])
    try:
        nb = json.loads(open(tmp, encoding="utf-8").read())
        assert nb["cells"][0]["source"] == "a = 10"
        assert nb["cells"][1]["source"] == "b = 20"
    finally:
        os.unlink(tmp)


def test_original_notebook_untouched(nb_file):
    path = nb_file(["x = 1"])
    before = json.loads(open(path, encoding="utf-8").read())
    tmp = apply_patches_to_notebook(path, [CellPatch(pattern=r"x = 1", replace="x = 2")])
    try:
        after = json.loads(open(path, encoding="utf-8").read())
        assert after == before, "original file must not be modified"
    finally:
        os.unlink(tmp)


def test_patch_matches_first_cell_only(nb_file):
    """Patch applies only to the first matching cell, not all of them."""
    path = nb_file(["x = 1", "x = 1"])
    tmp = apply_patches_to_notebook(path, [CellPatch(pattern=r"x = 1", replace="x = 99")])
    try:
        nb = json.loads(open(tmp, encoding="utf-8").read())
        assert nb["cells"][0]["source"] == "x = 99"
        assert nb["cells"][1]["source"] == "x = 1"  # second cell untouched
    finally:
        os.unlink(tmp)


def test_replace_fn_applied_locally(nb_file):
    path = nb_file(["SEED = 42"])
    tmp = apply_patches_to_notebook(path, [
        CellPatch(
            pattern=r"SEED = \d+",
            replace_fn=lambda s: s.replace("SEED = 42", "SEED = 99"),
        )
    ])
    try:
        nb = json.loads(open(tmp, encoding="utf-8").read())
        assert nb["cells"][0]["source"] == "SEED = 99"
    finally:
        os.unlink(tmp)
