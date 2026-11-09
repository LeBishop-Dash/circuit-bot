import os, discord, json, base64, requests
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
SUPABASE_URL = os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
client = OpenAI(api_key=OPENAI_API_KEY)

def supa_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "return=representation"}

def supa_insert(table, payload):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    r = requests.post(url, headers=supa_headers(), json=payload, timeout=20)
    print(f"Supabase insert {table}: {r.status_code} {r.text[:500]}")
    return r

def supa_select(table, query=""):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{query}"
    r = requests.get(url, headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=20)
    return r.json() if r.status_code==200 else []

def get_active_tournament():
    rows = supa_select("tournaments", "status=eq.active&order=id.desc&limit=1")
    if rows:
        return rows[0]
    return {"id": 1, "name": "current"}

async def read_boxscore(image_bytes):
    b64 = base64.b64encode(image_bytes).decode()
    prompt = """This is an NBA 2K MyCareer RECAP - GAME STATS screen.
LAYOUT: Two tables stacked. Top = 5 players, Bottom = 5 players. Left side has team logos + final scores. IGNORE logo text. Skip grey TOTAL row.
TEAM NAMES - GENERIC ONLY: Top = "Team 1", Bottom = "Team 2". NEVER use Barracudas, Nightfall, or logo name.
COLUMNS: GAMERTAG | GRD | PTS | REB | AST | STL | BLK | FOULS | TO | FGM/FGA | 3PM/3PA | FTM/FTA
EXAMPLE: "LeBishop- | B+ | 14 | 15 | 5 | 2 | 0 | 2 | 5 | 7/12 | 0/0 | 0/0" => gamertag=LeBishop-, team=Team 1, pts=14, reb=15, ast=5, stl=2, blk=0, fouls=2, tov=5, fgm=7, fga=12, tpm=0, tpa=0, ftm=0, fta=0
RULES: REB is after PTS, before AST. Do NOT use FOULS or TO as REB. Split FGM/FGA on "/". CHECK: Top 5 REBs sum to TOTAL row REB. Same bottom.
Return ONLY JSON: {"players": [{"gamertag":"NAME","team":"Team 1","pts":0,"reb":0,"ast":0,"stl":0,"blk":0,"fouls":0,"tov":0,"fgm":0,"fga":0,"tpm":0,"tpa":0,"ftm":0,"fta":0}]} 10 players total."""
    resp = client.chat.completions.create(model="gpt-4o", messages=[{"role":"user","content":[{"type":"text","text":prompt},{"type":"image_url","image_url":{"url":f"data:image/png;base64,{b64}"}}]}], max_tokens=3000)
    text = resp.choices[0].message.content.strip().replace("```json","").replace("```","").strip()
    return json.loads(text)

@bot.event
async def on_ready():
    print(f"Bot online as {bot.user} - Supabase: {SUPABASE_URL}")
    try:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        print("Slash commands synced")
    except Exception as e:
        print(f"Sync error: {e}")

@bot.event
async def on_message(message):
    if message.author.bot: return
    if message.channel.name == "tournament-scores" and message.attachments:
        await message.add_reaction("👀")
        tourn = get_active_tournament()
        tname = tourn.get("name","current")
        for att in message.attachments:
            if att.content_type and "image" in att.content_type:
                img = await att.read()
                try:
                    data = await read_boxscore(img)
                    # Send to Supabase Review Queue - NOT live yet
                    payload = {"tournament": tname, "uploader": str(message.author), "image_url": att.url, "raw_json": data, "status": "pending"}
                    r = supa_insert("pending_reviews", payload)
                    review_id = "?"
                    try:
                        review_id = r.json()[0].get("id", "?") if r.json() else "?"
                    except: pass
                    lines = "\n".join([f"{p.get('gamertag','?')} ({p.get('team','?')}): {p.get('pts',0)} PTS / {p.get('reb',0)} REB / {p.get('ast',0)} AST / {p.get('stl',0)} STL / {p.get('blk',0)} BLK / {p.get('tov',0)} TO | FG {p.get('fgm',0)}/{p.get('fga',0)}" for p in data.get("players",[])])
                    embed = discord.Embed(title=f"⏳ Sent to Review Queue #{review_id} - {tname}", description=lines + "\n\n⚠️ NOT live yet. Editors must review + assign to series in Review Queue on website.", color=0xFFD400)
                    embed.set_footer(text=f"Uploader: {message.author} | Website: Check Review Queue")
                    await message.channel.send(embed=embed)
                except Exception as e:
                    print(f"Read error: {e}")
                    await message.channel.send(f"Could not read this screenshot: {e}")
    await bot.process_commands(message)

@bot.tree.command(name="new-tournament", description="Start a new tournament")
@app_commands.describe(name="Name, e.g. Weekend Bash Nov 8")
async def new_tourn(interaction: discord.Interaction, name: str):
    supa_insert("tournaments", {"name": name, "status": "active"})
    # Archive others via PATCH
    try:
        requests.patch(f"{SUPABASE_URL}/rest/v1/tournaments?status=eq.active&name=neq.{name}", headers=supa_headers(), json={"status":"archived"}, timeout=10)
    except: pass
    await interaction.response.send_message(f"🏆 New tournament started: **{name}**")

@bot.tree.command(name="review-queue", description="List pending screenshots")
async def review_queue_cmd(interaction: discord.Interaction):
    rows = supa_select("pending_reviews", "status=eq.pending&order=id.desc&limit=10")
    if not rows:
        await interaction.response.send_message("✅ Review Queue is empty.", ephemeral=True); return
    desc = "\n".join([f"**#{r.get('id')}** - {r.get('tournament')} by {r.get('uploader')}" for r in rows])
    await interaction.response.send_message(embed=discord.Embed(title="⏳ Review Queue - Pending", description=desc, color=0xFFD400))

@bot.tree.command(name="approve-review", description="Approve a review and publish to series")
@app_commands.describe(review_id="Review ID", series="e.g. Semifinal 1", game_number="Game 1/2/3", format="BO1 or BO3")
async def approve_cmd(interaction: discord.Interaction, review_id: int, series: str, game_number: int = 1, format: str = "BO1"):
    rows = supa_select("pending_reviews", f"id=eq.{review_id}&status=eq.pending")
    if not rows:
        await interaction.response.send_message(f"No pending review #{review_id}.", ephemeral=True); return
    r0 = rows[0]
    data = r0.get("raw_json") or {}
    if isinstance(data, str):
        data = json.loads(data)
    tname = r0.get("tournament","current")
    tourn = get_active_tournament()
    tid = tourn.get("id",1)
    for p in data.get("players", []):
        supa_insert("stats", {"gamertag": p.get("gamertag","Unknown"), "team": p.get("team","Team 1"), "pts": p.get("pts",0), "reb": p.get("reb",0), "ast": p.get("ast",0), "stl": p.get("stl",0), "blk": p.get("blk",0), "tov": p.get("tov",0), "fouls": p.get("fouls",0), "fgm": p.get("fgm",0), "fga": p.get("fga",0), "tpm": p.get("tpm",0), "tpa": p.get("tpa",0), "ftm": p.get("ftm",0), "fta": p.get("fta",0), "tournament": tname, "tournament_id": tid, "series_label": series, "game_number": game_number, "format": format})
    # Mark approved
    requests.patch(f"{SUPABASE_URL}/rest/v1/pending_reviews?id=eq.{review_id}", headers=supa_headers(), json={"status":"approved","series_label":series,"game_number":game_number,"format":format}, timeout=10)
    await interaction.response.send_message(f"✅ Review #{review_id} approved -> Published to **{series} - Game {game_number} ({format})**. Now live on website.")

@bot.tree.command(name="stats", description="See a player's stats")
@app_commands.describe(gamertag="2K gamertag", tournament="Leave blank for current, or 'all'")
async def stats_cmd(interaction: discord.Interaction, gamertag: str, tournament: str = ""):
    if tournament.lower()=="all":
        rows = supa_select("stats", f"gamertag=ilike.*{gamertag}*")
        label="All-Time"
    elif tournament:
        rows = supa_select("stats", f"gamertag=ilike.*{gamertag}*&tournament=eq.{tournament}")
        label=f"Tournament: {tournament}"
    else:
        tourn=get_active_tournament(); tname=tourn.get("name","current")
        rows = supa_select("stats", f"gamertag=ilike.*{gamertag}*&tournament=eq.{tname}")
        label=f"Tournament: {tname}"
    if not rows:
        await interaction.response.send_message(f"No stats for {gamertag} in {label}.", ephemeral=True); return
    games=len(rows)
    def avg(k): return sum(r.get(k,0) or 0 for r in rows)/games if games else 0
    def pct(m,a): return round(m/a*100,1) if a else 0.0
    fgm=sum(r.get("fgm",0) or 0 for r in rows); fga=sum(r.get("fga",0) or 0 for r in rows)
    tpm=sum(r.get("tpm",0) or 0 for r in rows); tpa=sum(r.get("tpa",0) or 0 for r in rows)
    ftm=sum(r.get("ftm",0) or 0 for r in rows); fta=sum(r.get("fta",0) or 0 for r in rows)
    total=sum(r.get("pts",0) or 0 for r in rows)
    embed=discord.Embed(title=f"📊 {gamertag} - {label}", color=0x5865F2)
    embed.add_field(name="Games", value=str(games), inline=True)
    embed.add_field(name="PPG", value=f"{avg('pts'):.1f}", inline=True)
    embed.add_field(name="RPG", value=f"{avg('reb'):.1f}", inline=True)
    embed.add_field(name="APG", value=f"{avg('ast'):.1f}", inline=True)
    embed.add_field(name="SPG", value=f"{avg('stl'):.1f}", inline=True)
    embed.add_field(name="BPG", value=f"{avg('blk'):.1f}", inline=True)
    embed.add_field(name="TOV", value=f"{avg('tov'):.1f}", inline=True)
    embed.add_field(name="FG%", value=f"{pct(fgm,fga)}% ({fgm}/{fga})", inline=True)
    embed.add_field(name="3P%", value=f"{pct(tpm,tpa)}% ({tpm}/{tpa})", inline=True)
    embed.add_field(name="FT%", value=f"{pct(ftm,fta)}% ({ftm}/{fta})", inline=True)
    embed.add_field(name="Total PTS", value=str(total), inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="last-game", description="Show last box score")
async def last_game_cmd(interaction: discord.Interaction):
    rows = supa_select("stats", "order=id.desc&limit=10")
    if not rows:
        await interaction.response.send_message("No games yet.", ephemeral=True); return
    desc="\n".join([f"{r.get('gamertag')} ({r.get('team')}): {r.get('pts')} PTS / {r.get('reb')} REB / {r.get('ast')} AST" for r in reversed(rows)])
    await interaction.response.send_message(embed=discord.Embed(title="Last Box Score Read", description=desc, color=0x5865F2))

bot.run(DISCORD_TOKEN)
