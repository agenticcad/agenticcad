import os

os.environ.setdefault("AGENTICCAD_EDITION", "tabs")

from agenticcad.app import main  # noqa: E402

if __name__ == "__main__":
    main()
