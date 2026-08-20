from supybot import conf, registry

def configure(advanced):
    from supybot.questions import expect, anything, something, yn
    conf.registerPlugin('PetitBac', True)

PetitBac = conf.registerPlugin('PetitBac')

# ----------------------------------------------------------------------
# Catégories utilisées pour le jeu
# ----------------------------------------------------------------------
conf.registerGlobalValue(
    PetitBac, 'categories',
    registry.SpaceSeparatedListOfStrings(
        ['Prénom', 'Ville', 'Animal', 'Objet', 'Métier', 'Fruit', 'Pays', 'Adjectif'],
        """Liste des catégories utilisées pour le jeu du Petit Bac."""
    )
)

# ----------------------------------------------------------------------
# Durée d'un round (en secondes)
# ----------------------------------------------------------------------
conf.registerGlobalValue(
    PetitBac, 'roundDuration',
    registry.PositiveInteger(
        60,
        """Durée d'un round en secondes avant changement automatique de lettre."""
    )
)

# ----------------------------------------------------------------------
# Nombre de manches consécutives sans réponse avant arrêt automatique
# ----------------------------------------------------------------------
conf.registerGlobalValue(
    PetitBac, 'maxIdleRounds',
    registry.PositiveInteger(
        5,
        """Nombre de manches consécutives sans réponse avant arrêt automatique du jeu."""
    )
)

# ----------------------------------------------------------------------
# Auto-start : démarrer automatiquement une partie quand quelqu'un rejoint
# ----------------------------------------------------------------------
conf.registerGlobalValue(
    PetitBac, 'autoStart',
    registry.Boolean(
        False,
        """Démarre automatiquement une partie quand quelqu'un rejoint le salon."""
    )
)

# ----------------------------------------------------------------------
# Nombre de catégories joué dans les tours
# ----------------------------------------------------------------------
conf.registerGlobalValue(PetitBac, 'categoryCount',
    registry.Integer(3, """Nombre de catégories utilisées par manche."""))

# ----------------------------------------------------------------------
# Changement des catégories suivant le nombre de tours
# ----------------------------------------------------------------------
conf.registerGlobalValue(PetitBac, 'categoryRotation',
    registry.Integer(3, """Nombre de tours avant changement automatique des catégories."""))

# ----------------------------------------------------------------------
# Défini un salon sur lesquel le jeu fonctionne
# ----------------------------------------------------------------------   
conf.registerGlobalValue(
    PetitBac,
    'allowedChannel',
    registry.String('#Baccalaureat.chat', """Salon autorisé pour le jeu PetitBac.""")
)

# ----------------------------------------------------------------------
# Défini le nombre de manche d'une partie ( 10 manches = 1 partie )
# ----------------------------------------------------------------------

conf.registerGlobalValue(
    PetitBac,
    'maxRounds',
    registry.Integer(10, """Nombre de manches par partie avant la fin automatique.""")
)

# ----------------------------------------------------------------------
# Défini si un message s'affiche #EntreNous.chat lorsqu'une partie commence
# ----------------------------------------------------------------------
conf.registerGlobalValue(conf.supybot.plugins.PetitBac, 'announceMessage',
    registry.Boolean(False, """Active ou désactive l'annonce automatique sur <#announcechannel> lors du lancement d'une partie."""))

# ----------------------------------------------------------------------
# Défini le salon ou l'annonce sera délivré 
# ----------------------------------------------------------------------
conf.registerGlobalValue(
    conf.supybot.plugins.PetitBac,
    'announceChannel',
    registry.String("#EntreNous.chat", """Salon où envoyer le message de début de partie. Laisser vide pour utiliser le salon actuel.""")
)

# ----------------------------------------------------------------------
# Url de wikipedia pour la commande !info
# ----------------------------------------------------------------------
conf.registerGlobalValue(
    PetitBac,
    'wikipediaApiUrl',
    registry.String(
        'https://fr.wikipedia.org/api/rest_v1/page/summary/{mot}',
        """URL de l’API Wikipédia utilisée pour la commande !info."""
    )
)

# ----------------------------------------------------------------------
# Salon de l'équipe de dév du bot
# ----------------------------------------------------------------------
conf.registerChannelValue(
    PetitBac,
    'devChannel',
    registry.String('#_bo', """Salon où seront envoyés les messages de bug/suggestion via BotServ.""")
)

# ----------------------------------------------------------------------
# Salon de traitement des erreurs du bot
# ----------------------------------------------------------------------
conf.registerChannelValue(PetitBac, 'errorChannel',
    registry.String("_dev", """Salon où seront envoyées les erreurs du plugin."""))

# ----------------------------------------------------------------------
# Configuration API Petit Bac
# ----------------------------------------------------------------------
conf.registerGlobalValue(conf.supybot.plugins.PetitBac, 'apiEnabled',
    registry.Boolean(False, """Active ou désactive le serveur API PetitBac."""))

conf.registerGlobalValue(conf.supybot.plugins.PetitBac, 'apiAutostart',
    registry.Boolean(False, """Démarre automatiquement l’API au lancement du bot."""))

conf.registerGlobalValue(conf.supybot.plugins.PetitBac, 'apiAddress',
    registry.String('', """Adresse IP sur laquelle l’API écoute (ex: 127.0.0.1)."""))

conf.registerGlobalValue(conf.supybot.plugins.PetitBac, 'apiPort',
    registry.Integer(0, """Port sur lequel l’API écoute (ex: 8282)."""))

# ----------------------------------------------------------------------
# Salon plus silencieux (TAGMSG Orbit, moins de PRIVMSG redondants)
# ----------------------------------------------------------------------
conf.registerChannelValue(
    PetitBac,
    'quietChannel',
    registry.Boolean(
        True,
        """Réduit les PRIVMSG redondants (séparateurs, rappels).
        Les décomptes de manche (20/10/5 s), les TAGMSG Orbit et les
        messages essentiels (lettre, mots, scores) restent."""
    )
)




