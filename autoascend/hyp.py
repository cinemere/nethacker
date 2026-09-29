"""Hypothesis probes: each behaviour change is a named probe in one of three modes.

  off     -- the probe is not consulted at all
  shadow  -- its condition is evaluated and logged, but the bot acts exactly as the parent
  on      -- logged and acted on

A mode may also be a dict keyed by 'role-race', role or race ({"rog-hum": "on", "rog": "shadow"});
the most specific matching key wins and anything unmatched is 'off'.

MODES below is what a submitted bot plays. A lab harness can override it by mounting
/hyp/config.json ({"name": "mode", ...}) and collect activations from /hyp/out
(one JSON line per activation), so the same tree serves both scoring and analysis.
A game where no probe returned True is byte-identical to the parent's.
"""
import json
import os

MODES = {
    # baked for submission; every probe is scoped to the Rogue (other identities play as the parent)
    'grind_deeper': 'off',
    'grind_starve': 'off',
    'weak_pray': 'off',
    'pray_backoff': 'off',
    'elbereth_vs_at': 'off',
    'rogue_volley': 'off',
    'mines_shallow_hunt': 'off',
    'no_blind_wield': 'off',
    'elbereth_rest': 'off',
    'stand_farm': 'off',
    'rogue_grind8': {'rog-hum': 'on'},
    'threat_avoid': {'rog': 'on'},
    'elbereth_early': {'rog-hum': 'on'},
    'pick_first': 'off',
    'drop_memory': 'off',
    'loop_guard': 'off',
    'fast_descent': {'rog-orc': 'on'},
    'medusa_nohang': {'rog': 'on'},
    'dry_dig': 'off',
}

_CONFIG = os.environ.get('HYP_CONFIG', '/hyp/config.json')
_OUT = os.environ.get('HYP_OUT', '/hyp/out')
_log = None
_last = {}

try:
    with open(_CONFIG) as f:
        MODES.update(json.load(f))
except (OSError, ValueError):
    pass


def _short(table, value):
    names = [n for n, v in table.items() if v == value]
    return names[0][:3].lower() if names else '?'


def mode(name, agent=None):
    m = MODES.get(name, 'off')
    if isinstance(m, dict):
        if agent is None:
            return 'off'
        c = agent.character
        role, race = _short(type(c).name_to_role, c.role), _short(type(c).name_to_race, c.race)
        m = m.get(f'{role}-{race}', m.get(role, m.get(race, 'off')))
    return m


def fire(name, agent, **info):
    """Record that probe `name` would intervene now; True only when it is 'on'."""
    m = mode(name, agent)
    if m == 'off':
        return False
    _record(name, agent, info)
    return m == 'on'


def _record(name, agent, info):
    global _log
    turn = int(agent.blstats.time)
    if _last.get(name) == turn:
        return
    _last[name] = turn
    if _log is None:
        if not os.path.isdir(_OUT):
            _log = False
            return
        try:
            _log = open(os.path.join(_OUT, f'hyp.{os.getpid()}.jsonl'), 'a', buffering=1)
        except OSError:
            _log = False
    if _log:
        _log.write(json.dumps(dict(h=name, turn=turn, dlvl=int(agent.blstats.depth),
                                   xl=int(agent.blstats.experience_level),
                                   hp=int(agent.blstats.hitpoints), maxhp=int(agent.blstats.max_hitpoints),
                                   hunger=int(agent.blstats.hunger_state), **info)) + '\n')
