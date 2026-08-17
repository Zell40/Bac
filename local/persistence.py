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

class PersistenceMixin:

    # ----------------------------------------------------------------------
    # Stats global
    # ----------------------------------------------------------------------
    def _load_global_stats(self):
        path = os.path.join(self.storageDir, "global_stats.json")
        if not os.path.exists(path):
            return {"players": {}}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {"players": {}}

    def _save_global_stats(self):
        path = os.path.join(self.storageDir, "global_stats.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.global_stats, f, indent=2, ensure_ascii=False)
            
    def _normalize_existing_stats(self):
        players = self.global_stats.get("players", {})
        normalized = {}

        for nick, stats in players.items():
            key = nick.lower()
            if key not in normalized:
                normalized[key] = stats
            else:
                # Fusionner les stats si doublon
                for k, v in stats.items():
                    if isinstance(v, int):
                        normalized[key][k] += v
                    elif k == "last_seen":
                        normalized[key][k] = max(normalized[key][k], v)

        self.global_stats["players"] = normalized
        self._save_global_stats()
        
    def _update_global_stats(
            self, nick, *,
            games_played=0,
            rounds_played=0,
            words_validated=0,
            full_combos=0,
            total_points=0,
            speed_time=None,
            irc=None,
            channel=None,
            announce=True
        ):
        key = nick.lower()

        # --- Initialisation joueur ---
        player = self.global_stats["players"].setdefault(key, {
            "display": nick,
            "games_played": 0,
            "rounds_played": 0,
            "words_validated": 0,
            "full_combos": 0,
            "total_points": 0,
            "best_speed": None,
            "last_seen": None
        })

        # --- Mise à niveau anciens joueurs ---
        if "best_speed" not in player:
            player["best_speed"] = None
        if "display" not in player:
            player["display"] = nick

        # Mise à jour joueur
        player["display"] = nick
        player["games_played"] += games_played
        player["rounds_played"] += rounds_played
        player["words_validated"] += words_validated
        player["full_combos"] += full_combos
        player["total_points"] += total_points

        if speed_time is not None:
            if player["best_speed"] is None or speed_time < player["best_speed"]:
                player["best_speed"] = speed_time

        player["last_seen"] = time.strftime("%d/%m/%Y %H:%M:%S")

        # --- Stats globales ---
        g = self.global_stats["global"]

        # Mise à niveau anciens global_stats
        if "best_speed" not in g:
            g["best_speed"] = {"user": None, "seconds": None}
        if "best_score" not in g:
            g["best_score"] = {"user": None, "points": 0}
        if "best_full_combos" not in g:
            g["best_full_combos"] = {"user": None, "count": 0}

        g["games_played"] += games_played
        g["rounds_played"] += rounds_played
        g["words_validated"] += words_validated
        g["full_combos"] += full_combos
        g["total_points"] += total_points
        g["last_activity"] = player["last_seen"]

        # Records globaux
        if player["total_points"] > g["best_score"]["points"]:
            g["best_score"] = {"user": player["display"], "points": player["total_points"]}

        if player["full_combos"] > g["best_full_combos"]["count"]:
            g["best_full_combos"] = {"user": player["display"], "count": player["full_combos"]}

        if player["best_speed"] is not None:
            if g["best_speed"]["seconds"] is None or player["best_speed"] < g["best_speed"]["seconds"]:
                g["best_speed"] = {"user": player["display"], "seconds": player["best_speed"]}

        # --- Stats hebdo ---
        now = time.time()
        w = self.global_stats["weekly"]

        # Mise à niveau anciens weekly
        if "best_speed" not in w:
            w["best_speed"] = {"user": None, "seconds": None}
        if "best_score" not in w:
            w["best_score"] = {"user": None, "points": 0}
        if "best_full_combos" not in w:
            w["best_full_combos"] = {"user": None, "count": 0}

        if now - w["start"] > 7 * 24 * 3600:
            self.global_stats["weekly"] = {
                "start": now,
                "players": {},
                "best_score": {"user": None, "points": 0},
                "best_full_combos": {"user": None, "count": 0},
                "best_speed": {"user": None, "seconds": None}
            }
            w = self.global_stats["weekly"]

        w["players"].setdefault(key, 0)
        w["players"][key] += total_points

        if w["players"][key] > w["best_score"]["points"]:
            w["best_score"] = {"user": player["display"], "points": w["players"][key]}

        if player["full_combos"] > w["best_full_combos"]["count"]:
            w["best_full_combos"] = {"user": player["display"], "count": player["full_combos"]}

        if player["best_speed"] is not None:
            if w["best_speed"]["seconds"] is None or player["best_speed"] < w["best_speed"]["seconds"]:
                w["best_speed"] = {"user": player["display"], "seconds": player["best_speed"]}

        self._save_global_stats()

    # ----------------------------------------------------------------------
    # Chargement des catégories
    # ----------------------------------------------------------------------
    def _load_categories_json(self):
        path = os.path.join(self.storageDir, "categories.json")

        if not os.path.exists(path):
            return {
                "categories": {},
                "blacklist": [],
                "whitelist": [],
                "multicat": {},
                "ia": {
                    "exclusions": {},
                    "typos": {},
                    "patterns": {}
                }
            }

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        normalized = {
            "categories": {},
            "blacklist": [],
            "whitelist": [],
            "multicat": {},
            "ia": {
                "exclusions": {},
                "typos": {},
                "patterns": {}
            }
        }

        # Catégories
        for raw_cat, content in data.get("categories", {}).items():
            cat = self._norm_cat(raw_cat)
            mots = set(w.lower().strip() for w in content.get("mots", []))
            keywords = content.get("keywords", ["mot", "terme", "element", "concept"])
            normalized["categories"][cat] = {"mots": mots, "keywords": keywords}

        # Blacklist
        normalized["blacklist"] = [
            w.lower().strip() for w in data.get("blacklist", [])
        ]

        # Whitelist 🔥 (manquait totalement)
        normalized["whitelist"] = [
            w.lower().strip() for w in data.get("whitelist", [])
        ]

        # Multicat
        for mot, cats in data.get("multicat", {}).items():
            normalized["multicat"][mot.lower()] = [
                self._norm_cat(c) for c in cats
            ]

        # IA (exclusions, typos, patterns)
        ia = data.get("ia", {})
        normalized["ia"]["exclusions"] = ia.get("exclusions", {})
        normalized["ia"]["typos"] = ia.get("typos", {})
        normalized["ia"]["patterns"] = ia.get("patterns", {})

        return normalized
       
    # ----------------------------------------------------------------------
    # Sauvegarde / suppression des catégories
    # ----------------------------------------------------------------------   
    def _save_categories_json(self):
        path = os.path.join(self.storageDir, "categories.json")

        data = {
            "categories": {
                cat: {
                    "mots": sorted(list(content["mots"])),
                    "keywords": content["keywords"]
                }
                for cat, content in self.data["categories"].items()
            },
            "blacklist": sorted(self.data["blacklist"]),
            "multicat": {
                mot: cats
                for mot, cats in self.data["multicat"].items()
            },
            "ia": self.data.get("ia", {})
        }

        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        os.replace(tmp, path)

    # ----------------------------------------------------------------------
    # Scores
    #-----------------------------------------------------------------------   
    def _load_scores(self):
        path = os.path.join(self.storageDir, "scores.json")
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {}
            
    def _save_scores(self):
        path = os.path.join(self.storageDir, "scores.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.scoreboard, f, indent=2, ensure_ascii=False)
            
    def _load_last_games(self):
        path = os.path.join(self.storageDir, "last_games.json")
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return []

    def _save_last_games(self):
        path = os.path.join(self.storageDir, "last_games.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.last_games, f, indent=4, ensure_ascii=False)
            
    # ----------------------------------------------------------------------
    # Traitement fichier json 
    # ----------------------------------------------------------------------
    def _load_json(self, path):
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_json(self, path, data):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

    def _save_keywords(self):
        """
        Sauvegarde les mots-clés des catégories dans le fichier JSON.
        Version JSON : keywords sont maintenant stockés dans self.data["categories"][cat]["keywords"].
        """
        # On ne sauvegarde plus un fichier séparé : tout est dans categories.json
        self._save_categories_json()
        
    # ----------------------------------------------------------------------
    # Mot proposé en attente 
    # ----------------------------------------------------------------------
    def _load_pending_verifications(self):
        """
        Charge la liste des mots en attente de validation.
        Format : pending_verifications.json
        Retourne un dict : { id: {mot, categorie, auteur, timestamp} }
        """
        path = os.path.join(self.storageDir, "pending_verifications.json")

        # Si le fichier n'existe pas → aucune vérification en attente
        if not os.path.exists(path):
            return {}

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            # Fichier corrompu → sécurité : on ne casse pas le bot
            return {}

        pending = {}

        for vid, entry in data.items():
            try:
                mot = entry.get("mot", "").lower().strip()
                categorie = self._norm_cat(entry.get("categorie", ""))
                auteur = entry.get("auteur", "Inconnu")
                timestamp = int(entry.get("timestamp", time.time()))

                if mot and categorie:
                    pending[vid] = {
                        "mot": mot,
                        "categorie": categorie,
                        "auteur": auteur,
                        "timestamp": timestamp
                    }
            except Exception:
                # Si une entrée est cassée → on l'ignore
                continue

        return pending

    def _save_pending_verifications(self):
        """
        Sauvegarde automique de la liste des mots en attente de validation.
        Format : pending_verifications.json
        """
        path = os.path.join(self.storageDir, "pending_verifications.json")

        # Conversion en format JSON propre
        data = {
            vid: {
                "mot": entry["mot"],
                "categorie": entry["categorie"],
                "auteur": entry["auteur"],
                "timestamp": entry["timestamp"]
            }
            for vid, entry in self.pending_verifications.items()
        }

        # Sauvegarde atomique
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

        os.replace(tmp, path)
