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

from .events_mixin import EventsMixin
from .game_mixin import GameMixin
from .words_mixin import WordsMixin
from .vote_mixin import VoteMixin
from .stats_mixin import StatsMixin
from .verif_mixin import VerifMixin
from .feedback_mixin import FeedbackMixin
from .api_mixin import ApiMixin
from .persistence_mixin import PersistenceMixin


class PetitBac(EventsMixin, GameMixin, WordsMixin, VoteMixin, StatsMixin, VerifMixin, FeedbackMixin, ApiMixin, PersistenceMixin, callbacks.Plugin):
    """Jeu du Petit Bac avec catégories, timers et auto-start."""
    
    threaded = True

    def __init__(self, irc):
        super().__init__(irc)
        self.__parent = super(PetitBac, self)
        self.__parent.__init__(irc)

        import os
        from supybot import conf

        # 📁 Dossier data local au plugin
        base_dir = os.path.dirname(__file__)
        self.storageDir = os.path.join(base_dir, "data")
        if not os.path.exists(self.storageDir):
            os.makedirs(self.storageDir)

        # Jeux actifs : channel → dict
        self.active_games = {}

        # Mots en attente de validation
        self.pending_verifications = self._load_pending_verifications()
        # Message en attente pour l'auteur des mots
        self.pending_user_messages = self._load_json(
            os.path.join(self.storageDir, "pending_user_messages.json")
        )
        if not isinstance(self.pending_user_messages, dict):
            self.pending_user_messages = {}
        
        # Planifier la vérification quotidienne pour le salon autorisé
        allowed = conf.supybot.plugins.PetitBac.allowedChannel()
        self._schedule_daily_pending_check(irc, allowed)

        # Chargement des catégories et dictionnaires
        self.data = self._load_categories_json()
        
        # Chargement des messages fun
        self.messages = self._load_json(os.path.join(self.storageDir, "messages.json"))

        # 🔥 Message d'initialisation
        try:
            total_cats = len(self.data.get("categories", {}))
            log.info(f"[LOG] PetitBac: catégories chargées ({total_cats} catégories)")
        except Exception as e:
            log.error(f"[ERREUR] PetitBac: erreur lors de l'annonce du chargement des catégories : {e}")
        
        # Chargement des scores
        self.scoreboard = self._load_scores()
        # Chargement de l'historique des 10 dernières parties
        self.last_games = self._load_last_games()

        # Chargement des stats
        self.global_stats = self._load_global_stats()
        self._normalize_existing_stats()  # Normalisation des stats ( Zell différent de zell )
        
        # Charger dictionnaire JSON
        self.dictionnaire = set()
        try:
            path = os.path.join(os.path.dirname(__file__), "data", "dictionnaire.json")
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.dictionnaire = set(data.keys())
            log.info(f"[LOG] PetitBac: dictionnaire chargé ({len(self.dictionnaire)} mots)")
        except Exception as e:
            log.error(f"[ERREUR] PetitBac: erreur chargement dictionnaire : {e}")
                    
        # URL Wikipedia
        self.wikipedia_url_template = conf.supybot.plugins.PetitBac.wikipediaApiUrl()
        log.info("[LOG] Jeu du PetitBac EntreNous lancé.")
        
        # Lancement mini serveur http pour l'api
        self._api_started = False

        # Si autostart est activé → on démarre l'API même si apiEnabled = off
        if conf.supybot.plugins.PetitBac.apiAutostart():
            self.start_api_server()
            
        # Modes de jeu fichier modes_jeu.json
        self.modes_file = os.path.join(self.storageDir, "modes_jeu.json")
        self.modes = self._load_json(self.modes_file)
        self.current_mode = {}  # channel → mode actif

        # Vote en cours par salon
        self.mode_vote = {}  # channel → {proposed_mode, votes{}, players:set(), timer}
        self.restart_vote = {}  # channel → {votes, players, timer}

        # Pause du jeu pendant un vote
        self.game_paused = {}  # channel → True/False

        # Cooldown entre votes
        self.last_mode_change = {}  # channel → timestamp
        
        self.players = {}  # channel → set(nicks)
        
        # Mots souvent utilisés :
        self.word_usage = {}  # mot → nombre d’utilisations
