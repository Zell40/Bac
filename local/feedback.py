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

class FeedbackMixin:
  
    # -------------------------------------------------------------------------
    # !suggestion <message> pour améliorer le bot
    # -------------------------------------------------------------------------
    @wrap(['text'])
    def suggestion(self, irc, msg, args, message):
        """Propose une amélioration ou une idée pour le jeu.
        Réservé aux joueurs enregistrés.
        Usage : !suggestion <message>
        """
        nick = msg.nick.lower()

        # Vérifie si le joueur existe
        if nick not in self.global_stats.get("players", {}):
            irc.reply("❌ Vous devez avoir déjà joué au moins une partie pour proposer une suggestion.", notice=True)
            return

        # Fichier de stockage
        file_path = os.path.join(self.storageDir, "suggestions.json")

        # Chargement
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = []

        # Déterminer le prochain ID
        if data and "id" in data[-1]:
            new_id = data[-1]["id"] + 1
        else:
            new_id = 1

        # Ajout
        entry = {
            "id": new_id,
            "user": nick,
            "message": message,
            "timestamp": time.strftime("%d/%m/%Y %H:%M:%S")
        }
        data.append(entry)

        # Sauvegarde
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

        irc.reply("💡 Merci ! Votre suggestion a bien été enregistrée.", notice=True)
        
        # Notification BotServ 
        dev_channel = self.registryValue('devChannel', msg.channel)

        # Vérifier que le salon est configuré
        if dev_channel and dev_channel.startswith("#"):
            irc.queueMsg(ircmsgs.privmsg(
                "BotServ",
                f"say {dev_channel} [BAC][SUGGESTION] {msg.nick}: {message}"
            ))
        else:
            irc.queueMsg(ircmsgs.notice(
                msg.nick,
                "ℹ️ Suggestion enregistrée, mais aucun salon DEV n'est configuré pour la notification."
            ))

    # -------------------------------------------------------------------------
    # !bug <message> pour signaler un bug
    # -------------------------------------------------------------------------
    @wrap(['text'])
    def bug(self, irc, msg, args, message):
        """Signale un bug ou un comportement anormal.
        Réservé aux joueurs enregistrés.
        Usage : !bug <message>
        """
        nick = msg.nick.lower()

        # Vérifie si le joueur existe
        if nick not in self.global_stats.get("players", {}):
            irc.reply("❌ Vous devez avoir déjà joué au moins une partie pour signaler un bug.", notice=True)
            return

        # Fichier de stockage
        file_path = os.path.join(self.storageDir, "bugs.json")

        # Chargement
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = []

        # Déterminer le prochain ID
        if data and "id" in data[-1]:
            new_id = data[-1]["id"] + 1
        else:
            new_id = 1

        # Ajout
        entry = {
            "id": new_id,
            "user": nick,
            "message": message,
            "timestamp": time.strftime("%d/%m/%Y %H:%M:%S")
        }
        data.append(entry)

        # Sauvegarde
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

        irc.reply("🐞 Merci ! Le bug a été signalé et sera examiné.", notice=True)
        
        # Notification BotServ 
        dev_channel = self.registryValue('devChannel', msg.channel)

        # Vérifier que le salon est configuré
        if dev_channel and dev_channel.startswith("#"):
            irc.queueMsg(ircmsgs.privmsg(
                "BotServ",
                f"say {dev_channel} [BAC][BUG] {msg.nick}: {message}"
            ))
        else:
            irc.queueMsg(ircmsgs.notice(
                msg.nick,
                "ℹ️ Bug enregistré, mais aucun salon DEV n'est configuré pour la notification."
            ))
