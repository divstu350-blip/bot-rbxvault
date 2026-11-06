import os
import sqlite3
import time

import discord
from discord import app_commands

TOKEN = os.environ["MTU2ODI4NTM4MjUzNjA3NzQ1Mg.GZEUxb.M4QdqLxfOnBJzw2QVivSW31vX-0-4mwluVpGlw"]
DB_PATH = os.environ.get("DB_PATH", "robux.db")
MESSAGES_PER_ROBUX = int(os.environ.get("MESSAGES_PER_ROBUX", "40"))
# Anti-spam: a message only counts if this many seconds passed since the last counted one.
COOLDOWN_SECONDS = float(os.environ.get("COOLDOWN_SECONDS", "5"))

db = sqlite3.connect(DB_PATH)
db.execute(
    """
    CREATE TABLE IF NOT EXISTS users (
        guild_id   INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        progress   INTEGER NOT NULL DEFAULT 0,   -- messages toward next Robux
        robux      INTEGER NOT NULL DEFAULT 0,
        total_msgs INTEGER NOT NULL DEFAULT 0,
        last_count REAL    NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    )
    """
)
db.commit()


def register_message(guild_id: int, user_id: int) -> bool:
    """Count a message. Returns True if the user just earned a Robux."""
    now = time.time()
    row = db.execute(
        "SELECT progress, robux, total_msgs, last_count FROM users WHERE guild_id=? AND user_id=?",
        (guild_id, user_id),
    ).fetchone()
    progress, robux, total, last = row if row else (0, 0, 0, 0.0)

    if now - last < COOLDOWN_SECONDS:
        return False

    progress += 1
    total += 1
    earned = False
    if progress >= MESSAGES_PER_ROBUX:
        progress = 0
        robux += 1
        earned = True

    db.execute(
        """
        INSERT INTO users (guild_id, user_id, progress, robux, total_msgs, last_count)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            progress=excluded.progress, robux=excluded.robux,
            total_msgs=excluded.total_msgs, last_count=excluded.last_count
        """,
        (guild_id, user_id, progress, robux, total, now),
    )
    db.commit()
    return earned


class RobuxBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()  # no privileged intents needed
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()


bot = RobuxBot()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return
    if register_message(message.guild.id, message.author.id):
        try:
            await message.add_reaction("💰")
        except discord.HTTPException:
            pass


@bot.tree.command(name="balance", description="Check your (or someone's) Robux balance")
@app_commands.describe(user="User to check (defaults to you)")
async def balance(interaction: discord.Interaction, user: discord.Member | None = None):
    user = user or interaction.user
    row = db.execute(
        "SELECT robux, progress, total_msgs FROM users WHERE guild_id=? AND user_id=?",
        (interaction.guild_id, user.id),
    ).fetchone()
    robux, progress, total = row if row else (0, 0, 0)
    await interaction.response.send_message(
        f"**{user.display_name}** has **{robux} Robux**\n"
        f"Progress: {progress}/{MESSAGES_PER_ROBUX} messages to the next one "
        f"({total} counted messages total)."
    )


@bot.tree.command(name="leaderboard", description="Top Robux earners in this server")
async def leaderboard(interaction: discord.Interaction):
    rows = db.execute(
        "SELECT user_id, robux FROM users WHERE guild_id=? ORDER BY robux DESC, total_msgs DESC LIMIT 10",
        (interaction.guild_id,),
    ).fetchall()
    if not rows:
        await interaction.response.send_message("Nobody has earned anything yet.")
        return
    lines = [f"{i}. <@{uid}> — {rx} Robux" for i, (uid, rx) in enumerate(rows, 1)]
    await interaction.response.send_message(
        "**Leaderboard**\n" + "\n".join(lines),
        allowed_mentions=discord.AllowedMentions.none(),
    )


bot.run(TOKEN)
