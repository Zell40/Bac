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

class VerifMixin:
        
    # -------------------------------------------------------------------
    # Fonctions utilitaire 
    # -------------------------------------------------------------------
    # Envoyer les messages au format ircv3 Messages-Tags
    def _send_event(self, irc, channel, event_name, **payload):
        """
        Envoie un événement structuré en TAGMSG
        sans impacter les clients texte.

        InspIRCd refuse les lignes trop longues (numeric 417).
        On plafonne donc la taille avant envoi.
        """
        tags = {
            "+pb": "v1",
            "+ev": str(event_name),
        }

        for k, v in payload.items():
            tags[f"+{k}"] = str(v)

        msg = ircmsgs.IrcMsg(
            command="TAGMSG",
            args=(channel,),
            server_tags=tags
        )
        encoded = str(msg).encode("utf-8")
        # Marge sous la limite IRC classique (512) / InspIRCd 417
        if len(encoded) > 450:
            log.warning(
                "PetitBac: TAGMSG %s trop long (%d octets), non envoyé",
                event_name, len(encoded)
            )
            return False

        irc.queueMsg(msg)
        return True

    def _compact_score_pairs(self, pairs, limit=10):
        if isinstance(pairs, dict):
            pairs = pairs.items()
        parts = []
        for item in list(pairs)[:limit]:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                pts = float(item[1] or 0)
                pts_s = str(int(pts)) if pts == int(pts) else ('%.1f' % pts)
                parts.append("%s:%s" % (item[0], pts_s))
            elif isinstance(item, dict):
                nick = item.get("nick") or item.get("user") or ""
                pts = item.get("pts") or item.get("points") or 0
                if nick:
                    parts.append("%s:%s" % (nick, int(pts)))
        return ",".join(parts)

    def _is_quiet(self, channel, network=None):
        return bool(self.registryValue('quietChannel', channel, network))

    def _chan_say(self, irc, channel, text, essential=False):
        """PRIVMSG salon. Si quietChannel, n'envoie que les messages essentiels."""
        if essential or not self._is_quiet(channel):
            irc.queueMsg(ircmsgs.privmsg(channel, text))

    def _seconds_left(self, game):
        if game.get("paused"):
            return max(0, int(game.get("time_left") or 0))
        start = game.get("round_start_time") or 0
        duration = (game.get("config") or {}).get("duration") or 0
        if not start or not duration:
            return 0
        return max(0, int(duration - (time.time() - start)))

    def _game_ui_phase(self, game):
        if not game:
            return "idle"
        if game.get("paused"):
            return "paused"
        if game.get("round_active"):
            return "playing"
        return "starting"

    def _send_state_sync(self, irc, channel):
        game = self.active_games.get(channel)
        if not game:
            return
        cfg = game.get("config") or {}
        self._send_event(
            irc,
            channel,
            "state_sync",
            phase=self._game_ui_phase(game),
            round=game.get("round", 0),
            letter=game.get("letter") or "",
            categories=",".join(game.get("categories") or []),
            seconds_left=self._seconds_left(game),
            duration=cfg.get("duration", 0),
            max_rounds=cfg.get("max_rounds", 0),
            paused="1" if game.get("paused") else "0",
            mode=game.get("mode") or self.current_mode.get(channel, ""),
        )

    def _word_ok(self, irc, channel, nick, word, category, points):
        self._send_event(
            irc, channel, "word_ok",
            nick=nick, word=word, category=category, points=points,
        )

    def _word_ko(self, irc, channel, nick, word, reason, category=""):
        payload = {"nick": nick, "word": word, "reason": reason}
        if category:
            payload["category"] = category
        self._send_event(irc, channel, "word_ko", **payload)

    def verif_list(self, irc, msg, args):
        channel = msg.args[0]

        if not self.pending_verifications:
            irc.queueMsg(ircmsgs.privmsg(channel, "📭 Aucun mot en attente de validation."))
            return

        irc.queueMsg(ircmsgs.privmsg(channel, "📋 Mots en attente :"))

        # Tri par ID numérique
        for vid, data in sorted(self.pending_verifications.items(), key=lambda x: int(x[0])):
            mot = data["mot"]
            cat = data["categorie"]
            auteur = data["auteur"]
            ts = datetime.datetime.fromtimestamp(data["timestamp"]).strftime("%Y-%m-%d %H:%M")

            # Vérifier si la catégorie existe encore dans le JSON
            if cat not in self.data["categories"]:
                cat_display = f"{cat} (⚠ supprimée)"
            else:
                cat_display = cat

            irc.queueMsg(ircmsgs.privmsg(
                channel,
                f"  • ID {vid} — « {mot} » → {cat_display} (par {auteur}, {ts})"
            ))
                
    def verif_ok(self, irc, msg, args, vid):
        channel = msg.args[0]

        if vid not in self.pending_verifications:
            irc.queueMsg(ircmsgs.privmsg(channel, "ID inconnu."))
            return

        entry = self.pending_verifications[vid]
        mot = entry["mot"].lower()
        categorie = entry["categorie"]

        # 🔥 Ajouter le mot dans la catégorie JSON
        if categorie not in self.data["categories"]:
            irc.queueMsg(ircmsgs.privmsg(channel,
                f"❌ La catégorie « {categorie} » n'existe plus dans la base JSON."))
            return

        self.data["categories"][categorie]["mots"].add(mot)

        # 🔥 Mettre à jour multicatégories si nécessaire
        existing_cats = [
            c for c, content in self.data["categories"].items()
            if mot in content["mots"]
        ]
        if len(existing_cats) > 1:
            self.data["multicat"][mot] = existing_cats

        # 🔥 Sauvegarde JSON
        self._save_categories_json()

        # 🔥 Retirer de la liste d'attente
        del self.pending_verifications[vid]
        self._save_pending_verifications()

        irc.queueMsg(ircmsgs.privmsg(channel,
            f"✔ Le mot « {mot} » a été validé et ajouté à la catégorie « {categorie} »."))

    def verif_del(self, irc, msg, args, vid):
        channel = msg.args[0]

        if vid not in self.pending_verifications:
            irc.queueMsg(ircmsgs.privmsg(channel, "ID inconnu."))
            return

        del self.pending_verifications[vid]
        self._save_pending_verifications()

        irc.queueMsg(ircmsgs.privmsg(channel, "🗑 Mot supprimé de la liste."))

    def _notify_ops(self, irc, channel, message):
        """Envoie une notice à tous les opérateurs du salon."""
        for user in irc.state.channels[channel].users:
            if irc.state.channels[channel].isOp(user):
                irc.queueMsg(ircmsgs.notice(user, message))
                
    def _schedule_daily_pending_check(self, irc, channel):
        """Planifie une vérification quotidienne des mots en attente."""
        event_name = f"pending_check_{channel}"

        # Annuler un ancien timer si présent
        try:
            schedule.removeEvent(event_name)
        except KeyError:
            pass

        # Planifier dans 24h
        schedule.addEvent(
            lambda: self._safe(irc, lambda: self._daily_pending_check(irc, channel)),
            time.time() + 24*3600,
            name=event_name
        )

    def _daily_pending_check(self, irc, channel):
        """Envoie une alerte quotidienne si des mots sont en attente de validation."""

        pending = self.pending_verifications  # liste globale

        if pending:
            count = len(pending)

            # Notice aux opérateurs du salon
            self._notify_ops(irc, channel,
                f"⏳ {count} mot(s) sont toujours en attente de validation.")

            # Notification BotServ si devChannel configuré
            dev_channel = self.registryValue('devChannel', channel)
            if dev_channel and dev_channel.startswith("#"):
                irc.queueMsg(ircmsgs.privmsg(
                    "BotServ",
                    f"say {dev_channel} [BAC][ALERTE VERIFICATION] {count} mot(s) toujours en attente de validation."
                ))

        # Replanifier pour demain
        self._schedule_daily_pending_check(irc, channel)
    
    # Traitement des messages en attente pour remercier les utilisateurs
    def _reward_user(self, nick, mot, points):
        nick_key = nick.lower()

        # Appliquer les points immédiatement
        self.scoreboard[nick_key] = self.scoreboard.get(nick_key, 0) + points

        # Stocker pour message global
        entry = self.pending_user_messages.get(nick_key, {"points": 0, "words": []})
        entry["points"] += points
        entry["words"].append(mot)

        self.pending_user_messages[nick_key] = entry

        self._save_json(
            os.path.join(self.storageDir, "pending_user_messages.json"),
            self.pending_user_messages
        )
        
    # Envoi du message global pour remercier les utilisateurs
    def _send_reward_summary(self, irc, channel, nick):
        nick_key = nick.lower()

        if nick_key not in self.pending_user_messages:
            return

        entry = self.pending_user_messages[nick_key]
        points = entry["points"]
        words = entry["words"]

        mots_list = ", ".join(f"« {w} »" for w in words)
        count = len(words)

        irc.queueMsg(ircmsgs.notice(
            nick,
            f"🎉 Merci pour tes contributions ! {count} mot(s) validés : {mots_list} (+{points} points)."
        ))

        # Nettoyer
        del self.pending_user_messages[nick_key]
        self._save_json(
            os.path.join(self.storageDir, "pending_user_messages.json"),
            self.pending_user_messages
        )
