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
# Log of every time an admin resets a balance after a redemption.
db.execute(
    """
    CREATE TABLE IF NOT EXISTS redemptions (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id   INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        units      INTEGER NOT NULL,
        admin_id   INTEGER NOT NULL,
        created_at INTEGER NOT NULL
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
        intents = discord.Intents.default()
        intents.members = True  # privileged intent: needed to detect new members joining
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


WELCOME_CHANNEL_ID = int(os.environ.get("WELCOME_CHANNEL_ID", "1568330851291504650"))


@bot.event
async def on_member_join(member: discord.Member):
    if member.bot:
        return
    channel = bot.get_channel(WELCOME_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(WELCOME_CHANNEL_ID)
        except discord.HTTPException:
            print(f"[welcome] Could not find channel {WELCOME_CHANNEL_ID}")
            return

    embed = discord.Embed(
        description=(
            f"Hey {member.mention}, welcome to **rbxVault**!\n"
            "Make sure you verify to gain access to all the channels!"
        ),
        color=EMBED_COLOR,
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    view = discord.ui.View()
    view.add_item(
        discord.ui.Button(
            label="Verify now", style=discord.ButtonStyle.link, url=VERIFY_CHANNEL_URL
        )
    )
    try:
        # Mention in the message text so the new member actually gets pinged.
        await channel.send(content=member.mention, embed=embed, view=view)
    except discord.HTTPException as e:
        print(f"[welcome] Failed to send welcome message: {e}")


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


VERIFY_CHANNEL_URL = os.environ.get(
    "VERIFY_CHANNEL_URL",
    "https://discord.com/channels/1262760206597750876/1568322291971002428",
)


@bot.tree.command(
    name="notifyverify",
    description="DM a member to verify their Roblox account through Bloxlink",
)
@app_commands.describe(user="The member to remind")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def notifyverify(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You need the Manage Server permission to use this.", ephemeral=True
        )
        return

    if user.bot:
        await interaction.response.send_message("You can't DM a bot.", ephemeral=True)
        return

    server_name = interaction.guild.name
    embed = discord.Embed(
        title="Verify your Roblox account",
        description=(
            f"Hey {user.display_name}! To get access to the rest of **{server_name}**, "
            "you need to verify your Roblox account through **Bloxlink**.\n\n"
            "🔗 Press the button below, then follow the steps in the verification channel.\n\n"
            "⚠️ Never send passwords, cookies, 2FA codes or account tokens to anyone."
        ),
        color=EMBED_COLOR,
    )
    view = discord.ui.View()
    view.add_item(
        discord.ui.Button(
            label="Verify now", style=discord.ButtonStyle.link, url=VERIFY_CHANNEL_URL
        )
    )

    try:
        await user.send(embed=embed, view=view)
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
        f"Sent a verification reminder to {user.mention}.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="notify", description="DM a member about a giveaway")
@app_commands.describe(
    user="The member to notify",
    prize="What the giveaway is for, e.g. 5 Robux",
    message_id="The ID of the giveaway message",
    channel="Channel the giveaway is in (defaults to the current channel)",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def notify(
    interaction: discord.Interaction,
    user: discord.Member,
    prize: app_commands.Range[str, 1, 100],
    message_id: str,
    channel: discord.TextChannel | None = None,
):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You need the Manage Server permission to use this.", ephemeral=True
        )
        return

    if user.bot:
        await interaction.response.send_message("You can't DM a bot.", ephemeral=True)
        return

    message_id = message_id.strip()
    if not message_id.isdigit():
        await interaction.response.send_message(
            "That message ID isn't valid. It should be a long number "
            "(right-click the giveaway message -> Copy Message ID).",
            ephemeral=True,
        )
        return

    channel_id = channel.id if channel else interaction.channel_id

    # Check the giveaway message really exists in that channel (skipped if I can't read it).
    target = channel or interaction.channel
    try:
        await target.fetch_message(int(message_id))
    except discord.NotFound:
        await interaction.response.send_message(
            "I couldn't find that message in "
            f"{target.mention}. Check the message ID, or pick the right channel.",
            ephemeral=True,
        )
        return
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        pass

    url = f"https://discord.com/channels/{interaction.guild_id}/{channel_id}/{message_id}"
    prize = prize.strip()

    embed = discord.Embed(
        title=f"🎁 Giveaway: {prize}",
        description=(
            f"Hey {user.display_name}! There's a giveaway for **{prize}** "
            f"in **{interaction.guild.name}**.\n\n"
            "Press the button below to jump to the giveaway and enter.\n\n"
            "⚠️ Never send passwords, cookies, 2FA codes or account tokens."
        ),
        color=EMBED_COLOR,
    )
    view = discord.ui.View()
    view.add_item(
        discord.ui.Button(label="Go to giveaway", style=discord.ButtonStyle.link, url=url)
    )

    try:
        await user.send(embed=embed, view=view)
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
        f"Notified {user.mention} about the **{prize}** giveaway.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="message", description="DM a member with a custom message")
@app_commands.describe(
    user="The member to message",
    message="What to say (type \\n for a new line)",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def message_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
    message: app_commands.Range[str, 1, 2000],
):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You need the Manage Server permission to use this.", ephemeral=True
        )
        return

    if user.bot:
        await interaction.response.send_message("You can't DM a bot.", ephemeral=True)
        return

    text = message.replace("\\n", "\n").strip()
    if not text:
        await interaction.response.send_message("The message can't be empty.", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"Message from {interaction.guild.name}",
        description=text,
        color=EMBED_COLOR,
    )
    embed.set_footer(text=f"Sent by the {interaction.guild.name} staff team")

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
        f"Sent a DM to {user.mention}.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


class ConfirmReset(discord.ui.View):
    def __init__(self, admin_id: int, guild_id: int, target: discord.Member):
        super().__init__(timeout=60)
        self.admin_id = admin_id
        self.guild_id = guild_id
        self.target = target

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "Only the admin who ran this command can confirm it.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm reset", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = db.execute(
            "SELECT units FROM balances WHERE guild_id=? AND user_id=?",
            (self.guild_id, self.target.id),
        ).fetchone()
        units = row[0] if row else 0
        if units <= 0:
            self.stop()
            await interaction.response.edit_message(
                content=f"{self.target.display_name} has no balance to reset.", view=None
            )
            return

        db.execute(
            "INSERT INTO redemptions (guild_id, user_id, units, admin_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (self.guild_id, self.target.id, units, interaction.user.id, int(time.time())),
        )
        db.execute(
            "UPDATE balances SET units = 0 WHERE guild_id=? AND user_id=?",
            (self.guild_id, self.target.id),
        )
        db.commit()
        self.stop()
        await interaction.response.edit_message(content="Done.", view=None)

        public_text = f"Reset {self.target.mention}'s balance. Redeemed: **{fmt(units)} Robux**."
        no_ping = discord.AllowedMentions.none()
        try:
            await interaction.channel.send(public_text, allowed_mentions=no_ping)
        except discord.HTTPException:
            await interaction.followup.send(public_text, allowed_mentions=no_ping)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Cancelled. Nothing was changed.", view=None)


@bot.tree.command(name="reset", description="Reset a member's Robux balance after they redeem it")
@app_commands.describe(user="The member who redeemed their Robux")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def reset(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "Only admins can use this command.", ephemeral=True
        )
        return

    row = db.execute(
        "SELECT units FROM balances WHERE guild_id=? AND user_id=?",
        (interaction.guild_id, user.id),
    ).fetchone()
    units = row[0] if row else 0
    if units <= 0:
        await interaction.response.send_message(
            f"{user.display_name} has no balance to reset.", ephemeral=True
        )
        return

    view = ConfirmReset(interaction.user.id, interaction.guild_id, user)
    await interaction.response.send_message(
        f"Reset **{user.display_name}**'s balance of **{fmt(units)} Robux** to 0 "
        "and log it as redeemed? This can't be undone.",
        view=view,
        ephemeral=True,
    )


@bot.tree.command(name="transactions", description="Show how many times Robux were redeemed (resets)")
@app_commands.describe(user="Only show redemptions for this member (optional)")
@app_commands.guild_only()
async def transactions(interaction: discord.Interaction, user: discord.Member | None = None):
    where = "guild_id=?"
    params: list = [interaction.guild_id]
    if user:
        where += " AND user_id=?"
        params.append(user.id)

    count, total = db.execute(
        f"SELECT COUNT(*), COALESCE(SUM(units), 0) FROM redemptions WHERE {where}", params
    ).fetchone()
    recent = db.execute(
        f"SELECT user_id, units, admin_id, created_at FROM redemptions WHERE {where} "
        "ORDER BY id DESC LIMIT 10",
        params,
    ).fetchall()

    title = f"Redemptions for {user.display_name}" if user else "Redemptions"
    embed = discord.Embed(title=title, color=EMBED_COLOR)
    embed.add_field(name="Times redeemed", value=f"**{count:,}**", inline=True)
    embed.add_field(name="Total redeemed", value=f"**{fmt(total)}** Robux", inline=True)
    if recent:
        lines = [
            f"<@{uid}> — **{fmt(u)}** Robux · <t:{ts}:R> · by <@{aid}>"
            for uid, u, aid, ts in recent
        ]
        embed.add_field(name="Most recent", value="\n".join(lines), inline=False)
    else:
        embed.description = "No redemptions recorded yet."
    await interaction.response.send_message(embed=embed)


PAY_PAGE_SIZE = 10


def build_pay_embed(guild_id: int, page: int) -> tuple[discord.Embed, int, int]:
    """Returns (embed, total_pages, clamped_page)."""
    total_units, members = db.execute(
        "SELECT COALESCE(SUM(units), 0), COUNT(*) FROM balances WHERE guild_id=? AND units > 0",
        (guild_id,),
    ).fetchone()
    pages = max(1, (members + PAY_PAGE_SIZE - 1) // PAY_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    offset = page * PAY_PAGE_SIZE

    rows = db.execute(
        "SELECT user_id, units FROM balances WHERE guild_id=? AND units > 0 "
        "ORDER BY units DESC, user_id LIMIT ? OFFSET ?",
        (guild_id, PAY_PAGE_SIZE, offset),
    ).fetchall()

    description = (
        f"**Total to pay: {fmt(total_units)} Robux**\n"
        f"Across **{members:,}** member{'s' if members != 1 else ''}"
    )
    if rows:
        lines = [
            f"`{offset + i + 1}.` <@{uid}> — **{fmt(u)}** Robux"
            for i, (uid, u) in enumerate(rows)
        ]
        description += "\n\n" + "\n".join(lines)
    else:
        description += "\n\nNobody is owed anything right now."

    embed = discord.Embed(title="Robux to Pay", description=description, color=EMBED_COLOR)
    embed.set_footer(text=f"Page {page + 1}/{pages}")
    return embed, pages, page


class PayView(discord.ui.View):
    def __init__(self, interaction: discord.Interaction, page: int, pages: int):
        super().__init__(timeout=180)
        self.origin = interaction
        self.admin_id = interaction.user.id
        self.guild_id = interaction.guild_id
        self.page = page
        self.pages = pages
        self.sync_buttons()

    def sync_buttons(self):
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self.pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "Only the admin who ran this command can change pages. Run `/to pay` yourself.",
                ephemeral=True,
            )
            return False
        return True

    async def refresh(self, interaction: discord.Interaction):
        embed, self.pages, self.page = build_pay_embed(self.guild_id, self.page)
        self.sync_buttons()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀ Back", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        await self.refresh(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await self.refresh(interaction)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            await self.origin.edit_original_response(view=self)
        except discord.HTTPException:
            pass


to_group = app_commands.Group(
    name="to",
    description="Payout tools",
    default_permissions=discord.Permissions(administrator=True),
    guild_only=True,
)


@to_group.command(name="pay", description="Show how much Robux needs to be paid to members")
async def to_pay(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "Only admins can use this command.", ephemeral=True
        )
        return

    embed, pages, page = build_pay_embed(interaction.guild_id, 0)
    if pages > 1:
        await interaction.response.send_message(
            embed=embed, view=PayView(interaction, page, pages)
        )
    else:
        await interaction.response.send_message(embed=embed)


bot.tree.add_command(to_group)


bot.run(TOKEN)
