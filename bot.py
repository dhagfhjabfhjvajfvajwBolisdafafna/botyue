"""
FOCUSS TWEAKS — License Key Discord Bot
=========================================================================
Single-file discord.py bot that manages Focuss Tweaks license keys stored
in Supabase. Slash-command only, admin-restricted, ephemeral replies for
anything containing a key.

Setup:
    1. pip install -r requirements.txt
    2. Copy .env.example to .env and fill in your values
    3. Run the schema in supabase/schema.sql against your Supabase project
    4. python bot.py

This bot authenticates to Supabase with the SERVICE ROLE key, which
bypasses Row Level Security and has full read/write access to the
licenses table. Keep that key secret — it must never be shipped inside
the Electron app. The Electron app only ever talks to the public anon
key + the validate_license() SQL function (see licenseManager.js).
=========================================================================
"""

import os
import io
import logging
import secrets
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Literal

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from supabase import create_client, Client


# ═════════════════════════════════════════════════════════════════════════
# CONFIG
# ═════════════════════════════════════════════════════════════════════════
load_dotenv()

DISCORD_TOKEN         = os.getenv("DISCORD_TOKEN", "").strip()
SUPABASE_URL          = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY  = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
_GUILD_ID_RAW         = os.getenv("GUILD_ID", "").strip()
GUILD_ID              = int(_GUILD_ID_RAW) if _GUILD_ID_RAW.isdigit() else None
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_DISCORD_IDS", "").replace(" ", "").split(",")
    if x.isdigit()
}

PAGE_SIZE = 10


# ═════════════════════════════════════════════════════════════════════════
# LOGGING
# ═════════════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("focuss-bot")


# ═════════════════════════════════════════════════════════════════════════
# SUPABASE CLIENT (service role — full admin access, bypasses RLS)
# ═════════════════════════════════════════════════════════════════════════
supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_SERVICE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
else:
    log.critical("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing — set them in .env")


async def db(fn):
    """Run a blocking supabase-py call in a worker thread so it never blocks the bot's event loop."""
    return await asyncio.to_thread(fn)


# ═════════════════════════════════════════════════════════════════════════
# DISCORD BOT
# ═════════════════════════════════════════════════════════════════════════
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

PLAN_COLORS = {
    "basic": discord.Color.light_grey(),
    "premium": discord.Color.gold(),
}


# ═════════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════════
# No 0/O/1/I — avoids keys that are hard to read or type out loud
KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_key() -> str:
    """Generate a key in the form FOCUSS-XXXX-XXXX-XXXX."""
    groups = ["".join(secrets.choice(KEY_ALPHABET) for _ in range(4)) for _ in range(3)]
    return "FOCUSS-" + "-".join(groups)


def mask_key(key: str) -> str:
    """Turn FOCUSS-AB3D-EF9K-22ZZ into FOCUSS-AB3D-****-**** for safe display."""
    parts = key.split("-")
    if len(parts) != 4:
        return key
    return f"{parts[0]}-{parts[1]}-****-****"


def normalize_key(key: str) -> str:
    return key.strip().upper()


def fmt_dt(value: Optional[str]) -> str:
    if not value:
        return "Lifetime"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return f"<t:{int(dt.timestamp())}:f>"
    except Exception:
        return value


def admin_only():
    """Slash-command check: only Discord user IDs listed in ADMIN_DISCORD_IDS may proceed."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id not in ADMIN_IDS:
            raise app_commands.CheckFailure("not_admin")
        return True
    return app_commands.check(predicate)


def require_supabase():
    if supabase is None:
        raise RuntimeError("Supabase is not configured. Check SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env.")


async def find_license(key: str) -> Optional[dict]:
    """Fetch a single license row by exact key, or None if it doesn't exist."""
    res = await db(lambda: supabase.table("licenses").select("*").eq("key", key).limit(1).execute())
    return res.data[0] if res.data else None


# ═════════════════════════════════════════════════════════════════════════
# COMMANDS
# ═════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="genkey", description="Generate one or more Focuss Tweaks license keys")
@app_commands.describe(
    plan="License plan to generate",
    duration_days="Days until expiry (0 = lifetime key)",
    amount="How many keys to generate at once (max 25)",
    max_devices="How many different PCs may use each key",
    note="Optional note stored with the key(s)",
)
@admin_only()
async def genkey(
    interaction: discord.Interaction,
    plan: Literal["basic", "premium"],
    duration_days: app_commands.Range[int, 0, 3650] = 0,
    amount: app_commands.Range[int, 1, 25] = 1,
    max_devices: app_commands.Range[int, 1, 10] = 1,
    note: Optional[str] = None,
):
    require_supabase()
    await interaction.response.defer(ephemeral=True, thinking=True)

    expires_at = None
    if duration_days > 0:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=duration_days)).isoformat()

    keys = [generate_key() for _ in range(amount)]
    rows = [
        {
            "key": k,
            "key_preview": mask_key(k),
            "plan": plan,
            "status": "active",
            "expires_at": expires_at,
            "created_by_discord_id": str(interaction.user.id),
            "max_devices": max_devices,
            "notes": note,
        }
        for k in keys
    ]

    try:
        await db(lambda: supabase.table("licenses").insert(rows).execute())
    except Exception as e:
        log.exception("genkey: insert failed")
        await interaction.followup.send(f"❌ Failed to create key(s): `{e}`", ephemeral=True)
        return

    log.info(
        "[GENKEY] %s (%s) generated %dx %s key(s) | %sd | max_devices=%d",
        interaction.user, interaction.user.id, amount, plan,
        duration_days if duration_days else "lifetime", max_devices,
    )

    expiry_txt = "Lifetime" if duration_days == 0 else f"{duration_days} days ({fmt_dt(expires_at)})"
    embed = discord.Embed(
        title=f"✅ Generated {amount}x {plan.upper()} key{'s' if amount != 1 else ''}",
        color=PLAN_COLORS[plan],
    )
    embed.add_field(name="Expiry", value=expiry_txt, inline=True)
    embed.add_field(name="Max devices", value=str(max_devices), inline=True)
    if note:
        embed.add_field(name="Note", value=note, inline=False)

    keys_block = "\n".join(keys)
    if len(keys_block) > 900:
        file = discord.File(fp=io.BytesIO(keys_block.encode("utf-8")), filename="focuss_keys.txt")
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)
    else:
        embed.add_field(name="Key(s)", value=f"```\n{keys_block}\n```", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="ban", description="Ban a license key")
@app_commands.describe(key="The license key to ban", reason="Reason for the ban")
@admin_only()
async def ban(interaction: discord.Interaction, key: str, reason: str):
    require_supabase()
    await interaction.response.defer(ephemeral=True)
    key = normalize_key(key)

    res = await db(lambda: supabase.table("licenses")
                    .update({"is_banned": True, "ban_reason": reason})
                    .eq("key", key).execute())
    if not res.data:
        await interaction.followup.send("❌ No license found with that key.", ephemeral=True)
        return

    log.info("[BAN] %s (%s) banned %s — %s", interaction.user, interaction.user.id, mask_key(key), reason)
    embed = discord.Embed(title="🔨 Key Banned", color=discord.Color.red())
    embed.add_field(name="Key", value=mask_key(key), inline=False)
    embed.add_field(name="Reason", value=reason, inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="unban", description="Unban a license key")
@app_commands.describe(key="The license key to unban")
@admin_only()
async def unban(interaction: discord.Interaction, key: str):
    require_supabase()
    await interaction.response.defer(ephemeral=True)
    key = normalize_key(key)

    res = await db(lambda: supabase.table("licenses")
                    .update({"is_banned": False, "ban_reason": None})
                    .eq("key", key).execute())
    if not res.data:
        await interaction.followup.send("❌ No license found with that key.", ephemeral=True)
        return

    log.info("[UNBAN] %s (%s) unbanned %s", interaction.user, interaction.user.id, mask_key(key))
    await interaction.followup.send(f"✅ Key `{mask_key(key)}` has been unbanned.", ephemeral=True)


@bot.tree.command(name="deletekey", description="Permanently delete a license key")
@app_commands.describe(key="The license key to delete")
@admin_only()
async def deletekey(interaction: discord.Interaction, key: str):
    require_supabase()
    await interaction.response.defer(ephemeral=True)
    key = normalize_key(key)

    existing = await find_license(key)
    if not existing:
        await interaction.followup.send("❌ No license found with that key.", ephemeral=True)
        return

    await db(lambda: supabase.table("licenses").delete().eq("key", key).execute())
    log.info("[DELETEKEY] %s (%s) deleted %s", interaction.user, interaction.user.id, mask_key(key))
    await interaction.followup.send(f"🗑️ Key `{mask_key(key)}` has been permanently deleted.", ephemeral=True)


@bot.tree.command(name="list", description="List recent license keys")
@app_commands.describe(
    plan="Filter by plan",
    status="Filter by status",
    page="Page number (10 keys per page)",
)
@admin_only()
async def list_keys(
    interaction: discord.Interaction,
    plan: Optional[Literal["basic", "premium"]] = None,
    status: Optional[Literal["active", "disabled"]] = None,
    page: app_commands.Range[int, 1, 999] = 1,
):
    require_supabase()
    await interaction.response.defer(ephemeral=True)

    def query():
        q = supabase.table("licenses").select(
            "key_preview,plan,status,is_banned,created_at,expires_at,uses"
        ).order("created_at", desc=True)
        if plan:
            q = q.eq("plan", plan)
        if status:
            q = q.eq("status", status)
        start = (page - 1) * PAGE_SIZE
        end = start + PAGE_SIZE - 1
        return q.range(start, end).execute()

    res = await db(query)
    rows = res.data or []
    if not rows:
        await interaction.followup.send("No keys found for that filter/page.", ephemeral=True)
        return

    filters = []
    if plan:
        filters.append(f"plan={plan}")
    if status:
        filters.append(f"status={status}")
    subtitle = f" ({', '.join(filters)})" if filters else ""

    embed = discord.Embed(title=f"📋 License Keys — page {page}{subtitle}", color=discord.Color.blurple())
    for r in rows:
        flag = "🚫 BANNED" if r["is_banned"] else r["status"].upper()
        embed.add_field(
            name=r["key_preview"],
            value=f"Plan: **{r['plan']}**  ·  {flag}\nExpires: {fmt_dt(r['expires_at'])}  ·  Uses: {r['uses']}",
            inline=False,
        )
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="lookup", description="Show full information about a license key")
@app_commands.describe(key="The license key to look up")
@admin_only()
async def lookup(interaction: discord.Interaction, key: str):
    require_supabase()
    await interaction.response.defer(ephemeral=True)
    key = normalize_key(key)

    row = await find_license(key)
    if not row:
        await interaction.followup.send("❌ No license found with that key.", ephemeral=True)
        return

    devices_res = await db(lambda: supabase.table("license_devices")
                            .select("hwid_hash", count="exact")
                            .eq("license_id", row["id"]).execute())
    device_count = devices_res.count or 0

    embed = discord.Embed(title=f"🔎 {row['key_preview']}", color=PLAN_COLORS.get(row["plan"], discord.Color.default()))
    embed.add_field(name="Plan", value=row["plan"], inline=True)
    embed.add_field(name="Status", value=row["status"], inline=True)
    embed.add_field(name="Banned", value=(f"Yes — {row['ban_reason']}" if row["is_banned"] else "No"), inline=True)
    embed.add_field(name="Created", value=fmt_dt(row["created_at"]), inline=True)
    embed.add_field(name="Expires", value=fmt_dt(row["expires_at"]), inline=True)
    embed.add_field(name="Uses", value=str(row["uses"]), inline=True)
    embed.add_field(name="Devices", value=f"{device_count}/{row['max_devices']}", inline=True)
    embed.add_field(
        name="Created by",
        value=(f"<@{row['created_by_discord_id']}>" if row["created_by_discord_id"] else "Unknown"),
        inline=True,
    )
    embed.add_field(
        name="Claimed by",
        value=(f"<@{row['claimed_by_discord_id']}>" if row["claimed_by_discord_id"] else "—"),
        inline=True,
    )
    embed.add_field(name="Last used", value=fmt_dt(row["last_used_at"]), inline=False)
    if row["notes"]:
        embed.add_field(name="Notes", value=row["notes"], inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="extend", description="Extend a license key's expiry")
@app_commands.describe(key="The license key", days="Number of days to add")
@admin_only()
async def extend(interaction: discord.Interaction, key: str, days: app_commands.Range[int, 1, 3650]):
    require_supabase()
    await interaction.response.defer(ephemeral=True)
    key = normalize_key(key)

    row = await find_license(key)
    if not row:
        await interaction.followup.send("❌ No license found with that key.", ephemeral=True)
        return

    current = row.get("expires_at")
    now = datetime.now(timezone.utc)
    if current:
        base = datetime.fromisoformat(current.replace("Z", "+00:00"))
        if base < now:
            base = now
    else:
        # Lifetime key being extended just gets a fresh expiry from today
        base = now

    new_expiry = base + timedelta(days=days)
    await db(lambda: supabase.table("licenses")
             .update({"expires_at": new_expiry.isoformat()})
             .eq("key", key).execute())

    log.info("[EXTEND] %s (%s) extended %s by %dd -> %s",
              interaction.user, interaction.user.id, mask_key(key), days, new_expiry.isoformat())

    embed = discord.Embed(title="⏳ Key Extended", color=discord.Color.green())
    embed.add_field(name="Key", value=mask_key(key), inline=False)
    embed.add_field(name="New expiry", value=fmt_dt(new_expiry.isoformat()), inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="resethwid", description="Reset the device/HWID lock for a key")
@app_commands.describe(key="The license key")
@admin_only()
async def resethwid(interaction: discord.Interaction, key: str):
    require_supabase()
    await interaction.response.defer(ephemeral=True)
    key = normalize_key(key)

    row = await find_license(key)
    if not row:
        await interaction.followup.send("❌ No license found with that key.", ephemeral=True)
        return

    await db(lambda: supabase.table("license_devices").delete().eq("license_id", row["id"]).execute())
    await db(lambda: supabase.table("licenses").update({"hwid_hash": None}).eq("id", row["id"]).execute())

    log.info("[RESETHWID] %s (%s) reset devices for %s", interaction.user, interaction.user.id, mask_key(key))
    await interaction.followup.send(
        f"✅ Device lock reset for `{mask_key(key)}`. It can now be activated on a new PC.", ephemeral=True
    )


@bot.tree.command(name="stats", description="Show license key statistics")
@admin_only()
async def stats(interaction: discord.Interaction):
    require_supabase()
    await interaction.response.defer(ephemeral=True)

    now_iso = datetime.now(timezone.utc).isoformat()

    total   = await db(lambda: supabase.table("licenses").select("id", count="exact").execute())
    active  = await db(lambda: supabase.table("licenses").select("id", count="exact")
                        .eq("status", "active").eq("is_banned", False).execute())
    banned  = await db(lambda: supabase.table("licenses").select("id", count="exact")
                        .eq("is_banned", True).execute())
    expired = await db(lambda: supabase.table("licenses").select("id", count="exact")
                        .lt("expires_at", now_iso).execute())
    basic   = await db(lambda: supabase.table("licenses").select("id", count="exact")
                        .eq("plan", "basic").execute())
    premium = await db(lambda: supabase.table("licenses").select("id", count="exact")
                        .eq("plan", "premium").execute())

    embed = discord.Embed(title="📊 Focuss Tweaks — License Stats", color=discord.Color.blurple())
    embed.add_field(name="Total keys", value=str(total.count or 0), inline=True)
    embed.add_field(name="Active", value=str(active.count or 0), inline=True)
    embed.add_field(name="Banned", value=str(banned.count or 0), inline=True)
    embed.add_field(name="Expired", value=str(expired.count or 0), inline=True)
    embed.add_field(name="Basic", value=str(basic.count or 0), inline=True)
    embed.add_field(name="Premium", value=str(premium.count or 0), inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="help", description="Show all Focuss Tweaks bot commands")
@admin_only()
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🛠️ Focuss Tweaks — License Bot Commands",
        description="All commands are admin-only and reply privately (ephemeral).",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="/genkey", value="Generate license key(s) — `plan`, `duration_days`, `amount`, `max_devices`, `note`", inline=False)
    embed.add_field(name="/ban", value="Ban a key — `key`, `reason`", inline=False)
    embed.add_field(name="/unban", value="Unban a key — `key`", inline=False)
    embed.add_field(name="/deletekey", value="Permanently delete a key — `key`", inline=False)
    embed.add_field(name="/list", value="List recent keys — `plan`, `status`, `page`", inline=False)
    embed.add_field(name="/lookup", value="Show full details for a key — `key`", inline=False)
    embed.add_field(name="/extend", value="Extend a key's expiry — `key`, `days`", inline=False)
    embed.add_field(name="/resethwid", value="Reset the device lock on a key — `key`", inline=False)
    embed.add_field(name="/stats", value="Show totals: active, banned, expired, basic, premium", inline=False)
    embed.add_field(name="/help", value="Show this message", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ═════════════════════════════════════════════════════════════════════════
# ERROR HANDLING
# ═════════════════════════════════════════════════════════════════════════
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        msg = "🚫 You do not have permission to use this command."
    else:
        log.exception("Unhandled app command error", exc_info=error)
        msg = f"⚠️ Something went wrong: `{error}`"

    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        log.exception("Failed to deliver error message to user")


# ═════════════════════════════════════════════════════════════════════════
# STARTUP
# ═════════════════════════════════════════════════════════════════════════
@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "?")
    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            log.info("Synced %d slash command(s) to guild %d (instant)", len(synced), GUILD_ID)
        else:
            synced = await bot.tree.sync()
            log.info("Synced %d slash command(s) globally (can take up to ~1h to propagate)", len(synced))
    except Exception:
        log.exception("Failed to sync slash commands")


def main():
    if not DISCORD_TOKEN:
        log.critical("DISCORD_TOKEN is missing from .env — aborting.")
        return
    if supabase is None:
        log.critical("Supabase is not configured — aborting.")
        return
    if not ADMIN_IDS:
        log.warning("ADMIN_DISCORD_IDS is empty — nobody will be able to use any admin command!")

    bot.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
