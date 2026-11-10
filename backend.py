#!/usr/bin/env python3
"""
FOUGO EXTERNAL — IPA Backend Server
Runs on Render.com (single port, all routes merged)

Routes:
  POST /api/validate          ← IPA key validation
  POST /validate              ← backward-compat alias
  GET  /api/patch/{id}        ← serve .mxff patch file
  POST /inject/confirm        ← track inject state
  POST /inject/restore        ← track restore state
  POST /inject/cleanup        ← track cleanup state
  POST /inject/report_rules   ← sync rule count
  POST /patch/build           ← build .mxff from rules
  GET  /patch/templates       ← inject path templates
  GET  /health                ← health check for Render
"""

import asyncio
import hashlib
import json
import logging
import os
import struct as _struct
import uuid
import uuid as _uuid_mod
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web, ClientSession, ClientTimeout

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("FOUGO-BACKEND")

# ── Config ─────────────────────────────────────────────────────────────────────
PORT     = int(os.getenv("PORT", "10020"))          # Render sets PORT automatically
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))

KEYS_FILE      = DATA_DIR / "keys.json"
USERS_FILE     = DATA_DIR / "users.json"
CFG_FILE       = DATA_DIR / "config.json"
INSTANCES_FILE = DATA_DIR / "instances.json"
PATCH_DIR      = DATA_DIR / "patches"

# ── GitHub Gist Persistence ────────────────────────────────────────────────────
# Bot আর Backend একই Gist থেকে data পড়বে/লিখবে
GH_TOKEN  = os.getenv("GH_TOKEN", "")
GIST_ID   = os.getenv("GIST_ID", "02df366c20fa09d72a1af1bf16922aa0")
GIST_FILE = "fougo_data.json"
GIST_API  = f"https://api.github.com/gists/{GIST_ID}"
GIST_SYNC_INTERVAL = 30  # প্রতি ৩০ সেকেন্ডে Gist থেকে sync করবে

# ── .mxff Format ───────────────────────────────────────────────────────────────
MXCF_MAGIC    = b"3105PATCH"
BUNDLE_FF     = "com.dts.freefireth"
BUNDLE_FFMAX  = "com.dts.freefiremax"

MXCF_PATH_CACHE_RES    = "Documents/contentcache/Compulsory/ios/gameassetbundles/"
MXCF_PATH_SHADERS      = "Documents/contentcache/Optional/ios/gameassetbundles/"
MXCF_PATH_ASSETINDEXER = "Documents/contentcache/Compulsory/ios/gameassetbundles/avatar/"
MXCF_PATH_PLIST        = "Library/Preferences/"
MXCF_PATH_MODAVATAR    = "Documents/contentcache/Optional/ios/optionalavatarres/gameassetbundles/"

def is_mxff(data: bytes) -> bool:
    return data[:9] == MXCF_MAGIC

def _to_mxff(fname: str) -> str:
    if fname and fname.endswith(".mxff"):
        return fname[:-5] + ".mxff"
    return fname

def _resolve_inject_path(payload_filename: str) -> str:
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

# ── Data Helpers ───────────────────────────────────────────────────────────────
def _load(path: Path, default):
    try:
        return json.loads(path.read_text()) if path.exists() else default
    except Exception:
        return default

def _save(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))

def load_keys():       return _load(KEYS_FILE,       {})
def load_users():      return _load(USERS_FILE,      {})
def load_cfg():        return _load(CFG_FILE,        {"ipa_enabled": True})
def load_instances():  return _load(INSTANCES_FILE,  [])

def _schedule_gist_save():
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(gist_save())
    except RuntimeError:
        pass

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

# ── Key / Instance Helpers ─────────────────────────────────────────────────────
BUILTIN_INSTANCES = [
    {"id": "aimbot",     "name": "AIMBOT",       "label": "AIMBOT",       "icon": "scope",               "game": "both", "patch_file": None, "builtin": True},
    {"id": "aimsilent",  "name": "AIMSILENT",    "label": "AIMSILENT",    "icon": "scope.fill",          "game": "both", "patch_file": None, "builtin": True},
    {"id": "injectmenu", "name": "INJECT MENU",  "label": "INJECT MENU",  "icon": "list.bullet.indent",  "game": "both", "patch_file": None, "builtin": True},
]

def seed_builtin_instances():
    instances   = load_instances()
    existing    = {i["id"] for i in instances}
    changed     = False
    for bi in BUILTIN_INSTANCES:
        if bi["id"] not in existing:
            entry = dict(bi)
            entry["created_at"] = datetime.now(timezone.utc).isoformat()
            instances.append(entry)
            changed = True
            log.info(f"Seeded built-in instance: {bi['id']}")
    if changed:
        save_instances(instances)

def find_instance(iid):
    return next((i for i in load_instances() if i["id"] == iid), None)

def inst_has_patch(inst):
    return bool(inst.get("patch_file_ff") or inst.get("patch_file_ffmax") or inst.get("patch_file"))

def inst_patch_games(inst) -> list:
    games = []
    if inst.get("patch_file_ff") or (inst.get("patch_file") and not inst.get("patch_file_ffmax")):
        games.append("ff")
    if inst.get("patch_file_ffmax") or (inst.get("patch_file") and not inst.get("patch_file_ff")):
        games.append("ffmax")
    if inst.get("patch_file") and not inst.get("patch_file_ff") and not inst.get("patch_file_ffmax"):
        games = ["ff", "ffmax"]
    return games

def key_status(k: dict) -> str:
    if k.get("blocked"):
        return "blocked"
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

# ══════════════════════════════════════════════════════════════════════════════
# GIST SYNC — Bot আর Backend একই Gist থেকে data নেবে
# ══════════════════════════════════════════════════════════════════════════════

def _gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

async def gist_load() -> bool:
    """GitHub Gist থেকে সব data load করো।"""
    if not GH_TOKEN or not GIST_ID:
        log.warning("Gist: GH_TOKEN বা GIST_ID নেই — local data ব্যবহার হবে")
        return False
    try:
        async with ClientSession(timeout=ClientTimeout(total=30)) as session:
            async with session.get(GIST_API, headers=_gh_headers()) as r:
                if r.status != 200:
                    log.warning(f"Gist load failed: HTTP {r.status}")
                    return False
                data    = await r.json()
                content = data.get("files", {}).get(GIST_FILE, {}).get("content", "{}")
                all_data = json.loads(content)

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PATCH_DIR.mkdir(parents=True, exist_ok=True)

        if all_data.get("keys"):      _save(KEYS_FILE,      all_data["keys"])
        if all_data.get("users"):     _save(USERS_FILE,     all_data["users"])
        if all_data.get("config"):    _save(CFG_FILE,       all_data["config"])
        if all_data.get("instances"): _save(INSTANCES_FILE, all_data["instances"])
        if all_data.get("sellers"):   _save(SELLERS_FILE,   all_data["sellers"])
        if all_data.get("admins"):    _save(ADMINS_FILE,    all_data["admins"])

        # Patch files download করো (patch_*.b64 নামের files)
        patch_files = {
            k: v for k, v in data.get("files", {}).items()
            if k.startswith("patch_") and k.endswith(".b64")
        }
        PATCH_DIR.mkdir(parents=True, exist_ok=True)
        for gist_fname, file_info in patch_files.items():
            try:
                import base64 as _b64
                # Gist file content থেকে base64 decode করো
                b64_content = file_info.get("content", "")
                if not b64_content:
                    # truncated — raw_url থেকে fetch করো
                    raw_url = file_info.get("raw_url", "")
                    if raw_url:
                        async with ClientSession(timeout=ClientTimeout(total=30)) as s2:
                            async with s2.get(raw_url, headers=_gh_headers()) as r2:
                                if r2.status == 200:
                                    b64_content = await r2.text()
                if b64_content:
                    patch_data  = _b64.b64decode(b64_content)
                    # gist filename: patch_<fname>.b64 → actual fname: <fname>
                    actual_name = gist_fname[len("patch_"):-len(".b64")]
                    out_path    = PATCH_DIR / actual_name
                    out_path.write_bytes(patch_data)
                    log.info(f"✅ Patch '{actual_name}' Gist থেকে load হয়েছে")
            except Exception as pe:
                log.warning(f"Patch download error ({gist_fname}): {pe}")

        log.info("✅ Gist থেকে data load হয়েছে")
        return True
    except Exception as ex:
        log.warning(f"Gist load error: {ex}")
        return False

async def gist_save() -> bool:
    """সব data Gist-এ save করো।"""
    if not GH_TOKEN or not GIST_ID:
        return False
    try:
        all_data = {
            "keys":      load_keys(),
            "users":     load_users(),
            "config":    load_cfg(),
            "instances": load_instances(),
            "sellers":   _load(SELLERS_FILE, {}),
            "admins":    _load(ADMINS_FILE,  {}),
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
        log.info("✅ Gist-এ data save হয়েছে")
        return True
    except Exception as ex:
        log.warning(f"Gist save error: {ex}")
        return False

_last_gist_load = 0.0  # timestamp of last Gist load

async def gist_load_if_stale(max_age: float = 10.0) -> bool:
    """
    Gist থেকে data load করো যদি শেষ load ১০ সেকেন্ডের বেশি আগে হয়।
    Validate request-এ call হয় যাতে fresh data পাওয়া যায়।
    """
    global _last_gist_load
    import time
    if time.time() - _last_gist_load > max_age:
        result = await gist_load()
        _last_gist_load = time.time()
        return result
    return True

async def gist_sync_loop():
    """প্রতি ৩০ সেকেন্ডে Gist থেকে latest data load করো।"""
    global _last_gist_load
    import time
    while True:
        await asyncio.sleep(GIST_SYNC_INTERVAL)
        await gist_load()
        _last_gist_load = time.time()
        log.debug("Gist sync done")

# ══════════════════════════════════════════════════════════════════════════════
# API HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def health(request):
    """GET /health — Render health check"""
    return web.json_response({"ok": True, "service": "FOUGO-BACKEND"})


async def api_validate(request):
    """POST /api/validate  or  POST /validate"""
    # Fresh data load — ১০ সেকেন্ডের বেশি পুরনো হলে Gist থেকে reload করো
    await gist_load_if_stale(max_age=10.0)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"valid": False, "error": "bad request"}, status=400)

    cfg = load_cfg()
    if not cfg.get("ipa_enabled", True):
        return web.json_response({"valid": False, "blocked": True, "error": "IPA disabled"})

    key    = body.get("key", "").strip().upper()
    game   = body.get("game", "ff")
    dev_id = (request.headers.get("X-Device-ID", "") or body.get("device_id", "")).strip()
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
        keys[key] = kd
        save_keys(keys)

    used_by      = kd.get("used_by", [])
    device_limit = kd.get("device_limit", 0)

    if dev_id:
        if dev_id not in used_by:
            if device_limit and len(used_by) >= device_limit:
                return web.json_response({
                    "valid":       False,
                    "deviceLimit": True,
                    "limit":       device_limit,
                    "deviceCount": len(used_by),
                    "error":       f"Device limit reached ({device_limit})",
                })
            used_by.append(dev_id)

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

    active_insts = [
        i for i in all_insts
        if inst_has_patch(i) and i.get("enabled", True)
    ]

    active_injects = kd.get("active_injects", {})
    injected_set: set = set()
    for ak, av in active_injects.items():
        if av.get("device_id") == dev_id and av.get("status") == "injected":
            injected_set.add(f"{av['instance_id']}:{av['game']}")

    all_resp = []
    for i in active_insts:
        av_games  = inst_patch_games(i)
        in_key    = i["id"] in key_inst_ids
        multi_inj = bool(i.get("multi_inject", False))
        inj_games = [g for g in ("ff", "ffmax") if f"{i['id']}:{g}" in injected_set]
        rc_ff     = i.get("rule_count_ff",    i.get("rule_count", 1))
        rc_ffmax  = i.get("rule_count_ffmax", i.get("rule_count", 1))
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
            "multiInject":    multi_inj,
            "patchNameFF":    _to_mxff(i.get("patch_file_ff")    or i.get("patch_file") or f"{i['id']}_ff.mxff"),
            "patchNameFFMax": _to_mxff(i.get("patch_file_ffmax") or i.get("patch_file") or f"{i['id']}_ffmax.mxff"),
        })

    key_is_owner = bool(kd.get("owner") if key else False)
    if key_is_owner:
        key_resp = [i["id"] for i in active_insts]
        for item in all_resp:
            item["inKey"] = True
    else:
        key_resp = [i["id"] for i in active_insts if i["id"] in key_inst_ids]

    return web.json_response({
        "valid":        True,
        "instances":    all_resp,
        "keyInstances": key_resp,
        "game":         game,
        "deviceLimit":  False,
        "limit":        device_limit,
        "deviceCount":  len(used_by),
        "owner":        kd.get("owner", False),
        "stock":        kd.get("stock", False),
        "expires_at":   kd.get("expires_at"),
        "activated_at": kd.get("activated_at"),
    })


async def api_inject_confirm(request):
    """POST /inject/confirm"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad request"}, status=400)

    key          = body.get("key", "").strip().upper()
    instance_id  = body.get("instance_id", "")
    game         = body.get("game", "ff").strip().lower()
    backup_taken = bool(body.get("backup_taken", False))
    backup_path  = body.get("backup_path", "")
    dev_id       = request.headers.get("X-Device-ID", body.get("device_id", ""))

    if not key or not instance_id:
        return web.json_response({"ok": False, "error": "missing fields"}, status=400)

    keys = load_keys()
    if key not in keys or key_status(keys[key]) != "active":
        return web.json_response({"ok": False, "error": "invalid key"}, status=403)

    kd = keys[key]
    if instance_id not in kd.get("instances", []):
        return web.json_response({"ok": False, "error": "instance not in plan"}, status=403)

    inst = find_instance(instance_id)
    if inst:
        rule_count   = inst.get("rule_count_ff" if game == "ff" else "rule_count_ffmax", inst.get("rule_count", 1))
        multi_inject = (rule_count > 1)
    else:
        multi_inject = False

    active_key = f"{dev_id}:{instance_id}:{game}"
    active     = kd.setdefault("active_injects", {})
    active[active_key] = {
        "instance_id":  instance_id,
        "game":         game,
        "device_id":    dev_id,
        "status":       "injected",
        "backup_taken": False if multi_inject else backup_taken,
        "backup_path":  "" if multi_inject else backup_path,
        "multi_inject": multi_inject,
        "injected_at":  datetime.now(timezone.utc).isoformat(),
    }

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

    log.info(f"Inject confirmed: key={key[:10]}… inst={instance_id} game={game} multi={multi_inject}")
    return web.json_response({
        "ok":               True,
        "instance_id":      instance_id,
        "game":             game,
        "backup_taken":     False if multi_inject else backup_taken,
        "multi_inject":     multi_inject,
        "toggle_off_action": "delete" if multi_inject else "restore",
        "fallback_action":   "delete_injected",
    })


async def api_inject_restore(request):
    """POST /inject/restore"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad request"}, status=400)

    key           = body.get("key", "").strip().upper()
    instance_id   = body.get("instance_id", "")
    game          = body.get("game", "ff").strip().lower()
    dev_id        = request.headers.get("X-Device-ID", body.get("device_id", ""))
    restore_ok    = bool(body.get("restore_ok", True))
    restore_error = body.get("restore_error", "")

    if not key or not instance_id:
        return web.json_response({"ok": False, "error": "missing fields"}, status=400)

    keys = load_keys()
    if key not in keys:
        log.warning(f"Restore: key not found {key[:10]}…")
        return web.json_response({
            "ok": True, "instance_id": instance_id, "game": game,
            "force_delete": True, "message": "Key not found — delete injected file",
        })

    kd         = keys[key]
    active_key = f"{dev_id}:{instance_id}:{game}"
    active     = kd.setdefault("active_injects", {})
    new_status = "restored" if restore_ok else "force_cleanup"

    matched = False
    if active_key in active:
        active[active_key]["status"]      = new_status
        active[active_key]["restored_at"] = datetime.now(timezone.utc).isoformat()
        if restore_error:
            active[active_key]["restore_error"] = restore_error
        matched = True
    else:
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

    for entry in reversed(kd.get("inject_log", [])):
        if (entry.get("instance_id") == instance_id
                and entry.get("device_id") == dev_id
                and entry.get("status") in ("injected", "force_cleanup")
                and (entry.get("game") == game or "game" not in entry)):
            entry["status"]      = new_status
            entry["restored_at"] = datetime.now(timezone.utc).isoformat()
            break

    save_keys(keys)
    log.info(f"Restore {'OK' if restore_ok else 'FAILED→cleanup'}: key={key[:10]}… inst={instance_id} game={game}")

    return web.json_response({
        "ok":           True,
        "instance_id":  instance_id,
        "game":         game,
        "force_delete": not restore_ok,
        "message":      "Restore failed — delete the injected file" if not restore_ok else "Restored OK",
    })


async def api_inject_cleanup(request):
    """POST /inject/cleanup"""
    try:
        body = await request.json()
    except Exception:
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
            kd           = keys[key]
            active_key   = f"{dev_id}:{instance_id}:{game}"
            active       = kd.setdefault("active_injects", {})
            final_status = "cleaned" if deleted else "cleanup_failed"

            if active_key in active:
                active[active_key]["status"]     = final_status
                active[active_key]["cleaned_at"] = datetime.now(timezone.utc).isoformat()
                if delete_error:
                    active[active_key]["delete_error"] = delete_error
            else:
                for ak, av in active.items():
                    if (av.get("instance_id") == instance_id
                            and av.get("device_id") == dev_id
                            and av.get("status") in ("injected", "force_cleanup", "restored")):
                        av["status"]     = final_status
                        av["cleaned_at"] = datetime.now(timezone.utc).isoformat()
                        if delete_error:
                            av["delete_error"] = delete_error
                        break

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

    log.info(f"Inject cleanup: inst={instance_id} game={game} deleted={deleted}")
    return web.json_response({
        "ok": True, "cleaned": True, "deleted": deleted,
        "instance_id": instance_id, "game": game,
    })


async def api_report_rules(request):
    """POST /inject/report_rules"""
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

    instances = load_instances()
    changed   = False
    for inst in instances:
        if inst["id"] == instance_id:
            if game == "ff":
                inst["rule_count_ff"]    = rule_count
            elif game == "ffmax":
                inst["rule_count_ffmax"] = rule_count
            else:
                inst["rule_count"]       = rule_count
            total_rules      = max(
                inst.get("rule_count_ff",    inst.get("rule_count", 1)),
                inst.get("rule_count_ffmax", inst.get("rule_count", 1)),
            )
            inst["multi_inject"] = (total_rules > 1)
            changed = True
            break

    if changed:
        save_instances(instances)

    inst         = find_instance(instance_id)
    multi_inject = bool(inst and inst.get("multi_inject", False)) if inst else False

    log.info(f"Rules reported: inst={instance_id} game={game} rule_count={rule_count} multi={multi_inject}")
    return web.json_response({
        "ok": True, "instance_id": instance_id,
        "game": game, "rule_count": rule_count,
        "multi_inject": multi_inject,
    })


async def api_patch_templates(request):
    """GET /patch/templates"""
    key = request.headers.get("X-Key", "").strip().upper()
    if key:
        keys = load_keys()
        if key not in keys or not keys[key].get("owner"):
            return web.json_response({"ok": False, "error": "owner key required"}, status=403)

    templates = [
        {"id": "cache_res",    "label": "Cache Res (Compulsory)",      "path": MXCF_PATH_CACHE_RES,    "desc": "Main game asset bundles"},
        {"id": "shaders",      "label": "Shaders (Optional)",          "path": MXCF_PATH_SHADERS,      "desc": "Shader bundles"},
        {"id": "assetindexer", "label": "Asset Indexer (Compulsory)",  "path": MXCF_PATH_ASSETINDEXER, "desc": "Avatar asset bundles"},
        {"id": "plist",        "label": "Plist (Preferences)",         "path": MXCF_PATH_PLIST,        "desc": "Settings plist files"},
        {"id": "mod_avatar",   "label": "Mod Avatar (Optional)",       "path": MXCF_PATH_MODAVATAR,    "desc": "Optional avatar bundles"},
    ]
    return web.json_response({"ok": True, "templates": templates})


async def api_patch_build(request):
    """POST /patch/build"""
    import base64 as _b64
    import plistlib as _plist

    key = request.headers.get("X-Key", "").strip().upper()
    if not key:
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

    built_rules = []
    for idx, rule in enumerate(rules):
        fname       = rule.get("file_name", "").strip()
        inject_path = rule.get("inject_path", "").strip() or _resolve_inject_path(fname)
        data_b64    = rule.get("data_b64", "")
        if not fname:
            return web.json_response({"ok": False, "error": f"rule {idx}: missing file_name"}, status=400)
        if not data_b64:
            return web.json_response({"ok": False, "error": f"rule {idx}: missing data_b64"}, status=400)
        try:
            file_data = _b64.b64decode(data_b64)
        except Exception:
            return web.json_response({"ok": False, "error": f"rule {idx}: invalid base64"}, status=400)
        built_rules.append({"fileName": fname, "injectPath": inject_path, "data": file_data})

    pkg_id          = str(_uuid_mod.uuid4()).upper()
    content_key     = os.urandom(32)
    key_fingerprint = hashlib.sha256(content_key).digest()

    plist_rules = [
        {"fileName": r["fileName"], "injectPath": r["injectPath"], "fileData": r["data"]}
        for r in built_rules
    ]
    plist_obj = {
        "schemaVersion":       2,
        "packageID":           pkg_id,
        "patchName":           patch_name,
        "bundleID":            bundle_id,
        "ruleCount":           len(built_rules),
        "rules":               plist_rules,
        "isPasswordProtected": False,
        "publicContentKey":    content_key,
        "keyFingerprint":      key_fingerprint,
        "builtAt":             datetime.now(timezone.utc).isoformat(),
        "builtBy":             str(kd.get("created_by", "owner")),
    }
    plist_data = _plist.dumps(plist_obj, fmt=_plist.FMT_BINARY)
    mxff_data  = MXCF_MAGIC + b"\x00" + plist_data

    safe_name = "".join(c for c in patch_name if c.isalnum() or c in "-_").lower()
    out_fname = f"built_{safe_name}_{pkg_id[:8].lower()}.mxff"
    out_path  = PATCH_DIR / out_fname
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


async def patch_file(request):
    """GET /api/patch/{instance_id}"""
    instance_id = request.match_info.get("instance_id", "")
    if not instance_id:
        return web.Response(status=400, text="Missing instance_id")

    key  = request.headers.get("X-Key", "").strip().upper()
    game = (request.headers.get("X-Game", "") or request.rel_url.query.get("game", "ff")).strip().lower()
    if game not in ("ff", "ffmax"):
        game = "ff"

    key_is_owner = False
    if key:
        keys = load_keys()
        if key not in keys:
            return web.Response(status=403, text="Invalid key")
        kd = keys[key]
        if key_status(kd) != "active":
            return web.Response(status=403, text="Key inactive")
        key_is_owner = bool(kd.get("owner"))
        if not key_is_owner and instance_id not in kd.get("instances", []):
            return web.Response(status=403, text="Instance not in key plan")

    candidates = []
    inst       = find_instance(instance_id)
    if inst:
        game_field = "patch_file_ff" if game == "ff" else "patch_file_ffmax"
        if inst.get(game_field):
            candidates.append(PATCH_DIR / inst[game_field])
        if inst.get("patch_file"):
            candidates.append(PATCH_DIR / inst["patch_file"])

    for path in [
        PATCH_DIR / f"{instance_id}_{game}.mxff",
        PATCH_DIR / f"{instance_id}.mxff",
    ]:
        if path not in candidates:
            candidates.append(path)

    serve_path = next((p for p in candidates if p.exists()), None)
    if serve_path is None:
        log.warning(f"patch_file: NOT FOUND {instance_id} game={game}")
        return web.Response(status=404, text=f"Patch not found: {instance_id} (game={game})")

    data = serve_path.read_bytes()
    if not is_mxff(data):
        log.warning(f"patch_file: {serve_path.name} invalid format")
        return web.Response(status=422, text="Invalid patch format")

    log.info(f"patch_file: served {serve_path.name} ({len(data)} bytes) game={game}")
    return web.Response(
        body=data,
        content_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{serve_path.name}"'},
    )


# ══════════════════════════════════════════════════════════════════════════════
# APP STARTUP
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PATCH_DIR.mkdir(parents=True, exist_ok=True)

    # Gist থেকে latest data load করো
    await gist_load()
    seed_builtin_instances()

    # Background sync task — প্রতি ৩০ সেকেন্ডে Gist থেকে নতুন data নেবে
    asyncio.create_task(gist_sync_loop())

    app = web.Application()

    # ── Routes ────────────────────────────────────────────────────────────────
    app.router.add_get( "/health",                health)
    app.router.add_post("/api/validate",          api_validate)
    app.router.add_post("/validate",              api_validate)       # backward-compat
    app.router.add_get( "/api/patch/{instance_id}", patch_file)
    app.router.add_post("/inject/confirm",        api_inject_confirm)
    app.router.add_post("/inject/restore",        api_inject_restore)
    app.router.add_post("/inject/cleanup",        api_inject_cleanup)
    app.router.add_post("/inject/report_rules",   api_report_rules)
    app.router.add_post("/patch/build",           api_patch_build)
    app.router.add_get( "/patch/templates",       api_patch_templates)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    log.info(f"FOUGO Backend running on port {PORT}")
    log.info(f"Data dir: {DATA_DIR.resolve()}")
    log.info("Routes: /api/validate | /api/patch/<id> | /inject/* | /patch/*")

    # Keep running forever
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
