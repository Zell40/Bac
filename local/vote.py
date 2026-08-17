import os
import time
import random
import threading
import json
import requests
import urllib.parse
import re
import socket
import traceback
import difflib
import unicodedata
import datetime

from supybot import callbacks, ircmsgs, conf, world, schedule, log
from supybot.commands import wrap, additional, optional
from .messages import FUN_NO_ANSWER_MESSAGES

from http.server import BaseHTTPRequestHandler, HTTPServer

class VoteMixin:

    @wrap(['something', additional('text')])
    def jeu(self, irc, msg, args, mode, rest):
        """🛈 Commande !jeu. Pour voir les commandes disponibles, tapez : !jeu aide"""

        channel = msg.args[0]
        nick = msg.nick
        mode = re.sub(r"[<>]", "", mode.lower().strip())
        
        # ---------------------------------------------------------
        # Restriction : seuls les joueurs ayant déjà joué au moins une fois
        # peuvent utiliser la commande !jeu
        # ---------------------------------------------------------
        if nick.lower() not in self.global_stats.get("players", {}):
            irc.queueMsg(ircmsgs.notice(nick,
                "⛔ Vous devez avoir déjà joué au moins une partie pour utiliser la commande !jeu."))
            return

        
        # ---------------------------------------------------------
        # AIDE : !jeu (sans argument ou argument vide)
        # ---------------------------------------------------------
        if not mode or mode in ("?", "help", "aide"):
            irc.queueMsg(ircmsgs.notice(nick, "🎮 Commande !jeu — Aide complète"))
            irc.queueMsg(ircmsgs.notice(nick, " "))

            # --- Résumé rapide ---
            irc.queueMsg(ircmsgs.notice(nick, "📌 Utilisation rapide :"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !jeu <mode>"))
            irc.queueMsg(ircmsgs.notice(nick, "  • Exemple : !jeu facile"))
            irc.queueMsg(ircmsgs.notice(nick, " "))

            # --- Modes officiels ---
            irc.queueMsg(ircmsgs.notice(nick, "📘 Modes disponibles :"))
            irc.queueMsg(ircmsgs.notice(nick, "  • facile — 3 catégories, 30 sec, 10 manches"))
            irc.queueMsg(ircmsgs.notice(nick, "  • moyen — 5 catégories, 40 sec, 12 manches"))
            irc.queueMsg(ircmsgs.notice(nick, "  • difficile — 7 catégories, 45 sec, 15 manches"))
            irc.queueMsg(ircmsgs.notice(nick, " "))

            # --- Commandes avancées ---
            irc.queueMsg(ircmsgs.notice(nick, "⚙️ Commandes avancées :"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !jeu liste — Affiche tous les modes disponibles"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !jeu creer <nombre catégories> <durée> <nombre> — Créer un mode perso"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !jeu del <nom> — Supprimer un mode perso"))
            irc.queueMsg(ircmsgs.notice(nick, " "))

            # --- Infos utiles ---
            irc.queueMsg(ircmsgs.notice(nick, "ℹ️ Le mode choisi reste actif jusqu'à changement."))
            irc.queueMsg(ircmsgs.notice(nick, "ℹ️ Le bot démarre automatiquement en mode facile."))
            irc.queueMsg(ircmsgs.notice(nick, " "))

            # --- Autres commandes utiles ---
            irc.queueMsg(ircmsgs.notice(nick, "💡 Suggestions : !suggestion <idée>"))
            irc.queueMsg(ircmsgs.notice(nick, "🐞 Signaler un bug : !bug <message>"))
            irc.queueMsg(ircmsgs.notice(nick, "🆘 Aide générale : !aide"))
            return

        # Découpage du reste des arguments (comme !bac)
        parts = re.split(r"\s+", rest.strip()) if rest else []
        parts = [unicodedata.normalize("NFKC", p) for p in parts]

        # ---------------------------------------------------------
        # Création d'un mode
        # ---------------------------------------------------------
        if mode == "creer":
            if channel in self.active_games:
                irc.queueMsg(ircmsgs.notice(nick,
                    "❌ Impossible de créer un mode pendant une partie."))
                return

            if len(parts) < 3:
                irc.queueMsg(ircmsgs.notice(nick, "🛠 Création d’un mode personnalisé"))
                irc.queueMsg(ircmsgs.notice(nick, " "))
                irc.queueMsg(ircmsgs.notice(nick, "📌 Syntaxe :"))
                irc.queueMsg(ircmsgs.notice(nick, "  • !jeu creer <catégories> <durée> <manches>"))
                irc.queueMsg(ircmsgs.notice(nick, "  • Exemple : !jeu creer 5 40 12"))
                irc.queueMsg(ircmsgs.notice(nick, " "))
                irc.queueMsg(ircmsgs.notice(nick, "📘 Paramètres :"))
                irc.queueMsg(ircmsgs.notice(nick, f"  • catégories : entre 2 et {len(self.data['categories'])} - Nombre de catégories par manche."))
                irc.queueMsg(ircmsgs.notice(nick, "  • durée : entre 10 et 300 secondes - Durée en secondes de chaque manche."))
                irc.queueMsg(ircmsgs.notice(nick, "  • manches : entre 1 et 50 - Nombre de manches par partie."))
                irc.queueMsg(ircmsgs.notice(nick, " "))
                irc.queueMsg(ircmsgs.notice(nick, "💡 Le mode portera automatiquement votre nom."))
                irc.queueMsg(ircmsgs.notice(nick, "💡 Vous pourrez ensuite l’utiliser avec : !jeu <votre_pseudo>"))
                return

            try:
                categories = int(parts[0])
                duration   = int(parts[1])
                maxrounds  = int(parts[2])
            except ValueError:
                irc.queueMsg(ircmsgs.notice(nick, "❌ Les valeurs doivent être des nombres."))
                return

            if not (3 <= categories <= len(self.data["categories"])):
                irc.queueMsg(ircmsgs.notice(nick,
                    f"⛔ Catégories invalides (3–{len(self.data['categories'])})."))
                return

            if not (10 <= duration <= 300):
                irc.queueMsg(ircmsgs.notice(nick, "⛔ Durée invalide (10–300 sec)."))
                return

            if not (1 <= maxrounds <= 50):
                irc.queueMsg(ircmsgs.notice(nick, "⛔ Manches invalides (1–50)."))
                return

            now = int(time.time())

            self.modes[nick.lower()] = {
                "display": nick,
                "categories": categories,
                "duration": duration,
                "maxrounds": maxrounds,
                "created_at": now,
                "created_by": nick,
                "last_used": None,
                "times_used": 0,
                "modified_at": None,
                "modified_by": None
            }

            self._save_json(self.modes_file, self.modes)
            
            # Notification BotServ 
            dev_channel = self.registryValue('devChannel', msg.channel)

            # Vérifier que le salon est configuré
            if dev_channel and dev_channel.startswith("#"):
                irc.queueMsg(ircmsgs.privmsg(
                    "BotServ",
                    f"say {dev_channel} [BAC][MODE] {nick} a créé le mode « {nick.lower()} » "
                    f"({categories} catégories, {duration}s, {maxrounds} manches)."
                )) 

            irc.queueMsg(ircmsgs.notice(nick,
                f"💾 Mode personnalisé créé sous le nom « {nick} ». Tape !jeu {nick} pour l'utiliser."))
            return

        # ---------------------------------------------------------
        # Liste des modes
        # ---------------------------------------------------------
        if mode == "liste":
            active = self.current_mode.get(channel, "facile")

            irc.queueMsg(ircmsgs.notice(nick, "📋 Modes disponibles :"))

            # Séparer modes verrouillés et customs
            locked_modes = []
            custom_modes = []

            for key, cfg in self.modes.items():
                if cfg.get("locked"):
                    locked_modes.append((key, cfg))
                else:
                    custom_modes.append((key, cfg))

            # Trier les customs du plus utilisé au moins utilisé
            custom_modes.sort(key=lambda x: x[1].get("times_used", 0), reverse=True)

            # Fusion finale : verrouillés d'abord, customs ensuite
            ordered_modes = locked_modes + custom_modes

            for key, cfg in ordered_modes:
                name = cfg.get("display", key)

                # Cadenas si verrouillé
                lock_icon = " 🔒" if cfg.get("locked") else ""

                # Indicateur du mode actif
                marker = " \x02\x0303(actif)\x03\x02" if key == active else ""

                irc.queueMsg(ircmsgs.notice(
                    nick,
                    f"• \x02\x0304{name}{lock_icon}\x03\x02{marker} → "
                    f"\x02\x0303{cfg['categories']}\x03\x02 catégories, "
                    f"\x02\x0303{cfg['duration']}\x03\x02 secondes, "
                    f"\x02\x0303{cfg['maxrounds']}\x03\x02 manches."
                ))

            irc.queueMsg(ircmsgs.notice(nick,
                "Tapez !jeu <mode> pour sélectionner un mode de jeu (exemple : !jeu moyen)."))
            return

        # ---------------------------------------------------------
        # Suppression d'un mode
        # ---------------------------------------------------------
        if mode == "del":
            if len(parts) < 1:
                irc.queueMsg(ircmsgs.notice(nick, "❌ Syntaxe : !jeu del <mode> ( exemple !jeu del zell )"))
                return

            target = parts[0].lower()

            if target not in self.modes:
                irc.queueMsg(ircmsgs.notice(nick, f"❌ Aucun mode nommé « {target} »."))
                return

            if self.modes[target].get("locked"):
                irc.queueMsg(ircmsgs.notice(nick,
                    "⛔ Ce mode est officiel et ne peut pas être supprimé."))
                return

            # -----------------------------------------------------
            # Vérification des permissions
            # -----------------------------------------------------

            # 1) Vérifier si le mode est verrouillé
            if self.modes[target].get("locked"):
                irc.queueMsg(ircmsgs.notice(nick,
                    "⛔ Ce mode est verrouillé et ne peut pas être supprimé."))
                return

            # 2) Seul le propriétaire peut supprimer via !jeu del
            is_owner = (nick.lower() == target)

            if not is_owner:
                irc.queueMsg(ircmsgs.notice(nick,
                    "⛔ Seul le propriétaire du mode peut le supprimer via !jeu del"))
                return


            # -----------------------------------------------------
            # Suppression
            # -----------------------------------------------------
            del self.modes[target]
            self._save_json(self.modes_file, self.modes)
            
            # Notification BotServ 
            dev_channel = self.registryValue('devChannel', msg.channel)

            # Vérifier que le salon est configuré
            if dev_channel and dev_channel.startswith("#"):
                irc.queueMsg(ircmsgs.privmsg(
                    "BotServ",
                    f"say {dev_channel} [BAC][MODE] {nick} a supprimé le mode « {target} » "
                )) 
            
            # Si le mode supprimé était le mode actif du salon → fallback vers "facile"
            if self.current_mode.get(channel) == target:
                self.current_mode[channel] = "facile"

                # Mise à jour des paramètres du jeu
                cfg = self.modes["facile"]
                conf.supybot.plugins.PetitBac.roundDuration.setValue(cfg["duration"])
                conf.supybot.plugins.PetitBac.categoryCount.setValue(cfg["categories"])
                conf.supybot.plugins.PetitBac.maxRounds.setValue(cfg["maxrounds"])

                irc.queueMsg(ircmsgs.notice(nick,
                    "ℹ️ Le mode actif a été supprimé. Retour automatique au mode FACILE."))

            irc.queueMsg(ircmsgs.notice(nick, f"🗑 Mode « {target} » supprimé."))
            return

        # ---------------------------------------------------------
        # Sélection d'un mode existant
        # ---------------------------------------------------------
        mode = mode.lower()
        if mode not in self.modes:

            irc.queueMsg(ircmsgs.notice(nick,
                f"❌ Mode inconnu : {mode}. Tapez !jeu liste pour voir les modes disponibles."))
            return

        mode_cfg = self.modes[mode]
        now = time.time()

        # Cooldown
        if channel in self.last_mode_change:
            if now - self.last_mode_change[channel] < 120:
                irc.queueMsg(ircmsgs.notice(nick,
                    "⏳ Vous devez patienter 2 minutes avant un nouveau changement de mode."))
                return

        # ---------------------------------------------------------
        # Application directe si aucune partie en cours
        # ---------------------------------------------------------
        if channel not in self.active_games:
            conf.supybot.plugins.PetitBac.roundDuration.setValue(mode_cfg["duration"])
            conf.supybot.plugins.PetitBac.categoryCount.setValue(mode_cfg["categories"])
            conf.supybot.plugins.PetitBac.maxRounds.setValue(mode_cfg["maxrounds"])

            self.current_mode[channel] = mode
            now = int(time.time())
            self.modes[mode]["last_used"] = now
            self.modes[mode]["times_used"] += 1
            self._save_json(self.modes_file, self.modes)
            self.last_mode_change[channel] = now

            irc.queueMsg(ircmsgs.privmsg(channel,
                f"🎮 Le mode du jeu à été défini sur {mode.capitalize()}. Tapez !jouer pour commencer une partie avec ce mode."))
            return

        # ---------------------------------------------------------
        # Partie en cours → vote
        # ---------------------------------------------------------
        players = set(self.players.get(channel, set()))
        if nick in players:
            players.remove(nick)
           
        if len(players) == 0:
            # --- ANNULATION IMMÉDIATE DE TOUS LES TIMERS ---
            game = self.active_games.get(channel)
            if game:
                # Annuler tous les timers possibles AVANT tout affichage
                for ev in game.get("start_timers", []):
                    try: schedule.removeEvent(ev)
                    except KeyError: pass
                game["start_timers"] = []

                for ev in game.get("round_timers", []):
                    try: schedule.removeEvent(ev)
                    except KeyError: pass
                game["round_timers"] = []

                for ev in game.get("countdown_timers", []):
                    try: schedule.removeEvent(ev)
                    except KeyError: pass
                game["countdown_timers"] = []

                if game.get("pause_timer"):
                    try: schedule.removeEvent(game["pause_timer"])
                    except: pass
                game["pause_timer"] = None

                if game.get("fullcombo_timer"):
                    try: schedule.removeEvent(game["fullcombo_timer"])
                    except: pass
                game["fullcombo_timer"] = None

                try: schedule.removeEvent(f"idle_{channel}")
                except KeyError: pass

                try: schedule.removeEvent(f"round_timer_{channel}")
                except KeyError: pass

                # Supprimer la partie
                del self.active_games[channel]

            # --- Application immédiate du mode ---
            conf.supybot.plugins.PetitBac.roundDuration.setValue(mode_cfg["duration"])
            conf.supybot.plugins.PetitBac.categoryCount.setValue(mode_cfg["categories"])
            conf.supybot.plugins.PetitBac.maxRounds.setValue(mode_cfg["maxrounds"])

            self.current_mode[channel] = mode
            now = int(time.time())
            self.modes[mode]["last_used"] = now
            self.modes[mode]["times_used"] += 1
            self._save_json(self.modes_file, self.modes)
            self.last_mode_change[channel] = now

            irc.queueMsg(ircmsgs.privmsg(channel,
                f"🎮 Mode défini sur {mode.upper()} (vous êtes seul)."))
            irc.queueMsg(ircmsgs.privmsg(channel,
                "🔄 La partie précédente a été arrêtée et une nouvelle partie démarre avec ce mode."))

            # --- Nouvelle partie ---
            self._startGame(irc, channel, starter=nick, show_rules=False)
            return

        # Vote normal
        self.mode_vote[channel] = {
            "proposed_mode": mode,
            "votes": {},
            "players": players,
            "timer": None
        }

        self._do_pause(irc, channel, paused_by="vote de mode")

        irc.queueMsg(ircmsgs.privmsg(channel,
            "⏸️ Partie mise en pause (vote de mode)."))

        irc.queueMsg(ircmsgs.privmsg(channel,
            f"🗳 Vote : {nick} propose de passer en mode {mode.upper()}."))

        voters_list = ", ".join(players)
        irc.queueMsg(ircmsgs.privmsg(channel,
            f"👥 Joueurs concernés : {voters_list}"))

        irc.queueMsg(ircmsgs.privmsg(channel,
            "Votez : !oui ou !non (30 secondes)"))

        t = threading.Timer(30, lambda: self._safe(irc, lambda: self._finalize_mode_vote(irc, channel)))
        self.mode_vote[channel]["timer"] = t
        t.start()

    def oui(self, irc, msg, args):
        """ Commande de vote !oui """
        
        channel = msg.args[0]
        nick = msg.nick

        # Vote de mode
        if channel in self.mode_vote:
            vote = self.mode_vote[channel]
            
            # Nettoyage des votants absents
            if self._clean_vote_players(irc, channel, vote):
                self._finalize_mode_vote(irc, channel)
                return
                
            if nick not in vote["players"]:
                irc.queueMsg(ircmsgs.notice(nick, "❌ Seuls les joueurs peuvent voter."))
                return

            vote["votes"][nick] = True
            irc.queueMsg(ircmsgs.notice(channel, f"🟢 {nick} vote OUI"))

            if len(vote["votes"]) == len(vote["players"]):
                self._finalize_mode_vote(irc, channel)
            return

        # Vote continuer/recommencer
        if channel in self.restart_vote:
            vote = self.restart_vote[channel]
            if nick not in vote["players"]:
                irc.queueMsg(ircmsgs.notice(nick, "❌ Seuls les joueurs peuvent voter."))
                return

            vote["votes"][nick] = True
            irc.queueMsg(ircmsgs.notice(channel, f"🟢 {nick} vote CONTINUER"))

            if len(vote["votes"]) == len(vote["players"]):
                self._finalize_restart_vote(irc, channel)
            return

        irc.queueMsg(ircmsgs.notice(nick, "❌ Aucun vote en cours."))

    oui = wrap(oui)

    def non(self, irc, msg, args):
        """ Commande de vote !non """
        
        channel = msg.args[0]
        nick = msg.nick

        # Vote de mode
        if channel in self.mode_vote:
            vote = self.mode_vote[channel]
            
            # Nettoyage des votants absents
            if self._clean_vote_players(irc, channel, vote):
                self._finalize_mode_vote(irc, channel)
                return
                
            if nick not in vote["players"]:
                irc.queueMsg(ircmsgs.notice(nick, "❌ Seuls les joueurs peuvent voter."))
                return

            vote["votes"][nick] = False
            irc.queueMsg(ircmsgs.notice(channel, f"🔴 {nick} vote NON"))

            if len(vote["votes"]) == len(vote["players"]):
                self._finalize_mode_vote(irc, channel)
            return

        # Vote continuer/recommencer
        if channel in self.restart_vote:
            vote = self.restart_vote[channel]
            if nick not in vote["players"]:
                irc.queueMsg(ircmsgs.notice(nick, "❌ Seuls les joueurs peuvent voter."))
                return

            vote["votes"][nick] = False
            irc.queueMsg(ircmsgs.notice(channel, f"🔴 {nick} vote RECOMMENCER"))

            if len(vote["votes"]) == len(vote["players"]):
                self._finalize_restart_vote(irc, channel)
            return

        irc.queueMsg(ircmsgs.notice(nick, "❌ Aucun vote en cours."))

    non = wrap(non)

    def _finalize_mode_vote(self, irc, channel):
        vote = self.mode_vote[channel]
        mode = vote["proposed_mode"]
        votes = vote["votes"]

        if vote["timer"]:
            vote["timer"].cancel()

        yes = sum(1 for v in votes.values() if v)
        no = sum(1 for v in votes.values() if not v)

        # ---------------------------------------------------------
        # VOTE ACCEPTÉ → appliquer le mode + lancer second vote
        # ---------------------------------------------------------
        if yes == len(vote["players"]):
            # Enregistrer la modification du mode
            cfg = self.modes[mode]
            cfg["modified_at"] = int(time.time())
            cfg["modified_by"] = list(vote["votes"].keys())[0] if vote["votes"] else "inconnu"
            self._save_json(self.modes_file, self.modes)

            irc.queueMsg(ircmsgs.privmsg(channel,
                f"🟢 Vote accepté : passage en mode {mode.upper()}."))

            irc.queueMsg(ircmsgs.privmsg(channel,
                f"⚙️ Nouvelle configuration : {cfg['categories']} catégories, "
                f"{cfg['duration']} sec par manche, {cfg['maxrounds']} manches max."))

            irc.queueMsg(ircmsgs.privmsg(channel,
                "ℹ️ Ce mode sera appliqué à la prochaine partie."))

            # --- Lancer le second vote (continuer / recommencer) ---
            irc.queueMsg(ircmsgs.privmsg(channel,
                "❓ Voulez-vous continuer la partie actuelle ?"))
            irc.queueMsg(ircmsgs.privmsg(channel,
                "➡️ Tapez !oui pour continuer, !non pour recommencer une nouvelle partie."))

            players = self.players.get(channel, set()) or set()
            self.restart_vote[channel] = {
                "votes": {},
                "players": players,
                "timer": None
            }

            t = threading.Timer(30, lambda: self._safe(irc, lambda: self._restart_vote_timeout(irc, channel)))
            self.restart_vote[channel]["timer"] = t
            t.start()

            del self.mode_vote[channel]
            return

        # ---------------------------------------------------------
        # VOTE REFUSÉ → reprendre immédiatement la partie
        # ---------------------------------------------------------
        irc.queueMsg(ircmsgs.privmsg(channel,
            "🔴 Vote refusé : le mode actuel reste inchangé."))
        irc.queueMsg(ircmsgs.privmsg(channel,
            "ℹ️ La partie actuelle continue normalement."))
            
        # recalcul du temps restant avant reprise
        game = self.active_games.get(channel)
        if game:
            duration = conf.supybot.plugins.PetitBac.roundDuration()
            start_time = game.get("round_start_time")
            if start_time:
                elapsed = time.time() - start_time
                game["time_left"] = max(1, int(duration - elapsed))
            else:
                game["time_left"] = duration

        # Reprise en douceur (compte à rebours, timers, etc.)
        self._do_resume(irc, channel)

        del self.mode_vote[channel]

    def _finalize_restart_vote(self, irc, channel):
        vote = self.restart_vote[channel]

        if vote["timer"]:
            vote["timer"].cancel()

        yes = sum(1 for v in vote["votes"].values() if v)
        no = sum(1 for v in vote["votes"].values() if not v)

        if yes >= no:
            # CONTINUER
            self._do_resume(irc, channel)
        else:
            # RECOMMENCER : on ne reprend pas la manche, on repart de zéro
            game = self.active_games.get(channel)
            if game:
                # On nettoie juste l’état de pause éventuel
                game["paused"] = False
                game["pause_timer"] = None

            if channel in self.active_games:
                del self.active_games[channel]

            starter = list(vote["players"])[0] if vote["players"] else "Auto"
            # Appliquer le mode choisi maintenant
            mode = self.pending_mode_change.get(channel)
            if mode:
                cfg = self.modes[mode]
                conf.supybot.plugins.PetitBac.roundDuration.setValue(cfg["duration"])
                conf.supybot.plugins.PetitBac.categoryCount.setValue(cfg["categories"])
                conf.supybot.plugins.PetitBac.maxRounds.setValue(cfg["maxrounds"])
                self.current_mode[channel] = mode
                now = int(time.time())
                self.modes[mode]["last_used"] = now
                self.modes[mode]["times_used"] += 1
                self._save_json(self.modes_file, self.modes)
                self.last_mode_change[channel] = time.time()
                del self.pending_mode_change[channel]
            self._startGame(irc, channel, starter=starter, show_rules=False)

        del self.restart_vote[channel]

    def _restart_vote_timeout(self, irc, channel):
        if channel not in self.restart_vote:
            return

        irc.queueMsg(ircmsgs.privmsg(channel,
            "⏳ Temps écoulé : la partie actuelle continue."))

        self._do_resume(irc, channel)

        del self.restart_vote[channel]
        
    def _clean_vote_players(self, irc, channel, vote):
        """Retire les joueurs absents/away de la liste des votants."""
        cleaned = False
        players = set(vote["players"])

        for user in list(players):
            # 1) Le joueur n'est plus dans le salon
            if user not in irc.state.channels[channel].users:
                players.remove(user)
                cleaned = True
                continue

            # 2) Le joueur est AWAY
            if irc.state.getNick(user).away:
                players.remove(user)
                cleaned = True

        vote["players"] = players

        # Si on a nettoyé et que tous les votants restants ont voté → finaliser
        if cleaned and len(vote["votes"]) == len(players):
            return True  # signaler qu'il faut finaliser

        return False
