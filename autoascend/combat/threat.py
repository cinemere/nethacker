"""Observable, deliberately approximate exchange estimates for the Rogue probe.

NLE's permonst binding omits attack dice and monster inventories. The table uses
conservative damage per attack round, with weapon and poison allowances;
wand possession is inferred only from visible pickup/zap messages.
"""
import re

from ..item import Item
from .monster_utils import ONLY_RANGED_SLOW_MONSTERS, WEAK_MONSTERS


ROUND_DAMAGE = {
    'rothe': 8.5, 'dwarf': 8, 'dwarf lord': 12, 'dwarf king': 16,
    'gnome': 4.5, 'gnome lord': 6, 'gnomish wizard': 7, 'gnome king': 8,
    'giant ant': 3.5, 'soldier ant': 15, 'killer bee': 5, 'queen bee': 10,
    'fire ant': 10, 'Uruk-hai': 7, 'hill orc': 5, 'Mordor orc': 7,
    'orc-captain': 14, 'hobgoblin': 5, 'goblin': 3.5, 'hobbit': 3.5,
    'jackal': 1.5, 'fox': 2, 'sewer rat': 2, 'giant rat': 2,
    'small mimic': 7, 'large mimic': 9, 'giant mimic': 18,
    'pony': 6, 'horse': 10, 'warhorse': 16, 'mumak': 20,
    'leocrotta': 16.5, 'wood nymph': 0.5, 'water nymph': 0.5,
    'mountain nymph': 0.5,
}


def wand_users(agent, monsters):
    """Names are conservative class-level hints, not hidden inventory access."""
    recent = ' '.join(agent._message_history[-30:])
    names = set()
    for _, _, _, mon, _ in monsters:
        name = re.escape(mon.mname)
        if re.search(r'\b' + name + r' (?:zaps\b|picks up [^.!]*\bwand\b)', recent, re.I):
            names.add(mon.mname)
        # Shopkeepers zap under their proper names (e.g. "Ermenak zaps...").
        # A visible hostile shopkeeper plus a named zap is an observable hint.
        if mon.mname == 'shopkeeper' and re.search(
                r'\b(?!The\b|It\b|You\b)[A-Z][a-z]+ zaps\b', recent):
            names.add(mon.mname)
    return names


def damage_per_turn(agent, mon, wand=False):
    if mon.mname in ONLY_RANGED_SLOW_MONSTERS or mon.mname in WEAK_MONSTERS:
        return 0.25
    level = max(1, getattr(mon, 'mlevel', 4))
    damage = ROUND_DAMAGE.get(mon.mname, max(3.5, 1.5 * level))
    if 'were' in mon.mname:
        damage = max(damage, 7)
    if 'unicorn' in mon.mname:
        damage = 12
    # Keep a floor for uncertainty in weapons/enchantments; negative AC reduces
    # hit probability, but we do not assume perfect protection from low-level foes.
    hit = min(.95, max(.2, (10 + agent.blstats.armor_class + level) / 20))
    speed = max(.25, mon.mmove / 12)
    return max(damage * hit * speed, 12 if wand else 0)


def distance(y, x, monster):
    return max(abs(y - monster[1]), abs(x - monster[2]))


def choose_action(agent, monsters, actions, best_action):
    """Return an alternative and compact evidence, without changing game state."""
    if not monsters or agent.character.prop.hallu:
        return None
    y, x = agent.blstats.y, agent.blstats.x
    users = wand_users(agent, monsters)
    nearby = [m for m in monsters if distance(y, x, m) <= 3]
    damage = {id(m): damage_per_turn(agent, m[3], m[3].mname in users) for m in monsters}
    incoming = sum(damage[id(m)] * (1 if distance(y, x, m) <= 1 else
                                   .75 if distance(y, x, m) == 2 else .35) for m in nearby)
    weapon = agent.inventory.items.main_hand
    # The normal melee action first equips its best weapon. Estimate that
    # exchange, unless the current weapon is welded or a shield blocks the swap.
    best_weapon = agent.inventory.get_best_melee_weapon()
    if weapon is None or weapon.status != Item.CURSED:
        if best_weapon is None or not (best_weapon.objs[0].bi and agent.inventory.items.off_hand is not None):
            weapon = best_weapon
    # Digging tools can be wielded too; get_melee_bonus accepts weapons only.
    if weapon is not None and not weapon.is_weapon():
        weapon = None
    to_hit, dmg = agent.character.get_melee_bonus(weapon)
    rounds = 2.0
    for m in nearby:
        if m[3].mname in WEAK_MONSTERS + ONLY_RANGED_SLOW_MONSTERS:
            continue
        hit = min(.95, max(.2, (to_hit + getattr(m[3], 'ac', 5)) / 20))
        hp = max(4, 4.5 * getattr(m[3], 'mlevel', 4))
        rounds = max(rounds, min(4, hp / max(1, dmg * hit)))
    losing = bool(nearby) and agent.blstats.hitpoints < min(4, incoming) + 1.3 * incoming * rounds
    engraving = agent.inventory.engraving_below_me.lower() == 'elbereth'
    # Elbereth does not protect against missiles/wands or @ monsters. Do not
    # repeatedly engrave while a visible enemy can ignore its protection.
    protected = all(agent._respects_elbereth(m[3]) and m[3].mname not in users
                    and m[3].mname not in ('Uruk-hai', 'orc-captain', 'Mordor orc')
                    for m in monsters)
    candidates = []
    for priority, action in actions:
        if action[0] != 'ranged':
            continue
        dy, dx = action[1:3]
        targets = [m for m in monsters
                   if (m[1] - y) * dx == (m[2] - x) * dy
                   and (m[1] - y) * dy + (m[2] - x) * dx > 0]
        if not targets:
            continue
        target = min(targets, key=lambda m: distance(y, x, m))
        d = distance(y, x, target)
        if target[3].mname in users:
            candidates.append((100, action))
        elif losing and (not engraving or d > 1) and target[3].mname != 'gas spore':
            # Daggers retain the Rogue multishot advantage at close range. A
            # launcher swap costs a turn; prefer cover/retreat when one is needed.
            launcher, ammo = agent.inventory.get_best_ranged_set()
            if launcher is None or launcher.equipped:
                priority = 45 if d > 1 else 25
                if launcher is None and ammo.count >= 2 and ammo.objs[0].name in (
                        'dagger', 'elven dagger', 'orcish dagger', 'silver dagger') and \
                        agent.blstats.hitpoints > 2 * incoming:
                    priority = 65
                candidates.append((priority, action))

    if losing:
        adjacent = [m for m in nearby if distance(y, x, m) <= 1]
        if protected and engraving and agent.blstats.hitpoints < .9 * agent.blstats.max_hitpoints:
            candidates.append((90, ('wait',)))
        elif protected and not engraving and agent.can_engrave():
            # Write before the burst, with enough HP left to survive a failed
            # dust engraving. Fast monsters cannot simply be outrun.
            candidates.append((55 if adjacent else 35, ('elbereth',)))

        current_risk = sum(damage[id(m)] for m in monsters if distance(y, x, m) <= 1)
        walkable = agent.current_level().walkable
        stairs = agent.current_level().get_stairs(up=True)
        for _, action in actions:
            if action[0] != 'move':
                continue
            ny, nx = y + action[1], x + action[2]
            # A move is a retreat only if it reduces immediate exposure and accounts
            # for other enemies at the destination. Fast pursuers can catch up at range 2.
            risk = sum(damage[id(m)] for m in monsters
                       if distance(ny, nx, m) <= (2 if m[3].mmove > 12 else 1))
            old_distance = min(distance(y, x, m) for m in monsters)
            new_distance = min(distance(ny, nx, m) for m in monsters)
            if risk < current_risk or (not adjacent and risk == 0 and new_distance > old_distance):
                open_neighbors = int(walkable[max(0, ny-1):ny+2, max(0, nx-1):nx+2].sum())
                corridor = 2 if open_neighbors <= 4 else 0
                upstairs = 0
                if stairs:
                    before = min(max(abs(y-sy), abs(x-sx)) for sy, sx in stairs)
                    after = min(max(abs(ny-sy), abs(nx-sx)) for sy, sx in stairs)
                    upstairs = 2 * (before - after)
                candidates.append((40 + corridor + upstairs - risk, action))

    if not candidates:
        return None
    _, action = max(candidates, key=lambda pair: pair[0])
    if action == best_action:
        return None
    return action, dict(action=action[0], dpt=round(incoming, 1),
                        rounds=round(rounds, 1), wand=sorted(users),
                        monsters=[m[3].mname for m in nearby][:6])
