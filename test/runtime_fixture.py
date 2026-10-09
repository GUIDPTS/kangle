"""Copy optional private C++ runtimes into isolated test prefixes."""
import os
import shutil


def copy_runtime(binary, bindir):
    for name in ('libstdc++.so.6', 'libgcc_s.so.1'):
        source = os.path.join(os.path.dirname(binary), name)
        if os.path.isfile(source):
            shutil.copy2(source, os.path.join(bindir, name))
