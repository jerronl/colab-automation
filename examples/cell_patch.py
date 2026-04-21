from colab_automation import CellPatch

# Replace first matching line — exact string replacement
CellPatch(pattern=r"^VERSION = .+$", replace='VERSION = "v2"')

# Replace using a function
CellPatch(pattern=r"^SEED = \d+$", replace_fn=lambda s: s.replace("42", "99"))

# Comment out a line
CellPatch(pattern=r"^run_test = False$", replace="# run_test = False")
