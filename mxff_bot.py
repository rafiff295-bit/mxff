#!/usr/bin/env python3
"""
MANAGE x FF — Telegram Bot v7.0
Changes v7.0:
  - ONYX format REMOVED — only .mxff (3105PATCH magic byte format) supported
  - Upload: only .mxff files accepted for FF and FF MAX inject
  - .mxff files served as-is (3105PATCH format, no re-wrapping)
  - New built-in instances: AIMBOT, AIMSILENT, INJECT MENU
  - Patches make tab: rules-only system, ONYX builder removed
  - All UI strings updated to .mxff branding
  - Inject logic: mxff contains bundleID + inject path; IPA reads directly
  - Backup/restore logic preserved: on inject, backup original; on toggle-off, restore
"""

import asyncio
import io
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from aiohttp import web, ClientSession, ClientTimeout
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)
from telegram.constants import ParseMode
# ══════════════════════════════════════════════════════════════════════════════
# .mxff / 3105PATCH Format — native IPA patch format
# .onyx format is REMOVED. Only .mxff (magic: 3105PATCH) files are supported.
# The .mxff file is a plist-wrapped encrypted container carrying:
#   - schemaVersion, packageID, encryptedPayload (+ optional key wrapping)
# Inject paths and bundle IDs are embedded inside the .mxff by the patch maker.
# Bot only stores and serves the .mxff file as-is — no re-wrapping needed.
# ══════════════════════════════════════════════════════════════════════════════
import hashlib
import struct as _struct, uuid as _uuid_mod

MXCF_MAGIC = b"3105PATCH"

# ── Bundle IDs ──────────────────────────────────────────────────────────────────
BUNDLE_FF    = "com.dts.freefireth"
BUNDLE_FFMAX = "com.dts.freefiremax"

# ── Inject path constants (for reference / rules system) ────────────────────────
MXCF_PATH_CACHE_RES   = "Documents/contentcache/Compulsory/ios/gameassetbundles/"
MXCF_PATH_SHADERS     = "Documents/contentcache/Optional/ios/gameassetbundles/"
MXCF_PATH_ASSETINDEXER= "Documents/contentcache/Compulsory/ios/gameassetbundles/avatar/"
MXCF_PATH_PLIST       = "Library/Preferences/"
MXCF_PATH_MODAVATAR   = "Documents/contentcache/Optional/ios/optionalavatarres/gameassetbundles/"

def is_mxff(data: bytes) -> bool:
    """Return True if the data starts with the 3105PATCH magic."""
    return data[:9] == MXCF_MAGIC


def _to_mxff(fname: str) -> str:
    """Normalize a patch filename to .mxff extension (IPA expects .mxff locally)."""
    if fname and fname.endswith(".mxff"):
        return fname[:-5] + ".mxff"
    return fname


def _resolve_inject_path(payload_filename: str) -> str:
    """
    Determine the correct inject path from the payload filename.
    Rules:
      cache_res.*              → Documents/contentcache/Compulsory/ios/gameassetbundles/
      shaders.*                → Documents/contentcache/Optional/ios/gameassetbundles/
      optionalavatarres_commonab_shader.*  → Optional/ios/gameassetbundles/
      assetindexer.*           → Compulsory/ios/gameassetbundles/avatar/
      *.plist                  → Library/Preferences/
      optionalab_avatar_*      → Optional/ios/optionalavatarres/gameassetbundles/
      (anything else)          → Compulsory/ios/gameassetbundles/
    """
    name = payload_filename.lower()
    if name.endswith(".plist"):
        return MXCF_PATH_PLIST
    if name.startswith("optionalavatarres_commonab_shader"):
        return MXCF_PATH_SHADERS
    if name.startswith("shaders"):
        return MXCF_PATH_SHADERS
    if name.startswith("optionalab_avatar"):
        return MXCF_PATH_MODAVATAR
    if name.startswith("assetindexer"):
        return MXCF_PATH_ASSETINDEXER
    return MXCF_PATH_CACHE_RES






# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN   = "9160003953:AAHurUoAUdyNIg0un6v0ldHvu6G3pjabN8U"
OWNER_ID    = 8245269289
API_PORT    = int(os.getenv("API_PORT", "10020"))
PATCH_PORT  = int(os.getenv("PATCH_PORT", "10022"))

DATA_DIR       = Path(os.getenv("DATA_DIR", "data"))
KEYS_FILE      = DATA_DIR / "keys.json"
USERS_FILE     = DATA_DIR / "users.json"
CFG_FILE       = DATA_DIR / "config.json"
SELLERS_FILE   = DATA_DIR / "sellers.json"
INSTANCES_FILE = DATA_DIR / "instances.json"
ADMINS_FILE    = DATA_DIR / "admins.json"
PATCH_DIR      = DATA_DIR / "patches"

# ── GitHub Actions Config ──────────────────────────────────────────────────────
GH_TOKEN       = os.getenv("GH_TOKEN", "")
GH_REPO_OWNER  = os.getenv("GH_REPO_OWNER", "")
GH_REPO_NAME   = os.getenv("GH_REPO_NAME", "")
GH_WORKFLOW_ID = os.getenv("GH_WORKFLOW_ID", "build.yml")
GH_BRANCH      = os.getenv("GH_BRANCH", "main")
_GH_API        = "https://api.github.com"
_GH_ARTIFACT   = "FOUGO-EXTERNAL-ipa"

# ── Backend URL (Render) ───────────────────────────────────────────────────────
BACKEND_URL = os.getenv("BACKEND_URL", "https://mxff.onrender.com")

# ── GitHub Gist Persistence ────────────────────────────────────────────────────
# সব ডেটা (keys, users, instances, sellers, admins, config) এখানে save হবে
# GitHub Actions restart হলেও ডেটা মুছবে না
GIST_ID    = os.getenv("GIST_ID", "02df366c20fa09d72a1af1bf16922aa0")
GIST_FILE  = "fougo_data.json"
GIST_API   = f"https://api.github.com/gists/{GIST_ID}"


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("FOUGO")

# ── Game constants ──────────────────────────────────────────────────────────────
GAMES = [
    {"id": "ff",    "name": "Free Fire",     "bundle": "com.dts.freefireth"},
    {"id": "ffmax", "name": "Free Fire MAX",  "bundle": "com.dts.freefiremax"},
]

def game_by_id(gid):
    return next((g for g in GAMES if g["id"] == gid), GAMES[0])

# ── Premium Emoji ───────────────────────────────────────────────────────────────
_EMOJI_IDS = {
    "crown":       "6129780430112565418",
    "shield":      "6129491060985963253",
    "diamond":     "6129802669453221596",
    "star":        "5852684155380304970",
    "sparkles":    "5397916757333654639",
    "trophy":      "6194737030165959506",
    "broadcast":   "6129767609635183970",
    "check":       "6129419524010679934",
    "cross":       "5210952531676504517",
    "key":         "6176966310920983412",
    "rocket":      "5188481279963715781",
    "gear":        "5341715473882955310",
    "bin":         "5445267414562389170",
    "lock":        "5296369303661067030",
    "unlock":      "6129502391109688947",
    "person":      "6055441847663200437",
    "people":      "5453957997418004470",
    "calendar":    "5816442787544964208",
    "warning":     "5420323339723881652",
    "info":        "5222444124698853913",
    "fire":        "6129610005810258593",
    "bell":        "6129569027527288477",
    "home":        "5416041192905265756",
    "robot":       "5314391089514291948",
    "greendot":    "6228535983244055230",
    "upload":      "5445355530111437729",
    "package":     "5463172695132745432",
    "moneybag":    "6129741380269908529",
    "redcircle":   "5411225014148014586",
    "greencircle": "5296544559801582205",
    "refresh":     "6325832420020979066",
    "search":      "5319230516929502602",
    "back":        "5352759161945867747",
    "stop":        "5240241223632954241",
    "phone":       "5316594137154200720",
    "seller":      "5188481279963715781",
    "wrench":      "5341715473882955310",
    "aim":         "6194737030165959506",
    "timer":       "5816442787544964208",
    "plus":        "6129419524010679934",
    "edit":        "5341715473882955310",
    "gamepad":     "5188481279963715781",
}
_EMOJI_FB = {
    "crown": "👑", "shield": "🛡", "diamond": "💎", "star": "⭐",
    "sparkles": "✨", "trophy": "🏆", "broadcast": "📢", "check": "✅",
    "cross": "❌", "key": "🔑", "rocket": "🚀", "gear": "⚙️", "bin": "🗑",
    "lock": "🔒", "unlock": "🔓", "person": "👤", "people": "👥",
    "calendar": "📅", "warning": "⚠️", "info": "ℹ️", "fire": "🔥",
    "bell": "🔔", "home": "🏠", "robot": "🤖", "greendot": "🟢",
    "upload": "📤", "package": "📦", "moneybag": "💰", "redcircle": "🔴",
    "greencircle": "🟢", "refresh": "🔄", "search": "🔍", "back": "◀️",
    "stop": "⏹", "phone": "📱", "seller": "👨‍💼", "wrench": "🔧",
    "aim": "🎯", "timer": "⏱", "plus": "➕", "edit": "✏️", "gamepad": "🎮",
}

def e(name: str) -> str:
    eid = _EMOJI_IDS.get(name)
    if eid:
        return f'<tg-emoji emoji-id="{eid}">⚡</tg-emoji>'
    return _EMOJI_FB.get(name, "•")

# ── Data Helpers ────────────────────────────────────────────────────────────────
def _load(path: Path, default):
    try:
        return json.loads(path.read_text()) if path.exists() else default
    except Exception:
        return default

def _save(path: Path, data):
    path.write_text(json.dumps(data, indent=2, default=str))

def load_keys():       return _load(KEYS_FILE,       {})
def save_keys(d):      _save(KEYS_FILE,       d)
def load_users():      return _load(USERS_FILE,      {})
def save_users(d):     _save(USERS_FILE,      d)
def load_cfg():        return _load(CFG_FILE,        {"ipa_enabled": True})
def save_cfg(d):       _save(CFG_FILE,        d)
def load_sellers():    return _load(SELLERS_FILE,    {})
def save_sellers(d):   _save(SELLERS_FILE,    d)
def load_instances():  return _load(INSTANCES_FILE,  [])
def save_instances(d): _save(INSTANCES_FILE,  d)
def load_admins():     return _load(ADMINS_FILE,     {})
def save_admins(d):    _save(ADMINS_FILE,     d)

# ── Built-in Instances ──────────────────────────────────────────────────────────
BUILTIN_INSTANCES = [
    {
        "id":    "aimbot",
        "name":  "AIMBOT",
        "label": "AIMBOT",
        "icon":  "scope",
        "game":  "both",
        "patch_file": None,
        "builtin": True,
    },
    {
        "id":    "aimsilent",
        "name":  "AIMSILENT",
        "label": "AIMSILENT",
        "icon":  "scope.fill",
        "game":  "both",
        "patch_file": None,
        "builtin": True,
    },
    {
        "id":    "injectmenu",
        "name":  "INJECT MENU",
        "label": "INJECT MENU",
        "icon":  "list.bullet.indent",
        "game":  "both",
        "patch_file": None,
        "builtin": True,
    },
]

def seed_builtin_instances():
    """Ensure the 3 built-in instances exist in instances.json on startup."""
    instances = load_instances()
    existing_ids = {i["id"] for i in instances}
    changed = False
    from datetime import datetime, timezone
    for bi in BUILTIN_INSTANCES:
        if bi["id"] not in existing_ids:
            entry = dict(bi)
            entry["created_at"] = datetime.now(timezone.utc).isoformat()
            instances.append(entry)
            changed = True
            log.info(f"Seeded built-in instance: {bi['id']}")
    if changed:
        save_instances(instances)



# ── Instance Helpers ────────────────────────────────────────────────────────────
DEFAULT_ICONS = {
    "aimdrag":          "scope",
    "aimbody":          "figure.stand",
    "aimneck":          "person.bust",
    "aimchest":         "heart.fill",
    "hologram":         "cube.transparent",
    "3dholo":           "view.3d",
    "3d_holo":          "view.3d",
    "3dholomax":        "view.3d",
    "3dchams":          "cube.transparent",
    "3dchamsmax":       "cube.transparent",
    "hologram3d":       "cube.transparent",
    "magicbullet":      "sparkles",
    "magicbulletmax":   "sparkles",
    "aimbodymax":       "figure.stand",
    "aimneckmax":       "person.bust",
    "aimdragmax":       "scope",
    "aimchestmax":      "heart.fill",
    "aim_plus_holo":    "plus.viewfinder",
    "aim_plus_holomax": "plus.viewfinder",
    "modskin":          "tshirt.fill",
    "fpsboost":         "gauge.high",
    # ── New built-in instances ───────────────────────────────────────────────
    "aimbot":           "scope",
    "aimsilent":        "scope.fill",
    "injectmenu":       "list.bullet.indent",
}

def find_instance(iid):
    return next((i for i in load_instances() if i["id"] == iid), None)

def inst_has_patch(inst):
    """True if instance has at least one game-specific (or legacy) patch file."""
    return bool(
        inst.get("patch_file_ff")
        or inst.get("patch_file_ffmax")
        or inst.get("patch_file")   # backward-compat single file
    )

def inst_patch_games(inst) -> list:
    """Return list of game IDs that have a patch file: ['ff'], ['ffmax'], ['ff','ffmax'], or []."""
    games = []
    if inst.get("patch_file_ff") or (inst.get("patch_file") and not inst.get("patch_file_ffmax")):
        games.append("ff")
    if inst.get("patch_file_ffmax") or (inst.get("patch_file") and not inst.get("patch_file_ff")):
        games.append("ffmax")
    # Legacy: single patch_file serves both
    if inst.get("patch_file") and not inst.get("patch_file_ff") and not inst.get("patch_file_ffmax"):
        games = ["ff", "ffmax"]
    return games

def inst_game_label(inst):
    gid = inst.get("game", "ff")
    if gid == "both":
        return "FF + FF MAX"
    return "FF MAX" if gid == "ffmax" else "FF"

# ── Key Helpers ─────────────────────────────────────────────────────────────────
def gen_key() -> str:
    r = uuid.uuid4().hex.upper()
    return f"FOUGO-{r[:4]}-{r[4:8]}-{r[8:12]}-{r[12:16]}"

def key_status(k: dict) -> str:
    if k.get("blocked"):
        return "blocked"
    # Stock key: expires_at is None until first device activates it → always active until then
    if k.get("stock") and not k.get("activated_at"):
        return "active"
    exp = k.get("expires_at")
    if exp:
        try:
            dt = datetime.fromisoformat(exp)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt < datetime.now(timezone.utc):
                return "expired"
        except Exception:
            pass
    return "active"

def expiry_from_spec(hours=0, days=0):
    total = hours + days * 24
    return None if total <= 0 else (
        datetime.now(timezone.utc) + timedelta(hours=total)
    ).isoformat()

def format_expiry(exp) -> str:
    if not exp:
        return "Lifetime"
    try:
        dt = datetime.fromisoformat(exp)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = dt - datetime.now(timezone.utc)
        h = int(delta.total_seconds() / 3600)
        if h < 0:
            return "Expired"
        return f"{h}h" if h < 24 else f"{h // 24}d {h % 24}h"
    except Exception:
        return exp[:10]

# ── Auth ────────────────────────────────────────────────────────────────────────
def is_owner(uid: int) -> bool:
    return uid == OWNER_ID

def is_admin(uid: int) -> bool:
    return str(uid) in load_admins()

def is_seller(uid: int) -> bool:
    return str(uid) in load_sellers()

def is_staff(uid: int) -> bool:
    return is_owner(uid) or is_admin(uid) or is_seller(uid)

def get_admin_data(uid: int) -> dict:
    return load_admins().get(str(uid), {})

def get_admin_of_seller(sid: str) -> str | None:
    """Return admin UID (str) who created this seller, or None."""
    sellers = load_sellers()
    sd = sellers.get(sid, {})
    added_by = sd.get("added_by")
    if added_by and is_admin(int(added_by)):
        return str(added_by)
    return None

def _migrate_seller_to_uid(uid: int, username: str | None):
    """
    Sellers added via @username get their entry moved to numeric ID
    on first contact — so all future auth + quota checks work by UID.
    """
    if not username:
        return
    sellers = load_sellers()
    sid_str = str(uid)
    if sid_str in sellers:
        return  # already stored by numeric ID — nothing to do
    uname = username.lstrip("@").lower()
    for sid, sd in list(sellers.items()):
        if sd.get("username", "").lstrip("@").lower() == uname:
            sellers[sid_str] = sellers.pop(sid)
            save_sellers(sellers)
            log.info(f"seller migrated: '{sid}' → '{sid_str}'")
            return

def _migrate_admin_to_uid(uid: int, username: str | None):
    """Admins added via @username get their entry moved to numeric ID on first contact."""
    if not username:
        return
    admins = load_admins()
    aid_str = str(uid)
    if aid_str in admins:
        return
    uname = username.lstrip("@").lower()
    for aid, ad in list(admins.items()):
        if ad.get("username", "").lstrip("@").lower() == uname:
            admins[aid_str] = admins.pop(aid)
            save_admins(admins)
            log.info(f"admin migrated: '{aid}' → '{aid_str}'")
            return

def owner_only_cb(fn):
    @wraps(fn)
    async def w(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update.effective_user.id):
            await update.callback_query.answer("Owner only", show_alert=True)
            return
        return await fn(update, ctx)
    return w

def owner_or_admin_cb(fn):
    """Owner OR Admin — but not plain sellers."""
    @wraps(fn)
    async def w(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        _migrate_seller_to_uid(user.id, user.username)
        _migrate_admin_to_uid(user.id, user.username)
        if not (is_owner(user.id) or is_admin(user.id)):
            await update.callback_query.answer("Access denied", show_alert=True)
            return
        return await fn(update, ctx)
    return w

def staff_only_cb(fn):
    @wraps(fn)
    async def w(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        _migrate_seller_to_uid(user.id, user.username)
        _migrate_admin_to_uid(user.id, user.username)
        if not is_staff(user.id):
            await update.callback_query.answer("Access denied", show_alert=True)
            return
        return await fn(update, ctx)
    return w

def owner_only(fn):
    @wraps(fn)
    async def w(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update.effective_user.id):
            await update.message.reply_text(
                f"{e('lock')} <b>Access Denied</b>", parse_mode=ParseMode.HTML)
            return
        return await fn(update, ctx)
    return w

# ────────────────────────────────────────────────────────────────────────────────
# KEYBOARDS
# ────────────────────────────────────────────────────────────────────────────────
def back_kb(target="menu_main"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=target)]])

def main_kb(uid: int):
    if is_owner(uid):
        rows = [
            [InlineKeyboardButton("Keys",      callback_data="menu_keys"),
             InlineKeyboardButton("Instances", callback_data="menu_instances")],
            [InlineKeyboardButton("IPA",       callback_data="menu_ipa"),
             InlineKeyboardButton("Users",     callback_data="menu_users")],
            [InlineKeyboardButton("Stats",     callback_data="menu_stats"),
             InlineKeyboardButton("🔨 Build IPA", callback_data="menu_build")],
            [InlineKeyboardButton("Sellers",   callback_data="menu_sellers"),
             InlineKeyboardButton("Admins",    callback_data="menu_admins")],
            [InlineKeyboardButton("Broadcast", callback_data="menu_broadcast")],
        ]
    elif is_admin(uid):
        # Admin: only Keys + their own Sellers panel
        rows = [
            [InlineKeyboardButton("Keys",    callback_data="menu_keys"),
             InlineKeyboardButton("Sellers", callback_data="menu_sellers")],
        ]
    else:
        # Seller: only Keys
        rows = [
            [InlineKeyboardButton("Keys", callback_data="menu_keys")],
        ]
    return InlineKeyboardMarkup(rows)

def instances_kb(instances: list):
    rows = [[InlineKeyboardButton("New Instance", callback_data="inst_new")]]
    for inst in instances:
        patch_ok = inst_has_patch(inst)
        enabled  = inst.get("enabled", True)
        games    = inst_patch_games(inst)
        if not patch_ok:
            status = "[NO FILE]"
        elif not enabled:
            status = "[OFF]"
        else:
            ff_ok    = "ff" in games
            ffmax_ok = "ffmax" in games
            if ff_ok and ffmax_ok:
                status = "[FF✅ MAX✅]"
            elif ff_ok:
                status = "[FF✅ MAX❌]"
            elif ffmax_ok:
                status = "[FF❌ MAX✅]"
            else:
                status = "[NO FILE]"
        rows.append([
            InlineKeyboardButton(
                f"{status} {inst['label']}",
                callback_data=f"inst_info_{inst['id']}"
            ),
            InlineKeyboardButton("Delete", callback_data=f"inst_del_{inst['id']}"),
        ])
    if instances:
        rows.append([
            InlineKeyboardButton("Add All to Keys", callback_data="inst_addall_keys"),
            InlineKeyboardButton("Delete All",       callback_data="inst_del_all_ask"),
        ])
    rows.append([InlineKeyboardButton("Back", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)

def instance_info_kb(iid: str, enabled: bool = True, has_ff: bool = False, has_ffmax: bool = False):
    toggle_label = "Disable Instance" if enabled else "Enable Instance"
    toggle_data  = f"inst_disable_{iid}" if enabled else f"inst_enable_{iid}"
    ff_label    = "✅ FF .mxff (Replace)"    if has_ff    else "⬆️ Upload FF .mxff"
    ffmax_label = "✅ FFMax .mxff (Replace)" if has_ffmax else "⬆️ Upload FFMax .mxff"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(ff_label,    callback_data=f"inst_upload_ff_{iid}")],
        [InlineKeyboardButton(ffmax_label, callback_data=f"inst_upload_ffmax_{iid}")],
        [InlineKeyboardButton(toggle_label, callback_data=toggle_data)],
        [InlineKeyboardButton("Change Icon", callback_data=f"inst_icon_{iid}"),
         InlineKeyboardButton("Delete",      callback_data=f"inst_del_{iid}")],
        [InlineKeyboardButton("Back",        callback_data="menu_instances")],
    ])

# Game select keyboard for instance creation
def game_select_kb(prefix: str, selected_game: str = "ff"):
    rows = []
    for g in GAMES:
        mark = "[ON]" if g["id"] == selected_game else "[ ]"
        rows.append([InlineKeyboardButton(
            f"{mark} {g['name']}",
            callback_data=f"{prefix}_game_{g['id']}"
        )])
    rows.append([InlineKeyboardButton("Confirm",  callback_data=f"{prefix}_game_confirm")])
    rows.append([InlineKeyboardButton("Back",     callback_data="menu_instances")])
    return InlineKeyboardMarkup(rows)

def instance_select_kb(instances: list, selected_ids: list, prefix="keyinst"):
    rows = []
    for inst in instances:
        if not inst_has_patch(inst):
            continue
        checked = "[ON]" if inst["id"] in selected_ids else "[ ]"
        rows.append([InlineKeyboardButton(
            f"{checked} {inst['label']}",
            callback_data=f"{prefix}_toggle_{inst['id']}"
        )])
    rows.append([
        InlineKeyboardButton("All",  callback_data=f"{prefix}_all"),
        InlineKeyboardButton("None", callback_data=f"{prefix}_none"),
    ])
    rows.append([InlineKeyboardButton("Next: Duration", callback_data="key_go_dur")])
    rows.append([InlineKeyboardButton("Back",           callback_data="menu_keys")])
    return InlineKeyboardMarkup(rows)

def duration_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1h",        callback_data="dur_h1"),
         InlineKeyboardButton("6h",        callback_data="dur_h6"),
         InlineKeyboardButton("12h",       callback_data="dur_h12"),
         InlineKeyboardButton("24h",       callback_data="dur_h24")],
        [InlineKeyboardButton("3 Days",    callback_data="dur_d3"),
         InlineKeyboardButton("7 Days",    callback_data="dur_d7"),
         InlineKeyboardButton("30 Days",   callback_data="dur_d30"),
         InlineKeyboardButton("90 Days",   callback_data="dur_d90")],
        [InlineKeyboardButton("Lifetime",  callback_data="dur_d0"),
         InlineKeyboardButton("✏️ Custom", callback_data="dur_custom")],
        [InlineKeyboardButton("Back",      callback_data="key_create")],
    ])

def keys_menu_kb(uid: int = 0):
    # Custom Key is restricted to Owner and Admins only
    if is_owner(uid) or is_admin(uid):
        rows = [
            [InlineKeyboardButton("Create Key",    callback_data="key_create"),
             InlineKeyboardButton("Custom Key", callback_data="key_custom")],
            [InlineKeyboardButton("List Keys",     callback_data="key_list")],
            [InlineKeyboardButton("Block Key",     callback_data="key_block_ask"),
             InlineKeyboardButton("Delete Key",    callback_data="key_del_ask")],
            [InlineKeyboardButton("Back",          callback_data="menu_main")],
        ]
    else:
        rows = [
            [InlineKeyboardButton("Create Key", callback_data="key_create")],
            [InlineKeyboardButton("List Keys",  callback_data="key_list")],
            [InlineKeyboardButton("Block Key",  callback_data="key_block_ask"),
             InlineKeyboardButton("Delete Key", callback_data="key_del_ask")],
            [InlineKeyboardButton("Back",       callback_data="menu_main")],
        ]
    return InlineKeyboardMarkup(rows)

def ipa_kb(enabled: bool):
    toggle_label = "Disable IPA" if enabled else "Enable IPA"
    toggle_data  = "ipa_disable" if enabled else "ipa_enable"
    status_str   = "Enabled" if enabled else "Disabled"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Status: {status_str}", callback_data="noop")],
        [InlineKeyboardButton(toggle_label,            callback_data=toggle_data)],
        [InlineKeyboardButton("Back",                  callback_data="menu_main")],
    ])

def build_main_kb(run_id=None, run_status: str | None = None):
    """Keyboard for the Build IPA panel."""
    rows = [
        [InlineKeyboardButton("🔨 Trigger Build", callback_data="build_trigger")],
    ]
    if run_id:
        rows.append([
            InlineKeyboardButton("🔄 Refresh Status", callback_data=f"build_status_{run_id}"),
        ])
        if run_status == "completed_success":
            rows.append([
                InlineKeyboardButton("📥 Download IPA", callback_data=f"build_dl_{run_id}"),
            ])
    rows.append([InlineKeyboardButton("Back", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)

def admins_kb(admins: dict):
    rows = [[InlineKeyboardButton("Add Admin", callback_data="admin_add")]]
    for aid, ad in list(admins.items())[:20]:
        name    = ad.get("username") or aid
        blocked = ad.get("blocked", False)
        label   = f"[BLOCKED] {name}" if blocked else name
        rows.append([
            InlineKeyboardButton(label,    callback_data=f"admin_info_{aid}"),
            InlineKeyboardButton("Remove", callback_data=f"admin_del_{aid}"),
        ])
    rows.append([InlineKeyboardButton("Back", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)

def admin_info_kb(aid: str, blocked: bool = False):
    block_label = "Unblock Admin" if blocked else "Block Admin"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(block_label,        callback_data=f"admin_block_{aid}")],
        [InlineKeyboardButton("View Their Sellers", callback_data=f"admin_sellers_{aid}")],
        [InlineKeyboardButton("Remove Admin",      callback_data=f"admin_del_{aid}")],
        [InlineKeyboardButton("Back",              callback_data="menu_admins")],
    ])

def sellers_kb(sellers: dict, back_target: str = "menu_main"):
    rows = [[InlineKeyboardButton("Add Seller", callback_data="seller_add")]]
    for sid, sd in list(sellers.items())[:20]:
        name    = sd.get("username") or sid
        blocked = sd.get("blocked", False)
        label   = f"[BLOCKED] {name}" if blocked else name
        rows.append([
            InlineKeyboardButton(label,    callback_data=f"seller_info_{sid}"),
            InlineKeyboardButton("Remove", callback_data=f"seller_del_{sid}"),
        ])
    rows.append([InlineKeyboardButton("Back", callback_data=back_target)])
    return InlineKeyboardMarkup(rows)

def seller_info_kb(sid: str, blocked: bool = False):
    block_label = "Unblock Seller" if blocked else "Block Seller"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Set Quota",        callback_data=f"seller_quota_{sid}"),
         InlineKeyboardButton("Reset Quota",      callback_data=f"seller_reset_quota_{sid}")],
        [InlineKeyboardButton("View Keys",        callback_data=f"seller_keys_{sid}"),
         InlineKeyboardButton(block_label,        callback_data=f"seller_block_{sid}")],
        [InlineKeyboardButton("Delete All Keys",  callback_data=f"seller_delkeys_{sid}"),
         InlineKeyboardButton("Delete Unused",    callback_data=f"seller_delunused_{sid}")],
        [InlineKeyboardButton("Remove Seller",    callback_data=f"seller_del_{sid}")],
        [InlineKeyboardButton("Back",             callback_data="menu_sellers")],
    ])

def users_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("List Users",   callback_data="users_list"),
         InlineKeyboardButton("Active Today", callback_data="users_active")],
        [InlineKeyboardButton("Back",         callback_data="menu_main")],
    ])

# ── /start ──────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    username = update.effective_user.username
    _migrate_seller_to_uid(uid, username)
    _migrate_admin_to_uid(uid, username)
    # Block check for admins
    if is_admin(uid):
        ad = load_admins().get(str(uid), {})
        if ad.get("blocked"):
            await update.message.reply_text(
                f"{e('lock')} <b>Access Denied</b>\n\nYour admin account is blocked.",
                parse_mode=ParseMode.HTML)
            return
    if not is_staff(uid):
        await update.message.reply_text(
            f"{e('lock')} <b>Access Denied</b>\n\nYou are not authorised.",
            parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text(
        _main_menu_text(uid),
        parse_mode=ParseMode.HTML,
        reply_markup=main_kb(uid),
    )

def _main_menu_text(uid: int) -> str:
    cfg    = load_cfg()
    keys   = load_keys()
    active = sum(1 for k in keys.values() if key_status(k) == "active")
    ipa_st = f"{e('greencircle')} Enabled" if cfg.get("ipa_enabled", True) else f"{e('redcircle')} Disabled"

    if is_owner(uid):
        sellers   = load_sellers()
        admins    = load_admins()
        instances = load_instances()
        ready     = sum(1 for i in instances if inst_has_patch(i))
        return (
            f"{e('crown')} <b>MANAGE x FF</b> — Owner Panel\n\n"
            f"{e('shield')} IPA: {ipa_st}\n"
            f"{e('key')} Keys: <b>{active}</b> active / <b>{len(keys)}</b> total\n"
            f"{e('aim')} Instances: <b>{ready}</b> ready / <b>{len(instances)}</b> total\n"
            f"   {e('gamepad')} Each instance: FF + FF MAX\n"
            f"{e('seller')} Sellers: <b>{len(sellers)}</b>\n"
            f"{e('person')} Admins: <b>{len(admins)}</b>\n"
            f"{e('robot')} Bot: Online\n"
        )
    elif is_admin(uid):
        admins   = load_admins()
        ad       = admins.get(str(uid), {})
        sellers  = load_sellers()
        # Count sellers created by this admin
        my_sellers = [s for s, sd in sellers.items() if str(sd.get("added_by")) == str(uid)]
        # Count keys created by this admin or their sellers
        my_seller_uids = set(my_sellers)
        my_keys = [k for k, kd in keys.items()
                   if str(kd.get("created_by")) == str(uid)
                   or str(kd.get("created_by")) in my_seller_uids]
        my_active = sum(1 for k in my_keys if key_status(keys[k]) == "active")
        blocked_adm = ad.get("blocked", False)
        status = f"{e('redcircle')} Blocked" if blocked_adm else f"{e('greencircle')} Active"
        return (
            f"{e('shield')} <b>MANAGE x FF</b> — Admin Panel\n\n"
            f"{e('key')} My Keys: <b>{my_active}</b> active / <b>{len(my_keys)}</b> total\n"
            f"{e('seller')} My Sellers: <b>{len(my_sellers)}</b>\n"
            f"{e('shield')} Status: {status}\n"
            f"{e('robot')} Bot: Online\n"
        )
    else:
        sellers = load_sellers()
        sd      = sellers.get(str(uid), {})
        used    = sd.get("keys_created", 0)
        qmax    = sd.get("quota", 0)
        blocked = sd.get("blocked", False)
        status  = f"{e('redcircle')} Blocked" if blocked else f"{e('greencircle')} Active"
        return (
            f"{e('diamond')} <b>MANAGE x FF</b> — Seller Panel\n\n"
            f"{e('key')} Keys: <b>{used}</b> / <b>{qmax if qmax else '∞'}</b>\n"
            f"{e('shield')} Status: {status}\n"
            f"{e('robot')} Bot: Online\n"
        )

async def cb_main(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid      = update.effective_user.id
    username = update.effective_user.username
    _migrate_seller_to_uid(uid, username)
    _migrate_admin_to_uid(uid, username)
    if not is_staff(uid):
        return
    await q.edit_message_text(
        _main_menu_text(uid),
        parse_mode=ParseMode.HTML,
        reply_markup=main_kb(uid),
    )

# ── INSTANCE MANAGEMENT ─────────────────────────────────────────────────────────
@owner_only_cb
async def cb_menu_instances(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    instances = load_instances()
    ready = sum(1 for i in instances if inst_has_patch(i))
    txt = (
        f"{e('aim')} <b>Instances</b>\n\n"
        f"Total: <b>{len(instances)}</b>  |  Ready: <b>{ready}</b>\n\n"
        f"Each instance needs a <code>.mxff</code> file.\n"
        f"Each instance works for <b>both FF and FF MAX</b>.\n"
        f"[NO FILE] instances are excluded from key creation."
    )
    await q.edit_message_text(txt, parse_mode=ParseMode.HTML,
                               reply_markup=instances_kb(instances))

@owner_only_cb
async def cb_inst_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["pending"] = "inst_create_name"
    await q.edit_message_text(
        f"{e('plus')} <b>New Instance</b>\n\n"
        f"Send the instance name (e.g. <code>AimNeck</code>, <code>MagicBullet</code>):\n\n"
        f"{e('info')} Instance will automatically work for both <b>FF</b> and <b>FF MAX</b>.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_kb("menu_instances"),
    )

@owner_only_cb
async def cb_inst_game_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Legacy handler — kept for back-compat but game select is removed from flow."""
    q = update.callback_query; await q.answer()
    # Just redirect to instances menu — this step no longer exists
    instances = load_instances()
    ready = sum(1 for i in instances if inst_has_patch(i))
    txt = (
        f"{e('aim')} <b>Instances</b>\n\n"
        f"Total: <b>{len(instances)}</b>  |  Ready: <b>{ready}</b>\n\n"
        f"Each instance needs a <code>.mxff</code> file.\n"
        f"[NO FILE] instances are excluded from key creation."
    )
    await q.edit_message_text(txt, parse_mode=ParseMode.HTML,
                               reply_markup=instances_kb(instances))

@owner_only_cb
async def cb_inst_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    iid  = q.data.replace("inst_info_", "")
    inst = find_instance(iid)
    if not inst:
        await q.answer("Not found", show_alert=True); return
    enabled = inst.get("enabled", True)
    games   = inst_patch_games(inst)
    has_ff    = "ff" in games
    has_ffmax = "ffmax" in games
    status_icon = e("greencircle") if enabled else e("redcircle")
    status_txt  = "Enabled" if enabled else "Disabled (shown as Not in Plan in IPA)"

    # Per-game patch info
    def _patch_line(game_label, field_key, legacy_ok):
        pf = inst.get(field_key)
        if pf:
            kb = inst.get("file_size_" + field_key.replace("patch_file_", ""), 0)
            sz = f"{kb // 1024} KB" if kb else "?"
            return f"✅ {pf} ({sz})"
        elif legacy_ok and inst.get("patch_file"):
            pf2 = inst["patch_file"]
            kb2 = inst.get("file_size", 0)
            sz2 = f"{kb2 // 1024} KB" if kb2 else "?"
            return f"✅ {pf2} ({sz2}) [shared]"
        return "⚠️ No file — upload .mxff"

    ff_line    = _patch_line("Free Fire",    "patch_file_ff",    not inst.get("patch_file_ffmax"))
    ffmax_line = _patch_line("Free Fire MAX","patch_file_ffmax", not inst.get("patch_file_ff"))
    game_lbl   = inst_game_label(inst)

    # Inject mode — auto-determined from rule count (set on upload or IPA report)
    multi_inject = bool(inst.get("multi_inject", False))
    rc_ff    = inst.get("rule_count_ff",    inst.get("rule_count", 1))
    rc_ffmax = inst.get("rule_count_ffmax", inst.get("rule_count", 1))
    if multi_inject:
        inject_mode_txt = f"⚡ Multi-Rule ({rc_ff}FF / {rc_ffmax}MAX rules) — No backup, delete on off"
    else:
        inject_mode_txt = f"🔄 Single Rule — Backup+Restore on toggle-off"

    txt = (
        f"{e('aim')} <b>{inst['label']}</b>\n\n"
        f"ID: <code>{inst['id']}</code>\n"
        f"Works for: <b>{game_lbl}</b>\n"
        f"SF Icon: <code>{inst.get('icon', 'scope')}</code>\n"
        f"Status: {status_icon} <b>{status_txt}</b>\n"
        f"FF Patch:    {ff_line}\n"
        f"FFMax Patch: {ffmax_line}\n"
        f"Inject Mode: {inject_mode_txt}\n"
        f"Created: {inst.get('created_at', '?')[:10]}"
    )
    await q.edit_message_text(txt, parse_mode=ParseMode.HTML,
                               reply_markup=instance_info_kb(iid, enabled=enabled,
                                                             has_ff=has_ff, has_ffmax=has_ffmax))

@owner_only_cb
async def cb_inst_upload_ff(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Upload FF-specific .mxff patch file."""
    q = update.callback_query; await q.answer()
    iid  = q.data.replace("inst_upload_ff_", "")
    inst = find_instance(iid)
    if not inst:
        await q.answer("Not found", show_alert=True); return
    ctx.user_data["pending"] = f"inst_upload_file_ff_{iid}"
    await q.edit_message_text(
        f"{e('upload')} <b>Upload FF .mxff for {inst['label']}</b>\n"
        f"{e('gamepad')} This file will inject into <b>Free Fire</b> only.\n\n"
        f"Send the <code>.mxff</code> file now (3105PATCH format).\n"
        f"{e('info')} Files up to 2 GB are supported (streamed in background).\n"
        f"{e('warning')} Large files (20MB+) are fully supported — no limit.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_kb(f"inst_info_{iid}"),
    )

@owner_only_cb
async def cb_inst_upload_ffmax(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Upload FFMax-specific .mxff patch file."""
    q = update.callback_query; await q.answer()
    iid  = q.data.replace("inst_upload_ffmax_", "")
    inst = find_instance(iid)
    if not inst:
        await q.answer("Not found", show_alert=True); return
    ctx.user_data["pending"] = f"inst_upload_file_ffmax_{iid}"
    await q.edit_message_text(
        f"{e('upload')} <b>Upload FFMax .mxff for {inst['label']}</b>\n"
        f"{e('gamepad')} This file will inject into <b>Free Fire MAX</b> only.\n\n"
        f"Send the <code>.mxff</code> file now (3105PATCH format).\n"
        f"{e('info')} Files up to 2 GB are supported (streamed in background).\n"
        f"{e('warning')} Large files (20MB+) are fully supported — no limit.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_kb(f"inst_info_{iid}"),
    )

@owner_only_cb
async def cb_inst_upload(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Fallback/legacy handler — redirects to inst_info."""
    q = update.callback_query; await q.answer()
    # Strip known prefixes to get iid safely
    raw = q.data.replace("inst_upload_", "")
    await q.edit_message_text(
        f"{e('info')} Use the FF or FFMax upload buttons on the instance page.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Back", callback_data=f"inst_info_{raw}")],
        ]),
    )

@owner_only_cb
async def cb_inst_icon(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    iid = q.data.replace("inst_icon_", "")
    ctx.user_data["pending"] = f"inst_set_icon_{iid}"
    await q.edit_message_text(
        f"{e('edit')} Send new SF Symbol name:\n\n"
        f"Examples: <code>scope</code>, <code>figure.stand</code>, "
        f"<code>heart.fill</code>, <code>cube.transparent</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=back_kb("menu_instances"),
    )

@owner_only_cb
async def cb_inst_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    iid       = q.data.replace("inst_del_", "")
    instances = load_instances()
    inst      = next((i for i in instances if i["id"] == iid), None)
    if not inst:
        await q.answer("Not found", show_alert=True); return
    # Delete all game-specific and legacy patch files
    for fname in [
        inst.get("patch_file_ff"),
        inst.get("patch_file_ffmax"),
        inst.get("patch_file"),              # legacy single file
        f"{iid}_ff.mxff", f"{iid}_ffmax.mxff",
        f"{iid}.mxff",    f"{iid}.mxff",     # legacy names
    ]:
        if fname:
            fp = PATCH_DIR / fname
            if fp.exists():
                fp.unlink(missing_ok=True)
    instances = [i for i in instances if i["id"] != iid]
    save_instances(instances)
    await q.edit_message_text(
        f"{e('bin')} Instance <b>{inst['label']}</b> deleted.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_kb("menu_instances"),
    )

@owner_only_cb
async def cb_inst_toggle_enabled(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Enable or disable an instance (controls IPA visibility)."""
    q   = update.callback_query; await q.answer()
    raw = q.data  # inst_enable_<id> or inst_disable_<id>
    if raw.startswith("inst_enable_"):
        iid     = raw.replace("inst_enable_", "")
        new_val = True
    else:
        iid     = raw.replace("inst_disable_", "")
        new_val = False

    instances = load_instances()
    inst = next((i for i in instances if i["id"] == iid), None)
    if not inst:
        await q.answer("Not found", show_alert=True); return

    inst["enabled"] = new_val
    save_instances(instances)

    label     = inst["label"]
    icon      = e("greencircle") if new_val else e("redcircle")
    status    = "Enabled" if new_val else "Disabled"
    note      = "" if new_val else "\n\nThis instance will appear as <b>Not in Plan</b> in the IPA for all users."
    await q.edit_message_text(
        f"{icon} Instance <b>{label}</b> is now <b>{status}</b>.{note}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Back to Instance", callback_data=f"inst_info_{iid}")],
            [InlineKeyboardButton("Instances",        callback_data="menu_instances")],
        ]),
    )

@owner_only_cb
async def cb_inst_addall_keys(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """One-click: add ALL enabled instances (with patch) to every existing key."""
    q = update.callback_query; await q.answer()
    instances = load_instances()
    # Only enabled instances that have a patch file
    active_ids = [i["id"] for i in instances if inst_has_patch(i) and i.get("enabled", True)]
    if not active_ids:
        await q.answer("No active instances with .mxff files!", show_alert=True); return

    keys = load_keys()
    updated = 0
    for kd in keys.values():
        existing = set(kd.get("instances", []))
        merged   = list(existing | set(active_ids))
        if merged != list(existing):
            kd["instances"] = merged
            updated += 1
    save_keys(keys)

    await q.edit_message_text(
        f"{e('check')} <b>Done!</b>\n\n"
        f"Added <b>{len(active_ids)}</b> instance(s) to <b>{updated}</b> key(s).\n\n"
        f"Instances added: <code>{', '.join(active_ids)}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Instances", callback_data="menu_instances")],
            [InlineKeyboardButton("Main",      callback_data="menu_main")],
        ]),
    )

@owner_only_cb
async def cb_inst_del_all_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Ask for confirmation before deleting all instances."""
    q = update.callback_query; await q.answer()
    instances = load_instances()
    await q.edit_message_text(
        f"{e('warning')} <b>Delete ALL Instances?</b>\n\n"
        f"This will permanently delete all <b>{len(instances)}</b> instance(s) and their .mxff patch files.\n"
        f"Keys referencing them will have empty instance lists.\n\n"
        f"<b>This cannot be undone!</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Yes, Delete All", callback_data="inst_del_all_confirm")],
            [InlineKeyboardButton("Cancel",          callback_data="menu_instances")],
        ]),
    )

@owner_only_cb
async def cb_inst_del_all_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Delete all instances and their patch files."""
    q = update.callback_query; await q.answer()
    instances = load_instances()
    count = len(instances)
    for inst in instances:
        iid = inst["id"]
        for fname in [
            inst.get("patch_file_ff"),
            inst.get("patch_file_ffmax"),
            inst.get("patch_file"),
            f"{iid}_ff.mxff", f"{iid}_ffmax.mxff",
            f"{iid}.mxff",    f"{iid}.mxff",
        ]:
            if fname:
                fp = PATCH_DIR / fname
                if fp.exists():
                    fp.unlink(missing_ok=True)
    save_instances([])
    await q.edit_message_text(
        f"{e('bin')} All <b>{count}</b> instance(s) deleted.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_kb("menu_instances"),
    )



def _detect_mxff_rule_count(file_path) -> int:
    """
    Read a .mxff file and detect how many patch rules/files it contains.
    .mxff format: 9-byte magic '3105PATCH' + null byte + bplist data.
    The plist encryptedPayload is encrypted, so we can't read rules directly.
    We use file size heuristic + packageID uniqueness as a proxy:
      - If the same instance has 2+ separate .mxff files uploaded (ff + ffmax),
        each is treated as its own single-rule file.
    For a single .mxff, we return the rule_count stored in the instance metadata,
    defaulting to 1 if not set.
    This function is a placeholder — the IPA reports actual rule_count via
    POST /inject/report_rules after first inject.
    Returns: int (1 = single rule, 2+ = multi rule)
    """
    try:
        import plistlib
        data = open(file_path, "rb").read()
        if data[:9] != b"3105PATCH":
            return 1
        rest = data[9:]
        if rest[:1] == b"\x00":
            rest = rest[1:]
        pl = plistlib.loads(rest)
        # schemaVersion 2+ may carry multi-rule payloads
        schema = pl.get("schemaVersion", 1)
        # Check for multi-rule marker in plist (IPA sets this when building)
        rule_count = pl.get("ruleCount", None)
        if rule_count is not None:
            return int(rule_count)
        # Fallback: schema v1 with large payload may be multi-rule
        # We can't know without decrypting — default to 1, IPA will report
        return 1
    except Exception:
        return 1

# ── KEY MANAGEMENT ──────────────────────────────────────────────────────────────
@staff_only_cb
async def cb_menu_keys(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid  = update.effective_user.id
    keys = load_keys()

    if is_owner(uid):
        mine = keys
    elif is_admin(uid):
        # Admin sees own keys + their sellers' keys
        sellers    = load_sellers()
        my_sellers = {s for s, sd in sellers.items() if str(sd.get("added_by")) == str(uid)}
        mine = {k: v for k, v in keys.items()
                if str(v.get("created_by")) == str(uid)
                or str(v.get("created_by")) in my_sellers}
    else:
        mine = {k: v for k, v in keys.items() if v.get("created_by") == uid}

    active  = sum(1 for v in mine.values() if key_status(v) == "active")
    blocked = sum(1 for v in mine.values() if v.get("blocked"))

    quota_line = ""
    if not is_owner(uid) and not is_admin(uid):
        sellers = load_sellers()
        sd      = sellers.get(str(uid), {})
        qmax = sd.get("quota", 0)
        used = sd.get("keys_created", 0)
        quota_line = f"\n{e('moneybag')} Quota: <b>{used}</b> / <b>{qmax if qmax else '∞'}</b>"

    txt = (
        f"{e('key')} <b>Key Management</b>\n\n"
        f"{e('greencircle')} Active:  <b>{active}</b>\n"
        f"{e('redcircle')} Blocked: <b>{blocked}</b>\n"
        f"{e('people')} Total:   <b>{len(mine)}</b>"
        + quota_line
    )
    await q.edit_message_text(txt, parse_mode=ParseMode.HTML,
                               reply_markup=keys_menu_kb(uid))

@staff_only_cb
async def cb_key_create(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid       = update.effective_user.id
    instances = [i for i in load_instances() if inst_has_patch(i) and i.get("enabled", True)]
    if not instances:
        await q.answer("No instances with .mxff files yet!", show_alert=True)
        return
    if not is_owner(uid):
        sellers = load_sellers()
        sd      = sellers.get(str(uid), {})
        if sd.get("blocked"):
            await q.answer("Your account is blocked.", show_alert=True); return
        qmax = sd.get("quota", 0)
        if qmax and sd.get("keys_created", 0) >= qmax:
            await q.answer(f"Quota reached ({qmax})", show_alert=True); return

    # ── Reset ALL stale state from any previous key creation flow ──────────
    for _k in ("new_key_device_limit", "new_key_stock", "new_key_owner",
               "new_key_count", "key_flow", "pending"):
        ctx.user_data.pop(_k, None)
    # Reset instance selection fresh every time
    ctx.user_data["new_key_insts"] = [i["id"] for i in instances]
    sel = ctx.user_data["new_key_insts"]
    await q.edit_message_text(
        f"{e('aim')} <b>Create Key — Step 1: Select Instances</b>\n\n"
        f"Select which instances (features) this key unlocks:\n"
        f"{e('info')} Each instance works for both <b>FF & FF MAX</b>.",
        parse_mode=ParseMode.HTML,
        reply_markup=instance_select_kb(instances, sel),
    )

@staff_only_cb
async def cb_keyinst_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    data      = q.data
    instances = [i for i in load_instances() if inst_has_patch(i) and i.get("enabled", True)]
    sel       = list(ctx.user_data.get("new_key_insts", [i["id"] for i in instances]))

    if data == "keyinst_all":
        sel = [i["id"] for i in instances]
    elif data == "keyinst_none":
        sel = []
    else:
        iid = data.replace("keyinst_toggle_", "")
        if iid in sel:
            sel.remove(iid)
        else:
            sel.append(iid)

    ctx.user_data["new_key_insts"] = sel
    await q.edit_message_text(
        f"{e('aim')} <b>Create Key — Step 1: Select Instances</b>\n\n"
        f"Select which instances (features) this key unlocks:\n"
        f"{e('info')} Each instance works for both <b>FF & FF MAX</b>.",
        parse_mode=ParseMode.HTML,
        reply_markup=instance_select_kb(instances, sel),
    )

async def _custom_key_go_dur_inner(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Shared inner logic for custom key device-limit step (no decorator)."""
    q = update.callback_query
    sel = ctx.user_data.get("new_key_insts", [])
    if not sel:
        await q.answer("Select at least one instance!", show_alert=True); return
    instances = load_instances()
    names = [next((i['label'] for i in instances if i["id"] == s), s) for s in sel]
    await q.edit_message_text(
        f"{e('phone')} <b>Custom Key — Step 2: Device Limit</b>\n\n"
        f"{e('aim')} Instances: <b>{', '.join(names)}</b>\n\n"
        f"How many devices can use this key?\n"
        f"<i>0 or Unlimited = no limit</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("1 Device",   callback_data="cdevlimit_1"),
             InlineKeyboardButton("2 Devices",  callback_data="cdevlimit_2"),
             InlineKeyboardButton("3 Devices",  callback_data="cdevlimit_3")],
            [InlineKeyboardButton("5 Devices",  callback_data="cdevlimit_5"),
             InlineKeyboardButton("10 Devices", callback_data="cdevlimit_10"),
             InlineKeyboardButton("Unlimited",  callback_data="cdevlimit_0")],
            [InlineKeyboardButton("Back",       callback_data="key_custom")],
        ]),
    )


@staff_only_cb
async def cb_key_go_dur(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    # If we are in custom flow, hand off to custom device-limit step
    if ctx.user_data.get("key_flow") == "custom":
        await _custom_key_go_dur_inner(update, ctx)
        return
    sel = ctx.user_data.get("new_key_insts", [])
    if not sel:
        await q.answer("Select at least one instance!", show_alert=True); return
    instances = load_instances()
    names = [next((i['label'] for i in instances if i["id"] == s), s) for s in sel]
    # Go to device limit step first
    await q.edit_message_text(
        f"{e('phone')} <b>Create Key — Step 2: Device Limit</b>\n\n"
        f"{e('aim')} Instances: <b>{', '.join(names)}</b>\n\n"
        f"How many devices can use this key?\n"
        f"<i>0 or Unlimited = no limit</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=device_limit_kb(),
    )

def device_limit_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1 Device",   callback_data="devlimit_1"),
         InlineKeyboardButton("2 Devices",  callback_data="devlimit_2"),
         InlineKeyboardButton("3 Devices",  callback_data="devlimit_3")],
        [InlineKeyboardButton("5 Devices",  callback_data="devlimit_5"),
         InlineKeyboardButton("10 Devices", callback_data="devlimit_10"),
         InlineKeyboardButton("Unlimited",  callback_data="devlimit_0")],
        [InlineKeyboardButton("Back", callback_data="key_create")],
    ])

@staff_only_cb
async def cb_devlimit_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid   = update.effective_user.id
    limit = int(q.data.replace("devlimit_", ""))
    ctx.user_data["new_key_device_limit"] = limit
    sel = ctx.user_data.get("new_key_insts", [])
    instances = load_instances()
    names = [next((i['label'] for i in instances if i["id"] == s), s) for s in sel]
    limit_txt = f"Max {limit} device(s)" if limit else "Unlimited"

    # Stock and Owner key types: Admin/Owner only
    can_advanced = is_owner(uid) or is_admin(uid)

    if can_advanced:
        type_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Regular", callback_data="keytype_regular"),
             InlineKeyboardButton("Stock",   callback_data="keytype_stock"),
             InlineKeyboardButton("Owner",   callback_data="keytype_owner")],
            [InlineKeyboardButton("Back",    callback_data="key_go_dur")],
        ])
        note = (
            f"• <b>Regular</b> — timer starts at creation\n"
            f"• <b>Stock</b> — timer starts only when first device activates\n"
            f"• <b>Owner</b> — unlocks Patch Making tab in IPA"
        )
    else:
        # Seller: Regular only → skip type step, go straight to count
        ctx.user_data["new_key_stock"] = False
        ctx.user_data["new_key_owner"] = False
        type_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Regular", callback_data="keytype_regular")],
            [InlineKeyboardButton("Back",    callback_data="key_go_dur")],
        ])
        note = "• <b>Regular</b> — timer starts at creation"

    await q.edit_message_text(
        f"{e('key')} <b>Create Key — Step 3: Key Type</b>\n\n"
        f"{e('aim')} Instances: <b>{', '.join(names)}</b>\n"
        f"{e('phone')} Devices: <b>{limit_txt}</b>\n\n"
        + note,
        parse_mode=ParseMode.HTML,
        reply_markup=type_kb,
    )


@staff_only_cb
async def cb_keytype_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query; await q.answer()
    uid = update.effective_user.id
    ktype = q.data.replace("keytype_", "")  # regular / stock / owner

    # Double-check: only Admin/Owner can set stock or owner type
    if ktype in ("stock", "owner") and not (is_owner(uid) or is_admin(uid)):
        await q.answer("Only Admin/Owner can create Stock or Owner keys.", show_alert=True)
        return

    ctx.user_data["new_key_stock"] = (ktype == "stock")
    ctx.user_data["new_key_owner"] = (ktype == "owner")
    sel = ctx.user_data.get("new_key_insts", [])
    instances = load_instances()
    names = [next((i['label'] for i in instances if i["id"] == s), s) for s in sel]
    limit = ctx.user_data.get("new_key_device_limit", 0)
    limit_txt  = f"Max {limit} device(s)" if limit else "Unlimited"
    type_label = {"regular": "Regular", "stock": "Stock (timer on activation)", "owner": "Owner (Patch Making)"}[ktype]
    # → Step 4: Count
    await q.edit_message_text(
        f"{e('key')} <b>Create Key — Step 4: Count</b>\n\n"
        f"{e('aim')} Instances: <b>{', '.join(names)}</b>\n"
        f"{e('phone')} Devices: <b>{limit_txt}</b>\n"
        f"{e('key')} Type: <b>{type_label}</b>\n\n"
        f"How many keys to create at once?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("1",  callback_data="keycount_1"),
             InlineKeyboardButton("5",  callback_data="keycount_5"),
             InlineKeyboardButton("10", callback_data="keycount_10")],
            [InlineKeyboardButton("25", callback_data="keycount_25"),
             InlineKeyboardButton("50", callback_data="keycount_50"),
             InlineKeyboardButton("100",callback_data="keycount_100")],
            [InlineKeyboardButton("Back", callback_data="devlimit_back")],
        ]),
    )


@staff_only_cb
async def cb_keycount_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    count = int(q.data.replace("keycount_", ""))
    ctx.user_data["new_key_count"] = count
    sel = ctx.user_data.get("new_key_insts", [])
    instances = load_instances()
    names = [next((i['label'] for i in instances if i["id"] == s), s) for s in sel]
    limit     = ctx.user_data.get("new_key_device_limit", 0)
    limit_txt = f"Max {limit} device(s)" if limit else "Unlimited"
    stock     = ctx.user_data.get("new_key_stock", False)
    owner     = ctx.user_data.get("new_key_owner", False)
    type_label = "Owner (Patch Making)" if owner else ("Stock (timer on activation)" if stock else "Regular")
    # → Step 5: Duration
    await q.edit_message_text(
        f"{e('key')} <b>Create Key — Step 5: Duration</b>\n\n"
        f"{e('aim')} Instances: <b>{', '.join(names)}</b>\n"
        f"{e('phone')} Devices: <b>{limit_txt}</b>\n"
        f"{e('key')} Type: <b>{type_label}</b>\n"
        f"🔢 Count: <b>{count} key(s)</b>\n\n"
        f"Select validity period:",
        parse_mode=ParseMode.HTML,
        reply_markup=duration_kb(),
    )

def parse_custom_duration(text: str):
    """
    Parse custom duration strings like:
    '2d 6h', '1d', '12h', '3 days 4 hours', '45m' (minutes→hours), '2d6h', etc.
    Returns (hours: int, days: int, label: str) or None if unparsable.
    """
    import re as _re
    text = text.lower().strip()
    # Extract numbers with units
    days_match  = _re.search(r'(\d+)\s*(?:d|day|days)',    text)
    hours_match = _re.search(r'(\d+)\s*(?:h|hr|hour|hours)', text)
    mins_match  = _re.search(r'(\d+)\s*(?:m|min|mins|minutes)', text)

    days  = int(days_match.group(1))  if days_match  else 0
    hours = int(hours_match.group(1)) if hours_match else 0
    mins  = int(mins_match.group(1))  if mins_match  else 0

    # Round minutes up to hours
    if mins > 0 and hours == 0:
        hours = max(1, round(mins / 60))

    if days == 0 and hours == 0:
        return None

    # Build label
    parts = []
    if days:  parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    label = " ".join(parts) if parts else "Lifetime"
    return hours, days, label


def _finalize_key(uid: int, hours: int, days: int, label: str,
                  sel: list, device_limit: int,
                  stock: bool = False, owner: bool = False) -> str:
    """Create one key in DB, update seller quota, return key string."""
    key_str  = gen_key()
    keys     = load_keys()
    total_h  = hours + days * 24
    keys[key_str] = {
        "created_at":      datetime.now(timezone.utc).isoformat(),
        # Stock key: expires_at stays None until first device activates
        "expires_at":      None if stock else expiry_from_spec(hours=hours, days=days),
        "duration_hours":  total_h,   # saved so stock keys know how long to run after activation
        "blocked":         False,
        "used_by":         [],
        "created_by":      uid,
        "instances":       sel,
        "device_limit":    device_limit,
        "stock":           stock,
        "activated_at":    None,      # set on first device use (stock only)
        "owner":           owner,     # owner key → IPA shows patch making tab
    }
    save_keys(keys)
    if not is_owner(uid):
        sellers = load_sellers()
        sd      = sellers.setdefault(str(uid), {})
        sd["keys_created"] = sd.get("keys_created", 0) + 1
        save_sellers(sellers)
    return key_str


def _finalize_keys_bulk(uid: int, count: int, hours: int, days: int, label: str,
                        sel: list, device_limit: int,
                        stock: bool = False, owner: bool = False) -> list:
    """Create `count` keys at once, return list of key strings."""
    keys     = load_keys()
    total_h  = hours + days * 24
    created  = []
    for _ in range(count):
        ks = gen_key()
        keys[ks] = {
            "created_at":     datetime.now(timezone.utc).isoformat(),
            "expires_at":     None if stock else expiry_from_spec(hours=hours, days=days),
            "duration_hours": total_h,
            "blocked":        False,
            "used_by":        [],
            "created_by":     uid,
            "instances":      sel,
            "device_limit":   device_limit,
            "stock":          stock,
            "activated_at":   None,
            "owner":          owner,
        }
        created.append(ks)
    save_keys(keys)
    if not is_owner(uid):
        sellers = load_sellers()
        sd      = sellers.setdefault(str(uid), {})
        sd["keys_created"] = sd.get("keys_created", 0) + count
        save_sellers(sellers)
    return created


def _finalize_key_custom(uid: int, key_str: str, hours: int, days: int,
                         label: str, sel: list, device_limit: int) -> str:
    """
    Save a custom-named key to the DB.
    key_str is whatever the owner/admin typed — stored as-is (uppercased).
    All validation/logic stays identical to regular keys.
    """
    key_str = key_str.upper().strip()
    keys    = load_keys()
    keys[key_str] = {
        "created_at":   datetime.now(timezone.utc).isoformat(),
        "expires_at":   expiry_from_spec(hours=hours, days=days),
        "blocked":      False,
        "used_by":      [],
        "created_by":   uid,
        "instances":    sel,
        "device_limit": device_limit,
        "custom":       True,   # mark so we know it was manually named
    }
    save_keys(keys)
    # Only update quota counter for sellers (not owner or admins)
    if not is_owner(uid) and not is_admin(uid):
        sellers = load_sellers()
        sd      = sellers.setdefault(str(uid), {})
        sd["keys_created"] = sd.get("keys_created", 0) + 1
        save_sellers(sellers)
    return key_str


# ── CUSTOM KEY FLOW ──────────────────────────────────────────────────────────────
# Step 1 → Select instances  (reuses instance_select_kb)
# Step 2 → Device limit      (reuses device_limit_kb)
# Step 3 → Duration          (reuses duration_kb)
# Step 4 → Type custom key   (new pending state: custom_key_name)
# All validation, expiry, quota logic = identical to normal key creation

@owner_or_admin_cb
async def cb_key_custom(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entry point — Custom Key: Step 1 select instances. Owner and Admins only."""
    q = update.callback_query; await q.answer()
    uid       = update.effective_user.id
    instances = [i for i in load_instances() if inst_has_patch(i) and i.get("enabled", True)]
    if not instances:
        await q.answer("No instances available yet.", show_alert=True); return
    # ── Reset ALL stale state from any previous key creation flow ──────────
    for _k in ("new_key_device_limit", "new_key_stock", "new_key_owner",
               "new_key_count", "pending"):
        ctx.user_data.pop(_k, None)
    # Mark flow as custom so shared steps know where to return
    ctx.user_data["key_flow"]      = "custom"
    ctx.user_data["new_key_insts"] = [i["id"] for i in instances]
    sel = ctx.user_data["new_key_insts"]
    await q.edit_message_text(
        f"{e('edit')} <b>Custom Key — Step 1: Select Instances</b>\n\n"
        f"Select which instances this key unlocks:\n"
        f"{e('info')} Each instance works for both <b>FF & FF MAX</b>.",
        parse_mode=ParseMode.HTML,
        reply_markup=instance_select_kb(instances, sel),
    )


@owner_or_admin_cb
async def cb_custom_key_go_dur(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Custom Key Step 2 — Device limit. Owner and Admins only."""
    await update.callback_query.answer()
    await _custom_key_go_dur_inner(update, ctx)


@owner_or_admin_cb
async def cb_custom_devlimit_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Custom Key Step 3 — Duration. Owner and Admins only."""
    q = update.callback_query; await q.answer()
    limit = int(q.data.replace("cdevlimit_", ""))
    ctx.user_data["new_key_device_limit"] = limit
    sel       = ctx.user_data.get("new_key_insts", [])
    instances = load_instances()
    names     = [next((i['label'] for i in instances if i["id"] == s), s) for s in sel]
    limit_txt = f"Max {limit} device(s)" if limit else "Unlimited"
    await q.edit_message_text(
        f"{e('key')} <b>Custom Key — Step 3: Duration</b>\n\n"
        f"{e('aim')} Instances: <b>{', '.join(names)}</b>\n"
        f"{e('phone')} Devices: <b>{limit_txt}</b>\n\n"
        f"Select validity period:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("1h",        callback_data="cdur_h1"),
             InlineKeyboardButton("6h",        callback_data="cdur_h6"),
             InlineKeyboardButton("12h",       callback_data="cdur_h12"),
             InlineKeyboardButton("24h",       callback_data="cdur_h24")],
            [InlineKeyboardButton("3 Days",    callback_data="cdur_d3"),
             InlineKeyboardButton("7 Days",    callback_data="cdur_d7"),
             InlineKeyboardButton("30 Days",   callback_data="cdur_d30"),
             InlineKeyboardButton("90 Days",   callback_data="cdur_d90")],
            [InlineKeyboardButton("Lifetime",  callback_data="cdur_d0"),
             InlineKeyboardButton("✏️ Custom", callback_data="cdur_custom")],
            [InlineKeyboardButton("Back",      callback_data="custom_key_go_dur")],
        ]),
    )


@owner_or_admin_cb
async def cb_custom_dur_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Custom Key Step 4 — ask user to type their custom key name. Owner and Admins only."""
    q = update.callback_query; await q.answer()
    uid  = update.effective_user.id
    spec = q.data.replace("cdur_", "")

    if spec == "custom":
        # Ask for custom duration text first, then key name
        ctx.user_data["pending"] = "custom_key_duration_then_name"
        sel       = ctx.user_data.get("new_key_insts", [])
        instances = load_instances()
        names     = [next((i['label'] for i in instances if i["id"] == s), s) for s in sel]
        device_limit = ctx.user_data.get("new_key_device_limit", 0)
        limit_txt = f"Max {device_limit} device(s)" if device_limit else "Unlimited"
        await q.edit_message_text(
            f"{e('timer')} <b>Custom Key — Custom Validity</b>\n\n"
            f"{e('aim')} Instances: <b>{', '.join(names)}</b>\n"
            f"{e('phone')} Devices: <b>{limit_txt}</b>\n\n"
            f"Send the validity duration. Examples:\n"
            f"  <code>2d 6h</code>  →  2 days 6 hours\n"
            f"  <code>1d</code>     →  1 day\n"
            f"  <code>12h</code>    →  12 hours\n"
            f"  <code>45m</code>    →  ~1 hour (rounds up)",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Back", callback_data="custom_key_go_dur")],
            ]),
        )
        return

    # Pre-set duration picked → now ask for key name
    hours = 0; days = 0
    if spec.startswith("h"):
        hours = int(spec[1:]); label = f"{hours}h"
    elif spec.startswith("d"):
        days = int(spec[1:]); label = f"{days}d" if days else "Lifetime"
    else:
        label = "Lifetime"

    ctx.user_data["custom_key_hours"] = hours
    ctx.user_data["custom_key_days"]  = days
    ctx.user_data["custom_key_label"] = label
    ctx.user_data["pending"]          = "custom_key_name"

    sel       = ctx.user_data.get("new_key_insts", [])
    instances = load_instances()
    names     = [next((i['label'] for i in instances if i["id"] == s), s) for s in sel]
    device_limit = ctx.user_data.get("new_key_device_limit", 0)
    limit_txt = f"Max {device_limit} device(s)" if device_limit else "Unlimited"

    await q.edit_message_text(
        f"{e('edit')} <b>Custom Key — Step 4: Type Your Key</b>\n\n"
        f"{e('aim')} Instances: <b>{', '.join(names)}</b>\n"
        f"{e('timer')} Validity: <b>{label}</b>\n"
        f"{e('phone')} Devices: <b>{limit_txt}</b>\n\n"
        f"Now type your custom key name.\n"
        f"Any text, any format — examples:\n"
        f"  <code>REGEDIT-VIP-2025</code>\n"
        f"  <code>RG-AHMAD-7DAYS</code>\n"
        f"  <code>MYKEY123</code>\n"
        f"  <code>RG-EXT-AABBCC</code>\n\n"
        f"{e('warning')} Key will be saved exactly as you type it (uppercased).\n"
        f"{e('warning')} Must be unique — duplicate keys are rejected.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Cancel", callback_data="menu_keys")],
        ]),
    )


@staff_only_cb
async def cb_dur_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid  = update.effective_user.id
    spec = q.data.replace("dur_", "")

    # ── Custom duration: ask user to type it ────────────────────────────────
    if spec == "custom":
        ctx.user_data["pending"] = "custom_duration"
        sel = ctx.user_data.get("new_key_insts", [])
        instances = load_instances()
        names = [next((i['label'] for i in instances if i["id"] == s), s) for s in sel]
        device_limit = ctx.user_data.get("new_key_device_limit", 0)
        limit_txt = f"Max {device_limit} device(s)" if device_limit else "Unlimited"
        await q.edit_message_text(
            f"{e('timer')} <b>Create Key — Custom Validity</b>\n\n"
            f"{e('aim')} Instances: <b>{', '.join(names)}</b>\n"
            f"{e('phone')} Devices: <b>{limit_txt}</b>\n\n"
            f"Send the custom validity duration. Examples:\n"
            f"  <code>2d 6h</code> → 2 days 6 hours\n"
            f"  <code>1d</code>    → 1 day\n"
            f"  <code>12h</code>   → 12 hours\n"
            f"  <code>3 days 4 hours</code>\n"
            f"  <code>45m</code>   → 45 minutes (rounds up to 1h)",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Back", callback_data="key_go_dur")],
            ]),
        )
        return

    # ── Pre-set durations ───────────────────────────────────────────────────
    hours = 0; days = 0
    if spec.startswith("h"):
        hours = int(spec[1:]); label = f"{hours}h"
    elif spec.startswith("d"):
        days = int(spec[1:]); label = f"{days}d" if days else "Lifetime"
    else:
        label = "Lifetime"

    if not is_owner(uid):
        sellers = load_sellers()
        sd      = sellers.get(str(uid), {})
        if sd.get("blocked"):
            await q.answer("Your account is blocked.", show_alert=True); return
        qmax = sd.get("quota", 0)
        if qmax and sd.get("keys_created", 0) >= qmax:
            await q.answer("Quota reached", show_alert=True); return

    sel = ctx.user_data.get("new_key_insts", [])
    if not sel:
        await q.answer("No instances selected", show_alert=True); return

    device_limit = ctx.user_data.pop("new_key_device_limit", 0)
    stock        = ctx.user_data.pop("new_key_stock", False)
    owner        = ctx.user_data.pop("new_key_owner", False)
    count        = ctx.user_data.pop("new_key_count", 1)
    ctx.user_data.pop("new_key_insts", None)

    instances = load_instances()
    names     = [next((i['label'] for i in instances if i["id"] == s), s) for s in sel]
    limit_txt = f"Max {device_limit} device(s)" if device_limit else "Unlimited"
    type_label = "Owner" if owner else ("Stock" if stock else "Regular")
    stock_note = "\n⏳ Timer starts when each key is first activated." if stock else ""

    if count == 1:
        key_str = _finalize_key(uid, hours, days, label, sel, device_limit,
                                stock=stock, owner=owner)
        await q.edit_message_text(
            f"{e('check')} <b>Key Created!</b>\n\n"
            f"{e('key')} <code>{key_str}</code>\n\n"
            f"{e('aim')} Instances: <b>{', '.join(names)}</b>\n"
            f"{e('timer')} Validity: <b>{label}</b>\n"
            f"{e('phone')} Devices: <b>{limit_txt}</b>\n"
            f"🔑 Type: <b>{type_label}</b>{stock_note}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Create Another", callback_data="key_create"),
                 InlineKeyboardButton("Back to Keys",   callback_data="menu_keys")],
            ]),
        )
    else:
        key_list = _finalize_keys_bulk(uid, count, hours, days, label, sel, device_limit,
                                       stock=stock, owner=owner)
        keys_text = "\n".join(f"<code>{k}</code>" for k in key_list)
        await q.edit_message_text(
            f"{e('check')} <b>{count} Keys Created!</b>\n\n"
            f"{e('aim')} Instances: <b>{', '.join(names)}</b>\n"
            f"{e('timer')} Validity: <b>{label}</b>\n"
            f"{e('phone')} Devices: <b>{limit_txt}</b>\n"
            f"🔑 Type: <b>{type_label}</b>{stock_note}\n\n"
            f"{keys_text}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Create More", callback_data="key_create"),
                 InlineKeyboardButton("Back to Keys", callback_data="menu_keys")],
            ]),
        )

@staff_only_cb
async def cb_key_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid  = update.effective_user.id
    keys = load_keys()
    if is_owner(uid):
        show = list(keys.items())
    elif is_admin(uid):
        sellers    = load_sellers()
        my_sellers = {s for s, sd in sellers.items() if str(sd.get("added_by")) == str(uid)}
        show = [(k, v) for k, v in keys.items()
                if str(v.get("created_by")) == str(uid)
                or str(v.get("created_by")) in my_sellers]
    else:
        show = [(k, v) for k, v in keys.items() if v.get("created_by") == uid]
    inst_map = {i["id"]: i["label"] for i in load_instances()}

    if not show:
        await q.edit_message_text(
            f"{e('info')} No keys found.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb("menu_keys"))
        return

    # Edit the current message as a header
    await q.edit_message_text(
        f"{e('key')} <b>Keys</b> ({len(show)})"
        + (f" — showing first 20" if len(show) > 20 else ""),
        parse_mode=ParseMode.HTML,
        reply_markup=back_kb("menu_keys"),
    )

    # Send each key as its own message with action buttons
    for kstr, kd in show[:20]:
        st    = key_status(kd)
        icon  = e("greencircle") if st == "active" else e("redcircle")
        exp   = format_expiry(kd.get("expires_at"))
        insts = [inst_map.get(i, i) for i in kd.get("instances", [])]
        dlimit = kd.get("device_limit", 0)
        dcount = len(kd.get("used_by", []))
        dev_txt = f"{dcount}/{dlimit}" if dlimit else f"{dcount}/∞"
        txt   = (
            f"{icon} <code>{kstr}</code>\n"
            f"{e('timer')} Expires: <b>{exp}</b>\n"
            f"{e('phone')} Devices: <b>{dev_txt}</b>\n"
            f"{e('aim')} Instances: {', '.join(insts) or 'none'}"
        )
        # Block button label depends on current state
        block_lbl = "Unblock" if kd.get("blocked") else "Block"
        block_cb  = f"mykey_unblock_{kstr}" if kd.get("blocked") else f"mykey_block_{kstr}"
        await q.message.reply_text(
            txt,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(block_lbl,       callback_data=block_cb),
                    InlineKeyboardButton("Delete",        callback_data=f"mykey_del_ask_{kstr}"),
                ],
                [
                    InlineKeyboardButton("Reset Devices", callback_data=f"mykey_resetdev_{kstr}"),
                ],
            ]),
        )

@owner_only_cb
async def cb_key_block_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["pending"] = "block_key"
    await q.edit_message_text(
        f"{e('lock')} Send the key to block:", parse_mode=ParseMode.HTML,
        reply_markup=back_kb("menu_keys"))

@owner_only_cb
async def cb_key_del_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["pending"] = "delete_key"
    await q.edit_message_text(
        f"{e('bin')} Send the key to delete:", parse_mode=ParseMode.HTML,
        reply_markup=back_kb("menu_keys"))

# ── IPA CONTROL ─────────────────────────────────────────────────────────────────
@owner_only_cb
async def cb_menu_ipa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    cfg     = load_cfg()
    enabled = cfg.get("ipa_enabled", True)
    await q.edit_message_text(
        f"{e('phone')} <b>IPA Control</b>\n\nDisabling rejects all key validations.",
        parse_mode=ParseMode.HTML,
        reply_markup=ipa_kb(enabled),
    )

@owner_only_cb
async def cb_ipa_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    cfg                = load_cfg()
    cfg["ipa_enabled"] = (q.data == "ipa_enable")
    save_cfg(cfg)
    icon = e("check") if cfg["ipa_enabled"] else e("stop")
    await q.edit_message_text(
        f"{icon} IPA {'enabled' if cfg['ipa_enabled'] else 'disabled'}.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("IPA Menu", callback_data="menu_ipa"),
             InlineKeyboardButton("Main",     callback_data="menu_main")],
        ]),
    )

# ── USERS ────────────────────────────────────────────────────────────────────────
@owner_only_cb
async def cb_menu_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    users = load_users()
    now   = datetime.now(timezone.utc)
    active_today = 0
    for u in users.values():
        ls = u.get("last_seen")
        if ls:
            try:
                dt = datetime.fromisoformat(ls)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if (now - dt).total_seconds() < 86400:
                    active_today += 1
            except Exception:
                pass
    txt = (
        f"{e('people')} <b>Users</b>\n\n"
        f"{e('greendot')} Active today: <b>{active_today}</b>\n"
        f"{e('person')} Total: <b>{len(users)}</b>"
    )
    await q.edit_message_text(txt, parse_mode=ParseMode.HTML,
                               reply_markup=users_kb())

@owner_only_cb
async def cb_users_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    users = load_users()
    if not users:
        await q.edit_message_text(f"{e('info')} No users yet.", parse_mode=ParseMode.HTML,
                                   reply_markup=back_kb("menu_users")); return
    lines = [f"{e('people')} <b>All Users</b>\n"]
    for uid_str, ud in list(users.items())[:30]:
        seen = ud.get("last_seen", "?")[:16]
        key  = ud.get("last_key", "?")[:15]
        lines.append(f"{e('person')} <code>{uid_str}</code> | {seen}\n    ↳ {key}")
    await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML,
                               reply_markup=back_kb("menu_users"))

@owner_only_cb
async def cb_users_active(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    users = load_users()
    now   = datetime.now(timezone.utc)
    active = []
    for uid_str, ud in users.items():
        ls = ud.get("last_seen")
        if ls:
            try:
                dt = datetime.fromisoformat(ls)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if (now - dt).total_seconds() < 86400:
                    active.append((uid_str, ud))
            except Exception:
                pass
    if not active:
        await q.edit_message_text(f"{e('info')} No active users today.", parse_mode=ParseMode.HTML,
                                   reply_markup=back_kb("menu_users")); return
    lines = [f"{e('greendot')} <b>Active Today</b> ({len(active)})\n"]
    for uid_str, ud in active[:30]:
        seen = ud.get("last_seen", "?")[:16]
        key  = ud.get("last_key", "?")[:15]
        lines.append(f"{e('person')} <code>{uid_str}</code> | {seen}\n    ↳ {key}")
    await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML,
                               reply_markup=back_kb("menu_users"))

# ── STATS ────────────────────────────────────────────────────────────────────────
@owner_only_cb
async def cb_menu_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    keys      = load_keys()
    users     = load_users()
    instances = load_instances()
    sellers   = load_sellers()
    cfg       = load_cfg()
    active    = sum(1 for v in keys.values() if key_status(v) == "active")
    blocked_k = sum(1 for v in keys.values() if v.get("blocked"))
    expired_k = sum(1 for v in keys.values() if key_status(v) == "expired")
    ff_inst   = sum(1 for i in instances if i.get("game","ff")=="ff" and inst_has_patch(i))
    max_inst  = sum(1 for i in instances if i.get("game","ff")=="ffmax" and inst_has_patch(i))
    txt = (
        f"{e('trophy')} <b>Stats</b>\n\n"
        f"{e('key')} Keys\n"
        f"  Active: {active} | Blocked: {blocked_k} | Expired: {expired_k} | Total: {len(keys)}\n\n"
        f"{e('aim')} Instances: {ff_inst} FF / {max_inst} FF MAX ready\n"
        f"{e('people')} Users: {len(users)}\n"
        f"{e('seller')} Sellers: {len(sellers)}\n"
        f"{e('phone')} IPA: {'ON' if cfg.get('ipa_enabled', True) else 'OFF'}\n"
        f"{e('calendar')} {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
    )
    await q.edit_message_text(
        txt, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Refresh", callback_data="menu_stats"),
             InlineKeyboardButton("Back",    callback_data="menu_main")],
        ]),
    )

# ── SELLERS ──────────────────────────────────────────────────────────────────────
@owner_or_admin_cb
async def cb_menu_sellers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query; await q.answer()
    uid = update.effective_user.id
    all_sellers = load_sellers()
    if is_owner(uid):
        visible = all_sellers
    else:
        # Admin sees only sellers they created
        visible = {s: sd for s, sd in all_sellers.items()
                   if str(sd.get("added_by")) == str(uid)}
    txt = (
        f"{e('seller')} <b>{'My ' if is_admin(uid) else ''}Sellers</b>\n\n"
        f"{e('info')} {len(visible)} seller(s)"
    )
    await q.edit_message_text(txt, parse_mode=ParseMode.HTML,
                               reply_markup=sellers_kb(visible, back_target="menu_main"))

@owner_or_admin_cb
async def cb_seller_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query; await q.answer()
    uid = update.effective_user.id
    if is_admin(uid):
        ad = load_admins().get(str(uid), {})
        if ad.get("blocked"):
            await q.answer("Your admin account is blocked.", show_alert=True); return
    ctx.user_data["pending"] = "seller_add_id"
    await q.edit_message_text(
        f"{e('plus')} <b>Add Seller</b>\n\nSend the seller's Telegram ID or @username:",
        parse_mode=ParseMode.HTML,
        reply_markup=back_kb("menu_sellers"),
    )

@owner_or_admin_cb
async def cb_seller_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query; await q.answer()
    uid = update.effective_user.id
    sid = q.data.replace("seller_del_", "")
    sellers = load_sellers()
    sd      = sellers.get(sid, {})
    # Admin can only remove sellers they created
    if is_admin(uid) and str(sd.get("added_by")) != str(uid):
        await q.answer("Not your seller", show_alert=True); return
    sellers.pop(sid, None)
    save_sellers(sellers)
    name = sd.get("username", sid) if sd else sid
    await q.edit_message_text(
        f"{e('bin')} Seller <b>{name}</b> removed.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_kb("menu_sellers"),
    )

@owner_or_admin_cb
async def cb_seller_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query; await q.answer()
    uid = update.effective_user.id
    sid = q.data.replace("seller_info_", "")
    sellers = load_sellers()
    sd      = sellers.get(sid, {})
    if not sd:
        await q.answer("Seller not found", show_alert=True); return
    # Admin can only view their own sellers
    if is_admin(uid) and str(sd.get("added_by")) != str(uid):
        await q.answer("Not your seller", show_alert=True); return
    name       = sd.get("username") or sid
    quota      = sd.get("quota", 0)
    used       = sd.get("keys_created", 0)
    added_at   = sd.get("added_at", "?")[:10]
    blocked    = sd.get("blocked", False)
    status_str = f"{e('redcircle')} Blocked" if blocked else f"{e('greencircle')} Active"
    txt = (
        f"{e('seller')} <b>Seller Info</b>\n\n"
        f"{e('person')} Name/ID: <code>{name}</code> (<code>{sid}</code>)\n"
        f"{e('key')} Keys Created: <b>{used}</b>\n"
        f"{e('moneybag')} Quota: <b>{quota if quota else 'Unlimited'}</b>\n"
        f"{e('shield')} Status: {status_str}\n"
        f"{e('calendar')} Added: {added_at}\n"
    )
    await q.edit_message_text(txt, parse_mode=ParseMode.HTML,
                               reply_markup=seller_info_kb(sid, blocked=blocked))

@owner_or_admin_cb
async def cb_seller_quota(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query; await q.answer()
    uid = update.effective_user.id
    sid = q.data.replace("seller_quota_", "")
    sellers = load_sellers()
    sd      = sellers.get(sid, {})
    if is_admin(uid) and str(sd.get("added_by")) != str(uid):
        await q.answer("Not your seller", show_alert=True); return
    ctx.user_data["pending"] = f"seller_set_quota_{sid}"
    await q.edit_message_text(
        f"{e('moneybag')} Send new quota for seller <code>{sid}</code>:\n"
        f"<i>0 = Unlimited</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=back_kb("menu_sellers"),
    )

@owner_or_admin_cb
async def cb_seller_reset_quota(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query; await q.answer()
    uid = update.effective_user.id
    sid = q.data.replace("seller_reset_quota_", "")
    sellers = load_sellers()
    sd      = sellers.get(sid, {})
    if is_admin(uid) and str(sd.get("added_by")) != str(uid):
        await q.answer("Not your seller", show_alert=True); return
    if sid in sellers:
        sellers[sid]["keys_created"] = 0
        save_sellers(sellers)
        await q.edit_message_text(
            f"{e('refresh')} Quota counter reset for seller <code>{sid}</code>.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb("menu_sellers"),
        )
    else:
        await q.answer("Seller not found", show_alert=True)

@owner_or_admin_cb
async def cb_seller_keys(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query; await q.answer()
    uid = update.effective_user.id
    sid = q.data.replace("seller_keys_", "")
    sellers = load_sellers()
    sd      = sellers.get(sid, {})
    if is_admin(uid) and str(sd.get("added_by")) != str(uid):
        await q.answer("Not your seller", show_alert=True); return
    name = sd.get("username") or sid
    keys = load_keys()
    seller_keys = [(k, v) for k, v in keys.items()
                   if str(v.get("created_by")) == sid or str(v.get("created_by")) == sd.get("username")]
    if not seller_keys:
        await q.edit_message_text(
            f"{e('info')} Seller <b>{name}</b> has no keys.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb(f"seller_info_{sid}"),
        ); return
    inst_map = {i["id"]: i["label"] for i in load_instances()}
    lines    = [f"{e('key')} <b>Keys by {name}</b> ({len(seller_keys)})\n"]
    for kstr, kd in seller_keys[:20]:
        st   = key_status(kd)
        icon = e("greencircle") if st == "active" else e("redcircle")
        exp  = format_expiry(kd.get("expires_at"))
        insts = [inst_map.get(i, i) for i in kd.get("instances", [])]
        lines.append(f"{icon} <code>{kstr}</code> | {exp}\n    ↳ {', '.join(insts) or 'none'}")
    if len(seller_keys) > 20:
        lines.append(f"\n…and {len(seller_keys) - 20} more.")
    await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML,
                               reply_markup=back_kb(f"seller_info_{sid}"))

@owner_or_admin_cb
async def cb_seller_block(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query; await q.answer()
    uid = update.effective_user.id
    sid = q.data.replace("seller_block_", "")
    sellers = load_sellers()
    if sid not in sellers:
        await q.answer("Seller not found", show_alert=True); return
    sd = sellers[sid]
    if is_admin(uid) and str(sd.get("added_by")) != str(uid):
        await q.answer("Not your seller", show_alert=True); return
    current = sellers[sid].get("blocked", False)
    sellers[sid]["blocked"] = not current
    save_sellers(sellers)

    # Refresh seller info page so block/unblock button updates
    sd         = sellers[sid]
    name       = sd.get("username") or sid
    quota      = sd.get("quota", 0)
    used       = sd.get("keys_created", 0)
    added_at   = sd.get("added_at", "?")[:10]
    blocked    = sd["blocked"]
    status_str = f"{e('redcircle')} Blocked" if blocked else f"{e('greencircle')} Active"
    icon_hdr   = e("lock") if blocked else e("unlock")
    txt = (
        f"{e('seller')} <b>Seller Info</b>\n\n"
        f"{e('person')} Name/ID: <code>{name}</code> (<code>{sid}</code>)\n"
        f"{e('key')} Keys Created: <b>{used}</b>\n"
        f"{e('moneybag')} Quota: <b>{quota if quota else 'Unlimited'}</b>\n"
        f"{e('shield')} Status: {status_str}\n"
        f"{e('calendar')} Added: {added_at}\n\n"
        f"{icon_hdr} Seller {'blocked' if blocked else 'unblocked'} successfully."
    )
    await q.edit_message_text(txt, parse_mode=ParseMode.HTML,
                               reply_markup=seller_info_kb(sid, blocked=blocked))

@owner_or_admin_cb
async def cb_seller_delkeys(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Delete ALL keys created by a seller (confirmation step)."""
    q   = update.callback_query; await q.answer()
    uid = update.effective_user.id
    sid = q.data.replace("seller_delkeys_", "")
    sellers = load_sellers()
    sd      = sellers.get(sid, {})
    if is_admin(uid) and str(sd.get("added_by")) != str(uid):
        await q.answer("Not your seller", show_alert=True); return
    name = sd.get("username") or sid
    keys = load_keys()
    count = sum(1 for v in keys.values()
                if str(v.get("created_by")) == sid
                or str(v.get("created_by")) == sd.get("username"))
    await q.edit_message_text(
        f"{e('warning')} <b>Delete ALL keys by {name}?</b>\n\n"
        f"This will delete <b>{count}</b> key(s). This cannot be undone.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Yes, Delete All", callback_data=f"seller_delkeys_confirm_{sid}")],
            [InlineKeyboardButton("Cancel",            callback_data=f"seller_info_{sid}")],
        ]),
    )

@owner_or_admin_cb
async def cb_seller_delkeys_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Actually delete ALL keys by a seller."""
    q   = update.callback_query; await q.answer()
    uid = update.effective_user.id
    sid = q.data.replace("seller_delkeys_confirm_", "")
    sellers = load_sellers()
    sd      = sellers.get(sid, {})
    if is_admin(uid) and str(sd.get("added_by")) != str(uid):
        await q.answer("Not your seller", show_alert=True); return
    name   = sd.get("username") or sid
    keys   = load_keys()
    before = len(keys)
    keys   = {k: v for k, v in keys.items()
               if str(v.get("created_by")) != sid
               and str(v.get("created_by")) != sd.get("username")}
    deleted = before - len(keys)
    save_keys(keys)
    await q.edit_message_text(
        f"{e('bin')} Deleted <b>{deleted}</b> key(s) by seller <code>{name}</code>.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_kb(f"seller_info_{sid}"),
    )

@owner_or_admin_cb
async def cb_seller_delunused(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Delete expired + blocked (unused) keys created by a seller (confirmation step)."""
    q   = update.callback_query; await q.answer()
    uid = update.effective_user.id
    sid = q.data.replace("seller_delunused_", "")
    sellers = load_sellers()
    sd      = sellers.get(sid, {})
    if is_admin(uid) and str(sd.get("added_by")) != str(uid):
        await q.answer("Not your seller", show_alert=True); return
    name   = sd.get("username") or sid
    keys   = load_keys()
    unused = [(k, v) for k, v in keys.items()
               if (str(v.get("created_by")) == sid
                   or str(v.get("created_by")) == sd.get("username"))
               and key_status(v) in ("expired", "blocked")]
    await q.edit_message_text(
        f"{e('warning')} <b>Delete unused keys by {name}?</b>\n\n"
        f"Unused = expired or blocked keys.\n"
        f"Found <b>{len(unused)}</b> unused key(s). Continue?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Yes, Delete Unused", callback_data=f"seller_delunused_confirm_{sid}")],
            [InlineKeyboardButton("Cancel",               callback_data=f"seller_info_{sid}")],
        ]),
    )

@owner_or_admin_cb
async def cb_seller_delunused_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Actually delete expired + blocked keys by a seller."""
    q   = update.callback_query; await q.answer()
    uid = update.effective_user.id
    sid = q.data.replace("seller_delunused_confirm_", "")
    sellers = load_sellers()
    sd      = sellers.get(sid, {})
    if is_admin(uid) and str(sd.get("added_by")) != str(uid):
        await q.answer("Not your seller", show_alert=True); return
    name   = sd.get("username") or sid
    keys   = load_keys()
    before = len(keys)
    keys   = {k: v for k, v in keys.items()
               if not (
                   (str(v.get("created_by")) == sid
                    or str(v.get("created_by")) == sd.get("username"))
                   and key_status(v) in ("expired", "blocked")
               )}
    deleted = before - len(keys)
    save_keys(keys)
    await q.edit_message_text(
        f"{e('check')} Deleted <b>{deleted}</b> unused key(s) by seller <code>{name}</code>.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_kb(f"seller_info_{sid}"),
    )

# ── KEY INLINE ACTIONS (seller + admin + owner) ────────────────────────────────
def _can_manage_key(uid: int, key_data: dict) -> bool:
    """Check if uid has permission to manage this key."""
    if is_owner(uid):
        return True
    creator = key_data.get("created_by")
    if str(creator) == str(uid):
        return True
    if is_admin(uid):
        # Admin can manage keys created by their own sellers
        sellers = load_sellers()
        my_sellers = {s for s, sd in sellers.items() if str(sd.get("added_by")) == str(uid)}
        if str(creator) in my_sellers:
            return True
    return False

@staff_only_cb
async def cb_mykey_block(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Block a key — seller/admin can only block keys they have permission over."""
    q   = update.callback_query; await q.answer()
    uid = update.effective_user.id
    kstr = q.data.replace("mykey_block_", "")
    keys = load_keys()
    if kstr not in keys:
        await q.answer("Key not found", show_alert=True); return
    if not _can_manage_key(uid, keys[kstr]):
        await q.answer("Not your key", show_alert=True); return
    keys[kstr]["blocked"] = True
    save_keys(keys)
    await q.edit_message_text(
        q.message.text + f"\n\n{e('lock')} <b>Blocked</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Unblock", callback_data=f"mykey_unblock_{kstr}"),
            InlineKeyboardButton("Delete",  callback_data=f"mykey_del_ask_{kstr}"),
        ]]),
    )

@staff_only_cb
async def cb_mykey_unblock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Unblock a key."""
    q   = update.callback_query; await q.answer()
    uid = update.effective_user.id
    kstr = q.data.replace("mykey_unblock_", "")
    keys = load_keys()
    if kstr not in keys:
        await q.answer("Key not found", show_alert=True); return
    if not _can_manage_key(uid, keys[kstr]):
        await q.answer("Not your key", show_alert=True); return
    keys[kstr]["blocked"] = False
    save_keys(keys)
    await q.edit_message_text(
        q.message.text + f"\n\n{e('unlock')} <b>Unblocked</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Block",  callback_data=f"mykey_block_{kstr}"),
            InlineKeyboardButton("Delete", callback_data=f"mykey_del_ask_{kstr}"),
        ]]),
    )

@staff_only_cb
async def cb_mykey_del_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Ask confirmation before deleting a key."""
    q    = update.callback_query; await q.answer()
    uid  = update.effective_user.id
    kstr = q.data.replace("mykey_del_ask_", "")
    keys = load_keys()
    if kstr not in keys:
        await q.answer("Key not found", show_alert=True); return
    if not _can_manage_key(uid, keys[kstr]):
        await q.answer("Not your key", show_alert=True); return
    await q.edit_message_reply_markup(
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Confirm Delete", callback_data=f"mykey_del_confirm_{kstr}"),
            InlineKeyboardButton("Cancel",         callback_data=f"mykey_del_cancel_{kstr}"),
        ]]),
    )

@staff_only_cb
async def cb_mykey_del_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Actually delete the key."""
    q    = update.callback_query; await q.answer()
    uid  = update.effective_user.id
    kstr = q.data.replace("mykey_del_confirm_", "")
    keys = load_keys()
    if kstr not in keys:
        await q.answer("Key not found", show_alert=True); return
    if not _can_manage_key(uid, keys[kstr]):
        await q.answer("Not your key", show_alert=True); return
    del keys[kstr]
    save_keys(keys)
    await q.edit_message_text(
        f"{e('bin')} Key deleted: <code>{kstr}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=None,
    )

@staff_only_cb
async def cb_mykey_del_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cancel delete — restore original block/delete/reset buttons."""
    q    = update.callback_query; await q.answer()
    uid  = update.effective_user.id
    kstr = q.data.replace("mykey_del_cancel_", "")
    keys = load_keys()
    if kstr not in keys:
        await q.answer("Key not found", show_alert=True); return
    block_lbl = "Unblock" if keys[kstr].get("blocked") else "Block"
    block_cb  = f"mykey_unblock_{kstr}" if keys[kstr].get("blocked") else f"mykey_block_{kstr}"
    await q.edit_message_reply_markup(
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(block_lbl, callback_data=block_cb),
                InlineKeyboardButton("Delete",  callback_data=f"mykey_del_ask_{kstr}"),
            ],
            [
                InlineKeyboardButton("Reset Devices", callback_data=f"mykey_resetdev_{kstr}"),
            ],
        ]),
    )

@staff_only_cb
async def cb_mykey_resetdev(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Reset device list for a key — clears used_by so all devices can log in fresh."""
    q    = update.callback_query; await q.answer()
    uid  = update.effective_user.id
    kstr = q.data.replace("mykey_resetdev_", "")
    keys = load_keys()
    if kstr not in keys:
        await q.answer("Key not found", show_alert=True); return
    if not _can_manage_key(uid, keys[kstr]):
        await q.answer("Not your key", show_alert=True); return
    old_count = len(keys[kstr].get("used_by", []))
    keys[kstr]["used_by"] = []
    save_keys(keys)
    dlimit = keys[kstr].get("device_limit", 0)
    dev_txt = f"0/{dlimit}" if dlimit else "0/∞"
    block_lbl = "Unblock" if keys[kstr].get("blocked") else "Block"
    block_cb  = f"mykey_unblock_{kstr}" if keys[kstr].get("blocked") else f"mykey_block_{kstr}"
    # Update the message text to reflect new device count
    try:
        exp   = format_expiry(keys[kstr].get("expires_at"))
        inst_map = {i["id"]: i["label"] for i in load_instances()}
        insts = [inst_map.get(i, i) for i in keys[kstr].get("instances", [])]
        st    = key_status(keys[kstr])
        icon  = e("greencircle") if st == "active" else e("redcircle")
        new_txt = (
            f"{icon} <code>{kstr}</code>\n"
            f"{e('timer')} Expires: <b>{exp}</b>\n"
            f"{e('phone')} Devices: <b>{dev_txt}</b>\n"
            f"{e('aim')} Instances: {', '.join(insts) or 'none'}\n\n"
            f"{e('unlock')} <b>Devices reset</b> — cleared {old_count} device(s)"
        )
        await q.edit_message_text(
            new_txt,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(block_lbl,       callback_data=block_cb),
                    InlineKeyboardButton("Delete",        callback_data=f"mykey_del_ask_{kstr}"),
                ],
                [
                    InlineKeyboardButton("Reset Devices", callback_data=f"mykey_resetdev_{kstr}"),
                ],
            ]),
        )
    except Exception:
        await q.answer(f"Devices reset! Cleared {old_count} device(s).", show_alert=True)

# ── ADMIN PANEL (Owner only) ──────────────────────────────────────────────────────
@owner_only_cb
async def cb_menu_admins(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    admins = load_admins()
    txt = (
        f"{e('shield')} <b>Admins</b>\n\n"
        f"{e('info')} {len(admins)} admin(s) registered\n\n"
        f"Admins can manage their own sellers and keys.\n"
        f"They cannot access IPA, Instances, Patches, or Stats."
    )
    await q.edit_message_text(txt, parse_mode=ParseMode.HTML,
                               reply_markup=admins_kb(admins))

@owner_only_cb
async def cb_admin_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["pending"] = "admin_add_id"
    await q.edit_message_text(
        f"{e('plus')} <b>Add Admin</b>\n\nSend the admin's Telegram ID or @username:",
        parse_mode=ParseMode.HTML,
        reply_markup=back_kb("menu_admins"),
    )

@owner_only_cb
async def cb_admin_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query; await q.answer()
    aid = q.data.replace("admin_del_", "")
    admins = load_admins()
    ad     = admins.pop(aid, None)
    save_admins(admins)
    name = ad.get("username", aid) if ad else aid
    await q.edit_message_text(
        f"{e('bin')} Admin <b>{name}</b> removed.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_kb("menu_admins"),
    )

@owner_only_cb
async def cb_admin_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query; await q.answer()
    aid = q.data.replace("admin_info_", "")
    admins = load_admins()
    ad     = admins.get(aid, {})
    if not ad:
        await q.answer("Admin not found", show_alert=True); return
    name     = ad.get("username") or aid
    added_at = ad.get("added_at", "?")[:10]
    blocked  = ad.get("blocked", False)
    # Count their sellers
    sellers     = load_sellers()
    my_sellers  = [s for s, sd in sellers.items() if str(sd.get("added_by")) == aid]
    # Count their keys
    keys        = load_keys()
    my_seller_set = set(my_sellers)
    my_keys = [k for k, kd in keys.items()
                if str(kd.get("created_by")) == aid
                or str(kd.get("created_by")) in my_seller_set]
    status_str = f"{e('redcircle')} Blocked" if blocked else f"{e('greencircle')} Active"
    txt = (
        f"{e('shield')} <b>Admin Info</b>\n\n"
        f"{e('person')} Name/ID: <code>{name}</code> (<code>{aid}</code>)\n"
        f"{e('seller')} Sellers: <b>{len(my_sellers)}</b>\n"
        f"{e('key')} Keys (self+sellers): <b>{len(my_keys)}</b>\n"
        f"{e('shield')} Status: {status_str}\n"
        f"{e('calendar')} Added: {added_at}\n"
    )
    await q.edit_message_text(txt, parse_mode=ParseMode.HTML,
                               reply_markup=admin_info_kb(aid, blocked=blocked))

@owner_only_cb
async def cb_admin_block(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query; await q.answer()
    aid = q.data.replace("admin_block_", "")
    admins = load_admins()
    if aid not in admins:
        await q.answer("Admin not found", show_alert=True); return
    admins[aid]["blocked"] = not admins[aid].get("blocked", False)
    save_admins(admins)
    blocked    = admins[aid]["blocked"]
    icon       = e("lock") if blocked else e("unlock")
    status_str = "blocked" if blocked else "unblocked"
    # Refresh info page
    ad       = admins[aid]
    name     = ad.get("username") or aid
    added_at = ad.get("added_at", "?")[:10]
    sellers  = load_sellers()
    my_sellers = [s for s, sd in sellers.items() if str(sd.get("added_by")) == aid]
    keys     = load_keys()
    my_keys  = [k for k, kd in keys.items() if str(kd.get("created_by")) == aid
                or str(kd.get("created_by")) in set(my_sellers)]
    status_disp = f"{e('redcircle')} Blocked" if blocked else f"{e('greencircle')} Active"
    txt = (
        f"{e('shield')} <b>Admin Info</b>\n\n"
        f"{e('person')} Name/ID: <code>{name}</code> (<code>{aid}</code>)\n"
        f"{e('seller')} Sellers: <b>{len(my_sellers)}</b>\n"
        f"{e('key')} Keys: <b>{len(my_keys)}</b>\n"
        f"{e('shield')} Status: {status_disp}\n"
        f"{e('calendar')} Added: {added_at}\n\n"
        f"{icon} Admin {status_str} successfully."
    )
    await q.edit_message_text(txt, parse_mode=ParseMode.HTML,
                               reply_markup=admin_info_kb(aid, blocked=blocked))

@owner_only_cb
async def cb_admin_sellers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Owner views all sellers of a specific admin."""
    q   = update.callback_query; await q.answer()
    aid = q.data.replace("admin_sellers_", "")
    admins  = load_admins()
    ad      = admins.get(aid, {})
    name    = ad.get("username") or aid
    sellers = load_sellers()
    visible = {s: sd for s, sd in sellers.items() if str(sd.get("added_by")) == aid}
    if not visible:
        await q.edit_message_text(
            f"{e('info')} Admin <b>{name}</b> has no sellers.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb(f"admin_info_{aid}")); return
    lines = [f"{e('seller')} <b>Sellers of {name}</b> ({len(visible)})\n"]
    for sid, sd in list(visible.items())[:20]:
        sname   = sd.get("username") or sid
        blocked = sd.get("blocked", False)
        used    = sd.get("keys_created", 0)
        quota   = sd.get("quota", 0)
        icon    = e("redcircle") if blocked else e("greencircle")
        lines.append(f"{icon} <code>{sid}</code> {sname} | {used}/{quota if quota else '∞'} keys")
    await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML,
                               reply_markup=back_kb(f"admin_info_{aid}"))

# ── BROADCAST ────────────────────────────────────────────────────────────────────
@owner_only_cb
async def cb_menu_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["pending"] = "broadcast_msg"
    await q.edit_message_text(
        f"{e('broadcast')} <b>Broadcast</b>\n\n"
        f"Send the message to broadcast to all users:\n"
        f"{e('info')} Runs in background — bot stays online.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_kb("menu_main"),
    )

async def _do_broadcast(bot, msg: str, chat_id: int):
    users  = load_users()
    sent   = failed = 0
    for uid_str in users:
        try:
            await bot.send_message(
                int(uid_str),
                f"{e('broadcast')} {msg}",
                parse_mode=ParseMode.HTML,
            )
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    try:
        await bot.send_message(
            chat_id,
            f"{e('check')} Broadcast done!\n"
            f"{e('greencircle')} Sent: <b>{sent}</b>\n"
            f"{e('redcircle')} Failed: <b>{failed}</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

# ── MESSAGE HANDLER ──────────────────────────────────────────────────────────────
async def msg_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    username = update.effective_user.username
    _migrate_seller_to_uid(uid, username)
    _migrate_admin_to_uid(uid, username)
    if not is_staff(uid):
        return
    pending = ctx.user_data.get("pending", "")
    text    = update.message.text.strip()

    # ── Instance: name ────────────────────────────────────────────────────────
    if pending == "inst_create_name":
        name  = text.strip()
        # Use ONE consistent ID format: lowercase with underscores for spaces
        iid   = name.lower().replace(" ", "_")
        label = name.upper()
        instances = load_instances()
        if any(i["id"] == iid for i in instances):
            await update.message.reply_text(
                f"{e('warning')} Instance <code>{iid}</code> already exists. Try a different name.",
                parse_mode=ParseMode.HTML)
            return
        icon = DEFAULT_ICONS.get(iid, "scope")
        instances.append({
            "id":         iid,
            "name":       label,
            "label":      label,
            "icon":       icon,
            "game":       "both",   # works for FF and FF MAX
            "patch_file": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        save_instances(instances)
        ctx.user_data.pop("pending", None)
        await update.message.reply_text(
            f"{e('check')} Instance <b>{label}</b> created!\n"
            f"{e('gamepad')} Works for: <b>FF & FF MAX</b>\n\n"
            f"Now upload the <code>.mxff</code> file (3105PATCH format):",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Upload .mxff now", callback_data=f"inst_info_{iid}")],
                [InlineKeyboardButton("Instances", callback_data="menu_instances")],
            ]),
        )

    # ── Instance: icon ────────────────────────────────────────────────────────
    elif pending.startswith("inst_set_icon_"):
        iid       = pending.replace("inst_set_icon_", "")
        instances = load_instances()
        for i in instances:
            if i["id"] == iid:
                i["icon"] = text.strip(); break
        save_instances(instances)
        ctx.user_data.pop("pending", None)
        await update.message.reply_text(
            f"{e('check')} Icon updated to <code>{text.strip()}</code>.",
            parse_mode=ParseMode.HTML)

    # ── Key: block / delete ───────────────────────────────────────────────────
    elif pending in ("block_key", "delete_key"):
        key  = text.upper()
        keys = load_keys()
        if key not in keys:
            await update.message.reply_text(f"{e('cross')} Key not found."); return
        if pending == "block_key":
            keys[key]["blocked"] = True
            save_keys(keys)
            await update.message.reply_text(
                f"{e('lock')} Blocked: <code>{key}</code>", parse_mode=ParseMode.HTML)
        else:
            del keys[key]
            save_keys(keys)
            await update.message.reply_text(
                f"{e('bin')} Deleted: <code>{key}</code>", parse_mode=ParseMode.HTML)
        ctx.user_data.pop("pending", None)

    # ── Seller: add ───────────────────────────────────────────────────────────
    elif pending == "seller_add_id":
        ctx.user_data["seller_raw"] = text
        ctx.user_data["pending"]    = "seller_add_quota"
        await update.message.reply_text(
            f"{e('moneybag')} Set key quota (0 = Unlimited):",
            parse_mode=ParseMode.HTML)

    elif pending == "seller_add_quota":
        raw = ctx.user_data.get("seller_raw", "")
        try:
            quota = int(text)
        except ValueError:
            await update.message.reply_text(f"{e('cross')} Invalid number."); return
        sid = raw.lstrip("@") if raw.startswith("@") else raw
        sellers = load_sellers()
        sellers[sid] = {
            "username":     raw,
            "quota":        quota,
            "keys_created": 0,
            "blocked":      False,
            "added_at":     datetime.now(timezone.utc).isoformat(),
            "added_by":     uid,   # could be owner UID or admin UID
        }
        save_sellers(sellers)
        ctx.user_data.pop("pending", None)
        ctx.user_data.pop("seller_raw", None)
        await update.message.reply_text(
            f"{e('check')} Seller added!\n\n"
            f"{e('person')} ID: <code>{sid}</code>\n"
            f"{e('moneybag')} Quota: <b>{quota if quota else 'Unlimited'}</b>\n\n"
            f"They can now use /start in this bot.",
            parse_mode=ParseMode.HTML)

    # ── Admin: add ────────────────────────────────────────────────────────────
    elif pending == "admin_add_id":
        if not is_owner(uid):
            ctx.user_data.pop("pending", None); return
        raw = text.strip()
        aid = raw.lstrip("@") if raw.startswith("@") else raw
        admins = load_admins()
        if aid in admins:
            await update.message.reply_text(
                f"{e('warning')} Admin <code>{aid}</code> already exists.",
                parse_mode=ParseMode.HTML); return
        admins[aid] = {
            "username": raw,
            "blocked":  False,
            "added_at": datetime.now(timezone.utc).isoformat(),
            "added_by": uid,
        }
        save_admins(admins)
        ctx.user_data.pop("pending", None)
        await update.message.reply_text(
            f"{e('check')} Admin added!\n\n"
            f"{e('person')} ID: <code>{aid}</code>\n\n"
            f"They can now use /start in this bot as an Admin.",
            parse_mode=ParseMode.HTML)

    elif pending.startswith("seller_set_quota_"):
        sid = pending.replace("seller_set_quota_", "")
        try:
            quota = int(text)
        except ValueError:
            await update.message.reply_text(f"{e('cross')} Invalid number."); return
        sellers = load_sellers()
        sd      = sellers.get(sid, {})
        # Admin can only set quota for their own sellers
        if is_admin(uid) and str(sd.get("added_by")) != str(uid):
            await update.message.reply_text(
                f"{e('lock')} Not your seller.", parse_mode=ParseMode.HTML); return
        if sid in sellers:
            sellers[sid]["quota"] = quota
            save_sellers(sellers)
            await update.message.reply_text(
                f"{e('check')} Quota updated to <b>{quota if quota else 'Unlimited'}</b>.",
                parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(f"{e('cross')} Seller not found.")
        ctx.user_data.pop("pending", None)

    # ── Key: custom validity duration ────────────────────────────────────────
    elif pending == "custom_duration":
        parsed = parse_custom_duration(text)
        if not parsed:
            await update.message.reply_text(
                f"{e('cross')} Could not parse duration.\n\n"
                f"Use formats like:\n"
                f"  <code>2d 6h</code>  →  2 days 6 hours\n"
                f"  <code>1d</code>     →  1 day\n"
                f"  <code>12h</code>    →  12 hours\n"
                f"  <code>45m</code>    →  ~1 hour (rounds up)",
                parse_mode=ParseMode.HTML)
            return

        hours, days, label = parsed

        if not is_owner(uid):
            sellers = load_sellers()
            sd      = sellers.get(str(uid), {})
            if sd.get("blocked"):
                await update.message.reply_text(
                    f"{e('lock')} Your account is blocked.", parse_mode=ParseMode.HTML)
                ctx.user_data.pop("pending", None); return
            qmax = sd.get("quota", 0)
            if qmax and sd.get("keys_created", 0) >= qmax:
                await update.message.reply_text(
                    f"{e('cross')} Quota reached ({qmax}).", parse_mode=ParseMode.HTML)
                ctx.user_data.pop("pending", None); return

        sel = ctx.user_data.get("new_key_insts", [])
        if not sel:
            await update.message.reply_text(
                f"{e('warning')} No instances selected — please start key creation again.",
                parse_mode=ParseMode.HTML)
            ctx.user_data.pop("pending", None); return

        device_limit = ctx.user_data.pop("new_key_device_limit", 0)
        ctx.user_data.pop("new_key_insts", None)
        ctx.user_data.pop("pending", None)

        key_str   = _finalize_key(uid, hours, days, label, sel, device_limit)
        instances = load_instances()
        names     = [next((i['label'] for i in instances if i["id"] == s), s) for s in sel]
        limit_txt = f"Max {device_limit} device(s)" if device_limit else "Unlimited"
        await update.message.reply_text(
            f"{e('check')} <b>Key Created!</b>\n\n"
            f"{e('key')} <code>{key_str}</code>\n\n"
            f"{e('aim')} Instances: <b>{', '.join(names)}</b>\n"
            f"{e('timer')} Validity: <b>{label}</b>\n"
            f"{e('phone')} Devices: <b>{limit_txt}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Create Another", callback_data="key_create"),
                 InlineKeyboardButton("Back to Keys",   callback_data="menu_keys")],
            ]),
        )

    # ── Custom Key: type key name (after preset duration picked) ─────────────
    # Custom Key is Owner/Admin only — sellers are blocked at the callback level
    elif pending == "custom_key_name":
        if not is_owner(uid) and not is_admin(uid):
            ctx.user_data.pop("pending", None)
            await update.message.reply_text(
                f"{e('lock')} <b>Access Denied</b>\n\nCustom Key is for Owner and Admins only.",
                parse_mode=ParseMode.HTML)
            return

        raw_key = text.strip().upper()
        if not raw_key:
            await update.message.reply_text(
                f"{e('cross')} Key cannot be empty. Send a valid key name."); return

        # Check duplicate
        if raw_key in load_keys():
            await update.message.reply_text(
                f"{e('warning')} Key <code>{raw_key}</code> already exists.\n"
                f"Send a different key name.",
                parse_mode=ParseMode.HTML); return

        sel          = ctx.user_data.pop("new_key_insts", [])
        hours        = ctx.user_data.pop("custom_key_hours", 0)
        days         = ctx.user_data.pop("custom_key_days", 0)
        label        = ctx.user_data.pop("custom_key_label", "Lifetime")
        device_limit = ctx.user_data.pop("new_key_device_limit", 0)
        ctx.user_data.pop("pending", None)
        ctx.user_data.pop("key_flow", None)

        if not sel:
            await update.message.reply_text(
                f"{e('warning')} No instances — please start over.",
                parse_mode=ParseMode.HTML); return

        key_str   = _finalize_key_custom(uid, raw_key, hours, days, label, sel, device_limit)
        instances = load_instances()
        names     = [next((i['label'] for i in instances if i["id"] == s), s) for s in sel]
        limit_txt = f"Max {device_limit} device(s)" if device_limit else "Unlimited"
        await update.message.reply_text(
            f"{e('check')} <b>Custom Key Created!</b>\n\n"
            f"{e('key')} <code>{key_str}</code>\n\n"
            f"{e('aim')} Instances: <b>{', '.join(names)}</b>\n"
            f"{e('timer')} Validity: <b>{label}</b>\n"
            f"{e('phone')} Devices: <b>{limit_txt}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Create Another Custom", callback_data="key_custom"),
                 InlineKeyboardButton("Back to Keys",         callback_data="menu_keys")],
            ]),
        )

    # ── Custom Key: custom validity duration, then ask key name ───────────────
    elif pending == "custom_key_duration_then_name":
        parsed = parse_custom_duration(text)
        if not parsed:
            await update.message.reply_text(
                f"{e('cross')} Could not parse duration.\n\n"
                f"Use formats like:\n"
                f"  <code>2d 6h</code>  →  2 days 6 hours\n"
                f"  <code>1d</code>     →  1 day\n"
                f"  <code>12h</code>    →  12 hours\n"
                f"  <code>45m</code>    →  ~1 hour (rounds up)",
                parse_mode=ParseMode.HTML)
            return

        hours, days, label = parsed

        # Custom Key is Owner/Admin only — block any seller who somehow reached here
        if not is_owner(uid) and not is_admin(uid):
            ctx.user_data.pop("pending", None)
            await update.message.reply_text(
                f"{e('lock')} <b>Access Denied</b>\n\nCustom Key is for Owner and Admins only.",
                parse_mode=ParseMode.HTML)
            return

        ctx.user_data["custom_key_hours"] = hours
        ctx.user_data["custom_key_days"]  = days
        ctx.user_data["custom_key_label"] = label
        ctx.user_data["pending"]          = "custom_key_name"

        sel          = ctx.user_data.get("new_key_insts", [])
        instances    = load_instances()
        names        = [next((i['label'] for i in instances if i["id"] == s), s) for s in sel]
        device_limit = ctx.user_data.get("new_key_device_limit", 0)
        limit_txt    = f"Max {device_limit} device(s)" if device_limit else "Unlimited"
        await update.message.reply_text(
            f"{e('edit')} <b>Custom Key — Step 4: Type Your Key</b>\n\n"
            f"{e('aim')} Instances: <b>{', '.join(names)}</b>\n"
            f"{e('timer')} Validity: <b>{label}</b>\n"
            f"{e('phone')} Devices: <b>{limit_txt}</b>\n\n"
            f"Now type your custom key name.\n"
            f"Any text, any format — examples:\n"
            f"  <code>REGEDIT-VIP-2025</code>\n"
            f"  <code>RG-AHMAD-7DAYS</code>\n"
            f"  <code>MYKEY123</code>\n"
            f"  <code>RG-EXT-AABBCC</code>\n\n"
            f"{e('warning')} Key will be saved exactly as you type it (uppercased).\n"
            f"{e('warning')} Must be unique — duplicate keys are rejected.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Cancel", callback_data="menu_keys")],
            ]),
        )

    # ── Broadcast ─────────────────────────────────────────────────────────────
    elif pending == "broadcast_msg":
        ctx.user_data.pop("pending", None)
        users_count = len(load_users())
        await update.message.reply_text(
            f"{e('broadcast')} Broadcasting to <b>{users_count}</b> users in background…\n"
            f"{e('info')} You will get a report when done.",
            parse_mode=ParseMode.HTML,
        )
        asyncio.create_task(
            _do_broadcast(ctx.bot, text, update.effective_chat.id)
        )

# ── FILE HANDLER (Large File Upload — Telegram stream, no 20MB limit) ─────────
async def _stream_download_and_save(bot, file_id: str, dest_path: Path,
                                     chat_id: int, label: str, iid: str,
                                     file_size: int, game: str = "both"):
    """
    Stream download via Telegram Bot API — supports files up to 2 GB.
    Uses get_file() + download_to_drive() which streams directly.
    No 20 MB limit — works for large .mxff files.

    game: "ff" | "ffmax" | "both"  — determines which patch_file_* field is updated.
    """
    try:
        tg_file = await bot.get_file(
            file_id,
            read_timeout=60,
            write_timeout=60,
            connect_timeout=30,
            pool_timeout=30,
        )
        await tg_file.download_to_drive(
            dest_path,
            read_timeout=600,
            write_timeout=600,
            connect_timeout=60,
            pool_timeout=600,
        )

        instances = load_instances()
        fname     = dest_path.name
        # Auto-detect rule count from the downloaded .mxff file
        rule_count = _detect_mxff_rule_count(dest_path)
        for i in instances:
            if i["id"] == iid:
                i["file_id"] = file_id
                if game == "ff":
                    i["patch_file_ff"]      = fname
                    i["file_size_ff"]       = file_size
                    i["rule_count_ff"]      = rule_count
                elif game == "ffmax":
                    i["patch_file_ffmax"]   = fname
                    i["file_size_ffmax"]    = file_size
                    i["rule_count_ffmax"]   = rule_count
                else:  # "both" — legacy single file
                    i["patch_file"]         = fname
                    i["file_size"]          = file_size
                    i["rule_count"]         = rule_count
                # Auto-set multi_inject based on rule count:
                # multi_inject = True  → 2+ rules → no backup, delete on off
                # multi_inject = False → 1 rule   → backup+restore on off
                # Check ALL game files to decide: if ANY has multi-rule → multi_inject
                total_rules = max(
                    i.get("rule_count_ff",    i.get("rule_count", 1)),
                    i.get("rule_count_ffmax", i.get("rule_count", 1)),
                )
                i["multi_inject"] = (total_rules > 1)
                break
        save_instances(instances)

        size_mb   = file_size / (1024 * 1024)
        game_lbl  = "Free Fire" if game == "ff" else ("Free Fire MAX" if game == "ffmax" else "FF & FF MAX")
        await bot.send_message(
            chat_id,
            f"{e('check')} <b>.mxff uploaded for {label}!</b>\n\n"
            f"File: <code>{fname}</code> ({size_mb:.1f} MB)\n"
            f"{e('gamepad')} Game: <b>{game_lbl}</b>\n"
            f"Instance is now ready for {game_lbl}.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("View Instance", callback_data=f"inst_info_{iid}")],
                [InlineKeyboardButton("Instances",     callback_data="menu_instances")],
            ]),
        )
    except Exception as ex:
        log.exception("Background file download failed")
        try:
            await bot.send_message(
                chat_id,
                f"{e('cross')} Upload failed: <code>{type(ex).__name__}: {ex}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

async def file_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    if not is_owner(uid):
        return
    pending = ctx.user_data.get("pending", "")
    doc     = update.message.document
    if not doc:
        return

    # ── Per-game upload: FF ────────────────────────────────────────────────────
    if pending.startswith("inst_upload_file_ff_"):
        iid         = pending.replace("inst_upload_file_ff_", "")
        game        = "ff"
        game_label  = "Free Fire"
        fname_lower = doc.file_name.lower()
        if not fname_lower.endswith(".mxff"):
            await update.message.reply_text(
                f"{e('warning')} Only <code>.mxff</code> files accepted (3105PATCH format).",
                parse_mode=ParseMode.HTML); return
        inst = find_instance(iid)
        if not inst:
            await update.message.reply_text(
                f"{e('cross')} Instance not found.", parse_mode=ParseMode.HTML); return
        # Remove old FF patch file if exists
        old_ff = inst.get("patch_file_ff")
        if old_ff:
            fp = PATCH_DIR / old_ff
            fp.unlink(missing_ok=True)
        fname     = f"{iid}_ff.mxff"
        dest_path = PATCH_DIR / fname
        ctx.user_data.pop("pending", None)
        size_mb = doc.file_size / (1024 * 1024)
        await update.message.reply_text(
            f"{e('upload')} <b>Uploading FF .mxff in background…</b>\n\n"
            f"File: <code>{doc.file_name}</code> ({size_mb:.1f} MB)\n"
            f"{e('gamepad')} Game: <b>{game_label}</b>\n"
            f"{e('info')} Bot stays online. You will be notified when done.",
            parse_mode=ParseMode.HTML,
        )
        asyncio.create_task(
            _stream_download_and_save(
                ctx.bot, doc.file_id, dest_path,
                update.effective_chat.id, inst["label"],
                iid, doc.file_size, game=game,
            )
        )

    # ── Per-game upload: FFMax ─────────────────────────────────────────────────
    elif pending.startswith("inst_upload_file_ffmax_"):
        iid         = pending.replace("inst_upload_file_ffmax_", "")
        game        = "ffmax"
        game_label  = "Free Fire MAX"
        fname_lower = doc.file_name.lower()
        if not fname_lower.endswith(".mxff"):
            await update.message.reply_text(
                f"{e('warning')} Only <code>.mxff</code> files accepted (3105PATCH format).",
                parse_mode=ParseMode.HTML); return
        inst = find_instance(iid)
        if not inst:
            await update.message.reply_text(
                f"{e('cross')} Instance not found.", parse_mode=ParseMode.HTML); return
        # Remove old FFMax patch file if exists
        old_ffmax = inst.get("patch_file_ffmax")
        if old_ffmax:
            fp = PATCH_DIR / old_ffmax
            fp.unlink(missing_ok=True)
        fname     = f"{iid}_ffmax.mxff"
        dest_path = PATCH_DIR / fname
        ctx.user_data.pop("pending", None)
        size_mb = doc.file_size / (1024 * 1024)
        await update.message.reply_text(
            f"{e('upload')} <b>Uploading FFMax .mxff in background…</b>\n\n"
            f"File: <code>{doc.file_name}</code> ({size_mb:.1f} MB)\n"
            f"{e('gamepad')} Game: <b>{game_label}</b>\n"
            f"{e('info')} Bot stays online. You will be notified when done.",
            parse_mode=ParseMode.HTML,
        )
        asyncio.create_task(
            _stream_download_and_save(
                ctx.bot, doc.file_id, dest_path,
                update.effective_chat.id, inst["label"],
                iid, doc.file_size, game=game,
            )
        )

    # ── Legacy single-file upload (backward compat) ────────────────────────────
    elif pending.startswith("inst_upload_file_"):
        iid         = pending.replace("inst_upload_file_", "")
        fname_lower = doc.file_name.lower()
        if not fname_lower.endswith(".mxff"):
            await update.message.reply_text(
                f"{e('warning')} Only <code>.mxff</code> files accepted (3105PATCH format).",
                parse_mode=ParseMode.HTML); return
        inst = find_instance(iid)
        if not inst:
            await update.message.reply_text(
                f"{e('cross')} Instance not found.", parse_mode=ParseMode.HTML); return
        if inst.get("patch_file"):
            old = PATCH_DIR / inst["patch_file"]
            old.unlink(missing_ok=True)
        fname     = f"{iid}.mxff"
        dest_path = PATCH_DIR / fname
        ctx.user_data.pop("pending", None)
        size_mb = doc.file_size / (1024 * 1024)
        await update.message.reply_text(
            f"{e('upload')} <b>Uploading in background…</b>\n\n"
            f"File: <code>{doc.file_name}</code> ({size_mb:.1f} MB)\n"
            f"{e('gamepad')} Works for: <b>FF & FF MAX</b>\n"
            f"{e('info')} Bot stays online. You will be notified when done.",
            parse_mode=ParseMode.HTML,
        )
        asyncio.create_task(
            _stream_download_and_save(
                ctx.bot, doc.file_id, dest_path,
                update.effective_chat.id, inst["label"],
                iid, doc.file_size, game="both",
            )
        )



# ── OWNER COMMANDS ───────────────────────────────────────────────────────────────
@owner_only
async def cmd_newkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    spec  = ctx.args[0].lower() if ctx.args else "30d"
    hours = 0; days = 0
    if spec.endswith("h"):
        hours = int(spec[:-1]); label = f"{hours}h"
    elif spec.endswith("d"):
        days = int(spec[:-1]); label = f"{days}d" if days else "Lifetime"
    else:
        days = int(spec) if spec.isdigit() else 30; label = f"{days}d"
    key_str   = gen_key()
    keys      = load_keys()
    instances = [i["id"] for i in load_instances() if inst_has_patch(i)]
    keys[key_str] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expiry_from_spec(hours=hours, days=days),
        "blocked":    False,
        "used_by":    [],
        "created_by": OWNER_ID,
        "instances":  instances,
    }
    save_keys(keys)
    await update.message.reply_text(
        f"{e('key')} New key ({label}):\n\n<code>{key_str}</code>",
        parse_mode=ParseMode.HTML)

async def cmd_deletekey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_staff(uid):
        await update.message.reply_text(
            f"{e('lock')} <b>Access Denied</b>", parse_mode=ParseMode.HTML); return
    if not ctx.args:
        await update.message.reply_text(
            f"{e('info')} Usage: <code>/deletekey KEY</code>", parse_mode=ParseMode.HTML); return
    key  = " ".join(ctx.args).strip().upper()
    keys = load_keys()
    if key not in keys:
        await update.message.reply_text(f"{e('cross')} Key not found."); return
    # Seller can only delete their own keys
    if not is_owner(uid) and keys[key].get("created_by") != uid:
        await update.message.reply_text(
            f"{e('lock')} You can only delete your own keys.", parse_mode=ParseMode.HTML); return
    del keys[key]
    save_keys(keys)
    await update.message.reply_text(
        f"{e('bin')} Deleted: <code>{key}</code>", parse_mode=ParseMode.HTML)

async def cmd_blockkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_staff(uid):
        await update.message.reply_text(
            f"{e('lock')} <b>Access Denied</b>", parse_mode=ParseMode.HTML); return
    if not ctx.args:
        await update.message.reply_text(
            f"{e('info')} Usage: <code>/blockkey KEY</code>", parse_mode=ParseMode.HTML); return
    key  = " ".join(ctx.args).strip().upper()
    keys = load_keys()
    if key not in keys:
        await update.message.reply_text(f"{e('cross')} Key not found."); return
    if not is_owner(uid) and keys[key].get("created_by") != uid:
        await update.message.reply_text(
            f"{e('lock')} You can only block your own keys.", parse_mode=ParseMode.HTML); return
    keys[key]["blocked"] = True
    save_keys(keys)
    await update.message.reply_text(
        f"{e('lock')} Blocked: <code>{key}</code>", parse_mode=ParseMode.HTML)

async def cmd_unblockkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_staff(uid):
        await update.message.reply_text(
            f"{e('lock')} <b>Access Denied</b>", parse_mode=ParseMode.HTML); return
    if not ctx.args:
        await update.message.reply_text(
            f"{e('info')} Usage: <code>/unblockkey KEY</code>", parse_mode=ParseMode.HTML); return
    key  = " ".join(ctx.args).strip().upper()
    keys = load_keys()
    if key not in keys:
        await update.message.reply_text(f"{e('cross')} Key not found."); return
    if not is_owner(uid) and keys[key].get("created_by") != uid:
        await update.message.reply_text(
            f"{e('lock')} You can only unblock your own keys.", parse_mode=ParseMode.HTML); return
    keys[key]["blocked"] = False
    save_keys(keys)
    await update.message.reply_text(
        f"{e('unlock')} Unblocked: <code>{key}</code>", parse_mode=ParseMode.HTML)

async def cmd_resetdevices(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Clear used_by list for a key so all devices can re-register."""
    uid = update.effective_user.id
    if not is_staff(uid):
        await update.message.reply_text(
            f"{e('lock')} <b>Access Denied</b>", parse_mode=ParseMode.HTML); return
    if not ctx.args:
        await update.message.reply_text(
            f"{e('info')} Usage: <code>/resetdevices KEY</code>\n"
            f"Clears device list — all devices can log in fresh.",
            parse_mode=ParseMode.HTML); return
    key  = " ".join(ctx.args).strip().upper()
    keys = load_keys()
    if key not in keys:
        await update.message.reply_text(f"{e('cross')} Key not found."); return
    if not is_owner(uid) and keys[key].get("created_by") != uid:
        await update.message.reply_text(
            f"{e('lock')} You can only reset your own keys.", parse_mode=ParseMode.HTML); return
    old_count = len(keys[key].get("used_by", []))
    keys[key]["used_by"] = []
    save_keys(keys)
    await update.message.reply_text(
        f"{e('unlock')} <b>Devices reset</b> for <code>{key}</code>\n"
        f"Cleared {old_count} device(s). Key is now fresh.",
        parse_mode=ParseMode.HTML)


@owner_only
async def cmd_ipaon(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg = load_cfg(); cfg["ipa_enabled"] = True; save_cfg(cfg)
    await update.message.reply_text(f"{e('check')} IPA Enabled", parse_mode=ParseMode.HTML)

@owner_only
async def cmd_ipaoff(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg = load_cfg(); cfg["ipa_enabled"] = False; save_cfg(cfg)
    await update.message.reply_text(f"{e('stop')} IPA Disabled", parse_mode=ParseMode.HTML)

@owner_only
async def cmd_exportkeys(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keys = load_keys()
    buf  = io.BytesIO(json.dumps(keys, indent=2, default=str).encode())
    buf.name = f"fougo_keys_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    await update.message.reply_document(
        buf, caption=f"{e('key')} Keys exported", parse_mode=ParseMode.HTML)

@owner_only
async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /broadcast MESSAGE"); return
    msg         = " ".join(ctx.args)
    users_count = len(load_users())
    await update.message.reply_text(
        f"{e('broadcast')} Broadcasting to <b>{users_count}</b> users in background…",
        parse_mode=ParseMode.HTML)
    asyncio.create_task(
        _do_broadcast(ctx.bot, msg, update.effective_chat.id)
    )




# ══════════════════════════════════════════════════════════════════════════════
# GIST PERSISTENCE — ডেটা সবসময় Gist-এ save থাকবে
# ══════════════════════════════════════════════════════════════════════════════

async def gist_load() -> bool:
    """
    GitHub Gist থেকে সব ডেটা load করো।
    Bot চালু হওয়ার সময় একবার call হয়।
    """
    if not GH_TOKEN or not GIST_ID:
        log.warning("Gist: GH_TOKEN বা GIST_ID নেই — local data ব্যবহার হবে")
        return False
    try:
        async with ClientSession(timeout=ClientTimeout(total=30)) as session:
            async with session.get(GIST_API, headers=_gh_headers()) as r:
                if r.status != 200:
                    log.warning(f"Gist load failed: HTTP {r.status}")
                    return False
                data = await r.json()
                content = data.get("files", {}).get(GIST_FILE, {}).get("content", "{}")
                all_data = json.loads(content)

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PATCH_DIR.mkdir(parents=True, exist_ok=True)

        if "keys"      in all_data: _save(KEYS_FILE,      all_data["keys"])
        if "users"     in all_data: _save(USERS_FILE,     all_data["users"])
        if "config"    in all_data: _save(CFG_FILE,       all_data["config"])
        if "sellers"   in all_data: _save(SELLERS_FILE,   all_data["sellers"])
        if "instances" in all_data: _save(INSTANCES_FILE, all_data["instances"])
        if "admins"    in all_data: _save(ADMINS_FILE,    all_data["admins"])

        log.info("✅ Gist থেকে ডেটা load হয়েছে")
        return True
    except Exception as ex:
        log.warning(f"Gist load error: {ex}")
        return False


async def gist_save() -> bool:
    """
    সব ডেটা GitHub Gist-এ save করো।
    যেকোনো পরিবর্তনের পর call হয়।
    """
    if not GH_TOKEN or not GIST_ID:
        return False
    try:
        all_data = {
            "keys":      load_keys(),
            "users":     load_users(),
            "config":    load_cfg(),
            "sellers":   _load(SELLERS_FILE,   {}),
            "instances": load_instances(),
            "admins":    _load(ADMINS_FILE,    {}),
            "saved_at":  datetime.now(timezone.utc).isoformat(),
        }
        payload = {
            "files": {
                GIST_FILE: {
                    "content": json.dumps(all_data, indent=2, default=str)
                }
            }
        }
        async with ClientSession(timeout=ClientTimeout(total=30)) as session:
            async with session.patch(GIST_API, headers=_gh_headers(), json=payload) as r:
                if r.status != 200:
                    log.warning(f"Gist save failed: HTTP {r.status}")
                    return False
        log.info("✅ Gist-এ ডেটা save হয়েছে")
        return True
    except Exception as ex:
        log.warning(f"Gist save error: {ex}")
        return False


def _schedule_gist_save():
    """Safely schedule a Gist save — works from both sync and async context."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(gist_save())
    except RuntimeError:
        pass  # No running event loop — skip, will sync next time


# ── Override save functions to auto-sync to Gist ─────────────────────────────
def save_keys(d):
    _save(KEYS_FILE, d)
    _schedule_gist_save()

def save_users(d):
    _save(USERS_FILE, d)
    _schedule_gist_save()

def save_cfg(d):
    _save(CFG_FILE, d)
    _schedule_gist_save()

def save_instances(d):
    _save(INSTANCES_FILE, d)
    _schedule_gist_save()

# ══════════════════════════════════════════════════════════════════════════════
# GITHUB ACTIONS INTEGRATION
# ══════════════════════════════════════════════════════════════════════════════

def _gh_headers() -> dict:
    """Return auth headers for the GitHub REST API."""
    return {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def _gh_configured() -> bool:
    """Return True if the minimum GitHub env vars are set."""
    return bool(GH_TOKEN and GH_REPO_OWNER and GH_REPO_NAME)

async def gh_trigger_build(ref: str | None = None) -> dict:
    """
    Trigger a workflow_dispatch event for GH_WORKFLOW_ID.
    Returns {} on success, {"error": str} on failure.
    """
    if not _gh_configured():
        return {"error": "GitHub credentials not configured.\nSet GH_TOKEN, GH_REPO_OWNER, GH_REPO_NAME."}
    branch = ref or GH_BRANCH
    url = (
        f"{_GH_API}/repos/{GH_REPO_OWNER}/{GH_REPO_NAME}"
        f"/actions/workflows/{GH_WORKFLOW_ID}/dispatches"
    )
    try:
        async with ClientSession(timeout=ClientTimeout(total=30)) as session:
            async with session.post(url, headers=_gh_headers(), json={"ref": branch}) as r:
                if r.status == 204:
                    return {}
                text = await r.text()
                return {"error": f"GitHub API {r.status}: {text[:300]}"}
    except Exception as ex:
        return {"error": f"Request failed: {ex}"}

async def gh_get_latest_run() -> dict | None:
    """
    Fetch the most recent run for GH_WORKFLOW_ID.
    Returns the run dict or None on error / no runs.
    """
    if not _gh_configured():
        return None
    url = (
        f"{_GH_API}/repos/{GH_REPO_OWNER}/{GH_REPO_NAME}"
        f"/actions/workflows/{GH_WORKFLOW_ID}/runs"
    )
    try:
        async with ClientSession(timeout=ClientTimeout(total=30)) as session:
            async with session.get(url, headers=_gh_headers(),
                                   params={"per_page": "1", "branch": GH_BRANCH}) as r:
                if r.status != 200:
                    log.warning(f"gh_get_latest_run: HTTP {r.status}")
                    return None
                data = await r.json()
                runs = data.get("workflow_runs", [])
                return runs[0] if runs else None
    except Exception as ex:
        log.warning(f"gh_get_latest_run error: {ex}")
        return None

async def gh_get_run(run_id: int) -> dict | None:
    """Fetch a single workflow run by its integer ID."""
    if not _gh_configured():
        return None
    url = (
        f"{_GH_API}/repos/{GH_REPO_OWNER}/{GH_REPO_NAME}"
        f"/actions/runs/{run_id}"
    )
    try:
        async with ClientSession(timeout=ClientTimeout(total=30)) as session:
            async with session.get(url, headers=_gh_headers()) as r:
                if r.status != 200:
                    log.warning(f"gh_get_run {run_id}: HTTP {r.status}")
                    return None
                return await r.json()
    except Exception as ex:
        log.warning(f"gh_get_run error: {ex}")
        return None

async def gh_get_artifact(run_id: int) -> dict | None:
    """
    Fetch the first artifact for a run that matches _GH_ARTIFACT.
    Falls back to the first artifact if the name isn't found.
    Returns the artifact dict or None.
    """
    if not _gh_configured():
        return None
    url = (
        f"{_GH_API}/repos/{GH_REPO_OWNER}/{GH_REPO_NAME}"
        f"/actions/runs/{run_id}/artifacts"
    )
    try:
        async with ClientSession(timeout=ClientTimeout(total=30)) as session:
            async with session.get(url, headers=_gh_headers()) as r:
                if r.status != 200:
                    return None
                data  = await r.json()
                items = data.get("artifacts", [])
                # Prefer the named IPA artifact, fall back to first
                for a in items:
                    if a.get("name") == _GH_ARTIFACT:
                        return a
                return items[0] if items else None
    except Exception as ex:
        log.warning(f"gh_get_artifact error: {ex}")
        return None

async def gh_download_artifact(artifact_id: int) -> bytes | None:
    """
    Download and return the raw bytes of an artifact zip.
    GitHub responds with a redirect — aiohttp follows it automatically
    when the Authorization header is forwarded, which requires allow_redirects=True.
    Returns None on any failure.
    """
    if not _gh_configured():
        return None
    url = (
        f"{_GH_API}/repos/{GH_REPO_OWNER}/{GH_REPO_NAME}"
        f"/actions/artifacts/{artifact_id}/zip"
    )
    try:
        # GitHub returns a 302 → S3 pre-signed URL.
        # We follow the redirect but must NOT send the Authorization header
        # to the S3 URL (it causes a 400). So we do a two-step request.
        async with ClientSession(timeout=ClientTimeout(total=30)) as session:
            async with session.get(url, headers=_gh_headers(), allow_redirects=False) as r:
                if r.status not in (302, 301, 307, 308):
                    log.warning(f"gh_download_artifact: expected redirect, got {r.status}")
                    return None
                redirect_url = r.headers.get("Location")
        if not redirect_url:
            return None
        # Download from the S3 pre-signed URL — no auth header needed
        async with ClientSession(timeout=ClientTimeout(total=600)) as dl_session:
            async with dl_session.get(redirect_url) as r2:
                if r2.status != 200:
                    log.warning(f"gh_download_artifact S3: HTTP {r2.status}")
                    return None
                return await r2.read()
    except Exception as ex:
        log.warning(f"gh_download_artifact error: {ex}")
        return None

# ── Build status helpers ─────────────────────────────────────────────────────

def _run_composite_status(run: dict) -> str:
    """
    Map GitHub run status+conclusion to a simple string:
      pending | running | completed_success | completed_failure |
      completed_cancelled | completed_<other> | unknown
    """
    status     = run.get("status", "")
    conclusion = run.get("conclusion") or ""
    if status == "queued":
        return "pending"
    if status == "in_progress":
        return "running"
    if status == "completed":
        return f"completed_{conclusion}" if conclusion else "completed_unknown"
    return "unknown"

def _build_status_text(run: dict) -> str:
    """Format a workflow run as an HTML status message."""
    composite  = _run_composite_status(run)
    run_id     = run.get("id", "?")
    run_number = run.get("run_number", "?")
    created_at = (run.get("created_at") or "?")[:19].replace("T", " ")
    html_url   = run.get("html_url", "#")
    conclusion = run.get("conclusion") or ""

    icon_map = {
        "pending":             ("⏳", "Pending"),
        "running":             ("⚙️",  "Running"),
        "completed_success":   ("✅", "Success"),
        "completed_failure":   ("❌", "Failed"),
        "completed_timed_out": ("⌛", "Timed Out"),
        "completed_cancelled": ("⏹", "Cancelled"),
    }
    icon, label = icon_map.get(composite, ("❓", f"{run.get('status','?')}/{conclusion}"))

    return (
        f"🔨 <b>Build IPA — FOUGO EXTERNAL</b>\n\n"
        f"{icon} Status: <b>{label}</b>\n"
        f"🔢 Run: <b>#{run_number}</b>  |  ID: <code>{run_id}</code>\n"
        f"📅 Started: <b>{created_at} UTC</b>\n"
        f"🔗 <a href=\"{html_url}\">View on GitHub Actions</a>"
    )

# ── Background polling task ───────────────────────────────────────────────────

async def _poll_build_until_done(chat_id: int, run_id: int) -> None:
    """
    Poll the run every 30 s until it completes, then notify the owner.
    Caps at 90 polls (45 minutes) to avoid runaway tasks.
    """
    for _ in range(90):
        await asyncio.sleep(30)
        run = await gh_get_run(run_id)
        if run is None:
            continue
        composite = _run_composite_status(run)
        if not composite.startswith("completed"):
            continue

        # Build finished — send notification
        if composite == "completed_success":
            extra = "\n\n📥 Press <b>Download IPA</b> below to fetch the artifact."
        else:
            extra = f"\n\nConclusion: <b>{run.get('conclusion', '?')}</b>"

        txt = _build_status_text(run) + extra
        kb  = build_main_kb(run_id=run_id, run_status=composite)
        try:
            from telegram import Bot as _Bot
            bot = _Bot(token=BOT_TOKEN)
            await bot.send_message(
                chat_id, txt,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
                disable_web_page_preview=True,
            )
        except Exception as err:
            log.warning(f"_poll_build_until_done notify failed: {err}")
        break

# ── Build IPA handlers ────────────────────────────────────────────────────────

@owner_only_cb
async def cb_menu_build(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show the Build IPA panel with the most recent run status."""
    q = update.callback_query
    await q.answer()

    if not _gh_configured():
        await q.edit_message_text(
            f"{e('warning')} <b>GitHub Actions not configured</b>\n\n"
            f"Add these environment variables (or GitHub Secrets) to your bot:\n"
            f"• <code>GH_TOKEN</code> — Personal Access Token\n"
            f"  (needs <b>actions:write</b> + <b>contents:read</b> scopes)\n"
            f"• <code>GH_REPO_OWNER</code> — your GitHub username or org\n"
            f"• <code>GH_REPO_NAME</code> — your repository name\n"
            f"• <code>GH_WORKFLOW_ID</code> — workflow file (default: <code>build.yml</code>)\n"
            f"• <code>GH_BRANCH</code> — branch to build (default: <code>main</code>)",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb("menu_main"),
        )
        return

    run = await gh_get_latest_run()
    if run is None:
        txt = (
            f"🔨 <b>Build IPA — FOUGO EXTERNAL</b>\n\n"
            f"No builds found yet.\n"
            f"Press <b>Trigger Build</b> to start your first build."
        )
        run_id    = None
        composite = None
    else:
        run_id    = run.get("id")
        composite = _run_composite_status(run)
        txt       = _build_status_text(run)

    await q.edit_message_text(
        txt,
        parse_mode=ParseMode.HTML,
        reply_markup=build_main_kb(run_id=run_id, run_status=composite),
        disable_web_page_preview=True,
    )

@owner_only_cb
async def cb_build_trigger(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Dispatch the build.yml workflow and start background polling."""
    q = update.callback_query
    await q.answer("Triggering build…")

    if not _gh_configured():
        await q.answer("GitHub not configured — check env vars.", show_alert=True)
        return

    # Immediate feedback
    await q.edit_message_text(
        f"⏳ <b>Dispatching build…</b>\n\nSending workflow_dispatch to GitHub Actions.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_kb("menu_build"),
    )

    result = await gh_trigger_build()
    if "error" in result:
        await q.edit_message_text(
            f"{e('cross')} <b>Trigger failed</b>\n\n<code>{result['error']}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=build_main_kb(),
        )
        return

    # Give GitHub 4 seconds to register the new run
    await asyncio.sleep(4)
    run = await gh_get_latest_run()

    if run:
        run_id    = run.get("id")
        composite = _run_composite_status(run)
        txt       = f"🚀 <b>Build triggered!</b>\n\n" + _build_status_text(run)
    else:
        run_id    = None
        composite = None
        txt       = (
            f"🚀 <b>Build dispatched!</b>\n\n"
            f"GitHub is registering the run. "
            f"Press <b>Refresh Status</b> in a few seconds."
        )

    await q.edit_message_text(
        txt,
        parse_mode=ParseMode.HTML,
        reply_markup=build_main_kb(run_id=run_id, run_status=composite),
        disable_web_page_preview=True,
    )

    # Kick off background poller so the owner gets a push notification when done
    if run_id:
        asyncio.create_task(_poll_build_until_done(update.effective_chat.id, run_id))

@owner_only_cb
async def cb_build_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Manually refresh the status of an in-flight or completed build."""
    q = update.callback_query
    await q.answer("Refreshing…")

    raw   = q.data.replace("build_status_", "")
    try:
        run_id = int(raw)
    except ValueError:
        await q.answer("Invalid run ID.", show_alert=True)
        return

    run = await gh_get_run(run_id)
    if run is None:
        await q.answer("Could not fetch status from GitHub.", show_alert=True)
        return

    composite = _run_composite_status(run)
    txt       = _build_status_text(run)
    kb        = build_main_kb(run_id=run_id, run_status=composite)

    try:
        await q.edit_message_text(
            txt,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception:
        pass  # Telegram raises if message text is unchanged — safe to ignore

@owner_only_cb
async def cb_build_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Download the IPA artifact from GitHub and re-upload it to the owner's chat.
    GitHub serves a redirect to an S3 pre-signed URL; we follow it and stream
    the zip to Telegram as a Document message.
    """
    q = update.callback_query
    await q.answer("Fetching IPA from GitHub…")

    raw = q.data.replace("build_dl_", "")
    try:
        run_id = int(raw)
    except ValueError:
        await q.answer("Invalid run ID.", show_alert=True)
        return

    chat_id = update.effective_chat.id

    await q.edit_message_text(
        f"📥 <b>Downloading IPA…</b>\n\n"
        f"Fetching artifact <code>{_GH_ARTIFACT}</code> from GitHub.\n"
        f"This may take a moment for large files.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_kb("menu_build"),
    )

    # Step 1 — find the artifact record
    artifact = await gh_get_artifact(run_id)
    if artifact is None:
        await q.bot.send_message(
            chat_id,
            f"{e('cross')} <b>Artifact not found</b>\n\n"
            f"No artifact named <code>{_GH_ARTIFACT}</code> found for run "
            f"<code>{run_id}</code>.\n\n"
            f"The artifact may have expired (GitHub keeps them 90 days by default).",
            parse_mode=ParseMode.HTML,
            reply_markup=build_main_kb(run_id=run_id, run_status="completed_success"),
        )
        return

    artifact_id   = artifact.get("id")
    artifact_name = artifact.get("name", _GH_ARTIFACT)
    size_bytes    = artifact.get("size_in_bytes", 0)
    size_mb       = size_bytes / (1024 * 1024)

    # Step 2 — download bytes
    data = await gh_download_artifact(artifact_id)
    if data is None:
        await q.bot.send_message(
            chat_id,
            f"{e('cross')} <b>Download failed</b>\n\n"
            f"Could not download <code>{artifact_name}</code> (ID: {artifact_id}).\n"
            f"Check that the artifact has not expired and GH_TOKEN is valid.",
            parse_mode=ParseMode.HTML,
            reply_markup=build_main_kb(run_id=run_id, run_status="completed_success"),
        )
        return

    # Step 3 — send to Telegram
    # GitHub artifacts are always zips; the zip contains FOUGO_EXTERNAL.ipa
    bio      = io.BytesIO(data)
    bio.name = f"{artifact_name}.zip"

    await q.bot.send_document(
        chat_id,
        document=bio,
        filename=f"{artifact_name}.zip",
        caption=(
            f"📦 <b>{artifact_name}.zip</b>\n"
            f"📏 Size: {size_mb:.1f} MB\n"
            f"🔢 Run: <code>{run_id}</code>\n\n"
            f"{e('info')} Unzip to extract <code>FOUGO_EXTERNAL.ipa</code>"
        ),
        parse_mode=ParseMode.HTML,
    )

    # Restore the build panel after the file is sent
    run = await gh_get_run(run_id)
    if run:
        composite = _run_composite_status(run)
        txt       = _build_status_text(run)
        kb        = build_main_kb(run_id=run_id, run_status=composite)
        await q.bot.send_message(
            chat_id, txt,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
            disable_web_page_preview=True,
        )

# ── NOOP ─────────────────────────────────────────────────────────────────────────
async def cb_noop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()

# ── API ──────────────────────────────────────────────────────────────────────────
async def run_api():
    # ── Validate server (port 10020) ──────────────────────────────────────────
    # IPA calls: POST http://103.211.207.101:10020/api/validate
    validate_app = web.Application()
    validate_app.router.add_post("/api/validate", api_validate)
    # Also support /validate without /api prefix for backward compat
    validate_app.router.add_post("/validate",     api_validate)

    validate_runner = web.AppRunner(validate_app)
    await validate_runner.setup()
    validate_site = web.TCPSite(validate_runner, "0.0.0.0", API_PORT)
    await validate_site.start()
    log.info(f"Validate API listening on port {API_PORT}  (/api/validate)")

    # ── Patch server (port 8080) ───────────────────────────────────────────────
    # IPA calls: GET http://103.211.207.101:8080/api/patch/<patchId>
    patch_app = web.Application()
    patch_app.router.add_get("/api/patch/{instance_id}",  patch_file)
    # Inject tracking endpoints
    patch_app.router.add_post("/inject/confirm",           api_inject_confirm)
    patch_app.router.add_post("/inject/restore",           api_inject_restore)
    # Force-cleanup: IPA calls after deleting injected file (fallback when restore fails)
    patch_app.router.add_post("/inject/cleanup",           api_inject_cleanup)
    # Rule count report: IPA calls this after first inject to set actual rule count
    patch_app.router.add_post("/inject/report_rules",      api_report_rules)
    # Patch building endpoints (Onarchy — owner key only)
    patch_app.router.add_post("/patch/build",              api_patch_build)
    patch_app.router.add_get( "/patch/templates",          api_patch_templates)

    patch_runner = web.AppRunner(patch_app)
    await patch_runner.setup()
    patch_site = web.TCPSite(patch_runner, "0.0.0.0", PATCH_PORT)
    await patch_site.start()
    log.info(f"Patch server  listening on port {PATCH_PORT} (/api/patch/<id>)")

async def api_validate(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"valid": False, "error": "bad request"}, status=400)

    cfg = load_cfg()
    if not cfg.get("ipa_enabled", True):
        return web.json_response({"valid": False, "blocked": True, "error": "IPA disabled"})

    key    = body.get("key", "").strip().upper()
    game   = body.get("game", "ff")
    # Accept device ID from header OR body; strip "unknown" / empty — treat those as no device
    dev_id = (request.headers.get("X-Device-ID", "")
              or body.get("device_id", "")).strip()
    if dev_id.lower() in ("", "unknown", "null", "none"):
        dev_id = ""
    keys   = load_keys()

    if key not in keys:
        return web.json_response({"valid": False, "error": "Key not found"})

    kd = keys[key]
    st = key_status(kd)
    if st == "blocked":
        return web.json_response({"valid": False, "blocked": True})
    if st == "expired":
        return web.json_response({"valid": False, "expired": True})

    if kd.get("stock") and not kd.get("activated_at") and dev_id:
        dur_h = kd.get("duration_hours", 0)
        kd["activated_at"] = datetime.now(timezone.utc).isoformat()
        kd["expires_at"]   = expiry_from_spec(hours=dur_h) if dur_h else None
        # Save immediately so response carries the correct activated_at + expires_at
        keys[key] = kd
        save_keys(keys)

    used_by      = kd.get("used_by", [])
    device_limit = kd.get("device_limit", 0)

    if dev_id:
        if dev_id not in used_by:
            # New device — check limit before adding
            if device_limit and len(used_by) >= device_limit:
                return web.json_response({
                    "valid":       False,
                    "deviceLimit": True,
                    "limit":       device_limit,
                    "deviceCount": len(used_by),
                    "error":       f"Device limit reached ({device_limit})",
                })
            used_by.append(dev_id)
        # Device already in list — always allow, never re-check limit

    kd["used_by"]   = used_by
    kd["last_used"] = datetime.now(timezone.utc).isoformat()
    keys[key]       = kd
    save_keys(keys)

    if dev_id:
        users = load_users()
        users[dev_id] = {
            "last_seen": datetime.now(timezone.utc).isoformat(),
            "last_key":  key[:15],
        }
        save_users(users)

    all_insts    = load_instances()
    key_inst_ids = set(kd.get("instances", []))

    # All enabled instances with patch files — game param is ignored
    # because each instance works for BOTH FF and FF MAX
    active_insts = [
        i for i in all_insts
        if inst_has_patch(i)
        and i.get("enabled", True)
    ]

    # Build response list — IPA shows all instances regardless of game tab
    # inKey          = True  → this key has access (inject button active)
    # inKey          = False → "Not in Plan" (greyed out)
    # availableGames = ["ff", "ffmax"] | ["ff"] | ["ffmax"]
    #                  IPA uses this to show/grey the inject button per game tab
    # injectedGames  = which games are currently injected on THIS device
    #                  IPA uses this to show Inject vs Restore button per game

    # Build set of currently-injected game+instance combos for this device.
    # Only "injected" status counts — restored/force_cleanup/cleaned = no longer active.
    active_injects = kd.get("active_injects", {})
    injected_set: set = set()
    for ak, av in active_injects.items():
        if (av.get("device_id") == dev_id
                and av.get("status") == "injected"):
            injected_set.add(f"{av['instance_id']}:{av['game']}")

    all_resp = []
    for i in active_insts:
        av_games   = inst_patch_games(i)
        in_key     = i["id"] in key_inst_ids
        multi_inj  = bool(i.get("multi_inject", False))
        # Which games are injected right now on this device for this instance
        inj_games  = [g for g in ("ff", "ffmax") if f"{i['id']}:{g}" in injected_set]
        # Rule counts per game — IPA uses these to decide inject logic:
        #   ruleCountFF/FFMax == 1 → single rule → backup+restore
        #   ruleCountFF/FFMax >= 2 → multi rule  → no backup, delete on off
        rc_ff    = i.get("rule_count_ff",    i.get("rule_count", 1))
        rc_ffmax = i.get("rule_count_ffmax", i.get("rule_count", 1))
        all_resp.append({
            "id":             i["id"],
            "name":           i["name"],
            "label":          i["label"],
            "icon":           i.get("icon", "scope"),
            "game":           "both",
            "availableGames": av_games,
            "injectedGames":  inj_games,
            "patchId":        i["id"],
            "inKey":          in_key,
            "multiInject":    multi_inj,    # IPA: overall flag (True if any game has 2+ rules)
            "patchNameFF":    _to_mxff(i.get("patch_file_ff")    or i.get("patch_file") or f"{i['id']}_ff.mxff"),
            "patchNameFFMax": _to_mxff(i.get("patch_file_ffmax") or i.get("patch_file") or f"{i['id']}_ffmax.mxff"),
            "injectedGames":  inj_games,
        })

    # Backward-compat: list of IDs this key can access
    # v7.2: Owner keys get ALL instance IDs — IPA uses this to bypass instance check
    key_is_owner = bool(kd.get("owner") if key else False)
    if key_is_owner:
        key_resp = [i["id"] for i in active_insts]  # owner: all instances accessible
        for item in all_resp:                         # mark all as inKey for owner
            item["inKey"] = True
    else:
        key_resp = [i["id"] for i in active_insts if i["id"] in key_inst_ids]

    return web.json_response({
        "valid":         True,
        "instances":     all_resp,
        "keyInstances":  key_resp,
        "game":          game,
        "deviceLimit":   False,
        "limit":         device_limit,
        "deviceCount":   len(used_by),
        "owner":         kd.get("owner", False),
        "stock":         kd.get("stock", False),
        "expires_at":    kd.get("expires_at"),    # ISO8601 or null = never (freshly set for stock)
        "activated_at":  kd.get("activated_at"),  # ISO8601 when key was first activated (freshly set for stock)
    })


async def api_inject_confirm(request):
    """
    IPA calls this after successfully injecting a patch file.
    Body: { "key": "...", "instance_id": "...", "game": "ff"|"ffmax",
            "backup_taken": true, "backup_path": "...", "device_id": "..." }

    IPA flow:
      1. Download patch via GET /api/patch/<id>  (with X-Game: ff|ffmax)
      2. Back up the ORIGINAL file on-device  (IPA handles this locally)
      3. Inject patch file into the game container
      4. POST /inject/confirm  ← this endpoint
    Server tracks state so restore can be confirmed later.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad request"}, status=400)

    key         = body.get("key", "").strip().upper()
    instance_id = body.get("instance_id", "")
    game        = body.get("game", "ff").strip().lower()
    backup_taken= bool(body.get("backup_taken", False))
    backup_path = body.get("backup_path", "")        # informational — stored on device
    dev_id      = request.headers.get("X-Device-ID", body.get("device_id", ""))

    if not key or not instance_id:
        return web.json_response({"ok": False, "error": "missing fields"}, status=400)

    keys = load_keys()
    if key not in keys or key_status(keys[key]) != "active":
        return web.json_response({"ok": False, "error": "invalid key"}, status=403)

    kd = keys[key]
    if instance_id not in kd.get("instances", []):
        return web.json_response({"ok": False, "error": "instance not in plan"}, status=403)

    # ── Determine inject mode from rule count ────────────────────────────────
    # Single rule (rule_count == 1): IPA backed up original; server tracks backup.
    #   On toggle-off: IPA restores from backup_path → POST /inject/restore
    # Multi rule  (rule_count >= 2): IPA did NOT take a backup (multiple target files).
    #   On toggle-off: IPA DELETES all injected files → POST /inject/restore (delete mode)
    inst = find_instance(instance_id)
    if inst:
        # Use per-game rule count if available
        if game == "ff":
            rule_count = inst.get("rule_count_ff", inst.get("rule_count", 1))
        else:
            rule_count = inst.get("rule_count_ffmax", inst.get("rule_count", 1))
        multi_inject = (rule_count > 1)
    else:
        multi_inject = False

    # ── Track active inject state per device+game ────────────────────────────
    # active_injects: { "<dev_id>:<instance_id>:<game>": { status, injected_at, backup_path } }
    active_key = f"{dev_id}:{instance_id}:{game}"
    active     = kd.setdefault("active_injects", {})
    active[active_key] = {
        "instance_id":  instance_id,
        "game":         game,
        "device_id":    dev_id,
        "status":       "injected",
        "backup_taken": False if multi_inject else backup_taken,
        "backup_path":  "" if multi_inject else backup_path,
        "multi_inject": multi_inject,  # IPA uses this to decide delete vs restore on toggle-off
        "injected_at":  datetime.now(timezone.utc).isoformat(),
    }

    # Also append to inject_log (history, last 50)
    injects = kd.setdefault("inject_log", [])
    injects.append({
        "instance_id":  instance_id,
        "game":         game,
        "device_id":    dev_id,
        "injected_at":  datetime.now(timezone.utc).isoformat(),
        "status":       "injected",
        "backup_taken": False if multi_inject else backup_taken,
        "multi_inject": multi_inject,
    })
    kd["inject_log"] = injects[-50:]
    save_keys(keys)

    log.info(f"Inject confirmed: key={key[:10]}… inst={instance_id} game={game} "
             f"multi={multi_inject} backup={backup_taken} dev={dev_id[:8] if dev_id else '?'}…")
    return web.json_response({
        "ok":              True,
        "instance_id":     instance_id,
        "game":            game,
        "backup_taken":    False if multi_inject else backup_taken,
        "multi_inject":    multi_inject,
        # ── Toggle-off primary action ────────────────────────────────────────
        # multi_inject=true  → "delete"   (no backup was taken; delete injected files)
        # multi_inject=false → "restore"  (restore original from on-device backup_path)
        "toggle_off_action": "delete" if multi_inject else "restore",
        # ── Fallback if primary action fails ─────────────────────────────────
        # If restore fails for any reason (file missing, permission error, etc.)
        # IPA MUST NOT crash — delete the injected file instead, then call /inject/cleanup
        "fallback_action":   "delete_injected",
    })


async def api_inject_restore(request):
    """
    IPA calls this after attempting to restore the original file (undoing inject).

    Body: {
      "key":           "...",
      "instance_id":   "...",
      "game":          "ff" | "ffmax",
      "device_id":     "...",
      "restore_ok":    true | false,   ← NEW: did IPA restore succeed?
      "restore_error": "..."           ← NEW: error message if restore_ok=false
    }

    IPA restore flow:
      1. User toggles OFF in IPA
      2a. Single-rule: IPA tries to restore original from on-device backup_path
            SUCCESS → POST /inject/restore { restore_ok: true }
            FAIL    → POST /inject/restore { restore_ok: false, restore_error: "..." }
                      server responds { force_delete: true }
                      IPA deletes injected file, then calls POST /inject/cleanup
      2b. Multi-rule: IPA deletes injected files directly, then
            POST /inject/restore { restore_ok: true }  (treated as "done")
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad request"}, status=400)

    key           = body.get("key", "").strip().upper()
    instance_id   = body.get("instance_id", "")
    game          = body.get("game", "ff").strip().lower()
    dev_id        = request.headers.get("X-Device-ID", body.get("device_id", ""))
    # restore_ok: True = restore succeeded (or delete-mode completed)
    #             False = restore FAILED → server tells IPA to delete injected file
    restore_ok    = bool(body.get("restore_ok", True))   # default True (backward compat)
    restore_error = body.get("restore_error", "")

    if not key or not instance_id:
        return web.json_response({"ok": False, "error": "missing fields"}, status=400)

    keys = load_keys()
    if key not in keys:
        # Key not found — still return ok so IPA doesn't crash.
        # IPA should clean up locally.
        log.warning(f"Restore: key not found {key[:10]}… — returning force_delete")
        return web.json_response({
            "ok":          True,
            "instance_id": instance_id,
            "game":        game,
            "force_delete": True,
            "message":     "Key not found — delete injected file",
        })

    kd         = keys[key]
    active_key = f"{dev_id}:{instance_id}:{game}"
    active     = kd.setdefault("active_injects", {})

    # ── Determine final status ────────────────────────────────────────────────
    if restore_ok:
        new_status = "restored"
    else:
        # Restore failed — mark for force-cleanup
        new_status = "force_cleanup"
        log.warning(
            f"Restore FAILED: key={key[:10]}… inst={instance_id} game={game} "
            f"err='{restore_error}' dev={dev_id[:8] if dev_id else '?'}… "
            f"→ instructing IPA to delete injected file"
        )

    # ── Update active_injects ─────────────────────────────────────────────────
    matched = False
    if active_key in active:
        active[active_key]["status"]      = new_status
        active[active_key]["restored_at"] = datetime.now(timezone.utc).isoformat()
        if restore_error:
            active[active_key]["restore_error"] = restore_error
        matched = True
    else:
        # Fallback: legacy entries without game suffix or old key format
        for ak, av in active.items():
            if (av.get("instance_id") == instance_id
                    and av.get("device_id") == dev_id
                    and av.get("status") in ("injected", "force_cleanup")):
                av["status"]      = new_status
                av["restored_at"] = datetime.now(timezone.utc).isoformat()
                if restore_error:
                    av["restore_error"] = restore_error
                matched = True
                break

    if not matched:
        # No active inject record found — still return ok so IPA can clean up
        log.warning(
            f"Restore: no active inject record found for "
            f"inst={instance_id} game={game} dev={dev_id[:8] if dev_id else '?'}…"
        )

    # ── Update inject_log history ─────────────────────────────────────────────
    for entry in reversed(kd.get("inject_log", [])):
        if (entry.get("instance_id") == instance_id
                and entry.get("device_id") == dev_id
                and entry.get("status") in ("injected", "force_cleanup")
                and (entry.get("game") == game or "game" not in entry)):
            entry["status"]      = new_status
            entry["restored_at"] = datetime.now(timezone.utc).isoformat()
            break

    save_keys(keys)

    log.info(
        f"Restore {'OK' if restore_ok else 'FAILED→cleanup'}: "
        f"key={key[:10]}… inst={instance_id} game={game} "
        f"dev={dev_id[:8] if dev_id else '?'}…"
    )

    return web.json_response({
        "ok":           True,
        "instance_id":  instance_id,
        "game":         game,
        # force_delete=true → IPA must DELETE the injected file (restore failed)
        # force_delete=false → restore succeeded, nothing more to do
        "force_delete": not restore_ok,
        "message": (
            "Restore failed — delete the injected file to avoid crash"
            if not restore_ok else "Restored OK"
        ),
    })


async def api_inject_cleanup(request):
    """
    IPA calls this after force-deleting an injected file (fallback when restore fails).

    Body: {
      "key":         "...",
      "instance_id": "...",
      "game":        "ff" | "ffmax",
      "device_id":   "...",
      "deleted":     true | false,    ← did IPA successfully delete the injected file?
      "delete_error": "..."           ← error if deleted=false
    }

    This is the final cleanup step when:
      - Single-rule restore failed    → IPA deleted injected file → calls /inject/cleanup
      - Multi-rule delete completed   → IPA deleted all injected files → calls /inject/cleanup
      - Any unexpected error          → IPA calls /inject/cleanup as last resort

    Server response is ALWAYS ok:true so IPA never crashes waiting for this call.
    """
    try:
        body = await request.json()
    except Exception:
        # Even on bad request, return ok so IPA doesn't crash
        return web.json_response({"ok": True, "cleaned": True})

    key          = (body.get("key", "") or "").strip().upper()
    instance_id  = (body.get("instance_id", "") or "").strip()
    game         = (body.get("game", "ff") or "ff").strip().lower()
    dev_id       = request.headers.get("X-Device-ID", body.get("device_id", "") or "")
    deleted      = bool(body.get("deleted", True))
    delete_error = body.get("delete_error", "")

    if key and instance_id:
        keys = load_keys()
        if key in keys:
            kd         = keys[key]
            active_key = f"{dev_id}:{instance_id}:{game}"
            active     = kd.setdefault("active_injects", {})

            final_status = "cleaned" if deleted else "cleanup_failed"

            # Update primary key
            if active_key in active:
                active[active_key]["status"]     = final_status
                active[active_key]["cleaned_at"] = datetime.now(timezone.utc).isoformat()
                if delete_error:
                    active[active_key]["delete_error"] = delete_error
            else:
                # Fallback: match legacy or force_cleanup entries
                for ak, av in active.items():
                    if (av.get("instance_id") == instance_id
                            and av.get("device_id") == dev_id
                            and av.get("status") in ("injected", "force_cleanup", "restored")):
                        av["status"]     = final_status
                        av["cleaned_at"] = datetime.now(timezone.utc).isoformat()
                        if delete_error:
                            av["delete_error"] = delete_error
                        break

            # Update inject_log
            for entry in reversed(kd.get("inject_log", [])):
                if (entry.get("instance_id") == instance_id
                        and entry.get("device_id") == dev_id
                        and entry.get("status") in ("injected", "force_cleanup", "restored")
                        and (entry.get("game") == game or "game" not in entry)):
                    entry["status"]     = final_status
                    entry["cleaned_at"] = datetime.now(timezone.utc).isoformat()
                    break

            try:
                save_keys(keys)
            except Exception as ex:
                log.warning(f"Cleanup save_keys error (non-fatal): {ex}")

    log.info(
        f"Inject cleanup: inst={instance_id} game={game} "
        f"deleted={deleted} dev={dev_id[:8] if dev_id else '?'}… "
        + (f"err='{delete_error}'" if delete_error else "")
    )

    # ALWAYS return ok=True — IPA must never crash waiting for this response
    return web.json_response({
        "ok":      True,
        "cleaned": True,
        "deleted": deleted,
        "instance_id": instance_id,
        "game":    game,
    })


async def api_patch_templates(request):
    """
    GET /patch/templates
    Returns available inject path templates for the Onarchy patch builder.
    Header: X-Key (must be an owner key)

    IPA Patch Making tab uses this to populate path dropdown when adding rules.
    """
    key = request.headers.get("X-Key", "").strip().upper()
    if key:
        keys = load_keys()
        if key not in keys or not keys[key].get("owner"):
            return web.json_response({"ok": False, "error": "owner key required"}, status=403)

    templates = [
        {
            "id":    "cache_res",
            "label": "Cache Res (Compulsory)",
            "path":  MXCF_PATH_CACHE_RES,
            "desc":  "Main game asset bundles (Compulsory)",
        },
        {
            "id":    "shaders",
            "label": "Shaders (Optional)",
            "path":  MXCF_PATH_SHADERS,
            "desc":  "Shader asset bundles (Optional)",
        },
        {
            "id":    "assetindexer",
            "label": "Asset Indexer (Avatar)",
            "path":  MXCF_PATH_ASSETINDEXER,
            "desc":  "Avatar asset indexer bundles",
        },
        {
            "id":    "plist",
            "label": "Plist (Preferences)",
            "path":  MXCF_PATH_PLIST,
            "desc":  "Library preferences plist files",
        },
        {
            "id":    "modavatar",
            "label": "Mod Avatar (Optional)",
            "path":  MXCF_PATH_MODAVATAR,
            "desc":  "Optional avatar resource bundles",
        },
        {
            "id":    "custom",
            "label": "Custom Path",
            "path":  "",
            "desc":  "Enter a custom inject path manually",
        },
    ]
    return web.json_response({"ok": True, "templates": templates})


async def api_patch_build(request):
    """
    POST /patch/build
    Builds a new .mxff patch file from multiple rules provided by the IPA Patch Making tab.
    Header: X-Key (must be an owner key)

    Body (multipart/form-data OR JSON):
    {
      "key":        "<owner key>",
      "patch_name": "MyPatch",
      "bundle_id":  "com.dts.freefireth",   // or freefiremax
      "rules": [
        {
          "file_name":   "cache_res_123.bundle",
          "inject_path": "Documents/contentcache/Compulsory/ios/gameassetbundles/",
          "data_b64":    "<base64 encoded file content>"
        },
        {
          "file_name":   "shaders_abc.bundle",
          "inject_path": "Documents/contentcache/Optional/ios/gameassetbundles/",
          "data_b64":    "<base64 encoded file content>"
        }
        // ... more rules
      ]
    }

    The server builds a .mxff container with 3105PATCH magic + plist carrying:
      - schemaVersion: 2
      - packageID:     <uuid>
      - bundleID:      <bundle_id>
      - ruleCount:     <number of rules>
      - rules:         [ { fileName, injectPath, encryptedData } ]
      - isPasswordProtected: False
      - publicContentKey: <random 32 bytes>
      - keyFingerprint:   <sha256 of publicContentKey>

    Returns: the built .mxff file as application/octet-stream
    """
    import base64 as _b64
    import uuid as _uuid_mod

    key = request.headers.get("X-Key", "").strip().upper()
    if not key:
        # Try JSON body
        try:
            body = await request.json()
            key  = body.get("key", "").strip().upper()
        except Exception:
            return web.json_response({"ok": False, "error": "bad request"}, status=400)
    else:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad request"}, status=400)

    if not key:
        return web.json_response({"ok": False, "error": "missing key"}, status=400)

    keys = load_keys()
    if key not in keys:
        return web.json_response({"ok": False, "error": "invalid key"}, status=403)
    kd = keys[key]
    if key_status(kd) != "active":
        return web.json_response({"ok": False, "error": "key inactive"}, status=403)
    if not kd.get("owner"):
        return web.json_response({"ok": False, "error": "owner key required"}, status=403)

    patch_name = body.get("patch_name", "CustomPatch").strip()
    bundle_id  = body.get("bundle_id", BUNDLE_FF).strip()
    rules      = body.get("rules", [])

    if not rules:
        return web.json_response({"ok": False, "error": "no rules provided"}, status=400)
    if len(rules) > 20:
        return web.json_response({"ok": False, "error": "max 20 rules per patch"}, status=400)

    # Validate each rule
    built_rules = []
    for idx, rule in enumerate(rules):
        fname       = rule.get("file_name", "").strip()
        inject_path = rule.get("inject_path", "").strip()
        data_b64    = rule.get("data_b64", "")

        if not fname:
            return web.json_response({"ok": False, "error": f"rule {idx}: missing file_name"}, status=400)
        if not inject_path:
            # Auto-resolve from file name
            inject_path = _resolve_inject_path(fname)
        if not data_b64:
            return web.json_response({"ok": False, "error": f"rule {idx}: missing data_b64"}, status=400)

        try:
            file_data = _b64.b64decode(data_b64)
        except Exception:
            return web.json_response({"ok": False, "error": f"rule {idx}: invalid base64"}, status=400)

        built_rules.append({
            "fileName":   fname,
            "injectPath": inject_path,
            "data":       file_data,
        })

    # Build .mxff container
    import os as _os
    pkg_id         = str(_uuid_mod.uuid4()).upper()
    content_key    = _os.urandom(32)
    key_fingerprint= hashlib.sha256(content_key).digest()

    # Build plist payload
    # For multi-rule patches, store rules as an array of dicts
    # Each rule's data is stored as raw bytes (unencrypted, isPasswordProtected=False)
    import plistlib as _plist

    plist_rules = []
    for r in built_rules:
        plist_rules.append({
            "fileName":   r["fileName"],
            "injectPath": r["injectPath"],
            "fileData":   r["data"],   # bytes
        })

    plist_obj = {
        "schemaVersion":      2,
        "packageID":          pkg_id,
        "patchName":          patch_name,
        "bundleID":           bundle_id,
        "ruleCount":          len(built_rules),
        "rules":              plist_rules,
        "isPasswordProtected": False,
        "publicContentKey":   content_key,
        "keyFingerprint":     key_fingerprint,
        "builtAt":            datetime.now(timezone.utc).isoformat(),
        "builtBy":            str(kd.get("created_by", "owner")),
    }

    plist_data = _plist.dumps(plist_obj, fmt=_plist.FMT_BINARY)

    # Assemble .mxff: magic + null + plist
    mxff_data = MXCF_MAGIC + b"\x00" + plist_data

    # Save to patch dir for reference
    safe_name  = "".join(c for c in patch_name if c.isalnum() or c in "-_").lower()
    out_fname  = f"built_{safe_name}_{pkg_id[:8].lower()}.mxff"
    out_path   = PATCH_DIR / out_fname
    PATCH_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(mxff_data)

    log.info(f"Patch built: {out_fname} rules={len(built_rules)} bundle={bundle_id}")

    return web.Response(
        body=mxff_data,
        content_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{out_fname}"',
            "X-Rule-Count":        str(len(built_rules)),
            "X-Package-ID":        pkg_id,
            "X-Patch-Name":        patch_name,
        }
    )


async def api_report_rules(request):
    """
    IPA calls this after the first successful inject to report actual rule count.
    Body: { "key": "...", "instance_id": "...", "game": "ff"|"ffmax",
            "rule_count": <int>, "device_id": "..." }

    Logic:
      rule_count == 1 → single rule → multi_inject = False
                        IPA behaviour: backup original on inject, restore on toggle-off
      rule_count >= 2 → multi rule  → multi_inject = True
                        IPA behaviour: NO backup, DELETE injected files on toggle-off

    This lets the server stay in sync with what the IPA actually found inside
    the encrypted payload, correcting any heuristic set at upload time.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad request"}, status=400)

    key         = body.get("key", "").strip().upper()
    instance_id = body.get("instance_id", "")
    game        = body.get("game", "ff").strip().lower()
    rule_count  = int(body.get("rule_count", 1))
    dev_id      = request.headers.get("X-Device-ID", body.get("device_id", ""))

    if not key or not instance_id:
        return web.json_response({"ok": False, "error": "missing fields"}, status=400)

    keys = load_keys()
    if key not in keys or key_status(keys[key]) != "active":
        return web.json_response({"ok": False, "error": "invalid key"}, status=403)

    # Update instance with actual rule count and multi_inject flag
    instances = load_instances()
    changed = False
    for inst in instances:
        if inst["id"] == instance_id:
            # Update rule count for this game
            if game == "ff":
                inst["rule_count_ff"]    = rule_count
            elif game == "ffmax":
                inst["rule_count_ffmax"] = rule_count
            else:
                inst["rule_count"]       = rule_count

            # Recompute multi_inject: any game with 2+ rules → multi_inject True
            total_rules = max(
                inst.get("rule_count_ff",    inst.get("rule_count", 1)),
                inst.get("rule_count_ffmax", inst.get("rule_count", 1)),
            )
            inst["multi_inject"] = (total_rules > 1)
            changed = True
            break

    if changed:
        save_instances(instances)

    # Re-fetch to return current state
    inst = find_instance(instance_id)
    multi_inject = bool(inst and inst.get("multi_inject", False)) if inst else False

    log.info(f"Rules reported: inst={instance_id} game={game} rule_count={rule_count} "
             f"multi_inject={multi_inject} dev={dev_id[:8] if dev_id else '?'}…")

    return web.json_response({
        "ok":          True,
        "instance_id": instance_id,
        "game":        game,
        "rule_count":  rule_count,
        "multi_inject": multi_inject,
        # IPA uses multi_inject flag to decide backup/restore vs delete logic
    })


async def patch_file(request):
    """Serve a .mxff patch file for the given instance_id + game.

    IPA calls: GET /api/patch/<instance_id>
    Headers:
      X-Key:   license key (required)
      X-Game:  "ff" | "ffmax"  (defaults to "ff")

    v7.2 FIX:
      - Owner keys (key["owner"]=True) bypass instance-list check
      - File-first serving: even if instance not in DB, serves by name convention
        so owner can just drop files in data/patches/ without bot registration
      - Resolution: DB-registered name → {id}_{game}.mxff → {id}.mxff
    """
    instance_id = request.match_info.get('instance_id', '')
    if not instance_id:
        return web.Response(status=400, text='Missing instance_id')

    key  = request.headers.get('X-Key', '').strip().upper()
    game = (request.headers.get('X-Game', '')
            or request.rel_url.query.get('game', 'ff')).strip().lower()
    if game not in ('ff', 'ffmax'):
        game = 'ff'

    # ── Key validation ────────────────────────────────────────────────────────
    key_is_owner = False
    if key:
        keys = load_keys()
        if key not in keys:
            return web.Response(status=403, text='Invalid key')
        kd = keys[key]
        if key_status(kd) != 'active':
            return web.Response(status=403, text='Key inactive')
        # Owner keys can access any instance without being in instances list
        key_is_owner = bool(kd.get('owner'))
        if not key_is_owner and instance_id not in kd.get('instances', []):
            return web.Response(status=403, text='Instance not in key plan')

    # ── Resolve patch file path ───────────────────────────────────────────────
    # Priority: DB-registered filename → naming convention → legacy single file
    candidates = []

    inst = find_instance(instance_id)
    if inst:
        game_field = "patch_file_ff" if game == "ff" else "patch_file_ffmax"
        if inst.get(game_field):
            candidates.append(PATCH_DIR / inst[game_field])
        if inst.get("patch_file"):
            candidates.append(PATCH_DIR / inst["patch_file"])

    # Always try naming convention — works even without DB registration
    # This lets owner drop files in data/patches/ and have them served immediately
    for path in [
        PATCH_DIR / f'{instance_id}_{game}.mxff',
        PATCH_DIR / f'{instance_id}.mxff',
    ]:
        if path not in candidates:
            candidates.append(path)

    serve_path = next((p for p in candidates if p.exists()), None)

    if serve_path is None:
        log.warning(f"patch_file: NOT FOUND {instance_id} game={game} tried={[c.name for c in candidates]}")
        return web.Response(status=404,
                            text=f'Patch not found: {instance_id} (game={game}). '
                                 f'Upload {instance_id}_{game}.mxff via bot.')

    data = serve_path.read_bytes()

    # ── Validate: must be 3105PATCH magic ────────────────────────────────────
    if not is_mxff(data):
        log.warning(f"patch_file: {serve_path.name} invalid format (not 3105PATCH)")
        return web.Response(status=415, text='Invalid patch format — must be .mxff (3105PATCH)')

    log.info(f"patch_file: serving {serve_path.name} → {instance_id} game={game} "
             f"size={len(data)} owner={key_is_owner}")

    out_name = f'{instance_id}_{game}.mxff'
    return web.Response(
        body=data,
        content_type='application/octet-stream',
        headers={'Content-Disposition': f'attachment; filename="{out_name}"'}
    )


# ── MAIN ──────────────────────────────────────────────────────────────────────────
def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(600)
        .write_timeout(600)
        .connect_timeout(60)
        .pool_timeout(60)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("newkey",      cmd_newkey))
    app.add_handler(CommandHandler("deletekey",   cmd_deletekey))
    app.add_handler(CommandHandler("blockkey",    cmd_blockkey))
    app.add_handler(CommandHandler("unblockkey",  cmd_unblockkey))
    app.add_handler(CommandHandler("resetdevices", cmd_resetdevices))
    app.add_handler(CommandHandler("exportkeys",  cmd_exportkeys))
    app.add_handler(CommandHandler("ipaon",       cmd_ipaon))
    app.add_handler(CommandHandler("ipaoff",      cmd_ipaoff))
    app.add_handler(CommandHandler("broadcast",    cmd_broadcast))

    # Main
    app.add_handler(CallbackQueryHandler(cb_main,             pattern=r"^menu_main$"))
    app.add_handler(CallbackQueryHandler(cb_menu_keys,        pattern=r"^menu_keys$"))
    app.add_handler(CallbackQueryHandler(cb_menu_instances,   pattern=r"^menu_instances$"))
    app.add_handler(CallbackQueryHandler(cb_menu_ipa,         pattern=r"^menu_ipa$"))
    app.add_handler(CallbackQueryHandler(cb_ipa_toggle,       pattern=r"^ipa_(enable|disable)$"))
    app.add_handler(CallbackQueryHandler(cb_menu_users,       pattern=r"^menu_users$"))
    app.add_handler(CallbackQueryHandler(cb_users_list,       pattern=r"^users_list$"))
    app.add_handler(CallbackQueryHandler(cb_users_active,     pattern=r"^users_active$"))
    app.add_handler(CallbackQueryHandler(cb_menu_stats,       pattern=r"^menu_stats$"))
    app.add_handler(CallbackQueryHandler(cb_menu_sellers,     pattern=r"^menu_sellers$"))
    app.add_handler(CallbackQueryHandler(cb_menu_broadcast,   pattern=r"^menu_broadcast$"))

    # Instances — specific patterns BEFORE generic inst_del_ catch-all
    app.add_handler(CallbackQueryHandler(cb_inst_new,              pattern=r"^inst_new$"))
    app.add_handler(CallbackQueryHandler(cb_inst_game_select,      pattern=r"^instcreate_game_.+$"))
    app.add_handler(CallbackQueryHandler(cb_inst_info,             pattern=r"^inst_info_.+$"))
    # Per-game upload handlers MUST come BEFORE the generic inst_upload_ catch-all
    app.add_handler(CallbackQueryHandler(cb_inst_upload_ff,        pattern=r"^inst_upload_ff_.+$"))
    app.add_handler(CallbackQueryHandler(cb_inst_upload_ffmax,     pattern=r"^inst_upload_ffmax_.+$"))
    app.add_handler(CallbackQueryHandler(cb_inst_upload,           pattern=r"^inst_upload_.+$"))
    app.add_handler(CallbackQueryHandler(cb_inst_icon,             pattern=r"^inst_icon_.+$"))
    app.add_handler(CallbackQueryHandler(cb_inst_toggle_enabled,   pattern=r"^inst_(enable|disable)_.+$"))
    app.add_handler(CallbackQueryHandler(cb_inst_addall_keys,      pattern=r"^inst_addall_keys$"))
    app.add_handler(CallbackQueryHandler(cb_inst_del_all_ask,      pattern=r"^inst_del_all_ask$"))
    app.add_handler(CallbackQueryHandler(cb_inst_del_all_confirm,  pattern=r"^inst_del_all_confirm$"))
    app.add_handler(CallbackQueryHandler(cb_inst_del,              pattern=r"^inst_del_.+$"))

    # Keys
    app.add_handler(CallbackQueryHandler(cb_key_create,         pattern=r"^key_create$"))
    app.add_handler(CallbackQueryHandler(cb_keyinst_toggle,     pattern=r"^keyinst_(toggle_.+|all|none)$"))
    app.add_handler(CallbackQueryHandler(cb_key_go_dur,         pattern=r"^key_go_dur$"))
    app.add_handler(CallbackQueryHandler(cb_devlimit_pick,      pattern=r"^devlimit_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_keytype_pick,       pattern=r"^keytype_(regular|stock|owner)$"))
    app.add_handler(CallbackQueryHandler(cb_keycount_pick,      pattern=r"^keycount_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_dur_pick,           pattern=r"^dur_((h|d)\d+|custom)$"))
    # Custom Key
    app.add_handler(CallbackQueryHandler(cb_key_custom,         pattern=r"^key_custom$"))
    app.add_handler(CallbackQueryHandler(cb_custom_key_go_dur,  pattern=r"^custom_key_go_dur$"))
    app.add_handler(CallbackQueryHandler(cb_custom_devlimit_pick, pattern=r"^cdevlimit_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_custom_dur_pick,    pattern=r"^cdur_((h|d)\d+|custom)$"))
    app.add_handler(CallbackQueryHandler(cb_key_list,           pattern=r"^key_list$"))
    app.add_handler(CallbackQueryHandler(cb_key_block_ask,      pattern=r"^key_block_ask$"))
    app.add_handler(CallbackQueryHandler(cb_key_del_ask,        pattern=r"^key_del_ask$"))
    # Per-key inline actions (seller + owner)
    app.add_handler(CallbackQueryHandler(cb_mykey_block,        pattern=r"^mykey_block_.+$"))
    app.add_handler(CallbackQueryHandler(cb_mykey_unblock,      pattern=r"^mykey_unblock_.+$"))
    app.add_handler(CallbackQueryHandler(cb_mykey_del_ask,      pattern=r"^mykey_del_ask_.+$"))
    app.add_handler(CallbackQueryHandler(cb_mykey_del_confirm,  pattern=r"^mykey_del_confirm_.+$"))
    app.add_handler(CallbackQueryHandler(cb_mykey_del_cancel,   pattern=r"^mykey_del_cancel_.+$"))
    app.add_handler(CallbackQueryHandler(cb_mykey_resetdev,     pattern=r"^mykey_resetdev_.+$"))

    # Sellers
    app.add_handler(CallbackQueryHandler(cb_seller_add,          pattern=r"^seller_add$"))
    app.add_handler(CallbackQueryHandler(cb_seller_del,          pattern=r"^seller_del_.+$"))
    app.add_handler(CallbackQueryHandler(cb_seller_info,         pattern=r"^seller_info_.+$"))
    app.add_handler(CallbackQueryHandler(cb_seller_quota,        pattern=r"^seller_quota_.+$"))
    app.add_handler(CallbackQueryHandler(cb_seller_reset_quota,  pattern=r"^seller_reset_quota_.+$"))
    app.add_handler(CallbackQueryHandler(cb_seller_keys,         pattern=r"^seller_keys_.+$"))
    app.add_handler(CallbackQueryHandler(cb_seller_block,               pattern=r"^seller_block_.+$"))
    app.add_handler(CallbackQueryHandler(cb_seller_delkeys_confirm,     pattern=r"^seller_delkeys_confirm_.+$"))
    app.add_handler(CallbackQueryHandler(cb_seller_delkeys,             pattern=r"^seller_delkeys_.+$"))
    app.add_handler(CallbackQueryHandler(cb_seller_delunused_confirm,   pattern=r"^seller_delunused_confirm_.+$"))
    app.add_handler(CallbackQueryHandler(cb_seller_delunused,           pattern=r"^seller_delunused_.+$"))

    # Admins (Owner only)
    app.add_handler(CallbackQueryHandler(cb_menu_admins,    pattern=r"^menu_admins$"))
    app.add_handler(CallbackQueryHandler(cb_admin_add,      pattern=r"^admin_add$"))
    app.add_handler(CallbackQueryHandler(cb_admin_del,      pattern=r"^admin_del_.+$"))
    app.add_handler(CallbackQueryHandler(cb_admin_info,     pattern=r"^admin_info_.+$"))
    app.add_handler(CallbackQueryHandler(cb_admin_block,    pattern=r"^admin_block_.+$"))
    app.add_handler(CallbackQueryHandler(cb_admin_sellers,  pattern=r"^admin_sellers_.+$"))

    # GitHub Actions — Build IPA integration (owner only)
    app.add_handler(CallbackQueryHandler(cb_menu_build,    pattern=r"^menu_build$"))
    app.add_handler(CallbackQueryHandler(cb_build_trigger, pattern=r"^build_trigger$"))
    # build_status_ and build_dl_ carry the run_id as suffix — catch ALL with .+
    app.add_handler(CallbackQueryHandler(cb_build_status,  pattern=r"^build_status_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_build_download, pattern=r"^build_dl_\d+$"))

    app.add_handler(CallbackQueryHandler(cb_noop, pattern=r"^noop$"))

    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, file_handler))

    async def post_init(application):
        # Gist থেকে ডেটা load করো — restart হলেও ডেটা থাকবে
        await gist_load()
        seed_builtin_instances()
        # Backend (validate + patch) চলছে Render-এ: https://mxff.onrender.com
        log.info(f"Bot ready | Owner: {OWNER_ID} | Backend: {BACKEND_URL} | Gist: {GIST_ID[:8]}…")

    app.post_init = post_init
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
