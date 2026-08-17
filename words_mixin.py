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

class WordsMixin:

    # ----------------------------------------------------------------------
    # Réception des mots
    # ----------------------------------------------------------------------
    # Gestion automatique des catégories en fonction des mots
    def _guess_category(self, mot, wiki_summary=None, allowed_categories=None):
        mot_original = mot
        mot = mot.lower()

        # Trop court → IA refuse
        if len(mot) <= 3:
            return None

        # Limiter aux catégories de la manche
        if allowed_categories is None:
            allowed_categories = list(self.data["categories"].keys())

        # Ignorer Prénom pour les mots courts
        if len(mot) <= 4 and "prenom" in allowed_categories:
            allowed_categories = [c for c in allowed_categories if c != "prenom"]

        scores = {}

        # Similarité lexicale robuste
        def lexical_similarity(a, b):
            if sorted(a) == sorted(b):  # éviter anagrammes
                return 0.0
            return difflib.SequenceMatcher(None, a, b).ratio()

        # ----------------------------------------------------------------------
        # 🔥 Score lexical
        # ----------------------------------------------------------------------
        for cat in allowed_categories:
            words = self.data["categories"].get(cat, {}).get("mots", set())
            if not words:
                scores[cat] = 0
                continue

            sim_scores = [lexical_similarity(mot, w) for w in words]
            scores[cat] = max(sim_scores)

        # ----------------------------------------------------------------------
        # 🔥 Score Wikipédia
        # ----------------------------------------------------------------------
        if wiki_summary:
            summary = wiki_summary.lower()
            for cat in allowed_categories:
                keys = self.data["categories"].get(cat, {}).get("keywords", [])
                for k in keys:
                    if k in summary:
                        scores[cat] += 0.4

        # ----------------------------------------------------------------------
        # 🔥 Trouver la meilleure catégorie
        # ----------------------------------------------------------------------
        best_cat = max(scores, key=scores.get)
        best_score = scores[best_cat]

        sorted_scores = sorted(scores.values(), reverse=True)

        # Seuil minimal global
        if best_score < 0.45:
            return None

        # Seuil spécial pour Prénom
        if best_cat == "prenom" and best_score < 0.95:
            return None

        # Ambiguïté
        if len(sorted_scores) > 1 and (sorted_scores[0] - sorted_scores[1]) < 0.15:
            return None

        # ----------------------------------------------------------------------
        # 🧠 FILTRE LOGIQUE INTÉGRÉ (anti-absurdités)
        # ----------------------------------------------------------------------

        # 1) Majuscule → probablement prénom ou ville, rarement fruit
        if mot_original[0].isupper():
            if best_cat == "fruit":
                return None

        # 2) Fruits → presque toujours noms communs → pas de majuscule
        if best_cat == "fruit" and mot_original[0].isupper():
            return None

        # 3) Prénoms → rarement très longs
        if best_cat == "prenom" and len(mot) > 10:
            return None

        # 4) Villes → rarement très courtes
        if best_cat == "ville" and len(mot) <= 3:
            return None

        # 5) Cas spéciaux universels (Noël, Noël → jamais fruit)
        if mot in ["noel", "noël"] and best_cat == "fruit":
            return None

        # ----------------------------------------------------------------------
        # 🧠 FILTRE LOGIQUE INTÉGRÉ (anti-absurdités + corrections intelligentes)
        # ----------------------------------------------------------------------

        mot_clean = mot
        mot_original = mot_original.strip()

        # 1) Détection nom propre (majuscule initiale)
        is_proper = mot_original[0].isupper()

        # 2) Détection mot composé (souvent ville)
        is_composed = "-" in mot_original or " " in mot_original

        # 3) Détection accents (souvent prénoms ou villes)
        has_accent = any(c in "éèêëàâîïôöùûç" for c in mot_original.lower())

        # 4) Correction automatique : Noël → prénom
        if mot_clean in ["noel", "noël"]:
            if "prenom" in allowed_categories:
                return "prenom"

        # 5) Correction automatique : Paris, Londres, Tokyo → ville
        if is_proper and len(mot_original) > 3:
            if "ville" in allowed_categories and best_cat != "ville":
                # Si l’IA propose fruit/prénom → on corrige
                if best_cat in ["fruit", "prenom"]:
                    return "ville"

        # 6) Correction automatique : mots composés → ville
        if is_composed and "ville" in allowed_categories:
            if best_cat != "ville":
                return "ville"

        # 7) Fruits → jamais nom propre
        if best_cat == "fruit" and is_proper:
            return None

        # 8) Prénoms → rarement > 10 lettres
        if best_cat == "prenom" and len(mot_clean) > 10:
            return None

        # 9) Villes → rarement ≤ 3 lettres
        if best_cat == "ville" and len(mot_clean) <= 3:
            return None

        # 10) Prénoms → souvent avec accent (Léa, Chloé, Éric)
        if best_cat == "prenom" and not has_accent and len(mot_clean) <= 4:
            # Exemple : "Noel" → pas prénom
            if mot_clean not in ["lea", "leo", "ana"]:
                return None

        # ----------------------------------------------------------------------

        return best_cat

    def _debug_guess_category(self, mot, wiki_summary=None, allowed_categories=None):
        mot_original = mot
        mot = mot.lower()

        debug = {
            "lexical": {},
            "wiki": {},
            "final": {},
            "decision": None,
            "reason": None,
            "filters": []
        }

        # Trop court
        if len(mot) <= 3:
            debug["reason"] = "mot trop court"
            debug["filters"].append("longueur <= 3")
            return debug

        # Catégories autorisées
        if allowed_categories is None:
            allowed_categories = list(self.data["categories"].keys())

        # Ignorer prénom pour mots courts
        if len(mot) <= 4 and "prenom" in allowed_categories:
            allowed_categories = [c for c in allowed_categories if c != "prenom"]
            debug["filters"].append("mot court → suppression catégorie 'prenom'")

        # Fonction similarité
        def lexical_similarity(a, b):
            if sorted(a) == sorted(b):
                return 0.0
            return difflib.SequenceMatcher(None, a, b).ratio()

        # Scores lexicaux
        for cat in allowed_categories:
            words = self.data["categories"].get(cat, {}).get("mots", set())
            if not words:
                debug["lexical"][cat] = 0
                continue
            sim_scores = [lexical_similarity(mot, w) for w in words]
            debug["lexical"][cat] = max(sim_scores)

        # Scores wiki
        for cat in allowed_categories:
            debug["wiki"][cat] = {"score": 0, "found": []}

        if wiki_summary:
            summary = wiki_summary.lower()
            for cat in allowed_categories:
                keys = self.data["categories"].get(cat, {}).get("keywords", [])
                for k in keys:
                    if k in summary:
                        debug["wiki"][cat]["score"] += 0.4
                        debug["wiki"][cat]["found"].append(k)

        # Score final
        for cat in allowed_categories:
            debug["final"][cat] = debug["lexical"][cat] + debug["wiki"][cat]["score"]

        # Décision brute
        best_cat = max(debug["final"], key=debug["final"].get)
        best_score = debug["final"][best_cat]
        sorted_scores = sorted(debug["final"].values(), reverse=True)

        # Filtres identiques à _guess_category()

        # Score trop faible
        if best_score < 0.45:
            debug["reason"] = "score trop faible"
            debug["filters"].append("best_score < 0.45")
            return debug

        # Cas spécial prénom
        if best_cat == "prenom" and best_score < 0.95:
            debug["reason"] = "score prénom insuffisant"
            debug["filters"].append("best_score < 0.95 pour prénom")
            return debug

        # Ambiguïté
        if len(sorted_scores) > 1 and (sorted_scores[0] - sorted_scores[1]) < 0.15:
            debug["reason"] = "ambiguïté entre catégories"
            debug["filters"].append("écart < 0.15")
            return debug

        # Filtres logiques
        is_proper = mot_original[0].isupper()
        is_composed = "-" in mot_original or " " in mot_original
        has_accent = any(c in "éèêëàâîïôöùûç" for c in mot_original.lower())

        # Majuscule → fruit interdit
        if is_proper and best_cat == "fruit":
            debug["reason"] = "majuscule incompatible avec fruit"
            debug["filters"].append("mot propre → fruit interdit")
            return debug

        # Fruits → jamais majuscule
        if best_cat == "fruit" and is_proper:
            debug["reason"] = "fruit incompatible avec majuscule"
            debug["filters"].append("fruit + majuscule")
            return debug

        # Prénoms → rarement > 10 lettres
        if best_cat == "prenom" and len(mot) > 10:
            debug["reason"] = "prénom trop long"
            debug["filters"].append("prenom + longueur > 10")
            return debug

        # Villes → rarement ≤ 3 lettres
        if best_cat == "ville" and len(mot) <= 3:
            debug["reason"] = "ville trop courte"
            debug["filters"].append("ville + longueur <= 3")
            return debug

        # Cas Noël
        if mot in ["noel", "noël"] and best_cat == "fruit":
            debug["reason"] = "Noël ne peut pas être fruit"
            debug["filters"].append("noel/noël → fruit interdit")
            return debug

        # Correction automatique : Noël → prénom
        if mot in ["noel", "noël"]:
            debug["decision"] = "prenom"
            debug["reason"] = "correction automatique Noël → prénom"
            return debug

        # Correction automatique : Paris, Tokyo → ville
        if is_proper and len(mot_original) > 3:
            if "ville" in allowed_categories and best_cat != "ville":
                if best_cat in ["fruit", "prenom"]:
                    debug["decision"] = "ville"
                    debug["reason"] = "correction automatique : nom propre → ville"
                    return debug

        # Correction automatique : mots composés → ville
        if is_composed and "ville" in allowed_categories and best_cat != "ville":
            debug["decision"] = "ville"
            debug["reason"] = "correction automatique : mot composé → ville"
            return debug

        # Prénoms → souvent accent
        if best_cat == "prenom" and not has_accent and len(mot) <= 4:
            if mot not in ["lea", "leo", "ana"]:
                debug["reason"] = "prénom sans accent improbable"
                debug["filters"].append("prenom + pas d'accent")
                return debug

        # Si tout est OK
        debug["decision"] = best_cat
        debug["reason"] = "OK"
        return debug
        
    # -----------------------------------------------------------------------------
    # Commande !verifier
    # -----------------------------------------------------------------------------              
    @wrap(['text'])
    def verifier(self, irc, msg, args, texte):
        """Vérifie si un mot existe dans le dictionnaire ou sur Wikipédia, puis l'ajoute en attente.
           Syntaxe : !verifier <catégorie> <mot composé>
        """

        channel = msg.args[0]
        nick = msg.nick
        nick_key = nick.lower()

        # Découper le texte en morceaux
        parts = texte.strip().split()

        if len(parts) < 2:
            irc.queueMsg(ircmsgs.notice(nick,
                "❌ Utilisation : !verifier <catégorie> <mot>"))
            return

        # Catégorie = premier argument
        categorie_raw = parts[0]

        # Mot composé = tout le reste
        raw_mot = " ".join(parts[1:]).strip()

        mot = self._normalize_word(raw_mot)
        
        if mot in self.data["blacklist"]:
            irc.queueMsg(ircmsgs.notice(nick,
                f"⛔ Le mot « {mot} » est blacklisté et ne peut pas être proposé."))
            return

        if not mot:
            irc.queueMsg(ircmsgs.notice(nick,
                f"❌ Le mot « {raw_mot} » est invalide."))
            return

        # Vérifier que le joueur a déjà participé
        if nick_key not in self.global_stats.get("players", {}):
            irc.queueMsg(ircmsgs.notice(msg.nick,
                "❌ Vous devez avoir participé au jeu au moins une fois pour proposer un mot."))
            return

        # Nettoyage de la catégorie
        used_brackets = False
        if (categorie_raw.startswith("<") and categorie_raw.endswith(">")) or \
           (categorie_raw.startswith("(") and categorie_raw.endswith(")")) or \
           (categorie_raw.startswith("[") and categorie_raw.endswith("]")):
            used_brackets = True
            categorie_raw = categorie_raw[1:-1].strip()

        cat = self._norm_cat(categorie_raw)

        # Vérifier que la catégorie existe
        if cat not in self.data["categories"]:
            irc.queueMsg(ircmsgs.notice(msg.nick,
                f"❌ La catégorie « {categorie_raw} » n'existe pas dans la base du jeu."))
            irc.queueMsg(ircmsgs.notice(msg.nick,
                f"En revanche, tu peux nous la proposer via la commande !suggestion <message> (par exemple !suggestion Ajout de la catégorie fleur )."))
            return

        # Vérification dictionnaire interne ou Wikipédia
        if mot in self.dictionnaire:
            found = True
        else:
            summary = self.get_wikipedia_summary(mot)
            found = summary is not None

        if not found:
            irc.queueMsg(ircmsgs.privmsg(channel,
                f"❌ Le mot « {mot} » n'à pas été trouvé dans notre dictionnaire interne ni sur Wikipédia."))
            irc.queueMsg(ircmsgs.privmsg(channel,
                f"🔍 Tu peux vérifier sa définition avec : !info {mot}"))
            return
            
        # Vérifier si le mot existe déjà dans les catégories
        existing_cats = [
            c for c, content in self.data["categories"].items()
            if mot in content["mots"]
        ]

        if existing_cats:
            cats_str = ", ".join(existing_cats)
            irc.queueMsg(ircmsgs.notice(nick,
                f"ℹ️ Le mot « {mot} » existe déjà dans la/les catégorie(s) : {cats_str}."))
            irc.queueMsg(ircmsgs.notice(nick,
                "❌ Il n'est pas nécessaire de le reproposer."))
            return

        # Vérifier s'il est déjà en attente de vérification
        already_pending = [
            (vid, data) for vid, data in self.pending_verifications.items()
            if data.get("mot") == mot and data.get("categorie") == cat
        ]

        if already_pending:
            vid, data = already_pending[0]
            irc.queueMsg(ircmsgs.notice(nick,
                f"ℹ️ Le mot « {mot} » est déjà en attente de vérification dans « {cat} » (ID {vid})."))
            return

        # Ajouter dans la liste d'attente
        new_id = str(max([int(i) for i in self.pending_verifications.keys()] + [0]) + 1)
        
        self.pending_verifications[new_id] = {
            "mot": mot,
            "categorie": cat,
            "auteur": msg.nick,
            "timestamp": int(time.time())
        }

        self._save_pending_verifications()

        msg_extra = " (les <> entourant la catégorie ne sont pas nécessaires)" if used_brackets else ""
        irc.queueMsg(ircmsgs.privmsg(channel,
            f"ℹ️ Le mot « {mot} » pour la catégorie « {cat} » à été detecté valide. Il est en attente de vérification par un Opérateur (ID {new_id}).{msg_extra}"))
        irc.queueMsg(ircmsgs.notice(nick,
            f"ℹ️ {msg.nick} : Cela ne t’accorde pas de point pour l’instant. Propose un autre mot en attendant la validation."))

        # Notification opérateurs
        self._notify_ops(irc, channel,
            f"🔎 Nouveau mot en attente : « {mot} » (ID {new_id})")

        # Notification BotServ
        dev_channel = self.registryValue('devChannel', msg.channel)
        if dev_channel and dev_channel.startswith("#"):
            irc.queueMsg(ircmsgs.privmsg(
                "BotServ",
                f"say {dev_channel} [{irc.nick.upper()}][VERIFICATION DE MOT] {msg.nick} propose « {mot} » dans « {cat} » (ID {new_id})"
            ))

    def _resolve_verif_id(self, value):
        """
        Permet d'utiliser soit un ID numérique, soit un mot pour retrouver
        l'entrée dans pending_verifications.
        Retourne l'ID sous forme de string, ou None si introuvable.
        """

        # ID numérique direct
        if value.isdigit():
            return value if value in self.pending_verifications else None

        # Recherche par mot
        value = value.lower().strip()

        matches = [
            vid for vid, data in self.pending_verifications.items()
            if data.get("mot", "").lower() == value
        ]

        if not matches:
            return None

        # Si plusieurs → prendre le plus récent (ID le plus grand)
        return sorted(matches, key=lambda x: int(x))[-1]


    def _resolve_player_id(self, value):
        """
        Permet d'utiliser soit un pseudo, soit un ID numérique.
        Retourne le pseudo (clé) ou None si introuvable.
        """
        players = sorted(self.global_stats.get("players", {}).keys())

        # ID numérique
        if value.isdigit():
            idx = int(value) - 1
            if 0 <= idx < len(players):
                return players[idx]
            return None

        # Pseudo direct
        key = value.lower()
        return key if key in self.global_stats.get("players", {}) else None

    @wrap(['text'])
    def info(self, irc, msg, args, texte):
        """Donne une courte description du mot via Wikipédia.
           Syntaxe : !info <mot composé>
        """

        channel = msg.args[0]
        nick = msg.nick

        # Mot composé complet
        raw_mot = texte.strip()
        mot = self._normalize_word(raw_mot)

        # Vérification du mot
        if not mot:
            irc.queueMsg(ircmsgs.notice(nick,
                f"❌ Le mot « {raw_mot} » est invalide."))
            return

        # Récupération Wikipédia
        description = self.get_wikipedia_summary(mot)

        if not description:
            irc.queueMsg(ircmsgs.privmsg(channel,
                f"❌ Aucune information trouvée pour « {mot} » sur Wikipédia."))
            return

        # Nettoyage des caractères interdits par IRC
        clean = description.replace("\n", " ").replace("\xa0", " ")

        # Envoi via la fonction utilitaire
        self._send_long_message(
            irc,
            channel,
            f"ℹ️ {mot.capitalize()} — {clean}"
        )
      
    # -------------------------------------------------------------------------
    # !bac commande ( config / debug / verif / mots / bug / suggestion )
    # -------------------------------------------------------------------------
    @wrap(['something', additional('text')])
    def bac(self, irc, msg, args, action, rest=None):
        """Utilisation : config,debug,mots,verif,bug,suggestion,stats"""

        nick = msg.nick
        allowed = self.registryValue('allowedChannel')

        # Vérification opérateur du salon configuré
        chan_obj = irc.state.channels.get(allowed)
        is_op = chan_obj and chan_obj.isOp(nick)

        if not is_op:
            irc.error(f"❌ Vous devez être opérateur du salon {allowed} pour utiliser cette commande.")
            return

        # À partir d'ici, la commande est autorisée
        action = action.lower()

        # Parsing universel
        parts = re.split(r"\s+", rest.strip()) if rest else []
        parts = [unicodedata.normalize("NFKC", p) for p in parts]

        sub  = parts[0].lower() if len(parts) >= 1 else None
        arg1 = parts[1] if len(parts) >= 2 else None
        arg2 = parts[2] if len(parts) >= 3 else None

        # -----------------------------
        # MOTS (add/del/mod/addcat/delcat/multicat/list/check/blacklist/whitelist/exclure/typos)
        # -----------------------------
        if action == "mots":
            if not sub:
                irc.queueMsg(ircmsgs.notice(nick,
                    "❌ Utilisation : !bac mots <add|del|mod|addcat|delcat|multicat|list|check|blacklist|whitelist|exclure|typos>"))
                return

            # -----------------------------
            # addcat — créer une catégorie
            # -----------------------------
            if sub == "addcat":
                if not arg1:
                    irc.queueMsg(ircmsgs.notice(nick, "❌ Utilisation : !bac mots addcat <catégorie>"))
                    return

                cat = self._norm_cat(arg1)

                if cat in self.data["categories"]:
                    irc.queueMsg(ircmsgs.notice(nick, f"⚠️ La catégorie « {cat} » existe déjà."))
                    return

                self.data["categories"][cat] = {
                    "mots": set(),
                    "keywords": ["mot", "terme", "element", "concept"]
                }

                self._save_categories_json()
                irc.queueMsg(ircmsgs.notice(nick, f"✅ Catégorie « {cat} » créée."))
                return

            # -----------------------------
            # add — ajouter un mot
            # -----------------------------
            elif sub == "add":

                if len(parts) < 3:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Utilisation : !bac mots add <catégorie> <mot composé>"))
                    return

                # Catégorie
                cat = self._norm_cat(parts[1])

                if cat not in self.data["categories"]:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"❌ La catégorie « {parts[1]} » n'existe pas."))
                    return

                # Mot composé = tout ce qui suit la catégorie
                raw_word = " ".join(parts[2:])

                # Pas d’ID dans add
                if raw_word.isdigit():
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Impossible d’ajouter un mot via un ID."))
                    return

                # Normalisation
                word = self._normalize_word(raw_word)
                if not word:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"❌ Le mot « {raw_word} » est invalide."))
                    return
                    
                if word in self.data["blacklist"]:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"⛔ Le mot « {word} » est blacklisté."))
                    return

                # Vérifier si le mot existe déjà ailleurs
                for c, content in self.data["categories"].items():
                    if word in content["mots"]:
                        irc.queueMsg(ircmsgs.notice(nick,
                            f"❌ Le mot « {word} » existe déjà dans la catégorie « {c} »."))
                        return

                # Ajout
                self.data["categories"][cat]["mots"].add(word)
                
                # 🔥 Ajout automatique dans whitelist si nécessaire
                if self._should_be_whitelisted(word):
                    if word not in self.data["whitelist"]:
                        self.data["whitelist"].append(word)

                self._save_categories_json()

                irc.queueMsg(ircmsgs.notice(nick,
                    f"✅ Mot « {word} » ajouté dans « {cat} »."))
                return

            # -----------------------------
            # delcat — supprimer une catégorie
            # -----------------------------
            elif sub == "delcat":
                if not arg1:
                    irc.queueMsg(ircmsgs.notice(nick, "❌ Utilisation : !bac mots delcat <catégorie>"))
                    return

                cat = self._norm_cat(arg1)

                if cat not in self.data["categories"]:
                    irc.queueMsg(ircmsgs.notice(nick, f"❌ La catégorie « {cat} » n'existe pas."))
                    return

                # Vérifier si la catégorie contient des mots
                if self.data["categories"][cat]["mots"]:
                    count = len(self.data["categories"][cat]["mots"])
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"⚠️ La catégorie « {cat} » contient encore {count} mot(s)."))
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"➡️ Supprimez d'abord les mots contenus dans celle-ci."))
                    return

                del self.data["categories"][cat]
                self._save_categories_json()

                irc.queueMsg(ircmsgs.notice(nick, f"🗑️ Catégorie « {cat} » supprimée."))
                return

            # -----------------------------
            # del — supprimer un mot
            # -----------------------------
            elif sub == "del":
                if len(parts) < 3:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Utilisation : !bac mots del <catégorie> <mot|ID>"))
                    return

                cat = self._norm_cat(parts[1])

                if cat not in self.data["categories"]:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"❌ La catégorie « {parts[1]} » n'existe pas."))
                    return

                raw_word = " ".join(parts[2:])

                # ID fictif ?
                if hasattr(self, "temp_ids") and raw_word in self.temp_ids:
                    word = self.temp_ids[raw_word]
                else:
                    word = self._normalize_word(raw_word)
                    if not word:
                        irc.queueMsg(ircmsgs.notice(nick,
                            f"❌ Le mot « {raw_word} » est invalide."))
                        return

                if word not in self.data["categories"][cat]["mots"]:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"❌ Le mot « {word} » n'existe pas dans « {cat} »."))
                    return

                self.data["categories"][cat]["mots"].remove(word)
                self._save_categories_json()

                irc.queueMsg(ircmsgs.notice(nick,
                    f"🗑️ Mot « {word} » supprimé de « {cat} »."))
                return

            # -----------------------------
            # mod — déplacer un mot
            # -----------------------------
            elif sub == "mod":
                # Syntaxe : mod <nouvelle-catégorie> <mot>
                if len(parts) < 3:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Utilisation : !bac mots mod <nouvelle-catégorie> <mot composé|ID>"))
                    return

                # Nouvelle catégorie
                newcat = self._norm_cat(parts[1])

                if newcat not in self.data["categories"]:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"❌ La catégorie « {parts[1]} » n'existe pas."))
                    return

                # Extraction robuste du mot (ne casse pas les accents)
                raw_word = rest.split(" ", 2)[2]

                # ID fictif ?
                if hasattr(self, "temp_ids") and raw_word in self.temp_ids:
                    word = self.temp_ids[raw_word]
                else:
                    word = self._normalize_word(raw_word)
                    if not word:
                        irc.queueMsg(ircmsgs.notice(nick,
                            f"❌ Le mot « {raw_word} » est invalide."))
                        return

                # Vérifier que le mot existe dans au moins une catégorie
                existing_cats = [
                    c for c, content in self.data["categories"].items()
                    if word in content["mots"]
                ]

                if not existing_cats:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"❌ Le mot « {word} » n'existe dans aucune catégorie."))
                    return

                # 🔥 Nettoyage automatique :
                # Retirer le mot de TOUTES les catégories où il apparaît
                for c in existing_cats:
                    self.data["categories"][c]["mots"].remove(word)

                # Ajouter dans la nouvelle catégorie
                self.data["categories"][newcat]["mots"].add(word)

                # 🔥 Mise à jour multicatégorie
                # (ici, le mot n'est plus multicat puisqu'il n'est plus que dans newcat)
                if word in self.data["multicat"]:
                    del self.data["multicat"][word]

                # Sauvegarde
                self._save_categories_json()

                # Message
                if len(existing_cats) == 1:
                    oldcat = existing_cats[0]
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"🔁 Mot « {word} » déplacé de « {oldcat} » vers « {newcat} »."))
                else:
                    cats_list = ", ".join(existing_cats)
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"🔁 Mot « {word} » retiré de {cats_list} et déplacé vers « {newcat} »."))
                return

            # -----------------------------
            # list — lister les mots d'une catégorie
            # -----------------------------
            elif sub == "list":
                if not arg1:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Utilisation : !bac mots list <catégorie> [lettre]"))
                    return

                cat = self._norm_cat(arg1)

                if cat not in self.data["categories"]:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"❌ La catégorie « {cat} » n'existe pas."))
                    return

                # 🔥 Tri alphabétique insensible aux accents
                mots = sorted(
                    self.data["categories"][cat]["mots"],
                    key=lambda m: unicodedata.normalize("NFKD", m).encode("ASCII", "ignore").decode().lower()
                )

                # Si pas de lettre → afficher les lettres disponibles
                if not arg2:
                    if not mots:
                        irc.queueMsg(ircmsgs.notice(nick,
                            f"📖 Aucun mot dans la catégorie « {cat} »."))
                        return

                    lettres = sorted({m[0].upper() for m in mots})

                    irc.queueMsg(ircmsgs.notice(nick,
                        f"📚 La catégorie « {cat} » contient {len(mots)} mots."))
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"🔤 Lettres disponibles : {', '.join(lettres)}"))
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"➡️ Utilisation : !bac mots list {cat} <lettre>"))
                    return

                # Filtrer par préfixe (lettre ou plusieurs lettres)
                prefix = arg2.lower()

                # Normalisation Unicode (comme pour les mots)
                prefix = unicodedata.normalize("NFD", prefix)
                prefix = "".join(c for c in prefix if unicodedata.category(c) != "Mn")

                if not prefix.isalpha():
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Le préfixe doit contenir uniquement des lettres."))
                    return

                mots_filtrés = sorted(
                    [m for m in mots if m.startswith(prefix)],
                    key=lambda m: unicodedata.normalize("NFKD", m).encode("ASCII", "ignore").decode().lower()
                )

                if not mots_filtrés:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"📭 Aucun mot commençant par « {prefix.upper()} » dans « {cat} »."))
                    return

                # 🔥 Réinitialiser les IDs fictifs
                self.temp_ids = {}
                counter = 1

                chunk = 20
                for i in range(0, len(mots_filtrés), chunk):

                    bloc_mots = []
                    for m in mots_filtrés[i:i+chunk]:
                        id_str = str(counter)
                        self.temp_ids[id_str] = m
                        bloc_mots.append(f"{m}[{id_str}]")
                        counter += 1

                    bloc = ", ".join(bloc_mots)

                    irc.queueMsg(ircmsgs.notice(
                        nick,
                        f"{cat} — Préfixe {prefix.upper()} ({i+1}-{i+len(mots_filtrés[i:i+chunk])}) : {bloc}"
                    ))
                return
 
            # -----------------------------
            # check — Rechercher un mots dans les catégories
            # -----------------------------
            elif sub == "check":
                if not arg1:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Utilisation : !bac mots check <mot|préfixe>"))
                    return

                # 🔥 Prendre TOUT ce qui suit "cherche" (gère les mots composés)
                raw_word = " ".join(parts[1:])
                word = self._normalize_word(raw_word)

                found_any = False

                # ---------------------------------------------------------
                # 1) Recherche EXACTE dans les catégories
                # ---------------------------------------------------------
                exact_cats = [
                    c for c, content in self.data["categories"].items()
                    if word in content["mots"]
                ]

                if exact_cats:
                    found_any = True
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"📍 Mot EXACT trouvé dans les catégories :"))
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"• {word} → {', '.join(exact_cats)}"))

                # ---------------------------------------------------------
                # 2) Recherche EXACTE dans multicat
                # ---------------------------------------------------------
                if word in self.data.get("multicat", {}):
                    found_any = True
                    cats = ", ".join(self.data["multicat"][word])
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"📍 Mot EXACT trouvé dans multicat : {word} → {cats}"))

                # ---------------------------------------------------------
                # 3) Recherche EXACTE dans blacklist
                # ---------------------------------------------------------
                if word in self.data.get("blacklist", []):
                    found_any = True
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"⛔ Mot EXACT trouvé dans blacklist : {word}"))

                # ---------------------------------------------------------
                # 4) Recherche EXACTE dans whitelist
                # ---------------------------------------------------------
                if word in self.data.get("whitelist", []):
                    found_any = True
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"📘 Mot EXACT trouvé dans whitelist : {word}"))

                # ---------------------------------------------------------
                # 5) Recherche EXACTE dans IA exclusions
                # ---------------------------------------------------------
                ia_excl = self.data.get("ia", {}).get("exclusions", {})
                if word in ia_excl:
                    found_any = True
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"🤖 Mot EXACT trouvé dans exclusions IA : {word} → {', '.join(ia_excl[word])}"))

                # ---------------------------------------------------------
                # 6) Recherche EXACTE dans IA typos
                # ---------------------------------------------------------
                ia_typos = self.data.get("ia", {}).get("typos", {})
                if word in ia_typos:
                    found_any = True
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"🔤 Mot EXACT trouvé dans typos IA : {word} → {ia_typos[word]}"))

                # ---------------------------------------------------------
                # 7) Recherche EXACTE dans IA patterns
                # ---------------------------------------------------------
                ia_patterns = self.data.get("ia", {}).get("patterns", {})
                if word in ia_patterns:
                    found_any = True
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"🧩 Mot EXACT trouvé dans patterns IA : {word} → {ia_patterns[word]}"))

                # ---------------------------------------------------------
                # Si trouvé en exact → on s’arrête là
                # ---------------------------------------------------------
                if found_any:
                    return

                # ---------------------------------------------------------
                # 8) Recherche PAR PRÉFIXE dans les catégories
                # ---------------------------------------------------------
                matches = []
                for c, content in self.data["categories"].items():
                    for m in content["mots"]:
                        if m.startswith(word):
                            matches.append((m, c))

                if matches:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"🔎 Résultats pour le préfixe « {word} » dans les catégories :"))

                    matches = sorted(matches, key=lambda x: x[0])
                    for m, c in matches:
                        irc.queueMsg(ircmsgs.notice(nick, f"• {m} → {c}"))
                    return

                # ---------------------------------------------------------
                # Rien trouvé du tout
                # ---------------------------------------------------------
                irc.queueMsg(ircmsgs.notice(nick,
                    f"❌ Aucun mot ne correspond à « {word} » dans aucune base."))
                return 

            # -----------------------------
            # multicat — Gestion des mots multi-ctagéories
            # -----------------------------
            elif sub == "multicat":

                # Si aucune action n'est fournie → afficher l'aide
                if len(parts) < 2:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Utilisation : !bac mots multicat <add|del|sync|list> [mot/préfix] [catégorie]"))
                    return

                action2 = parts[1].lower()

                # -----------------------------
                # SYNC : ne nécessite aucun mot ni catégorie
                # -----------------------------
                if action2 == "sync":
                    # Reconstruire entièrement multicat à partir des catégories
                    new_multicat = {}

                    for cat, content in self.data["categories"].items():
                        for w in content["mots"]:
                            new_multicat.setdefault(w, []).append(cat)

                    cleaned_multicat = {
                        w: sorted(cats)
                        for w, cats in new_multicat.items()
                        if len(cats) > 1
                    }

                    old_multicat = self.data.get("multicat", {})

                    added = set(cleaned_multicat.keys()) - set(old_multicat.keys())
                    removed = set(old_multicat.keys()) - set(cleaned_multicat.keys())

                    self.data["multicat"] = cleaned_multicat
                    self._save_categories_json()

                    irc.queueMsg(ircmsgs.notice(nick, "🔄 Synchronisation multicatégorie effectuée."))

                    if added:
                        irc.queueMsg(ircmsgs.notice(nick,
                            f"➕ Ajoutés : {', '.join(sorted(added))}"))
                    if removed:
                        irc.queueMsg(ircmsgs.notice(nick,
                            f"➖ Supprimés : {', '.join(sorted(removed))}"))
                    if not added and not removed:
                        irc.queueMsg(ircmsgs.notice(nick,
                            "✔ Aucun changement. Multicat déjà cohérent."))

                    return
                    
                # -----------------------------
                # LIST — afficher les mots multicatégories (2 colonnes + filtre lettre obligatoire)
                # -----------------------------
                if action2 == "list":

                    # Préfixe obligatoire
                    if len(parts) < 3:
                        irc.queueMsg(ircmsgs.notice(nick,
                            "❌ Utilisation : !bac mots multicat list <lettre>"))
                        return

                    prefix = self._normalize_word(parts[2])
                    if not prefix.isalpha():
                        irc.queueMsg(ircmsgs.notice(nick,
                            "❌ Le préfixe doit contenir uniquement des lettres."))
                        return

                    multicat = self.data.get("multicat", {})

                    if not multicat:
                        irc.queueMsg(ircmsgs.notice(nick, "📭 Aucun mot multicatégorie."))
                        return

                    # Tri alphabétique
                    mots = sorted(
                        multicat.keys(),
                        key=lambda m: unicodedata.normalize("NFKD", m).encode("ASCII", "ignore").decode().lower()
                    )

                    # Filtrage par préfixe
                    mots = [m for m in mots if m.startswith(prefix)]

                    if not mots:
                        irc.queueMsg(ircmsgs.notice(nick,
                            f"📭 Aucun mot multicatégorie commençant par « {prefix.upper()} »."))
                        return

                    irc.queueMsg(ircmsgs.notice(nick,
                        f"📚 {len(mots)} mots multicatégories commençant par « {prefix.upper()} »."))
                    
                    # IDs fictifs
                    self.temp_ids = {}
                    counter = 1

                    col_width = 18  # largeur fixe pour aligner les colonnes

                    # Découpage en blocs de 40 mots (20 lignes)
                    for i in range(0, len(mots), 40):

                        bloc = mots[i:i+40]

                        for j in range(0, len(bloc), 2):

                            # Colonne gauche
                            left = bloc[j]
                            id_left = str(counter)
                            self.temp_ids[id_left] = left
                            cats_left = ", ".join(multicat[left])
                            counter += 1

                            left_str = f"{left}[{id_left}]".ljust(col_width)

                            # Colonne droite ?
                            if j+1 < len(bloc):
                                right = bloc[j+1]
                                id_right = str(counter)
                                self.temp_ids[id_right] = right
                                cats_right = ", ".join(multicat[right])
                                counter += 1

                                right_str = f"{right}[{id_right}]".ljust(col_width)

                                line = f"{left_str} → {cats_left} | {right_str} → {cats_right}"
                            else:
                                line = f"{left_str} → {cats_left}"

                            irc.queueMsg(ircmsgs.notice(nick, line))

                    return

                # -----------------------------
                # ADD / DEL nécessitent 4 arguments
                # -----------------------------
                if len(parts) < 4:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Utilisation : !bac mots multicat <add|del> <catégorie> <mot composé>"))
                    return

                cat2 = self._norm_cat(parts[2])

                if cat2 not in self.data["categories"]:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"❌ La catégorie « {cat2} » n'existe pas."))
                    return

                # Mot composé = tout ce qui suit la catégorie
                raw_word = " ".join(parts[3:])
                word = self._normalize_word(raw_word)

                if not word:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"❌ Le mot « {raw_word} » est invalide."))
                    return

                # Vérifier mot existant dans toutes les catégories
                all_cats = [
                    c for c, content in self.data["categories"].items()
                    if word in content["mots"]
                ]

                # -----------------------------
                # ➕ AJOUT multicatégorie
                # -----------------------------
                if action2 == "add":

                    if word in self.data["categories"][cat2]["mots"]:
                        irc.queueMsg(ircmsgs.notice(nick,
                            f"ℹ️ « {word} » est déjà dans « {cat2} »."))
                        return

                    # Ajouter dans la catégorie
                    self.data["categories"][cat2]["mots"].add(word)

                    # Mettre à jour multicat
                    all_cats.append(cat2)
                    all_cats = sorted(set(all_cats))

                    if len(all_cats) > 1:
                        self.data["multicat"][word] = all_cats
                    elif word in self.data["multicat"]:
                        del self.data["multicat"][word]

                    self._save_categories_json()

                    irc.queueMsg(ircmsgs.notice(nick,
                        f"➕ « {word} » ajouté dans « {cat2} ». Multicat = {', '.join(all_cats)}"))
                    return

                # -----------------------------
                # ➖ SUPPRESSION multicatégorie
                # -----------------------------
                if action2 == "del":

                    if word not in self.data["categories"][cat2]["mots"]:
                        irc.queueMsg(ircmsgs.notice(nick,
                            f"❌ « {word} » n'est pas dans « {cat2} »."))
                        return

                    # Retirer de la catégorie
                    self.data["categories"][cat2]["mots"].remove(word)

                    # Recalculer les catégories restantes
                    remaining = [
                        c for c, content in self.data["categories"].items()
                        if word in content["mots"]
                    ]

                    if len(remaining) > 1:
                        self.data["multicat"][word] = sorted(remaining)
                    elif len(remaining) == 1:
                        if word in self.data["multicat"]:
                            del self.data["multicat"][word]
                    else:
                        irc.queueMsg(ircmsgs.notice(nick,
                            f"⚠️ Attention : « {word} » n'est plus dans aucune catégorie !"))
                        if word in self.data["multicat"]:
                            del self.data["multicat"][word]

                    self._save_categories_json()

                    irc.queueMsg(ircmsgs.notice(nick,
                        f"➖ « {word} » retiré de « {cat2} »."))
                    return
                    
            # -----------------------------
            # blacklist - Gestion des mots interdits dans le jeu
            # -----------------------------
            elif sub == "blacklist":

                if len(parts) < 2:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Utilisation : !bac mots blacklist <list|add|del|check>"))
                    return

                action2 = parts[1].lower()

                # -----------------------------
                # LIST
                # -----------------------------
                if action2 == "list":

                    bl = self.data.get("blacklist", [])

                    if not bl:
                        irc.queueMsg(ircmsgs.notice(nick, "📭 Aucun mot dans la blacklist."))
                        return

                    irc.queueMsg(ircmsgs.notice(nick, "⛔ Mots blacklistés :"))

                    self.temp_ids = {}
                    counter = 1

                    for mot in sorted(bl):
                        id_str = str(counter)
                        self.temp_ids[id_str] = mot
                        irc.queueMsg(ircmsgs.notice(nick, f"  • {mot} [{id_str}]"))
                        counter += 1

                    return

                # -----------------------------
                # ADD
                # -----------------------------
                elif action2 == "add":

                    if len(parts) < 3:
                        irc.queueMsg(ircmsgs.notice(nick,
                            "❌ Utilisation : !bac mots blacklist add <mot composé>"))
                        return

                    raw_word = " ".join(parts[2:])
                    word = self._normalize_word(raw_word)

                    if not word:
                        irc.queueMsg(ircmsgs.notice(nick,
                            f"❌ Le mot « {raw_word} » est invalide."))
                        return

                    if word in self.data["blacklist"]:
                        irc.queueMsg(ircmsgs.notice(nick,
                            f"⚠️ Le mot « {word} » est déjà blacklisté."))
                        return

                    self.data["blacklist"].append(word)
                    self._save_categories_json()

                    irc.queueMsg(ircmsgs.notice(nick,
                        f"⛔ Mot « {word} » ajouté à la blacklist."))
                    return

                # -----------------------------
                # DEL
                # -----------------------------
                elif action2 == "del":

                    if len(parts) < 3:
                        irc.queueMsg(ircmsgs.notice(nick,
                            "❌ Utilisation : !bac mots blacklist del <mot|ID>"))
                        return

                    target = " ".join(parts[2:]).strip().lower()

                    if hasattr(self, "temp_ids") and target in self.temp_ids:
                        word = self.temp_ids[target]
                    else:
                        word = self._normalize_word(target)

                    if word not in self.data["blacklist"]:
                        irc.queueMsg(ircmsgs.notice(nick,
                            f"❌ Le mot « {target} » n'est pas dans la blacklist."))
                        return

                    self.data["blacklist"].remove(word)
                    self._save_categories_json()

                    irc.queueMsg(ircmsgs.notice(nick,
                        f"🗑️ Mot « {word} » retiré de la blacklist."))
                    return

                # -----------------------------
                # CHECK
                # -----------------------------
                elif action2 == "check":

                    if len(parts) < 3:
                        irc.queueMsg(ircmsgs.notice(nick,
                            "❌ Utilisation : !bac mots blacklist check <mot>"))
                        return

                    word = self._normalize_word(" ".join(parts[2:]))

                    if word in self.data["blacklist"]:
                        irc.queueMsg(ircmsgs.notice(nick,
                            f"⛔ Le mot « {word} » est blacklisté."))
                    else:
                        irc.queueMsg(ircmsgs.notice(nick,
                            f"✔ Le mot « {word} » n'est pas blacklisté."))
                    return
                    
            # -----------------------------
            # whitelist - Gestion des mots composés autorisés
            # -----------------------------
            elif sub == "whitelist":

                if len(parts) < 2:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Utilisation : !bac mots whitelist <list|del|check>"))
                    return

                action2 = parts[1].lower()

                wl = self.data.get("whitelist", [])

                # -----------------------------
                # LIST
                # -----------------------------
                if action2 == "list":

                    if not wl:
                        irc.queueMsg(ircmsgs.notice(nick, "📭 Aucun mot dans la whitelist."))
                        return

                    irc.queueMsg(ircmsgs.notice(nick, "📘 Mots whitelistés (mots composés autorisés) :"))

                    self.temp_ids = {}
                    counter = 1

                    for mot in sorted(wl):
                        id_str = str(counter)
                        self.temp_ids[id_str] = mot
                        irc.queueMsg(ircmsgs.notice(nick, f"  • {mot} [{id_str}]"))
                        counter += 1

                    return

                # -----------------------------
                # DEL
                # -----------------------------
                elif action2 == "del":

                    if len(parts) < 3:
                        irc.queueMsg(ircmsgs.notice(nick,
                            "❌ Utilisation : !bac mots whitelist del <mot|ID>"))
                        return

                    target = " ".join(parts[2:]).strip().lower()

                    if hasattr(self, "temp_ids") and target in self.temp_ids:
                        word = self.temp_ids[target]
                    else:
                        word = self._normalize_word(target)

                    if word not in wl:
                        irc.queueMsg(ircmsgs.notice(nick,
                            f"❌ Le mot « {target} » n'est pas dans la whitelist."))
                        return

                    wl.remove(word)
                    self._save_categories_json()

                    irc.queueMsg(ircmsgs.notice(nick,
                        f"🗑️ Mot « {word} » retiré de la whitelist."))
                    return

                # -----------------------------
                # CHECK
                # -----------------------------
                elif action2 == "check":

                    if len(parts) < 3:
                        irc.queueMsg(ircmsgs.notice(nick,
                            "❌ Utilisation : !bac mots whitelist check <mot>"))
                        return

                    word = self._normalize_word(" ".join(parts[2:]))

                    if word in wl:
                        irc.queueMsg(ircmsgs.notice(nick,
                            f"✔ Le mot « {word} » est whitelisté."))
                    else:
                        irc.queueMsg(ircmsgs.notice(nick,
                            f"ℹ️ Le mot « {word} » n'est pas whitelisté."))
                    return
            
            # -----------------------------
            # exclure — gérer les exclusions IA ( Pas d'ajout automatique par l IA )
            # -----------------------------
            elif sub == "exclure":

                action2 = arg1.lower() if arg1 else None
                excl = self.data.setdefault("ia", {}).setdefault("exclusions", {})

                # --- LIST ---
                if action2 == "list":
                    if excl:
                        irc.queueMsg(ircmsgs.notice(nick, "📌 Exclusions IA :"))
                        for mot in sorted(excl.keys()):
                            cats = ", ".join(sorted(excl[mot]))
                            irc.queueMsg(ircmsgs.notice(nick, f"- {mot} → {cats}"))
                    else:
                        irc.queueMsg(ircmsgs.notice(nick, "📌 Aucune exclusion IA enregistrée."))
                    return

                # --- ADD / DEL ---
                if action2 not in ("add", "del"):
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Utilisation : !bac mots exclure <add|del|list> <catégorie> <mot composé>"))
                    return

                if len(parts) < 4:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Utilisation : !bac mots exclure <add|del> <catégorie> <mot composé>"))
                    return

                # Catégorie en premier (comme toutes les autres commandes)
                cat = self._norm_cat(parts[2])

                if cat not in self.data["categories"]:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"❌ La catégorie « {parts[2]} » n'existe pas."))
                    return

                # Mot composé = tout ce qui suit la catégorie
                raw_word = " ".join(parts[3:])
                mot = self._normalize_word(raw_word)

                if not mot:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"❌ Le mot « {raw_word} » est invalide."))
                    return

                if action2 == "add":
                    excl.setdefault(mot, [])
                    if cat not in excl[mot]:
                        excl[mot].append(cat)
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"✅ Exclusion ajoutée : {mot} → {cat}"))

                elif action2 == "del":
                    if mot in excl and cat in excl[mot]:
                        excl[mot].remove(cat)
                        if not excl[mot]:
                            del excl[mot]
                        irc.queueMsg(ircmsgs.notice(nick,
                            f"🗑️ Exclusion supprimée : {mot} → {cat}"))
                    else:
                        irc.queueMsg(ircmsgs.notice(nick,
                            "ℹ️ Cette exclusion n'existe pas."))

                self._save_categories_json()
                return

            # -----------------------------
            # typos — gérer les fautes IA ( Si l'IA reconnait un mots en erreur, il modifie le mot dans la version correct automatiquement )
            # -----------------------------
            elif sub == "typos":

                action2 = arg1.lower() if arg1 else None
                typos = self.data.setdefault("ia", {}).setdefault("typos", {})

                # --- LIST ---
                if action2 == "list":
                    if typos:
                        irc.queueMsg(ircmsgs.notice(nick, "✏️ Typos IA :"))
                        for fautif in sorted(typos.keys()):
                            corr = typos[fautif]
                            irc.queueMsg(ircmsgs.notice(nick, f"- {fautif} → {corr}"))
                    else:
                        irc.queueMsg(ircmsgs.notice(nick, "✏️ Aucune correction automatique enregistrée."))
                    return

                # --- ADD / DEL ---
                if action2 not in ("add", "del"):
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Utilisation : !bac mots typos <add|del|list> [<mot> = <mot corrigé>]"))
                    return

                if not arg2:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Vous devez préciser le mot."))
                    return

                # Reconstituer tout ce qui suit <add|del>
                raw = " ".join(parts[2:]).strip()

                # --- ADD ---
                if action2 == "add":

                    # Vérifier la présence du séparateur "="
                    if "=" not in raw:
                        irc.queueMsg(ircmsgs.notice(nick,
                            "❌ Format invalide. Utilisez : <mot> = <mot corrigé>"))
                        return

                    mot_brut, correction_brut = map(str.strip, raw.split("=", 1))

                    mot = self._normalize_word(mot_brut)
                    correction = self._normalize_word(correction_brut)

                    if not mot or not correction:
                        irc.queueMsg(ircmsgs.notice(nick,
                            "❌ Mot ou correction invalide."))
                        return

                    typos[mot] = correction

                    irc.queueMsg(ircmsgs.notice(nick,
                        f"✏️ Correction ajoutée : {mot} → {correction}"))

                # --- DEL ---
                elif action2 == "del":

                    mot = self._normalize_word(raw)

                    if mot in typos:
                        del typos[mot]
                        irc.queueMsg(ircmsgs.notice(nick,
                            f"🗑️ Correction supprimée : {mot}"))
                    else:
                        irc.queueMsg(ircmsgs.notice(nick,
                            "ℹ️ Cette correction n'existe pas."))

                self._save_categories_json()
                return

        # -----------------------------
        # CONFIG
        # -----------------------------
        # Découpage des arguments pour CONFIG
        parts = (rest or "").split()

        sub  = parts[0].lower() if len(parts) >= 1 else None
        arg1 = parts[1] if len(parts) >= 2 else None
        arg2 = parts[2] if len(parts) >= 3 else None
        arg3 = parts[3] if len(parts) >= 4 else None
        
        if action == "config":
        
            duration = conf.supybot.plugins.PetitBac.roundDuration()
            rotation = conf.supybot.plugins.PetitBac.categoryRotation()
            max_idle = conf.supybot.plugins.PetitBac.maxIdleRounds()
            count = conf.supybot.plugins.PetitBac.categoryCount()
            max_rounds = conf.supybot.plugins.PetitBac.maxRounds()
            total_categories = len(self.data["categories"])
            autostart = conf.supybot.plugins.PetitBac.autostart()
            announce_channel = conf.supybot.plugins.PetitBac.announceChannel()
            dev_channel = self.registryValue('devChannel', msg.channel)
            allowed_channel = conf.supybot.plugins.PetitBac.allowedChannel()
            error_channel = conf.supybot.plugins.PetitBac.errorChannel()
            api_addr = conf.supybot.plugins.PetitBac.apiAddress()
            api_port = conf.supybot.plugins.PetitBac.apiPort()
            api_enabled = conf.supybot.plugins.PetitBac.apiEnabled()
            api_autostart = conf.supybot.plugins.PetitBac.apiAutostart()

            # Si aucune sous-commande → afficher la config
            if not sub:       
                irc.queueMsg(ircmsgs.notice(nick, "⚙️ Configuration du Petit Bac :"))
                irc.queueMsg(ircmsgs.notice(nick, f"  • Durée d'une manche : {duration} sec"))
                irc.queueMsg(ircmsgs.notice(nick, f"  • Rotation des catégories : {rotation} manches"))
                irc.queueMsg(ircmsgs.notice(nick, f"  • Manches sans réponse avant arrêt : {max_idle}"))
                irc.queueMsg(ircmsgs.notice(nick, f"  • Catégories par manche : {count}"))
                irc.queueMsg(ircmsgs.notice(nick, f"  • Manches max par partie : {max_rounds}"))
                irc.queueMsg(ircmsgs.notice(nick, f"  • Total catégories : {total_categories}"))
                irc.queueMsg(ircmsgs.notice(nick, f"  • Autostart : {'on' if autostart else 'off'}"))
                irc.queueMsg(ircmsgs.notice(nick, f"  • Annonce automatique : {'on' if conf.supybot.plugins.PetitBac.announceMessage() else 'off'}"))
                irc.queueMsg(ircmsgs.notice(nick, f"  • Salon d'annonce : {announce_channel or 'salon actuel'}"))
                irc.queueMsg(ircmsgs.notice(nick, f"  • Salon DEV : {dev_channel}"))
                irc.queueMsg(ircmsgs.notice(nick, f"  • Salon autorisé : {allowed_channel}"))
                irc.queueMsg(ircmsgs.notice(nick, f"  • Salon des logs : {error_channel}"))
                irc.queueMsg(ircmsgs.notice(nick, f"  • API serveur : {api_addr}"))
                irc.queueMsg(ircmsgs.notice(nick, f"  • API port : {api_port}"))
                irc.queueMsg(ircmsgs.notice(nick, f"  • API enabled : {'on' if api_enabled else 'off'}"))
                irc.queueMsg(ircmsgs.notice(nick, f"  • API autostart : {'on' if api_autostart else 'off'}"))
                return

            # Sous-commande SET
            if sub == "set":

                # 1) !bac config set  → liste des options
                if arg1 is None:
                    irc.queueMsg(ircmsgs.notice(nick, "⚙️ Options configurables :"))
                    irc.queueMsg(ircmsgs.notice(nick, "  • duration <sec> | Combien de secondes dure une manche."))
                    irc.queueMsg(ircmsgs.notice(nick, "  • rotation <manches> | Rotation des catégories au bout de x manche(s)."))
                    irc.queueMsg(ircmsgs.notice(nick, "  • maxidle <manches> | Le jeu s'arrète pour inactivité au bout de x manche(s)."))
                    irc.queueMsg(ircmsgs.notice(nick, "  • categories <nombre> | Nombre de catégorie en jeu dans une manche."))
                    irc.queueMsg(ircmsgs.notice(nick, "  • maxrounds <nombre> | Nombre de manche dans une partie"))
                    irc.queueMsg(ircmsgs.notice(nick, f"  • message <on/off> | Active ou Désactive le message de début de partie sur le salon {announce_channel} ."))
                    irc.queueMsg(ircmsgs.notice(nick, "  • announcechannel <#salon> | Salon qui affichera le message de début de partie ."))
                    irc.queueMsg(ircmsgs.notice(nick, "  • devchannel <#salon> | Salon qui recevra les messages des utilisateurs du jeu."))
                    irc.queueMsg(ircmsgs.notice(nick, "  • allowedchannel <#salon> | Salon actif pour jouer."))
                    irc.queueMsg(ircmsgs.notice(nick, "  • errorchannel <#salon> | Salon de log des erreurs du jeu."))
                    irc.queueMsg(ircmsgs.notice(nick, "  • autostart <on/off> | Démarrage automatique de la partie au join utilisateur."))
                    irc.queueMsg(ircmsgs.notice(nick, "  • api on/off | Active le serveur http pour récupérer les stats depuis un site web."))
                    irc.queueMsg(ircmsgs.notice(nick, "  • api autostart <on/off> | Active le démarrage automatique du serveur http."))
                    irc.queueMsg(ircmsgs.notice(nick, "  • api address <adresse> | Adresse du serveur http"))
                    irc.queueMsg(ircmsgs.notice(nick, "  • api port <port> | port du serveur http"))
                    return

                key = arg1.lower()
                subkey = arg2.lower() if arg2 else None
                val = arg3 if arg3 else None

                # 2) !bac config set <option>  → aide spécifique
                if subkey is None:
                    if key == "duration":
                        irc.queueMsg(ircmsgs.notice(nick, "⏳ Utilisation : !bac config set duration <secondes>"))
                        return
                    if key == "rotation":
                        irc.queueMsg(ircmsgs.notice(nick, "🔄 Utilisation : !bac config set rotation <manches>"))
                        return
                    if key == "maxidle":
                        irc.queueMsg(ircmsgs.notice(nick, "💤 Utilisation : !bac config set maxidle <manches>"))
                        return
                    if key == "categories":
                        irc.queueMsg(ircmsgs.notice(nick, "📚 Utilisation : !bac config set categories <nombre>"))
                        return
                    if key == "maxrounds":
                        irc.queueMsg(ircmsgs.notice(nick, "🏁 Utilisation : !bac config set maxrounds <nombre>"))
                        return
                    if key == "message":
                        irc.queueMsg(ircmsgs.notice(nick, "📣 Utilisation : !bac config set message <on/off>"))
                        return
                    if key == "announcechannel":
                        irc.queueMsg(ircmsgs.notice(nick, "👥 Utilisation : !bac config set announcechannel <#salon>"))
                        return
                    if key == "devchannel":
                        irc.queueMsg(ircmsgs.notice(nick, "👥 Utilisation : !bac config set devchannel <#salon>"))
                        return
                    if key == "allowedchannel":
                        irc.queueMsg(ircmsgs.notice(nick, "🔐 Utilisation : !bac config set allowedchannel <#salon>"))
                        return
                    if key == "allowedchannel":
                        irc.queueMsg(ircmsgs.notice(nick, "🔐 Utilisation : !bac config set errorchannel <#salon>"))
                        return
                    if key == "autostart":
                        irc.queueMsg(ircmsgs.notice(nick, "🚀 Utilisation : !bac config set autostart <on/off>"))
                        return
                    if key == "api":
                        irc.queueMsg(ircmsgs.notice(nick, "🌐 Utilisation API :"))
                        irc.queueMsg(ircmsgs.notice(nick, "  !bac config set api on/off"))
                        irc.queueMsg(ircmsgs.notice(nick, "  !bac config set api autostart <on/off>"))
                        irc.queueMsg(ircmsgs.notice(nick, "  !bac config set api address <adresse>"))
                        irc.queueMsg(ircmsgs.notice(nick, "  !bac config set api port <port>"))
                        return

                    irc.queueMsg(ircmsgs.notice(nick, "❌ Option inconnue. Tape !bac config set pour la liste."))
                    return

                # 3) Ici, on a : !bac config set <option> <valeur>
                
                # -----------------------------
                # SET duration
                # -----------------------------
                if key == "duration":
                    conf.supybot.plugins.PetitBac.roundDuration.set(int(subkey))
                    irc.queueMsg(ircmsgs.notice(nick, f"⏳ Durée mise à {subkey} sec."))
                    return

                # -----------------------------
                # SET rotation
                # -----------------------------
                if key == "rotation":
                    conf.supybot.plugins.PetitBac.categoryRotation.set(int(subkey))
                    irc.queueMsg(ircmsgs.notice(nick, f"🔄 Rotation mise à {subkey} manches."))
                    return

                # -----------------------------
                # SET maxidle
                # -----------------------------
                if key == "maxidle":
                    conf.supybot.plugins.PetitBac.maxIdleRounds.set(int(subkey))
                    irc.queueMsg(ircmsgs.notice(nick, f"💤 Arrêt après {subkey} manches sans réponse."))
                    return

                # -----------------------------
                # SET categories
                # -----------------------------
                if key == "categories":
                    conf.supybot.plugins.PetitBac.categoryCount.set(int(subkey))
                    irc.queueMsg(ircmsgs.notice(nick, f"📚 Catégories par manche : {subkey}."))
                    return

                # -----------------------------
                # SET maxrounds
                # -----------------------------
                if key == "maxrounds":
                    conf.supybot.plugins.PetitBac.maxRounds.set(int(subkey))
                    irc.queueMsg(ircmsgs.notice(nick, f"🏁 Manches max : {subkey}."))
                    return
                    
                # -----------------------------
                # SET autostart
                # -----------------------------
                if key == "autostart":
                    conf.supybot.plugins.PetitBac.autostart.setValue(subkey == "on")
                    irc.queueMsg(ircmsgs.notice(nick, f"🚀 Autostart {subkey}."))
                    return

                #-----------------------------
                # SET message (annonce automatique)
                # -----------------------------
                if key == "message":
                    # Si on veut activer l'annonce → vérifier announceChannel
                    if subkey == "on":
                        if not announce_channel:
                            irc.queueMsg(ircmsgs.notice(nick,
                                "❌ Impossible d'activer l'annonce automatique : aucun salon n'est configuré."))
                            irc.queueMsg(ircmsgs.notice(nick,
                                "➡️ Configurez d'abord un salon avec : !bac config set announcechannel <#salon>"))
                            return

                    conf.supybot.plugins.PetitBac.announceMessage.setValue(subkey == "on")
                    irc.queueMsg(ircmsgs.notice(nick, f"📣 Annonce automatique {subkey}."))
                    return
                    
                # -----------------------------
                # SET announcechannel
                # -----------------------------
                if key == "announcechannel":
                    conf.supybot.plugins.PetitBac.announceChannel.setValue(subkey)
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"📢 Salon d'annonce défini sur : {subkey}"))
                    return

                # -----------------------------
                # SET devchannel
                # -----------------------------
                if key == "devchannel":
                    self.setRegistryValue('devChannel', subkey, channel=channel)
                    irc.queueMsg(ircmsgs.notice(nick, f"👥 Salon DEV : {subkey}"))
                    return

                # -----------------------------
                # SET allowed
                # -----------------------------
                if key == "allowedchannel":
                    conf.supybot.plugins.PetitBac.allowedChannel.setValue(subkey)
                    irc.queueMsg(ircmsgs.notice(nick, f"🔐 Salon autorisé : {subkey}"))
                    return
                    
                # -----------------------------
                # SET errorchannel
                # -----------------------------
                if key == "errorchannel":
                    conf.supybot.plugins.PetitBac.errorChannel.setValue(subkey)
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"⚠️ Salon des erreurs défini sur : {subkey}"))
                    return

                # -----------------------------
                # API CONFIGURATION
                # -----------------------------

                # SET api address <adresse>
                if key == "api" and subkey == "address":
                    if not val:
                        irc.queueMsg(ircmsgs.notice(nick, "❌ Utilisation : !bac config set api address <adresse>"))
                        return
                    conf.supybot.plugins.PetitBac.apiAddress.setValue(val)
                    irc.queueMsg(ircmsgs.notice(nick, f"🌐 Adresse API : {val}"))
                    return

                # SET api port <port>
                if key == "api" and subkey == "port":
                    if not val:
                        irc.queueMsg(ircmsgs.notice(nick, "❌ Utilisation : !bac config set api port <port>"))
                        return
                    try:
                        port_value = int(val)
                    except ValueError:
                        irc.queueMsg(ircmsgs.notice(nick, "❌ Le port doit être un nombre."))
                        return
                    conf.supybot.plugins.PetitBac.apiPort.setValue(port_value)
                    irc.queueMsg(ircmsgs.notice(nick, f"🔌 Port API : {port_value}"))
                    return

                # SET api autostart on/off
                if key == "api" and subkey == "autostart":
                    if val not in ("on", "off"):
                        irc.queueMsg(ircmsgs.notice(nick, "❌ Utilisation : !bac config set api autostart <on/off>"))
                        return
                    conf.supybot.plugins.PetitBac.apiAutostart.setValue(val == "on")
                    irc.queueMsg(ircmsgs.notice(nick, f"🚀 API autostart {val}."))
                    return

                # SET api on/off
                if key == "api" and subkey in ("on", "off"):
                    conf.supybot.plugins.PetitBac.apiEnabled.setValue(subkey == "on")
                    irc.queueMsg(ircmsgs.notice(nick, f"🌐 API {subkey}."))
                    return

                # Mauvaise syntaxe API
                if key == "api":
                    irc.queueMsg(ircmsgs.notice(nick, "❌ Syntaxe API :"))
                    irc.queueMsg(ircmsgs.notice(nick, "  • !bac config set api on/off"))
                    irc.queueMsg(ircmsgs.notice(nick, "  • !bac config set api autostart on/off"))
                    irc.queueMsg(ircmsgs.notice(nick, "  • !bac config set api address <adresse>"))
                    irc.queueMsg(ircmsgs.notice(nick, "  • !bac config set api port <port>"))
                    return

                # Clé inconnue
                irc.queueMsg(ircmsgs.notice(nick,
                    "❌ Clé inconnue. Options : duration, rotation, maxidle, categories, maxrounds, autostart, message, devchannel, allowed, api"))
                return

        # -----------------------------
        # VERIFICATION DES MOTS
        # -----------------------------
        if action == "verif":
            if not sub:
                irc.queueMsg(ircmsgs.notice(nick,
                    "❌ Utilisation : !bac verif <list|ok|del|mod|exclure|typos>"))
                return

            # -----------------------------
            # list — afficher les mots en attente
            # -----------------------------
            if sub == "list":

                if not self.pending_verifications:
                    irc.queueMsg(ircmsgs.notice(nick, "📭 Aucun mot en attente de vérification."))
                    return

                irc.queueMsg(ircmsgs.notice(nick, "📋 Mots en attente :"))

                for vid, data in sorted(self.pending_verifications.items(), key=lambda x: int(x[0])):
                    mot = data["mot"]
                    cat = data["categorie"]
                    auteur = data["auteur"]
                    ts = datetime.datetime.fromtimestamp(data["timestamp"]).strftime("%d/%m/%Y %H:%M")

                    irc.queueMsg(ircmsgs.notice(nick,
                        f"  • ID {vid} — « {mot} » → {cat} (par {auteur}, le {ts})"))

                return

            # -----------------------------
            # ok — valider un mot
            # -----------------------------
            elif sub == "ok":
                if not arg1:
                    irc.queueMsg(ircmsgs.notice(nick, "❌ Utilisation : !bac verif ok <id|mot|all>"))
                    return

                target = arg1.lower().strip()

                # 🔥 Cas spécial : valider tous les mots
                if target == "all":
                    total = len(self.pending_verifications)

                    if total == 0:
                        irc.queueMsg(ircmsgs.notice(nick, "📭 Aucun mot en attente de vérification."))
                        return

                    validated = 0
                    removed = 0
                    blocked = 0
                    
                    for vid, data in list(self.pending_verifications.items()):
                        mot = data["mot"].lower().strip()
                        cat = self._norm_cat(data["categorie"])
                        auteur = data.get("auteur", None)

                        # Catégorie inexistante → impossible à valider
                        if cat not in self.data["categories"]:
                            blocked += 1
                            continue

                        # Mot existe déjà dans la même catégorie → on supprime l'entrée
                        if mot in self.data["categories"][cat]["mots"]:
                            removed += 1
                            del self.pending_verifications[vid]
                            continue

                        # Mot existe dans une autre catégorie → multicat nécessaire
                        existing = [
                            c for c, content in self.data["categories"].items()
                            if mot in content["mots"]
                        ]

                        if existing and cat not in existing:
                            blocked += 1
                            continue

                        # Ajout du mot
                        self.data["categories"][cat]["mots"].add(mot)
                        
                        if auteur:
                            auteur_clean = auteur.replace(" (IA)", "")
                            points = 0.5 if "(ia)" in auteur.lower() else 1
                            self._reward_user(auteur_clean, mot, points)
                        
                        # 🔥 Whitelist auto
                        if self._should_be_whitelisted(mot):
                            if mot not in self.data["whitelist"]:
                                self.data["whitelist"].append(mot)

                        # Mise à jour multicat
                        existing_cats = [
                            c for c, content in self.data["categories"].items()
                            if mot in content["mots"]
                        ]
                        if len(existing_cats) > 1:
                            self.data["multicat"][mot] = existing_cats

                        del self.pending_verifications[vid]
                        validated += 1

                    self._save_categories_json()
                    self._save_pending_verifications()
                    
                    # Après la boucle :
                    self._send_reward_summary(irc, allowed, auteur_clean)
                    
                    # Message final détaillé
                    irc.queueMsg(ircmsgs.notice(nick, "📊 Résultat de la validation :"))
                    irc.queueMsg(ircmsgs.notice(nick, f"✔ Validés : {validated}"))
                    irc.queueMsg(ircmsgs.notice(nick, f"🗑 Supprimés (doublons) : {removed}"))
                    irc.queueMsg(ircmsgs.notice(nick, f"⚠ En attente (multicatégorie ou erreur) : {blocked}"))
                    irc.queueMsg(ircmsgs.notice(nick, f"📦 Total initial : {total}"))

                    return

                # 🔍 Validation d’un seul mot
                found_id = None
                for vid, data in self.pending_verifications.items():
                    if vid == target or data["mot"].lower() == target:
                        found_id = vid
                        break

                if not found_id:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"❌ Aucun mot en attente correspondant à « {target} »."))
                    return

                entry = self.pending_verifications[found_id]
                mot = entry["mot"].lower().strip()
                cat = self._norm_cat(entry["categorie"])
                auteur = entry.get("auteur", None)

                # Vérifier si le mot existe déjà ailleurs
                existing = [
                    c for c, content in self.data["categories"].items()
                    if mot in content["mots"]
                ]

                if existing and cat not in existing:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"⚠️ Le mot « {mot} » existe déjà dans « {existing[0]} »."))
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"➡️ Utilisez : !bac mots multicat add {mot} {cat}"))
                    return
                    
                # 🔒 Empêcher d'ajouter un mot déjà présent dans la même catégorie
                if mot in self.data["categories"][cat]["mots"]:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"ℹ️ Le mot « {mot} » existe déjà dans la catégorie « {cat} »."))
                    irc.queueMsg(ircmsgs.notice(nick,
                        "🗑️ L’entrée a été retirée de la liste d’attente."))
                    del self.pending_verifications[found_id]
                    self._save_pending_verifications()
                    return

                # Ajouter le mot
                self.data["categories"][cat]["mots"].add(mot)
                
                # 🔥 Whitelist auto
                if self._should_be_whitelisted(mot):
                    if mot not in self.data["whitelist"]:
                        self.data["whitelist"].append(mot)

                # Mise à jour multicat
                existing_cats = [
                    c for c, content in self.data["categories"].items()
                    if mot in content["mots"]
                ]
                if len(existing_cats) > 1:
                    self.data["multicat"][mot] = existing_cats

                del self.pending_verifications[found_id]

                self._save_categories_json()
                self._save_pending_verifications()
                
                # --------------------------------------------------------------
                # 🔥 Attribution des points selon l'origine du mot
                # --------------------------------------------------------------
                if auteur:
                    auteur_clean = auteur.replace(" (IA)", "")
                    points = 0.5 if "(ia)" in auteur.lower() else 1
                    self._reward_user(auteur_clean, mot, points)
                    self._send_reward_summary(irc, allowed, auteur_clean)

                irc.queueMsg(ircmsgs.notice(nick,
                    f"✅ Mot « {mot} » validé dans la catégorie « {cat} »."))
                return

            # -----------------------------
            # del — suppression multiple dans la liste d'attente
            # -----------------------------
            elif sub == "del":

                if not arg1:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Utilisation : !bac verif del <id|mot|liste|plage>"))
                    return

                raw = arg1
                tokens = re.split(r"[,\s]+", raw)

                deleted = []
                errors = []

                def delete_pending(target):
                    # Chercher par ID ou par mot
                    for vid, data in list(self.pending_verifications.items()):
                        if vid == target or data["mot"].lower() == target:
                            mot = data["mot"]
                            del self.pending_verifications[vid]
                            deleted.append(mot)
                            return
                    errors.append(f"{target} (introuvable)")

                for t in tokens:

                    # Plage : 3-7
                    if "-" in t and t.replace("-", "").isdigit():
                        start, end = t.split("-", 1)
                        if start.isdigit() and end.isdigit():
                            for i in range(int(start), int(end) + 1):
                                delete_pending(str(i))
                            continue

                    # ID simple
                    if t.isdigit():
                        delete_pending(t)
                        continue

                    # Mot normal
                    delete_pending(self._normalize_word(t))

                self._save_pending_verifications()

                if deleted:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"🗑️ Supprimés de la liste d'attente : {', '.join(deleted)}"))

                if errors:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"⚠️ Erreurs : {', '.join(errors)}"))

                return

            # -----------------------------
            # mod — changer la catégorie d’un mot en attente
            # -----------------------------
            elif sub == "mod":
                # Syntaxe : mod <nouvelle-catégorie> <mot composé|ID>
                if len(parts) < 3:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Utilisation : !bac verif mod <nouvelle-catégorie> <mot|ID>"))
                    return

                newcat = self._norm_cat(parts[1])

                if newcat not in self.data["categories"]:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"❌ La catégorie « {newcat} » n'existe pas."))
                    return

                raw_target = " ".join(parts[2:]).strip().lower()

                found_id = None
                for vid, data in self.pending_verifications.items():
                    if vid == raw_target or data["mot"].lower() == raw_target:
                        found_id = vid
                        break

                if not found_id:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"❌ Aucun mot en attente correspondant à « {raw_target} »."))
                    return

                # Vérifier si le mot existe déjà ailleurs
                mot = self.pending_verifications[found_id]["mot"].lower().strip()
                existing = [
                    c for c, content in self.data["categories"].items()
                    if mot in content["mots"]
                ]

                if existing and newcat not in existing:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"⚠️ Le mot « {mot} » existe déjà dans « {existing[0]} »."))
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"➡️ Utilisez : !bac mots multicat add {mot} {newcat}"))
                    return

                self.pending_verifications[found_id]["categorie"] = newcat
                self._save_pending_verifications()

                irc.queueMsg(ircmsgs.notice(nick,
                    f"🔁 Mot « {mot} » (ID {found_id}) déplacé vers la catégorie « {newcat} »."))
                return
            
            # -----------------------------
            # verif exclure — ajouter un mot dans les exclusion IA
            # -----------------------------
            elif sub == "exclure":
                # Syntaxe : !bac verif exclure <catégorie> <mot|ID>
                if len(parts) < 3:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Utilisation : !bac verif exclure <catégorie> <mot|ID>"))
                    return

                categorie = self._norm_cat(arg1)
                target = " ".join(parts[2:]).strip().lower()

                if categorie not in self.data["categories"]:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"❌ La catégorie « {categorie} » n'existe pas."))
                    return

                # Trouver l'entrée dans la liste d'attente (ID OU mot)
                found_id = None
                for vid, data in self.pending_verifications.items():
                    if vid == target or data["mot"].lower() == target:
                        found_id = vid
                        break

                if not found_id:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"❌ Aucun mot en attente correspondant à « {target} »."))
                    return

                mot = self.pending_verifications[found_id]["mot"].lower()

                # Ajouter dans exclusions IA
                excl = self.data.setdefault("ia", {}).setdefault("exclusions", {})
                excl.setdefault(mot, [])
                if categorie not in excl[mot]:
                    excl[mot].append(categorie)

                # Retirer de la file d'attente
                del self.pending_verifications[found_id]

                self._save_categories_json()
                self._save_pending_verifications()

                irc.queueMsg(ircmsgs.notice(nick,
                    f"🚫 Mot « {mot} » exclu pour la catégorie « {categorie} »."))
                return

            # -----------------------------
            # verif typo — ajouter une correction automatique IA
            # -----------------------------
            elif sub == "typos":
                # Syntaxe : !bac verif typos <catégorie> <mot composé> = <correction composée>
                if len(parts) < 3:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Utilisation : !bac verif typos <catégorie> <mot> = <correction>"))
                    return

                categorie = self._norm_cat(arg1)

                if categorie not in self.data["categories"]:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"❌ La catégorie « {categorie} » n'existe pas."))
                    return

                # Reconstituer tout ce qui suit la catégorie
                raw = " ".join(parts[2:]).strip()

                # Vérifier la présence du séparateur "="
                if "=" not in raw:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Format invalide. Utilisez : <mot> = <correction>"))
                    return

                mot_brut, correction_brut = map(str.strip, raw.split("=", 1))

                mot = self._normalize_word(mot_brut)
                correction = self._normalize_word(correction_brut)

                if not mot or not correction:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Mot ou correction invalide."))
                    return

                # Trouver l'entrée dans la liste d'attente (ID OU mot)
                found_id = None
                for vid, data in self.pending_verifications.items():
                    if vid == mot or data["mot"].lower() == mot:
                        found_id = vid
                        break

                if not found_id:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"❌ Aucun mot en attente correspondant à « {mot} »."))
                    return

                mot_original = self.pending_verifications[found_id]["mot"].lower()

                # Ajouter dans typos IA
                typos = self.data.setdefault("ia", {}).setdefault("typos", {})
                typos[mot_original] = correction

                # Retirer de la file d'attente
                del self.pending_verifications[found_id]

                self._save_categories_json()
                self._save_pending_verifications()

                irc.queueMsg(ircmsgs.notice(nick,
                    f"✏️ Correction IA ajoutée : {mot_original} → {correction}"))
                return
                
        # -----------------------------
        # STATS
        # -----------------------------
        if action == "stats":
            if not sub:
                irc.queueMsg(ircmsgs.notice(nick,
                    "🧮 Utilisation : !bac stats <reset|delete|purge|joueurs>"))
                irc.queueMsg(ircmsgs.notice(nick,
                    "🧮 Pour visualiser les stats, utilisez !stat sur le salon."))
                irc.queueMsg(ircmsgs.notice(nick,
                    "🧮 Exemples :"))
                irc.queueMsg(ircmsgs.notice(nick,
                    "   • !bac stats reset global"))
                irc.queueMsg(ircmsgs.notice(nick,
                    "   • !bac stats reset semaine"))
                irc.queueMsg(ircmsgs.notice(nick,
                    "   • !bac stats reset <pseudo>"))
                irc.queueMsg(ircmsgs.notice(nick,
                    "   • !bac stats delete <pseudo>"))
                irc.queueMsg(ircmsgs.notice(nick,
                    "   • !bac stats purge <jours>"))
                irc.queueMsg(ircmsgs.notice(nick,
                    "   • !bac stats purge fantomes (suppression des joueurs fantomes)"))
                return

            # -----------------------------
            # stats reset global
            # -----------------------------
            if sub == "reset" and arg1 == "global":
                self.global_stats["global"] = {
                    "games_played": 0,
                    "rounds_played": 0,
                    "words_validated": 0,
                    "full_combos": 0,
                    "total_points": 0,
                    "last_activity": None
                }
                self._save_global_stats()
                irc.queueMsg(ircmsgs.notice(nick, "🧹 Statistiques globales réinitialisées."))
                return

            # -----------------------------
            # stats reset semaine
            # -----------------------------
            if sub == "reset" and arg1 == "semaine":
                self.global_stats["weekly"] = {
                    "start": time.time(),
                    "best_score": {"user": None, "points": 0},
                    "best_full_combos": {"user": None, "count": 0},
                    "best_speed": {"user": None, "seconds": 9999}
                }
                self._save_global_stats()
                irc.queueMsg(ircmsgs.notice(nick, "📅 Statistiques hebdomadaires réinitialisées."))
                return

            # -----------------------------
            # stats reset <pseudo>
            # -----------------------------
            if sub == "reset" and arg1 and not arg2:
                key = self._resolve_player_id(arg1)
                if not key:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"❌ Joueur ou ID « {arg1} » introuvable."))
                    irc.queueMsg(ircmsgs.notice(nick,
                        "ℹ️ Utilisation : !bac stats reset <pseudo>"))
                    return

                self.global_stats["players"][key] = {
                    "games_played": 0,
                    "rounds_played": 0,
                    "words_validated": 0,
                    "full_combos": 0,
                    "total_points": 0,
                    "last_seen": None
                }
                self._save_global_stats()
                irc.queueMsg(ircmsgs.notice(nick,
                    f"🧹 Statistiques du joueur « {key} » réinitialisées."))
                return

            # -----------------------------
            # stats delete <pseudo>
            # -----------------------------
            if sub == "delete" and arg1 and not arg2:
                key = self._resolve_player_id(arg1)
                if not key:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"❌ Joueur ou ID « {arg1} » introuvable."))
                    irc.queueMsg(ircmsgs.notice(nick,
                        "ℹ️ Utilisation : !bac stats delete <pseudo>"))
                    return

                del self.global_stats["players"][key]
                self._save_global_stats()
                irc.queueMsg(ircmsgs.notice(nick,
                    f"🗑 Joueur « {key} » supprimé des statistiques."))
                return
                
            # -----------------------------
            # stats purge fantomes
            # -----------------------------
            if sub == "purge" and arg1 == "fantomes":
                players = self.global_stats.get("players", {})
                to_delete = []

                for user, stats in players.items():
                    if (
                        stats.get("total_points", 0) == 0 and
                        stats.get("rounds_played", 0) == 0 and
                        stats.get("words_validated", 0) == 0 and
                        stats.get("full_combos", 0) == 0
                    ):
                        to_delete.append(user)

                for user in to_delete:
                    del players[user]

                self._save_global_stats()

                irc.queueMsg(ircmsgs.notice(nick,
                    f"🧹 {len(to_delete)} joueur(s) fantôme(s) supprimé(s)."))
                return

            # -----------------------------
            # stats purge <jours>
            # -----------------------------
            if sub == "purge" and not arg1:
                irc.queueMsg(ircmsgs.notice(nick,
                    "❌ Utilisation : !bac stats purge <jours>"))
                irc.queueMsg(ircmsgs.notice(nick,
                    "ℹ️ Exemple : !bac stats purge 30"))
                irc.queueMsg(ircmsgs.notice(nick,
                    "❌ Utilisation : !bac stats purge fantomes (suppression des joueurs fantomes)"))
                return

            if sub == "purge" and arg1 and not arg2:
                try:
                    days = int(arg1)
                except ValueError:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Utilisation : !bac stats purge <jours>"))
                    irc.queueMsg(ircmsgs.notice(nick,
                        "ℹ️ Exemple : !bac stats purge 30"))
                    return

                now = time.time()
                cutoff = now - (days * 24 * 3600)

                players = self.global_stats.get("players", {})
                to_delete = []

                for user, stats in players.items():
                    last_seen = stats.get("last_seen")
                    if not last_seen:
                        continue

                    try:
                        ts = time.mktime(time.strptime(last_seen, "%d/%m/%Y %H:%M:%S"))
                    except:
                        continue

                    if ts < cutoff:
                        to_delete.append(user)

                for user in to_delete:
                    del players[user]

                self._save_global_stats()

                irc.queueMsg(ircmsgs.notice(nick,
                    f"🧹 {len(to_delete)} joueur(s) inactif(s) depuis plus de {days} jours ont été supprimés."))
                return
                
            # -----------------------------
            # stats joueurs
            # -----------------------------
            if sub == "joueurs":
                players = self.global_stats.get("players", {})
                if not players:
                    irc.queueMsg(ircmsgs.notice(nick, "📭 Aucun joueur enregistré."))
                    return

                now = time.time()

                def activity_score(stats):
                    # Convertir last_seen en timestamp
                    last_seen = stats.get("last_seen")
                    if last_seen:
                        try:
                            ts = time.mktime(time.strptime(last_seen, "%d/%m/%Y %H:%M:%S"))
                        except:
                            ts = 0
                    else:
                        ts = 0

                    # Score d'activité :
                    # 1) Joueurs actifs (moins de 7 jours)
                    # 2) Joueurs ayant déjà joué
                    # 3) Joueurs fantômes
                    has_played = (
                        stats.get("total_points", 0) > 0 or
                        stats.get("rounds_played", 0) > 0 or
                        stats.get("words_validated", 0) > 0
                    )

                    recent = (now - ts) < (7 * 86400)

                    return (
                        0 if recent else
                        1 if has_played else
                        2,
                        -ts,  # plus récent en premier
                        -stats.get("total_points", 0)  # départage
                    )

                sorted_players = sorted(players.items(), key=lambda x: activity_score(x[1]))

                irc.queueMsg(ircmsgs.notice(nick, f"📋 Joueurs enregistrés ({len(players)}) :"))

                for idx, (user, stats) in enumerate(sorted_players, start=1):
                    pts = stats.get("total_points", 0)
                    rounds = stats.get("rounds_played", 0)
                    combos = stats.get("full_combos", 0)
                    last_seen = stats.get("last_seen", "N/A")

                    # Calcul inactivité
                    if last_seen and last_seen != "N/A":
                        try:
                            ts = time.mktime(time.strptime(last_seen, "%d/%m/%Y %H:%M:%S"))
                            days_inactive = int((now - ts) / 86400)
                            inactive_str = f"{days_inactive} j"
                        except:
                            inactive_str = "?"
                    else:
                        inactive_str = "N/A"

                    irc.queueMsg(ircmsgs.notice(
                        nick,
                        f"  {idx}. {user} — {pts} pts, {rounds} manches, {combos} combos — Dernière activité : {last_seen} ({inactive_str})"
                    ))
                return

            # -----------------------------
            # Aide si subcommand inconnue
            # -----------------------------
            irc.queueMsg(ircmsgs.notice(nick,
                f"❌ Sous-commande inconnue : {sub}"))
            irc.queueMsg(ircmsgs.notice(nick,
                "🧮 Utilisation : !bac stats <reset|delete|purge|joueurs>"))
            return
             
        # -----------------------------
        # BUGS
        # -----------------------------
        if action == "bug":
            if not sub:
                irc.queueMsg(ircmsgs.notice(nick,
                    "🐞 Syntaxe : !bac bug list / voir <id> / del <id>"))
                return

            file_path = os.path.join(self.storageDir, "bugs.json")
            data = self._load_json(file_path) if hasattr(self, "_load_json") else []

            # list
            if sub == "list":
                if not data:
                    irc.queueMsg(ircmsgs.notice(nick, "📭 Aucun bug enregistré."))
                    return

                irc.queueMsg(ircmsgs.notice(nick, f"🐞 Liste des bugs ({len(data)}) :"))
                for entry in data:
                    irc.queueMsg(ircmsgs.notice(
                        nick,
                        f"[{entry['id']}] {entry['user']} : {entry['message']} ({entry['timestamp']})"
                    ))
                return

            # voir <id>
            if sub == "voir" and arg1:
                try:
                    bug_id = int(arg1)
                except:
                    irc.queueMsg(ircmsgs.notice(nick, "❌ ID invalide."))
                    return

                entry = next((e for e in data if e["id"] == bug_id), None)
                if not entry:
                    irc.queueMsg(ircmsgs.notice(nick, "❌ Bug introuvable."))
                    return

                irc.queueMsg(ircmsgs.notice(
                    nick,
                    f"[{entry['id']}] {entry['user']} : {entry['message']} ({entry['timestamp']})"
                ))
                return

            # del <id>
            if sub == "del" and arg1:
                try:
                    bug_id = int(arg1)
                except:
                    irc.queueMsg(ircmsgs.notice(nick, "❌ ID invalide."))
                    return

                new_data = [e for e in data if e["id"] != bug_id]
                if len(new_data) == len(data):
                    irc.queueMsg(ircmsgs.notice(nick, "❌ Bug introuvable."))
                    return

                self._save_json(file_path, new_data)
                irc.queueMsg(ircmsgs.notice(nick, f"🗑 Bug {bug_id} supprimé."))
                return

            irc.queueMsg(ircmsgs.notice(nick,
                "❌ Syntaxe : !bac bug list / voir <id> / del <id>"))
            return
            
        # -----------------------------
        # SUGGESTIONS
        # -----------------------------
        if action == "suggestion":
            if not sub:
                irc.queueMsg(ircmsgs.notice(nick,
                    "💡 Syntaxe : !bac suggestion list / voir <id> / del <id>"))
                return

            file_path = os.path.join(self.storageDir, "suggestions.json")
            data = self._load_json(file_path) if hasattr(self, "_load_json") else []

            # list
            if sub == "list":
                if not data:
                    irc.queueMsg(ircmsgs.notice(nick, "📭 Aucune suggestion enregistrée."))
                    return

                irc.queueMsg(ircmsgs.notice(nick, f"💡 Suggestions enregistrées ({len(data)}) :"))
                for entry in data:
                    irc.queueMsg(ircmsgs.notice(
                        nick,
                        f"[{entry['id']}] {entry['user']} : {entry['message']} ({entry['timestamp']})"
                    ))
                return

            # voir <id>
            if sub == "voir" and arg1:
                try:
                    sug_id = int(arg1)
                except:
                    irc.queueMsg(ircmsgs.notice(nick, "❌ ID invalide."))
                    return

                entry = next((e for e in data if e["id"] == sug_id), None)
                if not entry:
                    irc.queueMsg(ircmsgs.notice(nick, "❌ Suggestion introuvable."))
                    return

                irc.queueMsg(ircmsgs.notice(
                    nick,
                    f"[{entry['id']}] {entry['user']} : {entry['message']} ({entry['timestamp']})"
                ))
                return

            # del <id>
            if sub == "del" and arg1:
                try:
                    sug_id = int(arg1)
                except:
                    irc.queueMsg(ircmsgs.notice(nick, "❌ ID invalide."))
                    return

                new_data = [e for e in data if e["id"] != sug_id]
                if len(new_data) == len(data):
                    irc.queueMsg(ircmsgs.notice(nick, "❌ Suggestion introuvable."))
                    return

                self._save_json(file_path, new_data)
                irc.queueMsg(ircmsgs.notice(nick, f"🗑 Suggestion {sug_id} supprimée."))
                return

            irc.queueMsg(ircmsgs.notice(nick,
                "❌ Syntaxe : !bac suggestion list / voir <id> / del <id>"))
            return
            
        # -----------------------------
        # DEBUG
        # -----------------------------
        if action == "debug":
            if not sub:
                irc.queueMsg(ircmsgs.notice(nick,
                    "🛠️ Syntaxe : !bac debug ia <mot>"))
                return

            # -----------------------------
            # DEBUG IA
            # -----------------------------
            if sub == "ia":
                if not arg1:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "🛠️ Utilisation : !bac debug ia <mot>"))
                    return

                mot = arg1.lower()

                # Catégories autorisées (si partie active)
                allowed = None
                if channel in self.active_games:
                    allowed = self.active_games[channel]["categories"]

                # Résumé Wikipédia
                summary = None
                if mot in self.dictionnaire:
                    summary = True
                else:
                    summary = self.get_wikipedia_summary(mot)

                # Analyse IA détaillée
                details = self._debug_guess_category(
                    mot,
                    summary if isinstance(summary, str) else None,
                    allowed
                )

                irc.queueMsg(ircmsgs.notice(nick, f"🔍 Analyse IA pour « {mot} »"))

                # Résumé Wikipédia
                if isinstance(summary, str):
                    short = summary[:200].replace("\n", " ") + "..."
                else:
                    short = "Aucun résumé trouvé"
                irc.queueMsg(ircmsgs.notice(nick, f"📘 Wikipédia : {short}"))

                # Scores lexicaux
                irc.queueMsg(ircmsgs.notice(nick, "📊 Scores lexicaux :"))
                for cat, score in details["lexical"].items():
                    irc.queueMsg(ircmsgs.notice(nick, f"  • {cat} : {score:.2f}"))

                # Scores Wikipédia
                irc.queueMsg(ircmsgs.notice(nick, "🔎 Scores Wikipédia :"))
                for cat, info in details["wiki"].items():
                    keys = ", ".join(info["found"]) if info["found"] else "—"
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"  • {cat} : +{info['score']:.2f} ({keys})"))

                # Score final
                irc.queueMsg(ircmsgs.notice(nick, "🏁 Score final :"))
                for cat, score in details["final"].items():
                    irc.queueMsg(ircmsgs.notice(nick, f"  • {cat} : {score:.2f}"))

                # Décision
                if details["decision"]:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"✅ Catégorie devinée : {details['decision']}"))
                else:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"❌ IA refuse — {details['reason']}"))

                return

            # -----------------------------
            # Mauvaise syntaxe
            # -----------------------------
            irc.queueMsg(ircmsgs.notice(nick,
                "❌ Syntaxe : !bac stats reset global|semaine|<pseudo> / delete <pseudo> / list joueurs / purge <jours>"))
            return 

        # -----------------------------
        # MODE — gestion des modes de jeu
        # -----------------------------
        if action == "modes":

            if not sub:
                irc.queueMsg(ircmsgs.notice(nick,
                    "❌ Utilisation : !bac modes <list|info|mod|del> [nom]"))
                return
                
            # -----------------------------
            # ADD
            # -----------------------------
            if sub == "add":
                if len(parts) < 5:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Utilisation : !bac modes add <nom> <cat> <durée> <manches>"))
                    return

                name = parts[1].lower()

                try:
                    categories = int(parts[2])
                    duration   = int(parts[3])
                    maxrounds  = int(parts[4])
                except:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Les valeurs doivent être des nombres."))
                    return

                # 🔥 RÈGLE : minimum 3 catégories
                if categories < 3:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Un mode doit contenir au minimum 3 catégories."))
                    return

                now = int(time.time())

                self.modes[name] = {
                    "display": name.capitalize(),
                    "categories": categories,
                    "duration": duration,
                    "maxrounds": maxrounds,
                    "created_at": now,
                    "created_by": nick,
                    "last_used": None,
                    "times_used": 0,
                    "locked": True,
                    "modified_at": None,
                    "modified_by": None
                }

                self._save_json(self.modes_file, self.modes)

                irc.queueMsg(ircmsgs.notice(nick,
                    f"🔒 Mode officiel « {name} » créé et verrouillé."))
                return

            # -----------------------------
            # LIST 
            # -----------------------------
            if sub == "list":
                now = int(time.time())
                two_months = 60 * 24 * 3600  # 60 jours

                # Trier du plus utilisé au moins utilisé
                modes = sorted(
                    self.modes.items(),
                    key=lambda x: x[1].get("times_used", 0),
                    reverse=True
                )

                irc.queueMsg(ircmsgs.notice(nick, "📋 Modes classés par utilisation :"))

                for key, cfg in modes:
                    name = cfg.get("display", key)
                    used = cfg.get("times_used", 0)
                    last = cfg.get("last_used")
                    locked = cfg.get("locked", False)

                    # Cadenas si verrouillé
                    lock_icon = " 🔒" if locked else ""

                    # Dernière utilisation
                    if last:
                        ago = self._time_ago(last)
                        last_str = f"{ago}"
                    else:
                        last_str = "jamais"

                    # Inactif si > 2 mois
                    inactive = ""
                    if last and (now - last > two_months):
                        inactive = " (inactif)"

                    irc.queueMsg(ircmsgs.notice(
                        nick,
                        f"• {name}{lock_icon} — {used} utilisation(s), dernier usage : {last_str}{inactive}"
                    ))

                return

            # -----------------------------
            # INFO
            # -----------------------------
            if sub == "info":
                if not arg1:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Utilisation : !bac modes info <nom>"))
                    return

                target = arg1.lower()

                if target not in self.modes:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"❌ Aucun mode nommé « {target} »."))
                    return

                cfg = self.modes[target]
                locked = cfg.get("locked", False)
                lock_icon = " 🔒" if locked else ""

                def fmt(ts):
                    if not ts:
                        return "Jamais"
                    date_str = time.strftime("%d-%m-%Y %H:%M:%S", time.localtime(ts))
                    ago = self._time_ago(ts)
                    return f"{date_str} ({ago})"

                irc.queueMsg(ircmsgs.notice(nick, f"ℹ️ Mode « {cfg.get('display', target)} »{lock_icon} :"))
                irc.queueMsg(ircmsgs.notice(nick, f"  • Créé par : {cfg.get('created_by', 'inconnu')}"))
                irc.queueMsg(ircmsgs.notice(nick, f"  • Créé le : {fmt(cfg.get('created_at'))}"))
                irc.queueMsg(ircmsgs.notice(nick, f"  • Dernière utilisation : {fmt(cfg.get('last_used'))}"))
                irc.queueMsg(ircmsgs.notice(nick, f"  • Utilisé : {cfg.get('times_used', 0)} fois"))
                irc.queueMsg(ircmsgs.notice(nick, f"  • Catégories : {cfg['categories']}"))
                irc.queueMsg(ircmsgs.notice(nick, f"  • Durée : {cfg['duration']} sec"))
                irc.queueMsg(ircmsgs.notice(nick, f"  • Manches : {cfg['maxrounds']}"))
                irc.queueMsg(ircmsgs.notice(nick, f"  • Verrouillé : {'oui' if locked else 'non'}"))
                return

            # -----------------------------
            # MOD — modifier un mode
            # -----------------------------
            if sub == "mod":
                if len(parts) < 5:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Utilisation : !bac modes mod <nom> <cat> <durée> <manches>"))
                    return

                target = parts[1].lower()

                if target not in self.modes:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"❌ Aucun mode nommé « {target} »."))
                    return

                try:
                    categories = int(parts[2])
                    duration   = int(parts[3])
                    maxrounds  = int(parts[4])
                except:
                    irc.queueMsg(ircmsgs.notice(nick, "❌ Les valeurs doivent être des nombres."))
                    return

                cfg = self.modes[target]
                cfg["categories"] = categories
                cfg["duration"] = duration
                cfg["maxrounds"] = maxrounds
                cfg["modified_at"] = int(time.time())
                cfg["modified_by"] = nick

                self._save_json(self.modes_file, self.modes)

                irc.queueMsg(ircmsgs.notice(nick,
                    f"🔧 Mode « {target} » modifié."))
                return

            # -----------------------------
            # DEL — supprimer un mode
            # -----------------------------
            if sub == "del":
                if not arg1:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Utilisation : !bac modes del <nom>"))
                    return

                target = arg1.lower()

                if target not in self.modes:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"❌ Aucun mode nommé « {target} »."))
                    return

                if target in ("facile", "moyen", "difficile"):
                    irc.queueMsg(ircmsgs.notice(nick,
                        "⛔ Impossible de supprimer un mode par défaut."))
                    return

                # 🔒 Mode verrouillé ?
                if self.modes[target].get("locked"):
                    irc.queueMsg(ircmsgs.notice(nick,
                        "⛔ Ce mode est verrouillé et ne peut pas être supprimé."))
                    return

                del self.modes[target]
                self._save_json(self.modes_file, self.modes)

                irc.queueMsg(ircmsgs.notice(nick,
                    f"🗑 Mode « {target} » supprimé."))
                return
            # ----------------------------------------
            # LOCK
            # ----------------------------------------
            if sub == "lock":
                if not arg1:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Utilisation : !bac modes lock <nom>"))
                    return

                target = arg1.lower()

                if target not in self.modes:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"❌ Aucun mode nommé « {target} »."))
                    return

                self.modes[target]["locked"] = True
                self.modes[target]["modified_at"] = int(time.time())
                self.modes[target]["modified_by"] = nick
                self._save_json(self.modes_file, self.modes)

                irc.queueMsg(ircmsgs.notice(nick,
                    f"🔒 Mode « {target} » verrouillé."))
                return            
            # ------------------------------------------
            # UNLOCK
            # ------------------------------------------
            if sub == "unlock":
                if not arg1:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Utilisation : !bac modes unlock <nom>"))
                    return

                target = arg1.lower()

                if target not in self.modes:
                    irc.queueMsg(ircmsgs.notice(nick,
                        f"❌ Aucun mode nommé « {target} »."))
                    return

                self.modes[target]["locked"] = False
                self.modes[target]["modified_at"] = int(time.time())
                self.modes[target]["modified_by"] = nick
                self._save_json(self.modes_file, self.modes)

                irc.queueMsg(ircmsgs.notice(nick,
                    f"🔓 Mode « {target} » déverrouillé."))
                return

            # -----------------------------
            # Sous-commande inconnue
            # -----------------------------
            irc.queueMsg(ircmsgs.notice(nick,
                "❌ Sous‑commande inconnue. Utilisation : !bac modes <liste|info|add|mod|del|lock|unlock>"))
            return

        # -----------------------------
        # Mauvaise syntaxe
        # -----------------------------
        irc.queueMsg(ircmsgs.notice(nick,
            "❌ Syntaxe incorrecte. Utilisez : !bac mots/stats/verif/config/debug/bug/suggestion/modes"))

    # ----------------------------------------------------------------------
    # Info sur un mot à ajouter 
    # ----------------------------------------------------------------------
    def get_wikipedia_summary(self, mot):
        mot_formate = mot.capitalize()
        mot_encoded = urllib.parse.quote(mot_formate)

        template = self._clean_irc_formatting(self.wikipedia_url_template)
        url = template % mot_encoded

        headers = {
            "User-Agent": "PetitBacBot/1.0 (https://www.entrenous.chat; contact: bots@entrenous.chat)"
        }

        try:
            r = requests.get(url, headers=headers, timeout=3)
            data = r.json()

            # Si la page n'existe pas → None
            if "extract" not in data:
                return None

            extract = data["extract"]

            # ⚠️ CORRECTION : si extract est vide → retourner "" au lieu de None
            if extract is None:
                return ""

            summary = extract.strip()

            # Si résumé vide → retourner "" (IA doit quand même tourner)
            if not summary:
                return ""

            # Nettoyage
            summary = summary.replace("\n", " ").replace("\xa0", " ")
            summary = re.sub(r"\s+", " ", summary).strip()

            if summary.endswith(":"):
                summary += " (plusieurs sens possibles)."

            sentences = summary.split(". ")
            if len(sentences) >= 2:
                summary = ". ".join(sentences[:2]) + "."
            else:
                if not summary.endswith("."):
                    summary += "."

            if len(summary) > 300:
                summary = summary[:297].rstrip() + "..."

            return summary

        except Exception as e:
            log.error(f"PetitBac: erreur Wikipedia: {e}")
            return None
        
    def _norm_cat(self, cat):
        # Convertir en minuscules + enlever espaces parasites
        cat = cat.lower().strip()

        # Normalisation Unicode : transforme é, è, ê, ë → e
        cat = unicodedata.normalize("NFD", cat)
        cat = "".join(c for c in cat if unicodedata.category(c) != "Mn")

        return cat
        
    def _normalize_word(self, mot):
        mot = mot.lower().strip()

        # Normalisation des espaces
        mot = " ".join(mot.split())

        # Normalisation avancée des apostrophes
        for bad in ["’", "‘", "‛", "´", "ʼ"]:
            mot = mot.replace(bad, "'")

        while " '" in mot:
            mot = mot.replace(" '", "'")
        while "' " in mot:
            mot = mot.replace("' ", "'")

        # Apostrophe mal placée
        if mot.startswith("'") or mot.endswith("'"):
            return None

        # Apostrophes multiples
        if "''" in mot:
            return None

        # Caractères interdits
        if any(c in mot for c in [",", ";", ":", "/", "\\", "|"]):
            return None

        return mot
        
    def _should_be_whitelisted(self, mot):
        mot_clean = mot.lower().strip()
        raw_words = mot_clean.split()

        # Pas multi-mots → pas whitelist
        if len(raw_words) <= 1:
            return False

        # Déjà whitelisté
        if mot_clean in self.data.get("whitelist", []):
            return False

        # Mots de liaison autorisés
        liaison = {"de", "du", "des", "d'", "l'", "la", "le", "les", "en", "à"}

        # Si un mot de liaison est présent → whitelist
        if any(w in liaison for w in raw_words):
            return True

        # Si tiret → whitelist
        if "-" in mot_clean:
            return True

        # Sinon → multi-mots suspects → whitelist nécessaire
        return True
