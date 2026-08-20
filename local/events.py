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

class EventsMixin:
                                    
    # ----------------------------------------------------------------------
    # Le bot ne lancera le jeu que sur un salon défini
    # ----------------------------------------------------------------------
    def _is_enabled(self, channel):
        allowed = conf.supybot.plugins.PetitBac.allowedChannel()
        return channel.lower() == allowed.lower()

    def _game_channel(self, channel):
        """Clé réelle dans active_games (le serveur / Orbit peuvent varier la casse)."""
        if not channel:
            return channel
        if channel in self.active_games:
            return channel
        cl = channel.lower()
        for ch in self.active_games:
            if ch.lower() == cl:
                return ch
        return channel

    # ----------------------------------------------------------------------
    # Auto-start quand quelqu’un rejoint
    # ----------------------------------------------------------------------
    def doJoin(self, irc, msg):
        channel = msg.args[0]
        nick = msg.nick

        if not self._is_enabled(channel):
            return

        # Ignorer les JOIN du bot lui-même
        if nick == irc.nick:
            return
            
        nick_key = nick.lower()

        if nick_key in self.pending_user_messages:
            entry = self.pending_user_messages[nick_key]
            points = entry.get("points", 0)
            words = entry.get("words", [])

            mots_list = ", ".join(f"« {w} »" for w in words)
            count = len(words)

            irc.queueMsg(ircmsgs.privmsg(
                nick,
                f"🎉 Merci pour tes contributions ! {count} mot(s) validés pendant ton absence : {mots_list} (+{points} points)."
            ))

            # Rien à appliquer ici : les points ont déjà été ajoutés lors de la validation

            del self.pending_user_messages[nick_key]
            self._save_json(
                os.path.join(self.storageDir, "pending_user_messages.json"),
                self.pending_user_messages
            )

        # Si une partie existe (en cours OU en pause)
        if channel in self.active_games:
            game = self.active_games[channel]

            # --- Partie en pause ---
            if game.get("paused"):
                irc.queueMsg(ircmsgs.notice(nick,
                    "⏸️ Une partie du Petit Bac est actuellement en pause. Seul un Opérateur de salon peut la relancer."))
                irc.queueMsg(ircmsgs.notice(nick,
                    "⏸️ La partie sera supprimée au-delà de 5 minutes d'inactivité."))
                irc.queueMsg(ircmsgs.notice(nick,
                    "⏸️ Vous pourrez alors taper !jouer pour en relancer une nouvelle !"))
                self._send_rules_notice(irc, nick)
                return

            # --- Partie en cours ---
            irc.queueMsg(ircmsgs.notice(nick,
                f"👋 Bonjour {nick}. Une partie du Petit Bac est actuellement en cours !"))

            # Envoyer les règles en notice
            self._send_rules_notice(irc, nick)

            # --- Mode actif ---
            mode_name = self.current_mode.get(channel, "facile")
            mode_cfg = self.modes.get(mode_name, self.modes["facile"])

            irc.queueMsg(ircmsgs.notice(nick,
                f"🎮 Mode de jeu actuel : \x0303\x02{mode_name}\x02\x0F"))

            # --- Configuration du jeu ---
            duration = mode_cfg["duration"]
            category_count = mode_cfg["categories"]
            max_rounds = mode_cfg["maxrounds"]
            rotation = conf.supybot.plugins.PetitBac.categoryRotation()

            irc.queueMsg(ircmsgs.notice(nick,
                "⚙️ Configuration de la partie en cours :"))

            irc.queueMsg(ircmsgs.notice(nick,
                f"⏳ Chaque manche dure \x0303\x02{duration}\x02\x0F secondes "
                f"et contient \x0303\x02{category_count}\x02\x0F catégories."))

            irc.queueMsg(ircmsgs.notice(nick,
                f"🔄 Les catégories changent toutes les \x0303\x02{rotation}\x02\x0F manches "
                f"et nous en sommes à la manche \x0303\x02{game['round']} sur {max_rounds}\x02\x0F."))

            irc.queueMsg(ircmsgs.notice(nick,
                f"🎲 Lettre actuelle : \x0304\x02{game['letter']}\x02\x0F | "
                f"Catégories : \x0303\x02{', '.join(game['categories'])}\x02\x0F"))

            irc.queueMsg(ircmsgs.notice(nick,
                f"Bonne chance {nick} !"))
            self._send_state_sync(irc, channel)
            return

        # Si aucune partie n'est en cours et autoStart activé → lancer une partie
        if conf.supybot.plugins.PetitBac.autoStart():
            schedule.addEvent(
                lambda: self._startGame(irc, channel, starter="AutoStart", player=nick, show_rules=True),
                time.time() + 3
            )

    def _pb_tag(self, tags, *names):
        """Lit un tag IRCv3, avec ou sans préfixe +."""
        if not tags:
            return ""
        for name in names:
            for key in (name, "+" + str(name).lstrip("+"), str(name).lstrip("+")):
                val = tags.get(key)
                if val not in (None, ""):
                    return str(val)
        return ""

    def doPrivmsg(self, irc, msg):
        if not msg.args or len(msg.args) < 2:
            return
        text = self._clean_irc_formatting(msg.args[1].strip())
        if text.startswith("!") or text.startswith("@"):
            return
        self._handle_play_word(irc, msg, text)

    def _handle_play_word(self, irc, msg, text, preferred_cat=None):
        channel = self._game_channel(msg.args[0] if msg.args else "")
        nick = msg.nick or ""
        nick_key = nick.lower()

        if not nick or nick == irc.nick:
            return

        if not self._is_enabled(channel):
            return

        if channel not in self.active_games:
            return

        if channel not in self.players:
            self.players[channel] = set()
        if nick not in self.players[channel]:
            self.players[channel].add(nick)
            if channel not in self.mode_vote and channel not in self.restart_vote:
                irc.queueMsg(ircmsgs.privmsg(channel, f"👋 {nick} rejoint la partie !"))

        game = self.active_games[channel]

        # 🔥 Empêcher les joueurs de jouer avant le début réel de la manche
        if not game.get("round_active", False):
            return

        game["last_activity"] = time.time()
        game["idle_rounds"] = 0

        if text.startswith("!") or text.startswith("@"):
            return

        raw_words = text.split()
        mot_clean = text.lower().strip()

        # ----------------------------------------------------------------------
        # WHITELIST
        # ----------------------------------------------------------------------
        if mot_clean not in self.data.get("whitelist", []):
            if len(raw_words) > 1:
                liaison = {"de", "du", "des", "d'", "l'", "la", "le", "les", "en", "à"}
                if not any(w.lower() in liaison for w in raw_words) and "-" not in text:
                    irc.queueMsg(ircmsgs.notice(nick,
                        "❌ Un seul mot par message est autorisé."))
                    irc.queueMsg(ircmsgs.notice(nick,
                        "ℹ️ Si ton mot est composé, utilise les liaisons (ex : « pomme de terre »)."))
                    return

        lettre = game["letter"].lower()
        mot = self._normalize_word(text)

        if not mot:
            irc.queueMsg(ircmsgs.notice(nick,
                f"❌ Le mot « {text} » contient des caractères invalides (,|/ etc...)."))
            return

        # ----------------------------------------------------------------------
        # BLACKLIST
        # ----------------------------------------------------------------------
        if mot in self.data["blacklist"]:
            irc.queueMsg(ircmsgs.notice(nick, f"⛔ Le mot « {mot} » est dans la liste des mots interdits du jeu."))
            chan = irc.state.channels.get(channel)
            if chan:
                for user in chan.users:
                    if chan.isOp(user):
                        irc.queueMsg(ircmsgs.notice(
                            user,
                            f"⚠️ {msg.nick} a tenté d'utiliser un mot blacklisté dans {channel} : « {mot} »"
                        ))
            dev_channel = self.registryValue('devChannel', msg.channel)
            if dev_channel and dev_channel.startswith("#"):
                irc.queueMsg(ircmsgs.privmsg(
                    "BotServ",
                    f"say {dev_channel} [BAC][ALERTE BLACKLIST] {msg.nick} a tenté d'utiliser « {mot} » dans {channel}"
                ))
            return

        # ----------------------------------------------------------------------
        # IA : typos + exclusions
        # ----------------------------------------------------------------------
        typos = self.data.get("ia", {}).get("typos", {})
        if mot in typos:
            original = mot
            mot = typos[mot]
            typo_msgs = self.messages.get("typo_corrections", [])
            if typo_msgs:
                phrase = random.choice(typo_msgs).format(original=original, corrected=mot)
                irc.queueMsg(ircmsgs.notice(nick, phrase))

        exclusions = self.data.get("ia", {}).get("exclusions", {})
        if mot in exclusions:
            for cat in exclusions[mot]:
                if cat in game["categories"]:
                    self._word_ko(irc, channel, nick, mot, "excluded", cat)
                    irc.queueMsg(ircmsgs.privmsg(channel,
                        f"{nick}: ❌ Le mot « {text} » n'est pas valide pour la catégorie {cat}."))
                    return

        if mot in game.get("used_words", set()):
            self._word_ko(irc, channel, nick, mot, "already_used")
            irc.queueMsg(ircmsgs.privmsg(channel,
                f"{nick}: ❌ Le mot « {text} » a déjà été utilisé dans cette partie."))
            return

        # ----------------------------------------------------------------------
        # Vérification de la lettre
        # ----------------------------------------------------------------------
        first_letter = unicodedata.normalize("NFD", mot[0])
        first_letter = "".join(c for c in first_letter if unicodedata.category(c) != "Mn").lower()

        if first_letter != lettre:
            self._word_ko(irc, channel, nick, mot, "wrong_letter")
            irc.queueMsg(ircmsgs.privmsg(channel,
                f"{nick}: ❌ Le mot « {text} » ne commence pas par la lettre {game['letter']}."))
            return

        for used_cat, used_word in game["answers"].get(nick_key, {}).items():
            if used_word == mot:
                self._word_ko(irc, channel, nick, mot, "already_round")
                irc.queueMsg(ircmsgs.privmsg(channel,
                    f"{nick}: ⚠️ Tu as déjà utilisé « {text} » dans cette manche."))
                return

        # ----------------------------------------------------------------------
        # 🔥 VALIDATION OFFICIELLE (unique, complète)
        # Un mot peut appartenir à plusieurs catégories (ex. zara → marque + prenom).
        # On croise TOUTES les catégories du mot avec celles de la manche,
        # au lieu de refuser dès la première catégorie hors jeu.
        # ----------------------------------------------------------------------
        matching_cats = [
            cat for cat, content in self.data["categories"].items()
            if mot in content.get("mots", [])
        ]
        active_matches = [c for c in matching_cats if c in game["categories"]]

        if matching_cats and not active_matches:
            self._word_ko(irc, channel, nick, mot, "bad_cat")
            irc.queueMsg(ircmsgs.privmsg(channel,
                f"{nick}: ❌ Le mot « {text} » n'est pas valide pour les catégories du tour."))
            return

        if active_matches:
            answers = game["answers"].get(nick_key, {})
            unfilled = [c for c in active_matches if c not in answers]
            preferred = self._norm_cat(preferred_cat) if preferred_cat else ""
            if preferred and preferred in game["categories"] and preferred not in active_matches:
                self._word_ko(irc, channel, nick, mot, "bad_cat", preferred)
                irc.queueMsg(ircmsgs.privmsg(channel,
                    f"{nick}: ❌ Le mot « {text} » n'est pas valide pour la catégorie {preferred}."))
                return
            if preferred and preferred in active_matches:
                if preferred in answers:
                    irc.queueMsg(ircmsgs.privmsg(channel,
                        f"{nick}: ⚠️ Tu as déjà validé la catégorie {preferred} dans cette manche."))
                    return
                cat = preferred
            else:
                if not unfilled:
                    irc.queueMsg(ircmsgs.privmsg(channel,
                        f"{nick}: ⚠️ Tu as déjà validé la catégorie {active_matches[0]} dans cette manche."))
                    return
                cat = unfilled[0]

            # Points
            if self._is_difficult_word(mot):
                points = 2
                msg_bonus = f"💎 Mot difficile « {text} » accepté! (+2 points)"
            else:
                points = 1
                msg_bonus = f"✔️ Mot « {text} » accepté (+1 point)"

            # Enregistrement
            self.scoreboard[nick_key] = self.scoreboard.get(nick_key, 0) + points
            game["answers"].setdefault(nick_key, {})[cat] = mot
            game["used_words"].add(mot)
            self.word_usage[mot] = self.word_usage.get(mot, 0) + 1

            irc.queueMsg(ircmsgs.privmsg(channel,
                f"{nick}: {msg_bonus} — Catégorie {cat}"))

            self._word_ok(irc, channel, nick, mot, cat, points)
            self._update_global_stats(nick_key, words_validated=1, total_points=points)
            self._check_full_combo(irc, channel, nick)
            return

        # ----------------------------------------------------------------------
        # MULTICAT
        # ----------------------------------------------------------------------
        categorie_trouvee = None

        if mot in self.data["multicat"]:
            possible = self.data["multicat"][mot]
            active = [c for c in possible if c in game["categories"]]

            if len(active) == 1:
                categorie_trouvee = active[0]

            elif len(active) > 1:
                summary = self.get_wikipedia_summary(mot)
                best = None
                best_score = -1

                for c in active:
                    score = self._guess_category(mot, summary, allowed_categories=[c])
                    try:
                        score = float(score)
                    except:
                        continue

                    if score > best_score:
                        best_score = score
                        best = c

                categorie_trouvee = best if best else sorted(active)[0]

        # ----------------------------------------------------------------------
        # IA
        # ----------------------------------------------------------------------
        if not categorie_trouvee:

            summary = mot if mot in self.dictionnaire else self.get_wikipedia_summary(mot)

            if summary is not None:
                guessed = self._guess_category(
                    mot,
                    summary if isinstance(summary, str) else None,
                    allowed_categories=game["categories"]
                )

                if guessed in game["categories"]:

                    already_pending = [
                        vid for vid, data in self.pending_verifications.items()
                        if data.get("mot") == mot and data.get("categorie") == guessed
                    ]

                    exists_in_base = any(
                        mot in self.data["categories"][c]["mots"]
                        for c in self.data["categories"]
                    )

                    if not already_pending and not exists_in_base:
                        new_id = str(max([int(i) for i in self.pending_verifications.keys()] + [0]) + 1)

                        self.pending_verifications[new_id] = {
                            "mot": mot,
                            "categorie": guessed,
                            "auteur": f"{nick} (IA)",
                            "timestamp": int(time.time())
                        }
                        self._save_pending_verifications()

                        self._notify_ops(irc, channel,
                            f"🔎 Nouveau mot IA en attente : « {mot} » (ID {new_id})")

                        dev_channel = self.registryValue('devChannel', channel)
                        if dev_channel and dev_channel.startswith("#"):
                            irc.queueMsg(ircmsgs.privmsg(
                                "BotServ",
                                f"say {dev_channel} [BAC][VERIFICATION IA] {nick} propose « {mot} » dans « {guessed} » (ID {new_id})"
                            ))

                    if guessed in game["answers"].get(nick_key, {}):
                        irc.queueMsg(ircmsgs.privmsg(channel,
                            f"{nick}: ⚠️ Tu as déjà validé la catégorie {guessed} dans cette manche."))
                        return

                    points = 0.5
                    self.scoreboard[nick_key] = self.scoreboard.get(nick_key, 0) + points

                    game["answers"].setdefault(nick_key, {})[guessed] = mot
                    game["used_words"].add(mot)
                    self.word_usage[mot] = self.word_usage.get(mot, 0) + 1

                    irc.queueMsg(ircmsgs.privmsg(channel,
                        f"{nick}: ✔️ Le mot « {mot} » été reconnu par l'IA 🤖 (+0.5 point) — Catégorie {guessed}"))

                    self._word_ok(irc, channel, nick, mot, guessed, 0.5)
                    self._check_full_combo(irc, channel, nick)
                    return

            irc.queueMsg(ircmsgs.privmsg(channel,
                f"{nick}: ❌ Le mot « {text} » n'est pas valide pour les catégories du tour."))
            irc.queueMsg(ircmsgs.privmsg(channel,
                f"ℹ️ Tu peux proposer ce mot en tapant : !verifier <catégorie> {text}"))
            self._word_ko(irc, channel, nick, mot, "invalid")
            self._send_event(
                irc, channel, "verify_hint",
                nick=nick, word=mot,
            )
            return

        # ----------------------------------------------------------------------
        # VALIDATION MULTICAT (unique)
        # ----------------------------------------------------------------------
        if categorie_trouvee not in game["categories"]:
            irc.queueMsg(ircmsgs.privmsg(channel,
                f"{nick}: ❌ Le mot « {text} » n'est pas valide pour les catégories du tour."))
            return

        if categorie_trouvee in game["answers"].get(nick_key, {}):
            irc.queueMsg(ircmsgs.privmsg(channel,
                f"{nick}: ⚠️ Tu as déjà validé la catégorie {categorie_trouvee} dans cette manche."))
            return

        points = 2 if self._is_difficult_word(mot) else 1
        msg_bonus = (
            f"💎 Mot difficile « {text} » accepté! (+2 points)"
            if points == 2 else
            f"✔️ Mot « {text} » accepté (+1 point)"
        )

        self.scoreboard[nick_key] = self.scoreboard.get(nick_key, 0) + points
        game["answers"].setdefault(nick_key, {})[categorie_trouvee] = mot
        game["used_words"].add(mot)
        self.word_usage[mot] = self.word_usage.get(mot, 0) + 1

        irc.queueMsg(ircmsgs.privmsg(channel,
            f"{nick}: {msg_bonus} — Catégorie {categorie_trouvee}"))

        self._word_ok(irc, channel, nick, mot, categorie_trouvee, points)
        self._update_global_stats(nick_key, words_validated=1, total_points=points)
        self._check_full_combo(irc, channel, nick)

    # -------------------------------------------------------------------------
    # !reboot pour redémarrer le bot lors de modification
    # -------------------------------------------------------------------------
    @wrap(['owner'])
    def reboot(self, irc, msg, args):
        """Commande de redémarrage du bot."""
        irc.reply("🔄 Redémarrage du bot…", notice=True)
        os.system("sudo systemctl restart Bac.service")

    #--------------------------------------------------------------------------
    # Commande !aide 
    #--------------------------------------------------------------------------
    @wrap([])
    def aide(self, irc, msg, args):
        """Affiche l'aide du jeu Petit Bac."""
        channel = msg.args[0]
        nick = msg.nick
        if not self._is_enabled(channel):
            return

        is_op = irc.state.channels[channel].isOp(nick)

        # Aide publique
        irc.queueMsg(ircmsgs.notice(nick, "📘 Aide du Petit Bac"))
        irc.queueMsg(ircmsgs.notice(nick, " "))

        irc.queueMsg(ircmsgs.notice(nick, "🎮 Commandes joueurs :"))
        irc.queueMsg(ircmsgs.notice(nick, "  • !jouer [noregles] — Démarre une nouvelle partie"))
        irc.queueMsg(ircmsgs.notice(nick, "  • !jeu <facile|moyen|difficile|<perso>|creer|del|liste> — Modifier le comportement du jeu"))
        irc.queueMsg(ircmsgs.notice(nick, "  • !oui / !non — Voter oui ( !oui ) ou non ( !non ) lors d'un vote"))
        irc.queueMsg(ircmsgs.notice(nick, "  • !scores — Affiche les scores cumulés"))
        irc.queueMsg(ircmsgs.notice(nick, "  • !stat [pseudo] — Affiche les statistiques globales, si pseudo celle du pseudo."))
        irc.queueMsg(ircmsgs.notice(nick, "  • !top [<nbr>] — Classement global"))
        irc.queueMsg(ircmsgs.notice(nick, "  • !manche — Affiche la lettre et les catgéories en jeu de la manche actuelle"))
        irc.queueMsg(ircmsgs.notice(nick, "  • !verifier <catégorie> <mot> — Propose un mot à valider"))
        irc.queueMsg(ircmsgs.notice(nick, "  • !info <mot> — Informations Wikipédia sur un mot"))
        irc.queueMsg(ircmsgs.notice(nick, "  • !bug <message> — Signaler un bug à l’équipe"))
        irc.queueMsg(ircmsgs.notice(nick, "  • !suggestion <message> — Proposer une amélioration du jeu"))
        irc.queueMsg(ircmsgs.notice(nick, " "))

        irc.queueMsg(ircmsgs.notice(nick, "ℹ️ Fonctionnement :"))
        irc.queueMsg(ircmsgs.notice(nick, "  • Une lettre et plusieurs catégories sont tirées au sort"))
        irc.queueMsg(ircmsgs.notice(nick, "  • Vous devez trouver un mot commençant par cette lettre"))
        irc.queueMsg(ircmsgs.notice(nick, "  • Le bot valide automatiquement les mots"))
        irc.queueMsg(ircmsgs.notice(nick, "  • Les points sont cumulés à chaque manche"))
        irc.queueMsg(ircmsgs.notice(nick, "  • Le classement est affiché à la fin de chaque manche"))
        irc.queueMsg(ircmsgs.notice(nick, " "))

        # Aide opérateur
        if is_op:
            irc.queueMsg(ircmsgs.notice(nick, "🔧 Commandes opérateur :"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !stop — Arrête la partie en cours"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !scores reset — Réinitialise les scores"))
            irc.queueMsg(ircmsgs.notice(nick, " "))

            irc.queueMsg(ircmsgs.notice(nick, "📚 Gestion des catégories et mots :"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac mots addcat <cat> — Crée une catégorie"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac mots delcat <cat> — Supprime une catégorie"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac mots <add|del> <cat> <mot> — Ajoute/supprime un mot"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac mots mod <nouvelle-cat> <mot> — Déplace un mot"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac mots list <cat> [lettres/préfix] — Liste les mots par lettre ou préfix"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac mots multicat <add|del|list> <categorie> <mot> — Gestion des mots multi-catégorie"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac mots multicat sync — Resynchronise les mots multi-catégorie"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac mots blacklist <add|del|list> <mot>— Gestion des mots interdits"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac mots whitelist <del|list> <mot>— Gestion des mots composés autorisés (ajouté automatiquement)"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac mots exclure <add|del|list> <catégorie> <mot> — Gestion des mots exclus par l'IA lors des propositions de mots"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac mots typos <add|del> <mot> [mot corrigé] — Gestion des mots mal orthographiés"))
            irc.queueMsg(ircmsgs.notice(nick, " "))

            irc.queueMsg(ircmsgs.notice(nick, "📝 Vérification des mots proposés :"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac verif list — Liste les mots en attente"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac verif ok <id|mot|all> — Valide un/des mot(s)"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac verif del <id|mot> — Supprime un mot en attente"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac verif mod <id|mot> <cat> — Change la catégorie d’un mot"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac verif exclure <catégorie> <id|mot> — Ajoute un mot dans les exclusions IA"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac verif typos <id|mot> <mot corrigé> — Ajoute un mot dans les typos IA"))
            irc.queueMsg(ircmsgs.notice(nick, " "))

            irc.queueMsg(ircmsgs.notice(nick, "🐞 Gestion des bugs :"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac bug voir all — Liste tous les bugs signalés"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac bug voir <id> — Affiche un bug précis"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac bug del <id> — Supprime un bug"))
            irc.queueMsg(ircmsgs.notice(nick, " "))

            irc.queueMsg(ircmsgs.notice(nick, "💡 Gestion des suggestions :"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac suggestion voir all — Liste toutes les suggestions"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac suggestion voir <id> — Affiche une suggestion"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac suggestion del <id> — Supprime une suggestion"))
            irc.queueMsg(ircmsgs.notice(nick, " "))

            irc.queueMsg(ircmsgs.notice(nick, "📊 Gestion des statistiques :"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac stats reset global — Réinitialise les stats globales"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac stats reset semaine — Réinitialise les stats hebdo"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac stats reset <pseudo> — Réinitialise les stats d’un joueur"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac stats delete <pseudo> — Supprime un joueur des stats"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac stats purge <nbr> — Supprime les stats des joueurs inactif depuis <nbr> jours"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac stats joueurs — Liste tous les joueurs enregistrés"))
            irc.queueMsg(ircmsgs.notice(nick, " "))

            irc.queueMsg(ircmsgs.notice(nick, "⚙️ Configuration du jeu :"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac config — Affiche la configuration"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac config set duration <sec> — Durée d'une manche"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac config set maxrounds <nb> — Nombre de manches"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac config set rotation <nb> — Rotation des catégories"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac config set maxidle <nb> — Manches sans réponse avant arrêt"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac config set categories <nb> — Catégories par manche"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac config set autostart <on/off> — Active ou Désactive le démarrage automatique"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac config set message <on/off> — Active/Désactive l’annonce"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac config set devchannel <salon> — Salon où l’équipe reçoit bugs & suggestions"))
            irc.queueMsg(ircmsgs.notice(nick, "  • !bac config set allowed <salon> — Salon où ce déroule le jeu"))
            irc.queueMsg(ircmsgs.notice(nick, " "))

        irc.queueMsg(ircmsgs.notice(nick, "📩 Cette aide vous a été envoyée en privé."))

    # ----------------------------------------------------------------------
    # Commandes Orbit via TAGMSG IRCv3 (pas de PRIVMSG !commande dans le tchat)
    # Client : @+pb=v1;+ev=cmd;+name=<cmd>;+arg=<...> TAGMSG #salon
    # ----------------------------------------------------------------------
    def _unescape_irc_tag(self, value):
        if not value:
            return ""
        s = str(value)
        out = []
        i = 0
        while i < len(s):
            if s[i] == "\\" and i + 1 < len(s):
                nxt = s[i + 1]
                mapping = {":": ";", "s": " ", "r": "\r", "n": "\n", "\\": "\\"}
                out.append(mapping.get(nxt, nxt))
                i += 2
            else:
                out.append(s[i])
                i += 1
        return "".join(out)

    def _msg_tag(self, msg, name):
        tags = getattr(msg, "server_tags", None) or getattr(msg, "tags", None) or {}
        if not isinstance(tags, dict):
            try:
                tags = dict(tags)
            except Exception:
                return ""
        return self._unescape_irc_tag(self._pb_tag(tags, name))

    def _orbit_cmd_once(self, nick, name, arg):
        if not hasattr(self, "_orbit_cmd_seen"):
            self._orbit_cmd_seen = {}
        key = (str(nick or "").lower(), str(name or ""), str(arg or ""))
        now = time.time()
        last = self._orbit_cmd_seen.get(key, 0)
        if now - last < 0.45:
            return False
        self._orbit_cmd_seen[key] = now
        if len(self._orbit_cmd_seen) > 80:
            cutoff = now - 10
            self._orbit_cmd_seen = {
                k: v for k, v in self._orbit_cmd_seen.items() if v >= cutoff
            }
        return True

    def _orbit_proxy(self, irc, msg, channel, tokens):
        text = "!" + " ".join(str(t) for t in tokens)
        fake = ircmsgs.IrcMsg(
            prefix=msg.prefix,
            command="PRIVMSG",
            args=(channel, text),
            server_tags=getattr(msg, "server_tags", None) or {},
        )
        try:
            self.Proxy(irc, fake, tokens)
        except Exception as e:
            log.warning("PetitBac: orbit cmd %s failed: %s", tokens, e)
            self._send_event(
                irc, channel, "cmd_err",
                name=tokens[0] if tokens else "",
                text=str(e)[:120],
            )

    def _orbit_reply_modes_list(self, irc, channel):
        active = self.current_mode.get(channel, "facile")
        locked_rows = []
        custom_rows = []
        for key, cfg in (self.modes or {}).items():
            row = "%s:%s:%s:%s:%s:%s" % (
                key,
                cfg.get("categories", 0),
                cfg.get("duration", 0),
                cfg.get("maxrounds", 0),
                "1" if cfg.get("locked") else "0",
                "1" if key == active else "0",
            )
            if cfg.get("locked"):
                locked_rows.append(row)
            else:
                custom_rows.append((cfg.get("times_used", 0), row))
        custom_rows.sort(key=lambda x: x[0], reverse=True)
        modes = ",".join(locked_rows + [r for _, r in custom_rows[:8]])
        self._send_event(irc, channel, "modes_list", modes=modes, active=active)

    def _orbit_reply_scores(self, irc, channel):
        joueurs = set()
        for game in (self.last_games or []):
            joueurs.update((game.get("scores") or {}).keys())
        summary = "%s partie(s), %s joueur(s)" % (
            len(self.last_games or []),
            len(joueurs),
        )
        ranking = ""
        if self.scoreboard:
            classement = sorted(
                self.scoreboard.items(), key=lambda x: x[1], reverse=True
            )
            ranking = self._compact_score_pairs(classement, limit=10)
        hist_parts = []
        for game in reversed((self.last_games or [])[-3:]):
            ts = str(game.get("timestamp") or "")[:16].replace(" ", "_")
            pairs = self._compact_score_pairs(
                sorted(
                    (game.get("scores") or {}).items(),
                    key=lambda x: x[1],
                    reverse=True,
                ),
                limit=4,
            )
            if ts:
                hist_parts.append("%s:%s" % (ts, pairs))
        history = "|".join(hist_parts)
        if self._send_event(
            irc, channel, "lobby_stats",
            summary=summary, ranking=ranking, history=history,
        ):
            return
        if self._send_event(
            irc, channel, "lobby_stats",
            summary=summary, ranking=ranking,
        ):
            return
        self._send_event(
            irc, channel, "lobby_stats",
            summary=summary,
            ranking=self._compact_score_pairs(
                sorted(
                    (self.scoreboard or {}).items(),
                    key=lambda x: x[1],
                    reverse=True,
                ),
                limit=5,
            ),
        )

    def _orbit_reply_stat(self, irc, channel, target=""):
        players = (self.global_stats or {}).get("players", {})
        if not target:
            gs = (self.global_stats or {}).get("global", {})
            self._send_event(
                irc, channel, "stat_result",
                kind="global",
                games=gs.get("games_played", 0),
                rounds=gs.get("rounds_played", 0),
                words=gs.get("words_validated", 0),
                combos=gs.get("full_combos", 0),
                pts=gs.get("total_points", 0),
                last=str(gs.get("last_activity") or "")[:32],
            )
            return
        key = target.strip().lower()
        if key not in players:
            self._send_event(
                irc, channel, "stat_result",
                kind="player", nick=target, ok="0",
            )
            return
        st = players[key]
        self._send_event(
            irc, channel, "stat_result",
            kind="player", nick=target, ok="1",
            games=st.get("games_played", 0),
            rounds=st.get("rounds_played", 0),
            words=st.get("words_validated", 0),
            combos=st.get("full_combos", 0),
            pts=st.get("total_points", 0),
            last=str(st.get("last_seen") or "")[:32],
        )

    def _orbit_reply_top(self, irc, channel, limit=5):
        try:
            limit = int(limit or 5)
        except (TypeError, ValueError):
            limit = 5
        limit = max(1, min(10, limit))
        players = (self.global_stats or {}).get("players", {})
        classement = sorted(
            players.items(),
            key=lambda x: x[1].get("total_points", 0),
            reverse=True,
        )
        parts = []
        for user, st in classement[:limit]:
            parts.append("%s:%s:%s" % (
                user,
                int(st.get("total_points", 0) or 0),
                int(st.get("full_combos", 0) or 0),
            ))
        self._send_event(
            irc, channel, "top_result",
            ranking=",".join(parts), limit=limit,
        )

    def _orbit_reply_info(self, irc, channel, word):
        raw = (word or "").strip()
        mot = self._normalize_word(raw) if hasattr(self, "_normalize_word") else raw.lower()
        if not mot:
            self._send_event(irc, channel, "info_result", word=raw, ok="0", text="invalide")
            return
        description = None
        try:
            description = self.get_wikipedia_summary(mot)
        except Exception:
            description = None
        if not description:
            self._send_event(irc, channel, "info_result", word=mot, ok="0", text="not_found")
            return
        clean = description.replace("\n", " ").replace("\xa0", " ").strip()
        if len(clean) > 300:
            clean = clean[:297] + "..."
        if not self._send_event(irc, channel, "info_result", word=mot, ok="1", text=clean):
            self._send_event(
                irc, channel, "info_result",
                word=mot, ok="1", text=clean[:180] + "...",
            )

    def _dispatch_orbit_cmd(self, irc, msg, channel, name, arg):
        name = (name or "").lower().strip()
        arg = (arg or "").strip()
        if not name:
            return
        if name in ("liste",):
            self._orbit_reply_modes_list(irc, channel)
            return
        if name == "jeu":
            arg_l = arg.lower().strip()
            if not arg_l or arg_l in ("liste", "aide", "help", "?"):
                self._orbit_reply_modes_list(irc, channel)
                return
            tokens = ["jeu"] + [p for p in re.split(r"\s+", arg) if p]
            self._orbit_proxy(irc, msg, channel, tokens)
            return
        if name == "scores":
            self._orbit_reply_scores(irc, channel)
            return
        if name in ("manche", "sync"):
            self._send_state_sync(irc, channel)
            if channel not in self.active_games:
                self._send_event(irc, channel, "cmd_err", name="manche", text="idle")
            return
        if name == "info":
            self._orbit_reply_info(irc, channel, arg)
            return
        if name == "stat":
            self._orbit_reply_stat(irc, channel, arg)
            return
        if name == "top":
            self._orbit_reply_top(irc, channel, arg or 5)
            return
        if name in ("jouer", "pause", "reprendre", "stop", "oui", "non", "verifier"):
            tokens = [name] + ([arg] if name == "verifier" and arg else (
                [p for p in re.split(r"\s+", arg) if p] if arg else []
            ))
            if name == "verifier" and arg:
                tokens = ["verifier"] + [p for p in re.split(r"\s+", arg) if p]
            self._orbit_proxy(irc, msg, channel, tokens)
            return
        self._send_event(irc, channel, "cmd_err", name=name, text="unknown")

    def _handle_orbit_tagmsg(self, irc, msg):
        if msg.command != "TAGMSG":
            return False
        if msg.nick == getattr(irc, "nick", None):
            return False
        if not msg.args:
            return False
        channel = self._game_channel(msg.args[0])
        if not channel or channel[0] not in "#&+!":
            return False
        if self._msg_tag(msg, "+pb") != "v1":
            return False
        if not self._is_enabled(channel):
            return False
        ev = self._msg_tag(msg, "+ev").lower()
        if ev == "play":
            word = self._msg_tag(msg, "+word")
            cat = self._msg_tag(msg, "+cat") or self._msg_tag(msg, "+category")
            if not word:
                return False
            if not self._orbit_cmd_once(msg.nick, "play", word + "|" + cat):
                return True
            try:
                self._handle_play_word(irc, msg, word, preferred_cat=cat)
            except Exception as e:
                self._log_error(irc, "TAGMSG play: %s" % e)
            return True
        if ev != "cmd":
            return False
        name = (self._msg_tag(msg, "+name") or self._msg_tag(msg, "+cmd")).lower()
        arg = self._msg_tag(msg, "+arg") or self._msg_tag(msg, "+text")
        if name in ("verifier", "verify") and not arg:
            cat = self._msg_tag(msg, "+cat") or self._msg_tag(msg, "+category")
            word = self._msg_tag(msg, "+word")
            if cat and word:
                arg = cat + " " + word
            name = "verifier"
        if name == "jeu" and not arg:
            mode = self._msg_tag(msg, "+mode")
            cats = self._msg_tag(msg, "+cats")
            dur = self._msg_tag(msg, "+dur") or self._msg_tag(msg, "+duration")
            rounds = self._msg_tag(msg, "+rounds")
            if cats or str(mode).lower() == "creer":
                arg = "creer %s %s %s" % (cats or "5", dur or "40", rounds or "12")
            elif mode:
                rest = self._msg_tag(msg, "+rest")
                arg = (mode + (" " + rest if rest else "")).strip()
        if name in ("jouer", "start"):
            name = "jouer"
            if not arg:
                a = (self._msg_tag(msg, "+regles") or "").lower()
                if a in ("1", "true", "regles", "règles"):
                    arg = "regles"
        if not name:
            return False
        if not self._orbit_cmd_once(msg.nick, name, arg):
            return True
        try:
            self._dispatch_orbit_cmd(irc, msg, channel, name, arg)
        except Exception as e:
            self._log_error(irc, "TAGMSG cmd %s: %s" % (name, e))
        return True

    def doTagmsg(self, irc, msg):
        self._handle_orbit_tagmsg(irc, msg)

    def doTAGMSG(self, irc, msg):
        self._handle_orbit_tagmsg(irc, msg)

    def inFilter(self, irc, msg):
        if msg.command == "TAGMSG":
            self._handle_orbit_tagmsg(irc, msg)
            return msg

        if msg.command != 'PRIVMSG':
            return msg

        channel, text = msg.args

        clean = self._clean_irc_formatting(text)

        if clean == text:
            return msg

        # 🔥 Recréer un message serveur propre
        new_msg = ircmsgs.IrcMsg(
            prefix=msg.prefix,
            command=msg.command,
            args=(channel, clean),
            server_tags=msg.server_tags
        )

        return new_msg
    
    def _clean_irc_formatting(self, text):
        # Supprimer tous les codes IRC (couleurs, styles, reset)
        return re.sub(
            r'(\x03(\d{1,2}(,\d{1,2})?)?)|[\x02\x1F\x16\x0F\x1D\x11\x12]',
            '',
            text
        )
        
    def _time_ago(self, ts):
        if not ts:
            return "jamais"

        now = int(time.time())
        diff = now - ts

        minutes = diff // 60
        hours   = diff // 3600
        days    = diff // 86400
        weeks   = diff // 604800
        months  = diff // 2592000
        years   = diff // 31536000

        if diff < 60:
            return "à l’instant"
        if minutes < 60:
            return f"il y a {minutes} minute{'s' if minutes>1 else ''}"
        if hours < 24:
            return f"il y a {hours} heure{'s' if hours>1 else ''}"
        if days < 7:
            return f"il y a {days} jour{'s' if days>1 else ''}"
        if weeks < 5:
            return f"il y a {weeks} semaine{'s' if weeks>1 else ''}"
        if months < 12:
            return f"il y a {months} mois"
        return f"il y a {years} an{'s' if years>1 else ''}"
            
    # ----------------------------------------------------------------------
    # Le bot rejoints le salon des logs à la connexion
    # ----------------------------------------------------------------------
    def do001(self, irc, msg):
        # Rejoindre le salon d’erreurs
        error_channel = conf.supybot.plugins.PetitBac.errorChannel()
        if error_channel and error_channel.startswith("#"):
            irc.queueMsg(ircmsgs.join(error_channel))
            
    def doNotice(self, irc, msg):
        self.log.info("NOTICE reçu: %r", msg.args)
        self.log.info("TAGS IRCv3: %r", msg.server_tags)
            
    # --------------------------------------------------------------
    # Traitement des erreurs du plugin
    # --------------------------------------------------------------
    def _log_error(self, irc, message):
        error_channel = conf.supybot.plugins.PetitBac.errorChannel()

        tb = traceback.format_exc()
        log.error(f"[PetitBac ERROR] {message}\n{tb}")

        # Toujours prévenir l’utilisateur si possible
        fallback = "#_dev"  # ← mets ton salon ici si tu veux un fallback

        target = error_channel if (error_channel and error_channel.startswith("#")) else fallback

        # Envoi IRC
        irc.queueMsg(ircmsgs.privmsg(target, f"[PetitBac][ERREUR] {message}"))

        for line in tb.splitlines():
            irc.queueMsg(ircmsgs.privmsg(target, f"[TB] {line}"))

    def callCommand(self, command, irc, msg, *args, **kwargs):
        try:
            return super().callCommand(command, irc, msg, *args, **kwargs)

        except callbacks.Error:
            raise  # erreurs normales → laisser Supybot gérer

        except Exception as e:
            cmd = command[0] if isinstance(command, (list, tuple)) else str(command)
            self._log_error(irc, f"Erreur dans la commande {cmd}: {repr(e)}")
            irc.queueMsg(ircmsgs.notice(msg.nick,
                "❌ Une erreur interne est survenue."))

    def _safe(self, irc, func):
        try:
            func()
        except Exception as e:
            self._log_error(irc, f"Erreur dans un timer: {e}")
