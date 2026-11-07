import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import discord
from discord import app_commands

TOKEN = os.environ["DISCORD_TOKEN"]
# Use DB_PATH if set; otherwise use the Railway volume if one is attached; otherwise the current folder.
DB_PATH = os.environ.get("DB_PATH") or os.path.join(
    os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "."), "robux.db"
)
print(f"[startup] Database path: {os.path.abspath(DB_PATH)}")
print(f"[startup] Volume mount path: {os.environ.get('RAILWAY_VOLUME_MOUNT_PATH')}")
print(f"[startup] Database file already exists: {os.path.exists(DB_PATH)}")

# Robux earned per counted message (default 0.0125 = 1/80 Robux).
ROBUX_PER_MESSAGE = Decimal(os.environ.get("ROBUX_PER_MESSAGE", "0.0125"))
# Anti-spam: a message only counts if this many seconds passed since the last counted one.
COOLDOWN_SECONDS = float(os.environ.get("COOLDOWN_SECONDS", "5"))
# Timezone used when staff type an exact deadline like "2026-11-08 02:35" (default IST, UTC+5:30).
# Discord shows the final timestamp in each user's own timezone automatically.
UTC_OFFSET_MINUTES = int(os.environ.get("UTC_OFFSET_MINUTES", "330"))
LOCAL_TZ = timezone(timedelta(minutes=UTC_OFFSET_MINUTES))

# Balances are stored as integers: 1 Robux = 10,000 units. No floating-point errors.
UNITS_PER_ROBUX = 10_000
UNITS_PER_MESSAGE = int(ROBUX_PER_MESSAGE * UNITS_PER_ROBUX)

EMBED_COLOR = discord.Color.from_rgb(0, 176, 111)

db = sqlite3.connect(DB_PATH)
db.execute(
    """
    CREATE TABLE IF NOT EXISTS balances (
        guild_id   INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        units      INTEGER NOT NULL DEFAULT 0,
        msgs       INTEGER NOT NULL DEFAULT 0,
        last_count REAL    NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    )
    """
)
# One-time migration from the previous version (0.025 Robux per message = 250 units).
try:
    db.execute(
        """
        INSERT OR IGNORE INTO balances (guild_id, user_id, units, msgs, last_count)
        SELECT guild_id, user_id, units * 250, units, last_count FROM wallets
        """
    )
except sqlite3.OperationalError:
    pass  # no old table, nothing to migrate
db.commit()


def fmt(units: int) -> str:
    return f"{units / UNITS_PER_ROBUX:,.4f}"


def register_message(guild_id: int, user_id: int) -> None:
    now = time.time()
    row = db.execute(
        "SELECT last_count FROM balances WHERE guild_id=? AND user_id=?",
        (guild_id, user_id),
    ).fetchone()
    if row and now - row[0] < COOLDOWN_SECONDS:
        return
    db.execute(
        """
        INSERT INTO balances (guild_id, user_id, units, msgs, last_count)
        VALUES (?, ?, ?, 1, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            units = units + excluded.units,
            msgs = msgs + 1,
            last_count = excluded.last_count
        """,
        (guild_id, user_id, UNITS_PER_MESSAGE, now),
    )
    db.commit()


def progress_bar(units: int, length: int = 10) -> str:
    filled = int((units % UNITS_PER_ROBUX) / UNITS_PER_ROBUX * length)
    return "▰" * filled + "▱" * (length - filled)


def parse_deadline(text: str) -> datetime | None:
    """Accepts a duration ("18h", "2d 6h", "30m") or an exact time ("2026-11-08 02:35")."""
    text = text.strip().lower()

    m = re.fullmatch(r"(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?", text)
    if m and any(m.groups()):
        days, hours, minutes = (int(x or 0) for x in m.groups())
        delta = timedelta(days=days, hours=hours, minutes=minutes)
        if delta.total_seconds() <= 0:
            return None
        return datetime.now(timezone.utc) + delta

    for pattern in ("%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=LOCAL_TZ)
        except ValueError:
            continue
    return None


class RobuxBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()  # no privileged intents needed
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        synced = await self.tree.sync()
        print(f"[startup] Synced commands: {[c.name for c in synced]}")


bot = RobuxBot()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return
    register_message(message.guild.id, message.author.id)


@bot.tree.command(name="balance", description="Check your (or someone's) Robux balance")
@app_commands.describe(user="User to check (defaults to you)")
async def balance(interaction: discord.Interaction, user: discord.Member | None = None):
    user = user or interaction.user
    row = db.execute(
        "SELECT units, msgs FROM balances WHERE guild_id=? AND user_id=?",
        (interaction.guild_id, user.id),
    ).fetchone()
    units, msgs = row if row else (0, 0)

    embed = discord.Embed(title=f"{user.display_name}'s Wallet", color=EMBED_COLOR)
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="Balance", value=f"**{fmt(units)}** Robux", inline=True)
    embed.add_field(name="Messages counted", value=f"{msgs:,}", inline=True)
    embed.add_field(
        name="Progress to next whole Robux",
        value=f"{progress_bar(units)}  {(units % UNITS_PER_ROBUX) / UNITS_PER_ROBUX:.0%}",
        inline=False,
    )
    embed.set_footer(text=f"Earn {ROBUX_PER_MESSAGE} Robux per message")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leaderboard", description="Top Robux earners in this server")
async def leaderboard(interaction: discord.Interaction):
    rows = db.execute(
        "SELECT user_id, units FROM balances WHERE guild_id=? ORDER BY units DESC LIMIT 10",
        (interaction.guild_id,),
    ).fetchall()

    embed = discord.Embed(title="Robux Leaderboard", color=EMBED_COLOR)
    if not rows:
        embed.description = "Nobody has earned anything yet. Start chatting!"
    else:
        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{uid}> — **{fmt(u)}** Robux"
            for i, (uid, u) in enumerate(rows)
        ]
        embed.description = "\n".join(lines)
    embed.set_footer(text=f"Earn {ROBUX_PER_MESSAGE} Robux per message")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="dm", description="DM a giveaway winner with their claim deadline")
@app_commands.describe(
    user="The winner",
    deadline="Claim deadline: a duration like 18h, 2d 6h, 30m, or an exact time like 2026-11-08 02:35",
    prize="What they won, e.g. 5 Robux (optional)",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def dm(
    interaction: discord.Interaction,
    user: discord.Member,
    deadline: str,
    prize: app_commands.Range[str, 1, 100] | None = None,
):
    # Extra safety on top of the default permission, in case server settings changed it.
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You need the Manage Server permission to use this.", ephemeral=True
        )
        return

    if user.bot:
        await interaction.response.send_message("You can't DM a bot.", ephemeral=True)
        return

    deadline_dt = parse_deadline(deadline)
    if deadline_dt is None:
        await interaction.response.send_message(
            "I couldn't understand that deadline. Use a duration like `18h`, `2d 6h` or `30m`, "
            "or an exact time like `2026-11-08 02:35`.",
            ephemeral=True,
        )
        return
    if deadline_dt <= datetime.now(timezone.utc):
        await interaction.response.send_message(
            "That deadline is in the past.", ephemeral=True
        )
        return

    ts = int(deadline_dt.timestamp())
    title = f"You won {prize.strip()}!" if prize and prize.strip() else "You won a giveaway!"

    embed = discord.Embed(
        title=title,
        description=(
            f"⏰ **Your personal claim deadline:** <t:{ts}:F> · <t:{ts}:R>\n\n"
            "🎫 Open a **Giveaway Claims** support ticket before your deadline. "
            "Once claimed on time, staff can deliver your prize afterward.\n\n"
            "⚠️ Never send passwords, cookies, 2FA codes or account tokens."
        ),
        color=EMBED_COLOR,
    )

    try:
        await user.send(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message(
            f"I couldn't DM {user.mention}. They probably have DMs from server members turned off.",
            ephemeral=True,
        )
        return
    except discord.HTTPException as e:
        await interaction.response.send_message(f"Failed to send the DM: {e}", ephemeral=True)
        return

    await interaction.response.send_message(
        f"Sent to {user.mention}: **{title}** Deadline: <t:{ts}:F> (<t:{ts}:R>).",
        allowed_mentions=discord.AllowedMentions.none(),
    )


bot.run(TOKEN)
