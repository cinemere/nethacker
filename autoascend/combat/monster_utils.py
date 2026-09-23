# heuristic monster types lists
ONLY_RANGED_SLOW_MONSTERS = ['floating eye', 'blue jelly', 'brown mold', 'gas spore', 'acid blob']
EXPLODING_MONSTERS = ['yellow light', 'gas spore', 'flaming sphere', 'freezing sphere', 'shocking sphere']
INSECTS = ['giant ant', 'killer bee', 'soldier ant', 'fire ant', 'giant beetle', 'queen bee']
WEAK_MONSTERS = ['lichen', 'newt', 'shrieker', 'grid bug']
WEIRD_MONSTERS = ['leprechaun', 'nymph']


def is_monster_faster(agent, monster):
    _, y, x, mon, _ = monster
    # TOOD: implement properly
    return 'bat' in mon.mname or 'dog' in mon.mname or 'cat' in mon.mname \
           or 'kitten' in mon.mname or 'pony' in mon.mname or 'horse' in mon.mname \
           or 'bee' in mon.mname or 'fox' in mon.mname


def imminent_death_on_melee(agent, monster):
    # Keep a buffer for retreating or using an emergency resource.
    if is_dangerous_monster(monster):
        return agent.blstats.hitpoints <= 20
    return agent.blstats.hitpoints <= 12


def is_dangerous_monster(monster):
    _, y, x, mon, _ = monster
    # hypothesis: using NetHack's difficulty rating to recognize threats makes
    # the early-game combat policy disengage from high-damage monsters before
    # they consume the remaining HP buffer, across both roles.
    is_pet = 'dog' in mon.mname or 'cat' in mon.mname or 'kitten' in mon.mname or 'pony' in mon.mname \
             or 'horse' in mon.mname
    # 'mumak' in mon.mname or 'orc' in mon.mname or 'rothe' in mon.mname \
    # or 'were' in mon.mname or 'unicorn' in mon.mname or 'elf' in mon.mname or 'leocrotta' in mon.mname \
    # or 'mimic' in mon.mname
    # hypothesis: explicitly recognizing high-damage early monsters makes the
    # existing retreat buffer engage even when their difficulty is understated.
    high_damage = ('rothe', 'mumak', 'winter wolf', 'unicorn', 'hill orc',
                   'werewolf', 'werejackal', 'ettin mummy')
    return is_pet or mon.mname in INSECTS or mon.mname in high_damage \
           or getattr(mon, 'difficulty', 0) >= 6


def consider_melee_only_ranged_if_hp_full(agent, monster):
    return monster[3].mname in ('brown mold', 'blue jelly') and agent.blstats.hitpoints == agent.blstats.max_hitpoints
