import os
import json
import unicodedata

_PLUGIN_DIR = os.path.dirname(os.path.dirname(__file__))
BASE = os.path.join(_PLUGIN_DIR, "data", "categories")
OUTPUT = os.path.join(_PLUGIN_DIR, "data", "categories.json")

# Si tu veux pré-remplir une blacklist :
DEFAULT_BLACKLIST = [
    # "insulte1",
    # "insulte2",
]

def norm_cat(cat):
    """Normalise un nom de catégorie : minuscules + accents supprimés + trim."""
    cat = cat.lower().strip()
    cat = unicodedata.normalize("NFD", cat)
    return "".join(c for c in cat if unicodedata.category(c) != "Mn")


def migrate():
    print("🔄 Migration PetitBac → categories.json")

    categories = {}     # cat → set(mots)
    word_to_cats = {}   # mot → [cats]

    # ------------------------------------------------------------
    # 1) Lire tous les fichiers .txt
    # ------------------------------------------------------------
    for filename in os.listdir(BASE):
        if not filename.endswith(".txt"):
            continue

        raw_cat = filename[:-4]
        cat = norm_cat(raw_cat)

        path = os.path.join(BASE, filename)

        try:
            with open(path, "r", encoding="utf-8") as f:
                words = {w.strip().lower() for w in f if w.strip()}
        except Exception as e:
            print(f"⚠️ Erreur lecture {filename}: {e}")
            continue

        if cat not in categories:
            categories[cat] = set()

        categories[cat].update(words)

        # Index inverse : mot → catégories
        for w in words:
            word_to_cats.setdefault(w, []).append(cat)

        print(f"📁 Catégorie détectée : {raw_cat} → {cat} ({len(words)} mots)")

    # ------------------------------------------------------------
    # 2) Détection des mots multi-catégories
    # ------------------------------------------------------------
    multicat = {
        mot: cats
        for mot, cats in word_to_cats.items()
        if len(cats) > 1
    }

    if multicat:
        print(f"🔍 Mots multi-catégories détectés : {len(multicat)}")
    else:
        print("✔ Aucun mot multi-catégorie détecté.")

    # ------------------------------------------------------------
    # 3) Construction du JSON final
    # ------------------------------------------------------------
    final = {
        "categories": {},
        "blacklist": DEFAULT_BLACKLIST,
        "multicat": multicat
    }

    for cat, mots in categories.items():
        final["categories"][cat] = {
            "mots": sorted(list(mots)),
            "keywords": ["mot", "terme", "element", "concept"]
        }

    # ------------------------------------------------------------
    # 4) Sauvegarde atomique
    # ------------------------------------------------------------
    tmp = OUTPUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(final, f, indent=4, ensure_ascii=False)
    os.replace(tmp, OUTPUT)

    print("\n✅ Migration terminée !")
    print(f"📦 Fichier généré : {OUTPUT}")
    print("⚠️ Aucun fichier .txt n’a été modifié ou supprimé.")
    print("👉 Vérifie le JSON avant de supprimer les anciens fichiers.")


if __name__ == "__main__":
    migrate()
