# zoo_db.py (updated with Gear + PvE streak + Black Market)
import sqlite3
import time
import random
import uuid

DB_PATH = "zoo.db"

# ================== CONFIG ==================
GACHA_COST = 1000
STAR_MAX = 5

STAR_UPGRADE_COST = {1: 200, 2: 500, 3: 1000, 4: 2000}

# Rarity pool (roll 1..1000)
RARITY_POOL = [
    ("common", 550),
    ("uncommon", 250),
    ("rare", 150),
    ("epic", 45),
    ("legendary", 5),
    ("mythic", 0),  # mythic handled separately
]
MYTHIC_ROLL = 20000  # 1 / 20000

# Né đòn theo rarity
RARITY_EVASION = {
    "common": 0.03,
    "uncommon": 0.05,
    "rare": 0.08,
    "epic": 0.12,
    "legendary": 0.18,
    "mythic": 0.25,
}

# ================== EQUIPMENT (GEAR) SYSTEM ==================
GEAR_RARITIES = ["common", "uncommon", "rare", "epic", "legendary", "mythic"]

GEAR_RARITY_WEIGHTS_PVE = {
    "common": 60,
    "uncommon": 25,
    "rare": 10,
    "epic": 4,
    "legendary": 0.9,
    "mythic": 0.1,
}
GEAR_RARITY_WEIGHTS_BOSS = {
    "rare": 70,
    "epic": 20,
    "legendary": 8,
    "mythic": 2,
}
BOSS_RARITY_WEIGHTS = {"rare": 60, "epic": 25, "legendary": 12, "mythic": 3}

GEAR_DROP_CHANCE_PVE = 0.35
GEAR_DROP_CHANCE_BOSS = 0.70

BOSS_SCALE = {"rare": 1.25, "epic": 1.45, "legendary": 1.70, "mythic": 2.00}
GEAR_SLOTS = 3

GEAR_NAME_POOLS = {
    "common": ["Wooden Charm", "Old Bandage", "Rusty Ring", "Worn Boots"],
    "uncommon": ["Hunter Gloves", "Swift Sandals", "Iron Charm", "Sturdy Belt"],
    "rare": ["Knight Badge", "Silver Amulet", "Shadow Cloak", "Sharpened Fang"],
    "epic": ["Dragon Scale", "Storm Bracer", "Vampire Cape", "Titan Plate"],
    "legendary": ["Sun Crown", "Moon Blade", "Phoenix Feather", "Leviathan Core"],
    "mythic": ["World Heart", "Celestial Halo", "Void Relic", "Ancient Rune"],
}

# ================== BLACK MARKET CONFIG ==================
MARKET_FEE_RATE = 0.03  # 3% fee (take from seller proceeds)
MARKET_MAX_ACTIVE_PER_USER = 20

# ----------------- DB Helpers -----------------
def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with connect() as con:
        cur = con.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            coins INTEGER NOT NULL DEFAULT 0,
            zoo_level INTEGER NOT NULL DEFAULT 1,
            capacity INTEGER NOT NULL DEFAULT 3,
            last_collect INTEGER NOT NULL DEFAULT 0,
            last_daily INTEGER NOT NULL DEFAULT 0
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS species(
            species_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            emoji TEXT NOT NULL,
            rarity TEXT NOT NULL,
            base_hp INTEGER NOT NULL,
            base_atk INTEGER NOT NULL,
            base_def INTEGER NOT NULL,
            base_speed INTEGER NOT NULL,
            base_income_per_min INTEGER NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_animals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            species_id INTEGER NOT NULL,
            level INTEGER NOT NULL DEFAULT 1,
            stars INTEGER NOT NULL DEFAULT 1,
            hp INTEGER NOT NULL,
            atk INTEGER NOT NULL,
            def INTEGER NOT NULL,
            speed INTEGER NOT NULL,
            income_per_min INTEGER NOT NULL,
            evasion REAL NOT NULL DEFAULT 0.03,
            created_at INTEGER NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS formations(
            user_id INTEGER NOT NULL,
            slot INTEGER NOT NULL,
            animal_id INTEGER NOT NULL,
            PRIMARY KEY(user_id, slot)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS items(
            item_key TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            price INTEGER NOT NULL,
            kind TEXT NOT NULL,
            value INTEGER NOT NULL,
            duration_turns INTEGER NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS inventory(
            user_id INTEGER NOT NULL,
            item_key TEXT NOT NULL,
            qty INTEGER NOT NULL,
            PRIMARY KEY(user_id, item_key)
        )
        """)

def migrate():
    """Run safe migrations for existing zoo.db (extended)."""
    with connect() as con:
        # user_animals columns
        cols = [r["name"] for r in con.execute("PRAGMA table_info(user_animals)").fetchall()]
        if "stars" not in cols:
            con.execute("ALTER TABLE user_animals ADD COLUMN stars INTEGER NOT NULL DEFAULT 1")
            con.execute("UPDATE user_animals SET stars = 1 WHERE stars IS NULL")
        if "evasion" not in cols:
            con.execute("ALTER TABLE user_animals ADD COLUMN evasion REAL NOT NULL DEFAULT 0.03")
            con.execute("UPDATE user_animals SET evasion = 0.03 WHERE evasion IS NULL")

        # gear inventory
        con.execute("""
        CREATE TABLE IF NOT EXISTS gear_inventory(
            user_id INTEGER NOT NULL,
            inst_id TEXT NOT NULL,
            name TEXT NOT NULL,
            rarity TEXT NOT NULL,
            atk INTEGER NOT NULL DEFAULT 0,
            hp INTEGER NOT NULL DEFAULT 0,
            def INTEGER NOT NULL DEFAULT 0,
            speed INTEGER NOT NULL DEFAULT 0,
            evasion REAL NOT NULL DEFAULT 0.0,
            money_bonus INTEGER NOT NULL DEFAULT 0,
            price INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            PRIMARY KEY(user_id, inst_id)
        )""")

        # gear equips
        con.execute("""
        CREATE TABLE IF NOT EXISTS gear_equips(
            user_id INTEGER NOT NULL,
            animal_id INTEGER NOT NULL,
            slot INTEGER NOT NULL,
            inst_id TEXT,
            PRIMARY KEY(user_id, animal_id, slot)
        )""")

        # pve streak
        con.execute("""
        CREATE TABLE IF NOT EXISTS pve_progress(
            user_id INTEGER PRIMARY KEY,
            win_streak INTEGER NOT NULL DEFAULT 0
        )""")

        # black market listings (header)
        con.execute("""
        CREATE TABLE IF NOT EXISTS market_listings(
            listing_id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL,
            item_type TEXT NOT NULL,       -- 'animal' or 'gear'
            price INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'  -- active/sold/cancelled
        )""")

        # black market payload: animals
        con.execute("""
        CREATE TABLE IF NOT EXISTS market_animals(
            listing_id INTEGER PRIMARY KEY,
            species_id INTEGER NOT NULL,
            level INTEGER NOT NULL,
            stars INTEGER NOT NULL,
            hp INTEGER NOT NULL,
            atk INTEGER NOT NULL,
            def INTEGER NOT NULL,
            speed INTEGER NOT NULL,
            income_per_min INTEGER NOT NULL,
            evasion REAL NOT NULL,
            created_at INTEGER NOT NULL
        )""")

        # black market payload: gears
        con.execute("""
        CREATE TABLE IF NOT EXISTS market_gears(
            listing_id INTEGER PRIMARY KEY,
            inst_id TEXT NOT NULL,
            name TEXT NOT NULL,
            rarity TEXT NOT NULL,
            atk INTEGER NOT NULL DEFAULT 0,
            hp INTEGER NOT NULL DEFAULT 0,
            def INTEGER NOT NULL DEFAULT 0,
            speed INTEGER NOT NULL DEFAULT 0,
            evasion REAL NOT NULL DEFAULT 0.0,
            money_bonus INTEGER NOT NULL DEFAULT 0,
            price_suggest INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        )""")

# ----------------- Core User -----------------
def ensure_user(user_id):
    with connect() as con:
        con.execute("""
            INSERT INTO users(user_id) VALUES(?)
            ON CONFLICT(user_id) DO NOTHING
        """, (int(user_id),))

def get_user(user_id):
    ensure_user(user_id)
    with connect() as con:
        row = con.execute("SELECT * FROM users WHERE user_id=?", (int(user_id),)).fetchone()
        return dict(row)

def add_coins(user_id, amount):
    ensure_user(user_id)
    with connect() as con:
        con.execute("UPDATE users SET coins = coins + ? WHERE user_id=?", (int(amount), int(user_id)))

def count_user_animals(user_id):
    ensure_user(user_id)
    with connect() as con:
        r = con.execute("SELECT COUNT(*) AS c FROM user_animals WHERE user_id=?", (int(user_id),)).fetchone()
        return int(r["c"])

# ----------------- Species Seed -----------------
def seed_species():
    rows = [
        ("Cat", "🐱", "common", 34, 6, 4, 11, 1),
        ("Duck", "🦆", "common", 30, 5, 3, 9, 1),
        ("Pig", "🐷", "common", 32, 6, 5, 8, 1),
        ("Cow", "🐮", "common", 36, 6, 6, 7, 1),
        ("Chicken", "🐔", "common", 28, 5, 4, 10, 1),
        ("Frog", "🐸", "common", 25, 5, 4, 12, 1),
        ("Snail", "🐌", "common", 22, 4, 6, 3, 1),
        ("Rabbit", "🐇", "common", 30, 6, 4, 14, 1),

        ("Fox", "🦊", "uncommon", 38, 7, 4, 12, 2),
        ("Penguin", "🐧", "uncommon", 42, 6, 6, 8, 2),
        ("Sloth", "🦥", "uncommon", 45, 7, 6, 5, 2),
        ("Bat", "🦇", "uncommon", 35, 8, 5, 14, 2),
        ("Owl", "🦉", "uncommon", 38, 8, 6, 13, 2),
        ("Squid", "🦑", "uncommon", 42, 9, 6, 9, 2),
        ("Octopus", "🐙", "uncommon", 44, 9, 7, 8, 2),
        ("Koala", "🐨", "uncommon", 40, 7, 7, 6, 2),
        ("Monkey", "🐵", "uncommon", 38, 9, 5, 15, 2),
        ("Raccoon", "🦝", "uncommon", 42, 8, 6, 13, 2),

        ("Wolf", "🐺", "rare", 45, 10, 6, 12, 3),
        ("Bear", "🐻", "rare", 60, 13, 9, 9, 3),
        ("Panda", "🐼", "rare", 65, 12, 10, 8, 3),
        ("Dolphin", "🐬", "rare", 55, 14, 8, 16, 3),
        ("Leopard", "🐆", "rare", 50, 15, 7, 18, 3),
        ("Crocodile", "🐊", "rare", 70, 14, 11, 10, 3),
        ("Snake", "🐍", "rare", 50, 15, 6, 18, 3),
        ("Hedgehog", "🦔", "rare", 55, 12, 10, 10, 3),
        ("Eagle", "🦅", "rare", 48, 16, 7, 22, 3),

        ("Lion", "🦁", "epic", 80, 18, 10, 14, 5),
        ("Gorilla", "🦍", "epic", 90, 17, 13, 10, 5),
        ("Kangaroo", "🦘", "epic", 75, 16, 9, 20, 5),
        ("Rhino", "🦏", "epic", 100, 20, 15, 8, 5),
        ("Giraffe", "🦒", "epic", 85, 15, 10, 16, 5),

        ("Shark",    "🦈", "legendary", 115, 26, 12, 18, 10),
        ("Gorilla",  "🦍", "legendary", 140, 23, 18, 10, 10),
        ("Peacock",  "🦚", "legendary",  95, 20, 14, 22, 10),
        ("Elephant", "🐘", "legendary", 170, 22, 22,  7, 10),
        ("Tiger",    "🐯", "legendary", 105, 24, 12, 20, 10),

        ("Orangutan","🦧", "mythic", 180, 30, 22, 12, 20),
        ("Unicorn",  "🦄", "mythic", 150, 28, 18, 22, 20),
        ("Phoenix",  "🐦‍🔥","mythic", 140, 32, 16, 26, 20),
        ("Dragon",   "🐲", "mythic", 220, 35, 24, 14, 20),
        ("T_rex",    "🦖", "mythic", 200, 38, 20, 12, 20),
    ]

    with connect() as con:
        for r in rows:
            exist = con.execute(
                "SELECT 1 FROM species WHERE name=? AND rarity=?",
                (r[0], r[2])
            ).fetchone()
            if not exist:
                con.execute("""
                    INSERT INTO species(name, emoji, rarity, base_hp, base_atk, base_def, base_speed, base_income_per_min)
                    VALUES(?,?,?,?,?,?,?,?)
                """, r)

def _pick_rarity():
    if random.randint(1, MYTHIC_ROLL) == 1:
        return "mythic"
    roll = random.randint(1, 1000)
    s = 0
    chosen = "common"
    for r, w in RARITY_POOL:
        s += w
        if roll <= s:
            chosen = r
            break
    return chosen

def _pick_species_by_rarity(rarity: str):
    with connect() as con:
        rows = con.execute("SELECT * FROM species WHERE rarity=?", (str(rarity),)).fetchall()
        if rows:
            return dict(random.choice(rows))
        rows = con.execute("SELECT * FROM species").fetchall()
        return dict(random.choice(rows))

def _pick_species():
    return _pick_species_by_rarity(_pick_rarity())

def _rand_stat(x):
    return max(1, int(round(x * random.uniform(0.9, 1.1))))

# ----------------- PvE Enemy Generator -----------------
def _rarity_by_level(zoo_level: int) -> str:
    zoo_level = max(1, int(zoo_level))
    roll = random.randint(1, 1000)
    if zoo_level <= 3:
        if roll <= 650: return "common"
        if roll <= 900: return "uncommon"
        return "rare"
    if zoo_level <= 7:
        if roll <= 500: return "common"
        if roll <= 800: return "uncommon"
        if roll <= 950: return "rare"
        return "epic"
    if zoo_level <= 12:
        if roll <= 350: return "common"
        if roll <= 650: return "uncommon"
        if roll <= 880: return "rare"
        if roll <= 980: return "epic"
        return "legendary"
    if roll <= 250: return "common"
    if roll <= 500: return "uncommon"
    if roll <= 750: return "rare"
    if roll <= 930: return "epic"
    if roll <= 995: return "legendary"
    return "mythic"

def make_enemy_team(zoo_level: int, size: int):
    zoo_level = max(1, int(zoo_level))
    size = max(1, min(5, int(size)))
    team = []
    for i in range(size):
        rarity = _rarity_by_level(zoo_level)
        sp = _pick_species_by_rarity(rarity)
        scale = 1.0 + (zoo_level - 1) * 0.05
        hp = max(1, int(round(_rand_stat(sp["base_hp"]) * scale)))
        atk = max(1, int(round(_rand_stat(sp["base_atk"]) * scale)))
        df = max(1, int(round(_rand_stat(sp["base_def"]) * scale)))
        spd = max(1, int(round(_rand_stat(sp["base_speed"]) * (1.0 + (zoo_level - 1) * 0.02))))
        eva = float(RARITY_EVASION.get(rarity, 0.03))
        team.append({
            "slot": i + 1,
            "name": sp["name"],
            "emoji": sp["emoji"],
            "rarity": rarity,
            "stars": 1,
            "hp": hp, "atk": atk, "def": df, "speed": spd,
            "evasion": eva,
            "cur_hp": hp,
        })
    return team

# ----------------- Gacha / Animals -----------------
def do_gacha(user_id):
    ensure_user(user_id)
    u = get_user(user_id)
    owned = count_user_animals(user_id)
    if owned >= int(u["capacity"]):
        return False, "Sở thú đầy. Dùng `/zoo upgrade` để tăng sức chứa."
    cost = GACHA_COST
    if int(u["coins"]) < cost:
        return False, f"Không đủ coins. Cần {cost}."
    sp = _pick_species()
    hp = _rand_stat(sp["base_hp"])
    atk = _rand_stat(sp["base_atk"])
    df = _rand_stat(sp["base_def"])
    spd = _rand_stat(sp["base_speed"])
    inc = _rand_stat(sp["base_income_per_min"])
    eva = float(RARITY_EVASION.get(sp["rarity"], 0.03))
    now = int(time.time())
    with connect() as con:
        con.execute("UPDATE users SET coins = coins - ? WHERE user_id=?", (cost, int(user_id)))
        cur = con.execute("""
            INSERT INTO user_animals(
                user_id, species_id, level, stars,
                hp, atk, def, speed, income_per_min, evasion, created_at
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """, (int(user_id), int(sp["species_id"]), 1, 1, hp, atk, df, spd, inc, eva, now))
        animal_id = cur.lastrowid
    return True, {
        "animal_id": animal_id,
        "name": sp["name"],
        "emoji": sp["emoji"],
        "rarity": sp["rarity"],
        "cost": cost,
        "stars": 1,
        "hp": hp, "atk": atk, "def": df, "speed": spd,
        "income_per_min": inc,
        "evasion": eva,
    }

def list_animals(user_id):
    ensure_user(user_id)
    with connect() as con:
        rows = con.execute("""
            SELECT a.id, a.stars, s.name, s.emoji, s.rarity, a.level,
                   a.hp, a.atk, a.def, a.speed, a.income_per_min, a.evasion
            FROM user_animals a
            JOIN species s ON s.species_id = a.species_id
            WHERE a.user_id=?
            ORDER BY a.id DESC
        """, (int(user_id),)).fetchall()
        return [dict(r) for r in rows]

def get_animal(user_id, animal_id):
    ensure_user(user_id)
    with connect() as con:
        r = con.execute("""
            SELECT a.id, a.stars, a.species_id, s.name, s.emoji, s.rarity, a.level,
                   a.hp, a.atk, a.def, a.speed, a.income_per_min, a.evasion, a.created_at
            FROM user_animals a
            JOIN species s ON s.species_id = a.species_id
            WHERE a.user_id=? AND a.id=?
        """, (int(user_id), int(animal_id))).fetchone()
        return dict(r) if r else None

# ----------------- Stars Upgrade -----------------
def star_upgrade_cost(current_star: int) -> int:
    return int(STAR_UPGRADE_COST.get(int(current_star), 10**9))

def upgrade_star(user_id, animal_id):
    ensure_user(user_id)
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        a = con.execute("""
            SELECT id, user_id, stars, hp, atk, def, speed, income_per_min, evasion
            FROM user_animals
            WHERE id=? AND user_id=?
        """, (int(animal_id), int(user_id))).fetchone()
        if not a:
            con.execute("ROLLBACK")
            return False, "Animal không tồn tại hoặc không thuộc về bạn."
        stars = int(a["stars"])
        if stars >= STAR_MAX:
            con.execute("ROLLBACK")
            return False, "Thú đã đạt tối đa 5⭐."
        cost = star_upgrade_cost(stars)
        u = con.execute("SELECT coins FROM users WHERE user_id=?", (int(user_id),)).fetchone()
        if not u or int(u["coins"]) < cost:
            con.execute("ROLLBACK")
            return False, f"Không đủ coins. Cần {cost}."
        con.execute("UPDATE users SET coins = coins - ? WHERE user_id=?", (cost, int(user_id)))
        new_stars = stars + 1
        new_hp = max(1, int(round(int(a["hp"]) + 3)))
        new_atk = max(1, int(round(int(a["atk"]) + 2)))
        new_def = max(1, int(round(int(a["def"]) + 1)))
        new_spd = max(1, int(round(int(a["speed"]) + 1)))
        new_inc = max(1, int(round(int(a["income_per_min"]) + 1)))
        new_eva = min(0.50, round(float(a["evasion"]) + 0.02, 3))
        con.execute("""
            UPDATE user_animals
            SET stars=?, hp=?, atk=?, def=?, speed=?, income_per_min=?, evasion=?
            WHERE id=? AND user_id=?
        """, (new_stars, new_hp, new_atk, new_def, new_spd, new_inc, new_eva, int(animal_id), int(user_id)))
        con.execute("COMMIT")
    return True, {
        "old": stars, "new": new_stars, "cost": cost,
        "hp": new_hp, "atk": new_atk, "def": new_def, "speed": new_spd,
        "income_per_min": new_inc,
        "evasion": new_eva,
    }

# ----------------- Upgrade / Collect -----------------
def do_upgrade(user_id):
    ensure_user(user_id)
    u = get_user(user_id)
    level = int(u["zoo_level"])
    cost = 600 + (level - 1) * 300
    if int(u["coins"]) < cost:
        return False, f"Không đủ coins. Cần {cost}."
    new_level = level + 1
    new_capacity = int(u["capacity"]) + 2
    with connect() as con:
        con.execute("""
            UPDATE users
            SET coins = coins - ?, zoo_level = ?, capacity = ?
            WHERE user_id=?
        """, (cost, new_level, new_capacity, int(user_id)))
    return True, {"cost": cost, "new_level": new_level, "new_capacity": new_capacity}

def do_collect(user_id):
    """Fix lỗi: lượt collect đầu tiên không được ăn CAP 8h."""
    ensure_user(user_id)
    now = int(time.time())
    with connect() as con:
        u = con.execute("SELECT last_collect, coins FROM users WHERE user_id=?", (int(user_id),)).fetchone()
        last = int(u["last_collect"])

        # first collect: set last_collect and earn 0
        if last <= 0:
            con.execute("UPDATE users SET last_collect=? WHERE user_id=?", (now, int(user_id)))
            coins = con.execute("SELECT coins FROM users WHERE user_id=?", (int(user_id),)).fetchone()["coins"]
            return True, {"minutes": 0, "total_per_min": 0, "earned": 0, "coins": int(coins), "capped": False, "gear_bonus_per_min": 0}

        delta_sec = max(0, now - last)
        minutes = delta_sec // 60

        cap_minutes = 8 * 60
        capped = False
        if minutes > cap_minutes:
            minutes = cap_minutes
            capped = True

        base_per_min = con.execute("""
            SELECT COALESCE(SUM(income_per_min), 0) AS s
            FROM user_animals WHERE user_id=?
        """, (int(user_id),)).fetchone()["s"]
        base_per_min = int(base_per_min)

        gear_bonus_per_min = con.execute("""
            SELECT COALESCE(SUM(g.money_bonus), 0) AS s
            FROM gear_equips e
            JOIN gear_inventory g
              ON g.user_id=e.user_id AND g.inst_id=e.inst_id
            WHERE e.user_id=? AND e.inst_id IS NOT NULL
        """, (int(user_id),)).fetchone()["s"]
        gear_bonus_per_min = int(gear_bonus_per_min)

        total_per_min = base_per_min + gear_bonus_per_min
        earned = total_per_min * int(minutes)

        con.execute("UPDATE users SET coins = coins + ?, last_collect=? WHERE user_id=?",
                    (earned, now, int(user_id)))
        coins = con.execute("SELECT coins FROM users WHERE user_id=?", (int(user_id),)).fetchone()["coins"]

    return True, {"minutes": int(minutes), "total_per_min": int(total_per_min), "earned": int(earned), "coins": int(coins),
                 "capped": capped, "gear_bonus_per_min": int(gear_bonus_per_min)}

# ----------------- Team -----------------
def set_team_slot(user_id, slot, animal_id):
    ensure_user(user_id)
    slot = int(slot)
    if slot < 1 or slot > 5:
        return False, "Slot chỉ từ 1 đến 5."
    a = get_animal(user_id, animal_id)
    if not a:
        return False, "Animal ID không tồn tại hoặc không thuộc về bạn."
    with connect() as con:
        con.execute("DELETE FROM formations WHERE user_id=? AND animal_id=?", (int(user_id), int(animal_id)))
        con.execute("""
            INSERT INTO formations(user_id, slot, animal_id)
            VALUES(?,?,?)
            ON CONFLICT(user_id, slot) DO UPDATE SET animal_id = excluded.animal_id
        """, (int(user_id), slot, int(animal_id)))
    return True, "OK"

def get_team(user_id):
    ensure_user(user_id)
    with connect() as con:
        rows = con.execute("""
            SELECT f.slot, f.animal_id, s.name, s.emoji, s.rarity,
                   a.stars, a.hp, a.atk, a.def, a.speed, a.income_per_min, a.evasion
            FROM formations f
            JOIN user_animals a ON a.id = f.animal_id
            JOIN species s ON s.species_id = a.species_id
            WHERE f.user_id=?
            ORDER BY f.slot ASC
        """, (int(user_id),)).fetchall()
        return [dict(r) for r in rows]

# ----------------- Shop / Inventory -----------------
def seed_items():
    items = [
        ("food_small", "🥕 Cà rốt", "Hồi 25 HP cho thú đang active", 100, "heal", 25, 0),
        ("food_big", "🍖 Thịt nướng", "Hồi 60 HP cho thú đang active", 500, "heal", 60, 0),
        ("atk_snack", "🍗 Snack lực", "Tăng +6 ATK trong 3 lượt", 300, "buff_atk", 6, 3),
    ]
    with connect() as con:
        for it in items:
            con.execute("""
                INSERT INTO items(item_key, name, description, price, kind, value, duration_turns)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(item_key) DO NOTHING
            """, it)

def list_shop_items():
    with connect() as con:
        rows = con.execute("SELECT * FROM items ORDER BY price ASC").fetchall()
        return [dict(r) for r in rows]

def get_item(item_key):
    with connect() as con:
        r = con.execute("SELECT * FROM items WHERE item_key=?", (str(item_key),)).fetchone()
        return dict(r) if r else None

def get_inventory(user_id):
    ensure_user(user_id)
    with connect() as con:
        rows = con.execute("""
            SELECT i.item_key, it.name, it.description, it.kind, it.value, it.duration_turns, i.qty, it.price
            FROM inventory i
            JOIN items it ON it.item_key = i.item_key
            WHERE i.user_id=? AND i.qty > 0
            ORDER BY it.price ASC
        """, (int(user_id),)).fetchall()
        return [dict(r) for r in rows]

def consume_item(user_id, item_key, qty=1):
    ensure_user(user_id)
    qty = int(qty)
    if qty <= 0:
        return False
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        r = con.execute(
            "SELECT qty FROM inventory WHERE user_id=? AND item_key=?",
            (int(user_id), str(item_key))
        ).fetchone()
        if not r or int(r["qty"]) < qty:
            con.execute("ROLLBACK")
            return False
        con.execute(
            "UPDATE inventory SET qty = qty - ? WHERE user_id=? AND item_key=?",
            (qty, int(user_id), str(item_key))
        )
        con.execute(
            "DELETE FROM inventory WHERE user_id=? AND item_key=? AND qty <= 0",
            (int(user_id), str(item_key))
        )
        con.execute("COMMIT")
        return True

def buy_item(user_id, item_key, qty):
    ensure_user(user_id)
    qty = int(qty)
    if qty <= 0:
        return False, "Số lượng phải > 0"
    it = get_item(item_key)
    if not it:
        return False, "Item không tồn tại."
    cost = int(it["price"]) * qty
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        u = con.execute("SELECT coins FROM users WHERE user_id=?", (int(user_id),)).fetchone()
        if not u or int(u["coins"]) < cost:
            con.execute("ROLLBACK")
            return False, f"Không đủ coins. Cần {cost}."
        con.execute("UPDATE users SET coins = coins - ? WHERE user_id=?", (cost, int(user_id)))
        con.execute("""
            INSERT INTO inventory(user_id, item_key, qty)
            VALUES(?,?,?)
            ON CONFLICT(user_id, item_key) DO UPDATE SET qty = qty + excluded.qty
        """, (int(user_id), str(item_key), qty))
        con.execute("COMMIT")
    return True, {"name": it["name"], "qty": qty, "cost": cost, "coins": get_user(user_id)["coins"]}

# ----------------- Sell Animal -----------------
def sell_animal(user_id, animal_id):
    ensure_user(user_id)
    a = get_animal(user_id, animal_id)
    if not a:
        return False, "Animal không tồn tại hoặc không thuộc về bạn."
    base = int(a["hp"]) + int(a["atk"]) * 3 + int(a["def"]) * 2 + int(a["speed"]) * 2 + int(a["income_per_min"]) * 10
    sell_price = int(base * (0.35 + 0.10 * (int(a["stars"]) - 1)))
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute("DELETE FROM formations WHERE user_id=? AND animal_id=?", (int(user_id), int(animal_id)))
        con.execute("DELETE FROM user_animals WHERE id=? AND user_id=?", (int(animal_id), int(user_id)))
        con.execute("UPDATE users SET coins = coins + ? WHERE user_id=?", (sell_price, int(user_id)))
        con.execute("COMMIT")
    return True, {"price": sell_price, "name": a["name"], "emoji": a["emoji"], "stars": int(a["stars"])}

# ----------------- Daily / Pay -----------------
DAILY_COOLDOWN = 24 * 60 * 60

def claim_daily(user_id):
    ensure_user(user_id)
    now = int(time.time())
    with connect() as con:
        u = con.execute("SELECT coins, zoo_level, last_daily FROM users WHERE user_id=?",
                        (int(user_id),)).fetchone()
        last = int(u["last_daily"])
        remain = (last + DAILY_COOLDOWN) - now
        if remain > 0:
            return False, {"remain": int(remain)}
        level = int(u["zoo_level"])
        reward = 1000 + (level - 1) * 120
        con.execute("UPDATE users SET coins = coins + ?, last_daily=? WHERE user_id=?",
                    (reward, now, int(user_id)))
        coins = con.execute("SELECT coins FROM users WHERE user_id=?", (int(user_id),)).fetchone()["coins"]
    return True, {"reward": int(reward), "coins": int(coins), "cooldown": DAILY_COOLDOWN}

def pay_coins(from_user_id, to_user_id, amount):
    amount = int(amount)
    if amount <= 0:
        return False, "Số tiền phải > 0."
    if int(from_user_id) == int(to_user_id):
        return False, "Không thể chuyển cho chính mình."
    ensure_user(from_user_id)
    ensure_user(to_user_id)
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        s = con.execute("SELECT coins FROM users WHERE user_id=?", (int(from_user_id),)).fetchone()
        if not s or int(s["coins"]) < amount:
            con.execute("ROLLBACK")
            return False, "Không đủ coins."
        con.execute("UPDATE users SET coins = coins - ? WHERE user_id=?", (amount, int(from_user_id)))
        con.execute("UPDATE users SET coins = coins + ? WHERE user_id=?", (amount, int(to_user_id)))
        con.execute("COMMIT")
        s_new = con.execute("SELECT coins FROM users WHERE user_id=?", (int(from_user_id),)).fetchone()["coins"]
        r_new = con.execute("SELECT coins FROM users WHERE user_id=?", (int(to_user_id),)).fetchone()["coins"]
    return True, {"sent": amount, "sender_coins": int(s_new), "receiver_coins": int(r_new)}

def admin_add_coins(user_id, amount):
    amount = int(amount)
    if amount == 0:
        return False
    ensure_user(user_id)
    with connect() as con:
        con.execute("UPDATE users SET coins = coins + ? WHERE user_id=?", (amount, int(user_id)))
    return True

# ----------------- GEAR helpers -----------------
def _weighted_choice(weight_map: dict) -> str:
    items = [(k, float(v)) for k, v in weight_map.items() if float(v) > 0]
    total = sum(w for _, w in items)
    r = random.random() * total if total > 0 else 0
    s = 0.0
    for k, w in items:
        s += w
        if r <= s:
            return k
    return items[-1][0] if items else "common"

def _gear_stat_ranges(rarity: str) -> dict:
    rarity = str(rarity)
    if rarity == "common":
        return {"atk": (0, 2), "hp": (0, 10), "def": (0, 2), "speed": (0, 1), "evasion": (0.0, 0.01), "money": (0, 0)}
    if rarity == "uncommon":
        return {"atk": (1, 4), "hp": (5, 18), "def": (1, 4), "speed": (0, 2), "evasion": (0.0, 0.02), "money": (0, 1)}
    if rarity == "rare":
        return {"atk": (3, 7), "hp": (12, 30), "def": (3, 7), "speed": (1, 3), "evasion": (0.01, 0.04), "money": (1, 2)}
    if rarity == "epic":
        return {"atk": (6, 12), "hp": (25, 55), "def": (6, 12), "speed": (2, 5), "evasion": (0.02, 0.07), "money": (2, 4)}
    if rarity == "legendary":
        return {"atk": (10, 18), "hp": (45, 85), "def": (10, 18), "speed": (3, 7), "evasion": (0.04, 0.10), "money": (4, 7)}
    return {"atk": (16, 28), "hp": (70, 140), "def": (16, 28), "speed": (5, 10), "evasion": (0.06, 0.16), "money": (7, 12)}

def _calc_gear_price(rarity: str, atk: int, hp: int, df: int, spd: int, eva: float, money_bonus: int) -> int:
    mult = {"common":1.0,"uncommon":1.5,"rare":2.5,"epic":4.0,"legendary":7.0,"mythic":12.0}.get(rarity, 1.0)
    base = (atk * 120) + (hp * 12) + (df * 100) + (spd * 180) + int(eva * 1000) * 60 + (money_bonus * 250)
    return max(50, int(base * mult))

def _make_random_gear(rarity: str) -> dict:
    rarity = str(rarity)
    if rarity == "legend":
        rarity = "legendary"
    if rarity not in GEAR_RARITIES:
        rarity = "common"
    ranges = _gear_stat_ranges(rarity)
    atk = random.randint(*ranges["atk"])
    hp = random.randint(*ranges["hp"])
    df = random.randint(*ranges["def"])
    spd = random.randint(*ranges["speed"])
    eva = round(random.uniform(*ranges["evasion"]), 3)
    money_bonus = random.randint(*ranges["money"])
    name = random.choice(GEAR_NAME_POOLS.get(rarity, ["Gear"]))
    price = _calc_gear_price(rarity, atk, hp, df, spd, eva, money_bonus)
    return {"inst_id": str(uuid.uuid4())[:8], "name": name, "rarity": rarity,
            "atk": atk, "hp": hp, "def": df, "speed": spd, "evasion": eva, "money_bonus": money_bonus, "price": price}

def ensure_pve_progress(user_id: int) -> None:
    ensure_user(user_id)
    with connect() as con:
        con.execute("INSERT INTO pve_progress(user_id, win_streak) VALUES(?,0) ON CONFLICT(user_id) DO NOTHING", (int(user_id),))

def get_pve_streak(user_id: int) -> int:
    ensure_pve_progress(user_id)
    with connect() as con:
        r = con.execute("SELECT win_streak FROM pve_progress WHERE user_id=?", (int(user_id),)).fetchone()
        return int(r["win_streak"]) if r else 0

def set_pve_streak(user_id: int, streak: int) -> None:
    ensure_pve_progress(user_id)
    with connect() as con:
        con.execute("UPDATE pve_progress SET win_streak=? WHERE user_id=?", (int(streak), int(user_id)))

def inc_pve_streak(user_id: int) -> int:
    ensure_pve_progress(user_id)
    with connect() as con:
        con.execute("UPDATE pve_progress SET win_streak = win_streak + 1 WHERE user_id=?", (int(user_id),))
        r = con.execute("SELECT win_streak FROM pve_progress WHERE user_id=?", (int(user_id),)).fetchone()
        return int(r["win_streak"]) if r else 0

def reset_pve_streak(user_id: int) -> None:
    set_pve_streak(user_id, 0)

def roll_boss_rarity() -> str:
    return _weighted_choice(BOSS_RARITY_WEIGHTS)

def make_boss_enemy_team(zoo_level: int, size: int, boss_rarity: str):
    boss_rarity = str(boss_rarity)
    if boss_rarity == "legend":
        boss_rarity = "legendary"
    if boss_rarity not in ("rare","epic","legendary","mythic"):
        boss_rarity = "rare"
    team = make_enemy_team(zoo_level, size)
    scale = float(BOSS_SCALE.get(boss_rarity, 1.25))
    for e in team:
        e["rarity"] = boss_rarity
        e["hp"] = max(1, int(round(int(e["hp"]) * scale)))
        e["atk"] = max(1, int(round(int(e["atk"]) * scale)))
        e["def"] = max(1, int(round(int(e["def"]) * scale)))
        e["speed"] = max(1, int(round(int(e["speed"]) * (1.0 + (scale-1.0)*0.25))))
        e["evasion"] = min(0.50, float(e.get("evasion", 0.03)) + (0.02 if boss_rarity in ("epic","legendary","mythic") else 0.01))
        e["cur_hp"] = int(e["hp"])
    return team

def add_gear_to_user(user_id: int, gear: dict) -> None:
    ensure_user(user_id)
    now = int(time.time())
    with connect() as con:
        con.execute("""
            INSERT INTO gear_inventory(
                user_id, inst_id, name, rarity,
                atk, hp, def, speed, evasion, money_bonus, price, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            int(user_id), str(gear["inst_id"]), str(gear["name"]), str(gear["rarity"]),
            int(gear.get("atk",0)), int(gear.get("hp",0)), int(gear.get("def",0)), int(gear.get("speed",0)),
            float(gear.get("evasion",0.0)), int(gear.get("money_bonus",0)), int(gear.get("price",0)), now
        ))

def list_gears(user_id: int):
    ensure_user(user_id)
    with connect() as con:
        rows = con.execute("""
            SELECT * FROM gear_inventory
            WHERE user_id=?
            ORDER BY
              CASE rarity
                WHEN 'common' THEN 1
                WHEN 'uncommon' THEN 2
                WHEN 'rare' THEN 3
                WHEN 'epic' THEN 4
                WHEN 'legendary' THEN 5
                WHEN 'mythic' THEN 6
                ELSE 7
              END,
              created_at DESC
        """, (int(user_id),)).fetchall()
        return [dict(r) for r in rows]

def get_gear(user_id: int, inst_id: str):
    ensure_user(user_id)
    with connect() as con:
        r = con.execute("SELECT * FROM gear_inventory WHERE user_id=? AND inst_id=?", (int(user_id), str(inst_id))).fetchone()
        return dict(r) if r else None

def _is_gear_equipped(user_id: int, inst_id: str) -> bool:
    with connect() as con:
        r = con.execute("SELECT 1 FROM gear_equips WHERE user_id=? AND inst_id=? LIMIT 1", (int(user_id), str(inst_id))).fetchone()
        return bool(r)

def equip_gear(user_id: int, animal_id: int, slot: int, inst_id: str):
    ensure_user(user_id)
    slot = int(slot)
    if slot < 1 or slot > GEAR_SLOTS:
        return False, f"Slot gear chỉ từ 1 đến {GEAR_SLOTS}."
    g = get_gear(user_id, inst_id)
    if not g:
        return False, "Gear không tồn tại hoặc không thuộc về bạn."
    a = get_animal(user_id, animal_id)
    if not a:
        return False, "Animal ID không tồn tại hoặc không thuộc về bạn."
    if _is_gear_equipped(user_id, inst_id):
        return False, "Gear này đang được trang bị ở thú khác. Hãy tháo ra trước."
    with connect() as con:
        con.execute("""
            INSERT INTO gear_equips(user_id, animal_id, slot, inst_id)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id, animal_id, slot) DO UPDATE SET inst_id = excluded.inst_id
        """, (int(user_id), int(animal_id), slot, str(inst_id)))
    return True, {"gear": g, "animal": a, "slot": slot}

def unequip_gear(user_id: int, animal_id: int, slot: int):
    ensure_user(user_id)
    slot = int(slot)
    if slot < 1 or slot > GEAR_SLOTS:
        return False, f"Slot gear chỉ từ 1 đến {GEAR_SLOTS}."
    with connect() as con:
        r = con.execute("SELECT inst_id FROM gear_equips WHERE user_id=? AND animal_id=? AND slot=?", (int(user_id), int(animal_id), slot)).fetchone()
        if not r or not r["inst_id"]:
            return False, "Slot này đang trống."
        inst_id = str(r["inst_id"])
        con.execute("UPDATE gear_equips SET inst_id=NULL WHERE user_id=? AND animal_id=? AND slot=?", (int(user_id), int(animal_id), slot))
    g = get_gear(user_id, inst_id)
    return True, {"inst_id": inst_id, "gear": g, "slot": slot}

def list_equipped_gears(user_id: int, animal_id: int):
    ensure_user(user_id)
    with connect() as con:
        for s in range(1, GEAR_SLOTS + 1):
            con.execute("INSERT INTO gear_equips(user_id, animal_id, slot, inst_id) VALUES(?,?,?,NULL) ON CONFLICT(user_id, animal_id, slot) DO NOTHING",
                        (int(user_id), int(animal_id), int(s)))
        rows = con.execute("""
            SELECT e.slot, e.inst_id, g.name, g.rarity, g.atk, g.hp, g.def, g.speed, g.evasion, g.money_bonus
            FROM gear_equips e
            LEFT JOIN gear_inventory g
              ON g.user_id = e.user_id AND g.inst_id = e.inst_id
            WHERE e.user_id=? AND e.animal_id=?
            ORDER BY e.slot ASC
        """, (int(user_id), int(animal_id))).fetchall()
        return [dict(r) for r in rows]

def sum_gear_bonus(user_id: int, animal_id: int) -> dict:
    ensure_user(user_id)
    with connect() as con:
        r = con.execute("""
            SELECT
              COALESCE(SUM(g.atk),0) AS atk,
              COALESCE(SUM(g.hp),0) AS hp,
              COALESCE(SUM(g.def),0) AS def,
              COALESCE(SUM(g.speed),0) AS speed,
              COALESCE(SUM(g.evasion),0.0) AS evasion,
              COALESCE(SUM(g.money_bonus),0) AS money_bonus
            FROM gear_equips e
            JOIN gear_inventory g
              ON g.user_id=e.user_id AND g.inst_id=e.inst_id
            WHERE e.user_id=? AND e.animal_id=? AND e.inst_id IS NOT NULL
        """, (int(user_id), int(animal_id))).fetchone()
        return dict(r) if r else {"atk":0,"hp":0,"def":0,"speed":0,"evasion":0.0,"money_bonus":0}

def sell_gears(user_id: int, inst_ids: list[str]):
    ensure_user(user_id)
    inst_ids = [str(x).strip() for x in inst_ids if str(x).strip()]
    if not inst_ids:
        return False, "Bạn chưa nhập gear ID."
    if len(inst_ids) > 50:
        return False, "Tối đa bán 50 gear mỗi lần."
    total = 0
    sold = []
    failed = []
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        for inst_id in inst_ids:
            if _is_gear_equipped(user_id, inst_id):
                failed.append((inst_id, "đang trang bị"))
                continue
            r = con.execute("SELECT price, name, rarity FROM gear_inventory WHERE user_id=? AND inst_id=?", (int(user_id), inst_id)).fetchone()
            if not r:
                failed.append((inst_id, "không có"))
                continue
            price = int(r["price"])
            total += price
            sold.append({"inst_id": inst_id, "name": r["name"], "rarity": r["rarity"], "price": price})
            con.execute("DELETE FROM gear_inventory WHERE user_id=? AND inst_id=?", (int(user_id), inst_id))
        if total > 0:
            con.execute("UPDATE users SET coins = coins + ? WHERE user_id=?", (total, int(user_id)))
        con.execute("COMMIT")
    return True, {"total": total, "sold": sold, "failed": failed, "coins": get_user(user_id)["coins"]}

def roll_gear_drop(is_boss: bool):
    if is_boss:
        if random.random() > float(GEAR_DROP_CHANCE_BOSS):
            return None
        rarity = _weighted_choice(GEAR_RARITY_WEIGHTS_BOSS)
        return _make_random_gear(rarity)
    if random.random() > float(GEAR_DROP_CHANCE_PVE):
        return None
    rarity = _weighted_choice(GEAR_RARITY_WEIGHTS_PVE)
    return _make_random_gear(rarity)

def team_money_bonus(user_id: int, animal_ids: list[int]) -> int:
    animal_ids = [int(x) for x in animal_ids]
    if not animal_ids:
        return 0
    q = ",".join(["?"] * len(animal_ids))
    with connect() as con:
        r = con.execute(f"""
            SELECT COALESCE(SUM(g.money_bonus),0) AS s
            FROM gear_equips e
            JOIN gear_inventory g
              ON g.user_id=e.user_id AND g.inst_id=e.inst_id
            WHERE e.user_id=? AND e.animal_id IN ({q}) AND e.inst_id IS NOT NULL
        """, (int(user_id), *animal_ids)).fetchone()
        return int(r["s"]) if r else 0

def pve_handle_victory(user_id: int, is_boss: bool, team_animal_ids: list[int]):
    base_reward = 50
    bonus = team_money_bonus(user_id, team_animal_ids)
    reward = int(base_reward + bonus)
    admin_add_coins(user_id, reward)
    dropped = roll_gear_drop(is_boss=is_boss)
    if dropped:
        add_gear_to_user(user_id, dropped)
    if is_boss:
        reset_pve_streak(user_id)
        streak = 0
    else:
        streak = inc_pve_streak(user_id)
        if streak > 5:
            set_pve_streak(user_id, 5)
            streak = 5
    return {"reward": reward, "bonus": bonus, "streak": streak, "dropped": dropped, "next_is_boss": (streak >= 5)}

# ================== BLACK MARKET (animals + gears) ==================
def _market_active_count(user_id: int) -> int:
    with connect() as con:
        r = con.execute("SELECT COUNT(*) AS c FROM market_listings WHERE seller_id=? AND status='active'", (int(user_id),)).fetchone()
        return int(r["c"]) if r else 0

def market_sell_animal(user_id: int, animal_id: int, price: int):
    ensure_user(user_id)
    animal_id = int(animal_id)
    price = int(price)
    if price <= 0:
        return False, "Giá phải > 0."
    if price > 2_000_000_000:
        return False, "Giá quá lớn."
    if _market_active_count(user_id) >= MARKET_MAX_ACTIVE_PER_USER:
        return False, f"Tối đa {MARKET_MAX_ACTIVE_PER_USER} listing đang bán."
    a = get_animal(user_id, animal_id)
    if not a:
        return False, "Animal không tồn tại hoặc không thuộc về bạn."

    # Không cho bán nếu đang ở đội hình
    with connect() as con:
        in_team = con.execute("SELECT 1 FROM formations WHERE user_id=? AND animal_id=? LIMIT 1", (int(user_id), animal_id)).fetchone()
        if in_team:
            return False, "Thú này đang ở đội hình. Hãy `/zoo setteam` đổi slot khác trước."
    # Không cho bán nếu đang có gear trang bị (đỡ mất gear)
    with connect() as con:
        has_gear = con.execute("SELECT 1 FROM gear_equips WHERE user_id=? AND animal_id=? AND inst_id IS NOT NULL LIMIT 1", (int(user_id), animal_id)).fetchone()
        if has_gear:
            return False, "Thú đang cầm gear. Hãy tháo gear (`/zoo gearunequip`) trước khi bán."

    now = int(time.time())
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        # create listing
        cur = con.execute("""
            INSERT INTO market_listings(seller_id, item_type, price, created_at, status)
            VALUES(?,?,?,?, 'active')
        """, (int(user_id), "animal", price, now))
        listing_id = int(cur.lastrowid)
        # payload
        con.execute("""
            INSERT INTO market_animals(listing_id, species_id, level, stars, hp, atk, def, speed, income_per_min, evasion, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """, (
            listing_id, int(a["species_id"]), int(a["level"]), int(a["stars"]),
            int(a["hp"]), int(a["atk"]), int(a["def"]), int(a["speed"]),
            int(a["income_per_min"]), float(a["evasion"]), int(a["created_at"])
        ))
        # remove animal from user
        con.execute("DELETE FROM user_animals WHERE id=? AND user_id=?", (animal_id, int(user_id)))
        con.execute("COMMIT")

    return True, {"listing_id": listing_id, "price": price, "name": a["name"], "emoji": a["emoji"], "stars": int(a["stars"])}

def market_sell_gear(user_id: int, inst_id: str, price: int):
    ensure_user(user_id)
    inst_id = str(inst_id).strip()
    price = int(price)
    if not inst_id:
        return False, "Thiếu gear ID."
    if price <= 0:
        return False, "Giá phải > 0."
    if price > 2_000_000_000:
        return False, "Giá quá lớn."
    if _market_active_count(user_id) >= MARKET_MAX_ACTIVE_PER_USER:
        return False, f"Tối đa {MARKET_MAX_ACTIVE_PER_USER} listing đang bán."

    if _is_gear_equipped(user_id, inst_id):
        return False, "Gear đang được trang bị. Hãy tháo ra trước."
    g = get_gear(user_id, inst_id)
    if not g:
        return False, "Gear không tồn tại hoặc không thuộc về bạn."

    now = int(time.time())
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        cur = con.execute("""
            INSERT INTO market_listings(seller_id, item_type, price, created_at, status)
            VALUES(?,?,?,?, 'active')
        """, (int(user_id), "gear", price, now))
        listing_id = int(cur.lastrowid)

        con.execute("""
            INSERT INTO market_gears(listing_id, inst_id, name, rarity, atk, hp, def, speed, evasion, money_bonus, price_suggest, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            listing_id, str(g["inst_id"]), str(g["name"]), str(g["rarity"]),
            int(g["atk"]), int(g["hp"]), int(g["def"]), int(g["speed"]),
            float(g["evasion"]), int(g["money_bonus"]), int(g["price"]), int(g["created_at"])
        ))

        con.execute("DELETE FROM gear_inventory WHERE user_id=? AND inst_id=?", (int(user_id), inst_id))
        con.execute("COMMIT")

    return True, {"listing_id": listing_id, "price": price, "name": g["name"], "rarity": g["rarity"], "inst_id": g["inst_id"]}

def market_list(page: int = 1, page_size: int = 10):
    page = max(1, int(page))
    page_size = max(5, min(20, int(page_size)))
    offset = (page - 1) * page_size
    with connect() as con:
        total = con.execute("SELECT COUNT(*) AS c FROM market_listings WHERE status='active'").fetchone()["c"]
        rows = con.execute("""
            SELECT listing_id, seller_id, item_type, price, created_at
            FROM market_listings
            WHERE status='active'
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """, (page_size, offset)).fetchall()

        out = []
        for r in rows:
            d = dict(r)
            if d["item_type"] == "animal":
                a = con.execute("""
                    SELECT ma.*, s.name, s.emoji, s.rarity
                    FROM market_animals ma
                    JOIN species s ON s.species_id = ma.species_id
                    WHERE ma.listing_id=?
                """, (int(d["listing_id"]),)).fetchone()
                d["payload"] = dict(a) if a else None
            else:
                g = con.execute("SELECT * FROM market_gears WHERE listing_id=?", (int(d["listing_id"]),)).fetchone()
                d["payload"] = dict(g) if g else None
            out.append(d)

    return {"total": int(total), "page": page, "page_size": page_size, "rows": out}

def market_list_mine(user_id: int, page: int = 1, page_size: int = 10):
    ensure_user(user_id)
    page = max(1, int(page))
    page_size = max(5, min(20, int(page_size)))
    offset = (page - 1) * page_size
    with connect() as con:
        total = con.execute("SELECT COUNT(*) AS c FROM market_listings WHERE status='active' AND seller_id=?", (int(user_id),)).fetchone()["c"]
        rows = con.execute("""
            SELECT listing_id, seller_id, item_type, price, created_at
            FROM market_listings
            WHERE status='active' AND seller_id=?
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """, (int(user_id), page_size, offset)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d["item_type"] == "animal":
                a = con.execute("""
                    SELECT ma.*, s.name, s.emoji, s.rarity
                    FROM market_animals ma
                    JOIN species s ON s.species_id = ma.species_id
                    WHERE ma.listing_id=?
                """, (int(d["listing_id"]),)).fetchone()
                d["payload"] = dict(a) if a else None
            else:
                g = con.execute("SELECT * FROM market_gears WHERE listing_id=?", (int(d["listing_id"]),)).fetchone()
                d["payload"] = dict(g) if g else None
            out.append(d)
    return {"total": int(total), "page": page, "page_size": page_size, "rows": out}

def market_cancel(user_id: int, listing_id: int):
    ensure_user(user_id)
    listing_id = int(listing_id)
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        l = con.execute("""
            SELECT * FROM market_listings WHERE listing_id=? AND status='active'
        """, (listing_id,)).fetchone()
        if not l:
            con.execute("ROLLBACK")
            return False, "Listing không tồn tại hoặc đã không còn active."
        if int(l["seller_id"]) != int(user_id):
            con.execute("ROLLBACK")
            return False, "Bạn không phải chủ listing này."

        item_type = str(l["item_type"])

        if item_type == "animal":
            a = con.execute("""
                SELECT * FROM market_animals WHERE listing_id=?
            """, (listing_id,)).fetchone()
            if not a:
                con.execute("ROLLBACK")
                return False, "Không tìm thấy dữ liệu animal."
            # return animal to seller (capacity check)
            owned = con.execute("SELECT COUNT(*) AS c FROM user_animals WHERE user_id=?", (int(user_id),)).fetchone()["c"]
            cap = con.execute("SELECT capacity FROM users WHERE user_id=?", (int(user_id),)).fetchone()["capacity"]
            if int(owned) >= int(cap):
                con.execute("ROLLBACK")
                return False, "Sở thú đầy, không thể nhận lại thú. Hãy upgrade hoặc bán bớt thú."
            cur = con.execute("""
                INSERT INTO user_animals(user_id, species_id, level, stars, hp, atk, def, speed, income_per_min, evasion, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """, (
                int(user_id), int(a["species_id"]), int(a["level"]), int(a["stars"]),
                int(a["hp"]), int(a["atk"]), int(a["def"]), int(a["speed"]),
                int(a["income_per_min"]), float(a["evasion"]), int(a["created_at"])
            ))
            new_animal_id = int(cur.lastrowid)
            # ensure no gear equips rows left (none should exist)
            con.execute("UPDATE market_listings SET status='cancelled' WHERE listing_id=?", (listing_id,))
            con.execute("DELETE FROM market_animals WHERE listing_id=?", (listing_id,))
            con.execute("COMMIT")
            return True, {"item_type": "animal", "animal_id": new_animal_id}
        else:
            g = con.execute("SELECT * FROM market_gears WHERE listing_id=?", (listing_id,)).fetchone()
            if not g:
                con.execute("ROLLBACK")
                return False, "Không tìm thấy dữ liệu gear."
            con.execute("""
                INSERT INTO gear_inventory(user_id, inst_id, name, rarity, atk, hp, def, speed, evasion, money_bonus, price, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                int(user_id), str(g["inst_id"]), str(g["name"]), str(g["rarity"]),
                int(g["atk"]), int(g["hp"]), int(g["def"]), int(g["speed"]),
                float(g["evasion"]), int(g["money_bonus"]), int(g["price_suggest"]), int(g["created_at"])
            ))
            con.execute("UPDATE market_listings SET status='cancelled' WHERE listing_id=?", (listing_id,))
            con.execute("DELETE FROM market_gears WHERE listing_id=?", (listing_id,))
            con.execute("COMMIT")
            return True, {"item_type": "gear", "gear_id": str(g["inst_id"])}

def market_buy(buyer_id: int, listing_id: int):
    ensure_user(buyer_id)
    listing_id = int(listing_id)
    now = int(time.time())
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        l = con.execute("SELECT * FROM market_listings WHERE listing_id=? AND status='active'", (listing_id,)).fetchone()
        if not l:
            con.execute("ROLLBACK")
            return False, "Listing không tồn tại hoặc đã hết hàng."
        seller_id = int(l["seller_id"])
        if seller_id == int(buyer_id):
            con.execute("ROLLBACK")
            return False, "Bạn không thể mua listing của chính mình."
        price = int(l["price"])
        item_type = str(l["item_type"])

        buyer = con.execute("SELECT coins, capacity FROM users WHERE user_id=?", (int(buyer_id),)).fetchone()
        if not buyer or int(buyer["coins"]) < price:
            con.execute("ROLLBACK")
            return False, "Không đủ coins."

        # capacity check if buying animal
        if item_type == "animal":
            owned = con.execute("SELECT COUNT(*) AS c FROM user_animals WHERE user_id=?", (int(buyer_id),)).fetchone()["c"]
            if int(owned) >= int(buyer["capacity"]):
                con.execute("ROLLBACK")
                return False, "Sở thú của bạn đầy. Hãy upgrade trước khi mua thú."

        # take coins from buyer
        con.execute("UPDATE users SET coins = coins - ? WHERE user_id=?", (price, int(buyer_id)))

        # seller proceeds after fee
        fee = int(round(price * MARKET_FEE_RATE))
        proceeds = max(0, price - fee)
        con.execute("UPDATE users SET coins = coins + ? WHERE user_id=?", (proceeds, seller_id))

        result = {"item_type": item_type, "price": price, "fee": fee, "proceeds": proceeds, "seller_id": seller_id}

        if item_type == "animal":
            a = con.execute("""
                SELECT * FROM market_animals WHERE listing_id=?
            """, (listing_id,)).fetchone()
            if not a:
                con.execute("ROLLBACK")
                return False, "Dữ liệu animal bị thiếu."
            cur = con.execute("""
                INSERT INTO user_animals(user_id, species_id, level, stars, hp, atk, def, speed, income_per_min, evasion, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """, (
                int(buyer_id), int(a["species_id"]), int(a["level"]), int(a["stars"]),
                int(a["hp"]), int(a["atk"]), int(a["def"]), int(a["speed"]),
                int(a["income_per_min"]), float(a["evasion"]), now
            ))
            new_animal_id = int(cur.lastrowid)
            result["animal_id"] = new_animal_id
            # cleanup payload
            con.execute("DELETE FROM market_animals WHERE listing_id=?", (listing_id,))
        else:
            g = con.execute("SELECT * FROM market_gears WHERE listing_id=?", (listing_id,)).fetchone()
            if not g:
                con.execute("ROLLBACK")
                return False, "Dữ liệu gear bị thiếu."
            con.execute("""
                INSERT INTO gear_inventory(user_id, inst_id, name, rarity, atk, hp, def, speed, evasion, money_bonus, price, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                int(buyer_id), str(g["inst_id"]), str(g["name"]), str(g["rarity"]),
                int(g["atk"]), int(g["hp"]), int(g["def"]), int(g["speed"]),
                float(g["evasion"]), int(g["money_bonus"]), int(g["price_suggest"]), now
            ))
            result["gear_id"] = str(g["inst_id"])
            con.execute("DELETE FROM market_gears WHERE listing_id=?", (listing_id,))

        # close listing
        con.execute("UPDATE market_listings SET status='sold' WHERE listing_id=?", (listing_id,))
        con.execute("COMMIT")

    result["buyer_coins"] = get_user(buyer_id)["coins"]
    return True, result
