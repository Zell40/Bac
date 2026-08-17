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

class GameMixin:
    
    # ------------------------------------------------------------------------
    # Changement des catégories
    # ------------------------------------------------------------------------
    def _pickCategories(self, channel, round_number):
        """
        Sélectionne les catégories pour une manche :
        - utilise self.data["categories"]
        - respecte la rotation configurée
        - choisit un nombre de catégories défini par la config
        """
        
        game = self.active_games.get(channel)
        rotation = game["config"]["rotation"]
        count = game["config"]["category_count"]

        # Liste des catégories disponibles
        all_cats = list(self.data["categories"].keys())

        # 🔥 Condition correcte :
        # - manche 1 → toujours changer
        # - sinon → changer toutes les "rotation" manches
        if round_number != 1 and (round_number - 1) % rotation != 0:
            return game.get("categories", all_cats[:count])

        # 🔥 Tirage des catégories
        if len(all_cats) <= count:
            return all_cats

        return random.sample(all_cats, count)
                        
    # ----------------------------------------------------------------------
    # Règles du jeu
    # ----------------------------------------------------------------------
    def _send_rules_notice(self, irc, nick):
        modes = self.modes

        irc.queueMsg(ircmsgs.notice(nick, "📘 \x0312\x02\x1FRègles du PetitBac\x0F"))
        irc.queueMsg(ircmsgs.notice(nick, "• Une lettre est tirée au sort."))
        irc.queueMsg(ircmsgs.notice(nick, "• Trouvez un mot commençant par cette lettre."))
        irc.queueMsg(ircmsgs.notice(nick, "• Le mot doit appartenir aux catégories affichées."))

        irc.queueMsg(ircmsgs.notice(nick,
            "• \x0303\x021 seul mot par catégorie\x0F, \x0303\x021 seul mot par ligne\x0F."))
        irc.queueMsg(ircmsgs.notice(nick,
            "• \x0303\x02+1 point\x0F mot simple, \x0303\x02+2 points\x0F mot difficile."))
        irc.queueMsg(ircmsgs.notice(nick,
            "• \x0303\x02Bonus\x0F : \x0303\x02+1 point\x0F si toutes les catégories sont remplies."))

        irc.queueMsg(ircmsgs.notice(nick, "• La manche s’arrête quand le temps est écoulé."))
        irc.queueMsg(ircmsgs.notice(nick, " "))

        irc.queueMsg(ircmsgs.notice(nick, "🎮 \x0312\x02\x1FModes de jeu\x0F"))
        irc.queueMsg(ircmsgs.notice(nick,
            "• Voir les modes disponibles : \x0303\x02!jeu liste\x0F"))
        irc.queueMsg(ircmsgs.notice(nick,
            "• Changer de mode : \x0303\x02!jeu <mode>\x0F (ex : \x0303\x02!jeu facile\x0F)"))
        irc.queueMsg(ircmsgs.notice(nick, "• Voter : \x0303\x02!oui\x0F / \x0303\x02!non\x0F"))
        irc.queueMsg(ircmsgs.notice(nick, " "))

        irc.queueMsg(ircmsgs.notice(nick, "🛈 \x0312\x02\x1FAide\x0F"))
        irc.queueMsg(ircmsgs.notice(nick,
            "Tapez \x0303\x02!aide\x0F pour voir les commandes disponibles"))
        irc.queueMsg(ircmsgs.notice(nick, "Bonne partie ! 🎉"))

    # ----------------------------------------------------------------------
    # Nouvelle partie
    # ----------------------------------------------------------------------
    def _randomLetter(self, categories, previous=None, round_number=None):
        hard_letters = set("WXYZKQ")

        # ---------------------------------------------------------
        # 1) Récupérer les lettres possibles pour chaque catégorie
        # ---------------------------------------------------------
        lettres_par_cat = []
        for cat in categories:
            mots = self.data["categories"].get(cat, {}).get("mots", [])

            def norm(l):
                return unicodedata.normalize("NFD", l)[0].upper()

            lettres = {norm(m.strip()[0]) for m in mots if m.strip()}
            lettres_par_cat.append(lettres)

        # Intersection des lettres possibles
        if lettres_par_cat:
            lettres_valides = set.intersection(*lettres_par_cat)
        else:
            lettres_valides = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

        # Filtrer A–Z
        lettres_valides = {l for l in lettres_valides if "A" <= l <= "Z"}

        # Retirer lettres difficiles en début de partie
        if round_number and round_number <= 3:
            lettres_valides -= hard_letters

        # Retirer lettres déjà utilisées
        used = set(getattr(self, "used_letters", []))
        lettres_valides -= used

        # Éviter la lettre précédente (90% du temps)
        if previous and previous in lettres_valides and random.random() >= 0.10:
            lettres_valides.discard(previous)

        # ---------------------------------------------------------
        # 2) Fallback si aucune lettre valide
        # ---------------------------------------------------------
        if not lettres_valides:
            lettres_valides = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

            if round_number and round_number <= 3:
                lettres_valides -= hard_letters

            lettres_valides -= used

            if previous:
                lettres_valides.discard(previous)

        # ---------------------------------------------------------
        # 3) Calcul des poids
        # ---------------------------------------------------------
        lettres = sorted(lettres_valides)
        poids = []

        for l in lettres:
            count = 0
            for cat in categories:
                mots = self.data["categories"].get(cat, {}).get("mots", [])
                count += sum(1 for m in mots if m.startswith(l.lower()))
            poids.append(count or 1)

        # ---------------------------------------------------------
        # 4) Sécurité finale : jamais de crash
        # ---------------------------------------------------------
        if not lettres or not poids or len(lettres) != len(poids):
            lettres = [l for l in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                       if not (round_number and round_number <= 3 and l in hard_letters)]
            poids = [1] * len(lettres)
            self.used_letters = []

        # ---------------------------------------------------------
        # 5) Choix final
        # ---------------------------------------------------------
        lettre = random.choices(lettres, weights=poids, k=1)[0]

        # Mémoriser la lettre utilisée
        if not hasattr(self, "used_letters"):
            self.used_letters = []
        self.used_letters.append(lettre)

        return lettre

    def _newGame(self, channel):
        """
        Initialise une nouvelle partie ou une nouvelle structure de jeu.
        Version JSON : utilise self.data["categories"].
        """

        # Charger les catégories JSON
        self.data = self._load_categories_json()

        # Numéro de manche (1 si nouvelle partie)
        previous_round = self.active_games.get(channel, {}).get("round", 0)
        round_number = previous_round + 1

        # Sélection des catégories
        cats = self._pickCategories(channel, round_number)

        # Initialiser la liste des lettres utilisées si besoin
        if not hasattr(self, "used_letters"):
            self.used_letters = []

        # Lettre précédente (si une partie existait déjà)
        previous_letter = self.active_games.get(channel, {}).get("letter")

        # Choix d'une lettre intelligente
        letter = self._randomLetter(cats, previous=previous_letter)

        # Construction de la structure de jeu
        game = {
            "letter": letter,
            "categories": cats,
            "answers": {},
            "round": round_number,
            "paused": False,
            "pause_timer": None,
            "idle_rounds": self.active_games.get(channel, {}).get("idle_rounds", 0),
            "used_words": set(), # mots enregistré utilisés dans une partie
            "round_start_time": time.time(),
            "speed_offset": 0,
            "last_activity": time.time(),
            "start_timers": [],
            "round_timers": [],
            "countdown_timers": [],
            "fullcombo_timer": None,
            "round_active": False,
        }

        # Fusionner avec la config déjà stockée dans active_games[channel]
        self.active_games[channel].update(game)

        return self.active_games[channel]
        
    def _is_difficult_word(self, mot):
        mot = mot.lower()
        score = 0

        # 1. Lettre difficile
        lettres_difficiles = {"w", "x", "y", "z", "k", "q"}
        if mot[0] in lettres_difficiles:
            score += 1

        # 2. Longueur
        if len(mot) >= 12:
            score += 1
        elif len(mot) >= 8:
            score += 0.5

        # 3. Rareté dans l’historique
        usage = self.word_usage.get(mot, 0)
        if usage == 0:
            score += 1
        elif usage <= 2:
            score += 0.5

        return score >= 2
        
    def _startGame(self, irc, channel, starter, player=None, show_rules=True):
        is_new_game = channel not in self.active_games
        
         # Si la partie démarre automatiquement → forcer le mode facile
        if starter == "AutoStart":
            # Forcer le mode facile
            self.current_mode[channel] = "facile"

            # Appliquer les paramètres du mode facile
            cfg = self.modes["facile"]
            conf.supybot.plugins.PetitBac.roundDuration.setValue(cfg["duration"])
            conf.supybot.plugins.PetitBac.categoryCount.setValue(cfg["categories"])
            conf.supybot.plugins.PetitBac.maxRounds.setValue(cfg["maxrounds"])

        # Reset des scores si nouvelle partie
        if is_new_game:
            self.scoreboard = {}
            self._save_scores()
            mode_name = self.current_mode.get(channel, "facile")
            irc.queueMsg(ircmsgs.privmsg(channel, f"📢 Nouvelle partie démarré en mode {mode_name} !"))
                        
        # Annonce automatique
        if conf.supybot.plugins.PetitBac.announceMessage():
            announce = (
                f"\00314Une partie de \00307\002Petit Baccalauréat\002 "
                f"\00314vient d'être lancée sur le salon\00303\002 {channel}\002 "
                f"\00314. Tape \00303\002/join {channel}\002 \00314pour rejoindre la partie. 🎉"
            )
            announce_channel = conf.supybot.plugins.PetitBac.announceChannel()
            irc.queueMsg(ircmsgs.privmsg("botserv", f"say {announce_channel} {announce}"))

        # 🔥 NOUVEAU : Charger les catégories JSON
        self.data = self._load_categories_json()

        if player is None:
            player = starter

        # Enregistrer le joueur comme participant actif
        if channel not in self.players:
            self.players[channel] = set()
        self.players[channel].add(player)

        # Annuler anciens timers de démarrage
        old = self.active_games.get(channel)
        if old:
            for ev in old.get("start_timers", []):
                try:
                    schedule.removeEvent(ev)
                except KeyError:
                    pass

        # Toujours appliquer le mode actif avant de figer la config
        mode_name = self.current_mode.get(channel, "facile")
        cfg = self.modes[mode_name]

        conf.supybot.plugins.PetitBac.roundDuration.setValue(cfg["duration"])
        conf.supybot.plugins.PetitBac.categoryCount.setValue(cfg["categories"])
        conf.supybot.plugins.PetitBac.maxRounds.setValue(cfg["maxrounds"])

        # Figer la configuration de la partie
        game_config = {
            "duration": conf.supybot.plugins.PetitBac.roundDuration(),
            "category_count": conf.supybot.plugins.PetitBac.categoryCount(),
            "max_rounds": conf.supybot.plugins.PetitBac.maxRounds(),
            "rotation": conf.supybot.plugins.PetitBac.categoryRotation(),
        }
        
        # Stocker la config dans active_games AVANT _newGame()
        self.active_games[channel] = {"config": game_config}

        # Nouveau jeu
        game = self._newGame(channel)
        game["round"] = 1
        
        # Enregistrement des mots pendant la partie
        game["used_words_game"] = set()

        # Timers de démarrage
        game["start_timers"] = []

        duration = game_config["duration"]
        rotation = game_config["rotation"]
        category_count = game_config["category_count"]
        max_rounds = game_config["max_rounds"]
        round = game["round"]
        
        # --- Événement IRCv3 : début de partie ---
        self._send_event(
            irc,
            channel,
            "game_start",
            mode=mode_name,
            duration=duration,
            categories=category_count,
            rotation=rotation,
            max_rounds=max_rounds,
            starter=starter,
            round=round,
        )

        delay = 0

        # --- BLOC 1 : RÈGLES ---
        if is_new_game and show_rules:
            # Messages TAGS
            self._send_event(
                irc,
                channel,
                "rules_start",
                player=player
            )
            
            event_name = f"start_{channel}_hello"
            schedule.addEvent(
                lambda: irc.queueMsg(ircmsgs.privmsg(channel,
                    f"📢 Bonjour {player} ! Rejoins-nous, ça va commencer...")),
                time.time() + delay,
                name=event_name
            )
            game["start_timers"].append(event_name)

            # --- RÈGLES COMPACTES & STYLISÉES ---
            rules_lines = [
            "📘 \x0312\x02\x1FRègles du Petit Bac\x0F",
            "• Une lettre est tirée au sort.",
            "• Trouvez un mot commençant par cette lettre.",
            "• Le mot doit appartenir aux catégories affichées.",

            "• \x0303\x021 seul mot par catégorie\x0F, \x0303\x021 seul mot par ligne\x0F.",
            "• \x0303\x02+1 point\x0F mot simple, \x0303\x02+2 points\x0F mot difficile.",
            "• \x0303\x02Bonus\x0F : \x0303\x02+1 point\x0F si toutes les catégories sont remplies.",

            "• La manche s’arrête quand le temps est écoulé.",
            "• Les scores s’additionnent.",
            " ",

            "🎮 \x0312\x02\x1FModes de jeu\x0F",
            "• Voir les modes de jeu disponibles : \x0303\x02!jeu liste\x0F",
            "• Changer de mode de jeu : \x0303\x02!jeu <mode>\x0F (ex : \x0303\x02!jeu facile\x0F)",
            "• Voter : \x0303\x02!oui\x0F / \x0303\x02!non\x0F",
            " ",

            "🛈 \x0312\x02\x1FAide\x0F",
            "Tapez \x0303\x02!aide\x0F pour voir les commandes disponibles.",
            "Bonne chance à tous ! 🎉",
            " "
            ]
            
            # Envoi progressif des règles
            for i, line in enumerate(rules_lines):
                delay += 0.05
                event_name = f"start_{channel}_rule_{i}"
                schedule.addEvent(
                    lambda l=line, n=player: irc.queueMsg(ircmsgs.notice(n, l)),
                    time.time() + delay,
                    name=event_name
                )
                game["start_timers"].append(event_name)

            delay += 10

        # --- BLOC 2 : CONFIG ---
        for i, line in enumerate([
            f"🏆 La partie démarre : {max_rounds} manches de {duration} secondes chacune.",
            f"📚 {category_count} catégories seront en jeu et elles changeront toutes les {rotation} manches.",
        ]):

            event_name = f"start_{channel}_cfg_{i}"
            schedule.addEvent(
                lambda l=line: irc.queueMsg(ircmsgs.privmsg(channel, l)),
                time.time() + delay,
                name=event_name
            )
            game["start_timers"].append(event_name)
            delay += 0.05

        delay += 5

        # --- BLOC 3 : COMPTE À REBOURS ---                    
        for n in [5, 4, 3, 2, 1]:

            # TAGMSG retardé
            tag_name = f"tag_countdown_{channel}_{n}"
            schedule.addEvent(
                lambda x=n: self._send_event(
                    irc,
                    channel,
                    "countdown_start",
                    seconds=x
                ),
                time.time() + delay,
                name=tag_name
            )
            game["start_timers"].append(tag_name)

            # PRIVMSG retardé
            msg_name = f"start_{channel}_{n}"
            schedule.addEvent(
                lambda x=n: irc.queueMsg(ircmsgs.privmsg(channel, f"⏳ Le jeu commence dans {x}...")),
                time.time() + delay,
                name=msg_name
            )
            game["start_timers"].append(msg_name)

            delay += 1.0

        # GO !
        # TAG : lancement officiel
        self._send_event(
            irc,
            channel,
            "game_go"
        )
        event_name = f"start_go_{channel}"
        schedule.addEvent(
            lambda: irc.queueMsg(ircmsgs.privmsg(channel, "🚀 C'est parti !")),
            time.time() + delay,
            name=event_name
        )
        game["start_timers"].append(event_name)

        delay += 0.5

        # --- Lancer la première manche ---
        event_name = f"start_round_{channel}"
        schedule.addEvent(
            lambda: self._startRound(irc, channel),
            time.time() + delay,
            name=event_name
        )
        game["start_timers"].append(event_name)

        # Rappel quotidien des mots en attente
        self._schedule_daily_pending_check(irc, channel)

    def _startRound(self, irc, channel):

        # --- Sécurité absolue : la partie doit exister ---
        if channel not in self.active_games:
            return

        game = self.active_games[channel]

        # --- Sécurité : la partie a été stoppée ---
        if game.get("stopped"):
            return

        # --- Annuler d'anciens timers de manche ---
        for ev in game.get("round_timers", []):
            try:
                schedule.removeEvent(ev)
            except KeyError:
                pass
        game["round_timers"] = []

        # --- Annuler d'anciens countdowns ---
        for ev in game.get("countdown_timers", []):
            try:
                schedule.removeEvent(ev)
            except KeyError:
                pass
        game["countdown_timers"] = []

        # --- Charger les catégories JSON à CHAQUE manche ---
        self.data = self._load_categories_json()

        # --- Sélectionner les catégories pour cette manche ---
        round_number = game.get("round", 1)
        game["categories"] = self._pickCategories(channel, round_number)

        # --- Sélectionner la lettre ---
        previous_letter = game.get("letter")
        game["letter"] = self._randomLetter(
            game["categories"],
            previous_letter,
            round_number=game["round"]
        )
        
        # --- Initialisation de la manche ---
        max_rounds = game["config"]["max_rounds"]
        duration = game["config"]["duration"]
        game["round_start_time"] = time.time()
        game["round_active"] = True
                      
        # Reset des réponses
        game["answers"] = {}
        game["speed_offset"] = 0
        game["last_activity"] = time.time()

        # Sauvegarde du score cumulé avant la manche
        game["score_before_round"] = dict(self.scoreboard)
        
        # --- Événement IRCv3 : début de manche ---
        self._send_event(
            irc,
            channel,
            "round_start",
            round=game["round"],
            letter=game["letter"],
            categories=",".join(game["categories"]),
            duration=duration,
            totalRounds=max_rounds
        )

        # --- Message : numéro de manche ---
        irc.queueMsg(ircmsgs.privmsg(channel,
            f"📊 Manche {game['round']} / {max_rounds}"))

        # --- Lettre + catégories ---
        event_name = f"round_info_{channel}"
        schedule.addEvent(
            lambda: irc.queueMsg(ircmsgs.privmsg(
                channel,
                f"🎲 Lettre : \x02\x0304{game['letter']}\x0F  |  "
                f"📚 Catégories : \x02\x0303{', '.join(game['categories'])}\x0F"
            )),
            time.time(),
            name=event_name
        )
        game["round_timers"].append(event_name)

        # --- Message rappel ---
        event_name = f"round_reminder_{channel}"
        schedule.addEvent(
            lambda: irc.queueMsg(ircmsgs.privmsg(
                channel,
                "📌 Rappel : tapez un mot par catégorie, sans virgule, point ou autre ponctuation."
            )),
            time.time() + 0.1,
            name=event_name
        )
        game["round_timers"].append(event_name)

        # --- Timer principal de fin de manche ---
        event_name = f"round_timer_{channel}"
        try:
            schedule.removeEvent(event_name)
        except KeyError:
            pass

        schedule.addEvent(
            lambda: self._auto_next_round(irc, channel),
            time.time() + duration,
            name=event_name
        )
        game["round_timers"].append(event_name)

        # Si la partie est en pause → ne pas lancer de countdown
        if game.get("paused"):
            return

        # --- Countdown 20 / 10 / 5 ---
        countdown_events = []
        for t in (20, 10, 5):
            if duration > t:
                ev_name = f"countdown_{channel}_{t}"
                schedule.addEvent(
                    lambda s=t: irc.queueMsg(ircmsgs.privmsg(
                        channel, f"⏳ Il reste {s} secondes..."
                    )),
                    time.time() + (duration - t),
                    name=ev_name
                )
                # Messages TAGS
                self._send_event(
                    irc,
                    channel,
                    "round_countdown",
                    seconds=t
                )
                countdown_events.append(ev_name)

        game["countdown_timers"] = countdown_events

    def _endRound(self, irc, channel):
        """Passe à la manche suivante ou termine la partie proprement."""

        # Sécurité : la partie doit exister
        if channel not in self.active_games:
            return

        game = self.active_games[channel]
        game["round_active"] = False

        # Sécurité : partie stoppée
        if game.get("stopped"):
            return
            
        # --- Mise à jour des stats globales : 1 manche jouée ---
        for user in game.get("answers", {}).keys():
            self._update_global_stats(user, rounds_played=1)

        # Préparer la manche suivante
        game["round"] += 1

        # 🔥 Recharger les catégories JSON
        self.data = self._load_categories_json()

        # 🔥 Nouvelle catégorie (via JSON)
        new_categories = self._pickCategories(channel, game["round"])

        # 🔥 Nouvelle lettre intelligente
        game["letter"] = self._randomLetter(
            new_categories,
            previous=game["letter"],
            round_number=game["round"]
        )

        # Mise à jour des catégories
        game["categories"] = new_categories

        # Reset des réponses
        game["answers"] = {}

        max_rounds = game["config"]["max_rounds"]
        
        # ------------------------------------------------------------------
        # FIN DE PARTIE
        # ------------------------------------------------------------------
        if game["round"] > max_rounds:
        
            irc.queueMsg(ircmsgs.privmsg(channel,
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"))
            irc.queueMsg(ircmsgs.privmsg(channel,
                "🏆  FIN DE LA PARTIE !  🏆"))
            irc.queueMsg(ircmsgs.privmsg(channel,
                f"🎯 Vous avez atteint les {max_rounds} manches."))
            irc.queueMsg(ircmsgs.privmsg(channel,
                "📊 Voici le classement final :"))
            irc.queueMsg(ircmsgs.privmsg(channel,
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"))

            # Classement final
            partie_scores = dict(self.scoreboard)
            classement = sorted(partie_scores.items(), key=lambda x: x[1], reverse=True)

            for user, pts in classement:
                irc.queueMsg(ircmsgs.privmsg(channel,
                    f"  • {user} : {pts} point(s)"))
            irc.queueMsg(ircmsgs.privmsg(channel,
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"))

            # ------------------------------------------------------------------
            # TOP JOUEURS GLOBAUX
            # ------------------------------------------------------------------
            players = self.global_stats.get("players", {})
            if players:
                irc.queueMsg(ircmsgs.privmsg(channel,
                    "🌟 Meilleurs joueurs du serveur (global) :"))

                top_global = sorted(
                    players.items(),
                    key=lambda x: x[1].get("total_points", 0),
                    reverse=True
                )[:5]

                rank = 1
                for user, stats in top_global:
                    pts = stats.get("total_points", 0)
                    fc = stats.get("full_combos", 0)
                    rounds = stats.get("rounds_played", 0)

                    irc.queueMsg(ircmsgs.privmsg(channel,
                        f"  {rank}. {user} — {pts} pts, {fc} combos, {rounds} manches"))
                    rank += 1

                irc.queueMsg(ircmsgs.privmsg(channel,
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"))

            # ------------------------------------------------------------------
            # RECORDS GLOBAUX
            # ------------------------------------------------------------------
            global_stats = self.global_stats.get("global", {})
            players = self.global_stats.get("players", {})

            irc.queueMsg(ircmsgs.privmsg(channel, "🏆 Records du serveur :"))

            # Points cumulés
            best_total = max(players.items(), key=lambda x: x[1].get("total_points", 0))
            irc.queueMsg(ircmsgs.privmsg(channel,
                f"🏅 Plus haut score cumulé : {best_total[0]} — {best_total[1].get('total_points', 0)} pts"))

            # Full combos
            best_fc = max(players.items(), key=lambda x: x[1].get("full_combos", 0))
            irc.queueMsg(ircmsgs.privmsg(channel,
                f"🔥 Plus grand nombre de full combos : {best_fc[0]} — {best_fc[1].get('full_combos', 0)} combos"))

            # Manches jouées
            best_rounds = max(players.items(), key=lambda x: x[1].get("rounds_played", 0))
            irc.queueMsg(ircmsgs.privmsg(channel,
                f"📘 Joueur le plus actif : {best_rounds[0]} — {best_rounds[1].get('rounds_played', 0)} manches"))

            # Vitesse
            best_speed = global_stats.get("best_speed", None)
            best_speed_user = global_stats.get("best_speed_user", None)
            if best_speed and best_speed_user:
                irc.queueMsg(ircmsgs.privmsg(channel,
                    f"⚡ Record de vitesse : {best_speed_user} — {best_speed:.2f} sec"))

            irc.queueMsg(ircmsgs.privmsg(channel,
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"))

            # ------------------------------------------------------------------
            # RECORDS HEBDOMADAIRES
            # ------------------------------------------------------------------
            weekly = self.global_stats.get("weekly", {})

            irc.queueMsg(ircmsgs.privmsg(channel, "📅 Records de la semaine :"))
            
            if not weekly.get("best_score", {}).get("user") \
               and not weekly.get("best_full_combos", {}).get("user") \
               and not weekly.get("best_speed", None):
                irc.queueMsg(ircmsgs.privmsg(channel,
                    "  Aucun record enregistré pour le moment."))

            ws = weekly.get("best_score", {})
            if ws.get("user"):
                irc.queueMsg(ircmsgs.privmsg(channel,
                    f"🏅 Score hebdo : {ws['user']} — {ws['points']} pts"))

            wfc = weekly.get("best_full_combos", {})
            if wfc.get("user"):
                irc.queueMsg(ircmsgs.privmsg(channel,
                    f"🔥 Full combos hebdo : {wfc['user']} — {wfc['count']} combos"))

            wsp = weekly.get("best_speed", None)
            wsp_user = weekly.get("best_speed_user", None)
            if wsp and wsp_user:
                irc.queueMsg(ircmsgs.privmsg(channel,
                    f"⚡ Vitesse hebdo : {wsp_user} — {wsp:.2f} sec"))
                    
            # --- TAG IRCv3 : fin de partie ---
            final_scores = dict(self.scoreboard)
            classement = sorted(final_scores.items(), key=lambda x: x[1], reverse=True)

            self._send_event(
                irc,
                channel,
                "game_end",
                final_ranking=json.dumps(classement),
                top_global=json.dumps(top_global),
                records_global=json.dumps({
                    "best_total": best_total,
                    "best_fc": best_fc,
                    "best_rounds": best_rounds,
                    "best_speed": best_speed,
                    "best_speed_user": best_speed_user
                }),
                records_weekly=json.dumps({
                    "best_score": ws,
                    "best_full_combos": wfc,
                    "best_speed": wsp,
                    "best_speed_user": wsp_user
                })
            )

            irc.queueMsg(ircmsgs.privmsg(channel,
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"))

            # Message final
            irc.queueMsg(ircmsgs.privmsg(channel,
                "🎮 Vous pouvez changer le mode de jeu en  tapant !jeu <mode> (par exemple !jeu facile)."))
            irc.queueMsg(ircmsgs.privmsg(channel,
                "🎮 Retrouver la liste des modes disponibles en tapant !jeu liste"))
            irc.queueMsg(ircmsgs.privmsg(channel,
                "✏️ Vous avez une suggestion ? Tapez !suggestion <message> (par exemple !suggestion ajouter une nouvelle catégorie fleur)."))
            irc.queueMsg(ircmsgs.privmsg(channel,
                "🐞 Signaler un bug ? Tapez !bug <message>"))
            irc.queueMsg(ircmsgs.privmsg(channel,
                "🎉 Merci d'avoir joué au Petit Bac sur EntreNous.chat | Tapez !jouer pour recommencer."))
            irc.queueMsg(ircmsgs.privmsg(channel,
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"))
                
            # Mise à jour des scores cumulés
            for user, pts in partie_scores.items():
                self.scoreboard[user] = self.scoreboard.get(user, 0) + pts

            self._save_scores()

            # Sauvegarder l'historique des 10 dernières parties
            self.last_games.append({
                "timestamp": time.strftime("%d/%m/%Y %H:%M:%S"),
                "scores": partie_scores
            })
            self.last_games = self.last_games[-10:]
            self._save_last_games()

            # Supprimer la partie
            if channel in self.players:
                del self.players[channel]

            del self.active_games[channel]
            return

        # ------------------------------------------------------------------
        # Sinon → lancer la manche suivante
        # ------------------------------------------------------------------
        # --- TAG IRCv3 : fin de manche ---
        round_scores = {
            user: pts - game["score_before_round"].get(user, 0)
            for user, pts in self.scoreboard.items()
        }

        self._send_event(
            irc,
            channel,
            "round_end",
            round=game["round"] - 1,
            round_scores=json.dumps(round_scores),
            stats=json.dumps(self.global_stats.get("players", {})),
        )
        self._startRound(irc, channel)

    # ----------------------------------------------------------------------
    # Commande !jouer
    # ----------------------------------------------------------------------  
    @wrap([optional('text')])
    def jouer(self, irc, msg, args, dummy):
        """Démarre une nouvelle partie du Petit Bac. Utilisation : !jouer [regles]"""
        channel = msg.args[0]
        nick = msg.nick

        # Vérification du salon autorisé
        if not self._is_enabled(channel):
            allowed = conf.supybot.plugins.PetitBac.allowedChannel()
            irc.queueMsg(ircmsgs.notice(msg.nick,
                f"❌ Le jeu du Petit Bac n'est pas autorisé sur {channel}."))
            irc.queueMsg(ircmsgs.notice(msg.nick,
                f"➡️ Salon autorisé : {allowed}"))
            return

        # Partie déjà en cours
        if channel in self.active_games:
            game = self.active_games[channel]
            irc.queueMsg(ircmsgs.privmsg(channel,
                f"{nick}: Une partie est déjà en cours !"))
            irc.queueMsg(ircmsgs.privmsg(channel,
                f"{nick}: 🎲 Lettre actuelle : {game['letter']}"))
            irc.queueMsg(ircmsgs.privmsg(channel,
                f"{nick}: 📚 Catégories : {', '.join(game['categories'])}"))
            return

        # Nouveau comportement :
        # - par défaut : PAS de règles
        # - si l'utilisateur écrit "regles" → afficher les règles
        show_rules = (dummy and dummy.lower() in ("regles", "règles"))

        # Démarrer la partie
        self._startGame(irc, channel, nick, show_rules=show_rules)
      
    # ----------------------------------------------------------------------
    # Commande !stop
    # ----------------------------------------------------------------------
    @wrap([])
    def stop(self, irc, msg, args):
        """Arrête la partie en cours et annule tous les timers."""
        channel = msg.args[0]
        nick = msg.nick

        if not self._is_enabled(channel):
            return

        # Vérifier opérateur
        if not irc.state.channels[channel].isOp(nick):
            irc.queueMsg(ircmsgs.notice(nick,
                "❌ Vous devez être opérateur (&) pour arrêter la partie."))
            return

        # Vérifier qu'une partie existe
        game = self.active_games.get(channel)
        if not game:
            irc.queueMsg(ircmsgs.notice(nick,
                "❌ Aucune partie en cours à arrêter."))
            return

        # Marquer la partie comme stoppée
        game["stopped"] = True

        # ------------------------------------------------------------------
        # ANNULATION DE TOUS LES TIMERS
        # ------------------------------------------------------------------

        # Timers de démarrage
        for ev in game.get("start_timers", []):
            try:
                schedule.removeEvent(ev)
            except KeyError:
                pass
        game["start_timers"] = []

        # Timers de manche
        for ev in game.get("round_timers", []):
            try:
                schedule.removeEvent(ev)
            except KeyError:
                pass
        game["round_timers"] = []

        # Timers de countdown
        for ev in game.get("countdown_timers", []):
            try:
                schedule.removeEvent(ev)
            except KeyError:
                pass
        game["countdown_timers"] = []

        # Timer de pause
        pause_timer = game.get("pause_timer")
        if pause_timer:
            try:
                schedule.removeEvent(pause_timer)
            except:
                pass
        game["pause_timer"] = None

        # Timer de full combo
        fullcombo_timer = game.get("fullcombo_timer")
        if fullcombo_timer:
            try:
                schedule.removeEvent(fullcombo_timer)
            except:
                pass
        game["fullcombo_timer"] = None

        # Timer d’inactivité
        try:
            schedule.removeEvent(f"idle_{channel}")
        except KeyError:
            pass

        # Timer principal round_timer
        try:
            schedule.removeEvent(f"round_timer_{channel}")
        except KeyError:
            pass

        # ------------------------------------------------------------------
        # Arrêt propre de la partie
        # ------------------------------------------------------------------
        self._stopGame(irc, channel, from_manual=True)

    def _stopGame(self, irc, channel, from_manual=False):
        """Arrête proprement la manche ou la partie, en annulant tous les timers."""

        # --- Sécurité : la partie doit exister ---
        game = self.active_games.get(channel)
        if not game:
            return

        # --- Marquer la partie comme stoppée uniquement si stop manuel ---
        if from_manual:
            game["stopped"] = True

        # ----------------------------------------------------------------------
        # ANNULATION DE TOUS LES TIMERS
        # ----------------------------------------------------------------------
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

        pause_timer = game.get("pause_timer")
        if pause_timer:
            try: schedule.removeEvent(pause_timer)
            except: pass
        game["pause_timer"] = None

        fullcombo_timer = game.get("fullcombo_timer")
        if fullcombo_timer:
            try: schedule.removeEvent(fullcombo_timer)
            except: pass
        game["fullcombo_timer"] = None

        try: schedule.removeEvent(f"idle_{channel}")
        except KeyError: pass

        try: schedule.removeEvent(f"round_timer_{channel}")
        except KeyError: pass

        # ----------------------------------------------------------------------
        # CAS 1 : STOP MANUEL
        # ----------------------------------------------------------------------
        if from_manual:
            irc.queueMsg(ircmsgs.privmsg(channel, "🛑 Partie arrêtée par un opérérateur."))
            if channel in self.players:
                del self.players[channel]
            del self.active_games[channel]
            return

        # ----------------------------------------------------------------------
        # CAS 2 : AUCUNE RÉPONSE
        # ----------------------------------------------------------------------
        if not game["answers"]:
            game["idle_rounds"] = game.get("idle_rounds", 0) + 1
            max_idle = conf.supybot.plugins.PetitBac.maxIdleRounds()

            if game["idle_rounds"] >= max_idle:
                irc.queueMsg(ircmsgs.privmsg(channel,
                    f"💤 Jeu arrêté pour inactivité ({max_idle} manches sans réponse)."))
                if channel in self.players:
                    del self.players[channel]
                del self.active_games[channel]
                return

            no_answer_msgs = self.messages.get("no_answer", [])
            if no_answer_msgs:
                irc.queueMsg(ircmsgs.privmsg(channel, random.choice(no_answer_msgs)))
            else:
                irc.queueMsg(ircmsgs.privmsg(channel, "😴 Aucun mot reçu !"))
                    
            self._endRound(irc, channel)
            return

        # ----------------------------------------------------------------------
        # CAS 3 : FIN NORMALE DE MANCHE (avec réponses)
        # ----------------------------------------------------------------------
        # Calcul du vrai score de la manche
        round_scores = {}
        before = game.get("score_before_round", {})

        for user in game["answers"].keys():
            key = user.lower()
            old = before.get(key, 0)
            new = self.scoreboard.get(key, 0)
            pts = new - old
            round_scores[user] = pts

        # ----------------------------------------------------------------------
        # AFFICHAGE DES SCORES (pas de modification du scoreboard)
        # ----------------------------------------------------------------------
        for user, pts in round_scores.items():
            irc.queueMsg(ircmsgs.privmsg(channel,
                f"Résultat pour {user} : {pts} point(s)"))

        classement = sorted(self.scoreboard.items(), key=lambda x: x[1], reverse=True)

        # 🔥 Message fun de fin de manche
        end_msgs = self.messages.get("end_round", [])
        if end_msgs:
            irc.queueMsg(ircmsgs.privmsg(channel, random.choice(end_msgs)))
        else:
            irc.queueMsg(ircmsgs.privmsg(channel, "🏁 Fin de manche !"))

        irc.queueMsg(ircmsgs.privmsg(channel, "📊 Scores cumulés :"))
        for user, pts in classement:
            irc.queueMsg(ircmsgs.privmsg(channel, f"{user} : {pts} point(s)"))

        self._endRound(irc, channel)

    # ----------------------------------------------------------------------
    # Timer round
    # ----------------------------------------------------------------------
    def _start_round_timer(self, irc, channel):
        duration = conf.supybot.plugins.PetitBac.roundDuration()

        # Nom unique pour ce salon
        event_name = f"round_timer_{channel}"

        # Annuler un éventuel ancien timer
        try:
            schedule.removeEvent(event_name)
        except KeyError:
            pass

        # Créer le nouveau timer
        schedule.addEvent(
            lambda: self._auto_next_round(irc, channel),
            time.time() + duration,
            name=event_name
        )

    def _auto_next_round(self, irc, channel):
        game = self.active_games.get(channel)
        if not game:
            return

        if not self._is_enabled(channel):
            return

        if game.get("paused"):
            return

        # Stopper la manche actuelle
        self._stopGame(irc, channel)

        # Si la partie a été arrêtée (inactivité, stop manuel)
        if channel not in self.active_games:
            return
             
    def _startRoundCountdown(self, irc, channel, duration):
        """Démarre le compte à rebours d'une manche, avec sécurité totale."""

        # --- Sécurité : la partie doit exister ---
        if channel not in self.active_games:
            return

        game = self.active_games[channel]

        # --- Sécurité : la partie est stoppée ---
        if game.get("stopped"):
            return

        # --- Sécurité : la partie est en pause ---
        if game.get("paused"):
            return

        # --- Annuler d'anciens countdowns ---
        for ev in game.get("countdown_timers", []):
            try:
                schedule.removeEvent(ev)
            except KeyError:
                pass
        game["countdown_timers"] = []

        countdown_events = []

        # Fonction interne sécurisée
        def countdown_step(seconds_left):
            # Vérifier que la partie existe encore
            if channel not in self.active_games:
                return

            game = self.active_games[channel]

            # Ne rien faire si la partie est stoppée ou en pause
            if game.get("stopped") or game.get("paused"):
                return

            irc.queueMsg(ircmsgs.privmsg(channel, f"⏳ Il reste {seconds_left} secondes..."))

        # Planifier les messages du countdown
        for t in (20, 10, 5):
            if duration > t:
                ev_name = f"countdown_{channel}_{t}"
                schedule.addEvent(
                    lambda s=t: countdown_step(s),
                    time.time() + (duration - t),
                    name=ev_name
                )
                countdown_events.append(ev_name)

        # Sauvegarder les timers
        game["countdown_timers"] = countdown_events
    
    def _check_full_combo(self, irc, channel, nick):
        game = self.active_games.get(channel)
        if not game:
            return

        nick_key = nick.lower()

        # Pas encore toutes les catégories
        if len(game["answers"].get(nick_key, {})) != len(game["categories"]):
            return

        # 🔥 FULL COMBO !
        full_msgs = self.messages.get("full_combo", [])
        if full_msgs:
            phrase = random.choice(full_msgs)
        else:
            phrase = "🔥 FULL COMBO ! Toutes les catégories validées !"

        irc.queueMsg(ircmsgs.privmsg(channel, "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"))
        irc.queueMsg(ircmsgs.privmsg(channel, f"{phrase}"))
        irc.queueMsg(ircmsgs.privmsg(channel, f"🎯 Bravo {nick.upper()} !"))
        irc.queueMsg(ircmsgs.privmsg(channel, "💥 Bonus +1 point attribué !"))
        irc.queueMsg(ircmsgs.privmsg(channel, "🏁 Fin immédiate de la manche !"))
        irc.queueMsg(ircmsgs.privmsg(channel, "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"))

        # Bonus
        self.scoreboard[nick_key] = self.scoreboard.get(nick_key, 0) + 1

        # Stats globales
        elapsed = time.time() - game.get("round_start_time", time.time())
        elapsed += game.get("speed_offset", 0)

        self._update_global_stats(
            nick_key,
            full_combos=1,
            speed_time=elapsed,
            irc=irc,
            channel=channel,
            announce=False
        )

        # Nettoyage ancien timer
        old_fc = game.get("fullcombo_timer")
        if old_fc:
            try:
                schedule.removeEvent(old_fc)
            except:
                pass

        # Timer fin de manche
        event_name = f"fullcombo_{channel}"
        schedule.addEvent(
            lambda: self._safe(irc, lambda: self._stopGame(irc, channel, from_manual=False)),
            time.time() + 4,
            name=event_name
        )
        game["fullcombo_timer"] = event_name

    # ----------------------------------------------------------------------
    # Commande !manche
    # ----------------------------------------------------------------------
    @wrap([])
    def manche(self, irc, msg, args):
        """Affiche les informations de la manche en cours (lettre, catégories, numéro)."""
        channel = msg.args[0]
        nick = msg.nick

        game = self.active_games.get(channel)
        if not game:
            irc.queueMsg(ircmsgs.privmsg(channel, "❌ Aucune partie en cours."))
            return

        # Recharger les catégories JSON pour cohérence
        self.data = self._load_categories_json()

        # Vérifier si les catégories existent encore dans le JSON
        cats_display = []
        for cat in game["categories"]:
            if cat in self.data["categories"]:
                cats_display.append(cat)
            else:
                cats_display.append(f"{cat} (⚠ supprimée)")

        # -----------------------------
        # 1) Messages visibles pour IRC
        # -----------------------------
        irc.queueMsg(ircmsgs.notice(nick, f"🧮 Manche : {game['round']}"))
        irc.queueMsg(ircmsgs.notice(nick, f"📚 Catégories actuelles : {', '.join(cats_display)}"))
        irc.queueMsg(ircmsgs.notice(nick, f"🎲 Lettre actuelle : {game['letter']}"))

    # ---------------------------------------------------------------------------------
    # Commande !pause
    # ---------------------------------------------------------------------------------
    @wrap([])
    def pause(self, irc, msg, args):
        """ Commande !pause """
        
        channel = msg.args[0]
        nick = msg.nick

        if not irc.state.channels[channel].isOp(nick):
            irc.queueMsg(ircmsgs.notice(nick,
                "❌ Vous devez être opérateur (&) pour mettre la partie en pause."))
            return

        game = self.active_games.get(channel)
        if not game:
            irc.queueMsg(ircmsgs.privmsg(channel, "⏸️ Aucune partie en cours à mettre en pause."))
            return

        if game.get("paused"):
            irc.queueMsg(ircmsgs.privmsg(channel,
                f"⏸️ La partie est déjà en pause (mise en pause par {game.get('paused_by')})."))
            return

        self._do_pause(irc, channel, paused_by=nick)

        irc.queueMsg(ircmsgs.privmsg(channel,
            f"⏸️ Partie mise en pause par {nick}. ( un opérateur doit utiliser !reprendre pour relancer la manche en cour )"))

    # ----------------------------------------------------------------------
    # Fonction interne : mettre la partie en pause (sans vérification opérateur)
    # ----------------------------------------------------------------------
    def _do_pause(self, irc, channel, paused_by="système"):
        game = self.active_games.get(channel)
        if not game:
            return

        # Annuler les countdowns
        for ev in game.get("countdown_timers", []):
            try:
                schedule.removeEvent(ev)
            except KeyError:
                pass
        game["countdown_timers"] = []
        
        # Annuler les countdowns de reprise (3,2,1,GO)
        for t in (1, 2, 3):
            try:
                schedule.removeEvent(f"resume_countdown_{channel}_{t}")
            except KeyError:
                pass

        try:
            schedule.removeEvent(f"resume_go_{channel}")
        except KeyError:
            pass
            
        # Annuler les timers de manche
        for ev in game.get("round_timers", []):
            try:
                schedule.removeEvent(ev)
            except KeyError:
                pass
        game["round_timers"] = []

        # Timer principal
        try:
            schedule.removeEvent(f"round_timer_{channel}")
        except KeyError:
            pass

        # Timer d’inactivité
        try:
            schedule.removeEvent(f"idle_{channel}")
        except KeyError:
            pass

        # Timer pause existant
        if game.get("pause_timer"):
            try:
                schedule.removeEvent(game["pause_timer"])
            except:
                pass
        game["pause_timer"] = None

        # Timer full combo
        if game.get("fullcombo_timer"):
            try:
                schedule.removeEvent(game["fullcombo_timer"])
            except:
                pass
        game["fullcombo_timer"] = None

        # Calcul du temps restant
        duration = conf.supybot.plugins.PetitBac.roundDuration()
        start_time = game.get("round_start_time")

        if start_time:
            elapsed = time.time() - start_time
            remaining = max(1, int(duration - elapsed))
        else:
            remaining = duration

        game["time_left"] = remaining

        # Marquer la pause
        game["paused"] = True
        game["paused_by"] = paused_by

        # Timer d’expiration
        def auto_stop():
            if channel in self.active_games and self.active_games[channel].get("paused"):
                irc.queueMsg(ircmsgs.privmsg(channel,
                    "⏳ La partie en pause a expiré après 5 minutes. Elle est maintenant arrêtée."))
                del self.active_games[channel]

        pause_event = f"pause_expire_{channel}"
        schedule.addEvent(auto_stop, time.time() + 300, name=pause_event)
        game["pause_timer"] = pause_event

    # -----------------------------------------------------------------------------------------
    # Commande !reprendre ( si une partie est en pause )
    # -----------------------------------------------------------------------------------------
    @wrap([])
    def reprendre(self, irc, msg, args):
        """ Commande !reprendre """
        
        channel = msg.args[0]
        nick = msg.nick

        if not irc.state.channels[channel].isOp(nick):
            irc.queueMsg(ircmsgs.notice(nick,
                "❌ Vous devez être opérateur (&) pour relancer la partie."))
            return

        game = self.active_games.get(channel)
        if not game or not game.get("paused"):
            irc.queueMsg(ircmsgs.privmsg(channel, "▶️ Aucune partie en pause à relancer."))
            return

        self._do_resume(irc, channel)

    # ----------------------------------------------------------------------
    # Fonction interne : reprendre la partie (sans vérification opérateur)
    # ----------------------------------------------------------------------
    def _do_resume(self, irc, channel):
        game = self.active_games.get(channel)
        if not game or not game.get("paused"):
            return

        if game.get("pause_timer"):
            try:
                schedule.removeEvent(game["pause_timer"])
            except:
                pass
        game["pause_timer"] = None

        game["paused"] = False

        time_left = game.get("time_left", conf.supybot.plugins.PetitBac.roundDuration())
        MIN_TIME = 5

        if time_left < MIN_TIME:
            irc.queueMsg(ircmsgs.privmsg(channel,
                f"⏳ Impossible de reprendre : il ne reste que {time_left} seconde(s)."))
            irc.queueMsg(ircmsgs.privmsg(channel,
                "🏁 La manche est automatiquement terminée."))
            self._stopGame(irc, channel, from_manual=False)
            return

        # Nettoyage countdowns
        for t in (20, 10, 5):
            try:
                schedule.removeEvent(f"countdown_{channel}_{t}")
            except KeyError:
                pass

        irc.queueMsg(ircmsgs.privmsg(channel, "▶️ Partie relancée !"))
        max_rounds = conf.supybot.plugins.PetitBac.maxRounds()
        irc.queueMsg(ircmsgs.privmsg(channel,
            f"📊 Manche {game['round']} / {max_rounds}"))
        irc.queueMsg(ircmsgs.privmsg(channel,
            f"⏳ Temps restant : {time_left} seconde(s)"))
        irc.queueMsg(ircmsgs.privmsg(
            channel,
            f"🎲 Lettre : \x02\x0304{game['letter']}\x0F  |  "
            f"📚 Catégories : \x02\x0303{', '.join(game['categories'])}\x0F"
        ))

        game.setdefault("countdown_timers", [])

        delay = 0
        for n in [3, 2, 1]:
            ev_name = f"resume_countdown_{channel}_{n}"
            schedule.addEvent(
                lambda x=n: irc.queueMsg(ircmsgs.privmsg(channel, f"⏳ Reprise dans {x}...")),
                time.time() + delay,
                name=ev_name
            )
            game["countdown_timers"].append(ev_name)
            delay += 1

        ev_name = f"resume_go_{channel}"
        schedule.addEvent(
            lambda: irc.queueMsg(ircmsgs.privmsg(channel, "🚀 GO !")),
            time.time() + delay,
            name=ev_name
        )
        game["countdown_timers"].append(ev_name)

        resume_time = time.time() + delay

        # Timer principal
        event_name = f"round_timer_{channel}"
        try:
            schedule.removeEvent(event_name)
        except KeyError:
            pass

        schedule.addEvent(
            lambda: self._auto_next_round(irc, channel),
            resume_time + time_left,
            name=event_name
        )

        game.setdefault("round_timers", [])
        game["round_timers"].append(event_name)

        for t in (20, 10, 5):
            if time_left > t:
                ev_name = f"countdown_{channel}_{t}"
                schedule.addEvent(
                    lambda s=t: irc.queueMsg(ircmsgs.privmsg(channel, f"⏳ Il reste {s} secondes...")),
                    resume_time + (time_left - t),
                    name=ev_name
                )
                game["countdown_timers"].append(ev_name)

        duration = conf.supybot.plugins.PetitBac.roundDuration()

        # Correction : offset + start_time correct
        game["speed_offset"] = duration - time_left
        game["round_start_time"] = resume_time
