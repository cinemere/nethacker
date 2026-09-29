from enum import IntEnum, auto

import nle.nethack as nh
import numpy as np
from nle.nethack import actions as A

from . import objects as O
from . import soko_solver
from . import utils
from . import hyp
from .character import Character
from .exceptions import AgentPanic
from .glyph import Hunger, G, MON
from .item import Item, flatten_items
from .item.item_priority_base import ItemPriorityBase
from .level import Level
from .strategy import Strategy


class ItemPriority(ItemPriorityBase):
    MAX_NUMBER_OF_ITEMS = 26 * 2 - 1  # + coin slot, one slot should be left for item arranging
    def __init__(self, agent):
        self.agent = agent
        self._take_sacrificial_corpses = False
        self._drop_gold_till_turn = -float('inf')

    def _split(self, items, forced_items, weight_capacity):
        if self.agent.global_logic.squeeze_cap:
            weight_capacity = min(weight_capacity, SQUEEZE_WEIGHT_CAP)
        remaining_weight = weight_capacity
        ret_inv = {}
        for item in forced_items:
            remaining_weight -= item.weight()
            ret_inv[item] = item.count

        ret_bag = {}
        bag = None

        def add_item(item, count=None, to_bag=False):
            nonlocal ret_inv, ret_bag, remaining_weight
            assert isinstance(item, Item)
            if to_bag and bag is not None and item is not bag:
                ret = ret_bag
            else:
                ret = ret_inv

            if remaining_weight < 0 or \
                    (ret is ret_inv and len(ret) >= ItemPriority.MAX_NUMBER_OF_ITEMS):  # TODO: coin slot
                return

            how_many_already_total = ret_inv.get(item, 0) + ret_bag.get(item, 0)
            how_many_already = ret.get(item, 0)
            unit_weight = item.unit_weight(with_content=False)
            # weightless items (e.g. wraith corpses) would make this an infinite count
            max_to_add = item.count if unit_weight <= 0 else int(remaining_weight // unit_weight)
            if count is not None:
                max_to_add = min(max_to_add, count)
            ret[item] = min(item.count, how_many_already_total + max_to_add) - (how_many_already_total - how_many_already)
            remaining_weight -= item.unit_weight(with_content=False) * (ret[item] - how_many_already)

        for item in items:
            if item.is_container() and item.status in [Item.UNCURSED, Item.BLESSED] and item.objs[0].desc == 'bag':
                bag = item  # TODO: select the best
                add_item(bag)

        if self._drop_gold_till_turn < self.agent.blstats.time:
            for item in items:
                if item.category == nh.COIN_CLASS:
                    add_item(item)

        def add_pick():
            for item in items:
                if item.is_unambiguous() and item.objs[0].name in ('pick-axe', 'dwarvish mattock') and \
                        item.status != Item.CURSED:
                    add_item(item)
                    break

        # squeezing through is only needed to get the pick back to the main dungeon, so under the
        # squeeze cap it goes first
        if self.agent.global_logic.squeeze_cap:
            add_pick()
        # hypothesis (pick_first): the digging tool is kept only after the weapon and armor, so when
        # they fill the carrying capacity the pick is dropped -- 67 drops in 3,600 Rogue games, and in
        # 148 games a Rogue held a pick yet never dug (score 0.057, as if it had none). A pick is worth
        # more than anything else it could carry: keep it first, before weapon and armor.
        elif any(i.is_unambiguous() and i.objs[0].name in ('pick-axe', 'dwarvish mattock') and
                 i.status != Item.CURSED for i in items) and hyp.fire('pick_first', self.agent):
            add_pick()

        for allow_unknown_status in [False, True]:
            item = self.agent.inventory.get_best_melee_weapon(items=forced_items + items,
                                                              allow_unknown_status=allow_unknown_status)
            if item is not None:
                add_item(item)

            for item in self.agent.inventory.get_best_armorset(items=forced_items + items,
                                                               allow_unknown_status=allow_unknown_status):
                if item is not None:
                    add_item(item)

        # a digging tool turns the rest of the game into a dive (Agent.dig_down), worth far more than
        # anything else we could carry in its weight
        add_pick()

        for item in items:
            if item.is_unambiguous():
                if item.object in [
                        O.from_name('healing', nh.POTION_CLASS),
                        O.from_name('extra healing', nh.POTION_CLASS),
                        O.from_name('full healing', nh.POTION_CLASS)]:
                    add_item(item)

                if self.agent.character.role in [Character.RANGER, Character.ROGUE,
                                                 Character.SAMURAI, Character.TOURIST] and \
                        (item.is_launcher() or item.is_fired_projectile()):
                    add_item(item)

        if self.agent.character.alignment == Character.LAWFUL:
            for item in sorted(filter(lambda i: i.objs[0].name == 'long sword', items),
                               key=lambda i: -utils.calc_dps(*self.agent.character.get_melee_bonus(i))):
                add_item(item)
                break

        for item in sorted(filter(lambda i: i.is_thrown_projectile(), items),
                           key=lambda i: -utils.calc_dps(*self.agent.character.get_ranged_bonus(None, i))):
            add_item(item)

        for item in sorted(filter(lambda i: i.is_food() and not i.is_corpse(), items),
                           key=lambda x: -x.nutrition_per_weight() - 1000 * (x.objs[0].name == 'sprig of wolfsbane')):
            add_item(item)

        if self._take_sacrificial_corpses:
            for item in filter(self.agent.global_logic.can_sacrify, items):
                add_item(item)

        # TODO: take nh.COIN_CLASS once shopping is implemented.
        # You have to drop all coins not to be attacked by a vault guard

        for item in sorted(items, key=lambda i: i.unit_weight(with_content=False)):
            if item.category in [nh.POTION_CLASS, nh.RING_CLASS, nh.AMULET_CLASS, nh.WAND_CLASS, nh.SCROLL_CLASS, nh.TOOL_CLASS]:
                if (not isinstance(item.objs[0], O.Container) or not item.is_chest()) and \
                        not item.is_possible_container():
                    to_bag = O.from_name('cancellation', nh.WAND_CLASS) not in item.objs and \
                             not item.is_offensive_usable_wand()
                    to_bag = False
                    if not item.is_container():  # remove condition to pick up bags
                        add_item(item, to_bag=to_bag)

        categories = [nh.WEAPON_CLASS, nh.ARMOR_CLASS, nh.TOOL_CLASS, nh.FOOD_CLASS, nh.GEM_CLASS, nh.AMULET_CLASS,
                      nh.RING_CLASS, nh.POTION_CLASS, nh.SCROLL_CLASS, nh.SPBOOK_CLASS, nh.WAND_CLASS]
        for item in sorted(items, key=lambda i: i.unit_weight(with_content=False)):
            if item.category in categories and not isinstance(item.objs[0], O.Container) and not item.is_corpse():
                if item.status == Item.UNKNOWN:
                    to_bag = O.from_name('cancellation', nh.WAND_CLASS) not in item.objs and \
                             not item.is_offensive_usable_wand()
                    to_bag = False
                    if not item.is_container():  # remove condition to pick up bags
                        add_item(item, to_bag=to_bag)

        r = {None: [ret_inv.get(item, 0) for item in items]}
        if bag is not None:
            r[bag] = [ret_bag.get(item, 0) for item in items]
        for item in items:
            if item.is_container() and item is not bag and ret_inv.get(item, 0) != 0:
                r[item] = [0 for _ in items]
        return r


class Milestone(IntEnum):
    BE_ON_FIRST_LEVEL = auto()
    FIND_GNOMISH_MINES = auto()
    # FIND_LIGHT_GNOMISH_MINES = auto()
    # FARM_LIGHT_GNOMISH_MINES = auto()
    FIND_MINETOWN = auto()
    FIND_SOKOBAN = auto()
    SOLVE_SOKOBAN = auto()
    FIND_MINES_END = auto()
    GO_DOWN = auto() # TODO


# experience level from which a character carrying a pick-axe skips the Dlvl 1 grind and digs down
EARLY_DIG_XL = 5
# turns a non-gnome, non-dwarf spends hunting the Mines' dwarves for a pick-axe after the Dlvl 1
# grind before it gives up and dives by the stairs; 0 disables the hunt
PICK_HUNT_TURNS = 3000
# experience level the Dlvl 1 grind stops at before the deep phase begins
GRIND_XL = 5
# rogue_grind8: the Rogue's Dlvl 1 grind target
ROGUE_GRIND_XL = 8
# NetHack refuses a diagonal squeeze between two rock squares to anyone carrying more than 600
SQUEEZE_WEIGHT_LIMIT = 600
# the inventory budget once squeezing is needed, a little under the limit for weight misestimates
SQUEEZE_WEIGHT_CAP = 580


class GlobalLogic:
    def __init__(self, agent):
        self.agent = agent
        self.milestone = Milestone(1)
        self._pick_hunt_start = None  # turn the Mines pick hunt began, if it did
        self._grind_deeper_target = 1
        self.step_completion_log = {}  # Milestone -> (step, turn)

        self.item_priority = ItemPriority(self.agent)

        self.oracle_level = None
        self.minetown_level = None

        self._got_artifact = False

        self.squeeze_cap = False  # carry at most SQUEEZE_WEIGHT_CAP (see _update_squeeze_cap)
        self._squeeze_hits = 0
        self._squeeze_check_turn = -float('inf')

    def _update_squeeze_cap(self):
        # hypothesis: a pick hunter that wins its digging tool in the Gnomish Mines often never turns
        # it into a dive, stalling on a Mines level for 5k-20k turns and scoring only its Xp
        # (~0.05-0.18) instead of the Dlvl 10-26 dive (0.13-0.5). Three things hold it there:
        # 1. the bot fills its pack to its carrying capacity (usually 700-1000, and the pick adds
        #    100-120), but NetHack forbids squeezing diagonally between two rock squares to anyone
        #    carrying over 600, and the Mines' cave levels are full of such gaps, so the up stairs
        #    are often unreachable. When a staircase of a Mines level stays reachable only by
        #    squeezing, cap the pack below the limit (the pick first) for the rest of the Mines
        #    visit, so the bot drops its least valued items and walks on (this method);
        # 2. before heading for the stairs it first explores every corner of each item-strewn Mines
        #    level, which with the fights and pickups along the way takes thousands of turns
        #    (_leaving_mines_to_dig);
        # 3. a dwarvish mattock is two-handed and cannot be applied under a shield, so a hunter
        #    whose tool is a mattock tries to dig and fails on every square
        #    (Inventory.get_best_armorset, Agent.pick_for_digging).
        # Outside the Mines and without a digging tool nothing changes.
        level = self.agent.current_level()
        if level.dungeon_number != Level.GNOMISH_MINES:
            self.squeeze_cap = False
            self._squeeze_hits = 0
            return
        if self.squeeze_cap or self.agent.inventory.items.total_weight <= SQUEEZE_WEIGHT_LIMIT:
            return
        turn = self.agent.blstats.time
        if turn - self._squeeze_check_turn < 10:
            return
        self._squeeze_check_turn = turn
        only_squeezing = (self.agent.bfs(can_squeeze=True) != -1) & (self.agent.bfs(can_squeeze=False) == -1)
        # a monster standing in a gap looks the same, and exploring may still turn up another way
        # around, so the cut-off has to persist for a while
        if (only_squeezing & utils.isin(level.objects, G.STAIR_UP, G.STAIR_DOWN)).any():
            self._squeeze_hits += 1
        else:
            self._squeeze_hits = 0
        if self._squeeze_hits >= 50:
            self.squeeze_cap = True

    def _leaving_mines_to_dig(self):
        # (see _update_squeeze_cap) with the digging tool in hand the Mines level has nothing left
        # to offer: walk straight back to the main dungeon rather than first exploring every corner
        # of this item-strewn cave level, which with fights and pickups can take thousands of turns
        return self.milestone == Milestone.GO_DOWN and \
               self.agent.current_level().dungeon_number == Level.GNOMISH_MINES and \
               self.agent.pick_for_digging() is not None

    def _fast_descent(self):
        # hypothesis (fast_descent): 65% of Rogue games never get a digging tool and average ~0.04;
        # on the way down they clear every level before taking the stairs -- ~770 turns per level of
        # hunger and fights. Score is the deepest level reached and death costs nothing, so without a
        # pick look only for the down staircase (Rogues are stealthy: sleepers stay asleep) and take
        # it; the heal-before-moving-on exploration below is kept. (Komershan did this for gnomes.)
        return self.milestone == Milestone.GO_DOWN and \
               self.agent.character.role == Character.ROGUE and \
               self.agent.current_level().dungeon_number == Level.DUNGEONS_OF_DOOM and \
               self.agent.pick_for_digging() is None and \
               hyp.fire('fast_descent', self.agent)

    def update(self):
        self._update_squeeze_cap()
        if not self.agent.character.prop.hallu:
            if utils.isin(self.agent.glyphs, G.ORACLE).any():
                if self.oracle_level is None:
                    self.oracle_level = self.agent.current_level().key()
                else:
                    assert self.oracle_level == self.agent.current_level().key()

            if self.agent.current_level().dungeon_number == Level.GNOMISH_MINES and \
                    utils.isin(self.agent.glyphs, G.SHOPKEEPER).any():
                if self.minetown_level is None:
                    self.minetown_level = self.agent.current_level().key()
                else:
                    assert self.minetown_level == self.agent.current_level().key()

    @utils.debug_log('solving sokoban')
    @Strategy.wrap
    def solve_sokoban_strategy(self):
        # TODO: refactor
        if not utils.isin(self.agent.current_level().objects, G.TRAPS).any():
            yield False
        yield True

        def push_bolder(ty, tx, dy, dx):
            while 1:
                if self.agent.bfs()[ty, tx] == -1:
                    return False
                self.agent.go_to(ty, tx, debug_tiles_args=dict(color=(255, 255, 255), is_path=True))
                with self.agent.atom_operation():
                    direction = self.agent.calc_direction(ty, tx, ty + dy, tx + dx)
                    self.agent.direction(direction)
                    message = self.agent.message

                if (self.agent.blstats.y, self.agent.blstats.x) == (ty, tx):
                    assert 'You hear a monster behind the boulder.' in message or \
                           'You try to move the boulder, but in vain.' in message, message
                    if self.agent.bfs()[ty + dy, tx + dx] != -1:
                        self.agent.go_to(ty + dy, tx + dx, debug_tiles_args=dict(color=(255, 255, 255), is_path=True))
                        continue
                    else:
                        pickaxe = None
                        for item in flatten_items(self.agent.inventory.items):
                            if item.is_unambiguous() and \
                                    item.object in [O.from_name('pick-axe'), O.from_name('dwarvish mattock')]:
                                pickaxe = item
                                break
                        if pickaxe is not None:
                            with self.agent.atom_operation():
                                pickaxe = self.agent.inventory.move_to_inventory(pickaxe)
                                self.agent.step(A.Command.APPLY)
                                self.agent.type_text(self.agent.inventory.items.get_letter(pickaxe))
                                self.agent.direction(direction)
                            return True
                else:
                    return True

                # TODO: not sure what to do
                self.agent.exploration.explore1(None).run()

        while 1:
            wall_map = utils.isin(self.agent.current_level().objects, G.WALL)
            for smap, answer in soko_solver.maps.items():
                sokomap = soko_solver.convert_map(smap)
                offset = np.array(min(zip(*wall_map.nonzero()))) - \
                         np.array(min(zip(*(sokomap.sokomap == soko_solver.WALL).nonzero())))
                mask = wall_map[offset[0] : offset[0] + sokomap.sokomap.shape[0],
                                offset[1] : offset[1] + sokomap.sokomap.shape[1]]
                if (mask & (sokomap.sokomap == soko_solver.WALL) == mask).all():
                    break
            else:
                assert 0, 'sokomap not found'

            possible_mimics = set()
            last_resort_move = None
            for (y, x), (dy, dx) in answer:
                boulder_map = utils.isin(self.agent.glyphs, G.BOULDER)
                mask = boulder_map[offset[0] : offset[0] + sokomap.sokomap.shape[0],
                                   offset[1] : offset[1] + sokomap.sokomap.shape[1]]
                ty, tx = offset[0] + y - dy, offset[1] + x - dx,
                soko_boulder_mask = sokomap.sokomap == soko_solver.BOULDER
                if self.agent.bfs()[ty, tx] != -1 and \
                        ((soko_boulder_mask | mask) == soko_boulder_mask).all() and \
                        self.agent.glyphs[ty + dy, tx + dx] in G.BOULDER:

                    soko_dis1 = sokomap.bfs()
                    sokomap.move(y, x, dy, dx)
                    soko_dis2 = sokomap.bfs()

                    # see points that will no longer be accessible
                    with self.agent.env.debug_log('checking for monsters'):
                        to_visit_mask = self.agent.bfs() != -1
                        to_visit_mask[offset[0] : offset[0] + sokomap.sokomap.shape[0],
                                      offset[1] : offset[1] + sokomap.sokomap.shape[1]] &= \
                                              (soko_dis1 != -1) & (soko_dis2 == -1)

                        with self.agent.env.debug_tiles(to_visit_mask, color=(255, 0, 0, 128)):
                            while to_visit_mask.any():
                                vy, vx = list(zip(*to_visit_mask.nonzero()))[0]
                                to_visit_mask[vy, vx] = 0

                                def clear_neighbors():
                                    to_visit_mask[self.agent.blstats.y, self.agent.blstats.x] = 0
                                    to_visit_mask[utils.isin(self.agent.glyphs, G.VISIBLE_FLOOR)] = 0
                                    return not to_visit_mask[vy, vx]

                                self.agent.go_to(vy, vx, callback=clear_neighbors)

                    if not push_bolder(ty, tx, dy, dx):
                        continue

                    possible_mimics = set()
                    last_resort_move = None

                    if not utils.isin(self.agent.current_level().objects, G.TRAPS).any():
                        return

                else:
                    sokomap.move(y, x, dy, dx)

                if (~soko_boulder_mask | mask).all():
                    if self.agent.bfs()[ty, tx] != -1 and \
                            self.agent.glyphs[ty + dy, tx + dx] in G.BOULDER and \
                            last_resort_move is None:
                        last_resort_move = (ty, tx, dy, dx)

                    if not possible_mimics:
                        for mim_y, mim_x in zip(*(~soko_boulder_mask & mask).nonzero()):
                            possible_mimics.add((mim_y + offset[0], mim_x + offset[1]))


            # sokoban configuration is not in the list. Check for mimics
            with self.agent.env.debug_log('mimics'):
                with self.agent.env.debug_tiles(possible_mimics, color=(255, 0, 0, 128)):
                    for mim_y, mim_x in sorted(possible_mimics):
                        if (self.agent.bfs()[max(0, mim_y - 1) : mim_y + 2, max(0, mim_x - 1) : mim_x + 2] != -1).any():
                            self.agent.go_to(mim_y, mim_x, stop_one_before=True,
                                             debug_tiles_args=dict(color=(255, 0, 0), is_path=True))
                            for _ in range(3):
                                self.agent.search()

            # last resort move (sometimes we can see a mimic diagonally but cannot reach it)
            with self.agent.env.debug_log('last_resort'):
                if last_resort_move is not None:
                    ty, tx, dy, dx = last_resort_move
                    push_bolder(ty, tx, dy, dx)
                    continue

            self.agent.stats_logger.log_event('sokoban_dropped')
            self.milestone = Milestone(int(self.milestone) + 1)
            raise AgentPanic('sokomap unsolvable')

    @Strategy.wrap
    def wait_out_unexpected_state_strategy(self):
        yielded = False
        while (
                self.agent.character.prop.blind or
                self.agent.character.prop.confusion or
                self.agent.character.prop.stun or
                self.agent.character.prop.hallu or
                self.agent.character.prop.polymorph):
            if not yielded:
                yield True
                yielded = True

            self.agent.direction('.')

        if not yielded:
            yield False

    @utils.debug_log('identify_items_on_altar')
    @Strategy.wrap
    def identify_items_on_altar(self):
        mask = utils.isin(self.agent.current_level().objects, G.ALTAR)
        if not mask.any():
            yield False

        dis = self.agent.bfs()
        mask &= dis != -1
        if not mask.any():
            yield False

        yield any((item.status == Item.UNKNOWN for item in flatten_items(self.agent.inventory.items)
                   if item.can_be_dropped_from_inventory()))

        (ty, tx), *_ = zip(*(mask & (dis == dis[mask].min())).nonzero())
        self.agent.go_to(ty, tx)
        items_to_drop = [item for item in flatten_items(self.agent.inventory.items)
                         if item.can_be_dropped_from_inventory() and item.status == Item.UNKNOWN]
        if not items_to_drop:
            raise AgentPanic('items to drop on altar vanished')

        # TODO: move chunking to inventory.drop
        items_to_drop = items_to_drop[:self.agent.inventory.items.free_slots()]

        self.agent.inventory.drop(items_to_drop)

    @utils.debug_log('dip_for_excalibur')
    @Strategy.wrap
    def dip_for_excalibur(self):
        if self.agent.character.alignment != Character.LAWFUL or self.agent.blstats.experience_level < 5:
            yield False
        if self.agent.current_level().dungeon_number == Level.GNOMISH_MINES and \
                (self.minetown_level is None or self.agent.current_level().key() == self.minetown_level):
            yield False

        dis = self.agent.bfs()
        mask = utils.isin(self.agent.current_level().objects, G.FOUNTAIN) & (dis != -1)
        if not mask.any():
            yield False

        def excalibur_candidate():
            candidate = None
            for item in flatten_items(self.agent.inventory.items):
                if item.is_unambiguous() and item.object == O.from_name('long sword'):
                    if item.dmg_bonus is not None: # TODO: better condition for excalibur existance
                        return None
                    candidate = item
            return candidate

        if excalibur_candidate() is None:
            yield False
        yield True

        self.agent.go_to(*list(zip(*mask.nonzero()))[0])

        candidate = excalibur_candidate()
        if candidate is None:
            return

        # TODO: refactor
        with self.agent.atom_operation():
            candidate = self.agent.inventory.move_to_inventory(candidate)
            self.agent.step(A.Command.DIP)
            self.agent.type_text(self.agent.inventory.items.get_letter(candidate))
            if ('What do you want to dip ' in self.agent.message and 'into?' in self.agent.message) or \
                    "You don't have anything to dip " in self.agent.message:
                raise AgentPanic('no fountain here')

    def can_sacrify(self, item):
        if not item.is_corpse() or item.comment == 'old':
            return False

        mname = MON.permonst(item.monster_id + nh.GLYPH_MON_OFF).mname
        if (mname == 'pony' and self.agent.character.role in [Character.KNIGHT, Character.BARBARIAN]) or \
                (mname == 'kitten' and self.agent.character.role == [Character.BARBARIAN, Character.WIZARD]) or \
                (mname == 'little dog' and item.naming):  # little dogs are always named
            # sufficient condition for being an initial pet
            return False

        if self.agent.character.alignment != Character.CHAOTIC:
            mapping = {
                Character.HUMAN: MON.M2_HUMAN | MON.M2_WERE,
                Character.DWARF: MON.M2_DWARF,
                Character.ELF: MON.M2_ELF,
                Character.GNOME: MON.M2_GNOME,
                Character.ORC: MON.M2_ORC,
            }
            f2 = MON.permonst(item.monster_id + nh.GLYPH_MON_OFF).mflags2
            if (f2 & mapping[self.agent.character.race]) > 0:
                return False

        return True

    @utils.debug_log('offer_corpses')
    @Strategy.wrap
    def offer_corpses(self):
        self.item_priority._take_sacrificial_corpses = False

        if self._got_artifact:
            yield False

        altars = [p for p, alignment in self.agent.current_level().altars.items()
                  if alignment == self.agent.character.alignment]

        if not altars:
            yield False

        dis = self.agent.bfs()
        altars = [(y, x) for y, x in altars if dis[y, x] != -1]

        if not altars:
            yield False

        self.item_priority._take_sacrificial_corpses = True

        if not any((self.can_sacrify(item) for item in flatten_items(self.agent.inventory.items))):
            yield False

        yield True

        y, x = min(altars, key=lambda p: dis[p])
        self.agent.go_to(y, x)
        with self.agent.panic_if_position_changes():
            while 1:
                for item in flatten_items(self.agent.inventory.items):
                    if self.can_sacrify(item):
                        with self.agent.atom_operation():
                            item = self.agent.inventory.move_to_inventory(item)
                            assert self.can_sacrify(item)
                            self.agent.step(A.Command.OFFER)
                            while ('There is ' in self.agent.message or 'There are ' in self.agent.message) and \
                                    ('sacrifice it?' in self.agent.message or 'sacrifice one?' in self.agent.message):
                                self.agent.type_text('n')
                            assert 'What do you want to sacrifice?' in self.agent.message, self.agent.message
                            self.agent.type_text(self.agent.inventory.items.get_letter(item))
                            if 'Nothing happens.' in self.agent.message:
                                self.agent.inventory.call_item(item, 'old')
                                return
                            if 'Use my gift wisely' in self.agent.message:
                                self._got_artifact = True
                                self.agent.inventory.get_items_below_me()
                                return
                            if 'So this is how you repay loyalty?' in self.agent.message:
                                raise AgentPanic('pet sacrified')
                            assert 'Your sacrifice is consumed in a flash of light' in self.agent.message or \
                                'Your sacrifice is consumed in a burst of flame' in self.agent.message or \
                                ('The blood covers the altar!' in self.agent.message and \
                                 'You have summoned ' in self.agent.message), \
                                self.agent.message
                            break
                else:
                    break

    @utils.debug_log('follow_guard')
    @Strategy.wrap
    def follow_guard(self):
        if not utils.isin(self.agent.glyphs, G.GUARD).any():
            yield False

        if any(item.category == nh.COIN_CLASS for item in flatten_items(self.agent.inventory.items)):
            yield True
            # if 'Please drop that gold and follow me.' in self.message:
            self.agent.stats_logger.log_event('drop_gold')
            self.item_priority._drop_gold_till_turn = self.agent.blstats.time + 100
            self.agent.inventory.arrange_items().run()
            return

        ys, xs = utils.isin(self.agent.glyphs, G.GUARD).nonzero()
        y, x = ys[0], xs[0]

        if utils.adjacent((y, x), (self.agent.blstats.y, self.agent.blstats.x)):
            yield False

        yield True

        self.agent.go_to(y, x, stop_one_before=True)

    def _grind_deeper_level(self):
        # hypothesis (grind_deeper): Dlvl 2-3 supply fresh monsters and corpses
        # while targeting the main dungeon instead of the Mines. Stay shallow
        # through XL 2, descend with >=80% HP (and at least 12 HP), and retreat
        # below half HP or 10 HP. Keep the target while traveling so a small hit
        # does not cancel descent; level drain also lowers the depth ceiling.
        stats = self.agent.blstats
        if self.agent.character.role != Character.ROGUE or not 3 <= stats.experience_level < GRIND_XL:
            return 1
        if stats.hitpoints < max(10, 0.5 * stats.max_hitpoints):
            return 1
        desired = int(stats.experience_level) - 1  # Dlvl 2 at XL 3, Dlvl 3 at XL 4
        if stats.hitpoints >= max(12, 0.8 * stats.max_hitpoints):
            return desired
        return min(self._grind_deeper_target, desired)

    @Strategy.wrap
    def current_strategy(self):
        yield True
        idle_iterations = 0
        while 1:
            explore_stairs_condition = lambda: False
            deeper_grind = False
            if self.milestone == Milestone.BE_ON_FIRST_LEVEL:
                # hypothesis (grind_starve, shadow): a character with nothing to eat and no safe
                # prayer left starves on the Dlvl 1 grind; logged to see how often, not acted on
                # hypothesis (rogue_grind8, after Komershan 5deb412): a Rogue leaving Dlvl 1 at XL 5
                # dies on Dlvl 2-4 before it meets a single dwarf in 95 of 300 games (half of them
                # fainting on the way); the pick hunt needs a sturdier character. Grinding on to XL 8
                # roughly doubles HP and banks Xp:8 = 0.075, above the 0.03-0.05 those games score now.
                # A Rogue with a pick still digs from EARLY_DIG_XL.
                condition = lambda: (self.agent.blstats.experience_level >= GRIND_XL and not (
                    self.agent.character.role == Character.ROGUE and
                    self.agent.blstats.experience_level < ROGUE_GRIND_XL and
                    hyp.fire('rogue_grind8', self.agent))) or (
                    self.agent.blstats.hunger_state >= Hunger.WEAK and
                    self.agent.inventory.items.total_nutrition() == 0 and
                    not self.agent.is_safe_to_pray(1000) and
                    hyp.fire('grind_starve', self.agent))
                # explore_stairs_condition = lambda: self.agent.inventory.items.total_nutrition() == 0 and \
                #                                    self.agent.blstats.hunger_state >= Hunger.NOT_HUNGRY
                level = (Level.DUNGEONS_OF_DOOM, 1)
                grind_level = self._grind_deeper_level()
                if self.agent.character.role == Character.ROGUE and \
                        3 <= self.agent.blstats.experience_level < GRIND_XL and \
                        (grind_level != 1 or self._grind_deeper_target != grind_level or
                         self.agent.current_level().key() != level) and \
                        hyp.fire('grind_deeper', self.agent, target=grind_level):
                    deeper_grind = True
                    self._grind_deeper_target = grind_level
                    level = (Level.DUNGEONS_OF_DOOM, grind_level)

            elif self.milestone == Milestone.FIND_SOKOBAN:
                condition = lambda: self.agent.current_level().dungeon_number == Level.SOKOBAN
                level = (Level.SOKOBAN, 4)

            elif self.milestone == Milestone.FIND_GNOMISH_MINES:
                condition = lambda: self.agent.current_level().dungeon_number == Level.GNOMISH_MINES
                level = (Level.GNOMISH_MINES, 1)

            # elif self.milestone == Milestone.FIND_LIGHT_GNOMISH_MINES:
            #     condition = lambda: self.agent.current_level().dungeon_number == Level.GNOMISH_MINES \
            #             and self.agent.current_level().is_light_level()
            #     level = (Level.GNOMISH_MINES, 9)

            # elif self.milestone == Milestone.FARM_LIGHT_GNOMISH_MINES:
            #     condition = lambda: self.agent.blstats.experience_level >= 11
            #     level = (Level.GNOMISH_MINES, self.agent.current_level().level_number)

            elif self.milestone == Milestone.FIND_MINETOWN:
                condition = lambda: self.minetown_level is not None
                level = (Level.GNOMISH_MINES, 4)  # TODO
                # hypothesis (mines_shallow_hunt): a non-gnome hunting for a pick walks down to
                # Minetown (Dlvl 5-8) through levels of hostile armed gnomes and dwarves, and most
                # Rogues die there at XL 5-7 before any pick turns up; the first two Mines levels
                # hold dwarves too. Hunt only there until PICK_HUNT_TURNS runs out.
                if self._pick_hunt_start is not None and \
                        self.agent.character.role == Character.ROGUE and \
                        hyp.fire('mines_shallow_hunt', self.agent):
                    level = (Level.GNOMISH_MINES, 2)

            elif self.milestone == Milestone.SOLVE_SOKOBAN:
                # TODO: fix the condition, monster can destroy doors
                condition = lambda: self.agent.current_level().key() == (Level.SOKOBAN, 1) and \
                                    not utils.isin(self.agent.current_level().objects, G.DOOR_CLOSED).any()
                level = (Level.SOKOBAN, 1)

            elif self.milestone == Milestone.FIND_MINES_END:
                # the Mines are 8 or 9 levels deep, so waiting for level 9 left every 8-level Mines
                # stuck at its bottom forever; level 8 is the bottom or one short of it
                condition = lambda: self.agent.current_level().dungeon_number == Level.GNOMISH_MINES and \
                                    self.agent.current_level().level_number >= 8
                level = (Level.GNOMISH_MINES, 9)  # TODO

            else:
                # TODO
                condition = lambda: False
                level = (Level.DUNGEONS_OF_DOOM, 100)

            # a character that can dig heads straight down instead of grinding on Dlvl 1 (see
            # Agent.dig_down); otherwise the Dlvl 1 milestone walks it back up after every hole
            if self.milestone < Milestone.GO_DOWN and \
                    self.agent.blstats.experience_level >= EARLY_DIG_XL and \
                    self.agent.pick_for_digging() is not None:
                self.milestone = Milestone.GO_DOWN
                continue

            # a pick hunt in the upper Mines that has not paid off: dive by the stairs instead
            if self.milestone in (Milestone.FIND_GNOMISH_MINES, Milestone.FIND_MINETOWN) and \
                    self._pick_hunt_start is not None and \
                    self.agent.blstats.time - self._pick_hunt_start > PICK_HUNT_TURNS:
                self.milestone = Milestone.GO_DOWN
                continue

            if condition():
                # hypothesis: after the Dlvl 1 grind to Xp 8 the deep phase heads into the Gnomish
                # Mines. For a gnome that is a safe road (most of the gnomes, dwarves and hill orcs
                # there are peaceful to it), but for any other race it is a gauntlet of hostile
                # packs on open cave levels that also bottom out at Mines' End (Dlvl 10-13), capping
                # the depth milestones -- the best-scoring part of the score. Non-gnomes instead
                # descend the main Dungeons of Doom (stairs, plus dig_down with any digging tool):
                # room-and-corridor levels where fights come one at a time and no floor until
                # Medusa. The switch happens only at the Xp 8 hand-off, so the grind is untouched.
                # Exception: races the Mines' dwarves are hostile to go there first, only as far
                # as Minetown, to kill dwarves for the pick-axe most of them carry -- with a pick
                # the milestone above switches to GO_DOWN and dig_down takes over in the main
                # dungeon. Dwarves and gnomes find those dwarves peaceful, so they skip the hunt and
                # instead walk the peaceful Mines straight to Mines' End (Dlvl 10-13), skipping the
                # long and risky Sokoban detour, before diving the main dungeon.
                mines_folk = self.agent.character.race in (Character.GNOME, Character.DWARF)
                if self.milestone == Milestone.BE_ON_FIRST_LEVEL and not mines_folk:
                    if PICK_HUNT_TURNS > 0:
                        self._pick_hunt_start = self.agent.blstats.time
                        self.milestone = Milestone.FIND_GNOMISH_MINES
                    else:
                        self.milestone = Milestone.GO_DOWN
                    continue
                if self.milestone == Milestone.FIND_MINETOWN and self._pick_hunt_start is not None:
                    self.milestone = Milestone.GO_DOWN
                    continue
                if self.milestone == Milestone.FIND_MINETOWN and mines_folk:
                    self.milestone = Milestone.FIND_MINES_END
                    continue
                self.milestone = Milestone(int(self.milestone) + 1)
                continue


            def exploration_strategy(level, **kwargs):
                return (
                    Strategy(lambda: self.agent.exploration.explore1(level, trap_search_offset=1,
                        kick_doors=self.agent.current_level().dungeon_number != Level.GNOMISH_MINES, **kwargs).strategy())
                    .preempt(self.agent, [
                        self.identify_items_on_altar().every(100),
                        self.identify_items_on_altar().condition(
                            lambda: self.agent.current_level().objects[self.agent.blstats.y,
                                                                       self.agent.blstats.x] in G.ALTAR),
                        self.dip_for_excalibur().condition(
                            lambda: self.agent.blstats.experience_level >= 7).every(10),
                    ])
                )

            def go_to_strategy(y, x):
                return (
                    exploration_strategy(None)
                    .preempt(self.agent, [
                        self.agent.exploration.go_to_strategy(y, x).preempt(self.agent, [
                            self.agent.inventory.gather_items(),
                            self.identify_items_on_altar(),
                            self.dip_for_excalibur().condition(lambda: self.agent.blstats.experience_level >= 7),
                        ])
                        .condition(lambda: self._got_artifact or
                                           not any([alignment == self.agent.character.alignment
                                                    for _, alignment in self.agent.current_level().altars.items()]))
                    ])
                    .until(self.agent, lambda: (self.agent.blstats.y, self.agent.blstats.x) == (y, x))
                )

            step_count_before = self.agent.step_count
            # hypothesis (grind_deeper): interrupt a long search when XL/HP makes
            # a different grind level appropriate. Do not advance the milestone:
            # XL 5 still starts the existing pick hunt / digging phase.
            def grind_target_changed():
                return self.milestone == Milestone.BE_ON_FIRST_LEVEL and \
                    self._grind_deeper_level() != level[1] and \
                    hyp.fire('grind_deeper', self.agent,
                             target=self._grind_deeper_level(), event='retarget')

            def explore_before_travel():
                # hypothesis (grind_deeper): once ready, use the stairs instead
                # of spending the food budget finishing the old level's search.
                return not (deeper_grind and self.agent.current_level().key() != level)

            (
                self.agent.exploration.go_to_level_strategy(*level, go_to_strategy, exploration_strategy(None))
                .before(exploration_strategy(None))#.before(self.agent.exploration.patrol())
                .preempt(self.agent, [
                    exploration_strategy(0).condition(
                        lambda: not self._leaving_mines_to_dig() and explore_before_travel() and
                        not self._fast_descent()),
                    exploration_strategy(None).until(
                        self.agent, lambda: self.agent.blstats.hitpoints >= 0.8 * self.agent.blstats.max_hitpoints)
                    .condition(explore_before_travel)
                ])
                .preempt(self.agent, [
                    self.agent.exploration.explore_stairs(go_to_strategy, all=True).condition(explore_stairs_condition),
                ])
                .until(self.agent, lambda: condition() or grind_target_changed())
            ).run()

            # hypothesis (see Agent.handle_exception): once the current level is fully explored and
            # searched, every sub-strategy declines without acting while the milestone condition (e.g.
            # Xp8 on Dlvl 1) is still unmet, so this loop spins forever without a single game action --
            # the agent hangs, the game idles until the no-progress timeout and the run's remaining
            # progress is forfeited. After many consecutive action-less passes, search in place so
            # game time passes (monsters spawn and come to us, XP keeps growing) instead of hanging.
            if self.agent.step_count == step_count_before:
                idle_iterations += 1
                if idle_iterations >= 20:
                    idle_iterations = 0
                    self.agent.search(10)
            else:
                idle_iterations = 0

    def global_strategy(self):
        return (
            self.current_strategy().repeat()
            .preempt(self.agent, [
                self.solve_sokoban_strategy()
                .condition(lambda: self.milestone == Milestone.SOLVE_SOKOBAN and
                                   self.agent.current_level().dungeon_number == Level.SOKOBAN)
            ])
            .preempt(self.agent, [
                self.offer_corpses().preempt(self.agent, [
                    self.agent.eat_corpses_from_ground().condition(lambda: self.agent.blstats.hunger_state >= Hunger.NOT_HUNGRY),
                ]),
            ])
            .preempt(self.agent, [
                self.wait_out_unexpected_state_strategy(),
            ])
            .preempt(self.agent, [
                self.agent.cure_disease().every(5),
            ])
            .preempt(self.agent, [
                self.agent.eat_corpses_from_ground(only_below_me=True).condition(lambda: self.agent.blstats.hunger_state >= Hunger.NOT_HUNGRY),
                self.agent.eat_corpses_from_ground().every(5).condition(lambda: self.agent.blstats.hunger_state >= Hunger.NOT_HUNGRY),
                self.agent.eat_from_inventory().every(5),
            ])
            .preempt(self.agent, [
                self.agent.faint_camp(),
                self.agent.elbereth_rest(),
            ])
            .preempt(self.agent, [
                self.follow_guard(),
            ])
            .preempt(self.agent, [
                self.agent.fight2(),
            ])
            .preempt(self.agent, [
                self.agent.engulfed_fight(),
            ])
            .preempt(self.agent, [
                self.agent.proactive_sleep_strategy(),
            ])
            .preempt(self.agent, [
                self.agent.emergency_strategy(),
            ])
            .preempt(self.agent, [
                self.agent.dig_down(),
            ])
        )
