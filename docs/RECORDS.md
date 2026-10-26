# Verification records

This source distribution does not include dated live-run receipts,
screenshots, or internal deployment logs. They belonged to the private
deployment this code was extracted from, and they contain repository names and
paths that are not part of the public project.

The public verification is:

- the offline test suite (`PYTHONPATH=src python3 -m unittest discover -s tests -t .`);
- the continuous integration workflow in `.github/workflows/tests.yml`;
- the example configuration and the scoped dry-run runner.

If you deploy FatChin, keep your own records. A dry run's `dry_run_actions`
field and the report written to your selected folder are the intended evidence
for what a fleet would have done.
