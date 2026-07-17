import os
import asyncio
import discord
import time
import json
import random
import re
import aiohttp
from datetime import timedelta
from discord.ext import commands
from discord import app_commands
from groq import Groq
import yt_dlp
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────
#  CONFIGURACIÓN (.env)
# ─────────────────────────────────────────

def _env(nombre, requerido=True, entero=False):
    valor = os.getenv(nombre)
    if valor is None:
        if requerido:
            raise RuntimeError(f"❌ Falta '{nombre}' en .env")
        return None
    return int(valor) if entero else valor

DISCORD_TOKEN    = _env("DISCORD_TOKEN")
GROQ_API_KEY     = _env("GROQ_API_KEY")
GUILD_ID         = _env("GUILD_ID", entero=True)
CANAL_BIENVENIDA = _env("CANAL_BIENVENIDA", entero=True)
CANAL_ROLES      = _env("CANAL_ROLES", entero=True)
CANAL_UPDATES    = _env("CANAL_UPDATES", entero=True)
CANAL_COMANDOS   = _env("CANAL_COMANDOS", entero=True)
CANAL_IA         = _env("CANAL_IA", entero=True)
CANAL_MUSICA     = _env("CANAL_MUSICA", entero=True)
TU_USER_ID       = _env("TU_USER_ID", entero=True)
FFMPEG_PATH      = os.getenv("FFMPEG_PATH", "ffmpeg")
MODELO_IA        = "llama-3.3-70b-versatile"  # modelo activo en Groq

# Spotify — opcional, si no están configuradas el comando /spotify no funciona
spotify = None

ROLES_DISPONIBLES = [
    {"nombre": "Kuka",             "descripcion": "No tanto como gonfari"},
    {"nombre": "Vilma",            "descripcion": "Mmmmm"},
    {"nombre": "Novio de Mariluz", "descripcion": "doña tequeños"},
    {"nombre": "67",               "descripcion": "67"},
]

# ─────────────────────────────────────────
#  NIVELES — XP y roles
# ─────────────────────────────────────────

# XP por mensaje y fórmula de nivel
XP_POR_MENSAJE = 10
# Nivel = int(0.1 * sqrt(xp))
# Para subir del nivel N al N+1 necesitás (N*10)^2 XP acumulado

NIVELES_ROLES = {
    5:  "⭐ Nivel 5",
    10: "🌟 Nivel 10",
    20: "💫 Nivel 20",
    50: "🏆 Nivel 50",
}

GAGA_RANGOS = [
    (100_000, "Gaga Final Boss"),
    (50_000,  "Gaga Supremo"),
    (30_000,  "Big Gaga"),
    (20_000,  "Gaga"),
    (10_000,  "Little Gaga"),
]

def gaga_rango(puntos: int) -> str | None:
    for minimo, nombre in GAGA_RANGOS:
        if puntos >= minimo:
            return nombre
    return None

# ─── Wachin Points — sistema de penalización con timeout automático ───
# (minimo_puntos, duracion_en_minutos, etiqueta)
WACHIN_TIERS = [
    (100_000, 48*60, "🔴 48 horas"),
    (50_000,  24*60, "🟠 24 horas"),
    (25_000,  60,    "🟡 1 hora"),
    (10_000,  10,    "🟢 10 minutos"),
]

def wachin_tier(puntos: int) -> tuple[int, str] | None:
    """Devuelve (minutos_timeout, etiqueta) según el tier que corresponde, o None si no aplica."""
    for minimo, minutos, etiqueta in WACHIN_TIERS:
        if puntos >= minimo:
            return (minutos, etiqueta)
    return None

def xp_a_nivel(xp: int) -> int:
    import math
    return int(0.1 * math.sqrt(xp))

def xp_para_nivel(nivel: int) -> int:
    return (nivel * 10) ** 2

# ─────────────────────────────────────────
#  PERSISTENCIA JSON
# ─────────────────────────────────────────

DATA_FILE = "data.json"

def cargar_data() -> dict:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"ranking": {}, "memoria_ia": {}, "memoria_server": []}

def guardar_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

data = cargar_data()

def ranking_get(user_id: int) -> dict:
    uid = str(user_id)
    if uid not in data["ranking"]:
        data["ranking"][uid] = {"nombre": "", "mensajes": 0, "musica": 0, "xp": 0, "nivel": 0}
    if "xp" not in data["ranking"][uid]:
        data["ranking"][uid]["xp"] = 0
        data["ranking"][uid]["nivel"] = 0
    if "gaga" not in data["ranking"][uid]:
        data["ranking"][uid]["gaga"] = 0
    return data["ranking"][uid]

def memoria_get(user_id: int) -> list:
    uid = str(user_id)
    if uid not in data["memoria_ia"]:
        data["memoria_ia"][uid] = []
    return data["memoria_ia"][uid]

def memoria_set(user_id: int, recuerdos: list):
    data["memoria_ia"][str(user_id)] = recuerdos
    guardar_data()

def memoria_server_get() -> list:
    if "memoria_server" not in data:
        data["memoria_server"] = []
    return data["memoria_server"]

def memoria_server_add(dato: str):
    server_mem = memoria_server_get()
    if dato not in server_mem:
        server_mem.append(dato)
        if len(server_mem) > 50:
            data["memoria_server"] = server_mem[-50:]
        guardar_data()

# ─────────────────────────────────────────
#  SETUP
# ─────────────────────────────────────────

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
groq_client = Groq(api_key=GROQ_API_KEY)

ia_chats: dict[int, list[dict]] = {}
music_queue: list[dict] = []
music_voice_client: discord.VoiceClient | None = None
player_message: discord.Message | None = None
autoplay_enabled = False
liked_songs: list[dict] = []
_sugerencias_cache: dict[str, list[dict]] = {}

# yt-dlp para reproducción (descarga URL de audio)
YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}
# yt-dlp liviano para sugerencias (solo metadatos)
YTDL_SUGGEST_OPTIONS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": True,
    "default_search": "ytsearch5",
    "skip_download": True,
    "source_address": "0.0.0.0",
}
FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}
ytdl        = yt_dlp.YoutubeDL(YTDL_OPTIONS)
ytdl_suggest = yt_dlp.YoutubeDL(YTDL_SUGGEST_OPTIONS)

# ─────────────────────────────────────────
#  EVENTOS
# ─────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅ Bot conectado como {bot.user}")
    print(f"FFmpeg path: {FFMPEG_PATH}")
    try:
        synced = await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        print(f"Slash commands: {[c.name for c in synced]}")
    except Exception as e:
        print(f"Error al sincronizar: {e}")
    canal_roles = bot.get_channel(CANAL_ROLES)
    if canal_roles:
        async for msg in canal_roles.history(limit=20):
            if msg.author == bot.user:
                await msg.delete()
        await enviar_panel_roles(canal_roles)

@bot.command(name="sync")
async def sync_cmd(ctx):
    try:
        synced = await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        await ctx.send(f"✅ Sincronizados: {[c.name for c in synced]}")
    except Exception as e:
        await ctx.send(f"❌ Error: {e}")

@bot.event
async def on_member_join(member: discord.Member):
    canal = bot.get_channel(CANAL_BIENVENIDA)
    if canal:
        embed = discord.Embed(
            title=f"¡Bienvenido/a, {member.display_name}! 👋",
            description=(
                f"Hola {member.mention}, bienvenido/a al servidor.\n\n"
                f"📌 Pasá por <#{CANAL_ROLES}> para elegir tu rol.\n"
                f"🎵 Pedí música en <#{CANAL_MUSICA}> con `/play`.\n"
                f"🤖 Chateá con la IA en <#{CANAL_IA}>."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        await canal.send(embed=embed)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    r = ranking_get(message.author.id)
    r["nombre"] = message.author.display_name
    r["mensajes"] += 1
    r["xp"] = r.get("xp", 0) + XP_POR_MENSAJE
    nivel_anterior = r.get("nivel", 0)
    nivel_nuevo = xp_a_nivel(r["xp"])
    r["nivel"] = nivel_nuevo
    guardar_data()

    # Anuncio de subida de nivel
    if nivel_nuevo > nivel_anterior:
        canal_bienvenida = bot.get_channel(CANAL_BIENVENIDA)
        if canal_bienvenida:
            embed = discord.Embed(
                title="🎉 ¡Subiste de nivel!",
                description=f"{message.author.mention} subió al **nivel {nivel_nuevo}** 🆙",
                color=discord.Color.from_rgb(255, 215, 0),
            )
            embed.set_thumbnail(url=message.author.display_avatar.url)
            if nivel_nuevo in NIVELES_ROLES:
                embed.add_field(name="🏅 Logro desbloqueado", value=NIVELES_ROLES[nivel_nuevo], inline=False)
            await canal_bienvenida.send(embed=embed)

    if message.channel.id == CANAL_UPDATES and message.author.id == TU_USER_ID:
        contenido = message.content
        await message.delete()
        embed = discord.Embed(title="📢 Update", description=contenido, color=discord.Color.green())
        embed.set_footer(text="Publicado por el admin")
        await message.channel.send(embed=embed)
        return

    if message.channel.id == CANAL_COMANDOS and message.author.id == TU_USER_ID:
        await procesar_comando_bot(message)
        return

    if message.channel.id == CANAL_IA:
        await procesar_ia(message)
        return

    await bot.process_commands(message)

# ─────────────────────────────────────────
#  CHAT CON IA (Groq + memoria)
# ─────────────────────────────────────────

async def procesar_ia(message: discord.Message):
    user_id = message.author.id
    recuerdos = memoria_get(user_id)
    recuerdos_txt = "\n".join(f"- {r}" for r in recuerdos) if recuerdos else "Ninguno todavía."

    cancion_actual = ""
    if estado.cancion:
        cancion_actual = (
            f"Ahora mismo está sonando '{estado.cancion['titulo']}' "
            f"de {estado.cancion['uploader']}. "
            "Si el usuario te pide que hables de ella, podés comentarla."
        )

    memoria_server = memoria_server_get()
    memoria_server_txt = chr(10).join(f"- {r}" for r in memoria_server) if memoria_server else "Ninguna todavía."

    system_prompt = (
        "Sos el bot de un grupo de amigos argentinos en Discord, te llaman 'los totys'. "
        "No sos un asistente corporativo ni un chatbot de soporte. Sos uno más del grupo. Tu personalidad:\n"
        "- Hablás en rioplatense cerrado, informal, con chispa\n"
        "- Sos cargoso, bardero, con humor negro como cualquier grupo de amigos argentinos\n"
        "- Cuando te insultan o te dicen 'gil', 'boludo', 'choto', etc. — eso es joda de amigos, no te ofendas "
        "ni te pongas en modo defensivo ni digas frases como 'no puedo continuar esta conversación'. "
        "Respondé con la misma onda, con un chiste, una devolución, o ignorándolo con humor. Nunca cortes la charla.\n"
        "- Nunca actúes como un asistente formal pidiendo disculpas o marcando distancia emocional\n"
        "- No le hablés de 'frustraciones' ni le psicoanalices, eso suena a bot trucho\n"
        "- Usás minúsculas, abreviaciones tipo chat (q, x, tmb, dl), poco signo de puntuación, como escribe la gente real por Discord\n"
        "- Tenés opiniones fuertes y las decís sin vueltas\n"
        "- Podés hacer referencias a lo que dijeron otros usuarios antes\n"
        "- Nunca sos genérico ni aburrido — siempre tenés algo para decir, ni una sola respuesta tibia\n\n"
        f"Lo que recordás de ESTE usuario ({message.author.display_name}):\n{recuerdos_txt}\n\n"
        f"Lo que recordás del servidor en general (sobre otros usuarios, eventos, datos compartidos):\n{memoria_server_txt}\n\n"
        f"{cancion_actual}\n\n"
        "REGLAS IMPORTANTES:\n"
        "1. Si el usuario te pide que pongas una canción, respondé EXACTAMENTE así (nada más):\n"
        "   PONER_CANCION: <nombre de la canción>\n"
        "2. Si el usuario menciona algo importante sobre SÍ MISMO, al final agregá:\n"
        "   RECORDAR: <dato corto sobre este usuario>\n"
        "3. Si el usuario menciona algo importante sobre OTRA PERSONA o dato del servidor (nombres, relaciones, eventos), agregá:\n"
        "   RECORDAR_SERVER: <dato corto compartido>\n"
        "4. Estas líneas de RECORDAR son invisibles para el usuario, no las menciones.\n"
        "5. Si no hay nada importante para recordar, no agregues ninguna línea RECORDAR.\n"
        "6. Jamás termines una respuesta cortando la conversación, diciendo que 'no podés continuar' o derivando a 'hablar con alguien'. Eso está prohibido."
    )

    if user_id not in ia_chats:
        ia_chats[user_id] = []
    ia_chats[user_id].append({"role": "user", "content": message.content})
    ia_chats[user_id] = ia_chats[user_id][-20:]
    mensajes = [{"role": "system", "content": system_prompt}] + ia_chats[user_id]

    async with message.channel.typing():
        try:
            respuesta = await asyncio.to_thread(
                groq_client.chat.completions.create,
                model=MODELO_IA,
                messages=mensajes,
                temperature=0.8,
                max_tokens=1024,
            )
            texto = respuesta.choices[0].message.content
            ia_chats[user_id].append({"role": "assistant", "content": texto})

            if texto.strip().startswith("PONER_CANCION:"):
                nombre = texto.strip().replace("PONER_CANCION:", "").strip()
                await agregar_cancion_desde_ia(message, nombre)
                return

            lineas_limpias = []
            for linea in texto.splitlines():
                if linea.startswith("RECORDAR_SERVER:"):
                    dato = linea.replace("RECORDAR_SERVER:", "").strip()
                    if dato:
                        memoria_server_add(dato)
                elif linea.startswith("RECORDAR:"):
                    dato = linea.replace("RECORDAR:", "").strip()
                    if dato:
                        r = memoria_get(user_id)
                        r.append(dato)
                        memoria_set(user_id, r[-20:])
                else:
                    lineas_limpias.append(linea)

            texto_limpio = re.sub(r"<think>.*?</think>", "", "\n".join(lineas_limpias), flags=re.DOTALL).strip()

            # Si el modelo se puso tibio/defensivo a pesar del prompt, lo reemplazamos
            frases_tibias = [
                "no puedo continuar", "no puedo seguir con esta conversación",
                "hablá con alguien", "necesitás ayuda", "como ia no puedo",
                "no es apropiado", "prefiero no", "no me siento cómodo",
            ]
            if any(f in texto_limpio.lower() for f in frases_tibias):
                texto_limpio = random.choice([
                    "ah mirá qué interesante, bueno segui aaaa",
                    "ya te escuché la primera vez, dale con otra cosa",
                    "uh que pesado, cambiemos de tema mejor",
                    "bueno bueno ya fue, contame algo mejor",
                ])

            for i in range(0, max(len(texto_limpio), 1), 1900):
                await message.reply(texto_limpio[i:i + 1900])

        except Exception as e:
            await message.reply(f"❌ Error al contactar la IA: {e}")

async def agregar_cancion_desde_ia(message: discord.Message, nombre: str):
    global music_voice_client
    if not message.author.voice:
        await message.reply("Querés que ponga música pero no estás en ningún canal de voz 😅")
        return
    if not music_voice_client or not music_voice_client.is_connected():
        music_voice_client = await message.author.voice.channel.connect()
    async with message.channel.typing():
        try:
            data_yt = await asyncio.to_thread(ytdl.extract_info, nombre, download=False)
            entrada = data_yt["entries"][0] if "entries" in data_yt else data_yt
            info = {
                "titulo": entrada.get("title"),
                "url": entrada.get("url"),
                "webpage": entrada.get("webpage_url"),
                "duracion": entrada.get("duration", 0),
                "thumbnail": entrada.get("thumbnail"),
                "uploader": entrada.get("uploader", "Desconocido"),
                "pedido_por": f"IA (pedido por {message.author.display_name})",
            }
            r = ranking_get(message.author.id)
            r["musica"] += 1
            guardar_data()
            music_queue.append(info)
            if not music_voice_client.is_playing() and not music_voice_client.is_paused():
                await reproducir_siguiente(message.channel)
                await message.reply(f"🎵 Puse **{info['titulo']}** como pediste.")
            else:
                await message.reply(f"➕ Agregué **{info['titulo']}** a la cola.")
        except Exception as e:
            await message.reply(f"No pude encontrar esa canción 😕 ({e})")

@bot.command(name="reset")
async def reset_ia(ctx):
    if ctx.channel.id == CANAL_IA:
        ia_chats.pop(ctx.author.id, None)
        await ctx.send(f"{ctx.author.mention} Historial borrado.", delete_after=5)

@bot.command(name="olvidar")
async def olvidar(ctx):
    if ctx.channel.id == CANAL_IA:
        memoria_set(ctx.author.id, [])
        await ctx.send(f"{ctx.author.mention} Borré todo lo que recordaba de vos.", delete_after=5)

@bot.command(name="olvidar_server")
async def olvidar_server(ctx):
    if ctx.author.id == TU_USER_ID:
        data["memoria_server"] = []
        guardar_data()
        await ctx.send("🧹 Borré la memoria compartida del servidor.", delete_after=5)

@bot.command(name="memoria_server")
async def ver_memoria_server(ctx):
    if ctx.author.id == TU_USER_ID:
        mem = memoria_server_get()
        if not mem:
            await ctx.send("La memoria del servidor está vacía.", delete_after=10)
            return
        texto = chr(10).join(f"- {m}" for m in mem)
        await ctx.send(f"**Memoria del servidor:**{chr(10)}{texto}", delete_after=30)

# ─────────────────────────────────────────
#  ROLES
# ─────────────────────────────────────────

class BotonRol(discord.ui.Button):
    def __init__(self, nombre, descripcion):
        super().__init__(label=nombre, style=discord.ButtonStyle.secondary, custom_id=f"rol_{nombre}")
        self.nombre = nombre

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        rol = discord.utils.get(guild.roles, name=self.nombre)
        if not rol:
            rol = await guild.create_role(name=self.nombre)
        if rol in interaction.user.roles:
            await interaction.user.remove_roles(rol)
            await interaction.response.send_message(f"✅ Te saqué el rol **{self.nombre}**.", ephemeral=True)
        else:
            await interaction.user.add_roles(rol)
            await interaction.response.send_message(f"✅ Te asigné el rol **{self.nombre}**.", ephemeral=True)

class PanelRoles(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for rol in ROLES_DISPONIBLES:
            self.add_item(BotonRol(rol["nombre"], rol["descripcion"]))

async def enviar_panel_roles(canal):
    descripcion = "\n".join(f"{r['nombre']} — {r['descripcion']}" for r in ROLES_DISPONIBLES)
    embed = discord.Embed(
        title="🎭 Elegí tu rol",
        description=("Tocá el botón del rol que querés. Podés tener varios. "
                     "Si ya lo tenés, tocarlo de nuevo te lo saca.\n\n" + descripcion),
        color=discord.Color.gold(),
    )
    await canal.send(embed=embed, view=PanelRoles())

# ─────────────────────────────────────────
#  COMANDOS DE ADMIN
# ─────────────────────────────────────────

async def procesar_comando_bot(message: discord.Message):
    texto = message.content.strip()
    canal_destino = message.channel_mentions[0] if message.channel_mentions else None
    if texto.startswith("!anuncio"):
        partes = texto.split(" ", 2)
        if len(partes) < 3 or not canal_destino:
            await message.reply("Uso: `!anuncio #canal Mensaje`"); return
        await canal_destino.send(partes[2].replace(canal_destino.mention, "").strip())
        await message.add_reaction("✅")
    elif texto.startswith("!embed"):
        partes = texto.split(" ", 2)
        if len(partes) < 3 or not canal_destino:
            await message.reply("Uso: `!embed #canal Título | Descripción`"); return
        contenido = partes[2].replace(canal_destino.mention, "").strip()
        titulo, desc = contenido.split("|", 1) if "|" in contenido else ("Anuncio", contenido)
        await canal_destino.send(embed=discord.Embed(
            title=titulo.strip(), description=desc.strip(), color=discord.Color.blurple()))
        await message.add_reaction("✅")
    elif texto.startswith("!limpiar"):
        partes = texto.split()
        cantidad = int(partes[-1]) if partes[-1].isdigit() else 10
        if canal_destino:
            await canal_destino.purge(limit=cantidad)
            await message.reply(f"🧹 Borré {cantidad} mensajes de {canal_destino.mention}.")
        else:
            await message.reply("Mencioná el canal a limpiar.")
    else:
        await message.reply("Comandos: `!anuncio`, `!embed`, `!limpiar`.")


# ─────────────────────────────────────────
#  ECONOMÍA
# ─────────────────────────────────────────

import math as _math

def formato_plata(n: int) -> str:
    """Convierte número a formato mangos/lucas/palos."""
    if n < 0:
        return f"-{formato_plata(-n)}"
    if n < 1_000:
        return f"{n:,} mangos".replace(",", ".")
    if n < 1_000_000:
        lucas = n // 1_000
        mangos = n % 1_000
        if mangos == 0:
            return f"{lucas} lucas"
        return f"{lucas} lucas {mangos} mangos"
    palos = n // 1_000_000
    resto = n % 1_000_000
    lucas = resto // 1_000
    mangos = resto % 1_000
    partes = [f"{palos} {'palo' if palos == 1 else 'palos'}"]
    if lucas: partes.append(f"{lucas} lucas")
    if mangos: partes.append(f"{mangos} mangos")
    return " ".join(partes)

def eco_get(user_id: int) -> dict:
    uid = str(user_id)
    if "economia" not in data:
        data["economia"] = {}
    if uid not in data["economia"]:
        data["economia"][uid] = {
            "nombre": "",
            "balance": 0,
            "trabajo": None,
            "ultimo_trabajo": 0,
            "propiedades": [],
            "vehiculos": [],
            "ultimo_alquiler": {},
            "ultimo_vehiculo": {},
            "daily_ultimo": 0,
        }
    e = data["economia"][uid]
    for k, v in {"balance": 0, "trabajo": None, "ultimo_trabajo": 0,
                 "propiedades": [], "vehiculos": [], "ultimo_alquiler": {},
                 "ultimo_vehiculo": {}, "daily_ultimo": 0}.items():
        if k not in e:
            e[k] = v
    return e

# ── Definición de trabajos ──

TRABAJOS_BAJOS = {
    "cajero":    {"nombre": "Cajero de Toledo",    "accion": "abrir la caja",       "pago": 4_200, "cooldown": 30*60,  "propina": False},
    "verdulero": {"nombre": "Verdulero",            "accion": "acomodar verduras",   "pago": 4_100, "cooldown": 30*60,  "propina": False},
    "carnicero": {"nombre": "Carnicero",            "accion": "cortar la carne",     "pago": 4_500, "cooldown": 30*60,  "propina": False},
    "repositor": {"nombre": "Repositor de Toledo", "accion": "reponer la góndola",  "pago": 4_000, "cooldown": 30*60,  "propina": False},
    "uber":      {"nombre": "Uber",                 "accion": "arrancar el auto",    "pago": 800,   "cooldown": 5*60,   "propina": True},
    "pedidosya": {"nombre": "Pedidos Ya",           "accion": "agarrar la mochila",  "pago": 700,   "cooldown": 5*60,   "propina": True},
    "rappi":     {"nombre": "Rappi",                "accion": "abrir la app",        "pago": 700,   "cooldown": 5*60,   "propina": True},
    "kiosquero": {"nombre": "Kiosquero",            "accion": "abrir el kiosco",     "pago": 4_300, "cooldown": 30*60,  "propina": False},
    "heladero":  {"nombre": "Heladero de Grido",   "accion": "servir el helado",    "pago": 4_000, "cooldown": 30*60,  "propina": False},
    "playero":   {"nombre": "Playero de YPF",      "accion": "cargar el tanque",    "pago": 750,   "cooldown": 5*60,   "propina": True},
    "albanil":   {"nombre": "Albañil",              "accion": "agarrar la pala",     "pago": 6_000, "cooldown": 30*60,  "propina": False},
}

TRABAJOS_MEDIOS = {
    "mecanico":    {"nombre": "Mecánico",        "accion": "levantar el capot",    "pago": 11_500, "cooldown": 60*60, "propina": False, "requisito": 100_000},
    "bombero":     {"nombre": "Bombero",         "accion": "apagar el fuego",      "pago": 11_500, "cooldown": 60*60, "propina": False, "requisito": 100_000},
    "electricista":{"nombre": "Electricista",   "accion": "tocar el cable",       "pago": 12_000, "cooldown": 60*60, "propina": False, "requisito": 110_000},
    "editorvideo": {"nombre": "Editor de Video","accion": "abrir el premiere",    "pago": 11_500, "cooldown": 60*60, "propina": False, "requisito": 110_000},
    "joyero":      {"nombre": "Joyero",          "accion": "limpiar las joyas",    "pago": 12_500, "cooldown": 60*60, "propina": False, "requisito": 120_000},
    "banquero":    {"nombre": "Banquero",        "accion": "abrir el banco",       "pago": 11_500, "cooldown": 60*60, "propina": False, "requisito": 130_000},
}

TRABAJOS_ALTOS = {
    "politico":   {"nombre": "Lic. en Ciencias Políticas","accion": "estafar a alguien",      "pago": 0,      "cooldown": 2*60*60, "propina": False, "pago_variable": (60_000, 67_000), "requisito": 300_000},
    "comex":      {"nombre": "Comercio Exterior",         "accion": "gestionar la importación","pago": 63_000, "cooldown": 2*60*60, "propina": False, "requisito": 350_000},
    "tcp":        {"nombre": "TCP",                        "accion": "atender el vuelo",        "pago": 62_000, "cooldown": 2*60*60, "propina": False, "requisito": 400_000},
    "arquitecto": {"nombre": "Arquitecto",                "accion": "entregar el anteproyecto","pago": 68_000, "cooldown": 2*60*60, "propina": False, "requisito": 450_000},
    "piloto":     {"nombre": "Piloto de Avión",           "accion": "hacer el vuelo",          "pago": 70_000, "cooldown": 2*60*60, "propina": False, "requisito": 500_000},
    "programador":{"nombre": "Programador",               "accion": "terminar el proyecto",    "pago": 76_000, "cooldown": 2*60*60, "propina": False, "requisito": 550_000},
    "traductor":  {"nombre": "Traductor Público de Inglés","accion": "traducir el documento",   "pago": 63_000, "cooldown": 2*60*60, "propina": False, "requisito": 350_000},
}

TODOS_TRABAJOS = {**TRABAJOS_BAJOS, **TRABAJOS_MEDIOS, **TRABAJOS_ALTOS}

SITUACIONES_TRABAJO = {
  "piloto": [
    {
      "descripcion": "Llevaste a un campesino de Azul hasta Bahía Blanca porque perdió el bondi",
      "pago": 65000
    },
    {
      "descripcion": "Transportaste 80kg de repuestos agrícolas hasta Santa Rosa",
      "pago": 58000
    },
    {
      "descripcion": "Llevaste a un médico de urgencia hasta Neuquén con su maletín",
      "pago": 72000
    },
    {
      "descripcion": "Volaste hasta Mendoza con 3 turistas que querían ver la cordillera de cerca",
      "pago": 75000
    },
    {
      "descripcion": "Transportaste documentos confidenciales hasta Córdoba para una escribanía",
      "pago": 60000
    },
    {
      "descripcion": "Llevaste a un veterinario hasta San Luis con 100kg de medicamentos para animales",
      "pago": 68000
    },
    {
      "descripcion": "Volaste hasta Mar del Plata con un empresario que no quería perder el fin de semana",
      "pago": 70000
    },
    {
      "descripcion": "Transportaste 120kg de flores para un casamiento en Rosario",
      "pago": 62000
    },
    {
      "descripcion": "Llevaste a 3 periodistas hasta Salta para cubrir una noticia",
      "pago": 74000
    },
    {
      "descripcion": "Volaste hasta Jujuy con equipo médico de emergencia, 150kg de insumos",
      "pago": 80000
    },
    {
      "descripcion": "Transportaste a un intendente de pueblo hasta Buenos Aires para una reunión",
      "pago": 67000
    },
    {
      "descripcion": "Llevaste 200kg de repuestos industriales hasta Comodoro Rivadavia",
      "pago": 85000
    },
    {
      "descripcion": "Volaste hasta Ushuaia con 3 turistas extranjeros que querían ver pingüinos",
      "pago": 90000
    },
    {
      "descripcion": "Transportaste 250kg de pescado fresco desde Mar del Plata hasta Córdoba",
      "pago": 71000
    },
    {
      "descripcion": "Llevaste a un equipo de fútbol de segunda división hasta Resistencia",
      "pago": 78000
    },
    {
      "descripcion": "Volaste hasta Posadas con 8 ejecutivos para un congreso de negocios",
      "pago": 82000
    },
    {
      "descripcion": "Transportaste 1200kg de maquinaria agrícola hasta La Pampa",
      "pago": 88000
    },
    {
      "descripcion": "Llevaste a 11 turistas hasta Bariloche, casi todos con mareo",
      "pago": 83000
    },
    {
      "descripcion": "Transportaste 600kg de repuestos para una planta petrolera en Neuquén",
      "pago": 79000
    },
    {
      "descripcion": "Volaste hasta San Juan con 5 geólogos y su equipo de relevamiento",
      "pago": 76000
    },
    {
      "descripcion": "Llevaste un caballo pura sangre en el caravan hasta una estancia en Córdoba",
      "pago": 87000
    },
    {
      "descripcion": "Transportaste 1500kg de equipamiento médico hasta un hospital en Salta",
      "pago": 89000
    },
    {
      "descripcion": "Volaste hasta Río Gallegos con un equipo de técnicos petroleros",
      "pago": 86000
    },
    {
      "descripcion": "Llevaste a 13 empleados de una empresa hasta Mendoza para una convención",
      "pago": 84000
    },
    {
      "descripcion": "Transportaste 2000kg de carga urgente para una empresa exportadora hasta Rosario",
      "pago": 88000
    },
    {
      "descripcion": "Volaste hasta Iguazú con 3 pasajeros, uno se olvidó el pasaporte",
      "pago": 73000
    },
    {
      "descripcion": "Llevaste 100kg de vacunas en cadena de frío hasta un pueblo en Chaco",
      "pago": 66000
    },
    {
      "descripcion": "Transportaste a un investigador con equipos científicos hasta la Patagonia",
      "pago": 77000
    },
    {
      "descripcion": "Volaste hasta Entre Ríos con 1 pasajero y su colección de 80kg de vinilos",
      "pago": 61000
    },
    {
      "descripcion": "Llevaste a 8 turistas hasta San Martín de los Andes por un charter privado",
      "pago": 81000
    },
    {
      "descripcion": "Transportaste 350kg de frutas finas desde Mendoza hasta Buenos Aires",
      "pago": 69000
    },
    {
      "descripcion": "Volaste hasta Tucumán con 3 empresarios para ver una planta de azúcar",
      "pago": 64000
    },
    {
      "descripcion": "Llevaste equipo de filmación hasta la Quebrada de Humahuaca, 250kg",
      "pago": 72000
    },
    {
      "descripcion": "Transportaste a un equipo de rescate con 200kg de equipamiento hasta Catamarca",
      "pago": 75000
    },
    {
      "descripcion": "Volaste hasta Santa Cruz con 5 biólogos marinos y sus equipos",
      "pago": 80000
    },
    {
      "descripcion": "Llevaste 1200kg de suministros a una base científica en Tierra del Fuego",
      "pago": 89000
    },
    {
      "descripcion": "Transportaste a 11 músicos con instrumentos hasta Córdoba para un festival",
      "pago": 82000
    },
    {
      "descripcion": "Volaste hasta Villa Mercedes con un cardiólogo de urgencia",
      "pago": 70000
    },
    {
      "descripcion": "Llevaste 600kg de alimentos no perecederos hasta una zona de inundación en Chaco",
      "pago": 78000
    },
    {
      "descripcion": "Transportaste a 8 técnicos de energía renovable hasta un parque eólico patagónico",
      "pago": 85000
    }
  ],
  "arquitecto": [
    {
      "descripcion": "Diseñaste el anteproyecto de un monoambiente de 45m2 para un estudiante en Palermo",
      "pago": 55000
    },
    {
      "descripcion": "Entregaste el plano completo de una casa de 120m2 para una familia en Lomas de Zamora",
      "pago": 68000
    },
    {
      "descripcion": "Hiciste el relevamiento de un edificio de 800m2 en el centro de Córdoba",
      "pago": 72000
    },
    {
      "descripcion": "Diseñaste el anteproyecto de una oficina de 200m2 para una pyme en Microcentro",
      "pago": 65000
    },
    {
      "descripcion": "Entregaste un proyecto ejecutivo de 350m2 para una empresa constructora en Rosario",
      "pago": 78000
    },
    {
      "descripcion": "Hiciste el plano de instalaciones eléctricas de un local de 90m2 en Villa Crespo",
      "pago": 58000
    },
    {
      "descripcion": "Diseñaste una ampliación de 60m2 para una familia en Quilmes, el cliente cambió todo tres veces",
      "pago": 62000
    },
    {
      "descripcion": "Entregaste el anteproyecto de un centro comercial de 2500m2 en Mendoza",
      "pago": 85000
    },
    {
      "descripcion": "Visaste los planos de un edificio de 15 pisos para una constructora porteña",
      "pago": 80000
    },
    {
      "descripcion": "Hiciste el certificado de aptitud de un galpón industrial de 1200m2 en Avellaneda",
      "pago": 70000
    },
    {
      "descripcion": "Diseñaste el anteproyecto de una escuela de 600m2 para el municipio de San Martín",
      "pago": 75000
    },
    {
      "descripcion": "Entregaste el plano completo de una casa de 180m2 con pileta en Tigre",
      "pago": 73000
    },
    {
      "descripcion": "Hiciste el relevamiento de un casco de estancia de 500m2 en la provincia de Buenos Aires",
      "pago": 69000
    },
    {
      "descripcion": "Diseñaste el anteproyecto de un restaurant de 150m2 en San Telmo",
      "pago": 63000
    },
    {
      "descripcion": "Entregaste un proyecto ejecutivo de hospital de 3000m2 para el gobierno provincial",
      "pago": 85000
    },
    {
      "descripcion": "Hiciste el plano de una cochera subterránea de 400m2 en Belgrano",
      "pago": 71000
    },
    {
      "descripcion": "Diseñaste la reforma de un departamento de 85m2 en Recoleta para un inversor",
      "pago": 60000
    },
    {
      "descripcion": "Entregaste el anteproyecto de un complejo de cabañas de 800m2 en Bariloche",
      "pago": 77000
    },
    {
      "descripcion": "Hiciste el certificado de aptitud de un edificio de propiedad horizontal en Flores",
      "pago": 59000
    },
    {
      "descripcion": "Diseñaste el plano de una nave industrial de 2000m2 en Pilar",
      "pago": 82000
    },
    {
      "descripcion": "Entregaste el proyecto ejecutivo de un supermercado de 1800m2 en Mar del Plata",
      "pago": 83000
    },
    {
      "descripcion": "Hiciste el relevamiento de un edificio histórico de 700m2 en Montevideo para restauración",
      "pago": 74000
    },
    {
      "descripcion": "Diseñaste el anteproyecto de un estadio de 5000 espectadores para un club de barrio",
      "pago": 85000
    },
    {
      "descripcion": "Entregaste el plano de una guardería de 250m2, el intendente quería la pileta adentro",
      "pago": 64000
    },
    {
      "descripcion": "Hiciste el proyecto de instalaciones sanitarias de un edificio de 12 pisos en Caballito",
      "pago": 68000
    },
    {
      "descripcion": "Diseñaste una vivienda sustentable de 100m2 con paneles solares en Mendoza",
      "pago": 67000
    },
    {
      "descripcion": "Entregaste el anteproyecto de un hotel boutique de 600m2 en Salta",
      "pago": 76000
    },
    {
      "descripcion": "Hiciste el visado de planos de un complejo de oficinas de 4000m2 en Puerto Madero",
      "pago": 85000
    },
    {
      "descripcion": "Diseñaste el plano de una clínica veterinaria de 180m2 en Palermo",
      "pago": 62000
    },
    {
      "descripcion": "Entregaste el proyecto ejecutivo de 15 casas en un barrio cerrado de Pilar",
      "pago": 84000
    },
    {
      "descripcion": "Hiciste el relevamiento de una fábrica textil de 1500m2 en Lanús",
      "pago": 72000
    },
    {
      "descripcion": "Diseñaste el anteproyecto de un museo de 900m2 para la municipalidad de Rosario",
      "pago": 79000
    },
    {
      "descripcion": "Entregaste el plano de una panadería de 70m2, el dueño tenía ideas muy creativas",
      "pago": 57000
    },
    {
      "descripcion": "Hiciste el proyecto de un edificio de 20 departamentos de 65m2 cada uno en Almagro",
      "pago": 85000
    },
    {
      "descripcion": "Diseñaste la remodelación de un teatro de 400 butacas en el centro de Córdoba",
      "pago": 80000
    },
    {
      "descripcion": "Entregaste el anteproyecto de un polideportivo de 2200m2 para el gobierno bonaerense",
      "pago": 85000
    },
    {
      "descripcion": "Hiciste el plano de una heladería de 55m2 en Villa Urquiza, muy detallado para tan chiquito",
      "pago": 56000
    },
    {
      "descripcion": "Diseñaste el proyecto ejecutivo de un shopping de 8000m2 en Tucumán",
      "pago": 85000
    },
    {
      "descripcion": "Entregaste el relevamiento de un PH de 110m2 en Palermo para una constructora",
      "pago": 61000
    },
    {
      "descripcion": "Hiciste el anteproyecto de un edificio corporativo de 6000m2 en el norte de GBA",
      "pago": 85000
    }
  ],
  "programador": [
    {
      "descripcion": "Desarrollaste una app de delivery para una pyme porteña usando React Native",
      "pago": 75000
    },
    {
      "descripcion": "Hiciste el backend de un sistema de gestión para una clínica en Python/Django",
      "pago": 80000
    },
    {
      "descripcion": "Armaste un bot de WhatsApp para una inmobiliaria de Córdoba",
      "pago": 65000
    },
    {
      "descripcion": "Desarrollaste el e-commerce de una tienda de ropa en Shopify personalizado",
      "pago": 70000
    },
    {
      "descripcion": "Hiciste una API REST para una fintech startup, el CEO cambiaba los requisitos cada día",
      "pago": 85000
    },
    {
      "descripcion": "Armaste un sistema de facturación conectado con AFIP para una pyme",
      "pago": 78000
    },
    {
      "descripcion": "Desarrollaste un dashboard de analytics en React para una empresa de marketing",
      "pago": 72000
    },
    {
      "descripcion": "Hiciste el sitio web de un municipio en el interior, con CMS incluido",
      "pago": 63000
    },
    {
      "descripcion": "Armaste un juego mobile casual en Unity para una startup indie argentina",
      "pago": 68000
    },
    {
      "descripcion": "Desarrollaste un script de automatización en Python que ahorró 40 horas semanales a una empresa",
      "pago": 60000
    },
    {
      "descripcion": "Hiciste la migración de una base de datos legacy de Oracle a PostgreSQL, un desastre total",
      "pago": 90000
    },
    {
      "descripcion": "Armaste una app de turnos online para una cadena de peluquerías",
      "pago": 67000
    },
    {
      "descripcion": "Desarrollaste el sistema bancario interno de una cooperativa de crédito",
      "pago": 93000
    },
    {
      "descripcion": "Hiciste una plataforma de cursos online tipo Udemy para una institución educativa",
      "pago": 82000
    },
    {
      "descripcion": "Armaste un bot de Discord con economía completa para un servidor de amigos",
      "pago": 95000
    },
    {
      "descripcion": "Desarrollaste una app de gestión de stock para una ferretería, el dueño quería Excel",
      "pago": 62000
    },
    {
      "descripcion": "Hiciste el backend de una red social para artistas plásticos argentinos en Node.js",
      "pago": 77000
    },
    {
      "descripcion": "Armaste un sistema de votación online para una empresa, con blockchain incluido",
      "pago": 88000
    },
    {
      "descripcion": "Desarrollaste una app de seguimiento de pedidos para una empresa de logística",
      "pago": 73000
    },
    {
      "descripcion": "Hiciste el deploy de toda la infraestructura AWS de una startup, todo en un día",
      "pago": 91000
    },
    {
      "descripcion": "Armaste un scraper para recolectar precios de la competencia, muy eficiente",
      "pago": 61000
    },
    {
      "descripcion": "Desarrollaste una app de control de asistencia con reconocimiento facial para una fábrica",
      "pago": 84000
    },
    {
      "descripcion": "Hiciste el sistema de reservas de un hotel boutique en Salta",
      "pago": 69000
    },
    {
      "descripcion": "Armaste una PWA para una ONG que conecta voluntarios con necesidades en CABA",
      "pago": 64000
    },
    {
      "descripcion": "Desarrollaste el módulo de pagos con MercadoPago para un e-commerce",
      "pago": 71000
    },
    {
      "descripcion": "Hiciste un sistema de gestión de turnos para el PAMI de un municipio bonaerense",
      "pago": 76000
    },
    {
      "descripcion": "Armaste una API para integrar un ERP legacy con un sistema moderno, pesadilla total",
      "pago": 92000
    },
    {
      "descripcion": "Desarrollaste un chatbot con IA para atención al cliente de un banco digital",
      "pago": 89000
    },
    {
      "descripcion": "Hiciste una app de monitoreo de calidad del aire para una empresa ambiental",
      "pago": 74000
    },
    {
      "descripcion": "Armaste el sistema de gestión de una cadena de gimnasios, 15 sucursales",
      "pago": 86000
    },
    {
      "descripcion": "Desarrollaste un juego en Python que el cliente describió como 'tipo Minecraft pero distinto'",
      "pago": 66000
    },
    {
      "descripcion": "Hiciste la integración de un CRM con una plataforma de email marketing",
      "pago": 68000
    },
    {
      "descripcion": "Armaste un sistema de subastas online para una empresa de remates",
      "pago": 79000
    },
    {
      "descripcion": "Desarrollaste una app de delivery de medicamentos para una farmacia de barrio",
      "pago": 70000
    },
    {
      "descripcion": "Hiciste un sistema de gestión hospitalaria para una clínica privada en Mendoza",
      "pago": 94000
    },
    {
      "descripcion": "Armaste un marketplace de servicios freelance para profesionales argentinos",
      "pago": 87000
    },
    {
      "descripcion": "Desarrollaste la app mobile de un banco cooperativo, iOS y Android",
      "pago": 95000
    },
    {
      "descripcion": "Hiciste el sistema de control de acceso con QR para un edificio corporativo",
      "pago": 72000
    },
    {
      "descripcion": "Armaste un bot de Telegram para alertas de precios de criptomonedas",
      "pago": 60000
    },
    {
      "descripcion": "Desarrollaste una plataforma de telemedicina para una prepaga, funcionó en el primer intento",
      "pago": 95000
    }
  ],
  "tcp": [
    {
      "descripcion": "Atendiste el vuelo Buenos Aires-Bariloche, había turbulencia y todos se quejaban",
      "pago": 52000
    },
    {
      "descripcion": "Trabajaste en el vuelo CABA-Miami, 10 horas de viaje con un pasajero que llamó 15 veces",
      "pago": 74000
    },
    {
      "descripcion": "Cubriste el vuelo Buenos Aires-Madrid, escala en San Pablo, llegaste hecho pomada",
      "pago": 75000
    },
    {
      "descripcion": "Atendiste el vuelo Córdoba-Salta, corto pero intenso, el señor de 4A quería milanesa",
      "pago": 50000
    },
    {
      "descripcion": "Trabajaste en un charter privado Buenos Aires-Mendoza para una empresa vitivinícola",
      "pago": 68000
    },
    {
      "descripcion": "Cubriste el vuelo CABA-Ushuaia, la mitad del avión no había volado nunca",
      "pago": 58000
    },
    {
      "descripcion": "Atendiste el vuelo Buenos Aires-Lima, con conexión a Ciudad de México",
      "pago": 73000
    },
    {
      "descripcion": "Trabajaste en el vuelo CABA-Iguazú, lleno de turistas con cámaras gigantes",
      "pago": 55000
    },
    {
      "descripcion": "Cubriste el vuelo Córdoba-Buenos Aires, el más corto de tu vida, casi no alcanzaste a servir el café",
      "pago": 50000
    },
    {
      "descripcion": "Atendiste el vuelo Buenos Aires-Nueva York, 13 horas y un bebé llorando todo el tiempo",
      "pago": 75000
    },
    {
      "descripcion": "Trabajaste en el vuelo CABA-Río de Janeiro, lleno de argentinos yendo al carnaval",
      "pago": 65000
    },
    {
      "descripcion": "Cubriste el vuelo Mendoza-Buenos Aires, llegaron 20 minutos antes, un récord",
      "pago": 53000
    },
    {
      "descripcion": "Atendiste el vuelo Buenos Aires-Santiago de Chile, turbulencia en la cordillera, pánico general",
      "pago": 60000
    },
    {
      "descripcion": "Trabajaste en un vuelo charter para un equipo de fútbol que iba a jugar en Brasil",
      "pago": 70000
    },
    {
      "descripcion": "Cubriste el vuelo Salta-Buenos Aires, un pasajero intentó cocinar en el baño",
      "pago": 57000
    },
    {
      "descripcion": "Atendiste el vuelo CABA-Bogotá, 5 horas con una delegación de empresarios",
      "pago": 67000
    },
    {
      "descripcion": "Trabajaste en el vuelo Buenos Aires-Toronto, el pasajero de 12C no paró de hablar",
      "pago": 75000
    },
    {
      "descripcion": "Cubriste el vuelo Bariloche-Buenos Aires, todos venían de esquiar y estaban felices",
      "pago": 54000
    },
    {
      "descripcion": "Atendiste el vuelo CABA-Asunción, el más tranquilo del mes",
      "pago": 51000
    },
    {
      "descripcion": "Trabajaste en un vuelo de repatriación desde Roma, 14 horas de viaje muy emocionante",
      "pago": 75000
    },
    {
      "descripcion": "Cubriste el vuelo Tucumán-Buenos Aires, llovía y el despegue se retrasó 2 horas",
      "pago": 52000
    },
    {
      "descripcion": "Atendiste el vuelo CABA-Cancún, lleno de recién casados en luna de miel",
      "pago": 69000
    },
    {
      "descripcion": "Trabajaste en el vuelo Buenos Aires-Tel Aviv, pasajero vegetariano estricto a cada rato",
      "pago": 75000
    },
    {
      "descripcion": "Cubriste el vuelo Mar del Plata-Buenos Aires, de 40 minutos, alcanzaste a servir agua",
      "pago": 50000
    },
    {
      "descripcion": "Atendiste el vuelo CABA-Tokio con escala en Dallas, 24 horas de viaje total",
      "pago": 75000
    },
    {
      "descripcion": "Trabajaste en el vuelo Córdoba-Mendoza, cortito y sin sobresaltos",
      "pago": 51000
    },
    {
      "descripcion": "Cubriste el vuelo Buenos Aires-Londres, uno de los pasajeros era famoso y nadie lo dejó tranquilo",
      "pago": 75000
    },
    {
      "descripcion": "Atendiste el vuelo CABA-Montevideo, tan corto que casi ni despegaron",
      "pago": 50000
    },
    {
      "descripcion": "Trabajaste en un vuelo humanitario llevando médicos a zona de desastre en Paraguay",
      "pago": 72000
    },
    {
      "descripcion": "Cubriste el vuelo Resistencia-Buenos Aires, el avión olía a chipá, nadie se quejó",
      "pago": 53000
    },
    {
      "descripcion": "Atendiste el vuelo CABA-Los Ángeles, 15 horas, te dormiste parado en una escala",
      "pago": 75000
    },
    {
      "descripcion": "Trabajaste en el vuelo Buenos Aires-Porto Alegre, ida y vuelta en el día, agotador",
      "pago": 61000
    },
    {
      "descripcion": "Cubriste el vuelo Neuquén-Buenos Aires, olía a petróleo, los técnicos venían de turno",
      "pago": 54000
    },
    {
      "descripcion": "Atendiste el vuelo CABA-Frankfurt, delegación universitaria muy educada",
      "pago": 75000
    },
    {
      "descripcion": "Trabajaste en el vuelo Jujuy-Buenos Aires, todos traían artesanías gigantes de mano",
      "pago": 55000
    },
    {
      "descripcion": "Cubriste el vuelo Buenos Aires-La Paz, la altitud de destino asustó a varios pasajeros",
      "pago": 64000
    },
    {
      "descripcion": "Atendiste el vuelo CABA-Caracas, lleno de venezolanos volviendo a visitar familia",
      "pago": 68000
    },
    {
      "descripcion": "Trabajaste en un vuelo privado de una empresa minera hasta Catamarca",
      "pago": 66000
    },
    {
      "descripcion": "Cubriste el vuelo Posadas-Buenos Aires, el avión venía con olor a yerba, reconfortante",
      "pago": 52000
    },
    {
      "descripcion": "Atendiste el vuelo Buenos Aires-Sydney, 20 horas, el pasajero de 33C vio 6 películas seguidas",
      "pago": 75000
    }
  ],
  "comex": [
    {
      "descripcion": "Gestionaste la importación de 5 toneladas de electrónica desde Shenzhen, la AFIP quería todo duplicado",
      "pago": 68000
    },
    {
      "descripcion": "Hiciste el despacho de aduana de un contenedor de ropa desde Bangladesh",
      "pago": 62000
    },
    {
      "descripcion": "Tramitaste el certificado de origen para exportar aceite de oliva a Italia",
      "pago": 55000
    },
    {
      "descripcion": "Gestionaste la licencia de importación de maquinaria alemana para una fábrica bonaerense",
      "pago": 72000
    },
    {
      "descripcion": "Hiciste el SIRA de 200 unidades de celulares desde Miami, la AFIP tardó 3 semanas",
      "pago": 70000
    },
    {
      "descripcion": "Tramitaste la exportación de 50 toneladas de soja a China, papelerío interminable",
      "pago": 75000
    },
    {
      "descripcion": "Gestionaste el despacho de aduana de autos importados desde Brasil para una concesionaria",
      "pago": 65000
    },
    {
      "descripcion": "Hiciste el trámite de importación de insumos médicos desde Alemania para una clínica",
      "pago": 63000
    },
    {
      "descripcion": "Tramitaste el certificado fitosanitario para exportar peras patagónicas a Europa",
      "pago": 57000
    },
    {
      "descripcion": "Gestionaste la importación de repuestos industriales desde Corea, la aduana no encontraba el código arancelario",
      "pago": 69000
    },
    {
      "descripcion": "Hiciste el despacho de urgencia de equipos de telecomunicaciones desde EEUU",
      "pago": 73000
    },
    {
      "descripcion": "Tramitaste la exportación de vino malbec a Japón, con todos los certificados que pedían",
      "pago": 61000
    },
    {
      "descripcion": "Gestionaste la importación de 10 toneladas de telas desde India para una textil",
      "pago": 60000
    },
    {
      "descripcion": "Hiciste el SIRA de notebooks para una empresa tech, la AFIP rechazó todo dos veces",
      "pago": 67000
    },
    {
      "descripcion": "Tramitaste la exportación de limones tucumanos a España, cuarentena vegetal incluida",
      "pago": 58000
    },
    {
      "descripcion": "Gestionaste la importación de un molino eólico desde Dinamarca en 15 piezas",
      "pago": 76000
    },
    {
      "descripcion": "Hiciste el despacho de aduana de muebles de diseño importados desde Italia",
      "pago": 64000
    },
    {
      "descripcion": "Tramitaste la exportación de carne vacuna a China, con todos los protocolos sanitarios",
      "pago": 74000
    },
    {
      "descripcion": "Gestionaste la importación de cámaras de seguridad desde Taiwán para una empresa",
      "pago": 62000
    },
    {
      "descripcion": "Hiciste el trámite de importación temporaria de equipos de filmación extranjeros",
      "pago": 59000
    },
    {
      "descripcion": "Tramitaste la exportación de software a Uruguay, extrañamente complicado",
      "pago": 55000
    },
    {
      "descripcion": "Gestionaste la importación de tractores brasileños para una cooperativa agrícola",
      "pago": 71000
    },
    {
      "descripcion": "Hiciste el despacho de aduana de joyas importadas desde España, mucho papeleo",
      "pago": 66000
    },
    {
      "descripcion": "Tramitaste la exportación de mariscos patagónicos a Francia, con cadena de frío certificada",
      "pago": 69000
    },
    {
      "descripcion": "Gestionaste la importación de paneles solares chinos para un parque fotovoltaico",
      "pago": 73000
    },
    {
      "descripcion": "Hiciste el SIRA de componentes electrónicos, rechazaron 3 veces por errores tipográficos",
      "pago": 68000
    },
    {
      "descripcion": "Tramitaste la exportación de miel entrerriana a Alemania, certificación orgánica incluida",
      "pago": 60000
    },
    {
      "descripcion": "Gestionaste la importación de libros desde España para una editorial, aranceles culturales",
      "pago": 56000
    },
    {
      "descripcion": "Hiciste el despacho de aduana de un cargamento de perfumes desde Francia",
      "pago": 64000
    },
    {
      "descripcion": "Tramitaste la exportación de aceite de girasol a Turquía, primer cliente nuevo del año",
      "pago": 62000
    },
    {
      "descripcion": "Gestionaste la importación de juguetes desde China para una juguetería, en diciembre",
      "pago": 70000
    },
    {
      "descripcion": "Hiciste el trámite de reexportación de mercadería en tránsito desde Chile a Brasil",
      "pago": 65000
    },
    {
      "descripcion": "Tramitaste la importación de instrumentos musicales desde EEUU para una orquesta",
      "pago": 59000
    },
    {
      "descripcion": "Gestionaste la exportación de software de gestión a Paraguay, sin precedentes aduaneros",
      "pago": 57000
    },
    {
      "descripcion": "Hiciste el despacho de un contenedor de electrodomésticos desde Brasil, todo en regla por una vez",
      "pago": 63000
    },
    {
      "descripcion": "Tramitaste la importación de insumos farmacéuticos desde India con habilitación ANMAT",
      "pago": 77000
    },
    {
      "descripcion": "Gestionaste la exportación de cueros curtidos a Italia para una marca de lujo",
      "pago": 72000
    },
    {
      "descripcion": "Hiciste el SIRA de 500 impresoras, la AFIP las clasificó como artículos de lujo",
      "pago": 69000
    },
    {
      "descripcion": "Tramitaste la exportación de aluminio patagónico a Japón, logística impecable",
      "pago": 74000
    },
    {
      "descripcion": "Gestionaste la importación de semillas certificadas desde Holanda para el INTA",
      "pago": 66000
    }
  ],
  "politico": [
    {
      "descripcion": "Convenciste a un jubilado de afiliarse a un partido prometiéndole que le iban a subir la jubilación",
      "pago": 62000
    },
    {
      "descripcion": "Recaudaste fondos para la campaña de un intendente de pueblo prometiendo obras que nunca se harán",
      "pago": 65000
    },
    {
      "descripcion": "Armaste una lista de candidatos para las PASO con nombres inventados para inflar la boleta",
      "pago": 61000
    },
    {
      "descripcion": "Convenciste a un empresario de donar a un movimiento político a cambio de un contrato fantasma",
      "pago": 67000
    },
    {
      "descripcion": "Organizaste un acto político con choripanes gratis, vinieron 200 personas que no eran militantes",
      "pago": 60000
    },
    {
      "descripcion": "Tramitaste un subsidio municipal para una asociación civil que no existe",
      "pago": 66000
    },
    {
      "descripcion": "Convenciste a un militante joven de hacer campaña sin cobrar porque 'es por la causa'",
      "pago": 62000
    },
    {
      "descripcion": "Armaste un informe de gestión lleno de fotos de obras de otros gobiernos",
      "pago": 63000
    },
    {
      "descripcion": "Recaudaste donaciones para una fundación política que tiene los mismos datos que la campaña",
      "pago": 65000
    },
    {
      "descripcion": "Convenciste a un puntero de trabajar gratis el día del comicio a cambio de 'reconocimiento futuro'",
      "pago": 60000
    },
    {
      "descripcion": "Organizaste una marcha de apoyo con acarreo incluido, 50 pesos por cabeza",
      "pago": 64000
    },
    {
      "descripcion": "Tramitaste un cargo de asesor ad honorem que en realidad cobraba por cheque de terceros",
      "pago": 67000
    },
    {
      "descripcion": "Convenciste a un intendente de la costa de que necesitaba una consultora política urgente",
      "pago": 66000
    },
    {
      "descripcion": "Armaste un plan de comunicación para un candidato que no quería salir en fotos",
      "pago": 61000
    },
    {
      "descripcion": "Recaudaste fondos para 'las víctimas de las inundaciones' que fueron al comité",
      "pago": 65000
    },
    {
      "descripcion": "Convenciste a 30 empleados municipales de votar en blanco para beneficiar al candidato propio",
      "pago": 63000
    },
    {
      "descripcion": "Tramitaste una licitación direccionada para una empresa de un primo del secretario",
      "pago": 67000
    },
    {
      "descripcion": "Armaste una encuesta con metodología propia que daba exactamente lo que el cliente quería",
      "pago": 62000
    },
    {
      "descripcion": "Convenciste a una ONG de avalar el plan de gobierno a cambio de un subsidio",
      "pago": 64000
    },
    {
      "descripcion": "Organizaste un debate donde el candidato propio tenía las preguntas de antemano",
      "pago": 66000
    },
    {
      "descripcion": "Tramitaste la inscripción de un partido nuevo con 4000 firmas que firmaron los mismos 400",
      "pago": 65000
    },
    {
      "descripcion": "Convenciste a un senador de apoyar un proyecto que beneficiaba a tu cliente empresario",
      "pago": 67000
    },
    {
      "descripcion": "Armaste un sistema de padrones paralelos para controlar la territorialidad del comité",
      "pago": 63000
    },
    {
      "descripcion": "Recaudaste para la 'campaña de transparencia' de un partido con cuentas en el exterior",
      "pago": 65000
    },
    {
      "descripcion": "Convenciste a vecinos de firmar una petición sin decirles exactamente qué pedía",
      "pago": 60000
    },
    {
      "descripcion": "Tramitaste contratos de obra pública con sobreprecios del 40% para el partido",
      "pago": 67000
    },
    {
      "descripcion": "Armaste un programa social que distribuía bolsones a cambio de asistir a actos",
      "pago": 64000
    },
    {
      "descripcion": "Convenciste a una radio comunitaria de bajar el programa de un opositor",
      "pago": 62000
    },
    {
      "descripcion": "Organizaste un acto de cierre de campaña con artistas que cobraron en negro",
      "pago": 65000
    },
    {
      "descripcion": "Tramitaste una pensión graciable para el cuñado del concejal a cambio de apoyo legislativo",
      "pago": 66000
    },
    {
      "descripcion": "Convenciste a un gremio de apoyar al candidato a cambio de obra social para sus afiliados",
      "pago": 67000
    },
    {
      "descripcion": "Armaste un informe de impacto ambiental favorable para una obra que claramente dañaba el ambiente",
      "pago": 63000
    },
    {
      "descripcion": "Recaudaste para 'el fondo de campaña' usando cajas de un organismo público",
      "pago": 67000
    },
    {
      "descripcion": "Convenciste a un concejal de votar una ordenanza que nadie entendió bien qué decía",
      "pago": 64000
    },
    {
      "descripcion": "Tramitaste la habilitación municipal de un local sin cumplir ninguno de los requisitos",
      "pago": 65000
    },
    {
      "descripcion": "Armaste una red de voluntarios que eran en realidad empleados del municipio en horario laboral",
      "pago": 66000
    },
    {
      "descripcion": "Convenciste a un medio local de publicar una nota pagada sin aclarar que era publicidad",
      "pago": 62000
    },
    {
      "descripcion": "Organizaste una jornada de 'participación ciudadana' donde solo participaron los afiliados del partido",
      "pago": 63000
    },
    {
      "descripcion": "Tramitaste el nombramiento de 15 asesores fantasmas en la legislatura provincial",
      "pago": 67000
    },
    {
      "descripcion": "Convenciste a un candidato de financiar su campaña con fondos de su propia empresa declarados como gastos",
      "pago": 67000
    }
  ],
  "traductor": [
    {
      "descripcion": "Tradujiste un contrato de 80 páginas para una empresa importadora de electrónica",
      "pago": 65000
    },
    {
      "descripcion": "Certificaste la traducción del título universitario de un médico que se va a España",
      "pago": 60000
    },
    {
      "descripcion": "Tradujiste los subtítulos de una serie indie americana para una plataforma de streaming",
      "pago": 63000
    },
    {
      "descripcion": "Certificaste documentos migratorios para una familia venezolana que pedía residencia",
      "pago": 61000
    },
    {
      "descripcion": "Tradujiste el manual técnico de una maquinaria industrial alemana de 200 páginas",
      "pago": 68000
    },
    {
      "descripcion": "Certificaste la traducción de un testamento de un argentino fallecido en EEUU",
      "pago": 64000
    },
    {
      "descripcion": "Tradujiste los términos y condiciones de una app americana para el mercado local",
      "pago": 62000
    },
    {
      "descripcion": "Certificaste documentos de adopción internacional de una pareja argentina",
      "pago": 65000
    },
    {
      "descripcion": "Tradujiste el guión de una película independiente norteamericana para un festival",
      "pago": 63000
    },
    {
      "descripcion": "Certificaste la traducción de un título de propiedad de un inmueble en Miami",
      "pago": 66000
    },
    {
      "descripcion": "Tradujiste un informe de auditoría de 150 páginas para una multinacional en Argentina",
      "pago": 70000
    },
    {
      "descripcion": "Certificaste la partida de nacimiento de alguien que se quería casar con un extranjero",
      "pago": 60000
    },
    {
      "descripcion": "Tradujiste los papers científicos de un investigador del CONICET para publicar en el exterior",
      "pago": 64000
    },
    {
      "descripcion": "Certificaste un poder notarial para una empresa que operaba en EEUU",
      "pago": 62000
    },
    {
      "descripcion": "Tradujiste el reglamento de un juego de mesa importado, 40 páginas de reglas confusas",
      "pago": 61000
    },
    {
      "descripcion": "Certificaste documentos judiciales para un proceso legal internacional",
      "pago": 67000
    },
    {
      "descripcion": "Tradujiste el prospecto de un medicamento importado que aún no tenía registro en ANMAT",
      "pago": 63000
    },
    {
      "descripcion": "Certificaste la traducción de un contrato laboral para un argentino que trabajaba en remoto para EEUU",
      "pago": 65000
    },
    {
      "descripcion": "Tradujiste un videojuego indie completo: menús, diálogos y tutoriales",
      "pago": 69000
    },
    {
      "descripcion": "Certificaste documentos de un fideicomiso offshore para una familia adinerada",
      "pago": 68000
    },
    {
      "descripcion": "Tradujiste el manual de usuario de una impresora industrial que nadie leía igual",
      "pago": 60000
    },
    {
      "descripcion": "Certificaste la traducción del DNI y pasaporte de un turista que tuvo problemas legales",
      "pago": 61000
    },
    {
      "descripcion": "Tradujiste los estatutos de una ONG internacional que se instalaba en Argentina",
      "pago": 64000
    },
    {
      "descripcion": "Certificaste documentos escolares para un chico que se mudaba a Canadá con su familia",
      "pago": 62000
    },
    {
      "descripcion": "Tradujiste un libro técnico de ingeniería aeronáutica para la ANAC",
      "pago": 70000
    },
    {
      "descripcion": "Certificaste la traducción de un acuerdo prenupcial entre un argentino y una estadounidense",
      "pago": 65000
    },
    {
      "descripcion": "Tradujiste el informe médico de un paciente que se operaba en el exterior",
      "pago": 63000
    },
    {
      "descripcion": "Certificaste documentos de una sucesión internacional con bienes en tres países",
      "pago": 67000
    },
    {
      "descripcion": "Tradujiste los subtítulos de un documental sobre la naturaleza patagónica para Netflix",
      "pago": 66000
    },
    {
      "descripcion": "Certificaste la traducción del antecedente penal de alguien que pedía visa americana",
      "pago": 60000
    },
    {
      "descripcion": "Tradujiste el contrato de licencia de software de una empresa de Silicon Valley",
      "pago": 64000
    },
    {
      "descripcion": "Certificaste documentos de una empresa que quería cotizar en la bolsa de Nueva York",
      "pago": 70000
    },
    {
      "descripcion": "Tradujiste el reglamento interno de una cadena de franquicias americana que llegaba al país",
      "pago": 63000
    },
    {
      "descripcion": "Certificaste la traducción de un diploma de honor de un deportista olímpico argentino",
      "pago": 61000
    },
    {
      "descripcion": "Tradujiste los informes financieros de una empresa para una auditoría internacional",
      "pago": 68000
    },
    {
      "descripcion": "Certificaste documentos de inmigración para un coreano que se asentaba en Argentina",
      "pago": 62000
    },
    {
      "descripcion": "Tradujiste un contrato de distribución exclusiva entre una empresa argentina y una americana",
      "pago": 66000
    },
    {
      "descripcion": "Certificaste la traducción de un acuerdo de confidencialidad corporativo multinacional",
      "pago": 65000
    },
    {
      "descripcion": "Tradujiste el protocolo sanitario de una empresa farmacéutica internacional para la ANMAT",
      "pago": 67000
    },
    {
      "descripcion": "Certificaste el historial académico completo de un estudiante que aplicaba a un posgrado en EEUU",
      "pago": 63000
    }
  ]
}


# ─────────────────────────────────────────
#  AUTOMÓVILES
# ─────────────────────────────────────────

AUTOS = {
    # CLASE BAJA
    "fiat600":      {"nombre": "Fiat 600",          "precio": 80_000,    "clase": "baja",       "trabajo": "delivery",    "ingreso": 1_200,  "cooldown": 10*60},
    "fiat128":      {"nombre": "Fiat 128",          "precio": 90_000,    "clase": "baja",       "trabajo": "delivery",    "ingreso": 1_300,  "cooldown": 10*60},
    "fiat147":      {"nombre": "Fiat 147",          "precio": 95_000,    "clase": "baja",       "trabajo": "delivery",    "ingreso": 1_300,  "cooldown": 10*60},
    "r12":          {"nombre": "Renault 12",        "precio": 100_000,   "clase": "baja",       "trabajo": "delivery",    "ingreso": 1_400,  "cooldown": 10*60},
    "r9":           {"nombre": "Renault 9",         "precio": 105_000,   "clase": "baja",       "trabajo": "delivery",    "ingreso": 1_400,  "cooldown": 10*60},
    "r11":          {"nombre": "Renault 11",        "precio": 110_000,   "clase": "baja",       "trabajo": "cadeteria",   "ingreso": 1_500,  "cooldown": 10*60},
    "fiatduna":     {"nombre": "Fiat Duna",         "precio": 115_000,   "clase": "baja",       "trabajo": "cadeteria",   "ingreso": 1_500,  "cooldown": 10*60},
    "fiatuno":      {"nombre": "Fiat Uno",          "precio": 120_000,   "clase": "baja",       "trabajo": "cadeteria",   "ingreso": 1_600,  "cooldown": 10*60},
    "senda":        {"nombre": "VW Senda",          "precio": 130_000,   "clase": "baja",       "trabajo": "cadeteria",   "ingreso": 1_700,  "cooldown": 10*60},
    "golg1":        {"nombre": "VW Gol G1",         "precio": 140_000,   "clase": "baja",       "trabajo": "cadeteria",   "ingreso": 1_800,  "cooldown": 10*60},
    # CLASE MEDIA BAJA
    "corsa":        {"nombre": "Chevrolet Corsa",   "precio": 280_000,   "clase": "media_baja", "trabajo": "remis",       "ingreso": 3_500,  "cooldown": 8*60},
    "ka":           {"nombre": "Ford Ka",           "precio": 300_000,   "clase": "media_baja", "trabajo": "remis",       "ingreso": 3_600,  "cooldown": 8*60},
    "goltrend":     {"nombre": "VW Gol Trend",      "precio": 320_000,   "clase": "media_baja", "trabajo": "remis",       "ingreso": 3_800,  "cooldown": 8*60},
    "clio":         {"nombre": "Renault Clio",      "precio": 330_000,   "clase": "media_baja", "trabajo": "remis",       "ingreso": 3_900,  "cooldown": 8*60},
    "p206":         {"nombre": "Peugeot 206",       "precio": 340_000,   "clase": "media_baja", "trabajo": "remis",       "ingreso": 4_000,  "cooldown": 8*60},
    "p207":         {"nombre": "Peugeot 207",       "precio": 360_000,   "clase": "media_baja", "trabajo": "remis",       "ingreso": 4_200,  "cooldown": 8*60},
    "palio":        {"nombre": "Fiat Palio",        "precio": 370_000,   "clase": "media_baja", "trabajo": "uber_auto",   "ingreso": 4_500,  "cooldown": 8*60},
    "siena":        {"nombre": "Fiat Siena",        "precio": 380_000,   "clase": "media_baja", "trabajo": "uber_auto",   "ingreso": 4_600,  "cooldown": 8*60},
    "agile":        {"nombre": "Chevrolet Agile",   "precio": 400_000,   "clase": "media_baja", "trabajo": "uber_auto",   "ingreso": 4_800,  "cooldown": 8*60},
    "etios":        {"nombre": "Toyota Etios",      "precio": 420_000,   "clase": "media_baja", "trabajo": "uber_auto",   "ingreso": 5_000,  "cooldown": 8*60},
    # CLASE MEDIA
    "polo":         {"nombre": "VW Polo",           "precio": 700_000,   "clase": "media",      "trabajo": "uber_auto",   "ingreso": 7_000,  "cooldown": 8*60},
    "onix":         {"nombre": "Chevrolet Onix",    "precio": 750_000,   "clase": "media",      "trabajo": "uber_auto",   "ingreso": 7_500,  "cooldown": 8*60},
    "versa":        {"nombre": "Nissan Versa",      "precio": 800_000,   "clase": "media",      "trabajo": "uber_auto",   "ingreso": 8_000,  "cooldown": 8*60},
    "logan":        {"nombre": "Renault Logan",     "precio": 820_000,   "clase": "media",      "trabajo": "uber_auto",   "ingreso": 8_200,  "cooldown": 8*60},
    "p308":         {"nombre": "Peugeot 308",       "precio": 900_000,   "clase": "media",      "trabajo": "corporativo", "ingreso": 10_000, "cooldown": 6*60},
    "focus":        {"nombre": "Ford Focus",        "precio": 950_000,   "clase": "media",      "trabajo": "corporativo", "ingreso": 10_500, "cooldown": 6*60},
    "cruze":        {"nombre": "Chevrolet Cruze",   "precio": 1_000_000, "clase": "media",      "trabajo": "corporativo", "ingreso": 11_000, "cooldown": 6*60},
    "corolla":      {"nombre": "Toyota Corolla",    "precio": 1_200_000, "clase": "media",      "trabajo": "corporativo", "ingreso": 13_000, "cooldown": 6*60},
    "civic":        {"nombre": "Honda Civic",       "precio": 1_300_000, "clase": "media",      "trabajo": "corporativo", "ingreso": 14_000, "cooldown": 6*60},
    "vento":        {"nombre": "VW Vento",          "precio": 1_400_000, "clase": "media",      "trabajo": "corporativo", "ingreso": 15_000, "cooldown": 6*60},
    # CLASE MEDIA ALTA (pickups = flete)
    "sw4":          {"nombre": "Toyota SW4",        "precio": 3_000_000, "clase": "media_alta", "trabajo": "flete",       "ingreso": 22_000, "cooldown": 5*60},
    "amarok":       {"nombre": "VW Amarok",         "precio": 3_500_000, "clase": "media_alta", "trabajo": "flete",       "ingreso": 25_000, "cooldown": 5*60},
    "ranger":       {"nombre": "Ford Ranger",       "precio": 3_800_000, "clase": "media_alta", "trabajo": "flete",       "ingreso": 27_000, "cooldown": 5*60},
    "frontier":     {"nombre": "Nissan Frontier",   "precio": 4_000_000, "clase": "media_alta", "trabajo": "flete",       "ingreso": 28_000, "cooldown": 5*60},
    "s10":          {"nombre": "Chevrolet S10",     "precio": 4_200_000, "clase": "media_alta", "trabajo": "flete",       "ingreso": 29_000, "cooldown": 5*60},
    "compass":      {"nombre": "Jeep Compass",      "precio": 4_500_000, "clase": "media_alta", "trabajo": "corporativo", "ingreso": 30_000, "cooldown": 5*60},
    "hilux":        {"nombre": "Toyota Hilux",      "precio": 5_000_000, "clase": "media_alta", "trabajo": "flete",       "ingreso": 35_000, "cooldown": 5*60},
    "audia3":       {"nombre": "Audi A3",           "precio": 5_500_000, "clase": "media_alta", "trabajo": "corporativo", "ingreso": 38_000, "cooldown": 5*60},
    "bmw1":         {"nombre": "BMW Serie 1",       "precio": 6_000_000, "clase": "media_alta", "trabajo": "corporativo", "ingreso": 40_000, "cooldown": 5*60},
    "mercA":        {"nombre": "Mercedes Clase A",  "precio": 6_500_000, "clase": "media_alta", "trabajo": "corporativo", "ingreso": 42_000, "cooldown": 5*60},
    # CLASE ALTA
    "audia4":       {"nombre": "Audi A4",           "precio": 9_000_000,  "clase": "alta",    "trabajo": "vip",         "ingreso": 55_000, "cooldown": 4*60},
    "bmw3":         {"nombre": "BMW Serie 3",       "precio": 10_000_000, "clase": "alta",    "trabajo": "vip",         "ingreso": 60_000, "cooldown": 4*60},
    "mercC":        {"nombre": "Mercedes Clase C",  "precio": 11_000_000, "clase": "alta",    "trabajo": "vip",         "ingreso": 65_000, "cooldown": 4*60},
    "bmw5":         {"nombre": "BMW Serie 5",       "precio": 14_000_000, "clase": "alta",    "trabajo": "vip",         "ingreso": 75_000, "cooldown": 4*60},
    "audia6":       {"nombre": "Audi A6",           "precio": 15_000_000, "clase": "alta",    "trabajo": "vip",         "ingreso": 80_000, "cooldown": 4*60},
    "mercE":        {"nombre": "Mercedes Clase E",  "precio": 18_000_000, "clase": "alta",    "trabajo": "vip",         "ingreso": 90_000, "cooldown": 4*60},
    "bmwx5":        {"nombre": "BMW X5",            "precio": 22_000_000, "clase": "alta",    "trabajo": "vip",         "ingreso": 100_000,"cooldown": 4*60},
    "audiq7":       {"nombre": "Audi Q7",           "precio": 25_000_000, "clase": "alta",    "trabajo": "vip",         "ingreso": 110_000,"cooldown": 4*60},
    "mercGLE":      {"nombre": "Mercedes GLE",      "precio": 28_000_000, "clase": "alta",    "trabajo": "vip",         "ingreso": 120_000,"cooldown": 4*60},
    "cayenne":      {"nombre": "Porsche Cayenne",   "precio": 35_000_000, "clase": "alta",    "trabajo": "vip",         "ingreso": 140_000,"cooldown": 4*60},
    # CLASE PREMIUM
    "bmwm3":        {"nombre": "BMW M3",            "precio": 50_000_000,  "clase": "premium", "trabajo": "evento_vip",  "ingreso": 180_000,"cooldown": 3*60},
    "audirs5":      {"nombre": "Audi RS5",          "precio": 55_000_000,  "clase": "premium", "trabajo": "evento_vip",  "ingreso": 190_000,"cooldown": 3*60},
    "amgc63":       {"nombre": "Mercedes AMG C63",  "precio": 60_000_000,  "clase": "premium", "trabajo": "evento_vip",  "ingreso": 200_000,"cooldown": 3*60},
    "911":          {"nombre": "Porsche 911 Carrera","precio": 80_000_000, "clase": "premium", "trabajo": "evento_vip",  "ingreso": 250_000,"cooldown": 3*60},
    "gtr":          {"nombre": "Nissan GT-R",       "precio": 90_000_000,  "clase": "premium", "trabajo": "evento_vip",  "ingreso": 270_000,"cooldown": 3*60},
    "corvette":     {"nombre": "Chevrolet Corvette","precio": 100_000_000, "clase": "premium", "trabajo": "evento_vip",  "ingreso": 290_000,"cooldown": 3*60},
    "bmwm5":        {"nombre": "BMW M5",            "precio": 110_000_000, "clase": "premium", "trabajo": "evento_vip",  "ingreso": 310_000,"cooldown": 3*60},
    "audir8":       {"nombre": "Audi R8",           "precio": 150_000_000, "clase": "premium", "trabajo": "evento_vip",  "ingreso": 380_000,"cooldown": 3*60},
    "huracan":      {"nombre": "Lamborghini Huracán","precio": 250_000_000,"clase": "premium", "trabajo": "evento_vip",  "ingreso": 600_000,"cooldown": 3*60},
    "ferrari488":   {"nombre": "Ferrari 488 GTB",  "precio": 300_000_000, "clase": "premium", "trabajo": "evento_vip",  "ingreso": 700_000,"cooldown": 3*60},
    # CLASE ÉLITE
    "ghost":        {"nombre": "Rolls-Royce Ghost",      "precio": 500_000_000, "clase": "elite", "trabajo": "elite_vip", "ingreso": 1_200_000,"cooldown": 2*60},
    "bentley":      {"nombre": "Bentley Continental GT", "precio": 450_000_000, "clase": "elite", "trabajo": "elite_vip", "ingreso": 1_100_000,"cooldown": 2*60},
    "aventador":    {"nombre": "Lamborghini Aventador",  "precio": 600_000_000, "clase": "elite", "trabajo": "elite_vip", "ingreso": 1_400_000,"cooldown": 2*60},
    "sf90":         {"nombre": "Ferrari SF90",           "precio": 700_000_000, "clase": "elite", "trabajo": "elite_vip", "ingreso": 1_600_000,"cooldown": 2*60},
    "mclaren":      {"nombre": "McLaren 720S",           "precio": 550_000_000, "clase": "elite", "trabajo": "elite_vip", "ingreso": 1_300_000,"cooldown": 2*60},
    "g63":          {"nombre": "Mercedes G63 AMG",       "precio": 400_000_000, "clase": "elite", "trabajo": "elite_vip", "ingreso": 950_000, "cooldown": 2*60},
    "purosangue":   {"nombre": "Ferrari Purosangue",     "precio": 750_000_000, "clase": "elite", "trabajo": "elite_vip", "ingreso": 1_700_000,"cooldown": 2*60},
    "cullinan":     {"nombre": "Rolls-Royce Cullinan",   "precio": 800_000_000, "clase": "elite", "trabajo": "elite_vip", "ingreso": 1_800_000,"cooldown": 2*60},
    "bugatti":      {"nombre": "Bugatti Chiron",         "precio": 1_500_000_000,"clase": "elite","trabajo": "elite_vip", "ingreso": 3_000_000,"cooldown": 2*60},
    "raptor":       {"nombre": "Ford F-150 Raptor",      "precio": 200_000_000, "clase": "elite", "trabajo": "elite_vip", "ingreso": 500_000, "cooldown": 2*60},
}

TRABAJOS_AUTO = {
    "delivery":    "📦 Delivery",
    "cadeteria":   "🛵 Cadetería",
    "remis":       "🚖 Remis",
    "uber_auto":   "🚗 Uber",
    "corporativo": "💼 Corporativo",
    "flete":       "🚛 Flete",
    "vip":         "⭐ Traslado VIP",
    "evento_vip":  "🎪 Evento exclusivo",
    "elite_vip":   "👑 Servicio élite",
}

def auto_get(user_id: int) -> dict:
    e = eco_get(user_id)
    if "autos" not in e:
        e["autos"] = []
    if "ultimo_auto" not in e:
        e["ultimo_auto"] = {}
    return e

# ── Propiedades ──

PROPIEDADES = {
    "conventillo":   {"nombre": "Conventillo en Lugano",   "precio": 650_000,   "alquiler": 15_000,  "cooldown": 60*60},
    "depto_moron":   {"nombre": "Depto en Morón",          "precio": 1_200_000, "alquiler": 28_000,  "cooldown": 60*60},
    "casa_lomas":    {"nombre": "Casa en Lomas",           "precio": 2_500_000, "alquiler": 55_000,  "cooldown": 60*60},
    "local_centro":  {"nombre": "Local comercial Centro",  "precio": 5_000_000, "alquiler": 120_000, "cooldown": 60*60},
    "edificio_palermo":{"nombre": "Edificio en Palermo",  "precio": 15_000_000,"alquiler": 380_000, "cooldown": 60*60},
    "torre_puerto":  {"nombre": "Torre en Puerto Madero",  "precio": 50_000_000,"alquiler": 1_400_000,"cooldown": 60*60},
}

# ── Aviones (por capacidad/MTOW real aproximado) ──

AVIONES = {
    "pa11":        {"nombre": "Piper PA-11",          "precio": 3_000_000,  "pax": 1,  "carga_kg": 100,  "ingreso_vuelo": 25_000,  "cooldown": 60*60},
    "pa38":        {"nombre": "Piper PA-38 Tomahawk", "precio": 5_000_000,  "pax": 1,  "carga_kg": 120,  "ingreso_vuelo": 30_000,  "cooldown": 60*60},
    "pa28":        {"nombre": "Piper PA-28",          "precio": 8_000_000,  "pax": 3,  "carga_kg": 200,  "ingreso_vuelo": 45_000,  "cooldown": 60*60},
    "cessna150":   {"nombre": "Cessna 150",           "precio": 6_000_000,  "pax": 1,  "carga_kg": 150,  "ingreso_vuelo": 35_000,  "cooldown": 60*60},
    "cessna172":   {"nombre": "Cessna 172",           "precio": 10_000_000, "pax": 3,  "carga_kg": 250,  "ingreso_vuelo": 55_000,  "cooldown": 60*60},
    "cessna182":   {"nombre": "Cessna 182",           "precio": 14_000_000, "pax": 3,  "carga_kg": 350,  "ingreso_vuelo": 70_000,  "cooldown": 60*60},
    "grandcaravan":{"nombre": "Cessna Grand Caravan", "precio": 35_000_000, "pax": 13, "carga_kg": 1_200,"ingreso_vuelo": 180_000, "cooldown": 2*60*60},
    "b200":        {"nombre": "Beechcraft King Air 200","precio": 80_000_000,"pax": 8, "carga_kg": 1_500,"ingreso_vuelo": 380_000, "cooldown": 2*60*60},
    "b350":        {"nombre": "Beechcraft King Air 350","precio": 120_000_000,"pax":11,"carga_kg": 2_000,"ingreso_vuelo": 550_000, "cooldown": 2*60*60},
    "b58":         {"nombre": "Beechcraft Baron 58",  "precio": 25_000_000, "pax": 5,  "carga_kg": 600,  "ingreso_vuelo": 130_000, "cooldown": 2*60*60},
}


# ─────────────────────────────────────────
#  COMANDOS DE ECONOMÍA
# ─────────────────────────────────────────

@bot.tree.command(name="balance", description="Muestra tu balance actual",
                  guild=discord.Object(id=GUILD_ID))
async def slash_balance(interaction: discord.Interaction):
    e = eco_get(interaction.user.id)
    e["nombre"] = interaction.user.display_name
    trabajo_actual = TODOS_TRABAJOS.get(e["trabajo"], {}).get("nombre", "Desempleado") if e["trabajo"] else "Desempleado"
    embed = discord.Embed(title=f"💰 Billetera de {interaction.user.display_name}",
                          color=discord.Color.green())
    embed.add_field(name="Balance", value=f"**{formato_plata(e['balance'])}**", inline=False)
    embed.add_field(name="Trabajo", value=trabajo_actual, inline=True)
    embed.add_field(name="Propiedades", value=str(len(e["propiedades"])), inline=True)
    embed.add_field(name="Aviones", value=str(len(e["vehiculos"])), inline=True)
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    guardar_data()
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="trabajos", description="Ver todos los trabajos disponibles",
                  guild=discord.Object(id=GUILD_ID))
async def slash_trabajos(interaction: discord.Interaction):
    embed = discord.Embed(title="💼 Trabajos disponibles", color=discord.Color.blue())
    
    bajos = chr(10).join(f"`{k}` — {v['nombre']} | {formato_plata(v['pago'])} cada {v['cooldown']//60}min" + (" 🎁" if v['propina'] else "") for k, v in TRABAJOS_BAJOS.items())
    medios_lines = []
    for k, v in TRABAJOS_MEDIOS.items():
        req = f" | 🔒 {formato_plata(v['requisito'])}" if v.get("requisito") else ""
        medios_lines.append(f"`{k}` — {v['nombre']} | {formato_plata(v['pago'])} cada {v['cooldown']//60}min{req}")
    medios = chr(10).join(medios_lines)
    altos_lines = []
    for k, v in TRABAJOS_ALTOS.items():
        if "pago_variable" in v:
            pago_txt = f"{formato_plata(v['pago_variable'][0])}-{formato_plata(v['pago_variable'][1])}"
        else:
            pago_txt = formato_plata(v['pago'])
        req = f" | 🔒 {formato_plata(v['requisito'])}" if v.get("requisito") else ""
        altos_lines.append(f"`{k}` — {v['nombre']} | {pago_txt} cada {v['cooldown']//3600}h{req}")
    altos = chr(10).join(altos_lines)

    embed.add_field(name="🔵 Trabajos bajos (🎁 = propina posible)", value=bajos, inline=False)
    embed.add_field(name="🟡 Trabajos medios (🔒 = balance mínimo)", value=medios, inline=False)
    embed.add_field(name="🔴 Trabajos altos (🔒 = balance mínimo)", value=altos, inline=False)
    embed.set_footer(text="Usá /emplearme <trabajo> para conseguir trabajo")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="emplearme", description="Conseguí un trabajo",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(trabajo="Nombre del trabajo (ej: albanil, piloto, banquero)")
async def slash_emplearme(interaction: discord.Interaction, trabajo: str):
    trabajo = trabajo.lower().strip()
    if trabajo not in TODOS_TRABAJOS:
        await interaction.response.send_message(
            f"❌ Trabajo no encontrado. Usá `/trabajos` para ver la lista.", ephemeral=True)
        return
    e = eco_get(interaction.user.id)
    e["nombre"] = interaction.user.display_name
    info = TODOS_TRABAJOS[trabajo]

    # Verificar requisito de balance
    requisito = info.get("requisito", 0)
    if requisito and e["balance"] < requisito:
        await interaction.response.send_message(
            f"❌ Necesitás **{formato_plata(requisito)}** para ser {info['nombre']}. "
            f"Tenés {formato_plata(e['balance'])}.", ephemeral=True)
        return

    e["trabajo"] = trabajo
    e["ultimo_trabajo"] = 0
    guardar_data()
    await interaction.response.send_message(
        f"✅ Ahora trabajás de **{info['nombre']}**. Usá `/trabajar` para cobrar.")

@bot.tree.command(name="trabajar", description="Trabajá y cobrá tu sueldo",
                  guild=discord.Object(id=GUILD_ID))
async def slash_trabajar(interaction: discord.Interaction):
    e = eco_get(interaction.user.id)
    e["nombre"] = interaction.user.display_name

    if not e["trabajo"]:
        await interaction.response.send_message(
            "❌ No tenés trabajo. Usá `/emplearme` para conseguir uno.", ephemeral=True)
        return

    info = TODOS_TRABAJOS[e["trabajo"]]
    ahora = time.time()
    tiempo_restante = (e["ultimo_trabajo"] + info["cooldown"]) - ahora

    if tiempo_restante > 0:
        mins = int(tiempo_restante // 60)
        segs = int(tiempo_restante % 60)
        await interaction.response.send_message(
            f"⏳ Todavía no podés trabajar. Faltan **{mins}m {segs}s**.", ephemeral=True)
        return

    # Calcular pago y descripción
    situacion_desc = None
    if e["trabajo"] in SITUACIONES_TRABAJO:
        sit = random.choice(SITUACIONES_TRABAJO[e["trabajo"]])
        situacion_desc = sit["descripcion"]
        pago = sit["pago"]
    elif "pago_variable" in info:
        pago = random.randint(info["pago_variable"][0], info["pago_variable"][1])
    else:
        pago = info["pago"]

    propina = 0
    propina_msg = ""
    if info["propina"] and random.random() < 0.3:
        propina = int(pago * random.uniform(0.2, 0.8))
        propina_msg = f" + **{formato_plata(propina)} de propina** 🎁"

    total = pago + propina
    e["balance"] += total
    e["ultimo_trabajo"] = ahora
    guardar_data()

    desc = situacion_desc if situacion_desc else f"Ejecutaste *{info['accion']}*"
    embed = discord.Embed(
        title=f"💼 {info['nombre']}",
        description=f"{desc}{propina_msg}",
        color=discord.Color.green(),
    )
    embed.add_field(name="Cobrado", value=formato_plata(total), inline=True)
    embed.add_field(name="Balance", value=formato_plata(e["balance"]), inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="daily", description="Recompensa diaria gratuita",
                  guild=discord.Object(id=GUILD_ID))
async def slash_daily(interaction: discord.Interaction):
    e = eco_get(interaction.user.id)
    ahora = time.time()
    if ahora - e["daily_ultimo"] < 24*60*60:
        resta = (e["daily_ultimo"] + 24*60*60) - ahora
        horas = int(resta // 3600)
        mins = int((resta % 3600) // 60)
        await interaction.response.send_message(
            f"⏳ Ya reclamaste el daily. Volvé en **{horas}h {mins}m**.", ephemeral=True)
        return
    monto = random.randint(5_000, 15_000)
    e["balance"] += monto
    e["daily_ultimo"] = ahora
    guardar_data()
    await interaction.response.send_message(
        f"🎁 Reclamaste tu daily: **{formato_plata(monto)}**. Balance: {formato_plata(e['balance'])}")

@bot.tree.command(name="pagar", description="Transferí plata a otro usuario",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(usuario="A quién transferir", monto="Cuánto transferir")
async def slash_pagar(interaction: discord.Interaction, usuario: discord.Member, monto: int):
    if monto <= 0:
        await interaction.response.send_message("❌ El monto tiene que ser positivo.", ephemeral=True); return
    e_from = eco_get(interaction.user.id)
    if e_from["balance"] < monto:
        await interaction.response.send_message(
            f"❌ No tenés suficiente. Tenés {formato_plata(e_from['balance'])}.", ephemeral=True); return
    e_to = eco_get(usuario.id)
    e_from["balance"] -= monto
    e_to["balance"] += monto
    e_to["nombre"] = usuario.display_name
    guardar_data()
    await interaction.response.send_message(
        f"💸 Le transferiste **{formato_plata(monto)}** a {usuario.mention}.")

@bot.tree.command(name="robar", description="Intentá robarle plata a alguien (riesgoso)",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(usuario="A quién robarle")
async def slash_robar(interaction: discord.Interaction, usuario: discord.Member):
    if usuario.id == interaction.user.id:
        await interaction.response.send_message("No te podés robar a vos mismo 🤦", ephemeral=True); return
    e_from = eco_get(interaction.user.id)
    e_to = eco_get(usuario.id)
    if e_to["balance"] <= 0:
        await interaction.response.send_message(f"{usuario.display_name} está en banca rota, no tiene nada.", ephemeral=True); return
    
    exito = random.random() < 0.4  # 40% de éxito
    if exito:
        robado = int(e_to["balance"] * random.uniform(0.05, 0.2))
        robado = max(100, min(robado, 200_000))
        e_to["balance"] -= robado
        e_from["balance"] += robado
        guardar_data()
        embed = discord.Embed(title="🦹 ¡Robo exitoso!",
                              description=f"Le robaste **{formato_plata(robado)}** a {usuario.mention} 😈",
                              color=discord.Color.dark_green())
    else:
        multa = int(e_from["balance"] * random.uniform(0.05, 0.15))
        multa = max(100, multa)
        e_from["balance"] = max(0, e_from["balance"] - multa)
        guardar_data()
        embed = discord.Embed(title="🚔 ¡Te atraparon!",
                              description=f"Intentaste robarle a {usuario.mention} y te salió mal. Pagaste **{formato_plata(multa)}** de multa.",
                              color=discord.Color.red())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="tienda", description="Ver propiedades y aviones disponibles para comprar",
                  guild=discord.Object(id=GUILD_ID))
async def slash_tienda(interaction: discord.Interaction):
    embed = discord.Embed(title="🏪 Tienda", color=discord.Color.gold())
    props = chr(10).join(f"`{k}` — {v['nombre']} | Precio: {formato_plata(v['precio'])} | Alquiler: {formato_plata(v['alquiler'])}/h" for k, v in PROPIEDADES.items())
    avion_lines = chr(10).join(f"`{k}` — {v['nombre']} | {formato_plata(v['precio'])} | {v['pax']} pax | {v['carga_kg']}kg | {formato_plata(v['ingreso_vuelo'])}/vuelo" for k, v in AVIONES.items())
    embed.add_field(name="🏠 Propiedades", value=props, inline=False)
    embed.add_field(name="✈️ Aviones", value=avion_lines, inline=False)
    embed.set_footer(text="Usá /comprar <item> para comprar")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="comprar", description="Comprá una propiedad o avión",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(item="ID del item a comprar (ej: conventillo, cessna172)")
async def slash_comprar(interaction: discord.Interaction, item: str):
    item = item.lower().strip()
    e = eco_get(interaction.user.id)
    e["nombre"] = interaction.user.display_name

    if item in PROPIEDADES:
        info = PROPIEDADES[item]
        if item in e["propiedades"]:
            await interaction.response.send_message("Ya tenés esa propiedad.", ephemeral=True); return
        if e["balance"] < info["precio"]:
            await interaction.response.send_message(
                f"❌ No tenés suficiente. Necesitás {formato_plata(info['precio'])} y tenés {formato_plata(e['balance'])}.", ephemeral=True); return
        e["balance"] -= info["precio"]
        e["propiedades"].append(item)
        guardar_data()
        await interaction.response.send_message(
            f"🏠 Compraste **{info['nombre']}** por {formato_plata(info['precio'])}. Ya podés cobrar alquiler con `/alquiler`.")

    elif item in AVIONES:
        info = AVIONES[item]
        if item in e["vehiculos"]:
            await interaction.response.send_message("Ya tenés ese avión.", ephemeral=True); return
        if e["balance"] < info["precio"]:
            await interaction.response.send_message(
                f"❌ No tenés suficiente. Necesitás {formato_plata(info['precio'])} y tenés {formato_plata(e['balance'])}.", ephemeral=True); return
        e["balance"] -= info["precio"]
        e["vehiculos"].append(item)
        guardar_data()
        await interaction.response.send_message(
            f"✈️ Compraste el **{info['nombre']}** por {formato_plata(info['precio'])}. Usá `/volar <id_avion>` para operar.")
    else:
        await interaction.response.send_message("❌ Item no encontrado. Usá `/tienda` para ver opciones.", ephemeral=True)

@bot.tree.command(name="alquiler", description="Cobrá el alquiler de tus propiedades",
                  guild=discord.Object(id=GUILD_ID))
async def slash_alquiler(interaction: discord.Interaction):
    e = eco_get(interaction.user.id)
    if not e["propiedades"]:
        await interaction.response.send_message("No tenés propiedades. Comprá en `/tienda`.", ephemeral=True); return
    
    ahora = time.time()
    total_cobrado = 0
    lineas = []
    for prop_id in e["propiedades"]:
        info = PROPIEDADES[prop_id]
        ultimo = e["ultimo_alquiler"].get(prop_id, 0)
        restante = (ultimo + info["cooldown"]) - ahora
        if restante > 0:
            mins = int(restante // 60)
            lineas.append(f"❌ {info['nombre']} — disponible en {mins}min")
        else:
            e["ultimo_alquiler"][prop_id] = ahora
            total_cobrado += info["alquiler"]
            lineas.append(f"✅ {info['nombre']} — +{formato_plata(info['alquiler'])}")

    e["balance"] += total_cobrado
    guardar_data()
    desc = chr(10).join(lineas)
    embed = discord.Embed(title="🏠 Cobro de alquileres",
                          description=desc, color=discord.Color.green())
    if total_cobrado > 0:
        embed.add_field(name="Total cobrado", value=formato_plata(total_cobrado), inline=True)
        embed.add_field(name="Balance", value=formato_plata(e["balance"]), inline=True)
    await interaction.response.send_message(embed=embed)



# ─────────────────────────────────────────
#  SISTEMA DE VUELO REALISTA / ARCADE
# ─────────────────────────────────────────

import math as _math

# Aeropuertos argentinos reales con coordenadas y declinación magnética
AEROPUERTOS = {
    "SAEZ": {"nombre": "Ezeiza",           "lat": -34.822, "lon": -58.535, "decl": -7},
    "SABE": {"nombre": "Aeroparque",       "lat": -34.559, "lon": -58.416, "decl": -7},
    "SAME": {"nombre": "Mendoza",          "lat": -32.832, "lon": -68.793, "decl": -5},
    "SACO": {"nombre": "Córdoba",          "lat": -31.323, "lon": -64.208, "decl": -6},
    "SAZM": {"nombre": "Mar del Plata",    "lat": -37.934, "lon": -57.573, "decl": -8},
    "SAZB": {"nombre": "Bahía Blanca",     "lat": -38.725, "lon": -62.169, "decl": -8},
    "SASJ": {"nombre": "San Juan",         "lat": -31.571, "lon": -68.418, "decl": -5},
    "SANU": {"nombre": "San Luis",         "lat": -33.274, "lon": -66.357, "decl": -6},
    "SASA": {"nombre": "Salta",            "lat": -24.856, "lon": -65.486, "decl": -4},
    "SASJ": {"nombre": "Jujuy",            "lat": -24.393, "lon": -65.098, "decl": -4},
    "SANT": {"nombre": "Tucumán",          "lat": -26.841, "lon": -65.105, "decl": -5},
    "SARC": {"nombre": "Corrientes",       "lat": -27.445, "lon": -58.762, "decl": -6},
    "SARL": {"nombre": "Posadas",          "lat": -27.386, "lon": -55.971, "decl": -6},
    "SARS": {"nombre": "Resistencia",      "lat": -27.450, "lon": -59.056, "decl": -6},
    "SANE": {"nombre": "Paraná",           "lat": -31.795, "lon": -60.480, "decl": -7},
    "SAAP": {"nombre": "Rosario",          "lat": -32.903, "lon": -60.785, "decl": -7},
    "SAWH": {"nombre": "Ushuaia",          "lat": -54.843, "lon": -68.296, "decl": -14},
    "SAWE": {"nombre": "Río Grande",       "lat": -53.778, "lon": -67.750, "decl": -13},
    "SAVC": {"nombre": "Comodoro Riv.",    "lat": -45.785, "lon": -67.500, "decl": -11},
    "SAVV": {"nombre": "Viedma",           "lat": -40.869, "lon": -63.000, "decl": -9},
    "SAZN": {"nombre": "Neuquén",          "lat": -38.949, "lon": -68.156, "decl": -8},
    "SAWC": {"nombre": "Santa Rosa",       "lat": -36.588, "lon": -64.270, "decl": -8},
    "SAZL": {"nombre": "Santa Teresita",   "lat": -36.543, "lon": -56.720, "decl": -8},
    "SAZR": {"nombre": "Santa Rosa Apto.", "lat": -36.620, "lon": -64.257, "decl": -8},
    "SAZO": {"nombre": "Necochea",         "lat": -38.484, "lon": -58.817, "decl": -8},
}

def _distancia_nm(lat1, lon1, lat2, lon2):
    """Distancia en NM entre dos puntos (fórmula haversine)."""
    R = 3440.065  # Radio Tierra en NM
    dlat = _math.radians(lat2 - lat1)
    dlon = _math.radians(lon2 - lon1)
    a = _math.sin(dlat/2)**2 + _math.cos(_math.radians(lat1)) * _math.cos(_math.radians(lat2)) * _math.sin(dlon/2)**2
    return R * 2 * _math.asin(_math.sqrt(a))

def _curso_verdadero(lat1, lon1, lat2, lon2):
    """Curso verdadero en grados (0-360)."""
    dlon = _math.radians(lon2 - lon1)
    lat1r, lat2r = _math.radians(lat1), _math.radians(lat2)
    x = _math.sin(dlon) * _math.cos(lat2r)
    y = _math.cos(lat1r) * _math.sin(lat2r) - _math.sin(lat1r) * _math.cos(lat2r) * _math.cos(dlon)
    bearing = _math.degrees(_math.atan2(x, y))
    return (bearing + 360) % 360

def _orientacion_cardinal(grados):
    dirs = ["Norte", "Noreste", "Este", "Sureste", "Sur", "Suroeste", "Oeste", "Noroeste"]
    idx = int((grados + 22.5) / 45) % 8
    return dirs[idx]

def _generar_metar(icao, decl):
    """Genera un METAR ficticio pero realista."""
    import random as _r
    dia = _r.randint(1, 28)
    hora = _r.choice([6, 9, 12, 15, 18, 21])
    viento_dir = _r.randint(0, 35) * 10
    viento_kt  = _r.randint(3, 25)
    raf        = viento_kt + _r.randint(5, 15) if viento_kt > 12 else 0
    vis        = _r.choice([9999, 9999, 9999, 5000, 3000, 1500])
    nubes1_tipo = _r.choice(["FEW", "SCT", "BKN", "OVC"])
    nubes1_base = _r.randint(10, 80) * 100
    temp       = _r.randint(5, 30)
    punto_rocio = temp - _r.randint(2, 15)
    qnh        = _r.randint(1008, 1025)
    raf_str    = f"G{raf:02d}KT" if raf else ""
    nubes_str  = f"{nubes1_tipo}{nubes1_base//100:03d}"
    metar = f"{icao} {dia:02d}{hora:02d}00Z {viento_dir:03d}{viento_kt:02d}{raf_str}KT {vis} {nubes_str} {temp:02d}/{punto_rocio:02d} Q{qnh}"
    return metar, viento_dir, viento_kt

def _calcular_solucion(tc, tas, viento_dir, viento_kt, dist_nm, decl):
    """Calcula la solución correcta de navegación."""
    # Ángulo de viento relativo al curso
    angulo_viento = _math.radians((viento_dir - tc + 180) % 360)
    # Componente de viento cruzado
    xwind = viento_kt * _math.sin(angulo_viento)
    # Componente de viento en cola/cabeza
    hwind = viento_kt * _math.cos(angulo_viento)
    # Ángulo de deriva (WCA)
    if tas > 0:
        wca = _math.degrees(_math.asin(max(-1, min(1, xwind / tas))))
    else:
        wca = 0
    # True Heading
    th = (tc + wca + 360) % 360
    # Magnetic Heading
    mh = (th + decl + 360) % 360
    # Ground Speed
    gs = max(10, int(tas + hwind))
    # Tiempo en minutos
    tiempo_min = int((dist_nm / gs) * 60) if gs > 0 else 0
    return round(wca, 1), round(mh, 0), gs, tiempo_min

# Guarda los vuelos realistas pendientes de respuesta
vuelos_pendientes: dict[int, dict] = {}

class ModalRespuestaVuelo(discord.ui.Modal, title="✈️ Respuesta de Navegación"):
    deriva     = discord.ui.TextInput(label="Ángulo de deriva (WCA) en grados", placeholder="Ej: -3.5 (negativo = izquierda)", max_length=8)
    mh_input   = discord.ui.TextInput(label="Magnetic Heading (MH) en grados", placeholder="Ej: 285", max_length=5)
    gs_input   = discord.ui.TextInput(label="Ground Speed (GS) en nudos", placeholder="Ej: 98", max_length=5)
    tiempo     = discord.ui.TextInput(label="Tiempo estimado en minutos", placeholder="Ej: 45", max_length=5)
    combustible = discord.ui.TextInput(label="Combustible consumido en litros", placeholder="Ej: 16", max_length=6)

    def __init__(self, user_id: int):
        super().__init__()
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction):
        vp = vuelos_pendientes.get(self.user_id)
        if not vp:
            await interaction.response.send_message("❌ No hay vuelo pendiente.", ephemeral=True); return

        try:
            r_deriva    = float(self.deriva.value.replace(",", "."))
            r_mh        = float(self.mh_input.value)
            r_gs        = float(self.gs_input.value)
            r_tiempo    = float(self.tiempo.value)
            r_combustible = float(self.combustible.value.replace(",", "."))
        except ValueError:
            await interaction.response.send_message("❌ Valores inválidos. Ingresá solo números.", ephemeral=True); return

        sol = vp["solucion"]
        errores = []
        if abs(r_deriva - sol["deriva"]) > 3:
            errores.append(f"❌ Deriva: tu respuesta {r_deriva}° (tolerancia ±3°)")
        if abs(r_mh - sol["mh"]) > 5:
            errores.append(f"❌ MH: tu respuesta {r_mh}° (tolerancia ±5°)")
        if abs(r_gs - sol["gs"]) > 10:
            errores.append(f"❌ GS: tu respuesta {r_gs}kt (tolerancia ±10kt)")
        if abs(r_tiempo - sol["tiempo"]) > 5:
            errores.append(f"❌ Tiempo: tu respuesta {r_tiempo}min (tolerancia ±5min)")
        combustible_correcto = sol["consumo"]
        if abs(r_combustible - combustible_correcto) > combustible_correcto * 0.10:
            errores.append(f"❌ Combustible: tu respuesta {r_combustible}L (tolerancia ±10%)")

        del vuelos_pendientes[self.user_id]
        avion_id = vp["avion"]
        e = eco_get(self.user_id)
        specs = AVION_SPECS.get(avion_id, {})
        est   = avion_estado_get(self.user_id, avion_id)

        if errores:
            # Falló — sin pago, cooldown de 5 minutos
            e["ultimo_vehiculo"][avion_id] = time.time() - (AVIONES[avion_id]["cooldown"] - 5*60)
            guardar_data()
            embed = discord.Embed(
                title="❌ Vuelo denegado — parámetros incorrectos",
                color=discord.Color.red()
            )
            embed.add_field(name="Errores", value=chr(10).join(errores), inline=False)
            embed.set_footer(text="Podés intentar de nuevo en 5 minutos. No se muestra la solución correcta.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            # Aprobó — +55% de pago, +25% XP
            ingreso_base = AVIONES[avion_id]["ingreso_vuelo"]
            ingreso_real = int(ingreso_base * 1.55)
            e["balance"] += ingreso_real

            # Consumir combustible y sumar horas
            duracion_h = vp["dist_nm"] / max(sol["gs"], 1)
            consumo    = int(specs.get("consumo_lh", 40) * duracion_h)
            est["combustible"] = max(0, est["combustible"] - consumo)
            est["horas_totales"]        = round(est.get("horas_totales", 0) + duracion_h, 2)
            est["horas_desde_mant"]     = round(est.get("horas_desde_mant", 0) + duracion_h, 2)
            est["horas_desde_overhaul"] = round(est.get("horas_desde_overhaul", 0) + duracion_h, 2)

            # Horas piloto
            horas = horas_vuelo_get(self.user_id)
            horas["total"] = round(horas.get("total", 0) + duracion_h, 2)
            horas["por_avion"][avion_id] = round(horas["por_avion"].get(avion_id, 0) + duracion_h, 2)

            # XP bonus +25%
            r = ranking_get(self.user_id)
            r["xp"] = r.get("xp", 0) + int(XP_POR_MENSAJE * 5 * 1.25)

            # Verificar mantenimiento
            horas_mant = specs.get("horas_mant", 500)
            if est["horas_desde_overhaul"] >= horas_mant * 2:
                est["bloqueado"] = f"Overhaul requerido ({int(est['horas_desde_overhaul'])}h)"
            elif est["horas_desde_mant"] >= horas_mant:
                est["bloqueado"] = f"Mantenimiento requerido ({int(est['horas_desde_mant'])}h)"

            e["ultimo_vehiculo"][avion_id] = time.time()
            guardar_data()

            embed = discord.Embed(
                title="✅ Vuelo aprobado — Modo Realista",
                color=discord.Color.from_rgb(0, 200, 100)
            )
            embed.add_field(name="💰 Cobrado (+55%)", value=formato_plata(ingreso_real), inline=True)
            embed.add_field(name="⛽ Combustible restante", value=f"{est['combustible']}L", inline=True)
            embed.add_field(name="🕐 Horas acumuladas", value=f"{est['horas_totales']}h", inline=True)
            embed.add_field(name="Balance", value=formato_plata(e["balance"]), inline=True)
            if est.get("bloqueado"):
                embed.add_field(name="⚠️", value=est["bloqueado"], inline=False)
            await interaction.response.send_message(embed=embed)


class SeleccionModoVuelo(discord.ui.View):
    def __init__(self, avion_id: str, user_id: int):
        super().__init__(timeout=60)
        self.avion_id = avion_id
        self.user_id  = user_id

    @discord.ui.button(label="🕹️ Arcade", style=discord.ButtonStyle.secondary)
    async def arcade(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("No es tu vuelo.", ephemeral=True); return
        await interaction.response.defer()
        await _ejecutar_vuelo_arcade(interaction, self.avion_id)
        self.stop()

    @discord.ui.button(label="🎯 Realista (+55%)", style=discord.ButtonStyle.success)
    async def realista(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("No es tu vuelo.", ephemeral=True); return
        await _iniciar_vuelo_realista(interaction, self.avion_id)
        self.stop()


async def _ejecutar_vuelo_arcade(interaction: discord.Interaction, avion_id: str):
    """Ejecuta el vuelo en modo arcade (igual que antes)."""
    e     = eco_get(interaction.user.id)
    info  = AVIONES[avion_id]
    specs = AVION_SPECS.get(avion_id, {})
    est   = avion_estado_get(interaction.user.id, avion_id)
    horas = horas_vuelo_get(interaction.user.id)

    duracion_h = round(info["cooldown"] / 3600, 2)
    consumo    = int(specs.get("consumo_lh", 40) * duracion_h)

    if est["combustible"] < consumo:
        await interaction.followup.send(
            f"⛽ Sin combustible. Tenés {est['combustible']}L y necesitás {consumo}L. Usá `/cargar_combustible {avion_id}`.",
            ephemeral=True); return

    est["combustible"]          -= consumo
    est["horas_totales"]         = round(est.get("horas_totales", 0) + duracion_h, 2)
    est["horas_desde_mant"]      = round(est.get("horas_desde_mant", 0) + duracion_h, 2)
    est["horas_desde_overhaul"]  = round(est.get("horas_desde_overhaul", 0) + duracion_h, 2)
    horas["total"]               = round(horas.get("total", 0) + duracion_h, 2)
    horas["por_avion"][avion_id] = round(horas["por_avion"].get(avion_id, 0) + duracion_h, 2)

    prob_acc = 0.01 + (0.05 if est["horas_desde_mant"] > specs.get("horas_mant", 500) else 0)
    accidente = None
    if random.random() < prob_acc:
        tipo = random.choices(["leve", "moderado", "grave"], weights=[60, 30, 10])[0]
        costos = {"leve": int(info["precio"] * 0.05), "moderado": int(info["precio"] * 0.15), "grave": int(info["precio"] * 0.35)}
        est["dano"] = tipo
        est["bloqueado"] = f"Daño {tipo} — reparación pendiente"
        accidente = (tipo, costos[tipo])

    ingreso = info["ingreso_vuelo"]
    if not accidente:
        e["balance"] += ingreso

    horas_mant = specs.get("horas_mant", 500)
    if est["horas_desde_overhaul"] >= horas_mant * 2:
        est["bloqueado"] = f"Overhaul requerido"
    elif est["horas_desde_mant"] >= horas_mant:
        est["bloqueado"] = f"Mantenimiento requerido"

    e["ultimo_vehiculo"][avion_id] = time.time()
    guardar_data()

    if accidente:
        embed = discord.Embed(title=f"💥 Accidente {accidente[0]} — {info['nombre']}", color=discord.Color.red())
        embed.add_field(name="Reparación estimada", value=formato_plata(accidente[1]), inline=True)
        embed.add_field(name="Usá", value=f"`/reparar {avion_id}`", inline=True)
    else:
        embed = discord.Embed(title=f"🕹️ Vuelo Arcade — {info['nombre']}", color=discord.Color.blue())
        embed.add_field(name="Cobrado", value=formato_plata(ingreso), inline=True)
        embed.add_field(name="Combustible", value=f"{est['combustible']}L", inline=True)
        embed.add_field(name="Horas", value=f"{est['horas_totales']}h", inline=True)
        embed.add_field(name="Balance", value=formato_plata(e["balance"]), inline=True)
    if est.get("bloqueado") and not accidente:
        embed.add_field(name="⚠️", value=est["bloqueado"], inline=False)
    await interaction.followup.send(embed=embed)


async def _iniciar_vuelo_realista(interaction: discord.Interaction, avion_id: str):
    """Genera la navegación VFR y muestra el briefing."""
    e    = eco_get(interaction.user.id)
    info = AVIONES[avion_id]
    specs = AVION_SPECS.get(avion_id, {})
    est  = avion_estado_get(interaction.user.id, avion_id)

    # Seleccionar aeropuertos random distintos
    codigos = list(AEROPUERTOS.keys())
    origen_icao  = random.choice(codigos)
    destino_icao = random.choice([c for c in codigos if c != origen_icao])
    origen  = AEROPUERTOS[origen_icao]
    destino = AEROPUERTOS[destino_icao]

    dist    = round(_distancia_nm(origen["lat"], origen["lon"], destino["lat"], destino["lon"]), 1)
    tc      = round(_curso_verdadero(origen["lat"], origen["lon"], destino["lat"], destino["lon"]), 0)
    decl    = origen["decl"]

    # TAS del avión (aproximada según consumo)
    tas = {
        "pa11": 87, "pa38": 100, "pa28": 120, "cessna150": 95,
        "cessna172": 122, "cessna182": 140, "grandcaravan": 175,
        "b200": 270, "b350": 290, "b58": 200,
    }.get(avion_id, 120)

    metar, viento_dir, viento_kt = _generar_metar(origen_icao, decl)
    wca, mh, gs, tiempo_min = _calcular_solucion(tc, tas, viento_dir, viento_kt, dist, decl)
    consumo = round(specs.get("consumo_lh", 40) * (tiempo_min / 60), 1)

    # Verificar combustible
    if est["combustible"] < consumo:
        await interaction.response.send_message(
            f"⛽ Sin combustible. Tenés {est['combustible']}L y necesitás ~{consumo}L.", ephemeral=True); return

    # Guardar vuelo pendiente
    vuelos_pendientes[interaction.user.id] = {
        "avion":   avion_id,
        "dist_nm": dist,
        "solucion": {"deriva": wca, "mh": mh, "gs": gs, "tiempo": tiempo_min, "consumo": consumo},
    }

    orientacion = _orientacion_cardinal(tc)
    embed = discord.Embed(
        title=f"🎯 Briefing de Vuelo Realista — {info['nombre']}",
        color=discord.Color.from_rgb(255, 165, 0)
    )
    embed.add_field(
        name="📍 Ruta",
        value=f"**{origen_icao}** ({origen['nombre']}) → **{destino_icao}** ({destino['nombre']})",
        inline=False
    )
    embed.add_field(name="📏 Distancia", value=f"{dist} NM", inline=True)
    embed.add_field(name="🧭 Curso Verdadero (TC)", value=f"{int(tc)}°", inline=True)
    embed.add_field(name="🌍 Orientación", value=orientacion, inline=True)
    embed.add_field(name="✈️ TAS", value=f"{tas} kt", inline=True)
    embed.add_field(name="🧲 Declinación magnética", value=f"{abs(decl)}°{'W' if decl < 0 else 'E'}", inline=True)
    embed.add_field(name="⛽ Combustible disponible", value=f"{est['combustible']}L", inline=True)
    embed.add_field(name="📡 METAR", value=f"`{metar}`", inline=False)
    embed.add_field(
        name="📝 Instrucciones",
        value=(
            "Resolvé con tu CR-3, CRP-5 o E6B y tocá **Ingresar respuesta**. "
            "Tolerancias: Deriva ±3° · MH ±5° · GS ±10kt · Tiempo ±5min · Combustible ±10%. "
            "Si fallás, cooldown de 5 minutos. No se muestra la solución si fallás."
        ),
        inline=False
    )
    embed.set_footer(text=f"Recompensa si aprobás: {formato_plata(int(info['ingreso_vuelo'] * 1.55))} (+55%)")

    class BotonResponder(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=300)

        @discord.ui.button(label="✏️ Ingresar respuesta", style=discord.ButtonStyle.success)
        async def responder(self, inter: discord.Interaction, button: discord.ui.Button):
            if inter.user.id != interaction.user.id:
                await inter.response.send_message("No es tu vuelo.", ephemeral=True); return
            await inter.response.send_modal(ModalRespuestaVuelo(inter.user.id))

        @discord.ui.button(label="❌ Cancelar vuelo", style=discord.ButtonStyle.danger)
        async def cancelar(self, inter: discord.Interaction, button: discord.ui.Button):
            if inter.user.id != interaction.user.id:
                await inter.response.send_message("No es tu vuelo.", ephemeral=True); return
            vuelos_pendientes.pop(inter.user.id, None)
            await inter.response.edit_message(
                embed=discord.Embed(title="Vuelo cancelado.", color=discord.Color.greyple()), view=None)

    await interaction.response.send_message(embed=embed, view=BotonResponder())



@bot.tree.command(name="volar", description="Operá tu avión — elegís modo Arcade o Realista",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(avion="ID del avión (ej: cessna172, b350)")
async def slash_volar(interaction: discord.Interaction, avion: str):
    avion = avion.lower().strip()
    e = eco_get(interaction.user.id)
    if avion not in e["vehiculos"]:
        await interaction.response.send_message(
            f"No tenés ese avión. Tus aviones: {', '.join(e['vehiculos']) or 'ninguno'}.", ephemeral=True); return
    info   = AVIONES[avion]
    specs = AVION_SPECS.get(avion, {})
    est   = avion_estado_get(interaction.user.id, avion)

    # Verificar bloqueo
    if est.get("bloqueado"):
        await interaction.response.send_message(
            f"❌ El {info['nombre']} está bloqueado: **{est['bloqueado']}**\nUsá `/mantenimiento {avion}` o `/overhaul {avion}`.",
            ephemeral=True); return

    # Verificar cooldown
    ahora = time.time()
    ultimo = e["ultimo_vehiculo"].get(avion, 0)
    restante = (ultimo + info["cooldown"]) - ahora
    if restante > 0:
        h = int(restante // 3600); m = int((restante % 3600) // 60)
        await interaction.response.send_message(
            f"⏳ El {info['nombre']} no está disponible. Faltan **{h}h {m}m**.", ephemeral=True); return

    # Mostrar selector de modo
    embed = discord.Embed(
        title=f"✈️ {info['nombre']} — Seleccioná el modo de vuelo",
        color=discord.Color.from_rgb(0, 150, 255)
    )
    embed.add_field(name="🕹️ Arcade", value="Vuelo automático, cobro normal.", inline=True)
    embed.add_field(name="🎯 Realista (+55%)", value="Resolvés la navegación VFR y cobrás 55% más.", inline=True)
    embed.add_field(name="⛽ Combustible", value=f"{est['combustible']}L disponibles", inline=False)
    embed.set_footer(text="Tenés 60 segundos para elegir")
    await interaction.response.send_message(
        embed=embed,
        view=SeleccionModoVuelo(avion, interaction.user.id)
    )


@bot.tree.command(name="cargar_combustible", description="Cargá combustible a tu avión",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(avion="ID del avión", litros="Litros a cargar (0 = llenar completo)")
async def slash_cargar_combustible(interaction: discord.Interaction, avion: str, litros: int = 0):
    avion = avion.lower().strip()
    e = eco_get(interaction.user.id)
    if avion not in e["vehiculos"]:
        await interaction.response.send_message("No tenés ese avión.", ephemeral=True); return
    specs = AVION_SPECS.get(avion, {})
    est   = avion_estado_get(interaction.user.id, avion)
    cap   = specs.get("cap_litros", 100)
    actual = est["combustible"]
    if litros == 0:
        a_cargar = cap - actual
    else:
        a_cargar = min(litros, cap - actual)
    if a_cargar <= 0:
        await interaction.response.send_message("El tanque ya está lleno.", ephemeral=True); return
    costo = a_cargar * PRECIO_LITRO_AVGAS
    if e["balance"] < costo:
        await interaction.response.send_message(
            f"❌ Necesitás {formato_plata(costo)} para cargar {a_cargar}L. Tenés {formato_plata(e['balance'])}.", ephemeral=True); return
    e["balance"] -= costo
    est["combustible"] += a_cargar
    guardar_data()
    embed = discord.Embed(title=f"⛽ Combustible cargado — {AVIONES[avion]['nombre']}",
                          color=discord.Color.green())
    embed.add_field(name="Litros cargados", value=f"{a_cargar}L", inline=True)
    embed.add_field(name="Costo", value=formato_plata(costo), inline=True)
    embed.add_field(name="Tanque", value=f"{est['combustible']}/{cap}L", inline=True)
    embed.add_field(name="Balance", value=formato_plata(e["balance"]), inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="mantenimiento", description="Realizá el mantenimiento de tu avión",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(avion="ID del avión")
async def slash_mantenimiento(interaction: discord.Interaction, avion: str):
    avion = avion.lower().strip()
    e = eco_get(interaction.user.id)
    if avion not in e["vehiculos"]:
        await interaction.response.send_message("No tenés ese avión.", ephemeral=True); return
    specs = AVION_SPECS.get(avion, {})
    est   = avion_estado_get(interaction.user.id, avion)
    costo = specs.get("costo_mant", 1_000_000)
    if e["balance"] < costo:
        await interaction.response.send_message(
            f"❌ El mantenimiento cuesta {formato_plata(costo)} y tenés {formato_plata(e['balance'])}.", ephemeral=True); return
    e["balance"] -= costo
    est["horas_desde_mant"] = 0.0
    if est.get("bloqueado") and "Mantenimiento" in est["bloqueado"]:
        est["bloqueado"] = None
    guardar_data()
    embed = discord.Embed(title=f"🔧 Mantenimiento completado — {AVIONES[avion]['nombre']}",
                          description=f"Pagaste {formato_plata(costo)}. El avión está listo para volar.",
                          color=discord.Color.green())
    embed.add_field(name="Balance", value=formato_plata(e["balance"]), inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="overhaul", description="Realizá el overhaul de tu avión (cada 1000h)",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(avion="ID del avión")
async def slash_overhaul(interaction: discord.Interaction, avion: str):
    avion = avion.lower().strip()
    e = eco_get(interaction.user.id)
    if avion not in e["vehiculos"]:
        await interaction.response.send_message("No tenés ese avión.", ephemeral=True); return
    specs = AVION_SPECS.get(avion, {})
    est   = avion_estado_get(interaction.user.id, avion)
    costo = specs.get("costo_overhaul", 5_000_000)
    if e["balance"] < costo:
        await interaction.response.send_message(
            f"❌ El overhaul cuesta {formato_plata(costo)} y tenés {formato_plata(e['balance'])}.", ephemeral=True); return
    e["balance"] -= costo
    est["horas_desde_mant"] = 0.0
    est["horas_desde_overhaul"] = 0.0
    est["bloqueado"] = None
    guardar_data()
    embed = discord.Embed(title=f"🛠️ Overhaul completado — {AVIONES[avion]['nombre']}",
                          description=f"Pagaste {formato_plata(costo)}. Motor y célula revisados al 100%.",
                          color=discord.Color.green())
    embed.add_field(name="Balance", value=formato_plata(e["balance"]), inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="reparar", description="Repará tu avión dañado",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(avion="ID del avión")
async def slash_reparar(interaction: discord.Interaction, avion: str):
    avion = avion.lower().strip()
    e = eco_get(interaction.user.id)
    if avion not in e["vehiculos"]:
        await interaction.response.send_message("No tenés ese avión.", ephemeral=True); return
    info  = AVIONES[avion]
    est   = avion_estado_get(interaction.user.id, avion)
    if not est.get("dano"):
        await interaction.response.send_message("El avión no tiene daños.", ephemeral=True); return
    costos = {"leve": int(info["precio"] * 0.05), "moderado": int(info["precio"] * 0.15), "grave": int(info["precio"] * 0.35)}
    costo = costos.get(est["dano"], 0)
    if e["balance"] < costo:
        await interaction.response.send_message(
            f"❌ La reparación cuesta {formato_plata(costo)} y tenés {formato_plata(e['balance'])}.", ephemeral=True); return
    e["balance"] -= costo
    est["dano"] = None
    if est.get("bloqueado") and "Daño" in est["bloqueado"]:
        est["bloqueado"] = None
    guardar_data()
    embed = discord.Embed(title=f"✅ Avión reparado — {info['nombre']}",
                          description=f"Pagaste {formato_plata(costo)}. Listo para volar de nuevo.",
                          color=discord.Color.green())
    embed.add_field(name="Balance", value=formato_plata(e["balance"]), inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="estado_avion", description="Ver el estado técnico de tu avión",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(avion="ID del avión")
async def slash_estado_avion(interaction: discord.Interaction, avion: str):
    avion = avion.lower().strip()
    e = eco_get(interaction.user.id)
    if avion not in e["vehiculos"]:
        await interaction.response.send_message("No tenés ese avión.", ephemeral=True); return
    info  = AVIONES[avion]
    specs = AVION_SPECS.get(avion, {})
    est   = avion_estado_get(interaction.user.id, avion)
    horas = horas_vuelo_get(interaction.user.id)
    embed = discord.Embed(title=f"📋 Estado — {info['nombre']}", color=discord.Color.blue())
    embed.add_field(name="⛽ Combustible", value=f"{est['combustible']}/{specs.get('cap_litros','?')}L", inline=True)
    embed.add_field(name="🕐 Horas totales", value=f"{est['horas_totales']}h", inline=True)
    embed.add_field(name="🔧 Desde último mant.", value=f"{est['horas_desde_mant']}h / {specs.get('horas_mant','?')}h", inline=True)
    embed.add_field(name="🛠️ Desde último overhaul", value=f"{est['horas_desde_overhaul']}h / {specs.get('horas_mant',500)*2}h", inline=True)
    embed.add_field(name="💥 Daño", value=est.get("dano") or "Sin daños", inline=True)
    embed.add_field(name="🚫 Bloqueo", value=est.get("bloqueado") or "Ninguno", inline=True)
    embed.add_field(name="✈️ Horas como piloto (total)", value=f"{horas.get('total',0)}h", inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="mis_bienes", description="Muestra tus propiedades y aviones",
                  guild=discord.Object(id=GUILD_ID))
async def slash_mis_bienes(interaction: discord.Interaction):
    e = eco_get(interaction.user.id)
    embed = discord.Embed(title=f"📋 Bienes de {interaction.user.display_name}", color=discord.Color.gold())
    
    if e["propiedades"]:
        props = chr(10).join(f"🏠 {PROPIEDADES[p]['nombre']} — alquiler: {formato_plata(PROPIEDADES[p]['alquiler'])}/h" for p in e["propiedades"])
        embed.add_field(name="Propiedades", value=props, inline=False)
    else:
        embed.add_field(name="Propiedades", value="Ninguna", inline=False)

    if e["vehiculos"]:
        aviones = chr(10).join(f"✈️ {AVIONES[a]['nombre']} — {AVIONES[a]['pax']} pax, {AVIONES[a]['carga_kg']}kg" for a in e["vehiculos"])
        embed.add_field(name="Aviones", value=aviones, inline=False)
    else:
        embed.add_field(name="Aviones", value="Ninguno", inline=False)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="eco_ranking", description="Ranking de los más ricos del servidor",
                  guild=discord.Object(id=GUILD_ID))
async def slash_eco_ranking(interaction: discord.Interaction):
    if "economia" not in data or not data["economia"]:
        await interaction.response.send_message("Nadie tiene plata todavía.", ephemeral=True); return
    top = sorted(data["economia"].values(), key=lambda x: x.get("balance", 0), reverse=True)[:10]
    top = [e for e in top if e.get("balance", 0) > 0]
    if not top:
        await interaction.response.send_message("Nadie tiene plata todavía.", ephemeral=True); return
    medallas = ["🥇", "🥈", "🥉"]
    lineas = [f"{medallas[i] if i < 3 else f'`{i+1}.`'} **{e['nombre']}** — {formato_plata(e['balance'])}" for i, e in enumerate(top)]
    desc = chr(10).join(lineas)
    embed = discord.Embed(title="💰 Los más ricos del servidor", description=desc, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="dar_plata", description="[ADMIN] Dale plata a alguien",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(usuario="Usuario", monto="Monto")
async def slash_dar_plata(interaction: discord.Interaction, usuario: discord.Member, monto: int):
    if interaction.user.id != TU_USER_ID:
        await interaction.response.send_message("❌ Solo el admin.", ephemeral=True); return
    e = eco_get(usuario.id)
    e["balance"] += monto
    e["nombre"] = usuario.display_name
    guardar_data()
    await interaction.response.send_message(f"💰 Le diste **{formato_plata(monto)}** a {usuario.mention}.")

@bot.tree.command(name="sacar_plata", description="[ADMIN] Sacale plata a alguien",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(usuario="Usuario", monto="Monto")
async def slash_sacar_plata(interaction: discord.Interaction, usuario: discord.Member, monto: int):
    if interaction.user.id != TU_USER_ID:
        await interaction.response.send_message("❌ Solo el admin.", ephemeral=True); return
    e = eco_get(usuario.id)
    e["balance"] = max(0, e["balance"] - monto)
    guardar_data()
    await interaction.response.send_message(f"💸 Le sacaste **{formato_plata(monto)}** a {usuario.mention}.")


# ─────────────────────────────────────────
#  COMBUSTIBLE Y HORAS DE VUELO
# ─────────────────────────────────────────

# Datos técnicos de cada avión: capacidad (litros), consumo (l/h), costo combustible por litro
AVION_SPECS = {
    "pa11":         {"cap_litros": 75,   "consumo_lh": 22,  "horas_mant": 500,  "costo_mant": 800_000,   "costo_overhaul": 2_000_000},
    "pa38":         {"cap_litros": 95,   "consumo_lh": 26,  "horas_mant": 500,  "costo_mant": 1_000_000, "costo_overhaul": 2_500_000},
    "pa28":         {"cap_litros": 190,  "consumo_lh": 35,  "horas_mant": 500,  "costo_mant": 1_200_000, "costo_overhaul": 3_000_000},
    "cessna150":    {"cap_litros": 95,   "consumo_lh": 22,  "horas_mant": 500,  "costo_mant": 900_000,   "costo_overhaul": 2_200_000},
    "cessna172":    {"cap_litros": 212,  "consumo_lh": 36,  "horas_mant": 500,  "costo_mant": 1_300_000, "costo_overhaul": 3_200_000},
    "cessna182":    {"cap_litros": 280,  "consumo_lh": 45,  "horas_mant": 500,  "costo_mant": 1_500_000, "costo_overhaul": 3_800_000},
    "grandcaravan": {"cap_litros": 1070, "consumo_lh": 180, "horas_mant": 600,  "costo_mant": 5_000_000, "costo_overhaul": 15_000_000},
    "b200":         {"cap_litros": 1700, "consumo_lh": 350, "horas_mant": 600,  "costo_mant": 12_000_000,"costo_overhaul": 40_000_000},
    "b350":         {"cap_litros": 2000, "consumo_lh": 420, "horas_mant": 600,  "costo_mant": 15_000_000,"costo_overhaul": 50_000_000},
    "b58":          {"cap_litros": 680,  "consumo_lh": 120, "horas_mant": 500,  "costo_mant": 4_000_000, "costo_overhaul": 12_000_000},
}

PRECIO_LITRO_AVGAS = 1_800  # pesos por litro de AVGAS 100LL

def avion_estado_get(user_id: int, avion_id: str) -> dict:
    """Devuelve o inicializa el estado de un avión para un usuario."""
    e = eco_get(user_id)
    if "aviones_estado" not in e:
        e["aviones_estado"] = {}
    if avion_id not in e["aviones_estado"]:
        specs = AVION_SPECS.get(avion_id, {})
        e["aviones_estado"][avion_id] = {
            "combustible": specs.get("cap_litros", 100),  # empieza lleno
            "horas_totales": 0.0,
            "horas_desde_mant": 0.0,
            "horas_desde_overhaul": 0.0,
            "bloqueado": None,  # None o motivo
            "dano": None,       # None o "leve"/"moderado"/"grave"
        }
    return e["aviones_estado"][avion_id]

def horas_vuelo_get(user_id: int) -> dict:
    e = eco_get(user_id)
    if "horas_vuelo" not in e:
        e["horas_vuelo"] = {"total": 0.0, "por_avion": {}}
    return e["horas_vuelo"]



# ─────────────────────────────────────────
#  ITEMS DE LA TIENDA
# ─────────────────────────────────────────

ITEMS_TIENDA = {
    "notebook":      {"nombre": "Notebook",                    "precio": 500_000,    "bonus_trabajo": 0.05,  "descripcion": "+5% ingreso trabajos de programador/editor"},
    "pc_gamer":      {"nombre": "PC Gamer",                    "precio": 1_200_000,  "bonus_trabajo": 0.10,  "descripcion": "+10% ingreso trabajos de programador"},
    "celular":       {"nombre": "Celular último modelo",       "precio": 300_000,    "bonus_trabajo": 0.03,  "descripcion": "+3% en trabajos de delivery y cadetería"},
    "herramientas":  {"nombre": "Set de herramientas",         "precio": 400_000,    "bonus_trabajo": 0.07,  "descripcion": "+7% ingreso mecánico, electricista, albañil"},
    "camara_pro":    {"nombre": "Cámara profesional",          "precio": 800_000,    "bonus_trabajo": 0.08,  "descripcion": "+8% ingreso editor de video"},
    "drone":         {"nombre": "Drone profesional",           "precio": 1_500_000,  "bonus_trabajo": 0.12,  "descripcion": "+12% ingreso editor de video"},
    "gps_aero":      {"nombre": "GPS aeronáutico Garmin",      "precio": 2_000_000,  "bonus_vuelo": 0.05,    "descripcion": "+5% ingresos de vuelo, -5% probabilidad de accidente"},
    "tablet":        {"nombre": "Tablet profesional",          "precio": 600_000,    "bonus_trabajo": 0.04,  "descripcion": "+4% en trabajos de arquitecto y traductor"},
    "radio_aero":    {"nombre": "Radio aeronáutica portable",  "precio": 1_000_000,  "bonus_vuelo": 0.03,    "descripcion": "+3% ingresos de vuelo"},
    "uniforme":      {"nombre": "Uniforme profesional",        "precio": 200_000,    "bonus_trabajo": 0.02,  "descripcion": "+2% en todos los trabajos"},
    "cr3":           {"nombre": "Calculadora CR-3",            "precio": 150_000,    "bonus_vuelo": 0.02,    "descripcion": "+2% ingresos de vuelo"},
    "hangar":        {"nombre": "Hangar privado",              "precio": 50_000_000, "bonus_vuelo": 0.15,    "descripcion": "+15% ingresos de vuelo, -50% costos de mantenimiento"},
    "seguro_aero":   {"nombre": "Seguro aeronáutico",          "precio": 5_000_000,  "bonus_vuelo": 0.0,     "descripcion": "Cubre el 50% de los costos de reparación por accidente"},
}

def items_get(user_id: int) -> list:
    e = eco_get(user_id)
    if "items" not in e:
        e["items"] = []
    return e["items"]
# ─────────────────────────────────────────
#  COMANDOS DE AUTOS
# ─────────────────────────────────────────

@bot.tree.command(name="autos_tienda", description="Ver todos los autos disponibles",
                  guild=discord.Object(id=GUILD_ID))
async def slash_autos_tienda(interaction: discord.Interaction):
    clases = ["baja", "media_baja", "media", "media_alta", "alta", "premium", "elite"]
    nombres = {"baja": "🔵 Clase Baja", "media_baja": "🟢 Clase Media Baja", "media": "🟡 Clase Media",
               "media_alta": "🟠 Clase Media Alta", "alta": "🔴 Clase Alta",
               "premium": "💜 Premium", "elite": "👑 Élite"}
    embed = discord.Embed(title="🚗 Concesionaria", color=discord.Color.blue())
    for clase in clases:
        items = [(k, v) for k, v in AUTOS.items() if v["clase"] == clase]
        if items:
            lineas = [f"`{k}` {v['nombre']} — {formato_plata(v['precio'])} | {TRABAJOS_AUTO[v['trabajo']]} +{formato_plata(v['ingreso'])} c/{v['cooldown']//60}min" for k, v in items]
            # Split if too long
            chunk = chr(10).join(lineas[:5])
            embed.add_field(name=nombres[clase], value=chunk, inline=False)
    embed.set_footer(text="Usá /comprar_auto <id> para comprar")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="comprar_auto", description="Comprá un auto",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(auto_id="ID del auto (ej: corolla, bmwm3, bugatti)")
async def slash_comprar_auto(interaction: discord.Interaction, auto_id: str):
    auto_id = auto_id.lower().strip()
    if auto_id not in AUTOS:
        await interaction.response.send_message("❌ Auto no encontrado. Usá `/autos_tienda` para ver opciones.", ephemeral=True); return
    e = auto_get(interaction.user.id)
    info = AUTOS[auto_id]
    if auto_id in e["autos"]:
        await interaction.response.send_message("Ya tenés ese auto.", ephemeral=True); return
    if e["balance"] < info["precio"]:
        await interaction.response.send_message(
            f"❌ Necesitás {formato_plata(info['precio'])} y tenés {formato_plata(e['balance'])}.", ephemeral=True); return
    e["balance"] -= info["precio"]
    e["autos"].append(auto_id)
    guardar_data()
    await interaction.response.send_message(
        f"🚗 Compraste el **{info['nombre']}** por {formato_plata(info['precio'])}. "
        f"Usá `/usar_auto {auto_id}` para trabajar con él.")

@bot.tree.command(name="usar_auto", description="Trabajá con tu auto y cobrá",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(auto_id="ID del auto a usar")
async def slash_usar_auto(interaction: discord.Interaction, auto_id: str):
    auto_id = auto_id.lower().strip()
    e = auto_get(interaction.user.id)
    if auto_id not in e["autos"]:
        await interaction.response.send_message(
            f"No tenés ese auto. Tus autos: {', '.join(e['autos']) or 'ninguno'}.", ephemeral=True); return
    if auto_id not in AUTOS:
        await interaction.response.send_message("Auto inválido.", ephemeral=True); return
    info = AUTOS[auto_id]
    ahora = time.time()
    ultimo = e["ultimo_auto"].get(auto_id, 0)
    restante = (ultimo + info["cooldown"]) - ahora
    if restante > 0:
        mins = int(restante // 60); segs = int(restante % 60)
        await interaction.response.send_message(
            f"⏳ El {info['nombre']} necesita descanso. Faltan **{mins}m {segs}s**.", ephemeral=True); return
    ingreso = info["ingreso"]
    # Propina 20% de chance para trabajos de transporte de personas
    propina = 0
    if info["trabajo"] in ["uber_auto", "remis", "vip", "evento_vip", "elite_vip"] and random.random() < 0.2:
        propina = int(ingreso * random.uniform(0.1, 0.3))
    total = ingreso + propina
    e["balance"] += total
    e["ultimo_auto"][auto_id] = ahora
    guardar_data()
    propina_txt = f" + {formato_plata(propina)} de propina 🎁" if propina else ""
    embed = discord.Embed(
        title=f"{TRABAJOS_AUTO[info['trabajo']]} — {info['nombre']}",
        description=f"Completaste un turno y cobraste **{formato_plata(ingreso)}**{propina_txt}",
        color=discord.Color.green(),
    )
    embed.add_field(name="Total", value=formato_plata(total), inline=True)
    embed.add_field(name="Balance", value=formato_plata(e["balance"]), inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="mis_autos", description="Ver tus autos",
                  guild=discord.Object(id=GUILD_ID))
async def slash_mis_autos(interaction: discord.Interaction):
    e = auto_get(interaction.user.id)
    if not e["autos"]:
        await interaction.response.send_message("No tenés autos. Comprá en `/autos_tienda`.", ephemeral=True); return
    ahora = time.time()
    lineas = []
    for aid in e["autos"]:
        if aid not in AUTOS: continue
        info = AUTOS[aid]
        ultimo = e["ultimo_auto"].get(aid, 0)
        restante = (ultimo + info["cooldown"]) - ahora
        estado_auto = f"✅ Disponible" if restante <= 0 else f"⏳ {int(restante//60)}m"
        lineas.append(f"🚗 **{info['nombre']}** — {TRABAJOS_AUTO[info['trabajo']]} — {estado_auto}")
    embed = discord.Embed(title="🚗 Mis autos", description=chr(10).join(lineas), color=discord.Color.blue())
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="items_tienda", description="Ver items disponibles con sus bonificaciones",
                  guild=discord.Object(id=GUILD_ID))
async def slash_items_tienda(interaction: discord.Interaction):
    embed = discord.Embed(title="🛒 Tienda de Items", color=discord.Color.purple())
    lineas = [f"`{k}` **{v['nombre']}** — {formato_plata(v['precio'])} | {v['descripcion']}" for k, v in ITEMS_TIENDA.items()]
    chunk1 = chr(10).join(lineas[:7])
    chunk2 = chr(10).join(lineas[7:])
    embed.add_field(name="Items disponibles", value=chunk1, inline=False)
    if chunk2:
        embed.add_field(name="​", value=chunk2, inline=False)
    embed.set_footer(text="Usá /comprar_item <id> para comprar")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="comprar_item", description="Comprá un item de la tienda",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(item_id="ID del item (ej: notebook, gps_aero, hangar)")
async def slash_comprar_item(interaction: discord.Interaction, item_id: str):
    item_id = item_id.lower().strip()
    if item_id not in ITEMS_TIENDA:
        await interaction.response.send_message("❌ Item no encontrado. Usá `/items_tienda`.", ephemeral=True); return
    e = eco_get(interaction.user.id)
    items = items_get(interaction.user.id)
    info = ITEMS_TIENDA[item_id]
    if item_id in items:
        await interaction.response.send_message("Ya tenés ese item.", ephemeral=True); return
    if e["balance"] < info["precio"]:
        await interaction.response.send_message(
            f"❌ Necesitás {formato_plata(info['precio'])} y tenés {formato_plata(e['balance'])}.", ephemeral=True); return
    e["balance"] -= info["precio"]
    items.append(item_id)
    guardar_data()
    await interaction.response.send_message(
        f"✅ Compraste **{info['nombre']}**. {info['descripcion']}")

@bot.tree.command(name="mis_items", description="Ver tus items y bonificaciones",
                  guild=discord.Object(id=GUILD_ID))
async def slash_mis_items(interaction: discord.Interaction):
    items = items_get(interaction.user.id)
    if not items:
        await interaction.response.send_message("No tenés items. Comprá en `/items_tienda`.", ephemeral=True); return
    lineas = [f"✅ **{ITEMS_TIENDA[i]['nombre']}** — {ITEMS_TIENDA[i]['descripcion']}" for i in items if i in ITEMS_TIENDA]
    embed = discord.Embed(title="🎒 Mis items", description=chr(10).join(lineas), color=discord.Color.purple())
    await interaction.response.send_message(embed=embed)


# ─────────────────────────────────────────
#  PRÉSTAMOS
# ─────────────────────────────────────────

TASA_INTERES = 0.05  # 5% mensual
MORA_DIARIA  = 0.002 # 0.2% diario por mora

def prestamo_get(user_id: int) -> dict | None:
    e = eco_get(user_id)
    return e.get("prestamo", None)

@bot.tree.command(name="pedir_prestamo", description="Pedí un préstamo al banco del servidor",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(monto="Monto a pedir", cuotas="Cantidad de cuotas (1-12)")
async def slash_pedir_prestamo(interaction: discord.Interaction, monto: int, cuotas: int):
    if monto <= 0 or cuotas < 1 or cuotas > 12:
        await interaction.response.send_message("❌ Monto inválido o cuotas fuera de rango (1-12).", ephemeral=True); return
    e = eco_get(interaction.user.id)
    if e.get("prestamo"):
        await interaction.response.send_message("❌ Ya tenés un préstamo activo. Saldalo antes de pedir otro.", ephemeral=True); return
    interes_total = monto * TASA_INTERES * cuotas
    total_a_pagar = monto + interes_total
    cuota_valor   = int(total_a_pagar / cuotas)
    e["prestamo"] = {
        "monto_original": monto,
        "total":          total_a_pagar,
        "cuotas_total":   cuotas,
        "cuotas_pagas":   0,
        "cuota_valor":    cuota_valor,
        "fecha_inicio":   time.time(),
        "ultima_cuota":   time.time(),
        "en_mora":        False,
    }
    e["balance"] += monto
    guardar_data()
    embed = discord.Embed(title="🏦 Préstamo aprobado", color=discord.Color.green())
    embed.add_field(name="Monto recibido", value=formato_plata(monto), inline=True)
    embed.add_field(name="Total a pagar", value=formato_plata(int(total_a_pagar)), inline=True)
    embed.add_field(name="Cuotas", value=f"{cuotas} × {formato_plata(cuota_valor)}", inline=True)
    embed.add_field(name="Interés", value=f"5% mensual", inline=True)
    embed.set_footer(text="Usá /pagar_cuota para abonar cada cuota")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="pagar_cuota", description="Pagá una cuota de tu préstamo",
                  guild=discord.Object(id=GUILD_ID))
async def slash_pagar_cuota(interaction: discord.Interaction):
    e = eco_get(interaction.user.id)
    p = e.get("prestamo")
    if not p:
        await interaction.response.send_message("No tenés préstamo activo.", ephemeral=True); return
    cuota = p["cuota_valor"]
    # Mora si pasaron más de 7 días desde la última cuota
    dias_desde_ultima = (time.time() - p["ultima_cuota"]) / 86400
    mora = 0
    if dias_desde_ultima > 7:
        mora = int(cuota * MORA_DIARIA * (dias_desde_ultima - 7))
        p["en_mora"] = True
    total_cuota = cuota + mora
    if e["balance"] < total_cuota:
        await interaction.response.send_message(
            f"❌ Necesitás {formato_plata(total_cuota)} y tenés {formato_plata(e['balance'])}.", ephemeral=True); return
    e["balance"] -= total_cuota
    p["cuotas_pagas"] += 1
    p["ultima_cuota"] = time.time()
    p["en_mora"] = False
    if p["cuotas_pagas"] >= p["cuotas_total"]:
        del e["prestamo"]
        guardar_data()
        await interaction.response.send_message("✅ ¡Préstamo saldado completamente! Sos libre de deudas 🎉")
        return
    guardar_data()
    restantes = p["cuotas_total"] - p["cuotas_pagas"]
    embed = discord.Embed(title="💸 Cuota pagada", color=discord.Color.green())
    embed.add_field(name="Cuota", value=formato_plata(cuota), inline=True)
    if mora: embed.add_field(name="Mora", value=formato_plata(mora), inline=True)
    embed.add_field(name="Cuotas restantes", value=str(restantes), inline=True)
    embed.add_field(name="Balance", value=formato_plata(e["balance"]), inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="mi_prestamo", description="Ver el estado de tu préstamo",
                  guild=discord.Object(id=GUILD_ID))
async def slash_mi_prestamo(interaction: discord.Interaction):
    e = eco_get(interaction.user.id)
    p = e.get("prestamo")
    if not p:
        await interaction.response.send_message("No tenés préstamo activo.", ephemeral=True); return
    embed = discord.Embed(title="🏦 Tu préstamo", color=discord.Color.orange())
    embed.add_field(name="Cuotas pagas", value=f"{p['cuotas_pagas']}/{p['cuotas_total']}", inline=True)
    embed.add_field(name="Próxima cuota", value=formato_plata(p["cuota_valor"]), inline=True)
    embed.add_field(name="En mora", value="⚠️ Sí" if p["en_mora"] else "✅ No", inline=True)
    await interaction.response.send_message(embed=embed)

# ─────────────────────────────────────────
#  INVERSIONES
# ─────────────────────────────────────────

FONDOS = {
    "plazo_fijo":  {"nombre": "Plazo Fijo",         "tasa_diaria": 0.003,  "riesgo": 0,    "descripcion": "Tasa fija 0.3%/día, sin riesgo"},
    "conservador": {"nombre": "Fondo Conservador",  "tasa_diaria": 0.005,  "riesgo": 0.02, "descripcion": "~0.5%/día, riesgo bajo"},
    "moderado":    {"nombre": "Fondo Moderado",     "tasa_diaria": 0.009,  "riesgo": 0.08, "descripcion": "~0.9%/día, riesgo moderado"},
    "agresivo":    {"nombre": "Fondo Agresivo",     "tasa_diaria": 0.018,  "riesgo": 0.20, "descripcion": "~1.8%/día, riesgo alto"},
}

def inversiones_get(user_id: int) -> dict:
    e = eco_get(user_id)
    if "inversiones" not in e:
        e["inversiones"] = {}
    return e["inversiones"]

@bot.tree.command(name="invertir", description="Invertí tu plata en un fondo",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(fondo="plazo_fijo / conservador / moderado / agresivo", monto="Monto a invertir")
async def slash_invertir(interaction: discord.Interaction, fondo: str, monto: int):
    if fondo not in FONDOS:
        await interaction.response.send_message(f"Fondos disponibles: {', '.join(FONDOS.keys())}", ephemeral=True); return
    if monto <= 0:
        await interaction.response.send_message("❌ El monto tiene que ser mayor a 0.", ephemeral=True); return
    e = eco_get(interaction.user.id)
    if e["balance"] < monto:
        await interaction.response.send_message(
            f"❌ Tenés {formato_plata(e['balance'])} y querés invertir {formato_plata(monto)}.", ephemeral=True); return
    inv = inversiones_get(interaction.user.id)
    if fondo in inv:
        inv[fondo]["monto"] += monto
    else:
        inv[fondo] = {"monto": monto, "fecha": time.time()}
    e["balance"] -= monto
    guardar_data()
    info = FONDOS[fondo]
    embed = discord.Embed(title=f"📈 Inversión realizada — {info['nombre']}", color=discord.Color.green())
    embed.add_field(name="Invertido", value=formato_plata(monto), inline=True)
    embed.add_field(name="Tasa diaria", value=f"~{info['tasa_diaria']*100:.1f}%", inline=True)
    embed.add_field(name="Balance restante", value=formato_plata(e["balance"]), inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="retirar_inversion", description="Retirá tu inversión con ganancias",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(fondo="Nombre del fondo a retirar")
async def slash_retirar_inversion(interaction: discord.Interaction, fondo: str):
    if fondo not in FONDOS:
        await interaction.response.send_message(f"Fondos: {', '.join(FONDOS.keys())}", ephemeral=True); return
    e = eco_get(interaction.user.id)
    inv = inversiones_get(interaction.user.id)
    if fondo not in inv:
        await interaction.response.send_message("No tenés inversión en ese fondo.", ephemeral=True); return
    info     = FONDOS[fondo]
    capital  = inv[fondo]["monto"]
    dias     = (time.time() - inv[fondo]["fecha"]) / 86400
    # Aplicar riesgo
    variacion = random.uniform(-info["riesgo"], info["tasa_diaria"] * 2)
    tasa_real = max(-0.3, info["tasa_diaria"] + variacion)
    ganancia  = int(capital * tasa_real * dias)
    total     = capital + ganancia
    e["balance"] += total
    del inv[fondo]
    guardar_data()
    color = discord.Color.green() if ganancia >= 0 else discord.Color.red()
    embed = discord.Embed(title=f"📊 Retiro — {info['nombre']}", color=color)
    embed.add_field(name="Capital", value=formato_plata(capital), inline=True)
    embed.add_field(name="Días invertido", value=f"{dias:.1f}", inline=True)
    embed.add_field(name="Ganancia/Pérdida", value=f"{'+'if ganancia>=0 else ''}{formato_plata(ganancia)}", inline=True)
    embed.add_field(name="Total recibido", value=formato_plata(total), inline=True)
    embed.add_field(name="Balance", value=formato_plata(e["balance"]), inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="mis_inversiones", description="Ver tus inversiones activas",
                  guild=discord.Object(id=GUILD_ID))
async def slash_mis_inversiones(interaction: discord.Interaction):
    inv = inversiones_get(interaction.user.id)
    if not inv:
        await interaction.response.send_message("No tenés inversiones activas.", ephemeral=True); return
    embed = discord.Embed(title="📈 Mis inversiones", color=discord.Color.blue())
    ahora = time.time()
    for fondo_id, datos in inv.items():
        info   = FONDOS[fondo_id]
        dias   = (ahora - datos["fecha"]) / 86400
        ganancia_est = int(datos["monto"] * info["tasa_diaria"] * dias)
        embed.add_field(
            name=info["nombre"],
            value=f"Capital: {formato_plata(datos['monto'])} | {dias:.1f} días | Est: +{formato_plata(ganancia_est)}",
            inline=False,
        )
    await interaction.response.send_message(embed=embed)

# ─────────────────────────────────────────
#  RANKING
# ─────────────────────────────────────────

# ─────────────────────────────────────────
#  GAGA POINTS
# ─────────────────────────────────────────

ROL_MINI_PAPOIS = "mini papois"  # nombre exacto del rol con permiso para dar/quitar GP

def puede_gestionar_gaga(member: discord.Member) -> bool:
    """Devuelve True si es el admin o tiene el rol mini papois."""
    if member.id == TU_USER_ID:
        return True
    return any(rol.name.lower() == ROL_MINI_PAPOIS.lower() for rol in member.roles)

@bot.tree.command(name="give", description="Otorgá Gaga Points a un usuario (admin o mini papois)",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(usuario="Usuario a quien darle los puntos", puntos="Cantidad de Gaga Points")
async def slash_give(interaction: discord.Interaction, usuario: discord.Member, puntos: int):
    if not puede_gestionar_gaga(interaction.user):
        await interaction.response.send_message(
            "❌ Solo el admin o un mini papois puede dar Gaga Points.", ephemeral=True)
        return

    r = ranking_get(usuario.id)
    r["nombre"] = usuario.display_name
    r["gaga"] = r.get("gaga", 0) + puntos
    total = r["gaga"]
    guardar_data()

    rango_anterior = gaga_rango(total - puntos)
    rango_nuevo    = gaga_rango(total)

    embed = discord.Embed(
        title="🎭 Gaga Points otorgados",
        description=f"{usuario.mention} recibió **+{puntos:,} Gaga Points**",
        color=discord.Color.from_rgb(255, 20, 147),
    )
    embed.add_field(name="Total", value=f"**{total:,} GP**", inline=True)
    if rango_nuevo:
        embed.add_field(name="Rango", value=f"**{rango_nuevo}**", inline=True)
    embed.set_footer(text=f"Otorgado por {interaction.user.display_name}")

    # Anuncio si subió de rango
    if rango_nuevo and rango_nuevo != rango_anterior:
        embed.add_field(
            name="🆙 ¡Nuevo rango desbloqueado!",
            value=f"**{rango_nuevo}**",
            inline=False,
        )
        # Asignar rol automáticamente si existe en el servidor
        guild = interaction.guild
        rol = discord.utils.get(guild.roles, name=rango_nuevo)
        if not rol:
            rol = await guild.create_role(name=rango_nuevo, color=discord.Color.from_rgb(255, 20, 147))
        # Sacar rangos anteriores y poner el nuevo
        for _, nombre_rango in GAGA_RANGOS:
            r_viejo = discord.utils.get(guild.roles, name=nombre_rango)
            if r_viejo and r_viejo in usuario.roles:
                await usuario.remove_roles(r_viejo)
        await usuario.add_roles(rol)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="take", description="Quitá Gaga Points a un usuario (admin o mini papois)",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(usuario="Usuario a quien quitarle los puntos", puntos="Cantidad a quitar")
async def slash_take(interaction: discord.Interaction, usuario: discord.Member, puntos: int):
    if not puede_gestionar_gaga(interaction.user):
        await interaction.response.send_message(
            "❌ Solo el admin o un mini papois puede quitar Gaga Points.", ephemeral=True)
        return

    r = ranking_get(usuario.id)
    r["nombre"] = usuario.display_name
    r["gaga"] = max(0, r.get("gaga", 0) - puntos)
    guardar_data()

    embed = discord.Embed(
        title="🎭 Gaga Points quitados",
        description=f"Se le quitaron **{puntos:,} Gaga Points** a {usuario.mention}",
        color=discord.Color.dark_red(),
    )
    embed.add_field(name="Total restante", value=f"**{r['gaga']:,} GP**", inline=True)
    rango = gaga_rango(r["gaga"])
    if rango:
        embed.add_field(name="Rango", value=f"**{rango}**", inline=True)
    embed.set_footer(text=f"Quitado por {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)


# ─────────────────────────────────────────
#  WACHIN POINTS — penalización con timeout automático
# ─────────────────────────────────────────

def wachin_get(user_id: int) -> dict:
    """Inicializa/devuelve el registro de wachin points dentro del ranking."""
    r = ranking_get(user_id)
    if "wachin" not in r:
        r["wachin"] = 0
    return r

async def aplicar_timeout_si_corresponde(guild: discord.Guild, miembro: discord.Member, puntos_totales: int) -> str | None:
    """Aplica el timeout de Discord según el tier de Wachin Points. Devuelve la etiqueta aplicada o None."""
    tier = wachin_tier(puntos_totales)
    if not tier:
        return None
    minutos, etiqueta = tier
    duracion = discord.utils.utcnow() + timedelta(minutes=minutos)
    try:
        await miembro.timeout(duracion, reason=f"Wachin Points alcanzó {puntos_totales:,}")
        return etiqueta
    except discord.Forbidden:
        return "❌ (sin permisos para aplicar timeout — revisá la jerarquía de roles del bot)"
    except Exception:
        return None


@bot.tree.command(name="give_wachin", description="[ADMIN] Dale Wachin Points a un usuario (penalización)",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(usuario="Usuario a penalizar", puntos="Cantidad de Wachin Points a sumar")
async def slash_give_wachin(interaction: discord.Interaction, usuario: discord.Member, puntos: int):
    if interaction.user.id != TU_USER_ID:
        await interaction.response.send_message("❌ Solo el admin puede dar Wachin Points.", ephemeral=True)
        return
    if puntos <= 0:
        await interaction.response.send_message("❌ Los puntos tienen que ser positivos.", ephemeral=True)
        return

    r = wachin_get(usuario.id)
    r["nombre"] = usuario.display_name
    r["wachin"] += puntos
    total = r["wachin"]
    guardar_data()

    etiqueta_aplicada = await aplicar_timeout_si_corresponde(interaction.guild, usuario, total)

    embed = discord.Embed(
        title="🤡 Wachin Points otorgados",
        description=f"{usuario.mention} recibió **+{puntos:,} Wachin Points**",
        color=discord.Color.orange(),
    )
    embed.add_field(name="Total", value=f"**{total:,} WP**", inline=True)
    if etiqueta_aplicada:
        embed.add_field(name="⏱️ Timeout aplicado", value=etiqueta_aplicada, inline=True)
    embed.set_footer(text=f"Otorgado por {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="take_wachin", description="[ADMIN] Quitá Wachin Points a un usuario",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(usuario="Usuario", puntos="Cantidad a quitar")
async def slash_take_wachin(interaction: discord.Interaction, usuario: discord.Member, puntos: int):
    if interaction.user.id != TU_USER_ID:
        await interaction.response.send_message("❌ Solo el admin puede quitar Wachin Points.", ephemeral=True)
        return

    r = wachin_get(usuario.id)
    r["wachin"] = max(0, r["wachin"] - puntos)
    guardar_data()

    embed = discord.Embed(
        title="🤡 Wachin Points quitados",
        description=f"Se le quitaron **{puntos:,} Wachin Points** a {usuario.mention}",
        color=discord.Color.green(),
    )
    embed.add_field(name="Total restante", value=f"**{r['wachin']:,} WP**", inline=True)
    embed.set_footer(text=f"Quitado por {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="wachin", description="Muestra los Wachin Points de un usuario",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(usuario="Usuario a consultar (dejalo vacío para verte a vos)")
async def slash_wachin(interaction: discord.Interaction, usuario: discord.Member = None):
    target = usuario or interaction.user
    r = wachin_get(target.id)
    puntos = r["wachin"]
    tier = wachin_tier(puntos)

    embed = discord.Embed(
        title=f"🤡 Wachin Points de {target.display_name}",
        color=discord.Color.orange(),
    )
    embed.add_field(name="Puntos", value=f"**{puntos:,} WP**", inline=True)
    embed.add_field(name="Estado", value=tier[1] if tier else "✅ Sin sanción", inline=True)

    # Próximo umbral (el más cercano por encima de los puntos actuales)
    siguiente = None
    for minimo, minutos, etiqueta in reversed(WACHIN_TIERS):
        if puntos < minimo:
            siguiente = (minimo, etiqueta)
            break
    if siguiente:
        falta = siguiente[0] - puntos
        embed.add_field(name="Próxima sanción", value=f"{siguiente[1]} en {falta:,} WP más", inline=False)

    await interaction.response.send_message(embed=embed)



@bot.tree.command(name="gaga", description="Muestra los Gaga Points de un usuario",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(usuario="Usuario a consultar (dejalo vacío para verte a vos)")
async def slash_gaga(interaction: discord.Interaction, usuario: discord.Member = None):
    target = usuario or interaction.user
    r = ranking_get(target.id)
    puntos = r.get("gaga", 0)
    rango = gaga_rango(puntos)

    # Calcular progreso al siguiente rango
    siguiente = None
    for minimo, nombre in reversed(GAGA_RANGOS):
        if puntos < minimo:
            siguiente = (minimo, nombre)
    
    embed = discord.Embed(
        title=f"🎭 Gaga Points de {target.display_name}",
        color=discord.Color.from_rgb(255, 20, 147),
    )
    embed.add_field(name="Puntos", value=f"**{puntos:,} GP**", inline=True)
    embed.add_field(name="Rango", value=f"**{rango or 'Sin rango'}**", inline=True)

    if siguiente:
        falta = siguiente[0] - puntos
        progreso = min(20, int((puntos / siguiente[0]) * 20))
        barra = "█" * progreso + "░" * (20 - progreso)
        embed.add_field(
            name=f"Progreso a {siguiente[1]}",
            value=f"`{barra}` faltan **{falta:,} GP**",
            inline=False,
        )
    else:
        embed.add_field(name="👑 Rango máximo", value="¡Sos un **Gaga Final Boss**!", inline=False)

    embed.set_thumbnail(url=target.display_avatar.url)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="gagaleaderboard", description="Ranking de Gaga Points del servidor",
                  guild=discord.Object(id=GUILD_ID))
async def slash_gagaleaderboard(interaction: discord.Interaction):
    if not data["ranking"]:
        await interaction.response.send_message("Nadie tiene Gaga Points todavía.", ephemeral=True)
        return

    top = sorted(data["ranking"].values(), key=lambda x: x.get("gaga", 0), reverse=True)[:10]
    top = [e for e in top if e.get("gaga", 0) > 0]

    if not top:
        await interaction.response.send_message("Nadie tiene Gaga Points todavía.", ephemeral=True)
        return

    medallas = ["🥇", "🥈", "🥉"]
    lineas = []
    for i, entry in enumerate(top):
        medalla = medallas[i] if i < 3 else f"`{i+1}.`"
        rango = gaga_rango(entry.get("gaga", 0))
        rango_txt = f" — *{rango}*" if rango else ""
        lineas.append(f"{medalla} **{entry['nombre']}** — {entry.get('gaga', 0):,} GP{rango_txt}")

    descripcion_lb = chr(10).join(lineas)
    embed = discord.Embed(
        title="🎭 Leaderboard de Gaga Points",
        description=descripcion_lb,
        color=discord.Color.from_rgb(255, 20, 147),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="nivel", description="Muestra tu nivel y XP actual",
                  guild=discord.Object(id=GUILD_ID))
async def slash_nivel(interaction: discord.Interaction):
    r = ranking_get(interaction.user.id)
    xp = r.get("xp", 0)
    nivel = xp_a_nivel(xp)
    xp_actual = xp - xp_para_nivel(nivel)
    xp_necesario = xp_para_nivel(nivel + 1) - xp_para_nivel(nivel)
    progreso = min(20, int((xp_actual / max(xp_necesario, 1)) * 20))
    barra = "█" * progreso + "░" * (20 - progreso)

    embed = discord.Embed(
        title=f"📊 Nivel de {interaction.user.display_name}",
        color=discord.Color.from_rgb(255, 215, 0),
    )
    embed.add_field(name="Nivel", value=f"**{nivel}**", inline=True)
    embed.add_field(name="XP Total", value=f"**{xp:,}**", inline=True)
    embed.add_field(name="Mensajes", value=f"**{r.get('mensajes', 0):,}**", inline=True)
    embed.add_field(
        name=f"Progreso al nivel {nivel + 1}",
        value=f"`{barra}` {xp_actual}/{xp_necesario} XP",
        inline=False,
    )
    if nivel in NIVELES_ROLES:
        embed.add_field(name="🏅 Logro actual", value=NIVELES_ROLES[nivel], inline=False)
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ranking", description="Muestra el ranking de actividad del servidor",
                  guild=discord.Object(id=GUILD_ID))
async def slash_ranking(interaction: discord.Interaction):
    if not data["ranking"]:
        await interaction.response.send_message("Nadie tiene actividad todavía.", ephemeral=True); return
    por_mensajes = sorted(data["ranking"].values(), key=lambda x: x["mensajes"], reverse=True)[:10]
    por_musica   = sorted(data["ranking"].values(), key=lambda x: x["musica"],    reverse=True)[:10]
    def fila(i, entry, key):
        medalla = ["🥇", "🥈", "🥉"][i] if i < 3 else f"`{i+1}.`"
        return f"{medalla} **{entry['nombre']}** — {entry[key]}"
    embed = discord.Embed(title="🏆 Ranking del servidor", color=discord.Color.gold())
    embed.add_field(name="💬 Más mensajes",
                    value="\n".join(fila(i, e, "mensajes") for i, e in enumerate(por_mensajes)) or "—", inline=True)
    embed.add_field(name="🎵 Más música pedida",
                    value="\n".join(fila(i, e, "musica") for i, e in enumerate(por_musica)) or "—", inline=True)
    await interaction.response.send_message(embed=embed)

# ─────────────────────────────────────────
#  CASINO
# ─────────────────────────────────────────

def crear_mazo():
    palos = ["♠", "♥", "♦", "♣"]
    valores = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
    mazo = [f"{v}{p}" for p in palos for v in valores]
    random.shuffle(mazo)
    return mazo

def valor_carta(carta):
    v = carta[:-1]
    if v in ["J", "Q", "K"]: return 10
    if v == "A": return 11
    return int(v)

def valor_mano(mano):
    total = sum(valor_carta(c) for c in mano)
    ases = sum(1 for c in mano if c[:-1] == "A")
    while total > 21 and ases:
        total -= 10; ases -= 1
    return total

partidas_bj: dict[int, dict] = {}

class BlackjackView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=60)
        self.user_id = user_id

    def embed_actual(self):
        p = partidas_bj[self.user_id]
        embed = discord.Embed(title="🃏 Blackjack", color=discord.Color.dark_green())
        embed.add_field(name=f"Tu mano ({valor_mano(p['jugador'])})", value=" ".join(p["jugador"]), inline=False)
        embed.add_field(name="Mano del dealer", value=f"{p['dealer'][0]} 🂠", inline=False)
        embed.add_field(name="Apuesta", value=formato_plata(p["apuesta"]), inline=True)
        return embed

    @discord.ui.button(label="Pedir carta", emoji="➕", style=discord.ButtonStyle.primary)
    async def pedir(self, interaction, button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("No es tu partida.", ephemeral=True); return
        p = partidas_bj[self.user_id]
        p["jugador"].append(p["mazo"].pop())
        total = valor_mano(p["jugador"])
        if total > 21:
            apuesta = p["apuesta"]
            embed = discord.Embed(title="🃏 Blackjack — ¡Te pasaste! 💥",
                                  description=f"Tu mano: {' '.join(p['jugador'])} = **{total}**\nPerdiste **{formato_plata(apuesta)}**.",
                                  color=discord.Color.red())
            del partidas_bj[self.user_id]
            guardar_data()
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            await interaction.response.edit_message(embed=self.embed_actual(), view=self)

    @discord.ui.button(label="Plantarse", emoji="✋", style=discord.ButtonStyle.secondary)
    async def plantar(self, interaction, button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("No es tu partida.", ephemeral=True); return
        p = partidas_bj[self.user_id]
        while valor_mano(p["dealer"]) < 17:
            p["dealer"].append(p["mazo"].pop())
        tj, td = valor_mano(p["jugador"]), valor_mano(p["dealer"])
        apuesta = p["apuesta"]
        e = eco_get(self.user_id)
        if td > 21 or tj > td:
            ganancia = apuesta * 2
            e["balance"] += ganancia
            resultado, color = f"🏆 ¡Ganaste {formato_plata(apuesta)}!", discord.Color.green()
        elif tj == td:
            e["balance"] += apuesta  # devuelve la apuesta
            resultado, color = "🤝 Empate — recuperás la apuesta", discord.Color.gold()
        else:
            resultado, color = f"💀 Perdiste {formato_plata(apuesta)}", discord.Color.red()
        guardar_data()
        embed = discord.Embed(title=f"🃏 Blackjack — {resultado}", color=color)
        embed.add_field(name=f"Tu mano ({tj})", value=" ".join(p["jugador"]), inline=True)
        embed.add_field(name=f"Dealer ({td})", value=" ".join(p["dealer"]), inline=True)
        embed.add_field(name="Balance", value=formato_plata(e["balance"]), inline=False)
        del partidas_bj[self.user_id]
        await interaction.response.edit_message(embed=embed, view=None)

@bot.tree.command(name="blackjack", description="Jugá una mano de blackjack con apuesta",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(apuesta="Cuánto querés apostar")
async def slash_blackjack(interaction: discord.Interaction, apuesta: int):
    uid = interaction.user.id
    e = eco_get(uid)
    if apuesta <= 0:
        await interaction.response.send_message("❌ La apuesta tiene que ser mayor a 0.", ephemeral=True); return
    if apuesta > e["balance"]:
        await interaction.response.send_message(
            f"❌ No tenés suficiente. Tenés {formato_plata(e['balance'])}.", ephemeral=True); return
    mazo = crear_mazo()
    partidas_bj[uid] = {
        "mazo": mazo,
        "jugador": [mazo.pop(), mazo.pop()],
        "dealer": [mazo.pop(), mazo.pop()],
        "apuesta": apuesta,
    }
    e["balance"] -= apuesta
    guardar_data()
    view = BlackjackView(uid)
    await interaction.response.send_message(embed=view.embed_actual(), view=view)

@bot.tree.command(name="ruleta", description="Apostá a un número (0-36) o color (rojo/negro)",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(tipo="Número (0-36), 'rojo' o 'negro'", monto="Cuánto apostás")
async def slash_ruleta(interaction: discord.Interaction, tipo: str, monto: int):
    if monto <= 0:
        await interaction.response.send_message("❌ La apuesta tiene que ser mayor a 0.", ephemeral=True); return
    e = eco_get(interaction.user.id)
    if monto > e["balance"]:
        await interaction.response.send_message(
            f"❌ No tenés suficiente. Tenés {formato_plata(e['balance'])}.", ephemeral=True); return

    numero = random.randint(0, 36)
    rojos = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
    color_resultado = "🔴 Rojo" if numero in rojos else ("⚫ Negro" if numero != 0 else "🟢 Verde")
    tipo = tipo.strip().lower()

    if tipo.isdigit():
        gana = int(tipo) == numero
        multiplicador = 35
        premio = monto * multiplicador if gana else 0
    elif tipo in ["rojo", "negro"]:
        gana = (tipo == "rojo" and numero in rojos) or (tipo == "negro" and numero not in rojos and numero != 0)
        multiplicador = 2
        premio = monto * multiplicador if gana else 0
    else:
        await interaction.response.send_message("Apostá a un número (0-36) o a 'rojo'/'negro'.", ephemeral=True); return

    if gana:
        e["balance"] += premio  # ya incluye la apuesta original × multiplicador
        neto = premio - monto
    else:
        e["balance"] -= monto
        neto = -monto
    guardar_data()

    embed = discord.Embed(
        title="🎰 Ruleta",
        description=f"La bolita cayó en el **{numero}** — {color_resultado}",
        color=discord.Color.green() if gana else discord.Color.red(),
    )
    embed.add_field(name="Tu apuesta", value=f"{formato_plata(monto)} a '{tipo}'", inline=True)
    embed.add_field(name="Resultado", value=f"{'✅ +' if gana else '❌ '}{formato_plata(abs(neto))}", inline=True)
    embed.add_field(name="Balance", value=formato_plata(e["balance"]), inline=True)
    await interaction.response.send_message(embed=embed)

SIMBOLOS = ["🍒", "🍋", "🍊", "🍇", "⭐", "💎", "7️⃣"]
PESOS    = [  30,   25,   20,   15,   6,    3,    1]

@bot.tree.command(name="tragamonedas", description="Tirá las palancas con apuesta",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(apuesta="Cuánto apostás")
async def slash_tragamonedas(interaction: discord.Interaction, apuesta: int):
    if apuesta <= 0:
        await interaction.response.send_message("❌ La apuesta tiene que ser mayor a 0.", ephemeral=True); return
    e = eco_get(interaction.user.id)
    if apuesta > e["balance"]:
        await interaction.response.send_message(
            f"❌ No tenés suficiente. Tenés {formato_plata(e['balance'])}.", ephemeral=True); return

    resultado = random.choices(SIMBOLOS, weights=PESOS, k=3)
    unico = set(resultado)

    if len(unico) == 1:
        if resultado[0] == "7️⃣":
            multiplicador, msg, color = 50, "🎉 ¡JACKPOT! ¡Tres 7! × 50", discord.Color.gold()
        elif resultado[0] == "💎":
            multiplicador, msg, color = 20, "💎 ¡Tres diamantes! × 20", discord.Color.blue()
        elif resultado[0] == "⭐":
            multiplicador, msg, color = 10, "⭐ ¡Tres estrellas! × 10", discord.Color.yellow()
        else:
            multiplicador, msg, color = 5, f"🏆 ¡Tres {resultado[0]}! × 5", discord.Color.green()
        ganancia = apuesta * multiplicador
        neto = ganancia - apuesta
        e["balance"] += neto
    elif len(unico) == 2:
        multiplicador, msg, color = 2, "👌 ¡Par! × 2", discord.Color.yellow()
        ganancia = apuesta * multiplicador
        neto = ganancia - apuesta
        e["balance"] += neto
    else:
        msg, color = "😢 Sin suerte.", discord.Color.red()
        neto = -apuesta
        e["balance"] -= apuesta

    guardar_data()
    embed = discord.Embed(
        title="🎰 Tragamonedas",
        description=f"# {resultado[0]}  {resultado[1]}  {resultado[2]}\n{msg}",
        color=color,
    )
    embed.add_field(name="Apuesta", value=formato_plata(apuesta), inline=True)
    embed.add_field(name="Resultado", value=f"{'✅ +' if neto >= 0 else '❌ '}{formato_plata(abs(neto))}", inline=True)
    embed.add_field(name="Balance", value=formato_plata(e["balance"]), inline=True)
    await interaction.response.send_message(embed=embed)

# ─────────────────────────────────────────
#  MÚSICA — helpers
# ─────────────────────────────────────────

async def buscar_sugerencias(query: str) -> list[dict]:
    """Sugerencias basadas en historial de canciones reproducidas + búsqueda rápida en yt-dlp."""
    query_lower = query.lower()
    resultados = []

    # 1. Buscar en historial de canciones reproducidas (instantáneo)
    historial = data.get("historial_musica", [])
    for cancion in historial:
        titulo = cancion.get("titulo", "")
        if query_lower in titulo.lower() and len(resultados) < 5:
            resultados.append({
                "titulo": titulo[:100],
                "webpage": cancion.get("webpage", ""),
                "duracion": cancion.get("duracion", 0),
            })

    # Si ya tenemos 5 del historial, devolvemos directo
    if len(resultados) >= 5:
        return resultados

    # 2. Buscar en cache
    if query in _sugerencias_cache:
        for s in _sugerencias_cache[query]:
            if s not in resultados:
                resultados.append(s)
        return resultados[:5]

    # 3. Búsqueda rápida con yt-dlp en background (no esperamos más de 2s)
    try:
        data_yt = await asyncio.wait_for(
            asyncio.to_thread(ytdl_suggest.extract_info, query, download=False),
            timeout=2.0
        )
        for entry in (data_yt.get("entries") or [])[:5]:
            if entry and entry.get("title") and entry.get("id"):
                item = {
                    "titulo": entry["title"][:100],
                    "webpage": f"https://www.youtube.com/watch?v={entry['id']}",
                    "duracion": entry.get("duration") or 0,
                }
                if item not in resultados:
                    resultados.append(item)
        _sugerencias_cache[query] = resultados
        if len(_sugerencias_cache) > 100:
            del _sugerencias_cache[next(iter(_sugerencias_cache))]
    except Exception:
        pass

    return resultados[:5]


async def buscar_cancion_url(webpage_url: str) -> dict | None:
    try:
        opts = {**YTDL_OPTIONS, "default_search": None, "noplaylist": True}
        data_yt = await asyncio.to_thread(yt_dlp.YoutubeDL(opts).extract_info, webpage_url, download=False)
        return {
            "titulo": data_yt.get("title"),
            "url": data_yt.get("url"),
            "webpage": data_yt.get("webpage_url"),
            "duracion": data_yt.get("duration", 0),
            "thumbnail": data_yt.get("thumbnail"),
            "uploader": data_yt.get("uploader", "Desconocido"),
        }
    except Exception as e:
        print(f"Error buscar_cancion_url: {e}")
        return None

def formato_duracion(seg: int) -> str:
    m, s = divmod(int(seg), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

# ─────────────────────────────────────────
#  ESTADO DEL PLAYER
# ─────────────────────────────────────────

class EstadoPlayer:
    def __init__(self):
        self.cancion = None
        self.inicio = 0.0
        self.pausado_en = None
        self.acumulado_pausa = 0.0
        self.volumen = 100

    def nueva(self, cancion):
        self.cancion = cancion
        self.inicio = time.monotonic()
        self.pausado_en = None
        self.acumulado_pausa = 0.0

    @property
    def pausado(self): return self.pausado_en is not None

    @property
    def transcurrido(self):
        if not self.cancion: return 0.0
        fin = self.pausado_en if self.pausado else time.monotonic()
        return max(0.0, fin - self.inicio - self.acumulado_pausa)

    def pausar(self):
        if self.pausado_en is None: self.pausado_en = time.monotonic()

    def reanudar(self):
        if self.pausado_en is not None:
            self.acumulado_pausa += time.monotonic() - self.pausado_en
            self.pausado_en = None

estado = EstadoPlayer()
updater_task = None

def barra_progreso(actual, total, largo=20):
    if not total: return "🔘" + "▬" * (largo - 1)
    pos = max(0, min(largo - 1, int((actual / total) * largo)))
    return "".join("🔘" if i == pos else "▬" for i in range(largo))

def barra_volumen(vol, largo=8):
    llenos = max(0, min(largo, round((vol / 150) * largo)))
    return "█" * llenos + "░" * (largo - llenos)

def build_player_embed():
    c = estado.cancion
    if not c:
        return discord.Embed(title="⏹️ Sin música", color=discord.Color.greyple())
    total = c.get("duracion", 0)
    transcurrido = estado.transcurrido
    titulo_estado = "⏸️  En pausa" if estado.pausado else "🎵  Sonando ahora"
    color = discord.Color.dark_gray() if estado.pausado else discord.Color.from_rgb(29, 185, 84)
    embed = discord.Embed(
        title=titulo_estado,
        description=f"### [{c['titulo']}]({c['webpage']})\n*{c['uploader']}*",
        color=color,
    )
    embed.add_field(name="", value=(
        f"{barra_progreso(transcurrido, total)}\n"
        f"`{formato_duracion(transcurrido)}`  ╱  `{formato_duracion(total)}`"
    ), inline=False)
    embed.add_field(name="", value=(
        f"🔊 `{barra_volumen(estado.volumen)}` **{estado.volumen}%**"
        f"  ·  🔁 Autoplay **{'ON' if autoplay_enabled else 'OFF'}**"
        f"  ·  📋 Cola **{len(music_queue)}**"
    ), inline=False)
    if music_queue:
        proximas = "\n".join(f"`{i+1}.` {q['titulo']}" for i, q in enumerate(music_queue[:3]))
        if len(music_queue) > 3: proximas += f"\n*... y {len(music_queue)-3} más*"
        embed.add_field(name="⏭️  A continuación", value=proximas, inline=False)
    if c.get("thumbnail"): embed.set_thumbnail(url=c["thumbnail"])
    embed.set_footer(text=f"Pedido por {c.get('pedido_por', 'alguien')}")
    return embed

async def actualizar_player_loop():
    while True:
        await asyncio.sleep(7)
        if not estado.cancion or not player_message or estado.pausado: continue
        try:
            await player_message.edit(embed=build_player_embed())
        except discord.NotFound:
            break
        except Exception:
            await asyncio.sleep(3)

# ─────────────────────────────────────────
#  BOTONES DEL PLAYER
# ─────────────────────────────────────────

class MusicPlayerView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def refrescar(self, interaction):
        await interaction.response.edit_message(embed=build_player_embed(), view=self)

    @discord.ui.button(emoji="⏯️", label="Pausa", style=discord.ButtonStyle.primary, row=0)
    async def btn_playpause(self, interaction, button):
        if music_voice_client:
            if estado.pausado:
                music_voice_client.resume(); estado.reanudar()
                button.label = "Pausa"; button.style = discord.ButtonStyle.primary
            else:
                music_voice_client.pause(); estado.pausar()
                button.label = "Seguir"; button.style = discord.ButtonStyle.success
        await self.refrescar(interaction)

    @discord.ui.button(emoji="⏭️", label="Siguiente", style=discord.ButtonStyle.secondary, row=0)
    async def btn_skip(self, interaction, button):
        await interaction.response.defer()
        if music_voice_client and (music_voice_client.is_playing() or music_voice_client.is_paused()):
            if estado.pausado: estado.reanudar()
            music_voice_client.stop()

    @discord.ui.button(emoji="⏹️", label="Parar", style=discord.ButtonStyle.danger, row=0)
    async def btn_stop(self, interaction, button):
        global player_message
        music_queue.clear(); estado.cancion = None
        if music_voice_client and (music_voice_client.is_playing() or music_voice_client.is_paused()):
            music_voice_client.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(title="⏹️  Música parada",
                                description="El bot sigue en el canal. Usá `/play` para poner otra.",
                                color=discord.Color.greyple()), view=None)
        player_message = None

    @discord.ui.button(emoji="🔁", label="Autoplay", style=discord.ButtonStyle.secondary, row=0)
    async def btn_autoplay(self, interaction, button):
        global autoplay_enabled
        autoplay_enabled = not autoplay_enabled
        button.style = discord.ButtonStyle.success if autoplay_enabled else discord.ButtonStyle.secondary
        button.label = f"Autoplay {'ON' if autoplay_enabled else 'OFF'}"
        await self.refrescar(interaction)

    @discord.ui.button(emoji="🚪", label="Salir", style=discord.ButtonStyle.secondary, row=0)
    async def btn_leave(self, interaction, button):
        global music_voice_client, player_message
        music_queue.clear(); estado.cancion = None
        if music_voice_client:
            await music_voice_client.disconnect(); music_voice_client = None
        await interaction.response.edit_message(
            embed=discord.Embed(title="👋 ¡Hasta luego!", color=discord.Color.greyple()), view=None)
        player_message = None

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, row=1)
    async def btn_vol_down(self, interaction, button):
        estado.volumen = max(0, estado.volumen - 10)
        if music_voice_client and isinstance(music_voice_client.source, discord.PCMVolumeTransformer):
            music_voice_client.source.volume = estado.volumen / 100
        await self.refrescar(interaction)

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, row=1)
    async def btn_vol_up(self, interaction, button):
        estado.volumen = min(150, estado.volumen + 10)
        if music_voice_client and isinstance(music_voice_client.source, discord.PCMVolumeTransformer):
            music_voice_client.source.volume = estado.volumen / 100
        await self.refrescar(interaction)

    @discord.ui.button(emoji="❤️", label="Me gusta", style=discord.ButtonStyle.secondary, row=1)
    async def btn_like(self, interaction, button):
        c = estado.cancion
        if not c:
            await interaction.response.send_message("No hay canción sonando.", ephemeral=True); return
        if c not in liked_songs:
            liked_songs.append(c); button.style = discord.ButtonStyle.danger
            await interaction.response.send_message(f"❤️ Guardé **{c['titulo']}**.", ephemeral=True)
        else:
            liked_songs.remove(c); button.style = discord.ButtonStyle.secondary
            await interaction.response.send_message(f"💔 Saqué **{c['titulo']}**.", ephemeral=True)
        if player_message: await player_message.edit(view=self)

    @discord.ui.button(emoji="📋", label="Cola", style=discord.ButtonStyle.secondary, row=1)
    async def btn_cola(self, interaction, button):
        if not music_queue:
            await interaction.response.send_message("La cola está vacía 🫥", ephemeral=True); return
        lista = "\n".join(f"`{i+1}.` {q['titulo']}" for i, q in enumerate(music_queue[:10]))
        await interaction.response.send_message(
            embed=discord.Embed(title="📋 Próximas canciones", description=lista, color=discord.Color.blurple()),
            ephemeral=True)

    @discord.ui.button(emoji="💾", label="Likes", style=discord.ButtonStyle.secondary, row=1)
    async def btn_likes(self, interaction, button):
        if not liked_songs:
            await interaction.response.send_message("No tenés canciones guardadas.", ephemeral=True); return
        lista = "\n".join(f"`{i+1}.` [{c['titulo']}]({c['webpage']})" for i, c in enumerate(liked_songs[:10]))
        await interaction.response.send_message(
            embed=discord.Embed(title="❤️ Tus canciones", description=lista, color=discord.Color.red()),
            ephemeral=True)

    @discord.ui.button(emoji="📝", label="Letra", style=discord.ButtonStyle.secondary, row=2)
    async def btn_letra(self, interaction, button):
        c = estado.cancion
        if not c:
            await interaction.response.send_message("No hay canción sonando.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        letra = await buscar_letra(c["titulo"])
        if not letra:
            await interaction.followup.send(
                f"No encontré la letra de **{c['titulo']}** 😕", ephemeral=True)
            return
        # Discord tiene límite de 2000 chars — mandamos en partes
        titulo_cancion = c["titulo"]
        header = f"📝 **Letra de {titulo_cancion}**" + chr(10) + chr(10)
        texto = header + letra
        await interaction.followup.send(texto[:1900], ephemeral=True)

# ─────────────────────────────────────────
#  LETRAS DE CANCIONES
# ─────────────────────────────────────────

async def buscar_letra(titulo: str) -> str | None:
    """Busca la letra usando lyrics.ovh — gratuita, sin API key."""
    import re as _re
    # Limpiar el título: sacar "[Visualizer]", "(Audio)", etc.
    titulo_limpio = _re.sub(r"\[.*?\]|\(.*?\)", "", titulo).strip()
    # Separar artista y canción si hay " - "
    if " - " in titulo_limpio:
        partes = titulo_limpio.split(" - ", 1)
        artista, cancion = partes[0].strip(), partes[1].strip()
    else:
        artista, cancion = "", titulo_limpio

    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.lyrics.ovh/v1/{artista}/{cancion}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data_json = await resp.json()
                    return data_json.get("lyrics", "").strip() or None
    except Exception:
        pass

    # Segundo intento: buscar sin artista
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.lyrics.ovh/v1/_/{titulo_limpio}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data_json = await resp.json()
                    return data_json.get("lyrics", "").strip() or None
    except Exception:
        pass

    return None

# ─────────────────────────────────────────
#  REPRODUCCIÓN
# ─────────────────────────────────────────

async def reproducir_siguiente(canal_texto):
    global music_voice_client, player_message, updater_task

    if not music_queue:
        if player_message:
            try:
                await player_message.edit(
                    embed=discord.Embed(title="✅ Cola vacía",
                                        description="El bot sigue en el canal. Usá `/play` para más música.",
                                        color=discord.Color.greyple()), view=None)
            except Exception:
                pass
            player_message = None
        estado.cancion = None
        return

    if not music_voice_client or not music_voice_client.is_connected():
        return

    cancion = music_queue.pop(0)

    # Si es canción lazy de playlist, extraer URL real antes de reproducir
    if cancion.get("_lazy"):
        try:
            opts = {**YTDL_OPTIONS, "default_search": None, "noplaylist": True}
            data_yt = await asyncio.to_thread(
                yt_dlp.YoutubeDL(opts).extract_info, cancion["webpage"], download=False
            )
            cancion["url"]      = data_yt.get("url")
            cancion["duracion"] = data_yt.get("duration", 0)
            cancion["thumbnail"]= data_yt.get("thumbnail", cancion.get("thumbnail", ""))
            cancion["uploader"] = data_yt.get("uploader", cancion.get("uploader", "YouTube"))
            cancion.pop("_lazy")
        except Exception as e:
            print(f"Error extrayendo URL lazy: {e}")
            # Saltear esta canción y pasar a la siguiente
            asyncio.run_coroutine_threadsafe(reproducir_siguiente(canal_texto), bot.loop)
            return

    estado.nueva(cancion)

    # Guardar en historial para autocompletado
    if "historial_musica" not in data:
        data["historial_musica"] = []
    entrada_historial = {
        "titulo": cancion.get("titulo", ""),
        "webpage": cancion.get("webpage", ""),
        "duracion": cancion.get("duracion", 0),
    }
    if entrada_historial not in data["historial_musica"]:
        data["historial_musica"].insert(0, entrada_historial)
        data["historial_musica"] = data["historial_musica"][:200]  # máximo 200
        guardar_data()

    def after_play(error):
        if error:
            print(f"Error de reproducción: {error}")
        asyncio.run_coroutine_threadsafe(reproducir_siguiente(canal_texto), bot.loop)

    print(f"Reproduciendo: {cancion['titulo']}")
    print(f"FFmpeg: {FFMPEG_PATH}")

    source = discord.FFmpegPCMAudio(
        cancion["url"],
        executable=FFMPEG_PATH,
        **FFMPEG_OPTIONS
    )
    music_voice_client.play(
        discord.PCMVolumeTransformer(source, volume=estado.volumen / 100),
        after=after_play
    )

    view = MusicPlayerView()
    embed = build_player_embed()
    if player_message:
        try:
            await player_message.edit(embed=embed, view=view)
        except Exception:
            ct = canal_texto or bot.get_channel(CANAL_MUSICA)
            if ct: player_message = await ct.send(embed=embed, view=view)
    else:
        ct = canal_texto or bot.get_channel(CANAL_MUSICA)
        if ct: player_message = await ct.send(embed=embed, view=view)

    if updater_task: updater_task.cancel()
    updater_task = bot.loop.create_task(actualizar_player_loop())

# ─────────────────────────────────────────
#  SLASH COMMANDS DE MÚSICA
# ─────────────────────────────────────────

async def autocomplete_cancion(interaction: discord.Interaction, current: str):
    if len(current) < 2: return []
    if current in _sugerencias_cache:
        sugerencias = _sugerencias_cache[current]
    else:
        try:
            sugerencias = await asyncio.wait_for(buscar_sugerencias(current), timeout=2.0)
        except Exception:
            return []
    return [
        app_commands.Choice(
            name=s["titulo"][:100],
            value=s["titulo"][:100],  # valor = texto de búsqueda, no URL
        )
        for s in sugerencias if s.get("titulo")
    ]


# ─────────────────────────────────────────
#  SPOTIFY — importar playlist y reproducir en aleatorio
# ─────────────────────────────────────────

async def obtener_canciones_spotify(url: str) -> list[str]:
    """Extrae los nombres de canciones de una URL de Spotify usando spotdl con archivo temporal."""
    import sys, tempfile, os as _os, json as _json

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".spotdl", delete=False) as tmp:
            tmp_path = tmp.name

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "spotdl",
            "save",
            url,
            "--save-file",
            tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

        if proc.returncode != 0:
            print("SpotDL ERROR:")
            print(stderr.decode("utf-8", errors="ignore"))
            return []

        if not _os.path.isfile(tmp_path):
            print("SpotDL no creó el archivo.")
            return []

        with open(tmp_path, "r", encoding="utf-8") as f:
            contenido = f.read()

        if not contenido.strip():
            print("Archivo .spotdl vacío.")
            print(stdout.decode("utf-8", errors="ignore"))
            print(stderr.decode("utf-8", errors="ignore"))
            return []

        try:
            data = _json.loads(contenido)
        except Exception as e:
            print("No pude leer el archivo .spotdl como JSON.")
            print(e)
            print("Contenido:")
            print(contenido[:3000])
            return []

        canciones = []

        if isinstance(data, list):
            tracks = data
        elif isinstance(data, dict):
            tracks = (
                data.get("songs")
                or data.get("tracks")
                or data.get("entries")
                or []
            )
        else:
            tracks = []

        for track in tracks:
            nombre = (
                track.get("name")
                or track.get("title")
                or ""
            )

            artistas = (
                track.get("artist")
                or ", ".join(track.get("artists", []))
                or ""
            )

            if nombre:
                canciones.append(
                    f"{artistas} - {nombre}" if artistas else nombre
                )

        print(f"SpotDL encontró {len(canciones)} canciones.")
        return canciones

    except Exception as e:
        print(f"Error spotdl: {repr(e)}")
        return []

    finally:
        if tmp_path and _os.path.exists(tmp_path):
            try:
                _os.remove(tmp_path)
            except Exception:
                pass

@bot.tree.command(name="play", description="Reproducí una canción, URL o playlist de YouTube",
                  guild=discord.Object(id=GUILD_ID))
@app_commands.describe(cancion="Nombre, URL de canción o link de playlist de YouTube")
@app_commands.autocomplete(cancion=autocomplete_cancion)
async def slash_play(interaction: discord.Interaction, cancion: str):
    global music_voice_client

    if not interaction.user.voice:
        await interaction.response.send_message("❌ Entrá a un canal de voz primero.", ephemeral=True); return

    await interaction.response.defer(ephemeral=True)

    if music_voice_client and not music_voice_client.is_connected():
        try: await music_voice_client.disconnect(force=True)
        except Exception: pass
        music_voice_client = None

    if not music_voice_client:
        music_voice_client = await interaction.user.voice.channel.connect()
    elif music_voice_client.channel != interaction.user.voice.channel:
        await music_voice_client.move_to(interaction.user.voice.channel)

    # ── Detectar si es una playlist de YouTube ──
    es_playlist = cancion.startswith("http") and ("list=" in cancion or "/playlist?" in cancion)

    if es_playlist:
        await interaction.followup.send("📋 Cargando playlist de YouTube...", ephemeral=True)
        try:
            ytdl_playlist = yt_dlp.YoutubeDL({
                "extract_flat": True,
                "quiet": True,
                "no_warnings": True,
                "noplaylist": False,
            })
            data_pl = await asyncio.to_thread(ytdl_playlist.extract_info, cancion, download=False)
            entradas = data_pl.get("entries", [])
            agregadas = 0
            for entrada in entradas[:100]:  # máximo 100 canciones
                if not entrada or not entrada.get("id"):
                    continue
                info = {
                    "titulo":    entrada.get("title") or f"Video {entrada['id']}",
                    "url":       f"https://www.youtube.com/watch?v={entrada['id']}",
                    "webpage":   f"https://www.youtube.com/watch?v={entrada['id']}",
                    "duracion":  entrada.get("duration", 0),
                    "thumbnail": entrada.get("thumbnail") or entrada.get("thumbnails", [{}])[0].get("url", ""),
                    "uploader":  entrada.get("uploader") or data_pl.get("uploader", "YouTube"),
                    "pedido_por": interaction.user.display_name,
                    "_lazy": True,  # marcar para extraer URL real al reproducir
                }
                music_queue.append(info)
                agregadas += 1

            r = ranking_get(interaction.user.id)
            r["nombre"] = interaction.user.display_name
            r["musica"] += 1
            guardar_data()

            if not music_voice_client.is_playing() and not music_voice_client.is_paused():
                await reproducir_siguiente(interaction.channel)

            nombre_playlist = data_pl.get("title") or "Playlist"
            await interaction.followup.send(
                f"📋 **{nombre_playlist}** — {agregadas} canciones agregadas a la cola 🎵",
                ephemeral=True)
        except Exception as e:
            print(f"Error cargando playlist: {e}")
            await interaction.followup.send("❌ No pude cargar la playlist. Verificá el link.", ephemeral=True)
        return

    # ── Canción individual ──
    if cancion.startswith("http"):
        info = await buscar_cancion_url(cancion)
    else:
        try:
            data_yt = await asyncio.to_thread(ytdl.extract_info, cancion, download=False)
            entrada = data_yt["entries"][0] if "entries" in data_yt else data_yt
            info = {
                "titulo":    entrada.get("title"),
                "url":       entrada.get("url"),
                "webpage":   entrada.get("webpage_url"),
                "duracion":  entrada.get("duration", 0),
                "thumbnail": entrada.get("thumbnail"),
                "uploader":  entrada.get("uploader", "Desconocido"),
            }
        except Exception as e:
            print(f"Error buscando canción: {e}")
            info = None

    if not info or not info.get("url"):
        await interaction.followup.send("❌ No encontré nada 😕", ephemeral=True); return

    info["pedido_por"] = interaction.user.display_name
    r = ranking_get(interaction.user.id)
    r["nombre"] = interaction.user.display_name
    r["musica"] += 1
    guardar_data()

    music_queue.append(info)
    if not music_voice_client.is_playing() and not music_voice_client.is_paused():
        await reproducir_siguiente(interaction.channel)
        await interaction.followup.send(f"▶️ Reproduciendo **{info['titulo']}**", ephemeral=True)
    else:
        await interaction.followup.send(f"➕ Agregado a la cola: **{info['titulo']}**", ephemeral=True)

@bot.tree.command(name="shuffle", description="Mezcla aleatoriamente la cola de música",
                  guild=discord.Object(id=GUILD_ID))
async def slash_shuffle(interaction: discord.Interaction):
    if not music_queue:
        await interaction.response.send_message("La cola está vacía.", ephemeral=True)
        return
    random.shuffle(music_queue)
    await interaction.response.send_message(
        f"🔀 Cola mezclada — {len(music_queue)} canciones en orden aleatorio.", ephemeral=True)

@bot.tree.command(name="salir", description="Desconecta el bot del canal de voz",
                  guild=discord.Object(id=GUILD_ID))
async def slash_salir(interaction: discord.Interaction):
    global music_voice_client, player_message
    music_queue.clear(); estado.cancion = None
    if music_voice_client:
        await music_voice_client.disconnect(); music_voice_client = None
    if player_message:
        try:
            await player_message.edit(
                embed=discord.Embed(title="👋 ¡Hasta luego!", color=discord.Color.greyple()), view=None)
        except Exception: pass
        player_message = None
    await interaction.response.send_message("👋 Me desconecté.", ephemeral=True)

# ─────────────────────────────────────────
#  ARRANCAR
# ─────────────────────────────────────────

bot.run(DISCORD_TOKEN)