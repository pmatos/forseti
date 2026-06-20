"""Enable ``python -m forseti.esbmc <file>`` — delegates to the CLI."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
