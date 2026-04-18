import re
import pytest
from colab_automation.config import CellPatch, RunConfig


class TestCellPatch:
    def test_static_replace(self):
        patch = CellPatch(pattern=r"^run_test = False$", replace="# run_test = False")
        result = patch.apply("run_test = True\nrun_test = False\n")
        assert result == "run_test = True\n# run_test = False\n"

    def test_replace_fn_takes_precedence_over_replace(self):
        # replace_fn wins when both are provided
        patch = CellPatch(
            pattern=r"^VERSION = .+$",
            replace='VERSION = "static"',
            replace_fn=lambda s: s.replace('VERSION = "old"', 'VERSION = "dynamic"'),
        )
        result = patch.apply('VERSION = "old"\n')
        assert result == 'VERSION = "dynamic"\n'

    def test_no_match_returns_original(self):
        patch = CellPatch(pattern=r"^NONEXISTENT = .+$", replace="x = 1")
        original = "a = 1\nb = 2\n"
        assert patch.apply(original) == original

    def test_multiline_flag_default(self):
        # Default flags=re.MULTILINE — ^ and $ match line boundaries
        patch = CellPatch(pattern=r"^N = \d+$", replace="N = 99")
        result = patch.apply("N = 1\nM = 2\n")
        assert result == "N = 99\nM = 2\n"

    def test_replace_fn_receives_full_source(self):
        received = []
        def capturer(src):
            received.append(src)
            return src
        patch = CellPatch(pattern=r"^X = \d+$", replace_fn=capturer)
        source = "X = 1\nY = 2\n"
        patch.apply(source)
        assert received == [source]


class TestRunConfig:
    def test_defaults(self):
        cfg = RunConfig(notebook_id="abc123")
        assert cfg.authuser == "0"
        assert cfg.cdp_port == 9223
        assert cfg.cell_patches == []
        assert cfg.pivot_cell_pattern is None
        assert cfg.dense_interval == 1.0
        assert cfg.sparse_interval == 5.0
        assert cfg.output_extractor is None
        assert cfg.output_path is None
