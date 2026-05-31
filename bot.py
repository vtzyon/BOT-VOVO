import os
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")

def load_config() -> dict:
    cfg = {}
    try:
        with open(CONFIG_FILE, "r") as f:
            import json
            cfg = json.load(f)
    except Exception:
        pass
    # Variable d'environnement Railway en priorité
    env_ch = os.getenv("OUTPUT_CHANNEL_ID")
    if env_ch:
        cfg["output_channel_id"] = int(env_ch)
    return cfg

def save_config(cfg: dict):
    import json
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f)

config = load_config()

async def get_output_channel(guild: discord.Guild) -> discord.TextChannel | None:
    channel_id = config.get("output_channel_id")
    if not channel_id:
        return None
    try:
        return await bot.fetch_channel(int(channel_id))
    except Exception:
        return None

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

X_MIN, X_MAX = -4800, 4800

# ── Session globale ──────────────────────────────────────────────────────────
session: dict = {
    "active":          False,
    "start_x":         None,
    "start_z":         None,
    "direction":       None,    # -100 (départ +4800) | +100 (départ -4800)
    "next_x":          None,    # prochain X séquentiel
    "assignments":     {},      # player_id -> x
    "completed":       [],      # [(display_name, x)]
    "freed_coords":    [],      # X libérés (quittés/virés), prioritaires
    "blocked_coords":  set(),   # X bloqués par admin, jamais assignés
    "blacklisted":     set(),   # player_id bannis de la sélection
    "player_msgs":     {},      # player_id -> (channel_id, message_id)
}


def reset_session():
    session.update({
        "active": False, "start_x": None, "start_z": None,
        "direction": None, "next_x": None, "assignments": {},
        "completed": [], "freed_coords": [], "blocked_coords": set(),
        "blacklisted": set(), "player_msgs": {},
    })


def get_next_coord() -> int | None:
    """Retourne le prochain X dispo : freed pool en priorité, puis séquentiel.
    Retourne None si plus aucune place entre ±4800."""
    d = session["direction"]
    blocked = session["blocked_coords"]
    taken = set(session["assignments"].values())

    # Freed pool en priorité (hors bloqués et hors déjà assignés)
    usable = [x for x in session["freed_coords"] if x not in blocked and x not in taken]
    if usable:
        best = max(usable) if d < 0 else min(usable)
        session["freed_coords"].remove(best)
        return best

    # Séquentiel : avancer next_x en sautant les bloqués et les pris
    x = session["next_x"]
    while X_MIN <= x <= X_MAX and (x in blocked or x in taken):
        x += d
    if not (X_MIN <= x <= X_MAX):
        return None
    session["next_x"] = x + d
    return x


def free_coord(player_id: int, display_name: str, *, truly_done: bool = False):
    """Retire un joueur de assignments, l'archive dans completed.
    Si truly_done=False, le X est remis dans freed_coords pour réassignement."""
    x = session["assignments"].pop(player_id, None)
    if x is None:
        return None
    session["completed"].append((display_name, x))
    if not truly_done and x not in session["freed_coords"] and x not in session["blocked_coords"]:
        session["freed_coords"].append(x)
    return x


async def edit_player_msg(
    guild: discord.Guild,
    player_id: int,
    content: str,
    view: discord.ui.View | None = None,
) -> bool:
    """Édite le message discord du joueur. Retourne True si réussi."""
    entry = session["player_msgs"].get(player_id)
    if not entry:
        return False
    channel_id, message_id = entry
    ch = guild.get_channel(channel_id)
    if not ch:
        return False
    try:
        msg = await ch.fetch_message(message_id)
        await msg.edit(content=content, view=view)
        return True
    except Exception:
        return False


# ── Étape 1 : choix X ────────────────────────────────────────────────────────
class XChoiceView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="X : +4800", style=discord.ButtonStyle.primary)
    async def x_pos(self, i: discord.Interaction, _: discord.ui.Button):
        session.update(start_x=4800, direction=-100, next_x=4800)
        await i.response.edit_message(
            content="**X = +4800** ✅\nChoisissez maintenant Z :", view=ZChoiceView()
        )

    @discord.ui.button(label="X : -4800", style=discord.ButtonStyle.primary)
    async def x_neg(self, i: discord.Interaction, _: discord.ui.Button):
        session.update(start_x=-4800, direction=100, next_x=-4800)
        await i.response.edit_message(
            content="**X = -4800** ✅\nChoisissez maintenant Z :", view=ZChoiceView()
        )


# ── Étape 2 : choix Z ────────────────────────────────────────────────────────
class ZChoiceView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Z : +4800", style=discord.ButtonStyle.primary)
    async def z_pos(self, i: discord.Interaction, _: discord.ui.Button):
        session["start_z"] = 4800
        await i.response.edit_message(
            content=f"X = **{session['start_x']}**, Z = **+4800** ✅\nSélectionnez les joueurs :",
            view=PlayerSelectView(),
        )

    @discord.ui.button(label="Z : -4800", style=discord.ButtonStyle.primary)
    async def z_neg(self, i: discord.Interaction, _: discord.ui.Button):
        session["start_z"] = -4800
        await i.response.edit_message(
            content=f"X = **{session['start_x']}**, Z = **-4800** ✅\nSélectionnez les joueurs :",
            view=PlayerSelectView(),
        )


# ── Étape 3 : sélection joueurs ──────────────────────────────────────────────
class PlayerUserSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Sélectionnez les joueurs...", min_values=1, max_values=25)

    async def callback(self, i: discord.Interaction):
        selected = [u for u in self.values if u.id not in session["blacklisted"]]
        if not selected:
            await i.response.send_message(
                "Tous les joueurs sélectionnés sont bloqués.", ephemeral=True
            )
            return

        session.update(assignments={}, completed=[], freed_coords=[], active=True)
        z = session["start_z"]
        d = session["direction"]

        await i.response.edit_message(
            content=f"✅ Session démarrée — X de départ = **{session['start_x']}**, Z = **{z}**",
            view=None,
        )

        ch = await get_output_channel(i.guild) or await i.guild.fetch_channel(i.channel_id)
        if ch is None:
            await i.followup.send(
                "❌ Aucun canal configuré. Tape `/setup` dans le canal souhaité d'abord.",
                ephemeral=True,
            )
            return
        for u in selected:
            x = get_next_coord()
            if x is None:
                await ch.send(f"⚠️ Plus de coordonnées disponibles pour {u.mention}.")
                continue
            display = getattr(u, "display_name", u.name)
            session["assignments"][u.id] = x
            view = PlayerView(u.id, display)
            try:
                msg = await ch.send(content=f"{u.mention} — **X = {x}, Z = {z}**", view=view)
                session["player_msgs"][u.id] = (ch.id, msg.id)
            except discord.Forbidden:
                await i.followup.send(
                    f"❌ Le bot n'a pas la permission d'écrire dans {ch.mention}. Vérifie les permissions du canal.",
                    ephemeral=True,
                )
                return
            except Exception as e:
                await i.followup.send(f"❌ Erreur : {e}", ephemeral=True)
                return


class PlayerSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(PlayerUserSelect())


# ── Vue joueur ────────────────────────────────────────────────────────────────
class PlayerView(discord.ui.View):
    def __init__(self, player_id: int, display_name: str):
        super().__init__(timeout=None)
        self.player_id = player_id
        self.display_name = display_name

    @discord.ui.button(label="✓  Terminé – Nouvelle ligne", style=discord.ButtonStyle.success)
    async def done(self, i: discord.Interaction, _: discord.ui.Button):
        if i.user.id != self.player_id:
            await i.response.send_message("Ce bouton ne vous appartient pas.", ephemeral=True)
            return
        if not session["active"]:
            await i.response.send_message("La session a été réinitialisée.", ephemeral=True)
            return

        # Archiver l'ancienne coordonnée comme VRAIMENT terminée (ne revient pas dans le pool)
        old_x = free_coord(self.player_id, self.display_name, truly_done=True)

        new_x = get_next_coord()
        if new_x is None:
            await i.response.edit_message(
                content=f"{i.user.mention} — ✅ Terminé ! Plus aucune coordonnée disponible.",
                view=None,
            )
            session["player_msgs"].pop(self.player_id, None)
            return

        session["assignments"][self.player_id] = new_x
        await i.response.edit_message(
            content=f"{i.user.mention} — Nouvelle ligne : **X = {new_x}, Z = {session['start_z']}**",
            view=PlayerView(self.player_id, self.display_name),
        )

    @discord.ui.button(label="✗  Quitter", style=discord.ButtonStyle.danger)
    async def quit_btn(self, i: discord.Interaction, _: discord.ui.Button):
        if i.user.id != self.player_id:
            await i.response.send_message("Ce bouton ne vous appartient pas.", ephemeral=True)
            return

        # Le X est libéré → retourne dans le pool pour d'autres
        freed_x = free_coord(self.player_id, self.display_name, truly_done=False)
        session["player_msgs"].pop(self.player_id, None)

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

    # ── kick ─────────────────────────────────────────────────────────────────
    @app_commands.command(name="kick", description="Retirer un joueur et libérer sa coordonnée pour les autres")
    async def kick(self, i: discord.Interaction, joueur: discord.Member):
        if joueur.id not in session["assignments"]:
            await i.response.send_message(
                f"{joueur.mention} n'a pas de coordonnée active.", ephemeral=True
            )
            return

        freed_x = free_coord(joueur.id, joueur.display_name, truly_done=False)
        await edit_player_msg(
            i.guild, joueur.id,
            f"{joueur.mention} — ❌ **Retiré de la session par un admin.** (X = {freed_x} remis en pool)",
            view=None,
        )
        session["player_msgs"].pop(joueur.id, None)

        await i.response.send_message(
            f"✅ {joueur.mention} retiré. X = **{freed_x}** remis dans le pool → attribué au prochain ✓.",
            ephemeral=True,
        )

    # ── desassigner ──────────────────────────────────────────────────────────
    @app_commands.command(
        name="desassigner",
        description="Retirer la coordonnée d'un joueur et lui en attribuer une nouvelle immédiatement",
    )
    async def desassigner(self, i: discord.Interaction, joueur: discord.Member):
        if joueur.id not in session["assignments"]:
            await i.response.send_message(
                f"{joueur.mention} n'a pas de coordonnée active.", ephemeral=True
            )
            return

        display = joueur.display_name
        old_x = free_coord(joueur.id, display, truly_done=False)

        new_x = get_next_coord()
        if new_x is None:
            await edit_player_msg(
                i.guild, joueur.id,
                f"{joueur.mention} — ⚠️ Coordonnée X = {old_x} retirée. Plus de coordonnées disponibles.",
                view=None,
            )
            await i.response.send_message(
                f"X = {old_x} retiré de {joueur.mention}. Aucune coordonnée de remplacement disponible.",
                ephemeral=True,
            )
            return

        session["assignments"][joueur.id] = new_x
        z = session["start_z"]
        await edit_player_msg(
            i.guild, joueur.id,
            f"{joueur.mention} — 🔄 Coordonnée mise à jour par un admin : **X = {new_x}, Z = {z}**",
            view=PlayerView(joueur.id, display),
        )
        await i.response.send_message(
            f"✅ {joueur.mention} : X = {old_x} → **X = {new_x}**.", ephemeral=True
        )

    # ── retirer_coord ─────────────────────────────────────────────────────────
    @app_commands.command(
        name="retirer_coord",
        description="Bloquer une coordonnée X (et réassigner automatiquement si quelqu'un l'avait)",
    )
    async def retirer_coord(self, i: discord.Interaction, x: int):
        if x % 100 != 0 or not (X_MIN <= x <= X_MAX):
            await i.response.send_message(
                f"X doit être un multiple de 100 entre {X_MIN} et {X_MAX}.", ephemeral=True
            )
            return

        session["blocked_coords"].add(x)
        session["freed_coords"] = [fx for fx in session["freed_coords"] if fx != x]

        # Réassigner le joueur qui possédait ce X
        victim_pid = next((pid for pid, px in session["assignments"].items() if px == x), None)
        reassign_info = ""

        if victim_pid is not None:
            member = i.guild.get_member(victim_pid)
            display = member.display_name if member else f"Joueur {victim_pid}"
            session["completed"].append((display, x))
            session["assignments"].pop(victim_pid, None)

            new_x = get_next_coord()
            if new_x is not None:
                session["assignments"][victim_pid] = new_x
                z = session["start_z"]
                await edit_player_msg(
                    i.guild, victim_pid,
                    f"{member.mention if member else display} — 🔄 Coordonnée X = {x} bloquée par admin. "
                    f"**Nouvelle ligne : X = {new_x}, Z = {z}**",
                    view=PlayerView(victim_pid, display),
                )
                reassign_info = f"\n🔄 {display} réassigné → X = **{new_x}**."
            else:
                if member:
                    await edit_player_msg(
                        i.guild, victim_pid,
                        f"{member.mention} — ⚠️ X = {x} bloqué. Plus de coordonnées disponibles.",
                        view=None,
                    )
                reassign_info = f"\n⚠️ {display} n'a plus de coordonnée disponible."

        await i.response.send_message(
            f"🚫 X = **{x}** bloqué (ne sera plus assigné).{reassign_info}", ephemeral=True
        )

    # ── ajouter_coord ─────────────────────────────────────────────────────────
    @app_commands.command(
        name="ajouter_coord",
        description="Remettre une coordonnée X bloquée dans le pool disponible",
    )
    async def ajouter_coord(self, i: discord.Interaction, x: int):
        if x % 100 != 0 or not (X_MIN <= x <= X_MAX):
            await i.response.send_message(
                f"X doit être un multiple de 100 entre {X_MIN} et {X_MAX}.", ephemeral=True
            )
            return

        if x in session["assignments"].values():
            await i.response.send_message(
                f"X = **{x}** est déjà assigné à un joueur actif.", ephemeral=True
            )
            return

        session["blocked_coords"].discard(x)
        if x not in session["freed_coords"]:
            session["freed_coords"].append(x)

        await i.response.send_message(
            f"✅ X = **{x}** remis dans le pool. Sera attribué au prochain ✓.", ephemeral=True
        )

    # ── bloquer ───────────────────────────────────────────────────────────────
    @app_commands.command(
        name="bloquer",
        description="Empêcher un joueur d'être sélectionné dans les prochaines sessions",
    )
    async def bloquer(self, i: discord.Interaction, joueur: discord.Member):
        session["blacklisted"].add(joueur.id)
        await i.response.send_message(
            f"🚫 {joueur.mention} est bloqué et ne peut plus être sélectionné.", ephemeral=True
        )

    # ── debloquer ─────────────────────────────────────────────────────────────
    @app_commands.command(name="debloquer", description="Autoriser à nouveau un joueur bloqué")
    async def debloquer(self, i: discord.Interaction, joueur: discord.Member):
        session["blacklisted"].discard(joueur.id)
        await i.response.send_message(
            f"✅ {joueur.mention} débloqué et peut à nouveau être sélectionné.", ephemeral=True
        )


# ── Enregistrement du groupe admin ───────────────────────────────────────────
bot.tree.add_command(AdminGroup())


# ── Événements ────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"Bot connecté : {bot.user}  (ID : {bot.user.id})")
    for guild in bot.guilds:
        ch = await get_output_channel(guild)
        if ch:
            print(f"Canal de sortie : #{ch.name} dans '{guild.name}'")
        else:
            print(f"ATTENTION : aucun canal configuré dans '{guild.name}' — utilise /setup pour en choisir un.")
    try:
        synced = await bot.tree.sync()
        print(f"{len(synced)} commande(s) synchronisée(s) globalement.")
    except Exception as e:
        print(f"Erreur sync : {e}")


@bot.event
async def on_guild_available(guild: discord.Guild):
    try:
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        ch = await get_output_channel(guild)
        canal_info = f"#{ch.name}" if ch else "aucun canal configuré (utilise /setup)"
        print(f"Serveur '{guild.name}' : {len(synced)} commande(s) syncées | Canal : {canal_info}")
    except Exception as e:
        print(f"Erreur sync sur '{guild.name}' : {e}")


# ── /setup ───────────────────────────────────────────────────────────────────
@bot.tree.command(name="setup", description="Configurer ce canal comme canal de sortie du bot")
@app_commands.default_permissions(manage_guild=True)
async def setup(i: discord.Interaction):
    config["output_channel_id"] = i.channel_id
    save_config(config)
    await i.response.send_message(
        f"✅ Canal configuré ! (<#{i.channel_id}>)\nTous les messages du bot iront dans ce canal.",
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
    reset_session()
    await i.response.send_message(
        "**Nouvelle session**\nChoisissez la coordonnée **X** de départ :",
        view=XChoiceView(),
        ephemeral=True,
    )


# ── /fait ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="fait", description="Voir toutes les coordonnées X assignées et terminées")
async def fait(i: discord.Interaction):
    if not session["active"]:
        await i.response.send_message("Aucune session active.", ephemeral=True)
        return

    z = session["start_z"]
    d = session["direction"]
    desc = f"+{d}" if d > 0 else str(d)
    lines = [f"**Session — Z = {z}  |  Pas : {desc}/joueur**\n"]

    if session["assignments"]:
        lines.append("**En cours :**")
        for pid, x in sorted(session["assignments"].items(), key=lambda kv: kv[1], reverse=(d < 0)):
            m = i.guild.get_member(pid)
            name = m.display_name if m else f"Joueur {pid}"
            lines.append(f"• {name} → X = **{x}**")
    else:
        lines.append("_Aucun joueur actif._")

    if session["completed"]:
        lines.append("\n**Terminées :**")
        for name, x in sorted(session["completed"], key=lambda t: t[1], reverse=(d < 0)):
            lines.append(f"• {name} → X = **{x}** ✅")

    if session["freed_coords"]:
        freed_sorted = sorted(session["freed_coords"], reverse=(d < 0))
        lines.append(f"\n**Pool libre (disponibles) :** {freed_sorted}")

    if session["blocked_coords"]:
        lines.append(f"**Coordonnées bloquées :** {sorted(session['blocked_coords'])}")

    if session["blacklisted"]:
        names = []
        for pid in session["blacklisted"]:
            m = i.guild.get_member(pid)
            names.append(m.display_name if m else str(pid))
        lines.append(f"**Joueurs bloqués :** {', '.join(names)}")

    await i.response.send_message("\n".join(lines))


# ── /fin ──────────────────────────────────────────────────────────────────────
@bot.tree.command(name="fin", description="Terminer la session en cours et afficher le bilan final")
@app_commands.default_permissions(manage_guild=True)
async def fin(i: discord.Interaction):
    if not session["active"]:
        await i.response.send_message("Aucune session active.", ephemeral=True)
        return

    z = session["start_z"]
    d = session["direction"]
    desc = f"+{d}" if d > 0 else str(d)
    lines = [f"**Session terminée — Z = {z}  |  Pas : {desc}/joueur**\n"]

    # Archiver les joueurs encore actifs
    guild = i.guild
    for pid, x in list(session["assignments"].items()):
        m = guild.get_member(pid)
        name = m.display_name if m else f"Joueur {pid}"
        session["completed"].append((name, x))
        await edit_player_msg(
            guild, pid,
            f"{m.mention if m else name} — 🔴 **Session terminée par un admin.**",
            view=None,
        )

    session["assignments"] = {}
    session["active"] = False

    if session["completed"]:
        lines.append("**Bilan des coordonnées :**")
        for name, x in sorted(session["completed"], key=lambda t: t[1], reverse=(d < 0)):
            lines.append(f"• {name} → X = **{x}** ✅")
    else:
        lines.append("_Aucune coordonnée n'a été complétée._")

    ch = await get_output_channel(guild) or await guild.fetch_channel(i.channel_id)
    await i.response.send_message("✅ Session terminée.", ephemeral=True)
    await ch.send("\n".join(lines))


# ── /aide ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="aide", description="Afficher l'explication complète du bot")
async def aide(i: discord.Interaction):
    embed = discord.Embed(
        title="📋 Guide du bot de coordonnées",
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="⚙️ Configuration (admin)",
        value=(
            "`/setup` — À faire **une seule fois** dans le canal où tu veux que le bot écrive.\n"
            "`/debut` — Lance une nouvelle session. Le bot te demande :\n"
            "　• La coordonnée **X** de départ : **+4800** ou **-4800**\n"
            "　• La coordonnée **Z** : **+4800** ou **-4800**\n"
            "　• Les **joueurs** à inclure (sélection depuis la liste)\n"
            "　→ Chaque joueur reçoit une ligne avec ses coordonnées."
        ),
        inline=False
    )

    embed.add_field(
        name="🎮 Comment ça marche",
        value=(
            "Chaque joueur reçoit un message avec sa coordonnée **X** et **Z**.\n"
            "Les coordonnées X sont distribuées dans l'ordre :\n"
            "　• Départ **+4800** → 4800, 4700, 4600… (−100 par joueur)\n"
            "　• Départ **−4800** → −4800, −4700, −4600… (+100 par joueur)\n\n"
            "Chaque message joueur contient **2 boutons** :\n"
            "　🟢 **✓ Terminé – Nouvelle ligne** : tu as fini ta ligne, le bot t'en attribue une nouvelle\n"
            "　🔴 **✗ Quitter** : tu quittes la session, ta coordonnée est libérée pour un autre"
        ),
        inline=False
    )

    embed.add_field(
        name="📊 Commandes d'information",
        value=(
            "`/fait` — Affiche toutes les coordonnées X **en cours** et **terminées**\n"
            "`/fin` *(admin)* — Termine la session et affiche le bilan final"
        ),
        inline=False
    )

    embed.add_field(
        name="🛡️ Commandes admin (`/admin`)",
        value=(
            "`/admin kick @joueur` — Retire un joueur, sa coordonnée revient dans le pool\n"
            "`/admin desassigner @joueur` — Lui retire sa coordonnée et lui en donne une nouvelle\n"
            "`/admin retirer_coord X` — Bloque une coordonnée X (réassigne automatiquement)\n"
            "`/admin ajouter_coord X` — Remet une coordonnée X bloquée dans le pool\n"
            "`/admin bloquer @joueur` — Interdit à ce joueur d'être sélectionné\n"
            "`/admin debloquer @joueur` — Retire l'interdiction"
        ),
        inline=False
    )

    embed.set_footer(text="Les coordonnées X restent entre −4800 et +4800 • Pas de 100 entre chaque joueur")

    await i.response.send_message(embed=embed)


# ── Lancement ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN:
        raise ValueError("DISCORD_TOKEN manquant dans .env — copie .env.example en .env et remplis-le.")
    bot.run(TOKEN)
