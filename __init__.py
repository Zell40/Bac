"""
Plugin Limnoria : jeu du Petit Bac.
"""

import supybot
import supybot.world as world
from importlib import reload

__version__ = "1.1.0"
__author__ = supybot.Author("Zell", "Zell", "zell@entrenous.chat")
__contributors__ = {}
__url__ = "https://github.com/Zell40/Bac"

from . import config
from . import plugin
from .local import (
    api,
    events,
    feedback,
    game,
    messages,
    persistence,
    stats,
    verif,
    vote,
    words,
)

# Rechargement des modules internes lors d'un `reload PetitBac`
reload(messages)
reload(events)
reload(game)
reload(words)
reload(vote)
reload(stats)
reload(verif)
reload(feedback)
reload(api)
reload(persistence)
reload(plugin)

if world.testing:
    from . import test

Class = plugin.Class
configure = config.configure
