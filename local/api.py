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

class ApiMixin:

    # --------------- ---------------------------------------------------
    # Utilisation de l'API 
    # -------------------------------------------------------------------
    # Lancement du mini serveur http pour l'api
    def start_api_server(self):
        if getattr(self, "_api_started", False):
            return

        enabled = conf.supybot.plugins.PetitBac.apiEnabled()
        autostart = conf.supybot.plugins.PetitBac.apiAutostart()
        address = conf.supybot.plugins.PetitBac.apiAddress()
        port = conf.supybot.plugins.PetitBac.apiPort()

        # Si ni enabled ni autostart → ne rien faire
        if not enabled and not autostart:
            log.info("PetitBac API: désactivée (apiEnabled=off et apiAutostart=off)")
            return

        # Adresse ou port manquant
        if not address or port == 0:
            log.error("PetitBac API: adresse ou port non configuré, API non démarrée.")
            return

        # Port déjà utilisé
        if self._port_in_use(address, port):
            log.error(f"PetitBac API: port {port} déjà utilisé, API non démarrée.")
            return

        self._api_started = True

        def run():
            try:
                server = HTTPServer((address, port), PetitBacAPI)
                server.plugin = self
                log.info(f"[LOG] PetitBac API HTTP démarrée sur {address}:{port}")
                server.serve_forever()
            except Exception as e:
                log.error(f"PetitBac API: erreur au démarrage : {e}")

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

    def _api_get_top(self):
        players = self.global_stats.get("players", {})
        sorted_players = sorted(players.items(), key=lambda x: x[1].get("total_points", 0), reverse=True)
        return [{"player": p, "points": d.get("total_points", 0)} for p, d in sorted_players]

    def _api_get_stats(self):
        return self.global_stats

    def _api_get_players(self):
        return list(self.global_stats.get("players", {}).keys())

    def _api_get_bugs(self):
        path = os.path.join(self.storageDir, "bugs.json")
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _api_get_suggestions(self):
        path = os.path.join(self.storageDir, "suggestions.json")
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
            
    def _port_in_use(self, address, port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex((address, port)) == 0


# ----------------------------------------------------------------------
# Fin de la classe PetitBac
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# API Petit Bac
# ----------------------------------------------------------------------
class PetitBacAPI(BaseHTTPRequestHandler):
    def do_GET(self):
        plugin = self.server.plugin  # accès au plugin PetitBac

        # Sécurité simple : clé API optionnelle
        token = "12345"  # tu peux changer
        if "token=" + token not in self.path:
            self._send_json({"error": "Unauthorized"}, 403)
            return

        # ROUTES
        if self.path.startswith("/api/petitbac/top"):
            data = plugin._api_get_top()
            self._send_json(data)
            return

        if self.path.startswith("/api/petitbac/stats"):
            data = plugin._api_get_stats()
            self._send_json(data)
            return

        if self.path.startswith("/api/petitbac/players"):
            data = plugin._api_get_players()
            self._send_json(data)
            return

        if self.path.startswith("/api/petitbac/bugs"):
            data = plugin._api_get_bugs()
            self._send_json(data)
            return

        if self.path.startswith("/api/petitbac/suggestions"):
            data = plugin._api_get_suggestions()
            self._send_json(data)
            return

        # Route inconnue
        self._send_json({"error": "Not found"}, 404)

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=4).encode("utf-8"))
# ----------------------------------------------------------------------
# Fin API Petit bac
# ----------------------------------------------------------------------