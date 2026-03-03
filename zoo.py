from __future__ import annotations

import random
import time
from typing import Optional, List, Dict, Any

import discord
from discord.ext import commands
from discord import app_commands

import zoo_db

RARITY_ICON = {
    "common": "⚪",
    "uncommon": "🟢",
    "rare": "🔵",
    "epic": "🟣",
    "legendary": "🟡",
    "mythic": "🔴",
}

# PvE sessions (user_id -> BattleState)
PVE_SESSIONS: Dict[int, "BattleState"] = {}

# PvE cooldown after a finished match (user_id -> unix timestamp ready)
PVE_COOLDOWN_SEC = 5
PVE_COOLDOWNS: Dict[int, float] = {}

# PvP sessions (user_id -> PvPBattleState)  (để chặn 1 user dính 2 trận)
PVP_SESSIONS: Dict[int, "PvPBattleState"] = {}


class PagedEmbedView(discord.ui.View):
    """Paginator for embeds (10 items/page). Only the command author can use buttons."""

    def __init__(self, pages: List[discord.Embed], user_id: int):
        super().__init__(timeout=120)
        self.pages = pages
        self.index = 0
        self.user_id = int(user_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Không phải trang của bạn.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="⬅️", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = (self.index - 1) % len(self.pages)
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="➡️", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = (self.index + 1) % len(self.pages)
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="❌", style=discord.ButtonStyle.danger)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()


def _calc_dmg(att_atk: int, tgt_def: int, defending: bool) -> int:
    base = max(1, int(att_atk) - (int(tgt_def) // 2))
    if defending:
        base = max(1, base // 2)
    return base


def _hp_bar(cur: int, mx: int, width: int = 10) -> str:
    mx = max(1, int(mx))
    cur = max(0, int(cur))
    filled = int(round(width * cur / mx))
    filled = max(0, min(width, filled))
    return "■" * filled + "□" * (width - filled)


def _pct(x: float) -> str:
    try:
        return f"{float(x)*100:.0f}%"
    except Exception:
        return "0%"


def _gear_brief(user_id: int, animal_id: int) -> str:
    """
    1-line summary for equipped gears.
    Fix case: gear_equips exists but left-join not returning name (missing gear row) -> still show inst_id.
    """
    try:
        rows = zoo_db.list_equipped_gears(int(user_id), int(animal_id))
    except Exception:
        return "🧩 (gear: lỗi đọc DB)"

    parts = []
    for r in rows:
        inst = r.get("inst_id")
        if not inst:
            continue
        rarity = r.get("rarity") or "common"
        icon = RARITY_ICON.get(rarity, "⚪")
        name = r.get("name") or f"ID {inst}"
        parts.append(f"{icon}{name}")

    if not parts:
        return "🧩 (không gear)"
    s = "🧩 " + " | ".join(parts)
    return s[:180]


# =========================================================
#                      PvE STATE
# =========================================================
class BattleState:
    def __init__(
        self,
        user_id: int,
        player_team: List[Dict[str, Any]],
        enemy_team: List[Dict[str, Any]],
        *,
        is_boss: bool = False,
        boss_rarity: str = "",
    ):
        self.user_id = int(user_id)
        self.player_team = player_team
        self.enemy_team = enemy_team
        self.is_boss = bool(is_boss)
        self.boss_rarity = str(boss_rarity or "")

        self.p_idx = 0
        self.e_idx = 0
        self.p_def = False
        self.e_def = False

        self.p_atk_bonus = 0
        self.p_buff_turns = 0

        self.logs: List[str] = []

    def p_active(self) -> Optional[Dict[str, Any]]:
        return None if self.p_idx >= len(self.player_team) else self.player_team[self.p_idx]

    def e_active(self) -> Optional[Dict[str, Any]]:
        return None if self.e_idx >= len(self.enemy_team) else self.enemy_team[self.e_idx]

    def is_over(self) -> bool:
        return self.p_active() is None or self.e_active() is None

    def _tick_buff(self) -> None:
        if self.p_buff_turns > 0:
            self.p_buff_turns -= 1
            if self.p_buff_turns == 0:
                self.p_atk_bonus = 0

    def _switch_if_dead(self) -> None:
        p = self.p_active()
        while p is not None and int(p["cur_hp"]) <= 0:
            self.logs.append(f"☠️ {p['emoji']} **{p['name']}** của bạn gục! ➜ đổi con tiếp theo...")
            self.p_idx += 1
            self.p_def = False
            self.p_atk_bonus = 0
            self.p_buff_turns = 0
            p = self.p_active()

        e = self.e_active()
        while e is not None and int(e["cur_hp"]) <= 0:
            self.logs.append(f"☠️ {e['emoji']} **{e['name']}** của địch gục! ➜ địch đổi con tiếp theo...")
            self.e_idx += 1
            self.e_def = False
            e = self.e_active()

    def _enemy_ai(self) -> str:
        e = self.e_active()
        if not e:
            return "ATK"
        if int(e["cur_hp"]) <= int(e["hp"]) * 0.35 and random.random() < 0.3:
            return "DEF"
        return "ATK" if random.random() < 0.8 else "DEF"

    def render_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=("👑 Boss Battle (PvE)" if self.is_boss else "⚔️ Zoo Battle (PvE)"),
            color=0x2B2D31,
        )
        p = self.p_active()
        e = self.e_active()

        if p is None or e is None:
            if self.p_active() is not None and self.e_active() is None:
                embed.description = "✅ Bạn thắng!"
            elif self.e_active() is not None and self.p_active() is None:
                embed.description = "❌ Bạn thua!"
            else:
                embed.description = "✅ Trận đấu đã kết thúc!"
            if self.logs:
                embed.add_field(name="Log", value="\n".join(self.logs[-8:])[:1024], inline=False)
            return embed

        pstars = "⭐" * int(p.get("stars", 1))
        embed.add_field(
            name="Bạn (Active)",
            value=(
                f"{p['emoji']} **{p['name']}** {pstars}\n"
                f"{_gear_brief(self.user_id, p.get('animal_id', 0))}\n"
                f"HP `{p['cur_hp']}/{p['hp']}` {_hp_bar(int(p['cur_hp']), int(p['hp']))}\n"
                f"ATK {p['atk']} (+{self.p_atk_bonus}) | DEF {p['def']} | SPD {p['speed']} | EVA {_pct(p.get('evasion', 0))}\n"
                f"🛡️ DEF: {'YES' if self.p_def else 'NO'}\n"
                f"🍗 Buff ATK còn: {self.p_buff_turns} lượt"
            ),
            inline=False,
        )

        embed.add_field(
            name="Địch (Active)",
            value=(
                f"{e['emoji']} **{e['name']}** {RARITY_ICON.get(e.get('rarity','common'),'⚪')}\n"
                f"HP `{e['cur_hp']}/{e['hp']}` {_hp_bar(int(e['cur_hp']), int(e['hp']))}\n"
                f"ATK {e['atk']} | DEF {e['def']} | SPD {e['speed']} | EVA {_pct(e.get('evasion', 0))}\n"
                f"🛡️ DEF: {'YES' if self.e_def else 'NO'}"
            ),
            inline=False,
        )

        if self.logs:
            embed.add_field(name="Log", value="\n".join(self.logs[-8:])[:1024], inline=False)

        embed.set_footer(text="ATK / DEF / ITEM  (ITEM: chọn đồ từ inventory)")
        return embed

    def _use_item_specific(self, user_id: int, item_key: str) -> bool:
        p = self.p_active()
        if not p:
            return False

        it = zoo_db.get_item(item_key)
        if not it:
            self.logs.append("❌ Item không tồn tại.")
            return False

        ok = zoo_db.consume_item(user_id, item_key, 1)
        if not ok:
            self.logs.append("❌ Bạn không đủ item này.")
            return False

        kind = it["kind"]
        val = int(it["value"])
        dur = int(it["duration_turns"])

        if kind == "heal":
            before = int(p["cur_hp"])
            p["cur_hp"] = min(int(p["hp"]), int(p["cur_hp"]) + val)
            healed = int(p["cur_hp"]) - before
            self.logs.append(f"🧪 Dùng **{it['name']}**: hồi **+{healed} HP**.")
            return True

        if kind == "buff_atk":
            self.p_atk_bonus = val
            self.p_buff_turns = max(0, dur)
            self.logs.append(f"🍗 Dùng **{it['name']}**: +{self.p_atk_bonus} ATK trong {self.p_buff_turns} lượt.")
            return True

        self.logs.append(f"❌ Item **{it['name']}** chưa hỗ trợ trong battle.")
        return True

    def step(self, action: str, item_key: Optional[str] = None) -> None:
        if self.is_over():
            return

        self._tick_buff()
        p = self.p_active()
        e = self.e_active()
        if not p or not e:
            return

        enemy_action = self._enemy_ai()
        first = "P" if int(p["speed"]) >= int(e["speed"]) else "E"

        def do(side: str, act: str) -> None:
            if self.is_over():
                return

            if side == "P":
                if act == "DEF":
                    self.p_def = True
                    self.logs.append("🛡️ Bạn DEF")
                elif act == "ITEM":
                    if not item_key:
                        self.logs.append("❌ Bạn chưa chọn item.")
                    else:
                        self._use_item_specific(self.user_id, item_key)
                else:
                    if random.random() < float(e.get("evasion", 0.0)):
                        self.e_def = False
                        self.logs.append(f"💨 {e['emoji']} **{e['name']}** né đòn!")
                    else:
                        dmg = _calc_dmg(int(p["atk"]) + int(self.p_atk_bonus), int(e["def"]), self.e_def)
                        self.e_def = False
                        e["cur_hp"] = int(e["cur_hp"]) - dmg
                        self.logs.append(f"⚔️ Bạn ATK: {p['emoji']} **{p['name']}** đánh {e['emoji']} **{e['name']}** **-{dmg}**")
            else:
                if act == "DEF":
                    self.e_def = True
                    self.logs.append("🛡️ Địch DEF")
                else:
                    if random.random() < float(p.get("evasion", 0.0)):
                        self.p_def = False
                        self.logs.append(f"💨 {p['emoji']} **{p['name']}** né đòn!")
                    else:
                        dmg = _calc_dmg(int(e["atk"]), int(p["def"]), self.p_def)
                        self.p_def = False
                        p["cur_hp"] = int(p["cur_hp"]) - dmg
                        self.logs.append(f"⚔️ Địch ATK: {e['emoji']} **{e['name']}** đánh {p['emoji']} **{p['name']}** **-{dmg}**")

            self._switch_if_dead()

        if first == "P":
            do("P", action)
            do("E", enemy_action)
        else:
            do("E", enemy_action)
            do("P", action)


class ItemSelect(discord.ui.Select):
    def __init__(self, state: BattleState):
        self.state = state
        inv = zoo_db.get_inventory(state.user_id)

        options: List[discord.SelectOption] = []
        for r in inv[:25]:
            label = f"{r['name']} x{r['qty']}"
            desc = (r["description"] or "")[:100]
            options.append(discord.SelectOption(label=label[:100], description=desc, value=r["item_key"]))

        if not options:
            options = [discord.SelectOption(label="(Không có item)", value="_none", description="Mua ở /zoo shop")]

        super().__init__(placeholder="Chọn item để dùng (mất lượt)", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.state.user_id:
            await interaction.response.send_message("❌ Không phải trận của bạn.", ephemeral=True)
            return

        val = self.values[0]
        if val == "_none":
            await interaction.response.send_message("🎒 Bạn không có item. Mua ở `/zoo shop`.", ephemeral=True)
            return

        self.state.step("ITEM", item_key=val)

        view = BattleView(self.state)

        if self.state.is_over():
            if self.state.p_active() is not None and self.state.e_active() is None:
                team_ids = [int(x.get("animal_id", 0)) for x in zoo_db.get_team(self.state.user_id)]
                team_ids = [x for x in team_ids if x]

                res = zoo_db.pve_handle_victory(
                    self.state.user_id,
                    is_boss=getattr(self.state, "is_boss", False),
                    team_animal_ids=team_ids,
                )
                reward = int(res["reward"])
                bonus = int(res.get("bonus", 0))
                drop = res.get("dropped")
                streak = int(res.get("streak", 0))

                if getattr(self.state, "is_boss", False):
                    self.state.logs.append(f"👑 Hạ boss! Nhận **+{reward} coins** (bonus gear +{bonus}). Streak reset.")
                else:
                    note = " (⚠️ Trận tiếp theo là BOSS!)" if res.get("next_is_boss") else ""
                    self.state.logs.append(f"🏆 Thắng PvE! Nhận **+{reward} coins** (bonus gear +{bonus}). Streak: {streak}/5{note}")

                if drop:
                    self.state.logs.append(f"🎁 Rơi gear: **{drop['name']}** [{drop['rarity'].upper()}] — ID `{drop['inst_id']}`")

            elif self.state.e_active() is not None and self.state.p_active() is None:
                zoo_db.reset_pve_streak(self.state.user_id)
                self.state.logs.append("❌ Thua PvE! Streak reset về 0.")

            for child in view.children:
                child.disabled = True
            PVE_SESSIONS.pop(self.state.user_id, None)
            PVE_COOLDOWNS[self.state.user_id] = time.time() + PVE_COOLDOWN_SEC

        await interaction.response.edit_message(embed=self.state.render_embed(), view=view)


class ItemPickView(discord.ui.View):
    def __init__(self, state: BattleState):
        super().__init__(timeout=60)
        self.state = state
        self.add_item(ItemSelect(state))

    @discord.ui.button(label="⬅️ Back", style=discord.ButtonStyle.secondary)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.state.render_embed(), view=BattleView(self.state))


class BattleView(discord.ui.View):
    def __init__(self, state: BattleState):
        super().__init__(timeout=180)
        self.state = state

    async def on_timeout(self):
        PVE_SESSIONS.pop(self.state.user_id, None)
        PVE_COOLDOWNS[self.state.user_id] = time.time() + PVE_COOLDOWN_SEC

    async def _handle(self, interaction: discord.Interaction, action: str):
        if interaction.user.id != self.state.user_id:
            await interaction.response.send_message("❌ Không phải trận của bạn.", ephemeral=True)
            return

        self.state.step(action)

        if self.state.is_over():
            if self.state.p_active() is not None and self.state.e_active() is None:
                team_ids = [int(x.get("animal_id", 0)) for x in zoo_db.get_team(self.state.user_id)]
                team_ids = [x for x in team_ids if x]

                res = zoo_db.pve_handle_victory(
                    self.state.user_id,
                    is_boss=getattr(self.state, "is_boss", False),
                    team_animal_ids=team_ids,
                )
                reward = int(res["reward"])
                bonus = int(res.get("bonus", 0))
                drop = res.get("dropped")
                streak = int(res.get("streak", 0))

                if getattr(self.state, "is_boss", False):
                    self.state.logs.append(f"👑 Hạ boss! Nhận **+{reward} coins** (bonus gear +{bonus}). Streak reset.")
                else:
                    note = " (⚠️ Trận tiếp theo là BOSS!)" if res.get("next_is_boss") else ""
                    self.state.logs.append(f"🏆 Thắng PvE! Nhận **+{reward} coins** (bonus gear +{bonus}). Streak: {streak}/5{note}")

                if drop:
                    self.state.logs.append(f"🎁 Rơi gear: **{drop['name']}** [{drop['rarity'].upper()}] — ID `{drop['inst_id']}`")

            elif self.state.e_active() is not None and self.state.p_active() is None:
                zoo_db.reset_pve_streak(self.state.user_id)
                self.state.logs.append("❌ Thua PvE! Streak reset về 0.")

            for child in self.children:
                child.disabled = True
            PVE_SESSIONS.pop(self.state.user_id, None)
            PVE_COOLDOWNS[self.state.user_id] = time.time() + PVE_COOLDOWN_SEC

        await interaction.response.edit_message(embed=self.state.render_embed(), view=self)

    @discord.ui.button(label="ATK", style=discord.ButtonStyle.danger)
    async def atk_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "ATK")

    @discord.ui.button(label="DEF", style=discord.ButtonStyle.secondary)
    async def def_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "DEF")

    @discord.ui.button(label="ITEM", style=discord.ButtonStyle.success)
    async def item_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        inv = zoo_db.get_inventory(self.state.user_id)
        if not inv:
            await interaction.response.send_message("🎒 Bạn chưa có item nào. Mua ở `/zoo shop`.", ephemeral=True)
            return
        await interaction.response.edit_message(embed=self.state.render_embed(), view=ItemPickView(self.state))

    @discord.ui.button(label="🏳️ Run", style=discord.ButtonStyle.secondary)
    async def run_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.state.user_id:
            await interaction.response.send_message("❌ Không phải trận của bạn.", ephemeral=True)
            return

        zoo_db.reset_pve_streak(self.state.user_id)
        self.state.logs.append("🏳️ Bạn đã đầu hàng! Streak reset về 0.")

        for child in self.children:
            child.disabled = True
        PVE_SESSIONS.pop(self.state.user_id, None)
        PVE_COOLDOWNS[self.state.user_id] = time.time() + PVE_COOLDOWN_SEC

        await interaction.response.edit_message(embed=self.state.render_embed(), view=self)


# =========================================================
#                      PvP STATE
# =========================================================
class PvPBattleState:
    def __init__(self, a_id: int, b_id: int, a_team: List[Dict[str, Any]], b_team: List[Dict[str, Any]]):
        self.a_id = int(a_id)
        self.b_id = int(b_id)
        self.a_team = a_team
        self.b_team = b_team

        self.a_idx = 0
        self.b_idx = 0

        self.a_def = False
        self.b_def = False

        self.a_atk_bonus = 0
        self.b_atk_bonus = 0
        self.a_buff_turns = 0
        self.b_buff_turns = 0

        self.logs: List[str] = []

        self.pending: Dict[int, Dict[str, Any]] = {}

    def a_active(self) -> Optional[Dict[str, Any]]:
        return None if self.a_idx >= len(self.a_team) else self.a_team[self.a_idx]

    def b_active(self) -> Optional[Dict[str, Any]]:
        return None if self.b_idx >= len(self.b_team) else self.b_team[self.b_idx]

    def is_over(self) -> bool:
        return self.a_active() is None or self.b_active() is None

    def surrender(self, user_id: int) -> None:
        uid = int(user_id)
        if uid == self.a_id:
            self.logs.append(f"🏳️ <@{uid}> đã đầu hàng! <@{self.b_id}> thắng.")
            self.a_idx = len(self.a_team)
        elif uid == self.b_id:
            self.logs.append(f"🏳️ <@{uid}> đã đầu hàng! <@{self.a_id}> thắng.")
            self.b_idx = len(self.b_team)
        self.pending.clear()

    def _tick_buffs(self):
        if self.a_buff_turns > 0:
            self.a_buff_turns -= 1
            if self.a_buff_turns == 0:
                self.a_atk_bonus = 0
        if self.b_buff_turns > 0:
            self.b_buff_turns -= 1
            if self.b_buff_turns == 0:
                self.b_atk_bonus = 0

    def _switch_if_dead(self):
        a = self.a_active()
        while a is not None and int(a["cur_hp"]) <= 0:
            self.logs.append(f"☠️ {a['emoji']} **{a['name']}** gục! ➜ A đổi con tiếp theo...")
            self.a_idx += 1
            self.a_def = False
            self.a_atk_bonus = 0
            self.a_buff_turns = 0
            a = self.a_active()

        b = self.b_active()
        while b is not None and int(b["cur_hp"]) <= 0:
            self.logs.append(f"☠️ {b['emoji']} **{b['name']}** gục! ➜ B đổi con tiếp theo...")
            self.b_idx += 1
            self.b_def = False
            self.b_atk_bonus = 0
            self.b_buff_turns = 0
            b = self.b_active()

    def _use_item(self, user_id: int, item_key: str) -> None:
        p = self.a_active() if user_id == self.a_id else self.b_active()
        if not p:
            return

        it = zoo_db.get_item(item_key)
        if not it:
            self.logs.append("❌ Item không tồn tại.")
            return

        ok = zoo_db.consume_item(user_id, item_key, 1)
        if not ok:
            self.logs.append("❌ Không đủ item.")
            return

        kind = it["kind"]
        val = int(it["value"])
        dur = int(it["duration_turns"])

        if kind == "heal":
            before = int(p["cur_hp"])
            p["cur_hp"] = min(int(p["hp"]), int(p["cur_hp"]) + val)
            healed = int(p["cur_hp"]) - before
            self.logs.append(f"🧪 <@{user_id}> dùng **{it['name']}**: hồi **+{healed} HP**.")
            return

        if kind == "buff_atk":
            if user_id == self.a_id:
                self.a_atk_bonus = val
                self.a_buff_turns = max(0, dur)
            else:
                self.b_atk_bonus = val
                self.b_buff_turns = max(0, dur)
            self.logs.append(f"🍗 <@{user_id}> dùng **{it['name']}**: +{val} ATK trong {dur} lượt.")
            return

        self.logs.append("❌ Item chưa hỗ trợ.")
        return

    def push_action(self, user_id: int, action: str, item_key: Optional[str] = None):
        if self.is_over():
            return
        if user_id not in (self.a_id, self.b_id):
            return

        self.pending[user_id] = {"action": action, "item_key": item_key}

        if self.a_id in self.pending and self.b_id in self.pending:
            self._resolve_round()
            self.pending.clear()

    def _resolve_round(self):
        if self.is_over():
            return

        self._tick_buffs()

        a = self.a_active()
        b = self.b_active()
        if not a or not b:
            return

        a_act = self.pending[self.a_id]["action"]
        b_act = self.pending[self.b_id]["action"]
        a_item = self.pending[self.a_id].get("item_key")
        b_item = self.pending[self.b_id].get("item_key")

        first = "A" if int(a["speed"]) >= int(b["speed"]) else "B"

        def do(side: str, act: str, item_key: Optional[str]):
            if self.is_over():
                return

            nonlocal a, b
            a = self.a_active()
            b = self.b_active()
            if not a or not b:
                return

            if side == "A":
                if act == "DEF":
                    self.a_def = True
                    self.logs.append(f"🛡️ <@{self.a_id}> DEF")
                elif act == "ITEM":
                    if not item_key:
                        self.logs.append(f"❌ <@{self.a_id}> chưa chọn item.")
                    else:
                        self._use_item(self.a_id, item_key)
                else:
                    if random.random() < float(b.get("evasion", 0.0)):
                        self.b_def = False
                        self.logs.append(f"💨 {b['emoji']} **{b['name']}** né đòn!")
                    else:
                        dmg = _calc_dmg(int(a["atk"]) + int(self.a_atk_bonus), int(b["def"]), self.b_def)
                        self.b_def = False
                        b["cur_hp"] = int(b["cur_hp"]) - dmg
                        self.logs.append(f"⚔️ <@{self.a_id}> ATK: {a['emoji']} **{a['name']}** đánh {b['emoji']} **{b['name']}** **-{dmg}**")
            else:
                if act == "DEF":
                    self.b_def = True
                    self.logs.append(f"🛡️ <@{self.b_id}> DEF")
                elif act == "ITEM":
                    if not item_key:
                        self.logs.append(f"❌ <@{self.b_id}> chưa chọn item.")
                    else:
                        self._use_item(self.b_id, item_key)
                else:
                    if random.random() < float(a.get("evasion", 0.0)):
                        self.a_def = False
                        self.logs.append(f"💨 {a['emoji']} **{a['name']}** né đòn!")
                    else:
                        dmg = _calc_dmg(int(b["atk"]) + int(self.b_atk_bonus), int(a["def"]), self.a_def)
                        self.a_def = False
                        a["cur_hp"] = int(a["cur_hp"]) - dmg
                        self.logs.append(f"⚔️ <@{self.b_id}> ATK: {b['emoji']} **{b['name']}** đánh {a['emoji']} **{a['name']}** **-{dmg}**")

            self._switch_if_dead()

        if first == "A":
            do("A", a_act, a_item)
            do("B", b_act, b_item)
        else:
            do("B", b_act, b_item)
            do("A", a_act, a_item)

    def render_embed(self) -> discord.Embed:
        embed = discord.Embed(title="⚔️ Zoo Battle (PvP)", color=0x2B2D31)

        a = self.a_active()
        b = self.b_active()

        if a is None or b is None:
            if a is not None and b is None:
                embed.description = f"✅ <@{self.a_id}> thắng!"
            elif b is not None and a is None:
                embed.description = f"✅ <@{self.b_id}> thắng!"
            else:
                embed.description = "✅ Trận đấu đã kết thúc!"
            if self.logs:
                embed.add_field(name="Log", value="\n".join(self.logs[-8:])[:1024], inline=False)
            return embed

        embed.add_field(
            name=f"A: <@{self.a_id}>",
            value=(
                f"{a['emoji']} **{a['name']}** {'⭐'*int(a.get('stars',1))}\n"
                f"{_gear_brief(self.a_id, a.get('animal_id', 0))}\n"
                f"HP `{a['cur_hp']}/{a['hp']}` {_hp_bar(int(a['cur_hp']), int(a['hp']))}\n"
                f"ATK {a['atk']} (+{self.a_atk_bonus}) | DEF {a['def']} | SPD {a['speed']} | EVA {_pct(a.get('evasion',0))}\n"
                f"🛡️ DEF: {'YES' if self.a_def else 'NO'} | 🍗 Buff: {self.a_buff_turns}"
            ),
            inline=False,
        )

        embed.add_field(
            name=f"B: <@{self.b_id}>",
            value=(
                f"{b['emoji']} **{b['name']}** {'⭐'*int(b.get('stars',1))}\n"
                f"{_gear_brief(self.b_id, b.get('animal_id', 0))}\n"
                f"HP `{b['cur_hp']}/{b['hp']}` {_hp_bar(int(b['cur_hp']), int(b['hp']))}\n"
                f"ATK {b['atk']} (+{self.b_atk_bonus}) | DEF {b['def']} | SPD {b['speed']} | EVA {_pct(b.get('evasion',0))}\n"
                f"🛡️ DEF: {'YES' if self.b_def else 'NO'} | 🍗 Buff: {self.b_buff_turns}"
            ),
            inline=False,
        )

        if self.pending:
            waiting = []
            if self.a_id not in self.pending:
                waiting.append(f"<@{self.a_id}>")
            if self.b_id not in self.pending:
                waiting.append(f"<@{self.b_id}>")
            if waiting:
                embed.add_field(name="⏳ Đang chờ", value=", ".join(waiting), inline=False)

        if self.logs:
            embed.add_field(name="Log", value="\n".join(self.logs[-8:])[:1024], inline=False)

        embed.set_footer(text="Cả 2 chọn ATK/DEF/ITEM rồi round mới chạy (ITEM: dùng 1 cái, mất lượt)")
        return embed


class PvPItemSelect(discord.ui.Select):
    def __init__(self, st: PvPBattleState, user_id: int):
        self.st = st
        self.user_id = int(user_id)
        inv = zoo_db.get_inventory(self.user_id)

        options: List[discord.SelectOption] = []
        for r in inv[:25]:
            label = f"{r['name']} x{r['qty']}"
            desc = (r["description"] or "")[:100]
            options.append(discord.SelectOption(label=label[:100], description=desc, value=r["item_key"]))

        if not options:
            options = [discord.SelectOption(label="(Không có item)", value="_none", description="Mua ở /zoo shop")]

        super().__init__(placeholder="Chọn item để dùng (mất lượt)", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Không phải menu của bạn.", ephemeral=True)
            return

        val = self.values[0]
        if val == "_none":
            await interaction.response.send_message("🎒 Bạn không có item. Mua ở `/zoo shop`.", ephemeral=True)
            return

        self.st.push_action(self.user_id, "ITEM", item_key=val)

        view = PvPBattleView(self.st)
        if self.st.is_over():
            for child in view.children:
                child.disabled = True
            PVP_SESSIONS.pop(self.st.a_id, None)
            PVP_SESSIONS.pop(self.st.b_id, None)

        await interaction.response.edit_message(embed=self.st.render_embed(), view=view)


class PvPItemPickView(discord.ui.View):
    def __init__(self, st: PvPBattleState, user_id: int):
        super().__init__(timeout=60)
        self.st = st
        self.user_id = int(user_id)
        self.add_item(PvPItemSelect(st, user_id))

    @discord.ui.button(label="⬅️ Back", style=discord.ButtonStyle.secondary)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.st.render_embed(), view=PvPBattleView(self.st))


class PvPBattleView(discord.ui.View):
    def __init__(self, st: PvPBattleState):
        super().__init__(timeout=240)
        self.st = st

    async def on_timeout(self):
        PVP_SESSIONS.pop(self.st.a_id, None)
        PVP_SESSIONS.pop(self.st.b_id, None)

    async def _choose(self, interaction: discord.Interaction, action: str):
        uid = interaction.user.id
        if uid not in (self.st.a_id, self.st.b_id):
            await interaction.response.send_message("❌ Không phải trận của bạn.", ephemeral=True)
            return

        if action == "ITEM":
            inv = zoo_db.get_inventory(uid)
            if not inv:
                await interaction.response.send_message("🎒 Bạn chưa có item nào. Mua ở `/zoo shop`.", ephemeral=True)
                return
            await interaction.response.edit_message(embed=self.st.render_embed(), view=PvPItemPickView(self.st, uid))
            return

        self.st.push_action(uid, action)

        if self.st.is_over():
            for child in self.children:
                child.disabled = True
            PVP_SESSIONS.pop(self.st.a_id, None)
            PVP_SESSIONS.pop(self.st.b_id, None)

        await interaction.response.edit_message(embed=self.st.render_embed(), view=self)

    @discord.ui.button(label="ATK", style=discord.ButtonStyle.danger)
    async def atk_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._choose(interaction, "ATK")

    @discord.ui.button(label="DEF", style=discord.ButtonStyle.secondary)
    async def def_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._choose(interaction, "DEF")

    @discord.ui.button(label="ITEM", style=discord.ButtonStyle.success)
    async def item_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._choose(interaction, "ITEM")

    @discord.ui.button(label="🏳️ Run", style=discord.ButtonStyle.secondary)
    async def run_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        if uid not in (self.st.a_id, self.st.b_id):
            await interaction.response.send_message("❌ Không phải trận của bạn.", ephemeral=True)
            return

        self.st.surrender(uid)
        for child in self.children:
            child.disabled = True
        PVP_SESSIONS.pop(self.st.a_id, None)
        PVP_SESSIONS.pop(self.st.b_id, None)

        await interaction.response.edit_message(embed=self.st.render_embed(), view=self)


class PvPChallengeView(discord.ui.View):
    def __init__(self, cog: "Zoo", challenger_id: int, target_id: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.challenger_id = int(challenger_id)
        self.target_id = int(target_id)

    @discord.ui.button(label="✅ Accept", style=discord.ButtonStyle.success)
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target_id:
            await interaction.response.send_message("❌ Bạn không phải người được thách đấu.", ephemeral=True)
            return
        await self.cog._start_pvp(interaction, self.challenger_id, self.target_id)

    @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.danger)
    async def decline_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target_id:
            await interaction.response.send_message("❌ Bạn không phải người được thách đấu.", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="❌ Kèo PvP đã bị từ chối.", view=self)


# =========================================================
#                         COG
# =========================================================
class Zoo(commands.GroupCog, name="zoo"):
    gear = app_commands.Group(name="gear", description="Quản lý gear/trang bị")
    market = app_commands.Group(name="market", description="Chợ đen: mua bán thú/gear")
    admin = app_commands.Group(name="admin", description="Lệnh admin")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    # ========= BASIC =========
    @app_commands.command(name="start", description="Create your zoo account")
    async def start(self, interaction: discord.Interaction):
        zoo_db.ensure_user(interaction.user.id)
        await interaction.response.send_message("✅ Tạo sở thú xong! Dùng `/zoo profile`.", ephemeral=False)

    @app_commands.command(name="profile", description="Show your zoo profile")
    async def profile(self, interaction: discord.Interaction):
        u = zoo_db.get_user(interaction.user.id)
        owned = zoo_db.count_user_animals(interaction.user.id)

        embed = discord.Embed(title="🏟️ Zoo Profile", color=0x2B2D31)
        embed.add_field(name="💰 Coins", value=str(u["coins"]), inline=True)
        embed.add_field(name="🏗️ Zoo Level", value=str(u["zoo_level"]), inline=True)
        embed.add_field(name="📦 Capacity", value=f"{owned}/{u['capacity']}", inline=True)
        if hasattr(zoo_db, "GACHA_COST"):
            embed.set_footer(text=f"Gacha cost: {zoo_db.GACHA_COST} coins")
        await interaction.response.send_message(embed=embed, ephemeral=False)

    # ========= GACHA / ANIMALS =========
    @app_commands.command(name="gacha", description="Roll 1 random animal (fixed cost)")
    async def gacha(self, interaction: discord.Interaction):
        ok, data = zoo_db.do_gacha(interaction.user.id)
        if not ok:
            await interaction.response.send_message(f"❌ {data}", ephemeral=False)
            return

        stars = "⭐" * int(data.get("stars", 1))
        embed = discord.Embed(title=f"🎟️ Gacha: {data['emoji']} {data['name']} {stars}", color=0x2B2D31)
        embed.add_field(name="Rarity", value=f"{RARITY_ICON.get(data['rarity'], '⭐')} `{data['rarity']}`", inline=True)
        embed.add_field(name="Spent", value=f"💰 {data['cost']}", inline=True)
        embed.add_field(name="Animal ID", value=f"`{data['animal_id']}`", inline=True)
        embed.add_field(name="❤️ HP", value=str(data["hp"]), inline=True)
        embed.add_field(name="⚔️ ATK", value=str(data["atk"]), inline=True)
        embed.add_field(name="🛡️ DEF", value=str(data["def"]), inline=True)
        embed.add_field(name="🏃 SPEED", value=str(data["speed"]), inline=True)
        embed.add_field(name="💨 EVA", value=_pct(data.get("evasion", 0.0)), inline=True)
        embed.add_field(name="💵 Income/min", value=str(data["income_per_min"]), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=False)

    @app_commands.command(name="animals", description="Show your animals list")
    async def animals(self, interaction: discord.Interaction):
        rows = zoo_db.list_animals(interaction.user.id)
        if not rows:
            await interaction.response.send_message("Bạn chưa có thú nào. Dùng `/zoo gacha`.", ephemeral=False)
            return

        pages: List[discord.Embed] = []
        chunk_size = 10
        total_pages = (len(rows) - 1) // chunk_size + 1

        for page_idx in range(total_pages):
            chunk = rows[page_idx * chunk_size : (page_idx + 1) * chunk_size]
            embed = discord.Embed(title=f"🦁 Your Animals — Page {page_idx + 1}/{total_pages}", color=0x2B2D31)
            lines = []
            for r in chunk:
                stars = "⭐" * int(r.get("stars", 1))
                lines.append(
                    f"**`{r['id']}`** {stars} {r['emoji']} **{r['name']}** {RARITY_ICON.get(r['rarity'], '⭐')}\n"
                    f"❤️{r['hp']} ⚔️{r['atk']} 🛡️{r['def']} 🏃{r['speed']} 💨{_pct(r.get('evasion', 0))} | 💵 {r['income_per_min']}/min"
                )
            embed.description = "\n\n".join(lines) if lines else "(trống)"
            pages.append(embed)

        view = PagedEmbedView(pages, interaction.user.id)
        await interaction.response.send_message(embed=pages[0], view=view, ephemeral=False)

    @app_commands.command(name="library", description="Hiển thị toàn bộ thư viện thú trong game")
    async def library(self, interaction: discord.Interaction):
        with zoo_db.connect() as con:
            rows = con.execute(
                """
                SELECT name, emoji, rarity,
                       base_hp, base_atk, base_def,
                       base_speed, base_income_per_min
                FROM species
                ORDER BY
                    CASE rarity
                        WHEN 'common' THEN 1
                        WHEN 'uncommon' THEN 2
                        WHEN 'rare' THEN 3
                        WHEN 'epic' THEN 4
                        WHEN 'legendary' THEN 5
                        WHEN 'mythic' THEN 6
                    END,
                    name ASC
                """
            ).fetchall()

        if not rows:
            await interaction.response.send_message("Thư viện thú trống.", ephemeral=True)
            return

        pages: List[discord.Embed] = []
        chunk_size = 10
        total_pages = (len(rows) - 1) // chunk_size + 1

        for page_idx in range(total_pages):
            chunk = rows[page_idx * chunk_size : (page_idx + 1) * chunk_size]
            embed = discord.Embed(title=f"📚 Zoo Animal Library — Page {page_idx + 1}/{total_pages}", color=0x2B2D31)
            parts = []
            for r in chunk:
                parts.append(
                    f"{r['emoji']} **{r['name']}** [{r['rarity'].upper()}]\n"
                    f"❤️{r['base_hp']} ⚔️{r['base_atk']} 🛡️{r['base_def']} "
                    f"🏃{r['base_speed']} 💰{r['base_income_per_min']}/min"
                )
            embed.description = "\n\n".join(parts) if parts else "(trống)"
            pages.append(embed)

        view = PagedEmbedView(pages, interaction.user.id)
        await interaction.response.send_message(embed=pages[0], view=view, ephemeral=False)

    @app_commands.command(name="starup", description="Nâng sao cho thú (tối đa 5⭐) — tăng stats + income + né")
    async def starup(self, interaction: discord.Interaction, animal_id: int):
        ok, data = zoo_db.upgrade_star(interaction.user.id, animal_id)
        if not ok:
            await interaction.response.send_message(f"❌ {data}", ephemeral=False)
            return

        await interaction.response.send_message(
            f"⭐ Nâng sao thành công!\n"
            f"- Animal ID: `{animal_id}`\n"
            f"- {data['old']}⭐ ➜ {data['new']}⭐\n"
            f"- Tốn: **{data['cost']} coins**\n"
            f"- Stats mới: ❤️{data['hp']} ⚔️{data['atk']} 🛡️{data['def']} 🏃{data['speed']} 💨{_pct(data.get('evasion',0))} | 💵 {data['income_per_min']}/min",
            ephemeral=False,
        )

    @app_commands.command(name="sell", description="Bán thú để nhận coins")
    async def sell(self, interaction: discord.Interaction, animal_id: int):
        ok, data = zoo_db.sell_animal(interaction.user.id, animal_id)
        if not ok:
            await interaction.response.send_message(f"❌ {data}", ephemeral=False)
            return

        stars = "⭐" * int(data.get("stars", 1))
        await interaction.response.send_message(
            f"💰 Bạn đã bán {data['emoji']} **{data['name']}** {stars}\nNhận được **{data['price']} coins**",
            ephemeral=False,
        )

    @app_commands.command(name="sellmany", description="Bán nhiều thú cùng lúc để nhận coins")
    @app_commands.describe(animal_ids="Danh sách ID, ví dụ: 1,2,3 hoặc 5-8 hoặc 1 2 3")
    async def sellmany(self, interaction: discord.Interaction, animal_ids: str):
        raw = animal_ids.replace(",", " ").replace(";", " ")
        parts = [p.strip() for p in raw.split() if p.strip()]
        ids: list[int] = []
        for p in parts:
            if "-" in p:
                a, b = p.split("-", 1)
                if a.isdigit() and b.isdigit():
                    lo, hi = int(a), int(b)
                    if lo > hi:
                        lo, hi = hi, lo
                    ids.extend(list(range(lo, hi + 1)))
            else:
                if p.isdigit():
                    ids.append(int(p))

        seen = set()
        ids = [x for x in ids if not (x in seen or seen.add(x))]

        if not ids:
            await interaction.response.send_message("❌ Bạn chưa nhập ID hợp lệ. Ví dụ: `1,2,3` hoặc `5-8`.", ephemeral=True)
            return
        if len(ids) > 50:
            await interaction.response.send_message("❌ Tối đa bán 50 thú mỗi lần.", ephemeral=True)
            return

        sold = []
        failed = []
        total = 0

        for aid in ids:
            ok, data = zoo_db.sell_animal(interaction.user.id, aid)
            if not ok:
                failed.append(aid)
                continue
            total += int(data.get("price", 0))
            sold.append((aid, data.get("emoji", ""), data.get("name", ""), int(data.get("stars", 1)), int(data.get("price", 0))))

        if not sold:
            await interaction.response.send_message(
                f"❌ Không bán được thú nào. ID lỗi: {', '.join(map(str, failed[:20]))}{'...' if len(failed)>20 else ''}",
                ephemeral=True,
            )
            return

        sold_lines = []
        for aid, emoji, name, stars, price in sold[:10]:
            sold_lines.append(f"`{aid}` {emoji} **{name}** {'⭐'*stars} (+{price})")
        more = f"\n… và **{len(sold)-10}** thú khác" if len(sold) > 10 else ""

        fail_note = ""
        if failed:
            fail_note = f"\n⚠️ Không tìm thấy/không bán được ID: {', '.join(map(str, failed[:15]))}{'...' if len(failed)>15 else ''}"

        await interaction.response.send_message(
            f"💰 Đã bán **{len(sold)}** thú, nhận tổng **{total} coins**.\n" + "\n".join(sold_lines) + more + fail_note,
            ephemeral=False,
        )

    # ========= ZOO / COINS =========
    @app_commands.command(name="upgrade", description="Upgrade your zoo (increase capacity)")
    async def upgrade(self, interaction: discord.Interaction):
        ok, data = zoo_db.do_upgrade(interaction.user.id)
        if not ok:
            await interaction.response.send_message(f"❌ {data}", ephemeral=False)
            return
        await interaction.response.send_message(
            f"🏗️ Nâng cấp sở thú thành công!\nTốn **{data['cost']}** coins.\nLevel: **{data['new_level']}** | Capacity: **{data['new_capacity']}**",
            ephemeral=False,
        )

    @app_commands.command(name="collect", description="Collect coins from your animals (realtime)")
    async def collect(self, interaction: discord.Interaction):
        try:
            u = zoo_db.get_user(interaction.user.id)
            if int(u.get("last_collect", 0)) == 0:
                now = int(time.time())
                with zoo_db.connect() as con:
                    con.execute("UPDATE users SET last_collect=? WHERE user_id=?", (now, int(interaction.user.id)))
                await interaction.response.send_message(
                    "✅ Đã kích hoạt thu tiền. Lần sau quay lại sau vài phút để nhận coins (cap 8h).",
                    ephemeral=False,
                )
                return
        except Exception:
            pass

        ok, data = zoo_db.do_collect(interaction.user.id)
        if not ok:
            await interaction.response.send_message(f"❌ {data}", ephemeral=False)
            return

        cap_note = " (CAP 8h)" if data.get("capped") else ""
        extra = ""
        if "gear_bonus_per_min" in data:
            extra = f"\n- 🧩 Gear bonus: **+{data['gear_bonus_per_min']} / phút**"
        await interaction.response.send_message(
            f"💰 Thu tiền thành công!\n"
            f"- ⏱️ Thời gian: **{data['minutes']} phút**{cap_note}\n"
            f"- 📈 Tổng income: **{data['total_per_min']} / phút**{extra}\n"
            f"- ✅ Nhận: **{data['earned']} coins**\n"
            f"- 🪙 Coins hiện tại: **{data['coins']}**",
            ephemeral=False,
        )

    # ========= TEAM =========
    @app_commands.command(name="setteam", description="Set an animal to a team slot")
    async def setteam(self, interaction: discord.Interaction, slot: int, animal_id: int):
        ok, msg = zoo_db.set_team_slot(interaction.user.id, slot, animal_id)
        if not ok:
            await interaction.response.send_message(f"❌ {msg}", ephemeral=False)
            return
        await interaction.response.send_message(f"✅ Set slot **{slot}** = animal **{animal_id}**", ephemeral=False)

    @app_commands.command(name="team", description="Show your current team formation")
    async def team(self, interaction: discord.Interaction):
        rows = zoo_db.get_team(interaction.user.id)
        if not rows:
            await interaction.response.send_message("Bạn chưa set đội hình. Dùng `/zoo setteam`.", ephemeral=False)
            return

        embed = discord.Embed(title="⚔️ Your Team", color=0x2B2D31)
        lines = []
        for r in rows:
            stars = "⭐" * int(r.get("stars", 1))
            lines.append(
                f"**Slot {r['slot']}** — `{r['animal_id']}` {stars} {r['emoji']} **{r['name']}** {RARITY_ICON.get(r['rarity'],'⭐')}\n"
                f"❤️{r['hp']} ⚔️{r['atk']} 🛡️{r['def']} 🏃{r['speed']} 💨{_pct(r.get('evasion',0))} | 💵 {r['income_per_min']}/min\n"
                f"{_gear_brief(interaction.user.id, r.get('animal_id',0))}"
            )
        embed.description = "\n\n".join(lines)
        await interaction.response.send_message(embed=embed, ephemeral=False)

    # ========= SHOP / ITEMS =========
    @app_commands.command(name="shop", description="Show shop items")
    async def shop(self, interaction: discord.Interaction):
        rows = zoo_db.list_shop_items()
        embed = discord.Embed(title="🛒 Zoo Shop", color=0x2B2D31)
        lines = []
        for r in rows:
            lines.append(f"**{r['name']}** — `{r['item_key']}`\n{r['description']}\n💰 {r['price']} coins")
        embed.description = "\n\n".join(lines) if lines else "Shop trống."
        await interaction.response.send_message(embed=embed, ephemeral=False)

    @app_commands.command(name="buy", description="Buy an item from shop")
    async def buy(self, interaction: discord.Interaction, item_key: str, qty: int):
        ok, data = zoo_db.buy_item(interaction.user.id, item_key, qty)
        if not ok:
            await interaction.response.send_message(f"❌ {data}", ephemeral=False)
            return
        await interaction.response.send_message(
            f"✅ Mua {data['qty']} x {data['name']} (tốn {data['cost']} coins). Coins còn: {data['coins']}",
            ephemeral=False,
        )

    @app_commands.command(name="inv", description="Show your inventory")
    async def inv(self, interaction: discord.Interaction):
        rows = zoo_db.get_inventory(interaction.user.id)
        if not rows:
            await interaction.response.send_message("🎒 Bạn chưa có item nào. Dùng `/zoo shop` để mua.", ephemeral=False)
            return
        embed = discord.Embed(title="🎒 Inventory", color=0x2B2D31)
        embed.description = "\n".join([f"**{r['name']}** `{r['item_key']}` x **{r['qty']}** — {r['description']}" for r in rows])
        await interaction.response.send_message(embed=embed, ephemeral=False)

    # ========= GEAR =========
    @gear.command(name="inv", description="Xem kho gear (trang bị) của bạn")
    async def gearinv(self, interaction: discord.Interaction):
        rows = zoo_db.list_gears(interaction.user.id)
        if not rows:
            await interaction.response.send_message("🧰 Bạn chưa có gear nào. Thắng PvE/Boss để rơi gear.", ephemeral=False)
            return

        pages: List[discord.Embed] = []
        chunk_size = 10
        total_pages = (len(rows) - 1) // chunk_size + 1
        for page_idx in range(total_pages):
            chunk = rows[page_idx * chunk_size : (page_idx + 1) * chunk_size]
            embed = discord.Embed(title=f"🧰 Gear Inventory — Page {page_idx + 1}/{total_pages}", color=0x2B2D31)
            lines = []
            for g in chunk:
                lines.append(
                    f"**`{g['inst_id']}`** {RARITY_ICON.get(g['rarity'], '⚪')} **{g['name']}** [{g['rarity'].upper()}]\n"
                    f"⚔️+{g['atk']} ❤️+{g['hp']} 🛡️+{g['def']} 🏃+{g['speed']} 💨+{_pct(g['evasion'])} "
                    f"💰+{g['money_bonus']}/min | 💲Sell {g['price']}"
                )
            embed.description = "\n\n".join(lines)[:4000]
            pages.append(embed)

        view = PagedEmbedView(pages, interaction.user.id)
        await interaction.response.send_message(embed=pages[0], view=view, ephemeral=False)

    @gear.command(name="equip", description="Trang bị gear cho thú (slot 1-3)")
    async def gearequip(self, interaction: discord.Interaction, animal_id: int, slot: int, gear_id: str):
        ok, data = zoo_db.equip_gear(interaction.user.id, animal_id, slot, gear_id)
        if not ok:
            await interaction.response.send_message(f"❌ {data}", ephemeral=True)
            return
        g = data["gear"]
        await interaction.response.send_message(
            f"✅ Đã trang bị gear **{g['name']}** [{g['rarity'].upper()}] (ID `{g['inst_id']}`) vào thú `{animal_id}` slot {slot}.",
            ephemeral=False,
        )

    @gear.command(name="unequip", description="Tháo gear khỏi thú (slot 1-3)")
    async def gearunequip(self, interaction: discord.Interaction, animal_id: int, slot: int):
        ok, data = zoo_db.unequip_gear(interaction.user.id, animal_id, slot)
        if not ok:
            await interaction.response.send_message(f"❌ {data}", ephemeral=True)
            return
        g = data.get("gear")
        if g:
            await interaction.response.send_message(
                f"✅ Đã tháo gear **{g['name']}** (ID `{data['inst_id']}`) khỏi thú `{animal_id}` slot {slot}.",
                ephemeral=False,
            )
        else:
            await interaction.response.send_message(
                f"✅ Đã tháo gear ID `{data['inst_id']}` khỏi thú `{animal_id}` slot {slot}.",
                ephemeral=False,
            )

    @gear.command(name="slots", description="Xem gear đang trang bị trên 1 thú")
    async def gearslots(self, interaction: discord.Interaction, animal_id: int):
        rows = zoo_db.list_equipped_gears(interaction.user.id, animal_id)
        embed = discord.Embed(title=f"🧩 Gear on Animal `{animal_id}`", color=0x2B2D31)
        lines = []
        for r in rows:
            if not r.get("inst_id"):
                lines.append(f"Slot {r['slot']}: (trống)")
            else:
                lines.append(
                    f"Slot {r['slot']}: `{r['inst_id']}` {RARITY_ICON.get(r.get('rarity','common'),'⚪')} **{r.get('name','?')}** "
                    f"⚔️+{r.get('atk',0)} ❤️+{r.get('hp',0)} 🛡️+{r.get('def',0)} 🏃+{r.get('speed',0)} "
                    f"💨+{_pct(r.get('evasion',0.0))} 💰+{r.get('money_bonus',0)}/min"
                )
        embed.description = "\n".join(lines) if lines else "(trống)"
        await interaction.response.send_message(embed=embed, ephemeral=False)

    @gear.command(name="sell", description="Bán 1 hoặc nhiều gear theo ID (cách nhau bởi , hoặc space)")
    async def gearsell(self, interaction: discord.Interaction, gear_ids: str):
        raw = gear_ids.replace(",", " ").replace(";", " ")
        ids = [x.strip() for x in raw.split() if x.strip()]
        ok, data = zoo_db.sell_gears(interaction.user.id, ids)
        if not ok:
            await interaction.response.send_message(f"❌ {data}", ephemeral=True)
            return

        sold = data["sold"]
        failed = data["failed"]
        total = data["total"]

        lines = []
        for s in sold[:10]:
            lines.append(f"`{s['inst_id']}` **{s['name']}** [{s['rarity'].upper()}] (+{s['price']})")
        more = f"\n… và **{len(sold)-10}** gear khác" if len(sold) > 10 else ""

        fail_note = ""
        if failed:
            fail_note = "\n⚠️ Không bán được: " + ", ".join([f"{gid}({reason})" for gid, reason in failed[:10]])

        await interaction.response.send_message(
            f"💰 Đã bán **{len(sold)}** gear, nhận **{total} coins**. Coins hiện tại: **{data['coins']}**\n"
            + ("\n".join(lines) + more if lines else "")
            + fail_note,
            ephemeral=False,
        )

    # ========= BLACK MARKET (CHỢ ĐEN) =========
    @market.command(name="list", description="Xem chợ đen (listing thú/gear)")
    async def market_list(self, interaction: discord.Interaction, page: int = 1):
        data = zoo_db.market_list(page=page, page_size=10)
        rows = data["rows"]
        if not rows:
            await interaction.response.send_message("🕳️ Chợ đen đang trống.", ephemeral=False)
            return

        total = data["total"]
        total_pages = max(1, (total - 1) // 10 + 1)
        pages: List[discord.Embed] = []

        for p in range(1, total_pages + 1):
            d = zoo_db.market_list(page=p, page_size=10)
            chunk = d["rows"]
            embed = discord.Embed(title=f"🕳️ Black Market — Page {p}/{total_pages}", color=0x2B2D31)
            lines = []
            for r in chunk:
                lid = r["listing_id"]
                seller = r["seller_id"]
                price = r["price"]
                if r["item_type"] == "animal":
                    a = r.get("payload") or {}
                    stars = "⭐" * int(a.get("stars", 1))
                    lines.append(
                        f"**`{lid}`** 🐾 **THÚ** — 💰 **{price}** | seller <@{seller}>\n"
                        f"{a.get('emoji','🐾')} **{a.get('name','?')}** {stars} {RARITY_ICON.get(a.get('rarity','common'),'⚪')} "
                        f"❤️{a.get('hp','?')} ⚔️{a.get('atk','?')} 🛡️{a.get('def','?')} 🏃{a.get('speed','?')} 💨{_pct(a.get('evasion',0))} | 💵 {a.get('income_per_min','?')}/min"
                    )
                else:
                    g = r.get("payload") or {}
                    lines.append(
                        f"**`{lid}`** 🧰 **GEAR** — 💰 **{price}** | seller <@{seller}>\n"
                        f"`{g.get('inst_id','?')}` {RARITY_ICON.get(g.get('rarity','common'),'⚪')} **{g.get('name','?')}** [{str(g.get('rarity','')).upper()}]\n"
                        f"⚔️+{g.get('atk',0)} ❤️+{g.get('hp',0)} 🛡️+{g.get('def',0)} 🏃+{g.get('speed',0)} 💨+{_pct(g.get('evasion',0))} 💰+{g.get('money_bonus',0)}/min"
                    )
            embed.description = "\n\n".join(lines)[:4000]
            embed.set_footer(text="Mua: /zoo market buy <listing_id>")
            pages.append(embed)

        view = PagedEmbedView(pages, interaction.user.id)
        idx = max(1, min(int(page), len(pages))) - 1
        await interaction.response.send_message(embed=pages[idx], view=view, ephemeral=False)

    @market.command(name="mine", description="Xem listing bạn đang bán trong chợ đen")
    async def market_mine(self, interaction: discord.Interaction, page: int = 1):
        data = zoo_db.market_list_mine(interaction.user.id, page=page, page_size=10)
        rows = data["rows"]
        if not rows:
            await interaction.response.send_message("🧾 Bạn chưa có listing nào đang bán.", ephemeral=False)
            return

        total = data["total"]
        total_pages = max(1, (total - 1) // 10 + 1)

        pages: List[discord.Embed] = []
        for p in range(1, total_pages + 1):
            d = zoo_db.market_list_mine(interaction.user.id, page=p, page_size=10)
            chunk = d["rows"]
            embed = discord.Embed(title=f"🧾 My Listings — Page {p}/{total_pages}", color=0x2B2D31)
            lines = []
            for r in chunk:
                lid = r["listing_id"]
                price = r["price"]
                if r["item_type"] == "animal":
                    a = r.get("payload") or {}
                    stars = "⭐" * int(a.get("stars", 1))
                    lines.append(
                        f"**`{lid}`** 🐾 THÚ — 💰 **{price}**\n"
                        f"{a.get('emoji','🐾')} **{a.get('name','?')}** {stars} {RARITY_ICON.get(a.get('rarity','common'),'⚪')}"
                    )
                else:
                    g = r.get("payload") or {}
                    lines.append(
                        f"**`{lid}`** 🧰 GEAR — 💰 **{price}**\n"
                        f"`{g.get('inst_id','?')}` {RARITY_ICON.get(g.get('rarity','common'),'⚪')} **{g.get('name','?')}**"
                    )
            embed.description = "\n\n".join(lines)[:4000]
            embed.set_footer(text="Huỷ: /zoo market cancel <listing_id>")
            pages.append(embed)

        view = PagedEmbedView(pages, interaction.user.id)
        idx = max(1, min(int(page), len(pages))) - 1
        await interaction.response.send_message(embed=pages[idx], view=view, ephemeral=False)

    @market.command(name="sell_animal", description="Đăng bán 1 thú lên chợ đen")
    async def market_sell_animal(self, interaction: discord.Interaction, animal_id: int, price: int):
        ok, data = zoo_db.market_sell_animal(interaction.user.id, animal_id, price)
        if not ok:
            await interaction.response.send_message(f"❌ {data}", ephemeral=True)
            return
        stars = "⭐" * int(data.get("stars", 1))
        await interaction.response.send_message(
            f"🕳️ Đã đăng bán {data['emoji']} **{data['name']}** {stars}\n"
            f"- Listing ID: `{data['listing_id']}`\n"
            f"- Giá: **{data['price']} coins**\n"
            f"Người khác mua bằng `/zoo market buy {data['listing_id']}`",
            ephemeral=False,
        )

    @market.command(name="sell_gear", description="Đăng bán 1 gear lên chợ đen")
    async def market_sell_gear(self, interaction: discord.Interaction, gear_id: str, price: int):
        ok, data = zoo_db.market_sell_gear(interaction.user.id, gear_id, price)
        if not ok:
            await interaction.response.send_message(f"❌ {data}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🕳️ Đã đăng bán gear **{data['name']}** [{str(data['rarity']).upper()}]\n"
            f"- Gear ID: `{data['inst_id']}`\n"
            f"- Listing ID: `{data['listing_id']}`\n"
            f"- Giá: **{data['price']} coins**",
            ephemeral=False,
        )

    @market.command(name="buy", description="Mua 1 listing trong chợ đen")
    async def market_buy(self, interaction: discord.Interaction, listing_id: int):
        ok, data = zoo_db.market_buy(interaction.user.id, listing_id)
        if not ok:
            await interaction.response.send_message(f"❌ {data}", ephemeral=True)
            return

        fee = int(data.get("fee", 0))
        msg = (
            f"✅ Mua thành công listing `{listing_id}`!\n"
            f"- Giá: **{data['price']} coins**\n"
            f"- Phí chợ đen: **{fee}** (trừ vào tiền người bán)\n"
            f"- Coins của bạn còn: **{data.get('buyer_coins','?')}**"
        )
        if data["item_type"] == "animal":
            msg += f"\n🐾 Bạn nhận được **animal_id `{data['animal_id']}`**"
        else:
            msg += f"\n🧰 Bạn nhận được **gear `{data['gear_id']}`**"
        await interaction.response.send_message(msg, ephemeral=False)

    @market.command(name="cancel", description="Huỷ bán (chỉ chủ listing)")
    async def market_cancel(self, interaction: discord.Interaction, listing_id: int):
        ok, data = zoo_db.market_cancel(interaction.user.id, listing_id)
        if not ok:
            await interaction.response.send_message(f"❌ {data}", ephemeral=True)
            return

        if data["item_type"] == "animal":
            await interaction.response.send_message(
                f"✅ Đã huỷ listing `{listing_id}` và trả lại thú. Animal ID mới: `{data['animal_id']}`",
                ephemeral=False,
            )
        else:
            await interaction.response.send_message(
                f"✅ Đã huỷ listing `{listing_id}` và trả lại gear `{data['gear_id']}`",
                ephemeral=False,
            )

    # ========= DAILY / PAY =========
    @app_commands.command(name="daily", description="Nhận daily coins (24h 1 lần)")
    async def daily(self, interaction: discord.Interaction):
        ok, data = zoo_db.claim_daily(interaction.user.id)
        if not ok:
            remain = int(data["remain"])
            h = remain // 3600
            m = (remain % 3600) // 60
            s = remain % 60
            await interaction.response.send_message(f"⏳ Bạn đã nhận daily rồi. Quay lại sau **{h}h {m}m {s}s**.", ephemeral=False)
            return
        await interaction.response.send_message(f"🎁 Daily nhận: **+{data['reward']} coins**\n🪙 Coins hiện tại: **{data['coins']}**", ephemeral=False)

    @app_commands.command(name="pay", description="Chuyển coins cho người khác")
    async def pay(self, interaction: discord.Interaction, user: discord.Member, amount: int):
        if user.bot:
            await interaction.response.send_message("❌ Không thể chuyển tiền cho bot.", ephemeral=False)
            return
        ok, data = zoo_db.pay_coins(interaction.user.id, user.id, amount)
        if not ok:
            await interaction.response.send_message(f"❌ {data}", ephemeral=False)
            return

        await interaction.response.send_message(
            f"✅ Bạn đã chuyển **{data['sent']} coins** cho {user.mention}.\n🪙 Coins của bạn còn: **{data['sender_coins']}**",
            ephemeral=False,
        )

    # ========= ADMIN =========
    @admin.command(name="addcoins", description="(Admin) Cộng coins cho user")
    @app_commands.checks.has_permissions(administrator=True)
    async def admin_addcoins(self, interaction: discord.Interaction, user: discord.Member, amount: int):
        if amount == 0:
            await interaction.response.send_message("❌ Amount phải khác 0", ephemeral=False)
            return

        ok = zoo_db.admin_add_coins(user.id, amount)
        if not ok:
            await interaction.response.send_message("❌ Không thể cộng coins.", ephemeral=False)
            return

        await interaction.response.send_message(f"💰 Admin đã cộng **{amount} coins** cho {user.mention}", ephemeral=False)

    # ========= PvE =========
    @app_commands.command(name="battle_ui", description="Pokemon-style PvE battle (ATK/DEF/ITEM)")
    async def battle_ui(self, interaction: discord.Interaction):
        uid = interaction.user.id

        now = time.time()
        ready_at = PVE_COOLDOWNS.get(uid, 0)
        if now < ready_at:
            left = max(1, int(ready_at - now))
            await interaction.response.send_message(f"⏳ Bạn vừa đánh PvE xong. Chờ **{left}s** rồi đánh tiếp nhé.", ephemeral=True)
            return

        if uid in PVE_SESSIONS:
            st = PVE_SESSIONS[uid]
            await interaction.response.send_message("⚠️ Bạn đang có trận PvE chưa kết thúc!", embed=st.render_embed(), view=BattleView(st), ephemeral=False)
            return

        team = zoo_db.get_team(uid)
        if not team:
            await interaction.response.send_message("❌ Bạn chưa set đội hình. Dùng `/zoo setteam`.", ephemeral=False)
            return

        player_team: List[Dict[str, Any]] = []
        for r in team[:5]:
            bonus = zoo_db.sum_gear_bonus(uid, int(r["animal_id"]))
            player_team.append(
                {
                    "slot": int(r["slot"]),
                    "animal_id": int(r["animal_id"]),
                    "name": r["name"],
                    "emoji": r["emoji"],
                    "rarity": r.get("rarity", "common"),
                    "stars": int(r.get("stars", 1)),
                    "hp": int(r["hp"]) + int(bonus.get("hp", 0)),
                    "atk": int(r["atk"]) + int(bonus.get("atk", 0)),
                    "def": int(r["def"]) + int(bonus.get("def", 0)),
                    "speed": int(r["speed"]) + int(bonus.get("speed", 0)),
                    "evasion": float(r.get("evasion", 0.03)) + float(bonus.get("evasion", 0.0)),
                    "cur_hp": int(r["hp"]) + int(bonus.get("hp", 0)),
                }
            )

        u = zoo_db.get_user(uid)
        streak = zoo_db.get_pve_streak(uid)
        is_boss = streak >= 5
        boss_rarity = ""
        if is_boss:
            boss_rarity = zoo_db.roll_boss_rarity()
            enemy_team = zoo_db.make_boss_enemy_team(int(u["zoo_level"]), size=min(3, len(player_team)), boss_rarity=boss_rarity)
        else:
            enemy_team = zoo_db.make_enemy_team(int(u["zoo_level"]), size=min(3, len(player_team)))

        st = BattleState(uid, player_team, enemy_team, is_boss=is_boss, boss_rarity=boss_rarity)
        PVE_SESSIONS[uid] = st
        await interaction.response.send_message(embed=st.render_embed(), view=BattleView(st), ephemeral=False)

    # ========= PvP =========
    @app_commands.command(name="pvp", description="Thách đấu PvP (Pokemon-style)")
    async def pvp(self, interaction: discord.Interaction, target: discord.Member):
        if target.bot:
            await interaction.response.send_message("❌ Không thể PvP với bot.", ephemeral=True)
            return
        if target.id == interaction.user.id:
            await interaction.response.send_message("❌ Không thể PvP với chính bạn.", ephemeral=True)
            return

        a = interaction.user.id
        b = target.id

        if a in PVP_SESSIONS or b in PVP_SESSIONS:
            await interaction.response.send_message("⚠️ Một trong hai đang ở trận PvP khác.", ephemeral=True)
            return

        if not zoo_db.get_team(a):
            await interaction.response.send_message("❌ Bạn chưa set đội hình. Dùng `/zoo setteam`.", ephemeral=True)
            return
        if not zoo_db.get_team(b):
            await interaction.response.send_message(f"❌ {target.mention} chưa set đội hình.", ephemeral=True)
            return

        view = PvPChallengeView(self, a, b)
        await interaction.response.send_message(
            f"⚔️ {target.mention} — bạn có muốn accept kèo PvP từ {interaction.user.mention} không?",
            view=view,
            ephemeral=False,
        )

    async def _start_pvp(self, interaction: discord.Interaction, a_id: int, b_id: int):
        a_rows = zoo_db.get_team(a_id)
        b_rows = zoo_db.get_team(b_id)

        def build(rows, uid):
            team = []
            for r in rows[:5]:
                bonus = zoo_db.sum_gear_bonus(uid, int(r["animal_id"]))
                team.append(
                    {
                        "slot": int(r["slot"]),
                        "animal_id": int(r["animal_id"]),
                        "name": r["name"],
                        "emoji": r["emoji"],
                        "rarity": r.get("rarity", "common"),
                        "stars": int(r.get("stars", 1)),
                        "hp": int(r["hp"]) + int(bonus.get("hp", 0)),
                        "atk": int(r["atk"]) + int(bonus.get("atk", 0)),
                        "def": int(r["def"]) + int(bonus.get("def", 0)),
                        "speed": int(r["speed"]) + int(bonus.get("speed", 0)),
                        "evasion": float(r.get("evasion", 0.03)) + float(bonus.get("evasion", 0.0)),
                        "cur_hp": int(r["hp"]) + int(bonus.get("hp", 0)),
                    }
                )
            return team

        st = PvPBattleState(a_id, b_id, build(a_rows, a_id), build(b_rows, b_id))
        PVP_SESSIONS[a_id] = st
        PVP_SESSIONS[b_id] = st

        view = PvPBattleView(st)
        await interaction.response.edit_message(content="✅ PvP bắt đầu! Cả 2 chọn action để chạy round.", embed=st.render_embed(), view=view)

    # ========= ERROR HANDLER =========
    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        orig = getattr(error, "original", error)

        if isinstance(orig, app_commands.CommandOnCooldown):
            msg = f"⏳ Đợi hồi chiêu: **{orig.retry_after:.1f}s**."
        elif isinstance(orig, app_commands.MissingPermissions):
            msg = "❌ Bạn không có quyền dùng lệnh này."
        elif isinstance(orig, app_commands.CheckFailure):
            msg = "❌ Bạn không thể dùng lệnh này lúc này."
        else:
            msg = "❌ Có lỗi xảy ra khi chạy lệnh."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


async def setup(bot):
    await bot.add_cog(Zoo(bot))