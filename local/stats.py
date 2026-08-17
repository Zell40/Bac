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

class StatsMixin:

    # ----------------------------------------------------------------------
    # Commande !scores
    # ----------------------------------------------------------------------
    @wrap([optional('text')])
    def scores(self, irc, msg, args, sub):
        """Affiche les scores cumulés et l'historique des 10 dernières parties."""
        channel = msg.args[0]
        nick = msg.nick

        # Sous-commande RESET
        if sub and sub.lower() == "reset":
            if not irc.state.channels[channel].isOp(nick):
                irc.queueMsg(ircmsgs.notice(nick,
                    "❌ Vous devez être opérateur (&) pour réinitialiser les scores."))
                return

            self.scoreboard = {}
            self._save_scores()
            irc.queueMsg(ircmsgs.notice(nick, "🔄 Scores cumulés remis à zéro."))
            return

        # ------------------------------------------------------------------
        # Résumé global
        # ------------------------------------------------------------------

        # Compter les joueurs ayant participé à au moins une partie
        joueurs = set()
        for game in self.last_games:
            joueurs.update(game["scores"].keys())

        total_joueurs = len(joueurs)
        total_parties = len(self.last_games)

        irc.queueMsg(ircmsgs.notice(nick,
            f"📈 Résumé global : {total_parties} partie(s) enregistrée(s), "
            f"{total_joueurs} joueur(s) au total."
        ))

        # ------------------------------------------------------------------
        # Scores cumulés
        # ------------------------------------------------------------------
        if not self.scoreboard:
            irc.queueMsg(ircmsgs.notice(nick, "📊 Aucun score pour le moment."))
        else:
            classement = sorted(self.scoreboard.items(), key=lambda x: x[1], reverse=True)

            irc.queueMsg(ircmsgs.notice(nick, "📊 Classement général :"))

            # Classement coloré
            couleurs = ["\x0303", "\x0307", "\x0302"]  # vert, orange, bleu
            for i, (user, pts) in enumerate(classement, start=1):
                couleur = couleurs[(i - 1) % len(couleurs)]
                irc.queueMsg(ircmsgs.notice(
                    nick,
                    f"  {couleur}{i}. {user} : {pts} point(s)\x03"
                ))

        # ------------------------------------------------------------------
        # Historique des 10 dernières parties (du plus récent au plus ancien)
        # ------------------------------------------------------------------
        if not self.last_games:
            irc.queueMsg(ircmsgs.notice(nick, "📘 Aucun historique de parties."))
            return

        irc.queueMsg(ircmsgs.notice(nick, "📘 Historique des 10 dernières parties :"))

        for idx, game in enumerate(reversed(self.last_games), start=1):
            irc.queueMsg(ircmsgs.notice(
                nick,
                f"  Partie {idx} — {game['timestamp']}"
            ))

            sorted_scores = sorted(game["scores"].items(), key=lambda x: x[1], reverse=True)

            # Affichage compact
            ligne = "    "
            for user, pts in sorted_scores:
                ligne += f"{user}({pts})  "

            irc.queueMsg(ircmsgs.notice(nick, ligne.strip()))

    # ----------------------------------------------------------------------
    # Commande !stat
    # ----------------------------------------------------------------------
    @wrap([optional('text')])
    def stat(self, irc, msg, args, target):
        """Affiche les statistiques globales du jeu ou celles d'un joueur. Utilisation : !stat [pseudo]"""

        channel = msg.args[0]

        players = self.global_stats.get("players", {})
        global_stats = self.global_stats.get("global", {})

        # --- 1) AUCUN ARGUMENT → STATS GLOBALES ---
        if not target:
            irc.queueMsg(ircmsgs.privmsg(channel, "📊 Statistiques globales du Petit Bac :"))
            irc.queueMsg(ircmsgs.privmsg(channel, f"  • Parties jouées : {global_stats.get('games_played', 0)}"))
            irc.queueMsg(ircmsgs.privmsg(channel, f"  • Manches jouées : {global_stats.get('rounds_played', 0)}"))
            irc.queueMsg(ircmsgs.privmsg(channel, f"  • Mots validés : {global_stats.get('words_validated', 0)}"))
            irc.queueMsg(ircmsgs.privmsg(channel, f"  • Full combos : {global_stats.get('full_combos', 0)}"))
            irc.queueMsg(ircmsgs.privmsg(channel, f"  • Points cumulés : {global_stats.get('total_points', 0)}"))
            irc.queueMsg(ircmsgs.privmsg(channel, f"  • Dernière activité : {global_stats.get('last_activity', 'N/A')}"))
            return

        # --- 2) AVEC ARGUMENT → STATS DU JOUEUR ---
        key = target.strip().lower()

        if key not in players:
            irc.queueMsg(ircmsgs.privmsg(channel,
                f"❌ Aucun historique trouvé pour « {target} »."))
            return

        stats = players[key]

        irc.queueMsg(ircmsgs.privmsg(channel, f"📊 Statistiques globales pour {target} :"))
        irc.queueMsg(ircmsgs.privmsg(channel, f"  • Parties jouées : {stats['games_played']}"))
        irc.queueMsg(ircmsgs.privmsg(channel, f"  • Manches jouées : {stats['rounds_played']}"))
        irc.queueMsg(ircmsgs.privmsg(channel, f"  • Mots validés : {stats['words_validated']}"))
        irc.queueMsg(ircmsgs.privmsg(channel, f"  • Full combos : {stats['full_combos']}"))
        irc.queueMsg(ircmsgs.privmsg(channel, f"  • Points cumulés : {stats['total_points']}"))
        irc.queueMsg(ircmsgs.privmsg(channel, f"  • Dernière activité : {stats['last_seen']}"))

    # ----------------------------------------------------------------------
    # Commande !top
    # ----------------------------------------------------------------------
    @wrap([optional('int')])
    def top(self, irc, msg, args, limit):
        """Affiche le classement global des joueurs. Utilisation : !top [nombre]"""

        channel = msg.args[0]

        if not limit:
            limit = 5  # valeur par défaut

        players = self.global_stats.get("players", {})

        if not players:
            irc.queueMsg(ircmsgs.privmsg(channel, "📊 Aucun joueur enregistré pour le moment."))
            return

        # Tri par total_points décroissant
        classement = sorted(
            players.items(),
            key=lambda x: x[1].get("total_points", 0),
            reverse=True
        )

        irc.queueMsg(ircmsgs.privmsg(channel, f"🏆 Top {limit} joueurs du Petit Bac :"))

        for i, (user, stats) in enumerate(classement[:limit], start=1):
            pts = stats.get("total_points", 0)
            fc = stats.get("full_combos", 0)
            irc.queueMsg(ircmsgs.privmsg(channel,
                f"  {i}. {user} — {pts} pts ({fc} full combos)"))
    
    # -------------------------------------------------------------------------
    # Commande !info
    # -------------------------------------------------------------------------
    def _send_long_message(self, irc, channel, text):
        max_len = 350
        while len(text) > max_len:
            part = text[:max_len]
            irc.queueMsg(ircmsgs.privmsg(channel, part))
            text = text[max_len:]
        if text:
            irc.queueMsg(ircmsgs.privmsg(channel, text))
