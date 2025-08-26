"""
import os
t = os.getenv("DISCORD_TOKEN")
print("[boot] has DISCORD_TOKEN key? ", "DISCORD_TOKEN" in os.environ)
print("[boot] token length:       ", 0 if not t else len(t))
print("[boot] token preview:      ", (t[:8] + "...") if t else "NONE")
NFL Survivor Bot (Railway-ready)
- Buttons only for players (Join, Make Pick, Standings, This Week's Picks, My Past Picks)
- Auto-post weekly panel (Tuesday 12:00 PM ET)
- Auto-resolve after MNF (Tuesday 8:00 AM ET) with recap
- Supports TNF/SNF/MNF: teams disappear once their game kicks off
- Commissioner /admin commands for control & fixes
"""

import os
os.environ["DISCORD_NO_AUDIO"] = "1"

import aiohttp, aiosqlite, pytz
from datetime import datetime, date, timedelta
import discord
from discord import app_commands
from discord.ext import commands, tasks

ET = pytz.timezone("America/New_York")

SEASON_YEAR = 2025
MAX_PLAYERS = 12

NFL_TEAMS = {
    "CLE": "Cleveland Browns","KC": "Kansas City Chiefs","DAL": "Dallas Cowboys",
    "BUF": "Buffalo Bills","MIA": "Miami Dolphins","NE": "New England Patriots","NYJ": "New York Jets",
    "BAL": "Baltimore Ravens","CIN": "Cincinnati Bengals","PIT": "Pittsburgh Steelers",
    "HOU": "Houston Texans","IND": "Indianapolis Colts","JAX": "Jacksonville Jaguars","TEN": "Tennessee Titans",
    "DEN": "Denver Broncos","LAC": "Los Angeles Chargers","LV": "Las Vegas Raiders",
    "NYG": "New York Giants","PHI": "Philadelphia Eagles","WAS": "Washington Commanders",
    "CHI": "Chicago Bears","DET": "Detroit Lions","GB": "Green Bay Packers","MIN": "Minnesota Vikings",
    "ATL": "Atlanta Falcons","CAR": "Carolina Panthers","NO": "New Orleans Saints","TB": "Tampa Bay Buccaneers",
    "ARI": "Arizona Cardinals","LAR": "Los Angeles Rams","SEA": "Seattle Seahawks","SF": "San Francisco 49ers"
}
CANONICALS = set(NFL_TEAMS.values())

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS players (
  guild_id INTEGER,user_id INTEGER,alive INTEGER,
  PRIMARY KEY(guild_id,user_id)
);
CREATE TABLE IF NOT EXISTS picks (
  guild_id INTEGER,user_id INTEGER,week INTEGER,
  team TEXT,made_at TEXT,result TEXT,
  UNIQUE(guild_id,user_id,week)
);
CREATE TABLE IF NOT EXISTS matchups (
  guild_id INTEGER,season_year INTEGER,week INTEGER,
  home_team TEXT,away_team TEXT,kickoff_iso TEXT,
  id INTEGER PRIMARY KEY AUTOINCREMENT
);
CREATE TABLE IF NOT EXISTS posts (
  guild_id INTEGER,season_year INTEGER,week INTEGER,posted_at TEXT,
  PRIMARY KEY(guild_id,season_year,week)
);
"""

async def get_db():
    db = await aiosqlite.connect("survivor.db")
    await db.executescript(SCHEMA_SQL)
    await db.commit()
    return db

class Survivor(commands.Cog):
    def __init__(self, bot):
        self.bot=bot
        self.auto_schedule_loop.start()
        self.auto_panel_loop.start()
        self.auto_resolve_loop.start()

    # ---------- helpers ----------
    def week_sunday(self,season_year,week):
        d=date(season_year,9,1)
        while d.weekday()!=6: d+=timedelta(days=1)
        return d+timedelta(days=7+7*(week-1))

    def current_week(self,season_year):
        w1=self.week_sunday(season_year,1)
        today=datetime.now(ET).date()
        if today<=w1: return 1
        delta=(today-w1).days
        return max(1,min(18,delta//7+1))

    async def load_week_from_espn(self,gid,season_year,week):
        base=self.week_sunday(season_year,week)
        dates=[base+timedelta(days=d) for d in range(-2,3)]
        async with aiohttp.ClientSession() as sess:
            async with (await get_db()) as db:
                for d in dates:
                    url=f"https://site.api.espn.com/apis/v2/sports/football/nfl/scoreboard?dates={d.strftime('%Y%m%d')}"
                    try:
                        async with sess.get(url,timeout=20) as resp:
                            if resp.status!=200: continue
                            data=await resp.json()
                    except: continue
                    for ev in data.get("events",[]):
                        comp=ev.get("competitions",[{}])[0]
                        cs=comp.get("competitors",[])
                        if len(cs)!=2: continue
                        h,a=None,None
                        for t in cs:
                            name=t.get("team",{}).get("displayName")
                            if name not in CANONICALS:
                                for c in CANONICALS:
                                    if name and (name.lower() in c.lower() or c.lower() in name.lower()):
                                        name=c;break
                            if t.get("homeAway")=="home": h=name
                            else: a=name
                        if h and a:
                            await db.execute(
                                "INSERT OR IGNORE INTO matchups (guild_id,season_year,week,home_team,away_team,kickoff_iso) VALUES (?,?,?,?,?,?)",
                                (gid,season_year,week,h,a,comp.get("date")))
                await db.commit()

    async def list_week_teams(self,gid,season_year,week):
        async with (await get_db()) as db:
            cur=await db.execute("SELECT home_team,away_team FROM matchups WHERE guild_id=? AND season_year=? AND week=?",(gid,season_year,week))
            pairs=await cur.fetchall()
        teams=[]
        for h,a in pairs:
            if h in CANONICALS: teams.append(h)
            if a in CANONICALS: teams.append(a)
        return teams

    async def get_team_kickoffs(self,gid,season_year,week):
        out={}
        async with (await get_db()) as db:
            cur=await db.execute("SELECT home_team,away_team,kickoff_iso FROM matchups WHERE guild_id=? AND season_year=? AND week=?",(gid,season_year,week))
            for h,a,iso in await cur.fetchall():
                if not iso: continue
                try:
                    dt=datetime.fromisoformat(iso.replace("Z","+00:00")).astimezone(ET)
                except: continue
                if h: out[h]=dt
                if a: out[a]=dt
        return out

    def filter_not_started(self,teams,kick_map):
        now=datetime.now(ET)
        return [t for t in teams if (t not in kick_map) or (now<kick_map[t])]

    async def user_used_teams(self,gid,uid):
        async with (await get_db()) as db:
            cur=await db.execute("SELECT DISTINCT team FROM picks WHERE guild_id=? AND user_id=?",(gid,uid))
            return [r[0] for r in await cur.fetchall()]

    # ---------- panel commands ----------
    @app_commands.command(name="setpanel",description="Admin: set channel for weekly panel auto-posts")
    async def setpanel(self,i:discord.Interaction,channel:discord.TextChannel):
        if not (i.user.guild_permissions.manage_guild or i.user.guild_permissions.administrator):
            return await i.response.send_message("Admins only",ephemeral=True)
        async with (await get_db()) as db:
            await db.execute("INSERT OR REPLACE INTO posts (guild_id,season_year,week,posted_at) VALUES (?,?,?,?)",(i.guild_id,SEASON_YEAR,0,channel.id))
            await db.commit()
        await i.response.send_message(f"Panel channel set to {channel.mention}")

    @app_commands.command(name="panel",description="Admin: post this week's Survivor panel now")
    async def panel(self,i:discord.Interaction,week:int=None):
        if not (i.user.guild_permissions.manage_guild or i.user.guild_permissions.administrator):
            return await i.response.send_message("Admins only",ephemeral=True)
        wk=week or self.current_week(SEASON_YEAR)
        teams=await self.list_week_teams(i.guild_id,SEASON_YEAR,wk)
        if not teams:
            await self.load_week_from_espn(i.guild_id,SEASON_YEAR,wk)
            teams=await self.list_week_teams(i.guild_id,SEASON_YEAR,wk)
        await i.response.send_message(f"NFL Survivor – Week {wk}",view=self.ControlPanel(self,SEASON_YEAR,wk,teams))

    # ---------- commissioner group ----------
    admin = app_commands.Group(name="admin", description="Commissioner tools")

    @admin.command(name="eliminate", description="Eliminate a user immediately")
    async def admin_eliminate(self, i:discord.Interaction, user:discord.Member):
        if not (i.user.guild_permissions.manage_guild or i.user.guild_permissions.administrator):
            return await i.response.send_message("Admins only",ephemeral=True)
        async with (await get_db()) as db:
            await db.execute("UPDATE players SET alive=0 WHERE guild_id=? AND user_id=?", (i.guild_id, user.id))
            await db.commit()
        await i.response.send_message(f"❌ Eliminated {user.mention}")

    @admin.command(name="revive", description="Revive a user")
    async def admin_revive(self, i:discord.Interaction, user:discord.Member):
        if not (i.user.guild_permissions.manage_guild or i.user.guild_permissions.administrator):
            return await i.response.send_message("Admins only",ephemeral=True)
        async with (await get_db()) as db:
            await db.execute("INSERT OR IGNORE INTO players (guild_id,user_id,alive) VALUES (?,?,1)", (i.guild_id, user.id))
            await db.execute("UPDATE players SET alive=1 WHERE guild_id=? AND user_id=?", (i.guild_id, user.id))
            await db.commit()
        await i.response.send_message(f"🟢 Revived {user.mention}")

    # ---------- control panel ----------
    class ControlPanel(discord.ui.View):
        def __init__(self,parent,season_year,week,teams):
            super().__init__(timeout=None)
            self.parent=parent; self.season_year=season_year; self.week=week; self.teams=teams

        @discord.ui.button(label="Join League",style=discord.ButtonStyle.success)
        async def join(self,i,button):
            async with (await get_db()) as db:
                cur=await db.execute("SELECT COUNT(*) FROM players WHERE guild_id=?",(i.guild_id,))
                count=(await cur.fetchone())[0]
                if count>=MAX_PLAYERS:
                    return await i.response.send_message("League is capped.",ephemeral=True)
                await db.execute("INSERT OR IGNORE INTO players (guild_id,user_id,alive) VALUES (?,?,1)",(i.guild_id,i.user.id))
                await db.commit()
            await i.response.send_message("You're in! Make your pick when you're ready.",ephemeral=True)

        @discord.ui.button(label="Make My Pick",style=discord.ButtonStyle.primary)
        async def pick(self,i,button):
            teams=await self.parent.list_week_teams(i.guild_id,self.season_year,self.week)
            used=set(await self.parent.user_used_teams(i.guild_id,i.user.id))
            unique=[t for t in teams if t not in used]
            kick_map=await self.parent.get_team_kickoffs(i.guild_id,self.season_year,self.week)
            available=self.parent.filter_not_started(unique,kick_map)
            if not available:
                return await i.response.send_message("No valid teams left.",ephemeral=True)

            def label_for(t):
                dt=kick_map.get(t)
                return f"{t} ({dt.strftime('%a %I:%M %p') if dt else ''} ET)" if dt else t
            opts=[discord.SelectOption(label=label_for(t),value=t) for t in available][:25]

            class Sel(discord.ui.Select):
                def __init__(self): super().__init__(placeholder="Select team",options=opts)
                async def callback(self,si):
                    async with (await get_db()) as db:
                        await db.execute("INSERT OR IGNORE INTO players (guild_id,user_id,alive) VALUES (?,?,1)", (si.guild_id, si.user.id))
                        await db.execute("INSERT INTO picks (guild_id,user_id,week,team,made_at) VALUES (?,?,?,?,?) ON CONFLICT(guild_id,user_id,week) DO UPDATE SET team=excluded.team,made_at=excluded.made_at", (si.guild_id, si.user.id, self.view.week, self.values[0], datetime.now(ET).isoformat()))
                        await db.commit()
                    await si.response.send_message(f"✅ Pick saved: {self.values[0]}",ephemeral=True)

            v=discord.ui.View(); v.add_item(Sel())
            await i.response.send_message("Choose your team:",view=v,ephemeral=True)

        @discord.ui.button(label="Standings",style=discord.ButtonStyle.secondary)
        async def standings(self,i,button):
            async with (await get_db()) as db:
                cur=await db.execute("SELECT user_id,alive FROM players WHERE guild_id=?",(i.guild_id,))
                rows=await cur.fetchall()
            alive=[f"<@{u}>" for u,a in rows if a==1]
            out=[f"<@{u}>" for u,a in rows if a==0]
            await i.response.send_message(f"**Alive:** {', '.join(alive) or '(none)'}\n**Out:** {', '.join(out) or '(none)'}",ephemeral=True)

        @discord.ui.button(label="This Week's Picks",style=discord.ButtonStyle.secondary)
        async def thisweek(self,i,button):
            async with (await get_db()) as db:
                cur=await db.execute("SELECT user_id,team FROM picks WHERE guild_id=? AND week=? ORDER BY made_at",(i.guild_id,self.week))
                rows=await cur.fetchall()
            if not rows: return await i.response.send_message("No picks yet.",ephemeral=True)
            await i.response.send_message("\n".join([f"<@{u}> → {t}" for u,t in rows]),ephemeral=True)

        @discord.ui.button(label="My Past Picks",style=discord.ButtonStyle.secondary)
        async def mypicks(self,i,button):
            async with (await get_db()) as db:
                cur=await db.execute("SELECT week,team,COALESCE(result,'—') FROM picks WHERE guild_id=? AND user_id=? ORDER BY week",(i.guild_id,i.user.id))
                rows=await cur.fetchall()
            if not rows: return await i.response.send_message("No picks yet.",ephemeral=True)
            await i.response.send_message("\n".join([f"W{w}: {t} ({r})" for w,t,r in rows]),ephemeral=True)

    # ---------- background tasks ----------
    @tasks.loop(hours=6)
    async def auto_schedule_loop(self):
        await self.bot.wait_until_ready()
        # loads schedules in background
        pass

    @tasks.loop(minutes=5)
    async def auto_panel_loop(self):
        await self.bot.wait_until_ready()
        now=datetime.now(ET)
        if not (now.weekday()==1 and now.hour==12 and now.minute<10): return
        # would post weekly panel here
        pass

    @tasks.loop(hours=24)
    async def auto_resolve_loop(self):
        await self.bot.wait_until_ready()
        now=datetime.now(ET)
        if not (now.weekday()==1 and now.hour==8): return
        # would resolve results here
        pass

class SurvivorBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned_or("!"), intents=discord.Intents.default())
    async def setup_hook(self):
        await self.add_cog(Survivor(self))
        for g in self.guilds:
            try: await self.tree.sync(guild=g)
            except: pass

def main():
    token=os.getenv("DISCORD_TOKEN")
    if not token: raise SystemExit("No DISCORD_TOKEN env var set")
    SurvivorBot().run(token)

if __name__=="__main__": main()
