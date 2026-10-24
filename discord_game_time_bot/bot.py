import os
import sqlite3
import calendar
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ROLE_NAME = os.getenv("WINNER_ROLE_NAME", "Unemployed")
TIMEZONE_NAME = os.getenv("BOT_TIMEZONE", "Asia/Colombo")
DB_PATH = os.getenv("DB_PATH", "game_time.sqlite3")
CHECK_EVERY_SECONDS = 60
LOCAL_TZ = ZoneInfo(TIMEZONE_NAME)

if not TOKEN:
    raise RuntimeError(
        "Set DISCORD_BOT_TOKEN as an environment variable. "
        "Never paste your bot token into public code."
    )

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.presences = True

bot = commands.Bot(command_prefix="!", intents=intents)


def now_local():
    return datetime.now(LOCAL_TZ)


def month_key(dt=None):
    dt = dt or now_local()
    return dt.strftime("%Y-%m")


def previous_month_key(dt=None):
    dt = dt or now_local()
    first_day = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous_day = first_day.fromordinal(first_day.toordinal() - 1)
    return previous_day.strftime("%Y-%m")


def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS game_minutes (
                guild_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                month TEXT NOT NULL,
                minutes INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id, month)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                guild_id TEXT PRIMARY KEY,
                winner_role_id TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS awards (
                guild_id TEXT NOT NULL,
                month TEXT NOT NULL,
                user_id TEXT NOT NULL,
                awarded_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, month)
            )
        """)


def add_minutes(guild_id: int, user_id: int, month: str, minutes: int):
    if minutes <= 0:
        return
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            INSERT INTO game_minutes (guild_id, user_id, month, minutes)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id, month)
            DO UPDATE SET minutes = minutes + excluded.minutes
        """, (str(guild_id), str(user_id), month, minutes))


def get_minutes(guild_id: int, user_id: int, month: str):
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("""
            SELECT minutes FROM game_minutes
            WHERE guild_id = ? AND user_id = ? AND month = ?
        """, (str(guild_id), str(user_id), month)).fetchone()
    return row[0] if row else 0


def leaderboard_rows(guild_id: int, month: str, limit: int = 10):
    with sqlite3.connect(DB_PATH) as con:
        return con.execute("""
            SELECT user_id, minutes FROM game_minutes
            WHERE guild_id = ? AND month = ?
            ORDER BY minutes DESC, user_id ASC
            LIMIT ?
        """, (str(guild_id), month, limit)).fetchall()


def get_saved_role_id(guild_id: int):
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT winner_role_id FROM settings WHERE guild_id = ?",
            (str(guild_id),)
        ).fetchone()
    return int(row[0]) if row and row[0] else None


def save_role_id(guild_id: int, role_id: int):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            INSERT INTO settings (guild_id, winner_role_id) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET winner_role_id = excluded.winner_role_id
        """, (str(guild_id), str(role_id)))


def was_awarded(guild_id: int, month: str):
    with sqlite3.connect(DB_PATH) as con:
        return con.execute(
            "SELECT 1 FROM awards WHERE guild_id = ? AND month = ?",
            (str(guild_id), month)
        ).fetchone() is not None


def record_award(guild_id: int, month: str, user_id: int):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            INSERT OR REPLACE INTO awards (guild_id, month, user_id, awarded_at)
            VALUES (?, ?, ?, ?)
        """, (str(guild_id), month, str(user_id), now_local().isoformat()))


def is_playing_game(member: discord.Member) -> bool:
    for activity in member.activities:
        if isinstance(activity, discord.Game):
            return True
        if getattr(activity, "type", None) == discord.ActivityType.playing:
            return True
    return False


def format_time(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


async def award_month_winner(guild: discord.Guild, month: str):
    if was_awarded(guild.id, month):
        return "This month's winner has already been awarded."

    rows = leaderboard_rows(guild.id, month, 1)
    if not rows:
        return f"No game time was recorded for {month}; no role was assigned."

    user_id, minutes = rows[0]
    role_id = get_saved_role_id(guild.id)
    role = guild.get_role(role_id) if role_id else discord.utils.get(guild.roles, name=ROLE_NAME)

    if role is None:
        return (
            f"I couldn't find the **{ROLE_NAME}** role. "
            "Ask an admin to run `/setwinnerrole` with the correct role."
        )

    for member in list(role.members):
        if member.id != int(user_id):
            try:
                await member.remove_roles(role, reason=f"Monthly game-time winner changed for {month}")
            except discord.Forbidden:
                return "I don't have permission to manage the winner role. Check role permissions/order."

    winner = guild.get_member(int(user_id))
    if winner is None:
        try:
            winner = await guild.fetch_member(int(user_id))
        except discord.HTTPException:
            return "The top player is no longer in this server, so I couldn't assign the role."

    if role not in winner.roles:
        try:
            await winner.add_roles(role, reason=f"Monthly game-time winner for {month}")
        except discord.Forbidden:
            return "I can't assign the role. Put the bot's role above the winner role and enable Manage Roles."

    record_award(guild.id, month, int(user_id))
    return f"🏆 {winner.mention} wins **{month}** with **{format_time(minutes)}** of tracked game time and receives {role.mention}!"


@bot.event
async def on_ready():
    init_db()
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
    except Exception as exc:
        print(f"Slash-command sync failed: {exc}")
    if not game_time_tracker.is_running():
        game_time_tracker.start()
    if not monthly_award_checker.is_running():
        monthly_award_checker.start()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@tasks.loop(seconds=CHECK_EVERY_SECONDS)
async def game_time_tracker():
    current_month = month_key()
    for guild in bot.guilds:
        for member in list(guild.members):
            if member.bot:
                continue
            if member.status != discord.Status.offline and is_playing_game(member):
                add_minutes(guild.id, member.id, current_month, 1)


@game_time_tracker.before_loop
async def before_tracker():
    await bot.wait_until_ready()


@tasks.loop(minutes=1)
async def monthly_award_checker():
    now = now_local()
    if now.day == 1 and now.hour == 0 and now.minute < 2:
        target_month = previous_month_key(now)
        for guild in bot.guilds:
            result = await award_month_winner(guild, target_month)
            print(f"[{guild.name}] {result}")


@monthly_award_checker.before_loop
async def before_award_checker():
    await bot.wait_until_ready()


@bot.tree.command(name="leaderboard", description="Show the monthly game-time leaderboard.")
@app_commands.describe(month="Optional month in YYYY-MM format; defaults to this month.")
async def leaderboard(interaction: discord.Interaction, month: str | None = None):
    if interaction.guild is None:
        await interaction.response.send_message("Use this command in your server.", ephemeral=True)
        return

    selected_month = month or month_key()
    try:
        datetime.strptime(selected_month, "%Y-%m")
    except ValueError:
        await interaction.response.send_message("Use the month format `YYYY-MM`, such as `2026-10`.", ephemeral=True)
        return

    rows = leaderboard_rows(interaction.guild.id, selected_month, 10)
    if not rows:
        await interaction.response.send_message(f"No game time recorded for **{selected_month}** yet.")
        return

    lines = []
    for position, (user_id, minutes) in enumerate(rows, start=1):
        member = interaction.guild.get_member(int(user_id))
        display = member.mention if member else f"<@{user_id}>"
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(position, f"`{position}.`")
        lines.append(f"{medal} {display} — **{format_time(minutes)}**")

    embed = discord.Embed(
        title=f"🎮 Game Time Leaderboard — {selected_month}",
        description="\n".join(lines),
        color=discord.Color.blurple()
    )
    embed.set_footer(text="Only game activity visible to the bot while it is online is counted.")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="mytime", description="Show your tracked game time for this month.")
async def mytime(interaction: discord.Interaction):
    if interaction.guild is None or interaction.user is None:
        await interaction.response.send_message("Use this command in your server.", ephemeral=True)
        return
    minutes = get_minutes(interaction.guild.id, interaction.user.id, month_key())
    await interaction.response.send_message(
        f"🎮 You've got **{format_time(minutes)}** of tracked game time this month.",
        ephemeral=True
    )


@bot.tree.command(name="setwinnerrole", description="Admin: choose the role given to the monthly winner.")
@app_commands.describe(role="The monthly winner role, e.g. Unemployed.")
@app_commands.checks.has_permissions(manage_roles=True)
async def setwinnerrole(interaction: discord.Interaction, role: discord.Role):
    if interaction.guild is None:
        await interaction.response.send_message("Use this command in your server.", ephemeral=True)
        return
    if role.is_default() or role.managed:
        await interaction.response.send_message("Choose a normal server role.", ephemeral=True)
        return
    bot_member = interaction.guild.me
    if bot_member is None or role >= bot_member.top_role:
        await interaction.response.send_message(
            "Move the bot's highest role above the reward role, then try again.",
            ephemeral=True
        )
        return
    save_role_id(interaction.guild.id, role.id)
    await interaction.response.send_message(
        f"✅ Monthly winner role set to {role.mention}. The bot will transfer it to each new winner.",
        ephemeral=True
    )


@bot.tree.command(name="awardwinner", description="Admin: manually award the winner for a month.")
@app_commands.describe(month="Month in YYYY-MM format, e.g. 2026-09. Defaults to last month.")
@app_commands.checks.has_permissions(manage_roles=True)
async def awardwinner(interaction: discord.Interaction, month: str | None = None):
    if interaction.guild is None:
        await interaction.response.send_message("Use this command in your server.", ephemeral=True)
        return
    selected_month = month or previous_month_key()
    try:
        datetime.strptime(selected_month, "%Y-%m")
    except ValueError:
        await interaction.response.send_message("Use the month format `YYYY-MM`.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=False)
    result = await award_month_winner(interaction.guild, selected_month)
    await interaction.followup.send(result)


@leaderboard.error
@mytime.error
@setwinnerrole.error
@awardwinner.error
async def command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        message = "You need the **Manage Roles** permission to use that command."
    else:
        message = "Something went wrong. Check that the bot has the right permissions and try again."
        print(f"Command error: {error}")

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


init_db()
bot.run(TOKEN)
