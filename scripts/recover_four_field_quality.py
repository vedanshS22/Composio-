"""Compatibility entry point for the generic quality-research runner.

The historic file name is retained for shell-history compatibility only. It
has no four-app allowlist; use ``run_quality_research.py`` for new runs.
"""
from scripts.run_quality_research import main


if __name__ == "__main__":
    main()
