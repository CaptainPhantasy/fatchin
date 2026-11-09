"""The two owner-selected fleet providers; no silent model substitution."""
from __future__ import annotations

from dataclasses import replace

from .config_loader import model_auth_problems

# Fleet work runs unattended, so the daily ceiling can be lowered by the
# operator but never removed. A fleet that spends without a limit is a fleet
# nobody can leave alone.
FLEET_DAILY_CALLS_MAX = 25000


def fleet_daily_ceiling(configured):
    """Never unlimited: clamp the operator's ceiling to the hard maximum."""
    return max(1, min(int(configured or FLEET_DAILY_CALLS_MAX), FLEET_DAILY_CALLS_MAX))


GLM_MODEL_ID = 'glm-5.3-flash'
DEEPSEEK_MODEL_ID = 'deepseek-flash'

PROVIDERS = (
    {'id': 'glm', 'label': 'GLM-5.3-flash', 'model_id': GLM_MODEL_ID, 'limit': 10},
    {'id': 'deepseek', 'label': 'DeepSeek V4.1 Flash 1017', 'model_id': DEEPSEEK_MODEL_ID, 'limit': 10,
     'context_window': 1_000_000},
)


def model_config(cfg, env, provider):
    """Return a private runtime copy, leaving saved settings and keys intact.

    Each provider has its own daily ledger. An optional per-provider override
    replaces the model default, while the hard daily ceiling always applies.
    Every individual task also retains a bounded call allowance.
    """
    values = dict(env)
    if provider == 'glm':
        if cfg.model.model_id != GLM_MODEL_ID:
            raise ValueError('The GLM fleet requires GLM-5.3-flash. Restore that model in Settings.')
        model = cfg.model
    elif provider == 'deepseek':
        selected = str(values.get('FATCHIN_DEEPSEEK_AUTH_MODE') or '').strip()
        if selected not in {'', 'env', 'broker-lease'}:
            raise ValueError('DeepSeek has an unknown credential choice. Open Settings and choose its dedicated key or company broker connection.')
        if selected == 'env' and not values.get('DEEPSEEK_API_KEY'):
            raise ValueError('DeepSeek is set to use a dedicated key, but no DeepSeek key is saved. Store that key in Settings, or choose the existing company broker connection.')
        if selected == 'broker-lease' and not values.get('MCP_SECRET_BROKER_ADMIN_TOKEN'):
            raise ValueError('DeepSeek is set to use the company broker, but its credential is missing. Store a broker credential in Settings, or choose a saved dedicated DeepSeek key.')
        if selected == 'broker-lease' or (not selected and values.get('MCP_SECRET_BROKER_ADMIN_TOKEN')):
            model = replace(cfg.model, model_id=DEEPSEEK_MODEL_ID, auth_mode='broker-lease',
                            broker_server_id='deepseek', disable_reasoning=False,
                            base_url=cfg.model.broker_base_url.rstrip('/') + '/proxy/deepseek')
        elif selected == 'env' or values.get('DEEPSEEK_API_KEY'):
            values['GHA_MODEL_API_KEY'] = values['DEEPSEEK_API_KEY']
            model = replace(cfg.model, model_id=DEEPSEEK_MODEL_ID, auth_mode='env',
                            base_url='https://api.deepseek.com', disable_reasoning=False)
        else:
            raise ValueError('DeepSeek is not connected. Open Settings to store a dedicated DeepSeek key or company broker credential, then choose that connection.')
    else:
        raise ValueError('Choose the GLM or DeepSeek fleet pool.')
    daily = (getattr(cfg, 'fleet_max_calls_per_day', None) or {}).get(provider, model.max_calls_per_day)
    resolved = replace(cfg, model=replace(
        model, max_calls_per_day=fleet_daily_ceiling(daily),
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
        source = (str(env.get('FATCHIN_DEEPSEEK_AUTH_MODE') or '').strip()
                  or ('broker-lease' if env.get('MCP_SECRET_BROKER_ADMIN_TOKEN') else 'env')) if spec['id'] == 'deepseek' else cfg.model.auth_mode
        row['credential_source'] = source
        row['credential_source_label'] = 'Company broker' if source == 'broker-lease' else 'Dedicated provider key' if source == 'env' else 'Choose a connection'
        row['next_step'] = ('Choose the saved DeepSeek connection in Settings, apply the change, then send one test request when ready.'
                            if spec['id'] == 'deepseek' else 'Review this model connection in Settings, apply changes, then send one test request when ready.')
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
