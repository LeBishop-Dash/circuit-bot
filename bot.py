import os, sys, discord, json, base64, requests
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
from openai import OpenAI

def log(*a, **kw):
    print(*a, flush=True)

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY")

log(f"STARTUP SUPABASE_URL={SUPABASE_URL} key_len={len(SUPABASE_KEY) if SUPABASE_KEY else 0} key_start={(SUPABASE_KEY or '')[:12]}")
log(f"STARTUP DISCORD set={bool(DISCORD_TOKEN)} OPENAI set={bool(OPENAI_API_KEY)} GUILD={GUILD_ID}")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
client = OpenAI(api_key=OPENAI_API_KEY)

def headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "return=representation"}

def supa_insert(table, payload):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    try:
        r = requests.post(url, headers=headers(), json=payload, timeout=20)
        log(f"Supabase insert {table}: {r.status_code} {r.text[:800]}", flush=True)
        return r
    except Exception as e:
        log(f"Supabase insert EXCEPTION {e}", flush=True)
        raise

def supa_select(table, q=""):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{q}"
    r = requests.get(url, headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=20)
    log(f"Supabase select {table}: {r.status_code} {r.text[:400]}", flush=True)
    return r.json() if r.status_code==200 else []

def get_active_tournament():
    try:
        rows = supa_select("tournaments", "status=eq.active&order=id.desc&limit=1")
        if rows: return rows[0]
    except Exception as e:
        log(f"get_active_tournament error {e}", flush=True)
    return {"id": 1, "name": "current"}

async def read_boxscore(img_bytes):
    b64 = base64.b64encode(img_bytes).decode()
    prompt = """This is NBA 2K Pro-Am GAME STATS screen - THIS EXACT LAYOUT, not MC Recap.
LAYOUT:
- Title at top: "GAME STATS"
- Left side: team logos + scores (84 vs 82). IGNORE logos, IGNORE "NETS" / "RWE" / team names. NEVER use logo names.
- Two tables stacked. Top table = Team 1 (5 players). Bottom table = Team 2 (5 players).
- Each table has header: GRD | PTS | REB | AST | STL | BLK | FOULS | TO | FGM/FGA | 3PM/3PA | FTM/FTA
- Each table has a grey/yellow TOTAL row at bottom. SKIP TOTAL row as a player, but USE IT AS CHECKSUM.
- One row is highlighted yellow (the uploader, "YOU"). Read it normally, it is still a player. Do NOT skip yellow row.
- Gamertags may have PlayStation / Xbox icons before them. Ignore icons.
CHECKSUM RULE - CRITICAL:
- Sum of 5 players PTS must equal TOTAL PTS. Sum REB must equal TOTAL REB. Sum AST must equal TOTAL AST. Same for STL, BLK, FOULS, TO.
- Example from real screenshot: Top TOTAL is 84 PTS / 21 REB / 23 AST. Players must sum to that. If your sums do NOT match TOTAL, you misread a column - re-read that column carefully. REB is often 0 for guards, that is REAL, do not "fix" 0s to look nicer.
- REB is column AFTER PTS, BEFORE AST. Do NOT use FOULS or TO as REB.
- FGM/FGA split on "/". Same for 3PM/3PA, FTM/FTA.
TEAM NAMES: Top = "Team 1", Bottom = "Team 2" ONLY.
Return ONLY JSON: {"players": [{"gamertag":"NAME","team":"Team 1","pts":0,"reb":0,"ast":0,"stl":0,"blk":0,"fouls":0,"tov":0,"fgm":0,"fga":0,"tpm":0,"tpa":0,"ftm":0,"fta":0}]} 10 players total, no TOTAL rows."""

    resp = client.chat.completions.create(model="gpt-4o", messages=[{"role":"user","content":[{"type":"text","text":prompt},{"type":"image_url","image_url":{"url":f"data:image/png;base64,{b64}"}}]}], max_tokens=3000)
    text = resp.choices[0].message.content.strip().replace("```json","").replace("```","").strip()
    return json.loads(text)

@bot.event
async def on_ready():
    log(f"Bot online as {bot.user}", flush=True)
    try:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        log("Slash commands synced", flush=True)
    except Exception as e:
        log(f"Sync error: {e}", flush=True)

@bot.event
async def on_message(message):
    if message.author.bot: return
    if message.channel.name == "tournament-scores" and message.attachments:
        await message.add_reaction("\U0001F440")
        tourn = get_active_tournament()
        tname = tourn.get("name","current")
        log(f"GOT IMAGE from {message.author} tourn={tname}", flush=True)
        for att in message.attachments:
            if att.content_type and "image" in att.content_type:
                img = await att.read()
                try:
                    data = await read_boxscore(img)
                    log(f"READ OK players={len(data.get('players',[]))}", flush=True)
                    payload = {"tournament": tname, "uploader": str(message.author), "image_url": att.url, "raw_json": data, "status": "pending"}
                    r = supa_insert("pending_reviews", payload)
                    if r.status_code not in (200,201):
                        await message.channel.send(f"\u26a0\ufe0f Bot read it BUT Supabase FAILED to save ({r.status_code}): {r.text[:600]}\nCheck Railway SUPABASE_SERVICE_KEY - is it Secret key (sb_secret) not Publishable?")
                    else:
                        try:
                            j = r.json()
                            rid = j[0].get("id","?") if isinstance(j,list) and j else "?"
                        except: rid="?"
                        lines = "\n".join([f"{p.get('gamertag','?')} ({p.get('team','?')}): {p.get('pts',0)} PTS / {p.get('reb',0)} REB / {p.get('ast',0)} AST" for p in data.get("players",[])])
                        embed = discord.Embed(title=f"\u23f3 Sent to Review Queue #{rid} - {tname}", description=lines+"\n\nSaved to Supabase. Check website Review Queue.", color=0xFFD400)
                        await message.channel.send(embed=embed)
                except Exception as e:
                    log(f"Read error: {e}", flush=True)
                    await message.channel.send(f"Could not read this screenshot: {e}")
    await bot.process_commands(message)

@bot.tree.command(name="test-supabase", description="Test if bot can write to Supabase")
async def test_supabase_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    try:
        r = supa_insert("pending_reviews", {"tournament":"TEST","uploader":"test","image_url":"https://example.com/test.png","raw_json":{"players":[]},"status":"pending"})
        await interaction.followup.send(f"Supabase test: status {r.status_code}\n{r.text[:600]}\nIf 201, check Supabase pending_reviews for TEST row then delete it. If 401, SERVICE_KEY is wrong (need Secret sb_secret, not Publishable). If 400, table/payload issue.")
    except Exception as e:
        await interaction.followup.send(f"Supabase test EXCEPTION: {e}")

@bot.tree.command(name="review-queue", description="List pending screenshots")
async def review_queue_cmd(interaction: discord.Interaction):
    rows = supa_select("pending_reviews", "status=eq.pending&order=id.desc&limit=10")
    if not rows:
        await interaction.response.send_message("Review Queue is empty.", ephemeral=True); return
    desc = "\n".join([f"**#{r.get('id')}** - {r.get('tournament')} by {r.get('uploader')}" for r in rows])
    await interaction.response.send_message(embed=discord.Embed(title="Review Queue - Pending", description=desc, color=0xFFD400))

bot.run(DISCORD_TOKEN)
