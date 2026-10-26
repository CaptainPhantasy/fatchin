#!/usr/bin/env python3
"""Offline dry-run demo: real Git and the real worker, simulated GitHub.

Run from the repository root:

    PYTHONPATH=src python3 scripts/demo-offline.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import test_fleet_runner as fixture


def main() -> int:
    case = fixture.FleetRunnerTests('test_dry_run_review_records_the_write_and_sends_nothing')
    case.setUp()
    try:
        item = case.item()
        item['fleet'] = {**item['fleet'], 'dry_run': True}
        finding = {'title': 'Wrong result',
                   'body': 'Return zero for the documented case.',
                   'path': 'calc.py', 'line': 2}
        result, _, _ = case.execute(item, [fixture.finish(findings=[finding])])
        actions = result.get('dry_run_actions') or []

        print('FatChin — dry run')
        print('real Git checkout · real review worker · simulated GitHub')
        print('=' * 58)
        print(f"repository    {item['repo']}")
        print('mode          DRY RUN')
        print(f"result        {result.get('status')}")
        print(f"findings      {len(result.get('findings') or [])}")
        for action in actions:
            body = str((action.get('body') or {}).get('body') or '')
            target = action.get('path') or action.get('branch') or action.get('repo') or ''
            print(f"would send    {str(action.get('kind')).upper()} {target}")
            first = next((line for line in body.splitlines()
                          if line.strip() and not line.lstrip().startswith('<!--')), '')
            if first:
                print(f"              {first[:72]}")
        print(f"github writes {len(case.gh.writes)}")
        print(f"git pushes    {len(case.pushes)}")
        print('—' * 58)
        print('Nothing was commented, pushed, or merged.')
        return 0
    finally:
        case.doCleanups()


if __name__ == '__main__':
    sys.exit(main())
