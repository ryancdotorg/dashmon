from . import *

"""Import all modules that exist in the current directory."""
# Ref https://rya.nc/so/60861023
from importlib import import_module
from pathlib import Path

for f in Path(__file__).parent.glob("*.py"):
    module_name = f.stem
    if (not module_name.startswith("_")) and (module_name not in globals()):
        module = import_module(f".{module_name}", __package__)
        if hasattr(module, '__all__'):
            for k in module.__all__:
                globals()[k] = getattr(module, k)

    del f, module_name
del import_module, Path
