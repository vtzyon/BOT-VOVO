import os
import json
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
X_MIN, X_MAX = -4800, 4800

# ── Stockage multi-serveur ────────────────────────────────────────────────────
sessions: dict[int, dict] = {}
configs:  dict[int, dict] = {}


def default_session() -> dict:
    return {
        "active":          False,
        "start_x":         None,
        "start_z":         None,
        "direction":       None,
        "next_x":          None,
        "assignments":     {},
        "completed":       [],
        "freed_coords":    [],
        "blocked_coords":  set(),
        "blacklisted":     set(),
        "player_msgs":     {},
    }


def get_session(guild_id: int) -> dict:
    if guild_id not in sessions:
        sessions[guild_id] = default_session()
    return sessions[guild_id]


def reset_session(guild_id: int):
    sessions[guild_id] = default_session()


# ── Config par serveur ────────────────────────────────────────────────────────
def _load_configs():
    try:
        with open(CONFIG_FILE, "r") as f:
            raw = json.load(f)
        for k, v in raw.items():
            configs[int(k)] = v
    except Exception:
        pass

def save_configs():
    with open(CONFIG_FILE, "w") as f:
        json.dump({str(k): v for k, v in configs.items()}, f)

_load_configs()


def get_config(guild_id: int) -> dict:
    if guild_id not in configs:
        configs[guild_id] = {}
        env_ch = os.getenv("OUTPUT_CHANNEL_ID")
        if env_ch:
            configs[guild_id]["output_channel_id"] = int(env_ch)
    return configs[guild_id]


# ── Helpers ───────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)


def get_next_coord(guild_id: int) -> int | None:
    s = get_session(guild_id)
    d = s["direction"]
    blocked = s["blocked_coords"]
    taken = set(s["assignments"].values())

    usable = [x for x in s["freed_coords"] if x not in blocked and x not in taken]
    if usable:
        best = max(usable) if d < 0 else min(usable)
        s["freed_coords"].remove(best)
        return best

    x = s["next_x"]
    while X_MIN <= x <= X_MAX and (x in blocked or x in taken):
        x += d
    if not (X_MIN <= x <= X_MAX):
        return None
    s["next_x"] = x + d
    return x


def free_coord(guild_id: int, player_id: int, display_name: str, *, truly_done: bool = False):
    s = get_session(guild_id)
    x = s["assignments"].pop(player_id, None)
    if x is None:
        return None
    if truly_done:
        # Travail terminé → historique uniquement
        s["completed"].append((display_name, x))
    else:
        # Kicked / quitte / retiré → pool uniquement, pas dans l'historique
        if x not in s["freed_coords"] and x not in s["blocked_coords"]:
            s["freed_coords"].append(x)
    return x


async def get_output_channel(guild_id: int) -> discord.TextChannel | None:
    cfg = get_config(guild_id)
    channel_id = cfg.get("output_channel_id")
    if not channel_id:
        return None
    try:
        return await bot.fetch_channel(int(channel_id))
    except Exception:
        return None


async def edit_player_msg(guild_id: int, player_id: int, content: str, view=None) -> bool:
    s = get_session(guild_id)
    entry = s["player_msgs"].get(player_id)
    if not entry:
        return False
    channel_id, message_id = entry
    try:
        ch = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        msg = await ch.fetch_message(message_id)
        await msg.edit(content=content, view=view)
        return True
    except Exception:
        return False


# ── Étape 1 : choix X ────────────────────────────────────────────────────────
class XChoiceView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=120)
        self.guild_id = guild_id

    @discord.ui.button(label="X : +4800", style=discord.ButtonStyle.primary)
    async def x_pos(self, i: discord.Interaction, _: discord.ui.Button):
        s = get_session(self.guild_id)
        s.update(start_x=4800, direction=-100, next_x=4800)
        await i.response.edit_message(
            content="**X = +4800** ✅\nChoisissez maintenant Z :",
            view=ZChoiceView(self.guild_id)
        )

    @discord.ui.button(label="X : -4800", style=discord.ButtonStyle.primary)
    async def x_neg(self, i: discord.Interaction, _: discord.ui.Button):
        s = get_session(self.guild_id)
        s.update(start_x=-4800, direction=100, next_x=-4800)
        await i.response.edit_message(
            content="**X = -4800** ✅\nChoisissez maintenant Z :",
            view=ZChoiceView(self.guild_id)
        )


# ── Étape 2 : choix Z ────────────────────────────────────────────────────────
class ZChoiceView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=120)
        self.guild_id = guild_id

    @discord.ui.button(label="Z : +4800", style=discord.ButtonStyle.primary)
    async def z_pos(self, i: discord.Interaction, _: discord.ui.Button):
        s = get_session(self.guild_id)
        s["start_z"] = 4800
        await i.response.edit_message(
            content=f"X = **{s['start_x']}**, Z = **+4800** ✅\nSélectionnez les joueurs :",
            view=PlayerSelectView(self.guild_id)
        )

    @discord.ui.button(label="Z : -4800", style=discord.ButtonStyle.primary)
    async def z_neg(self, i: discord.Interaction, _: discord.ui.Button):
        s = get_session(self.guild_id)
        s["start_z"] = -4800
        await i.response.edit_message(
            content=f"X = **{s['start_x']}**, Z = **-4800** ✅\nSélectionnez les joueurs :",
            view=PlayerSelectView(self.guild_id)
        )


# ── Étape 3 : sélection joueurs ──────────────────────────────────────────────
class PlayerUserSelect(discord.ui.UserSelect):
    def __init__(self, guild_id: int):
        super().__init__(placeholder="Sélectionnez les joueurs...", min_values=1, max_values=25)
        self.guild_id = guild_id

    async def callback(self, i: discord.Interaction):
        s = get_session(self.guild_id)
        selected = [u for u in self.values if u.id not in s["blacklisted"]]
        if not selected:
            await i.response.send_message("Tous les joueurs sélectionnés sont bloqués.", ephemeral=True)
            return

        s.update(assignments={}, completed=[], freed_coords=[], active=True)
        z = s["start_z"]

        await i.response.edit_message(
            content=f"✅ Session démarrée — X de départ = **{s['start_x']}**, Z = **{z}**",
            view=None,
        )

        ch = await get_output_channel(self.guild_id)
        if ch is None:
            try:
                ch = await bot.fetch_channel(i.channel_id)
            except Exception:
                await i.followup.send("❌ Aucun canal configuré. Tape `/setup` dans le canal souhaité.", ephemeral=True)
                return

        for u in selected:
            x = get_next_coord(self.guild_id)
            if x is None:
                await ch.send(f"⚠️ Plus de coordonnées disponibles pour {u.mention}.")
                continue
            display = getattr(u, "display_name", u.name)
            s["assignments"][u.id] = x
            view = PlayerView(self.guild_id, u.id, display)
            try:
                msg = await ch.send(content=f"{u.mention} — **X = {x}, Z = {z}**", view=view)
                s["player_msgs"][u.id] = (ch.id, msg.id)
            except discord.Forbidden:
                await i.followup.send(f"❌ Pas la permission d'écrire dans {ch.mention}. Vérifie les permissions.", ephemeral=True)
                return


class PlayerSelectView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=120)
        self.add_item(PlayerUserSelect(guild_id))


# ── Vue joueur ────────────────────────────────────────────────────────────────
class PlayerView(discord.ui.View):
    def __init__(self, guild_id: int, player_id: int, display_name: str):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.player_id = player_id
        self.display_name = display_name

    @discord.ui.button(label="✓  Terminé – Nouvelle ligne", style=discord.ButtonStyle.success)
    async def done(self, i: discord.Interaction, _: discord.ui.Button):
        if i.user.id != self.player_id:
            await i.response.send_message("Ce bouton ne vous appartient pas.", ephemeral=True)
            return
        s = get_session(self.guild_id)
        if not s["active"]:
            await i.response.send_message("La session a été réinitialisée.", ephemeral=True)
            return

        free_coord(self.guild_id, self.player_id, self.display_name, truly_done=True)

        new_x = get_next_coord(self.guild_id)
        if new_x is None:
            await i.response.edit_message(
                content=f"{i.user.mention} — ✅ Terminé ! Plus aucune coordonnée disponible.",
                view=None,
            )
            s["player_msgs"].pop(self.player_id, None)
            return

        s["assignments"][self.player_id] = new_x
        await i.response.edit_message(
            content=f"{i.user.mention} — Nouvelle ligne : **X = {new_x}, Z = {s['start_z']}**",
            view=PlayerView(self.guild_id, self.player_id, self.display_name),
        )

    @discord.ui.button(label="✗  Quitter", style=discord.ButtonStyle.danger)
    async def quit_btn(self, i: discord.Interaction, _: discord.ui.Button):
        if i.user.id != self.player_id:
            await i.response.send_message("Ce bouton ne vous appartient pas.", ephemeral=True)
            return

        freed_x = free_coord(self.guild_id, self.player_id, self.display_name, truly_done=False)
        s = get_session(self.guild_id)
        s["player_msgs"].pop(self.player_id, None)

        note = f" (X = {freed_x} remis en pool)" if freed_x is not None else ""
        await i.response.edit_message(
            content=f"{i.user.mention} — **Vous avez quitté la session.**{note}",
            view=None,
        )


# ── Groupe /admin ─────────────────────────────────────────────────────────────
class AdminGroup(app_commands.Group):
    def __init__(self):
        super().__init__(
            name="admin",
            description="Gestion de la session (réservé aux admins)",
            default_permissions=discord.Permissions(manage_guild=True),
        )

    @app_commands.command(name="kick", description="Retirer un joueur et libérer sa coordonnée")
    async def kick(self, i: discord.Interaction, joueur: discord.Member):
        gid = i.guild_id
        s = get_session(gid)
        if joueur.id not in s["assignments"]:
            await i.response.send_message(f"{joueur.mention} n'a pas de coordonnée active.", ephemeral=True)
            return
        freed_x = free_coord(gid, joueur.id, joueur.display_name, truly_done=False)
        await edit_player_msg(gid, joueur.id,
            f"{joueur.mention} — ❌ **Retiré de la session par un admin.** (X = {freed_x} remis en pool)", view=None)
        s["player_msgs"].pop(joueur.id, None)
        await i.response.send_message(
            f"✅ {joueur.mention} retiré. X = **{freed_x}** remis dans le pool.", ephemeral=True)

    @app_commands.command(name="desassigner", description="Retirer la coordonnée d'un joueur et lui en attribuer une nouvelle")
    async def desassigner(self, i: discord.Interaction, joueur: discord.Member):
        gid = i.guild_id
        s = get_session(gid)
        if joueur.id not in s["assignments"]:
            await i.response.send_message(f"{joueur.mention} n'a pas de coordonnée active.", ephemeral=True)
            return
        display = joueur.display_name
        old_x = free_coord(gid, joueur.id, display, truly_done=False)
        new_x = get_next_coord(gid)
        if new_x is None:
            await edit_player_msg(gid, joueur.id,
                f"{joueur.mention} — ⚠️ Coordonnée X = {old_x} retirée. Plus de coordonnées disponibles.", view=None)
            await i.response.send_message(f"X = {old_x} retiré. Aucune coordonnée de remplacement.", ephemeral=True)
            return
        s["assignments"][joueur.id] = new_x
        z = s["start_z"]
        await edit_player_msg(gid, joueur.id,
            f"{joueur.mention} — 🔄 Coordonnée mise à jour : **X = {new_x}, Z = {z}**",
            view=PlayerView(gid, joueur.id, display))
        await i.response.send_message(f"✅ {joueur.mention} : X = {old_x} → **X = {new_x}**.", ephemeral=True)

    @app_commands.command(name="retirer_coord", description="Bloquer une coordonnée X (réassigne automatiquement)")
    async def retirer_coord(self, i: discord.Interaction, x: int):
        gid = i.guild_id
        s = get_session(gid)
        if x % 100 != 0 or not (X_MIN <= x <= X_MAX):
            await i.response.send_message(f"X doit être un multiple de 100 entre {X_MIN} et {X_MAX}.", ephemeral=True)
            return
        s["blocked_coords"].add(x)
        s["freed_coords"] = [fx for fx in s["freed_coords"] if fx != x]
        victim = next((pid for pid, px in s["assignments"].items() if px == x), None)
        reassign_info = ""
        if victim is not None:
            member = i.guild.get_member(victim)
            display = member.display_name if member else f"Joueur {victim}"
            s["completed"].append((display, x))
            s["assignments"].pop(victim, None)
            new_x = get_next_coord(gid)
            if new_x is not None:
                s["assignments"][victim] = new_x
                z = s["start_z"]
                await edit_player_msg(gid, victim,
                    f"{member.mention if member else display} — 🔄 X = {x} bloqué. **Nouvelle ligne : X = {new_x}, Z = {z}**",
                    view=PlayerView(gid, victim, display))
                reassign_info = f"\n🔄 {display} réassigné → X = **{new_x}**."
            else:
                if member:
                    await edit_player_msg(gid, victim,
                        f"{member.mention} — ⚠️ X = {x} bloqué. Plus de coordonnées disponibles.", view=None)
                reassign_info = f"\n⚠️ {display} n'a plus de coordonnée disponible."
        await i.response.send_message(f"🚫 X = **{x}** bloqué.{reassign_info}", ephemeral=True)

    @app_commands.command(name="ajouter_coord", description="Remettre une coordonnée X bloquée dans le pool")
    async def ajouter_coord(self, i: discord.Interaction, x: int):
        gid = i.guild_id
        s = get_session(gid)
        if x % 100 != 0 or not (X_MIN <= x <= X_MAX):
            await i.response.send_message(f"X doit être un multiple de 100 entre {X_MIN} et {X_MAX}.", ephemeral=True)
            return
        if x in s["assignments"].values():
            await i.response.send_message(f"X = **{x}** est déjà assigné à un joueur actif.", ephemeral=True)
            return
        s["blocked_coords"].discard(x)
        if x not in s["freed_coords"]:
            s["freed_coords"].append(x)
        await i.response.send_message(f"✅ X = **{x}** remis dans le pool.", ephemeral=True)

    @app_commands.command(name="ajouter", description="Ajouter un joueur à la session en cours")
    async def ajouter(self, i: discord.Interaction, joueur: discord.Member):
        gid = i.guild_id
        s = get_session(gid)
        if not s["active"]:
            await i.response.send_message("Aucune session active.", ephemeral=True)
            return
        if joueur.id in s["assignments"]:
            await i.response.send_message(f"{joueur.mention} est déjà dans la session.", ephemeral=True)
            return
        if joueur.id in s["blacklisted"]:
            await i.response.send_message(f"{joueur.mention} est bloqué.", ephemeral=True)
            return

        x = get_next_coord(gid)
        if x is None:
            await i.response.send_message("Plus aucune coordonnée disponible.", ephemeral=True)
            return

        display = joueur.display_name
        s["assignments"][joueur.id] = x
        z = s["start_z"]

        ch = await get_output_channel(gid)
        if ch is None:
            try:
                ch = await bot.fetch_channel(i.channel_id)
            except Exception:
                await i.response.send_message("❌ Canal de sortie introuvable.", ephemeral=True)
                return

        view = PlayerView(gid, joueur.id, display)
        msg = await ch.send(content=f"{joueur.mention} — **X = {x}, Z = {z}**", view=view)
        s["player_msgs"][joueur.id] = (ch.id, msg.id)
        await i.response.send_message(f"✅ {joueur.mention} ajouté → X = **{x}**.", ephemeral=True)

    @app_commands.command(name="delall", description="Supprimer toutes les coordonnées d'un joueur (actives et historique)")
    async def delall(self, i: discord.Interaction, joueur: discord.Member):
        gid = i.guild_id
        s = get_session(gid)
        display = joueur.display_name
        count = 0

        # Retirer la coordonnée active sans passer par completed
        x_actif = s["assignments"].pop(joueur.id, None)
        if x_actif is not None:
            if x_actif not in s["freed_coords"] and x_actif not in s["blocked_coords"]:
                s["freed_coords"].append(x_actif)
            await edit_player_msg(gid, joueur.id,
                f"{joueur.mention} — 🗑️ **Toutes vos coordonnées ont été supprimées par un admin.**",
                view=None)
            s["player_msgs"].pop(joueur.id, None)
            count += 1

        # Retirer l'historique et remettre ces X dans le pool
        restant = []
        for name, x in s["completed"]:
            if name == display:
                if x not in s["freed_coords"] and x not in s["blocked_coords"] and x not in s["assignments"].values():
                    s["freed_coords"].append(x)
                count += 1
            else:
                restant.append((name, x))
        s["completed"] = restant

        if count == 0:
            await i.response.send_message(f"{joueur.mention} n'a aucune coordonnée à supprimer.", ephemeral=True)
        else:
            await i.response.send_message(
                f"🗑️ **{count}** coordonnée(s) de {joueur.mention} supprimées et remises dans le pool.",
                ephemeral=True
            )

    @app_commands.command(name="bloquer", description="Empêcher un joueur d'être sélectionné")
    async def bloquer(self, i: discord.Interaction, joueur: discord.Member):
        get_session(i.guild_id)["blacklisted"].add(joueur.id)
        await i.response.send_message(f"🚫 {joueur.mention} est bloqué.", ephemeral=True)

    @app_commands.command(name="debloquer", description="Autoriser à nouveau un joueur bloqué")
    async def debloquer(self, i: discord.Interaction, joueur: discord.Member):
        get_session(i.guild_id)["blacklisted"].discard(joueur.id)
        await i.response.send_message(f"✅ {joueur.mention} débloqué.", ephemeral=True)


bot.tree.add_command(AdminGroup())


# ── Événements ────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"Bot connecté : {bot.user}  (ID : {bot.user.id})")
    print(f"Présent sur {len(bot.guilds)} serveur(s).")
    try:
        synced = await bot.tree.sync()
        print(f"{len(synced)} commande(s) synchronisée(s).")
    except Exception as e:
        print(f"Erreur sync : {e}")


# ── /setup ────────────────────────────────────────────────────────────────────
@bot.tree.command(name="setup", description="Configurer ce canal comme canal de sortie du bot")
@app_commands.default_permissions(manage_guild=True)
async def setup(i: discord.Interaction):
    get_config(i.guild_id)["output_channel_id"] = i.channel_id
    save_configs()
    await i.response.send_message(
        f"✅ Canal configuré : <#{i.channel_id}>\nTous les messages du bot iront dans ce canal.",
        ephemeral=True,
    )
    try:
        ch = await bot.fetch_channel(i.channel_id)
        await ch.send("✅ Ce canal est maintenant configuré comme canal de sortie du bot.")
    except Exception as e:
        await i.followup.send(f"⚠️ Canal sauvegardé mais impossible d'y écrire : {e}", ephemeral=True)


# ── /debut ────────────────────────────────────────────────────────────────────
@bot.tree.command(name="debut", description="Démarrer une nouvelle session de coordonnées")
@app_commands.default_permissions(manage_guild=True)
async def debut(i: discord.Interaction):
    reset_session(i.guild_id)
    await i.response.send_message(
        "**Nouvelle session**\nChoisissez la coordonnée **X** de départ :",
        view=XChoiceView(i.guild_id),
        ephemeral=True,
    )


# ── /fait ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="fait", description="Voir toutes les coordonnées X assignées et terminées")
async def fait(i: discord.Interaction):
    s = get_session(i.guild_id)
    if not s["active"]:
        await i.response.send_message("Aucune session active.", ephemeral=True)
        return

    z = s["start_z"]
    d = s["direction"]
    desc = f"+{d}" if d > 0 else str(d)
    lines = [f"**Session — Z = {z}  |  Pas : {desc}/joueur**\n"]

    if s["assignments"]:
        lines.append("**En cours :**")
        for pid, x in sorted(s["assignments"].items(), key=lambda kv: kv[1], reverse=(d < 0)):
            m = i.guild.get_member(pid)
            lines.append(f"• {m.display_name if m else pid} → X = **{x}**")
    else:
        lines.append("_Aucun joueur actif._")

    if s["completed"]:
        lines.append("\n**Terminées :**")
        for name, x in sorted(s["completed"], key=lambda t: t[1], reverse=(d < 0)):
            lines.append(f"• {name} → X = **{x}** ✅")

    if s["freed_coords"]:
        lines.append(f"\n**Pool libre :** {sorted(s['freed_coords'], reverse=(d < 0))}")
    if s["blocked_coords"]:
        lines.append(f"**Coordonnées bloquées :** {sorted(s['blocked_coords'])}")
    if s["blacklisted"]:
        names = [i.guild.get_member(pid) for pid in s["blacklisted"]]
        lines.append(f"**Joueurs bloqués :** {', '.join(m.display_name if m else str(m) for m in names)}")

    forteresses = get_config(i.guild_id).get("fortresses", [])
    if forteresses:
        lines.append("\n**🏰 Forteresses :**")
        for idx, f in enumerate(forteresses, 1):
            lines.append(f"　**{idx}.** {f.get('nom')} — X = **{f['x']}**, Z = **{f['z']}**")

    await i.response.send_message("\n".join(lines))


# ── /fin ──────────────────────────────────────────────────────────────────────
@bot.tree.command(name="fin", description="Terminer la session en cours et afficher le bilan final")
@app_commands.default_permissions(manage_guild=True)
async def fin(i: discord.Interaction):
    gid = i.guild_id
    s = get_session(gid)
    if not s["active"]:
        await i.response.send_message("Aucune session active.", ephemeral=True)
        return

    z = s["start_z"]
    d = s["direction"]
    desc = f"+{d}" if d > 0 else str(d)
    lines = [f"**Session terminée — Z = {z}  |  Pas : {desc}/joueur**\n"]

    for pid, x in list(s["assignments"].items()):
        m = i.guild.get_member(pid)
        name = m.display_name if m else f"Joueur {pid}"
        s["completed"].append((name, x))
        await edit_player_msg(gid, pid,
            f"{m.mention if m else name} — 🔴 **Session terminée par un admin.**", view=None)

    s["assignments"] = {}
    s["active"] = False

    if s["completed"]:
        lines.append("**Bilan des coordonnées :**")
        for name, x in sorted(s["completed"], key=lambda t: t[1], reverse=(d < 0)):
            lines.append(f"• {name} → X = **{x}** ✅")
    else:
        lines.append("_Aucune coordonnée n'a été complétée._")

    ch = await get_output_channel(gid) or await bot.fetch_channel(i.channel_id)
    await i.response.send_message("✅ Session terminée.", ephemeral=True)
    await ch.send("\n".join(lines))


# ── /fort ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="fort", description="Enregistrer une forteresse avec ses coordonnées")
async def fort(i: discord.Interaction, x: int, z: int, nom: str = ""):
    cfg = get_config(i.guild_id)
    cfg.setdefault("fortresses", [])
    entry = {"x": x, "z": z, "nom": nom or f"Forteresse {len(cfg['fortresses']) + 1}", "auteur": i.user.display_name}
    cfg["fortresses"].append(entry)
    save_configs()
    await i.response.send_message(
        f"🏰 **{entry['nom']}** enregistrée — X = **{x}**, Z = **{z}** (par {i.user.mention})"
    )


@bot.tree.command(name="forts", description="Voir toutes les forteresses enregistrées")
async def forts(i: discord.Interaction):
    cfg = get_config(i.guild_id)
    liste = cfg.get("fortresses", [])
    if not liste:
        await i.response.send_message("Aucune forteresse enregistrée.", ephemeral=True)
        return
    lines = ["**🏰 Forteresses enregistrées :**"]
    for idx, f in enumerate(liste, 1):
        nom = f.get("nom", f"Forteresse {idx}")
        lines.append(f"**{idx}.** {nom} — X = **{f['x']}**, Z = **{f['z']}** *(par {f.get('auteur', '?')})*")
    await i.response.send_message("\n".join(lines))


@bot.tree.command(name="fort_supprimer", description="Supprimer une forteresse par son numéro (voir /forts)")
@app_commands.default_permissions(manage_guild=True)
async def fort_supprimer(i: discord.Interaction, numero: int):
    cfg = get_config(i.guild_id)
    liste = cfg.get("fortresses", [])
    if numero < 1 or numero > len(liste):
        await i.response.send_message(f"Numéro invalide. Il y a {len(liste)} forteresse(s).", ephemeral=True)
        return
    removed = liste.pop(numero - 1)
    save_configs()
    await i.response.send_message(f"🗑️ **{removed['nom']}** supprimée.", ephemeral=True)


# ── /aide ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="aide", description="Afficher l'explication complète du bot")
async def aide(i: discord.Interaction):
    embed = discord.Embed(title="📋 Guide du bot de coordonnées", color=discord.Color.blurple())
    embed.add_field(name="⚙️ Configuration (admin)", value=(
        "`/setup` — À faire **une seule fois** dans le canal où tu veux que le bot écrive.\n"
        "`/debut` — Lance une nouvelle session. Le bot te demande :\n"
        "　• La coordonnée **X** de départ : **+4800** ou **-4800**\n"
        "　• La coordonnée **Z** : **+4800** ou **-4800**\n"
        "　• Les **joueurs** à inclure (sélection depuis la liste)\n"
        "　→ Chaque joueur reçoit une ligne avec ses coordonnées."
    ), inline=False)
    embed.add_field(name="🎮 Comment ça marche", value=(
        "Chaque joueur reçoit un message avec sa coordonnée **X** et **Z**.\n"
        "　• Départ **+4800** → 4800, 4700, 4600… (−100 par joueur)\n"
        "　• Départ **−4800** → −4800, −4700, −4600… (+100 par joueur)\n\n"
        "Chaque message joueur contient **2 boutons** :\n"
        "　🟢 **✓ Terminé – Nouvelle ligne** : le bot t'attribue une nouvelle coordonnée\n"
        "　🔴 **✗ Quitter** : tu quittes, ta coordonnée est libérée pour un autre"
    ), inline=False)
    embed.add_field(name="📊 Informations", value=(
        "`/fait` — Affiche les coordonnées en cours, terminées et les forteresses\n"
        "`/fin` *(admin)* — Termine la session et affiche le bilan final"
    ), inline=False)
    embed.add_field(name="🏰 Forteresses", value=(
        "`/fort x: z:` — Enregistre une forteresse (paramètre `nom:` optionnel)\n"
        "`/forts` — Liste toutes les forteresses enregistrées\n"
        "`/fort_supprimer numéro:` *(admin)* — Supprime une forteresse par son numéro"
    ), inline=False)
    embed.add_field(name="🛡️ Commandes admin (`/admin`)", value=(
        "`/admin ajouter @joueur` — Ajoute un joueur à la session en cours\n"
        "`/admin kick @joueur` — Retire un joueur, sa coordonnée revient dans le pool\n"
        "`/admin desassigner @joueur` — Lui retire sa coordonnée et lui en donne une nouvelle\n"
        "`/admin delall @joueur` — Supprime **toutes** les coordonnées d'un joueur (actives + historique)\n"
        "`/admin retirer_coord X` — Bloque une coordonnée X (réassigne automatiquement)\n"
        "`/admin ajouter_coord X` — Remet une coordonnée X bloquée dans le pool\n"
        "`/admin bloquer @joueur` — Interdit à ce joueur d'être sélectionné\n"
        "`/admin debloquer @joueur` — Retire l'interdiction"
    ), inline=False)
    embed.set_footer(text="Coordonnées X entre −4800 et +4800 • Pas de 100 entre chaque joueur")
    await i.response.send_message(embed=embed)


# ── Lancement ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN:
        raise ValueError("DISCORD_TOKEN manquant. Sur Railway : ajoute DISCORD_TOKEN dans Variables.")
    bot.run(TOKEN)
