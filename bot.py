"""
hikari | Racing
Bot com comandos de barra (/) que gera um link do Discord (cdn.discordapp.com)
a partir de um link de música ou de um arquivo de áudio.

Comandos:
  /musica link:<url>      -> baixa a música do link e devolve o link do Discord
  /arquivo audio:<anexo>  -> pega o arquivo de áudio enviado e devolve o link do Discord
  /ajuda                  -> mostra como usar

Configure tudo no arquivo .env. Não precisa mexer neste código.
"""
import asyncio
import io
import logging
import os
import re
import tempfile

import aiohttp
import discord
import yt_dlp
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

# ----------------------- CONFIGURAÇÃO (vem do .env) -----------------------
TOKEN = os.getenv("DISCORD_TOKEN")
CANAL_ARMAZENAMENTO_ID = int(os.getenv("CANAL_ARMAZENAMENTO_ID", "0"))  # canal privado onde os mp3 ficam guardados
CANAL_COMANDO_ID = int(os.getenv("CANAL_COMANDO_ID", "0"))              # só aceita comandos nesse canal (0 = qualquer canal)
GUILD_ID = int(os.getenv("GUILD_ID", "0"))                              # ID do seu servidor (comandos aparecem na hora)
LIMITE_MB = int(os.getenv("LIMITE_MB", "10"))                           # limite de upload do servidor
DURACAO_MAX_MIN = int(os.getenv("DURACAO_MAX_MIN", "20"))               # recusa músicas maiores que isso
# --------------------------------------------------------------------------

EXT_OK = (".mp3", ".ogg", ".wav", ".m4a", ".flac")
URL_RE = re.compile(r"^https?://\S+$")
LIMITE_BYTES = LIMITE_MB * 1024 * 1024
SEM = asyncio.Semaphore(2)  # no máximo 2 downloads ao mesmo tempo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hikari")


class Hikari(discord.Client):
    def __init__(self):
        # comandos de barra não precisam do Message Content Intent
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)   # aparece na hora no seu servidor
        else:
            await self.tree.sync()              # global: pode levar até 1 hora para aparecer


client = Hikari()


# ----------------------------- DOWNLOADS ---------------------------------
def baixar_com_ytdlp(url: str) -> tuple[bytes, str]:
    """Baixa o áudio de YouTube e outros sites e converte para mp3.
    Tenta 128k e, se passar do limite, refaz em 64k."""
    with tempfile.TemporaryDirectory() as pasta:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        duracao = info.get("duration") or 0
        if duracao and duracao > DURACAO_MAX_MIN * 60:
            raise ValueError(f"Música muito longa (máx. {DURACAO_MAX_MIN} min).")

        for qualidade in ("128", "64"):
            opcoes = {
                "format": "bestaudio/best",
                "outtmpl": os.path.join(pasta, f"musica_{qualidade}.%(ext)s"),
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": qualidade,
                }],
            }
            with yt_dlp.YoutubeDL(opcoes) as ydl:
                ydl.download([url])

            arquivo = os.path.join(pasta, f"musica_{qualidade}.mp3")
            if os.path.getsize(arquivo) <= LIMITE_BYTES:
                titulo = re.sub(r"[^\w\- ]", "", info.get("title", "musica"))[:60].strip() or "musica"
                with open(arquivo, "rb") as f:
                    return f.read(), f"{titulo}.mp3"

        raise ValueError(f"Mesmo em 64k o arquivo passou de {LIMITE_MB}MB. Use uma música mais curta.")


async def baixar_direto(url: str) -> tuple[bytes, str]:
    """Baixa um link direto de arquivo de áudio."""
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            if r.status != 200:
                raise ValueError(f"Erro ao baixar (HTTP {r.status}).")
            dados = await r.content.read(LIMITE_BYTES + 1)
    if len(dados) > LIMITE_BYTES:
        raise ValueError(f"O arquivo passou de {LIMITE_MB}MB.")
    nome = url.split("?")[0].split("/")[-1] or "musica.mp3"
    return dados, nome


async def gerar_link(dados: bytes, nome: str, autor: discord.abc.User) -> str:
    """Envia o arquivo para o canal de armazenamento (visível aos jogadores)
    e devolve o link do Discord. A mensagem fica com título, quem adicionou e o link."""
    if len(dados) > LIMITE_BYTES:
        raise ValueError(f"O arquivo passou de {LIMITE_MB}MB.")
    canal = client.get_channel(CANAL_ARMAZENAMENTO_ID) or await client.fetch_channel(CANAL_ARMAZENAMENTO_ID)

    titulo = os.path.splitext(nome)[0].replace("_", " ").strip() or "Música"
    sem_ping = discord.AllowedMentions.none()  # cita o autor sem notificar ninguém

    msg = await canal.send(
        content=f"🎵 **{titulo}**\n👤 Adicionada por {autor.mention}",
        file=discord.File(io.BytesIO(dados), filename=nome),
        allowed_mentions=sem_ping,
    )
    url = msg.attachments[0].url
    # coloca o link copiável na própria mensagem, para os jogadores pegarem direto do canal
    await msg.edit(
        content=f"🎵 **{titulo}**\n👤 Adicionada por {autor.mention}\n🔗 ```{url}```",
        allowed_mentions=sem_ping,
    )
    return url


def texto_erro(e: Exception) -> str:
    t = str(e)
    if "ffmpeg" in t.lower() or "ffprobe" in t.lower():
        return "ffmpeg não encontrado. Instale o ffmpeg e coloque no PATH."
    if "Missing Access" in t or "Missing Permissions" in t:
        return "Não tenho permissão no canal de armazenamento. Dê ao hikari: Ver Canal, Enviar Mensagens e Anexar Arquivos."
    return t[:300]


async def canal_permitido(inter: discord.Interaction) -> bool:
    if CANAL_COMANDO_ID and inter.channel_id != CANAL_COMANDO_ID:
        await inter.response.send_message(f"Use este comando em <#{CANAL_COMANDO_ID}>.", ephemeral=True)
        return False
    return True


# ------------------------------ COMANDOS ----------------------------------
@client.tree.command(name="musica", description="Gera um link do Discord a partir do link de uma música")
@app_commands.describe(
    link="Link da música (YouTube etc.) ou link direto de um arquivo de áudio",
    publico="Mostrar a resposta para todos no canal (padrão: só você vê)",
)
async def musica(inter: discord.Interaction, link: str, publico: bool = False):
    if not await canal_permitido(inter):
        return
    await inter.response.defer(ephemeral=not publico, thinking=True)

    url = link.strip().strip("<>")
    if not URL_RE.match(url):
        return await inter.followup.send("Isso não parece um link válido. Cole o link completo, começando com https://")

    try:
        async with SEM:
            if url.lower().split("?")[0].endswith(EXT_OK):
                dados, nome = await baixar_direto(url)
            else:
                dados, nome = await asyncio.to_thread(baixar_com_ytdlp, url)
            novo = await gerar_link(dados, nome, inter.user)
        await inter.followup.send(f"✅ Pronto! Copie o link:\n```{novo}```")
    except Exception as e:
        log.exception("Falha em /musica (%s)", url[:100])
        await inter.followup.send(f"❌ Erro: {texto_erro(e)}")


@client.tree.command(name="arquivo", description="Gera um link do Discord a partir de um arquivo de áudio")
@app_commands.describe(
    audio="Arquivo de áudio (mp3, ogg, wav, m4a, flac)",
    publico="Mostrar a resposta para todos no canal (padrão: só você vê)",
)
async def arquivo(inter: discord.Interaction, audio: discord.Attachment, publico: bool = False):
    if not await canal_permitido(inter):
        return
    await inter.response.defer(ephemeral=not publico, thinking=True)

    if not audio.filename.lower().endswith(EXT_OK):
        return await inter.followup.send("Formato não suportado. Use mp3, ogg, wav, m4a ou flac.")
    if audio.size > LIMITE_BYTES:
        return await inter.followup.send(f"O arquivo passou de {LIMITE_MB}MB.")

    try:
        novo = await gerar_link(await audio.read(), audio.filename, inter.user)
        await inter.followup.send(f"✅ Pronto! Copie o link:\n```{novo}```")
    except Exception as e:
        log.exception("Falha em /arquivo")
        await inter.followup.send(f"❌ Erro: {texto_erro(e)}")


@client.tree.command(name="ajuda", description="Como usar o Hikari")
async def ajuda(inter: discord.Interaction):
    await inter.response.send_message(
        "**Hikari | O melhor bot de musica da racing** 🎵\n"
        "`/musica` — cola o link da música e eu gero o link do Discord\n"
        "`/arquivo` — envia o arquivo de áudio e eu gero o link do Discord\n\n"
        f"Limite: {LIMITE_MB}MB por música. Se o link parar de tocar depois de um tempo, gere outro.",
        ephemeral=True,
    )


@client.event
async def on_ready():
    await client.change_presence(activity=discord.Activity(
        type=discord.ActivityType.listening, name="🏁 Racing a #top1 • /musica"))
    log.info("hikari online como %s", client.user)


if not TOKEN:
    raise SystemExit("Falta o DISCORD_TOKEN no arquivo .env")
if not CANAL_ARMAZENAMENTO_ID:
    raise SystemExit("Falta o CANAL_ARMAZENAMENTO_ID no arquivo .env")

client.run(TOKEN)