# PetitBac

Plugin [Limnoria](https://limnoria.net/) pour jouer au **Petit Bac** sur IRC.

Tu n’as **pas** à installer chaque fichier un par un. Limnoria charge le **dossier** `PetitBac/` comme un seul plugin.

## Structure (ce que Limnoria attend)

À la racine, seulement les fichiers standards d’un plugin :

| Fichier / dossier | Rôle |
|---|---|
| `__init__.py` | Point d’entrée (Limnoria charge ça) |
| `config.py` | Options (`config plugins.PetitBac ...`) |
| `plugin.py` | Classe principale du jeu |
| `test.py` | Tests |
| `local/` | Code interne (mixins). Tu n’as rien à y toucher pour installer. |
| `data/` | Dictionnaire, catégories, scores, stats |
| `docs/` | Aide / notes |

## Installation

### 1. Placer le plugin dans le dossier plugins du bot

Le bot Limnoria a un répertoire `plugins/` (souvent à côté de ton fichier `.conf`).

**Option A — clone Git (recommandé, pour `git pull` ensuite) :**

```bash
cd /chemin/vers/ton/bot/plugins
git clone https://github.com/Zell40/Bac.git PetitBac
```

Le dossier **doit** s’appeler `PetitBac` (c’est le nom du plugin).

**Option B — copie manuelle :**

Copie tout le dossier `PetitBac/` dans `plugins/` du bot. Résultat attendu :

```
bot/
  plugins/
    PetitBac/
      __init__.py
      config.py
      plugin.py
      local/
      data/
```

### 2. Dire à Limnoria où chercher les plugins

Dans la config du bot (`supybot.directories.plugins`), le dossier parent doit être listé. Exemple :

```
config supybot.directories.plugins /chemin/vers/ton/bot/plugins
```

### 3. Charger le plugin

Dans IRC, en tant que propriétaire du bot :

```
load PetitBac
```

Pour recharger après une mise à jour :

```
reload PetitBac
```

### 4. Mise à jour depuis GitHub

```bash
cd /chemin/vers/ton/bot/plugins/PetitBac
git pull origin main
```

Puis dans IRC : `reload PetitBac`.

## Première config utile

```
config plugins.PetitBac.allowedChannel #ton-salon
config plugins.PetitBac.roundDuration 60
```

Le jeu ne démarre que sur le salon défini dans `allowedChannel`.
