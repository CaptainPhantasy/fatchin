"""Test package marker for gha-worker's core component.

Required by `python -m unittest discover -s tests -t .`: Python 3.11+ discovery
refuses a start directory that is not an importable package. Also makes
`from tests.mock_github import make_client` work for other components' tests.
"""
