"""The two owner-selected fleet providers; no silent model substitution."""
from __future__ import annotations

import sys
from dataclasses import replace

from .config_loader import model_auth_problems

PROVIDERS = (
    {'id': 'glm', 'label': 'GLM-5.3-flash', 'model_id': 'your-model', 'limit': 10},
    {'id': 'deepseek', 'label': 'DeepSeek V4.1 Flash 1017', 'model_id': 'your-second-model', 'limit': 10,
     'context_window': 1_000_000},
)


def model_config(cfg, env, provider):
    """Return a private runtime copy, leaving saved settings and keys intact.

    Fleet work is continuous. Its own ledger counts usage without the legacy
    daily stop; every individual task retains a bounded call allowance.
    """
    values = dict(env)
    if provider == 'glm':
        if cfg.model.model_id != 'your-model':
            raise ValueError('The GLM fleet requires GLM-5.3-flash. Restore that model in Settings.')
        model = cfg.model
    elif provider == 'deepseek':
        if values.get('MCP_SECRET_BROKER_ADMIN_TOKEN'):
            model = replace(cfg.model, model_id='your-second-model', auth_mode='broker-lease',
                            broker_server_id='deepseek', disable_reasoning=False,
                            base_url=cfg.model.broker_base_url.rstrip('/') + '/proxy/deepseek')
        elif values.get('DEEPSEEK_API_KEY'):
            values['GHA_MODEL_API_KEY'] = values['DEEPSEEK_API_KEY']
            model = replace(cfg.model, model_id='your-second-model', auth_mode='env',
                            base_url='https://api.deepseek.com', disable_reasoning=False)
        else:
            raise ValueError('DeepSeek is not connected. Supply the existing broker connection or a dedicated DEEPSEEK_API_KEY in the worker environment.')
    else:
        raise ValueError('Choose the GLM or DeepSeek fleet pool.')
    resolved = replace(cfg, model=replace(model, max_calls_per_day=sys.maxsize,
                                          max_calls_per_job=min(100, max(1, model.max_calls_per_job))))
    problems = model_auth_problems(resolved, values)
    if problems:
        raise ValueError(' '.join(problems))
    return resolved, values


def provider_status(cfg, env):
    """Configuration status only: this does not send model or broker requests."""
    result = []
    for spec in PROVIDERS:
        row = {**spec, 'available': False, 'error': '', 'status': 'not-configured'}
        try:
            resolved, _ = model_config(cfg, env, spec['id'])
            row.update(available=True, status='configured', model_id=resolved.model.model_id)
        except ValueError as exc:
            row['error'] = str(exc)
        result.append(row)
    return result


def fleet_startup_available(cfg, env):
    """A saved active fleet may start without the ordinary GLM worker's key."""
    from pathlib import Path
    from .fleet_store import FleetStore
    if not cfg.state_dir:
        return False
    available = {p['id'] for p in provider_status(cfg, env) if p['available']}
    return any(f['status'] in {'running', 'paused'} and
               ((f.get('workers', 0) and 'glm' in available) or
                (f.get('deepseek_workers', 0) and 'deepseek' in available))
               for f in FleetStore(Path(cfg.state_dir) / 'fleet-v1').internal()['fleets'].values())
