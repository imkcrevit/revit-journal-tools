import multiprocessing

from .cli import main

# Guarded so that Windows "spawn" child processes never re-run the CLI;
# freeze_support() is a no-op unless the tool is packaged as a frozen executable.
if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
