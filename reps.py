#!/usr/bin/env python3
"""Reps : suivi de séances de sport. Un seul fichier, zéro dépendance, 100 % local.

  python reps.py            -> application (fenêtre dédiée)
  python reps.py web        -> même interface dans le navigateur (--port 8001, --no-browser)
  python reps.py log "Développé couché" 8x60 8x60 6x62,5   -> noter une séance en 10 secondes
  python reps.py --help     -> toutes les commandes

Données : un fichier SQLite (~/.reps.db) en mode WAL, index sur les séries, records et statistiques
calculés en une passe, serveur local réservé à 127.0.0.1.
"""
import argparse
import base64
import csv
import io
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.request
import webbrowser
from collections import defaultdict
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DB_PATH = Path.home() / ".reps.db"

GROUPES = {"pecs": "Pectoraux", "dos": "Dos", "jambes": "Jambes", "epaules": "Épaules",
           "bras": "Bras", "tronc": "Tronc", "cardio": "Cardio", "autre": "Autre"}
TYPES = ("charge", "corps", "duree", "cardio")
EQUIPEMENTS = ("barre", "haltere", "machine", "poulie", "corps", "autre")
REGLAGES = {"theme": "auto", "repos": "90", "barre": "20", "objectif": "3"}

# Une série compte quand elle contient au moins une donnée utile.
VALIDE = "(r.reps IS NOT NULL OR r.duree_s IS NOT NULL OR r.distance_m IS NOT NULL)"

STATE = {"v": 0}  # compteur de modifications : invalide le cache des records

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS exercice(
  id INTEGER PRIMARY KEY,
  nom TEXT NOT NULL UNIQUE COLLATE NOCASE,
  groupe TEXT NOT NULL DEFAULT 'autre',
  type TEXT NOT NULL DEFAULT 'charge',
  equipement TEXT NOT NULL DEFAULT 'autre',
  note TEXT,
  archive INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS seance(
  id INTEGER PRIMARY KEY,
  nom TEXT NOT NULL,
  jour TEXT NOT NULL,
  duree_min INTEGER,
  note TEXT,
  en_cours INTEGER NOT NULL DEFAULT 0,
  debut_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_seance_jour ON seance(jour, id);
CREATE TABLE IF NOT EXISTS serie(
  id INTEGER PRIMARY KEY,
  seance_id INTEGER NOT NULL REFERENCES seance(id) ON DELETE CASCADE,
  exercice_id INTEGER NOT NULL REFERENCES exercice(id),
  ordre INTEGER NOT NULL,
  reps INTEGER, poids REAL, duree_s INTEGER, distance_m REAL,
  echauffement INTEGER NOT NULL DEFAULT 0,
  fait INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_serie_seance ON serie(seance_id, ordre);
CREATE INDEX IF NOT EXISTS idx_serie_exo ON serie(exercice_id);
CREATE TABLE IF NOT EXISTS modele(
  id INTEGER PRIMARY KEY,
  nom TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS modele_exo(
  id INTEGER PRIMARY KEY,
  modele_id INTEGER NOT NULL REFERENCES modele(id) ON DELETE CASCADE,
  exercice_id INTEGER NOT NULL REFERENCES exercice(id),
  ordre INTEGER NOT NULL,
  series INTEGER NOT NULL DEFAULT 3,
  reps INTEGER, poids REAL, duree_s INTEGER, distance_m REAL
);
CREATE TABLE IF NOT EXISTS mesure(
  id INTEGER PRIMARY KEY,
  jour TEXT NOT NULL UNIQUE,
  poids REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reglage(cle TEXT PRIMARY KEY, valeur TEXT);
"""

EXERCICES_DE_BASE = [
    ("Développé couché", "pecs", "charge", "barre"), ("Développé incliné haltères", "pecs", "charge", "haltere"),
    ("Écartés à la poulie", "pecs", "charge", "poulie"), ("Pompes", "pecs", "corps", "corps"),
    ("Dips", "pecs", "corps", "corps"),
    ("Tractions", "dos", "corps", "corps"), ("Rowing barre", "dos", "charge", "barre"),
    ("Tirage vertical", "dos", "charge", "poulie"), ("Rowing haltère", "dos", "charge", "haltere"),
    ("Soulevé de terre", "dos", "charge", "barre"),
    ("Squat", "jambes", "charge", "barre"), ("Presse à cuisses", "jambes", "charge", "machine"),
    ("Fentes marchées", "jambes", "charge", "haltere"), ("Soulevé de terre roumain", "jambes", "charge", "barre"),
    ("Hip thrust", "jambes", "charge", "barre"), ("Mollets debout", "jambes", "charge", "machine"),
    ("Développé militaire", "epaules", "charge", "barre"), ("Élévations latérales", "epaules", "charge", "haltere"),
    ("Oiseau", "epaules", "charge", "haltere"),
    ("Curl biceps", "bras", "charge", "haltere"), ("Curl barre", "bras", "charge", "barre"),
    ("Extension triceps à la poulie", "bras", "charge", "poulie"), ("Barre au front", "bras", "charge", "barre"),
    ("Gainage", "tronc", "duree", "corps"), ("Crunch", "tronc", "corps", "corps"),
    ("Relevé de jambes", "tronc", "corps", "corps"),
    ("Course à pied", "cardio", "cardio", "autre"), ("Vélo", "cardio", "cardio", "machine"),
    ("Rameur", "cardio", "cardio", "machine"),
]


class Base(sqlite3.Connection):
    pass


def sans_accents(s):
    s = unicodedata.normalize("NFD", str(s or ""))
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower()


def connect():
    db = sqlite3.connect(DB_PATH, factory=Base, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.create_function("sa", 1, sans_accents, deterministic=True)
    db.executescript(SCHEMA)
    if not db.execute("SELECT 1 FROM reglage WHERE cle='semis'").fetchone():
        with db:
            for nom, groupe, typ, equip in EXERCICES_DE_BASE:
                db.execute("INSERT OR IGNORE INTO exercice(nom,groupe,type,equipement) VALUES(?,?,?,?)",
                           (nom, groupe, typ, equip))
            db.execute("INSERT INTO reglage(cle,valeur) VALUES('semis','1')")
    return db


def reglage(db, cle, defaut=None):
    r = db.execute("SELECT valeur FROM reglage WHERE cle=?", (cle,)).fetchone()
    return r["valeur"] if r else (defaut if defaut is not None else REGLAGES.get(cle))


def lundi(d):
    return d - timedelta(days=d.weekday())


def fmt_nombre(x):
    if x is None:
        return ""
    return (f"{x:.2f}".rstrip("0").rstrip(".")).replace(".", ",")


# ---------------------------------------------------------------- analyse
def e1rm(poids, reps):
    """Charge maximale estimée (formule d'Epley), fiable jusqu'à 12 répétitions."""
    if poids and reps and 1 <= reps <= 12:
        return round(poids * (1 + reps / 30), 1)
    return None


def mesures_serie(typ, r):
    """Valeurs comparables d'une série de travail, selon le type d'exercice."""
    reps, poids, duree, dist = r["reps"], r["poids"], r["duree_s"], r["distance_m"]
    v = {}
    if typ == "charge":
        if reps and poids:
            v["poids"], v["serie"] = poids, reps * poids
            e = e1rm(poids, reps)
            if e:
                v["e1rm"] = e
    elif typ == "corps":
        if reps:
            v["reps"] = reps
        if poids:
            v["lest"] = poids
    elif typ == "duree":
        if duree:
            v["duree"] = duree
    elif typ == "cardio":
        if dist:
            v["distance"] = dist
        if dist and dist >= 1000 and duree:
            v["allure"] = duree / (dist / 1000)  # secondes par km : plus petit = mieux
    return v


MOINS_EST_MIEUX = {"allure"}


def analyser(db):
    """Parcourt l'historique une seule fois : records par exercice, et séries qui battent le meilleur des séances précédentes."""
    rows = db.execute(
        "SELECT r.id, r.seance_id, r.exercice_id, r.reps, r.poids, r.duree_s, r.distance_m, s.jour, e.type "
        "FROM serie r JOIN seance s ON s.id=r.seance_id JOIN exercice e ON e.id=r.exercice_id "
        "WHERE r.fait=1 AND r.echauffement=0 ORDER BY s.jour, s.id, r.ordre, r.id").fetchall()
    meilleur_seance = {}  # (séance, exercice, mesure) -> meilleure valeur de cette séance
    for r in rows:
        for kind, val in mesures_serie(r["type"], r).items():
            cle = (r["seance_id"], r["exercice_id"], kind)
            cur = meilleur_seance.get(cle)
            if cur is None or (val < cur[0] if kind in MOINS_EST_MIEUX else val > cur[0]):
                meilleur_seance[cle] = (val, r["id"], r["jour"])
    maxi, drapeaux = {}, {}
    for (sid, exo, kind), (val, serie_id, jour) in meilleur_seance.items():  # ordre chronologique des séances
        ancien = maxi.get((exo, kind))
        if ancien is None or (val < ancien["valeur"] if kind in MOINS_EST_MIEUX else val > ancien["valeur"]):
            if ancien is not None:  # un historique existait : c'est un vrai record
                drapeaux[(sid, exo, kind)] = (serie_id, val, jour)
            maxi[(exo, kind)] = {"valeur": val, "serie_id": serie_id, "seance_id": sid, "jour": jour}
    prs_serie, prs_seance, evenements = defaultdict(list), defaultdict(set), []
    for (sid, exo, kind), (serie_id, val, jour) in drapeaux.items():
        prs_serie[serie_id].append(kind)
        prs_seance[sid].add(serie_id)
        evenements.append({"jour": jour, "seance_id": sid, "exercice_id": exo, "kind": kind,
                           "valeur": val, "serie_id": serie_id})
    evenements.sort(key=lambda e: (e["jour"], e["serie_id"]), reverse=True)
    records = defaultdict(dict)
    for (exo, kind), m in maxi.items():
        records[exo][kind] = m
    return {"prs_serie": dict(prs_serie), "prs_seance": {k: len(v) for k, v in prs_seance.items()},
            "records": dict(records), "evenements": evenements}


_CACHE = {"v": -1, "an": None}


def analyse_cache(db):
    if _CACHE["v"] != STATE["v"] or _CACHE["an"] is None:
        _CACHE["an"], _CACHE["v"] = analyser(db), STATE["v"]
    return _CACHE["an"]


# ---------------------------------------------------------------- lecture
def precedent(db, exo_id, jour, sid):
    """Dernière séance (avant celle-ci) où l'exercice a été fait, avec ses séries."""
    s = db.execute(
        "SELECT s.id, s.jour FROM seance s JOIN serie r ON r.seance_id=s.id "
        "WHERE r.exercice_id=? AND r.fait=1 AND " + VALIDE + " AND (s.jour<? OR (s.jour=? AND s.id<?)) "
        "ORDER BY s.jour DESC, s.id DESC LIMIT 1", (exo_id, jour, jour, sid)).fetchone()
    if not s:
        return None
    series = db.execute(
        "SELECT reps, poids, duree_s, distance_m, echauffement FROM serie r "
        "WHERE r.seance_id=? AND r.exercice_id=? AND r.fait=1 AND " + VALIDE + " ORDER BY r.ordre, r.id",
        (s["id"], exo_id)).fetchall()
    return {"jour": s["jour"], "seance_id": s["id"], "series": [dict(x) for x in series]}


def resume_series(rows):
    valides = [r for r in rows if r["fait"] and not r["echauffement"]
               and (r["reps"] is not None or r["duree_s"] is not None or r["distance_m"] is not None)]
    return {"nb_series": len(valides),
            "nb_exos": len({r["exercice_id"] for r in valides}),
            "volume": round(sum(r["reps"] * r["poids"] for r in valides if r["reps"] and r["poids"]), 1)}


def detail_seance(db, sid):
    s = db.execute("SELECT * FROM seance WHERE id=?", (sid,)).fetchone()
    if not s:
        raise Erreur(404, "Séance introuvable.")
    an = analyse_cache(db)
    rows = db.execute(
        "SELECT r.*, e.nom, e.groupe, e.type, e.equipement FROM serie r JOIN exercice e ON e.id=r.exercice_id "
        "WHERE r.seance_id=? ORDER BY r.ordre, r.id", (sid,)).fetchall()
    blocs = {}
    for r in rows:
        b = blocs.get(r["exercice_id"])
        if not b:
            b = blocs[r["exercice_id"]] = {
                "exercice": {"id": r["exercice_id"], "nom": r["nom"], "groupe": r["groupe"],
                             "type": r["type"], "equipement": r["equipement"]}, "series": []}
        b["series"].append({"id": r["id"], "reps": r["reps"], "poids": r["poids"], "duree_s": r["duree_s"],
                            "distance_m": r["distance_m"], "echauffement": r["echauffement"], "fait": r["fait"],
                            "pr": an["prs_serie"].get(r["id"], [])})
    for b in blocs.values():
        b["precedent"] = precedent(db, b["exercice"]["id"], s["jour"], sid)
    res = resume_series(rows)
    res["prs"] = an["prs_seance"].get(sid, 0)
    return {"seance": dict(s), "exercices": list(blocs.values()), "resume": res}


def liste_seances(db, q="", limite=60, decalage=0):
    an = analyse_cache(db)
    q = sans_accents(q).strip()
    where, args = "", []
    if q:
        where = ("WHERE sa(s.nom) LIKE ? OR EXISTS(SELECT 1 FROM serie r2 JOIN exercice e2 ON e2.id=r2.exercice_id "
                 "WHERE r2.seance_id=s.id AND sa(e2.nom) LIKE ?)")
        args = [f"%{q}%", f"%{q}%"]
    total = db.execute(f"SELECT COUNT(*) FROM seance s {where}", args).fetchone()[0]
    rows = db.execute(
        "SELECT s.id, s.nom, s.jour, s.duree_min, s.en_cours, s.debut_ts, "
        "COUNT(DISTINCT CASE WHEN r.fait=1 AND " + VALIDE + " THEN r.exercice_id END) nb_exos, "
        "COALESCE(SUM(CASE WHEN r.fait=1 AND r.echauffement=0 AND " + VALIDE + " THEN 1 END),0) nb_series, "
        "COALESCE(SUM(CASE WHEN r.fait=1 AND r.echauffement=0 AND r.reps IS NOT NULL AND r.poids IS NOT NULL "
        "THEN r.reps*r.poids END),0) volume "
        f"FROM seance s LEFT JOIN serie r ON r.seance_id=s.id {where} GROUP BY s.id "
        "ORDER BY s.en_cours DESC, s.jour DESC, s.id DESC LIMIT ? OFFSET ?", [*args, limite, decalage]).fetchall()
    ids = [r["id"] for r in rows]
    exos = defaultdict(list)
    if ids:
        marques = ",".join("?" * len(ids))
        for x in db.execute(
                "SELECT DISTINCT r.seance_id, e.nom, e.groupe, MIN(r.ordre) o FROM serie r "
                "JOIN exercice e ON e.id=r.exercice_id "
                f"WHERE r.seance_id IN ({marques}) AND r.fait=1 AND " + VALIDE + " "
                "GROUP BY r.seance_id, e.id ORDER BY o", ids):
            exos[x["seance_id"]].append({"nom": x["nom"], "groupe": x["groupe"]})
    out = []
    for r in rows:
        d = dict(r)
        d["volume"] = round(d["volume"], 1)
        d["exercices"] = exos.get(r["id"], [])
        d["prs"] = an["prs_seance"].get(r["id"], 0)
        out.append(d)
    return {"seances": out, "total": total}


def liste_exercices(db, archives=False):
    an = analyse_cache(db)
    rows = db.execute(
        "SELECT e.*, COUNT(CASE WHEN r.fait=1 AND " + VALIDE + " THEN 1 END) nb_series, "
        "MAX(CASE WHEN r.fait=1 THEN s.jour END) dernier_jour "
        "FROM exercice e LEFT JOIN serie r ON r.exercice_id=e.id LEFT JOIN seance s ON s.id=r.seance_id "
        + ("" if archives else "WHERE e.archive=0 ") + "GROUP BY e.id ORDER BY e.nom COLLATE NOCASE").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["records"] = {k: {"valeur": v["valeur"], "jour": v["jour"]} for k, v in an["records"].get(r["id"], {}).items()}
        out.append(d)
    return out


def progres_exercice(db, eid):
    e = db.execute("SELECT * FROM exercice WHERE id=?", (eid,)).fetchone()
    if not e:
        raise Erreur(404, "Exercice introuvable.")
    an = analyse_cache(db)
    rows = db.execute(
        "SELECT r.*, s.jour FROM serie r JOIN seance s ON s.id=r.seance_id "
        "WHERE r.exercice_id=? AND r.fait=1 AND r.echauffement=0 AND " + VALIDE + " ORDER BY s.jour, s.id, r.ordre",
        (eid,)).fetchall()
    points, cle = [], None
    for r in rows:
        if cle != r["seance_id"]:
            cle = r["seance_id"]
            points.append({"jour": r["jour"], "seance_id": cle, "series": 0, "volume": 0.0, "poids_max": None,
                           "e1rm": None, "reps_max": None, "reps_total": 0, "duree_max": None,
                           "distance_max": None, "allure_min": None})
        p = points[-1]
        p["series"] += 1
        p["reps_total"] += r["reps"] or 0
        if r["reps"] and r["poids"]:
            p["volume"] = round(p["volume"] + r["reps"] * r["poids"], 1)
        for kind, val in mesures_serie(e["type"], r).items():
            champ = {"poids": "poids_max", "lest": "poids_max", "reps": "reps_max", "e1rm": "e1rm", "duree": "duree_max",
                     "distance": "distance_max", "allure": "allure_min"}.get(kind)
            if champ:
                cur = p[champ]
                if cur is None or (val < cur if kind in MOINS_EST_MIEUX else val > cur):
                    p[champ] = val
    return {"exercice": dict(e), "points": points,
            "records": {k: {"valeur": v["valeur"], "jour": v["jour"]} for k, v in an["records"].get(eid, {}).items()}}


def calcul_stats(db):
    an = analyse_cache(db)
    aujourdhui = date.today()
    obj = max(1, int(reglage(db, "objectif") or 3))
    depuis52 = (lundi(aujourdhui) - timedelta(weeks=51)).isoformat()
    sessions = db.execute(
        "SELECT s.id, s.jour, s.duree_min, "
        "COALESCE(SUM(CASE WHEN r.fait=1 AND r.echauffement=0 AND " + VALIDE + " THEN 1 END),0) series, "
        "COALESCE(SUM(CASE WHEN r.fait=1 AND r.echauffement=0 AND r.reps IS NOT NULL AND r.poids IS NOT NULL "
        "THEN r.reps*r.poids END),0) volume "
        "FROM seance s LEFT JOIN serie r ON r.seance_id=s.id WHERE s.en_cours=0 GROUP BY s.id").fetchall()
    sessions = [s for s in sessions if s["series"] > 0]
    semaines = defaultdict(lambda: {"seances": 0, "volume": 0.0, "series": 0})
    jours = defaultdict(lambda: {"n": 0, "volume": 0.0})
    for s in sessions:
        d = date.fromisoformat(s["jour"])
        w = semaines[lundi(d).isoformat()]
        w["seances"] += 1
        w["volume"] += s["volume"]
        w["series"] += s["series"]
        if s["jour"] >= depuis52:
            j = jours[s["jour"]]
            j["n"] += 1
            j["volume"] += s["volume"]
    debut_semaine = lundi(aujourdhui)
    douze = []
    for i in range(11, -1, -1):
        k = (debut_semaine - timedelta(weeks=i)).isoformat()
        w = semaines.get(k, {"seances": 0, "volume": 0.0, "series": 0})
        douze.append({"debut": k, "seances": w["seances"], "volume": round(w["volume"], 1), "series": w["series"]})
    # régularité : semaines consécutives où l'objectif est atteint (la semaine en cours ne casse jamais la série)
    cette = semaines.get(debut_semaine.isoformat(), {"seances": 0})["seances"]
    serie_sem, k = 0, debut_semaine
    if cette >= obj:
        serie_sem, k = 1, k - timedelta(weeks=1)
    else:
        k = k - timedelta(weeks=1)
    while semaines.get(k.isoformat(), {"seances": 0})["seances"] >= obj:
        serie_sem += 1
        k -= timedelta(weeks=1)
    il_y_a_30 = (aujourdhui - timedelta(days=30)).isoformat()
    groupes = [{"groupe": r["groupe"], "series": r["n"]} for r in db.execute(
        "SELECT e.groupe, COUNT(*) n FROM serie r JOIN seance s ON s.id=r.seance_id "
        "JOIN exercice e ON e.id=r.exercice_id WHERE r.fait=1 AND r.echauffement=0 AND " + VALIDE +
        " AND s.jour>=? GROUP BY e.groupe ORDER BY n DESC", (il_y_a_30,))]
    noms = {r["id"]: (r["nom"], r["groupe"], r["type"]) for r in db.execute("SELECT id, nom, groupe, type FROM exercice")}
    recents = []
    for ev in an["evenements"][:12]:
        nom, groupe, typ = noms.get(ev["exercice_id"], ("?", "autre", "charge"))
        recents.append({**ev, "nom": nom, "groupe": groupe, "type": typ})
    mesures = [dict(r) for r in db.execute("SELECT id, jour, poids FROM mesure ORDER BY jour DESC LIMIT 180")][::-1]
    return {
        "totaux": {"seances": len(sessions), "series": sum(s["series"] for s in sessions),
                   "volume": round(sum(s["volume"] for s in sessions), 1),
                   "minutes": sum(s["duree_min"] or 0 for s in sessions),
                   "depuis": min((s["jour"] for s in sessions), default=None)},
        "semaines": douze, "jours": jours, "groupes": groupes, "objectif": obj, "cette_semaine": cette,
        "regularite": serie_sem, "records_recents": recents, "mesures": mesures,
    }


# ---------------------------------------------------------------- écriture partagée (API + CLI)
class Erreur(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def series_pour_exercice(db, exo_id, n, defaut=None, avant=None):
    """Séries pré-remplies : celles de la dernière fois (ou les valeurs du modèle), n lignes."""
    jour, sid = avant if avant else ("9999-12-31", 10 ** 12)
    p = precedent(db, exo_id, jour, sid)
    travail = [x for x in (p["series"] if p else []) if not x["echauffement"]]
    base = [{k: x[k] for k in ("reps", "poids", "duree_s", "distance_m")} for x in travail]
    if not base:
        d = defaut or {}
        base = [{"reps": d.get("reps"), "poids": d.get("poids"), "duree_s": d.get("duree_s"),
                 "distance_m": d.get("distance_m")}]
    out = []
    for i in range(n):
        out.append(dict(base[i] if i < len(base) else base[-1]))
    return out


def prochain_ordre(db, sid):
    return (db.execute("SELECT COALESCE(MAX(ordre),0) FROM serie WHERE seance_id=?", (sid,)).fetchone()[0]) + 1


def ajouter_series(db, sid, exo_id, series, fait):
    o = prochain_ordre(db, sid)
    for s in series:
        db.execute("INSERT INTO serie(seance_id,exercice_id,ordre,reps,poids,duree_s,distance_m,echauffement,fait) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (sid, exo_id, o, s.get("reps"), s.get("poids"), s.get("duree_s"), s.get("distance_m"),
                    1 if s.get("echauffement") else 0, 1 if fait else 0))
        o += 1


def nb_series_par_defaut(typ, precedentes):
    travail = [x for x in precedentes if not x["echauffement"]]
    if travail:
        return min(len(travail), 6)
    return 3 if typ in ("charge", "corps") else 1


def exercice_par_nom(db, nom):
    """Retrouve un exercice par son nom (insensible à la casse et aux accents, début de nom accepté)."""
    cible = sans_accents(nom).strip()
    tous = db.execute("SELECT * FROM exercice WHERE archive=0").fetchall()
    exacts = [e for e in tous if sans_accents(e["nom"]) == cible]
    if exacts:
        return exacts[0]
    partiels = [e for e in tous if cible and cible in sans_accents(e["nom"])]
    if len(partiels) == 1:
        return partiels[0]
    if len(partiels) > 1:
        raise Erreur(400, "Plusieurs exercices correspondent : " + ", ".join(e["nom"] for e in partiels[:6]))
    raise Erreur(404, f"Exercice « {nom} » introuvable.")


# ---------------------------------------------------------------- exports et sauvegarde
def donnees_export(db):
    seances = db.execute("SELECT * FROM seance WHERE en_cours=0 ORDER BY jour, id").fetchall()
    out = []
    for s in seances:
        rows = db.execute(
            "SELECT r.*, e.nom, e.groupe, e.type FROM serie r JOIN exercice e ON e.id=r.exercice_id "
            "WHERE r.seance_id=? AND r.fait=1 AND " + VALIDE + " ORDER BY r.ordre, r.id", (s["id"],)).fetchall()
        if not rows:
            continue
        blocs = {}
        for r in rows:
            b = blocs.setdefault(r["exercice_id"], {"exercice": r["nom"], "groupe": r["groupe"], "type": r["type"], "series": []})
            b["series"].append({"reps": r["reps"], "poids": r["poids"], "duree_s": r["duree_s"],
                                "distance_m": r["distance_m"], "echauffement": bool(r["echauffement"])})
        out.append({"jour": s["jour"], "nom": s["nom"], "duree_min": s["duree_min"], "note": s["note"],
                    "volume": resume_series([dict(x) for x in rows])["volume"], "exercices": list(blocs.values())})
    return out


def export_csv(db):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["jour", "seance", "exercice", "groupe", "numero_serie", "echauffement", "repetitions",
                "poids_kg", "duree_s", "distance_m"])
    for s in donnees_export(db):
        for b in s["exercices"]:
            for i, x in enumerate(b["series"], 1):
                w.writerow([s["jour"], s["nom"], b["exercice"], GROUPES.get(b["groupe"], b["groupe"]), i,
                            1 if x["echauffement"] else 0, x["reps"] if x["reps"] is not None else "",
                            fmt_nombre(x["poids"]), x["duree_s"] if x["duree_s"] is not None else "",
                            fmt_nombre(x["distance_m"])])
    return "\ufeff" + buf.getvalue()  # BOM : Excel lit correctement les accents


def texte_serie(typ, x):
    if typ == "duree":
        t = f"{x['duree_s']} s"
    elif typ == "cardio":
        t = " en ".join(p for p in (f"{fmt_nombre((x['distance_m'] or 0) / 1000)} km" if x["distance_m"] else "",
                                    f"{fmt_nombre(round((x['duree_s'] or 0) / 60, 1))} min" if x["duree_s"] else "") if p)
    else:
        t = f"{x['reps']}" + (f"×{fmt_nombre(x['poids'])}" if x["poids"] else "")
    return f"({t})" if x["echauffement"] else t


def export_markdown(db):
    lignes = ["# Journal d'entraînement\n"]
    for s in reversed(donnees_export(db)):
        meta = ", ".join(p for p in (f"{s['duree_min']} min" if s["duree_min"] else "",
                                     f"{fmt_nombre(s['volume'])} kg soulevés" if s["volume"] else "") if p)
        lignes.append(f"## {s['jour']} — {s['nom']}" + (f" ({meta})" if meta else "") + "\n")
        for b in s["exercices"]:
            lignes.append(f"- **{b['exercice']}** : " + ", ".join(texte_serie(b["type"], x) for x in b["series"]))
        if s["note"]:
            lignes.append(f"\n> {s['note']}")
        lignes.append("")
    return "\n".join(lignes)


def export_json(db):
    return json.dumps({"seances": donnees_export(db),
                       "mesures": [dict(r) for r in db.execute("SELECT jour, poids FROM mesure ORDER BY jour")]},
                      ensure_ascii=False, indent=2)


def sauvegarde_octets(db):
    """Copie cohérente de toute la base grâce à l'API de sauvegarde de SQLite."""
    with tempfile.TemporaryDirectory() as tmp:
        chemin = Path(tmp) / "reps.db"
        dest = sqlite3.connect(chemin)
        try:
            db.backup(dest)
        finally:
            dest.close()
        return chemin.read_bytes()


# ---------------------------------------------------------------- ligne de commande
def decimal(s):
    return float(re.sub(r"\s+", "", s).replace(",", ".").replace("kg", ""))


def lire_duree(texte):
    """« 45 », « 45s », « 1m30 », « 23min30 », « 25min », « 1h05 » -> secondes."""
    t = texte.strip().lower().replace(" ", "")
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)(?:min|m))?(?:(\d+)s?)?", t)
    if not t or not m or not any(m.groups()):
        raise ValueError(texte)
    h, mi, sec = (int(x or 0) for x in m.groups())
    if m.group(1) and m.group(3) and not m.group(2) and not t.endswith("s"):  # « 1h05 » = 1 h 05 min
        mi, sec = sec, 0
    return h * 3600 + mi * 60 + sec


def lire_serie(jeton, typ):
    """« 8x60 », « 10 », « 45s », « 1m30 », « 5km », « 5km:25min » -> dictionnaire de série."""
    j = jeton.strip().lower().replace("×", "x")
    m = re.fullmatch(r"(\d+)\s*x\s*([\d.,]+)(?:kg)?", j)
    if m:
        return {"reps": int(m.group(1)), "poids": decimal(m.group(2))}
    m = re.fullmatch(r"([\d.,]+)\s*km(?::(.+))?", j)
    if m:
        d = {"distance_m": round(decimal(m.group(1)) * 1000, 1)}
        if m.group(2):
            try:
                d["duree_s"] = lire_duree(m.group(2))
            except ValueError:
                raise Erreur(400, f"Durée illisible : « {m.group(2)} » (exemples : 25min, 23min30, 1h05).") from None
        return d
    if re.fullmatch(r"\d+", j):
        return {"duree_s": int(j)} if typ == "duree" else {"reps": int(j)}
    try:
        return {"duree_s": lire_duree(j)}
    except ValueError:
        raise Erreur(400, f"Série illisible : « {jeton} » (exemples : 8x60, 10, 45s, 1m30, 5km:25min).") from None


def cmd_log(db, a):
    exo = exercice_par_nom(db, a.exercice)
    jour = date.fromisoformat(a.jour).isoformat() if a.jour else date.today().isoformat()
    nom = a.seance or f"Séance du {jour}"
    s = db.execute("SELECT id FROM seance WHERE jour=? AND nom=? AND en_cours=0", (jour, nom)).fetchone()
    series = [lire_serie(t, exo["type"]) for t in a.series]
    with db:
        if s:
            sid = s["id"]
        else:
            sid = db.execute("INSERT INTO seance(nom,jour,duree_min) VALUES(?,?,?)", (nom, jour, a.duree)).lastrowid
        ajouter_series(db, sid, exo["id"], series, True)
    STATE["v"] += 1
    print(f"{exo['nom']} : {len(series)} série(s) ajoutée(s) à « {nom} » ({jour}).")


def cmd_historique(db, a):
    for s in liste_seances(db, a.recherche or "", a.limite)["seances"]:
        noms = ", ".join(e["nom"] for e in s["exercices"][:4]) + ("…" if len(s["exercices"]) > 4 else "")
        duree = f" · {s['duree_min']} min" if s["duree_min"] else ""
        prs = f" · {s['prs']} record(s)" if s["prs"] else ""
        print(f"[{s['id']}] {s['jour']} · {s['nom']}{duree} · {fmt_nombre(s['volume'])} kg · {s['nb_series']} série(s){prs}")
        if noms:
            print(f"      {noms}")


NOMS_RECORDS = {"poids": "charge max", "e1rm": "1RM estimé", "serie": "meilleure série", "reps": "répétitions",
                "lest": "lest max", "duree": "durée", "distance": "distance", "allure": "allure"}


def valeur_record(kind, v):
    if kind in ("poids", "e1rm", "lest"):
        return f"{fmt_nombre(v)} kg"
    if kind == "serie":
        return f"{fmt_nombre(v)} kg (reps×charge)"
    if kind == "duree":
        return f"{int(v)} s"
    if kind == "distance":
        return f"{fmt_nombre(v / 1000)} km"
    if kind == "allure":
        return f"{int(v // 60)}:{int(v % 60):02d} /km"
    return f"{fmt_nombre(v)}"


def cmd_records(db, a):
    exos = [e for e in liste_exercices(db) if e["records"]]
    if a.exercice:
        cible = exercice_par_nom(db, a.exercice)["id"]
        exos = [e for e in exos if e["id"] == cible]
    if not exos:
        print("Aucun record pour l'instant : note quelques séances d'abord.")
    for e in exos:
        print(f"{e['nom']} ({GROUPES.get(e['groupe'], e['groupe'])})")
        for kind, v in e["records"].items():
            print(f"   {NOMS_RECORDS.get(kind, kind)} : {valeur_record(kind, v['valeur'])}  ({v['jour']})")


def cmd_stats(db, a):
    st = calcul_stats(db)
    t = st["totaux"]
    if not t["seances"]:
        print("Aucune séance enregistrée.")
        return
    print(f"{t['seances']} séances depuis le {t['depuis']} · {t['series']} séries · "
          f"{fmt_nombre(t['volume'] / 1000)} t soulevées · {fmt_nombre(round(t['minutes'] / 60, 1))} h d'entraînement")
    print(f"Cette semaine : {st['cette_semaine']}/{st['objectif']} séances · régularité : {st['regularite']} semaine(s) de suite")
    for g in st["groupes"]:
        print(f"   {GROUPES.get(g['groupe'], g['groupe'])} : {g['series']} séries (30 derniers jours)")


def cmd_export(db, a):
    fonction = {"csv": export_csv, "json": export_json, "md": export_markdown}[a.format]
    sortie = fonction(db)
    if a.sortie:
        Path(a.sortie).write_text(sortie, encoding="utf-8", newline="")
        print(f"Exporté vers {a.sortie}")
    else:
        print(sortie)


def cmd_sauvegarde(db, a):
    chemin = Path(a.sortie or f"reps-sauvegarde-{date.today().isoformat()}.db")
    chemin.write_bytes(sauvegarde_octets(db))
    print(f"Sauvegarde enregistrée : {chemin}")


def build_parser():
    p = argparse.ArgumentParser(prog="reps", description="Reps : suivi de séances de sport")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("log", help="noter des séries : log \"Squat\" 5x100 5x100 5x100")
    s.add_argument("exercice"); s.add_argument("series", nargs="+", help="8x60, 10, 45s, 1m30, 5km:25min")
    s.add_argument("--jour", help="AAAA-MM-JJ (défaut : aujourd'hui)")
    s.add_argument("--seance", help="nom de la séance (défaut : « Séance du <jour> »)")
    s.add_argument("--duree", type=int, help="durée de la séance en minutes")
    s.set_defaults(fn=cmd_log)

    s = sub.add_parser("historique", help="lister les séances")
    s.add_argument("--limite", type=int, default=15); s.add_argument("--recherche")
    s.set_defaults(fn=cmd_historique)

    s = sub.add_parser("records", help="records personnels")
    s.add_argument("exercice", nargs="?"); s.set_defaults(fn=cmd_records)

    sub.add_parser("stats", help="résumé de l'entraînement").set_defaults(fn=cmd_stats)

    s = sub.add_parser("export", help="exporter en CSV, JSON ou Markdown")
    s.add_argument("--format", choices=["csv", "json", "md"], default="csv")
    s.add_argument("-o", "--sortie"); s.set_defaults(fn=cmd_export)

    s = sub.add_parser("sauvegarde", help="copie complète de la base")
    s.add_argument("-o", "--sortie"); s.set_defaults(fn=cmd_sauvegarde)

    s = sub.add_parser("web", help="version web locale (navigateur)")
    s.add_argument("--port", type=int, default=8001); s.add_argument("--no-browser", action="store_true")
    s.set_defaults(fn=cmd_web)

    sub.add_parser("gui", help="application (fenêtre dédiée)").set_defaults(fn=cmd_gui)
    return p


# ======================= INTERFACE (HTML/CSS/JS) =======================
APP_HTML = r'''<!doctype html>
<html lang="fr" data-theme="auto">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reps</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%2317181b'/%3E%3Ccircle cx='32' cy='32' r='20' fill='%23f2c230'/%3E%3Ccircle cx='32' cy='32' r='7' fill='%2317181b'/%3E%3C/svg%3E">
<style>
:root{
  --bg:#e9e8e4;--surface:#ffffff;--raised:#f4f3f0;--ink:#17181b;--muted:#62656c;--line:#d8d6d0;
  --accent:#f2c230;--accent-ink:#17181b;--good:#1f8a4c;--bad:#c62334;--bar:#17181b;
  --g-pecs:#d62839;--g-dos:#1f6fd1;--g-jambes:#f2b705;--g-epaules:#1f9d55;--g-bras:#f26a1b;--g-tronc:#7b4fd1;--g-cardio:#14a3a3;--g-autre:#6b7280;
  --display:"Franklin Gothic Medium","Avenir Next Condensed","Arial Narrow","Roboto Condensed","Helvetica Neue",system-ui,sans-serif;
  --text:"Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,"Helvetica Neue",sans-serif;
}
:root[data-theme="dark"]{
  --bg:#101114;--surface:#1a1c21;--raised:#22252b;--ink:#eeede9;--muted:#9a9ea8;--line:#2d313a;--good:#3cc47a;--bad:#ff6577;--bar:#0a0b0d;
  --g-pecs:#ff5468;--g-dos:#5aa2ff;--g-jambes:#ffcc33;--g-epaules:#3ccf7a;--g-bras:#ff8a4a;--g-tronc:#a183ff;--g-cardio:#3cd1d1;--g-autre:#9aa0ab;
}
@media (prefers-color-scheme:dark){:root[data-theme="auto"]{
  --bg:#101114;--surface:#1a1c21;--raised:#22252b;--ink:#eeede9;--muted:#9a9ea8;--line:#2d313a;--good:#3cc47a;--bad:#ff6577;--bar:#0a0b0d;
  --g-pecs:#ff5468;--g-dos:#5aa2ff;--g-jambes:#ffcc33;--g-epaules:#3ccf7a;--g-bras:#ff8a4a;--g-tronc:#a183ff;--g-cardio:#3cd1d1;--g-autre:#9aa0ab;
}}
*{box-sizing:border-box}
html{scroll-padding-top:70px}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 var(--text);-webkit-font-smoothing:antialiased}
button,input,select,textarea{font:inherit;color:inherit}
button{cursor:pointer}
a{color:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
[hidden]{display:none!important}
.mut{color:var(--muted)}
.num{font-variant-numeric:tabular-nums}

/* ---------- barre du haut ---------- */
.top{background:var(--bar);color:#fff;position:sticky;top:0;z-index:10}
.topin{max-width:1080px;margin:0 auto;display:flex;align-items:center;gap:26px;padding:0 20px;height:58px}
.brand{display:flex;align-items:center;gap:10px;text-decoration:none;font:400 26px/1 var(--display);letter-spacing:.06em}
.nav{display:flex;gap:4px;height:100%}
.nav a{display:flex;align-items:center;padding:0 14px;text-decoration:none;color:#c9cbd1;border-bottom:3px solid transparent;margin-top:3px}
.nav a:hover{color:#fff}
.nav a[aria-current="page"]{color:#fff;border-bottom-color:var(--accent)}
.topr{margin-left:auto;display:flex;gap:10px;align-items:center}
.livepill{display:flex;align-items:center;gap:8px;background:var(--accent);color:var(--accent-ink);border-radius:999px;padding:5px 12px;font-weight:700;text-decoration:none;font-size:14px}
.livepill i,.badge.live i{width:8px;height:8px;border-radius:50%;background:var(--bad);animation:pulse 1.4s infinite}
@keyframes pulse{50%{opacity:.25}}
.menu{position:relative}
.dots{background:none;border:1px solid #3a3d45;color:#fff;border-radius:8px;width:38px;height:36px;display:grid;place-items:center}
.dots:hover{background:#25272c}
.drop{position:absolute;right:0;top:44px;background:var(--surface);color:var(--ink);border:1px solid var(--line);border-radius:12px;padding:6px;min-width:250px;box-shadow:0 12px 30px rgba(0,0,0,.25)}
.drop a,.drop button{display:flex;gap:10px;align-items:center;width:100%;text-align:left;background:none;border:0;border-radius:8px;padding:9px 12px;text-decoration:none}
.drop a:hover,.drop button:hover{background:var(--raised)}
.drop hr{border:0;border-top:1px solid var(--line);margin:6px 0}

/* ---------- éléments communs ---------- */
.page{max-width:1080px;margin:0 auto;padding:26px 20px 120px}
h1,h2,h3{font-family:var(--display);font-weight:400;margin:0}
h1{font-size:clamp(32px,5vw,46px);line-height:1.05}
h2{font-size:26px;margin:34px 0 12px}
.btn{display:inline-flex;align-items:center;gap:8px;border:1.5px solid var(--ink);background:transparent;border-radius:10px;padding:9px 16px;font-weight:600;text-decoration:none;transition:background .12s}
.btn:hover{background:var(--raised)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);font-weight:700}
.btn.primary:hover{filter:brightness(1.06);background:var(--accent)}
.btn.dark{background:var(--ink);color:var(--bg)}
.btn.danger{color:var(--bad);border-color:var(--bad)}
.btn.danger:hover{background:var(--bad);color:#fff}
.btn.small{padding:6px 12px;font-size:14px;border-width:1px;border-color:var(--line)}
.btn.small:hover{border-color:var(--ink)}
.btn[aria-pressed="true"]{background:var(--ink);color:var(--bg)}
.iconbtn{border:0;background:none;padding:6px;border-radius:8px;color:var(--muted);display:grid;place-items:center}
.iconbtn:hover{background:var(--raised);color:var(--ink)}
.plate{--gc:var(--g-autre);display:inline-block;width:13px;height:13px;border-radius:50%;background:var(--gc);position:relative;vertical-align:-1px;margin-right:6px;flex:none}
.plate::after{content:"";position:absolute;inset:4px;border-radius:50%;background:var(--surface)}
.plate.big{width:30px;height:30px;margin:0}
.plate.big::after{inset:11px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:14px}
.badge{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:1px 10px;font-size:13px;font-weight:600;background:var(--raised);border:1px solid var(--line);margin-left:8px;vertical-align:2px}
.badge.live{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.badge.pr{background:none;margin:0}
.none{color:var(--muted);padding:28px 4px}
.back{display:inline-block;margin-bottom:14px;color:var(--muted);text-decoration:none}
.back:hover{color:var(--ink)}
.field{display:block;margin:14px 0 4px;font-weight:600;font-size:14px}
.inp{width:100%;border:1.5px solid var(--line);background:var(--raised);border-radius:9px;padding:9px 11px;outline:none}
.inp:focus{border-color:var(--ink)}
.search{display:flex;align-items:center;gap:10px;background:var(--surface);border:1.5px solid var(--line);border-radius:10px;padding:8px 12px;color:var(--muted);min-width:240px}
.search:focus-within{border-color:var(--ink)}
.search input{border:0;background:none;outline:none;color:var(--ink);flex:1;min-width:0}

/* ---------- accueil ---------- */
.hero{display:flex;flex-wrap:wrap;gap:18px;align-items:flex-end;justify-content:space-between;margin-bottom:22px}
.week{display:flex;gap:6px;align-items:center;padding:16px 18px;margin-bottom:22px;flex-wrap:wrap}
.wdays{display:flex;gap:10px}
.wd{display:grid;justify-items:center;gap:5px;font-size:12px;color:var(--muted)}
.wd span.c{width:34px;height:34px;border-radius:50%;border:2px solid var(--line);display:grid;place-items:center;font:400 16px var(--display);color:var(--muted)}
.wd.on span.c{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.wd.today span.c{border-color:var(--ink)}
.wtxt{margin-left:18px;flex:1;min-width:200px}
.wtxt b{font:400 24px var(--display)}
.bar{height:6px;border-radius:3px;background:var(--line);overflow:hidden;margin-top:8px;max-width:320px}
.bar i{display:block;height:100%;background:var(--ink)}
.livecard{display:flex;gap:16px;align-items:center;justify-content:space-between;flex-wrap:wrap;padding:16px 20px;margin-bottom:22px;background:var(--accent);color:var(--accent-ink);border-radius:14px}
.livecard h3{font-size:24px}
#liste{margin-top:16px}
.mois{font:400 20px var(--display);color:var(--muted);margin:26px 0 8px;text-transform:capitalize}
.sc{display:grid;grid-template-columns:70px minmax(0,1fr) auto;gap:18px;align-items:center;padding:14px 18px 14px 0;margin-bottom:10px;text-decoration:none;position:relative}
.sc:hover{border-color:var(--ink)}
.sc .stub{display:grid;justify-items:center;border-right:2px dashed var(--line);padding:4px 0}
.sc .dn{font:400 34px/1 var(--display)}
.sc .mo{font-size:13px;color:var(--muted)}
.sc h3{font-size:22px}
.sc .ex{margin:4px 0 0;color:var(--muted);font-size:14px;display:flex;flex-wrap:wrap;gap:2px 14px}
.sc .ex span{white-space:nowrap}
.smet{display:flex;gap:16px;align-items:center;font-variant-numeric:tabular-nums;font-size:14px;flex-wrap:wrap;justify-content:flex-end}
.smet b{font:400 18px var(--display)}

/* ---------- séance ---------- */
.shead{margin-bottom:18px}
.titre{font:400 clamp(30px,5vw,44px)/1.05 var(--display);border:0;border-bottom:2px solid transparent;background:none;width:100%;outline:none;padding:2px 0;color:var(--ink)}
.titre:hover,.titre:focus{border-bottom-color:var(--line)}
.sline{display:flex;flex-wrap:wrap;gap:8px 20px;align-items:center;margin:8px 0 14px;color:var(--muted)}
.sline input[type=date]{border:1.5px solid var(--line);background:var(--surface);border-radius:8px;padding:4px 8px;color:var(--ink)}
.clock{font:400 30px var(--display);font-variant-numeric:tabular-nums;color:var(--ink);display:inline-flex;align-items:center;gap:10px}
.clock i{width:10px;height:10px;border-radius:50%;background:var(--bad);animation:pulse 1.4s infinite}
.sacts{display:flex;flex-wrap:wrap;gap:8px}
.bloc{padding:16px 18px 14px;margin-bottom:16px}
.bh{display:flex;gap:12px;align-items:center;margin-bottom:6px}
.bh h3{font-size:24px;line-height:1.1}
.bh p{margin:0;font-size:14px}
.bh .iconbtn{margin-left:auto}
.prev{font-size:14px;color:var(--muted);margin:2px 0 10px;padding-left:42px}
.cols,.row{display:grid;align-items:center;gap:8px}
.cols{font-size:12px;color:var(--muted);text-align:center;margin-bottom:4px}
.t-charge .cols,.t-charge .row{grid-template-columns:38px 86px 14px 104px minmax(0,1fr) 34px 42px 30px}
.t-corps .cols,.t-corps .row{grid-template-columns:38px 86px 14px 104px minmax(0,1fr) 34px 42px 30px}
.t-duree .cols,.t-duree .row{grid-template-columns:38px 130px minmax(0,1fr) 34px 42px 30px}
.t-cardio .cols,.t-cardio .row{grid-template-columns:38px 100px 130px minmax(0,1fr) 34px 42px 30px}
.row{padding:4px 0;border-radius:10px}
.row.fait{background:color-mix(in srgb,var(--good) 10%,transparent)}
.row .n{text-align:center;font:400 20px var(--display);color:var(--muted)}
.row .x{text-align:center;color:var(--muted)}
.in{width:100%;height:42px;text-align:center;font:400 22px var(--display);font-variant-numeric:tabular-nums;background:var(--raised);border:1.5px solid var(--line);border-radius:9px;outline:none;padding:0 4px}
.in:focus{border-color:var(--ink)}
.in::placeholder{color:var(--muted);opacity:.55}
.w{width:32px;height:32px;border-radius:8px;border:1.5px solid var(--line);background:none;color:var(--muted);font-size:13px;font-weight:700}
.w[aria-pressed="true"]{background:var(--ink);border-color:var(--ink);color:var(--bg)}
.ok{width:40px;height:40px;border-radius:50%;border:2px solid var(--line);background:none;display:grid;place-items:center;color:transparent}
.ok:hover{border-color:var(--good)}
.ok[aria-pressed="true"]{background:var(--good);border-color:var(--good);color:#fff}
.rx{opacity:0;transition:opacity .12s}
.row:hover .rx,.rx:focus-visible{opacity:1}
@media (hover:none){.rx{opacity:1}}
.mid{display:flex;align-items:center;gap:8px;min-width:0;padding-left:4px}
.pr{font-size:18px}
.plates{background:none;border:0;padding:0;line-height:0}
.allure{font-size:13px;color:var(--muted)}
.add{margin:2px 0 26px}
.snote textarea{width:100%;min-height:90px;resize:vertical;margin-top:8px}

/* ---------- graphiques ---------- */
.grid2{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:22px}
.panel{padding:18px 20px}
.panel h3{font-size:22px;margin-bottom:10px}
.chart svg{width:100%;height:auto;display:block;overflow:visible}
.ax{fill:var(--muted);font:11px var(--text)}
.gl{stroke:var(--line);stroke-width:1}
.bar-r{fill:var(--ink)}
.bar-r.now{fill:var(--accent)}
.bar-r:hover{opacity:.75}
.ln{fill:none;stroke:var(--ink);stroke-width:2.5;stroke-linejoin:round;stroke-linecap:round}
.pt{fill:var(--surface);stroke:var(--ink);stroke-width:2}
.pt.rec{fill:var(--accent);stroke:var(--ink)}
.heat{display:grid;grid-auto-flow:column;grid-template-rows:repeat(7,14px);gap:3px;overflow-x:auto;padding-bottom:4px}
.hc{width:14px;height:14px;border-radius:3px;background:var(--raised);border:1px solid var(--line)}
.hc.l1{background:#f7e08a;border-color:#f7e08a}.hc.l2{background:#f2c230;border-color:#f2c230}.hc.l3{background:#d99a00;border-color:#d99a00}.hc.l4{background:#a56a00;border-color:#a56a00}
.hc.fut{visibility:hidden}
.legend{display:flex;gap:6px;align-items:center;font-size:12px;color:var(--muted);margin-top:8px}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0 10px}
.tabs button{border:1.5px solid var(--line);background:none;border-radius:999px;padding:3px 12px;font-size:14px}
.tabs button[aria-pressed="true"]{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.rec-list{list-style:none;margin:0;padding:0}
.rec-list li{display:flex;gap:10px;align-items:baseline;padding:8px 0;border-top:1px solid var(--line)}
.rec-list li:first-child{border-top:0}
.rec-list .v{margin-left:auto;font:400 18px var(--display);white-space:nowrap}
.gbar{display:grid;grid-template-columns:110px minmax(0,1fr) 84px;gap:10px;align-items:center;margin:8px 0;font-size:14px}
.gbar i{display:block;height:10px;border-radius:5px;background:var(--gc)}
.gbar .t{height:10px;border-radius:5px;background:var(--raised)}
.sentence{font-size:19px;max-width:60ch;margin:6px 0 4px}

/* ---------- exercices et modèles ---------- */
.egroup h2{display:flex;align-items:center;gap:10px}
.erow{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:16px;align-items:center;padding:11px 16px;border-top:1px solid var(--line)}
.erow:first-child{border-top:0}
.erow b{font-weight:600}
.erow .mut{font-size:14px}
.mcard{padding:16px 20px;margin-bottom:12px}
.mcard h3{font-size:24px;margin-bottom:6px}
.mcard ul{margin:6px 0 12px;padding-left:0;list-style:none;color:var(--muted)}
.mcard li{margin:2px 0}

/* ---------- fenêtres ---------- */
.scrim{position:fixed;inset:0;background:rgba(8,9,12,.6);backdrop-filter:blur(3px);display:grid;place-items:center;padding:20px;z-index:30;animation:fade .15s}
.dialog{width:min(520px,100%);max-height:90vh;overflow:auto;background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:24px;animation:pop .18s}
.dialog h3{font-size:30px;line-height:1.05;margin-bottom:6px}
.dialog p{color:var(--muted);margin:6px 0}
.dactions{display:flex;justify-content:flex-end;gap:8px;margin-top:22px;flex-wrap:wrap}
.pick{max-height:46vh;overflow:auto;margin:10px -6px 0}
.pick button{display:flex;align-items:center;gap:10px;width:100%;text-align:left;background:none;border:0;border-radius:9px;padding:9px 10px}
.pick button:hover{background:var(--raised)}
.pick small{margin-left:auto;color:var(--muted)}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0 0}
.chips button{border:1.5px solid var(--line);background:none;border-radius:999px;padding:2px 11px;font-size:13px}
.chips button[aria-pressed="true"]{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.radios label{display:flex;gap:10px;align-items:center;padding:9px 12px;border:1.5px solid var(--line);border-radius:10px;margin:6px 0;cursor:pointer}
.radios input:checked+span{font-weight:700}
.radios label:has(input:checked){border-color:var(--ink);background:var(--raised)}
.sum{display:flex;gap:26px;margin:14px 0;flex-wrap:wrap}
.sum div{display:grid}
.sum b{font:400 34px/1 var(--display)}
.sum span{font-size:13px;color:var(--muted)}
#toasts{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);display:flex;flex-direction:column;gap:8px;z-index:40;align-items:center;pointer-events:none}
.toast{pointer-events:auto;background:var(--ink);color:var(--bg);padding:10px 16px;border-radius:10px;display:flex;gap:16px;align-items:center;animation:pop .18s}
.toast button{background:none;border:0;color:inherit;font-weight:700;text-decoration:underline}
#rest{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);background:var(--bar);color:#fff;border-radius:16px;padding:12px 16px 14px;display:grid;gap:8px;z-index:20;min-width:min(420px,92vw);box-shadow:0 12px 34px rgba(0,0,0,.35);border:1px solid #3a3d45}
#rest .r1{display:flex;align-items:center;gap:12px}
#rest .t{font:400 34px/1 var(--display);font-variant-numeric:tabular-nums;min-width:90px}
#rest .l{color:#c9cbd1;font-size:14px;flex:1}
#rest button{background:#2a2c33;border:0;color:#fff;border-radius:8px;padding:6px 10px;font-weight:600}
#rest button:hover{background:#3a3d45}
#rest .pg{height:5px;background:#34363d;border-radius:3px;overflow:hidden}
#rest .pg i{display:block;height:100%;background:var(--accent);transition:width .25s linear}
#rest.fin{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);animation:flash .6s 3}
#rest.fin .l{color:var(--accent-ink)}
@keyframes flash{50%{filter:brightness(1.25)}}
@keyframes fade{from{opacity:0}to{opacity:1}}
@keyframes pop{from{opacity:0;transform:translateY(6px) scale(.98)}to{opacity:1;transform:none}}

@media (max-width:820px){
  .topin{padding:0 10px;gap:8px}.brand{font-size:0;gap:0}.nav a{padding:0 7px;font-size:13.5px}.livepill span{display:none}.livepill{padding:6px 10px}
  .grid2{grid-template-columns:minmax(0,1fr)}
  .sc{grid-template-columns:56px minmax(0,1fr);}
  .smet{grid-column:1/-1;justify-content:flex-start;padding-left:18px}
  .t-charge .cols,.t-charge .row,.t-corps .cols,.t-corps .row{grid-template-columns:28px 64px 10px 82px minmax(0,1fr) 30px 38px 24px}
  .in{font-size:19px}
}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
@media print{
  :root,:root[data-theme="dark"],:root[data-theme="auto"]{--bg:#fff;--surface:#fff;--raised:#f6f6f6;--ink:#111;--muted:#555;--line:#cfcfcf;--bar:#111}
  body{background:#fff}
  .top,.sacts,.back,.add,#rest,#toasts,.rx,.w,.ok,.iconbtn,.btn,.snote textarea+*{display:none!important}
  .page{padding:0}
  .bloc{break-inside:avoid;border-color:#bbb}
  .in{border:0;background:none;font-size:18px}
  .titre{border:0}
  .row.fait{background:none}
}
</style>
</head>
<body>
<header class="top"><div class="topin">
  <a class="brand" href="#/seances"><svg viewBox="0 0 40 40" width="32" height="32" aria-hidden="true"><circle cx="20" cy="20" r="18" fill="#f2c230"/><circle cx="20" cy="20" r="6" fill="#17181b"/><circle cx="20" cy="20" r="12.5" fill="none" stroke="#17181b" stroke-width="1.6" stroke-opacity=".35"/></svg>REPS</a>
  <nav class="nav" id="nav" aria-label="Navigation">
    <a href="#/seances" data-p="seances">Séances</a><a href="#/progres" data-p="progres">Progrès</a><a href="#/exercices" data-p="exercices">Exercices</a><a href="#/modeles" data-p="modeles">Modèles</a>
  </nav>
  <div class="topr">
    <a class="livepill" id="livepill" href="#" hidden><i></i><span>Séance en cours</span></a>
    <div class="menu"><button class="dots" id="b-menu" aria-label="Menu" aria-expanded="false"></button>
      <div class="drop" id="drop" hidden>
        <a href="/api/export?format=csv" download id="m-csv"></a>
        <a href="/api/export?format=md" download id="m-md"></a>
        <a href="/api/export?format=json" download id="m-json"></a>
        <hr>
        <a href="/api/sauvegarde" download id="m-save"></a>
        <button data-act="reglages" id="m-set"></button>
        <hr>
        <button data-act="quit" id="m-quit"></button>
      </div></div>
  </div>
</div></header>
<main id="main"></main>
<div id="rest" hidden></div>
<div id="toasts" aria-live="polite"></div>

<script>
(()=>{
'use strict';
const $=(s,r=document)=>r.querySelector(s),$$=(s,r=document)=>[...r.querySelectorAll(s)];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const NUM=new Intl.NumberFormat('fr-FR',{maximumFractionDigits:2}),NUM1=new Intl.NumberFormat('fr-FR',{maximumFractionDigits:1});
const GR={pecs:'Pectoraux',dos:'Dos',jambes:'Jambes',epaules:'Épaules',bras:'Bras',tronc:'Tronc',cardio:'Cardio',autre:'Autre'};
const TYPES={charge:'Charge (répétitions × kg)',corps:'Poids du corps (lest en option)',duree:'Durée',cardio:'Cardio (distance + durée)'};
const EQUIP={barre:'Barre',haltere:'Haltères',machine:'Machine',poulie:'Poulie',corps:'Poids du corps',autre:'Autre'};
const PRN={poids:'Charge max',e1rm:'1RM estimé',serie:'Meilleure série',reps:'Répétitions max',lest:'Lest max',duree:'Durée max',distance:'Distance max',allure:'Meilleure allure'};
const P={
  edit:'<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/>',trash:'<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',check:'<path d="M5 12.5l4.5 4.5L19 7.5"/>',search:'<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
  dots:'<circle cx="12" cy="5" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="12" cy="19" r="1.4"/>',down:'<path d="M12 3v12"/><path d="M7 11l5 5 5-5"/><path d="M5 21h14"/>',
  disk:'<path d="M5 3h11l4 4v14H5z"/><path d="M8 3v6h8V3"/><rect x="8" y="14" width="8" height="7"/>',gear:'<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1"/>',
  power:'<path d="M12 3v9"/><path d="M6.3 6.3a8 8 0 1011.4 0"/>',print:'<path d="M6 9V3h12v6"/><rect x="4" y="9" width="16" height="9" rx="2"/><path d="M7 14h10v7H7z"/>',
  copy:'<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 012-2h9"/>',bolt:'<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',tpl:'<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 8h8M8 12h8M8 16h5"/>'
};
const ic=(n,s=18)=>`<svg viewBox="0 0 24 24" width="${s}" height="${s}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[n]}</svg>`;

const S={reg:{theme:'auto',repos:'90',barre:'20',objectif:'3'},sd:null,exos:null,rest:null,live:null,q:'',clock:null};
const THEMES=['auto','clair','sombre'];

async function api(m,u,b){
  const o={method:m,headers:{}};
  if(m==='POST'||m==='PUT'){o.headers['Content-Type']='application/json';o.body=JSON.stringify(b||{})}
  const r=await fetch('/api/'+u,o);let j={};try{j=await r.json()}catch(e){}
  if(!r.ok)throw new Error(j.erreur||('Erreur '+r.status));return j;
}
function toast(msg,action,cb){
  const pile=$('#toasts').children;while(pile.length>=2)pile[0].remove();
  const el=document.createElement('div');el.className='toast';el.setAttribute('role','status');el.innerHTML=`<span>${esc(msg)}</span>`;
  if(action){const b=document.createElement('button');b.textContent=action;b.onclick=()=>{el.remove();cb()};el.append(b)}
  $('#toasts').append(el);setTimeout(()=>el.remove(),action?7000:3200);
}
const fail=e=>toast(e&&e.message?e.message:'Une erreur est survenue.');
function dialog(html,onsubmit){
  const s=document.createElement('div');s.className='scrim';s.innerHTML=`<form class="dialog" role="dialog" aria-modal="true">${html}</form>`;document.body.append(s);
  const f=s.firstElementChild,close=()=>s.remove();
  s.addEventListener('mousedown',e=>{if(e.target===s)close()});
  f.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',close));
  f.addEventListener('submit',async ev=>{ev.preventDefault();if(!onsubmit)return;try{await onsubmit(Object.fromEntries(new FormData(f)),close)}catch(err){fail(err)}});
  const first=f.querySelector('input:not([type=radio]):not([type=hidden]),select')||f.querySelector('button');if(first)first.focus();
  return {close,form:f};
}

/* ---------- formats ---------- */
const D=j=>new Date(j+'T12:00:00');
const today=()=>new Date().toLocaleDateString('sv-SE');
const fCourt=j=>D(j).toLocaleDateString('fr-FR',{day:'numeric',month:'short'}).replace('.','');
const fLong=j=>D(j).toLocaleDateString('fr-FR',{weekday:'long',day:'numeric',month:'long'});
const kg=x=>`${NUM.format(x)} kg`;
const vol=x=>x>=1000?`${NUM1.format(x/1000)} t`:`${NUM.format(x)} kg`;
const fDur=m=>!m?'':m>=60?`${Math.floor(m/60)} h ${String(m%60).padStart(2,'0')}`:`${m} min`;
const mmss=s=>{s=Math.round(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60),r=s%60;return h?`${h}:${String(m).padStart(2,'0')}:${String(r).padStart(2,'0')}`:`${m}:${String(r).padStart(2,'0')}`};
const numStr=x=>x==null?'':String(x).replace('.',',');
const dec=t=>{t=String(t??'').trim().replace(/\s+/g,'').replace(',','.').replace(/kg$/i,'');if(t==='')return null;const x=parseFloat(t);return Number.isFinite(x)?x:NaN};
function parseDuree(t,typ){
  t=String(t??'').trim().toLowerCase().replace(/\s+/g,'');if(t==='')return null;
  let m=t.match(/^(\d+):(\d{1,2})(?::(\d{1,2}))?$/);
  if(m)return m[3]!==undefined?(+m[1])*3600+(+m[2])*60+(+m[3]):(+m[1])*60+(+m[2]);
  m=t.match(/^(?:(\d+)h)?(?:(\d+)(?:min|m))?(?:(\d+)s?)?$/);
  if(m&&(m[1]||m[2]||m[3])){let h=+(m[1]||0),mi=+(m[2]||0),s=+(m[3]||0);if(m[1]&&m[3]&&!m[2]&&!t.endsWith('s')){mi=s;s=0}return h*3600+mi*60+s}
  return NaN;
}
const e1rm=(p,r)=>p&&r&&r>=1&&r<=12?Math.round(p*(1+r/30)*10)/10:null;
function texteSerie(typ,x){
  let t;
  if(typ==='duree')t=mmss(x.duree_s||0);
  else if(typ==='cardio')t=[x.distance_m?`${NUM.format(x.distance_m/1000)} km`:'',x.duree_s?mmss(x.duree_s):''].filter(Boolean).join(' en ');
  else t=`${x.reps??'?'}`+(x.poids?`×${NUM.format(x.poids)}`:'');
  return x.echauffement?`(${t})`:t;
}
function valKind(typ,s,k){
  if(k==='poids'||k==='lest')return kg(s.poids);if(k==='e1rm')return kg(e1rm(s.poids,s.reps));if(k==='serie')return kg(s.reps*s.poids);
  if(k==='reps')return `${s.reps}`;if(k==='duree')return mmss(s.duree_s);if(k==='distance')return `${NUM.format(s.distance_m/1000)} km`;
  if(k==='allure'){const a=s.duree_s/(s.distance_m/1000);return `${Math.floor(a/60)}:${String(Math.round(a%60)).padStart(2,'0')} /km`}
  return '';
}
const valRecord=(k,v)=>k==='poids'||k==='e1rm'||k==='lest'||k==='serie'?kg(v):k==='duree'?mmss(v):k==='distance'?`${NUM.format(v/1000)} km`:k==='allure'?`${Math.floor(v/60)}:${String(Math.round(v%60)).padStart(2,'0')} /km`:NUM.format(v);
const PRIO=['poids','reps','duree','distance','allure','lest','e1rm','serie'];
function grouperRecords(evs){
  const g=new Map();
  evs.forEach(e=>{const k=e.serie_id;if(!g.has(k))g.set(k,{...e,kinds:[]});g.get(k).kinds.push([e.kind,e.valeur])});
  return [...g.values()].map(x=>{x.kinds.sort((a,b)=>PRIO.indexOf(a[0])-PRIO.indexOf(b[0]));return x});
}
const plate=g=>`<span class="plate" style="--gc:var(--g-${g})" title="${GR[g]||''}"></span>`;

/* ---------- disques de barre ---------- */
const DISQUES=[25,20,15,10,5,2.5,1.25];
const COUL={25:'#d62839',20:'#1f6fd1',15:'#f2b705',10:'#1f9d55',5:'#f4f4f4',2.5:'#2b2d31',1.25:'#9aa0a6'};
function disques(poids,barre){let r=(poids-barre)/2;const out=[];if(!(r>0))return out;for(const d of DISQUES)while(r+1e-9>=d){out.push(d);r-=d}return out}
function svgDisques(list,mini){
  const w=mini?6:11,gap=mini?1:2,hmax=mini?24:70,H=hmax+4,L=mini?8:14;let x=L;const parts=[];
  list.forEach(d=>{const h=Math.round(hmax*(0.38+0.62*Math.min(1,d/25)));parts.push(`<rect x="${x}" y="${(H-h)/2}" width="${w}" height="${h}" rx="2" fill="${COUL[d]}" stroke="${d===5?'var(--muted)':'rgba(0,0,0,.35)'}" stroke-width="1"/>`);x+=w+gap});
  const W=x+(mini?8:20);
  return `<svg viewBox="0 0 ${W} ${H}" width="${mini?W*1.25:W*1.6}" height="${mini?H*1.25:H*1.6}" role="img" aria-label="Disques : ${list.join(', ')}"><rect x="0" y="${H/2-2}" width="${W}" height="4" rx="2" fill="var(--muted)"/><rect x="${L-4}" y="${H/2-6}" width="3" height="12" rx="1" fill="var(--muted)"/>${parts.join('')}</svg>`;
}

/* ---------- routage ---------- */
function setNav(p){$$('#nav a').forEach(a=>a.setAttribute('aria-current',a.dataset.p===p?'page':'false'))}
async function route(){
  const h=location.hash.replace(/^#\/?/,'')||'seances',[page,arg]=h.split('/');
  setNav(page==='seance'?'seances':page);stopClock();
  try{
    if(page==='seance')await pageSeance(+arg);else if(page==='progres')await pageProgres();else if(page==='exercices')await pageExercices();
    else if(page==='modeles')await pageModeles();else await pageSeances();
  }catch(e){fail(e);if(page==='seance')location.hash='#/seances'}
  window.scrollTo(0,0);
}
async function majLive(){
  try{const r=await api('GET','seances?limite=1');const s=r.seances[0];S.live=s&&s.en_cours?s:null}catch(e){S.live=null}
  const p=$('#livepill');p.hidden=!S.live;if(S.live)p.href='#/seance/'+S.live.id;
}

/* ---------- accueil : séances ---------- */
function carteSeance(s){
  const d=D(s.jour);
  return `<a class="sc card${s.en_cours?' live':''}" href="#/seance/${s.id}"><div class="stub"><span class="dn">${d.getDate()}</span><span class="mo">${esc(d.toLocaleDateString('fr-FR',{month:'short'}).replace('.',''))}</span></div>
    <div><h3>${esc(s.nom)}${s.en_cours?'<span class="badge live"><i></i>En cours</span>':''}</h3>
    <p class="ex">${s.exercices.slice(0,5).map(e=>`<span>${plate(e.groupe)}${esc(e.nom)}</span>`).join('')}${s.exercices.length>5?`<span>+${s.exercices.length-5}</span>`:''}</p></div>
    <div class="smet">${s.duree_min?`<span>${fDur(s.duree_min)}</span>`:''}${s.volume?`<span><b>${vol(s.volume)}</b></span>`:''}<span>${s.nb_series} série${s.nb_series>1?'s':''}</span>${s.prs?`<span class="badge pr" title="Records battus">🏆 ${s.prs}</span>`:''}</div></a>`;
}
function listeSeances(r){
  if(!r.seances.length)return S.q?`<p class="none">Aucune séance ne correspond à « ${esc(S.q)} ».</p>`:'<p class="none">Aucune séance pour l’instant. Démarre ta première ci-dessus : l’appli te montrera ce que tu avais fait la dernière fois.</p>';
  let mois='',h='';
  for(const s of r.seances){
    const m=D(s.jour).toLocaleDateString('fr-FR',{month:'long',year:'numeric'});
    if(m!==mois&&!s.en_cours){mois=m;h+=`<h2 class="mois">${esc(m)}</h2>`}
    h+=carteSeance(s);
  }
  return h+(r.total>r.seances.length?`<p class="none">${r.total-r.seances.length} séance${r.total-r.seances.length>1?'s':''} plus ancienne${r.total-r.seances.length>1?'s':''} : utilise la recherche pour les retrouver.</p>`:'');
}
async function pageSeances(){
  const [r,st]=await Promise.all([api('GET','seances?q='+encodeURIComponent(S.q)),api('GET','stats')]);
  await majLive();
  const lundi=new Date();lundi.setHours(12,0,0,0);lundi.setDate(lundi.getDate()-((lundi.getDay()+6)%7));
  const jours=[...Array(7)].map((_,i)=>{const d=new Date(lundi);d.setDate(d.getDate()+i);return d});
  const t=today(),L='LMMJVSD';
  const wk=jours.map((d,i)=>{const j=d.toLocaleDateString('sv-SE');return `<div class="wd${st.jours[j]?' on':''}${j===t?' today':''}" title="${esc(fLong(j))}"><span class="c">${st.jours[j]?'✓':d.getDate()}</span>${L[i]}</div>`}).join('');
  const pct=Math.min(100,Math.round(st.cette_semaine/st.objectif*100));
  const live=S.live?`<div class="livecard"><div><h3>${esc(S.live.nom)}</h3><span>Séance en cours depuis ${fDur(Math.max(1,Math.round((Date.now()/1000-(S.live.debut_ts||Date.now()/1000))/60)))||'quelques instants'}</span></div><a class="btn dark" href="#/seance/${S.live.id}">Reprendre la séance</a></div>`:'';
  $('#main').innerHTML=`<div class="page">
    <div class="hero"><div><h1>Séances</h1><p class="sentence mut" style="margin:6px 0 0">${st.totaux.seances?`${st.totaux.seances} séance${st.totaux.seances>1?'s':''}, ${vol(st.totaux.volume)} soulevées, ${NUM1.format(st.totaux.minutes/60)} h d’entraînement.`:'Ton journal d’entraînement commence ici.'}</p></div>
    <div class="sacts"><button class="btn primary" data-act="start">${ic('bolt')}Démarrer une séance</button><button class="btn" data-act="past">${ic('plus')}Saisir une séance passée</button></div></div>
    ${live}
    <section class="week card"><div class="wdays">${wk}</div><div class="wtxt"><b>${st.cette_semaine} sur ${st.objectif}</b> séances cette semaine${st.regularite>0?`, ${st.regularite} semaine${st.regularite>1?'s':''} de suite à l’objectif`:''}<div class="bar"><i style="width:${pct}%"></i></div></div></section>
    <label class="search">${ic('search')}<span class="sr">Rechercher</span><input id="q" type="search" placeholder="Rechercher une séance ou un exercice" value="${esc(S.q)}"></label>
    <div id="liste">${listeSeances(r)}</div></div>`;
}
let qTimer=null;
async function chercher(){
  const r=await api('GET','seances?q='+encodeURIComponent(S.q));$('#liste').innerHTML=listeSeances(r);
}

/* ---------- dialogues : démarrer / saisir ---------- */
async function dlgDemarrer(passee){
  const mods=(await api('GET','modeles')).modeles;
  if(!passee&&S.live)return toast('Une séance est déjà en cours : reprends-la ou termine-la.');
  const radios=`<div class="radios"><label><input type="radio" name="modele_id" value="" checked><span>Séance libre</span></label>${mods.map(m=>`<label><input type="radio" name="modele_id" value="${m.id}"><span>${esc(m.nom)}</span><small class="mut" style="margin-left:auto">${m.exercices.length} exercice${m.exercices.length>1?'s':''}</small></label>`).join('')}</div>`;
  dialog(`<h3>${passee?'Séance passée':'Nouvelle séance'}</h3>
    ${passee?`<label class="field" for="d-jour">Jour</label><input class="inp" id="d-jour" type="date" name="jour" value="${today()}" required>`:''}
    <label class="field" for="d-nom">Nom (facultatif)</label><input class="inp" id="d-nom" name="nom" maxlength="80" placeholder="Push A, Jambes, Footing…">
    <label class="field">Partir de</label>${radios}
    <div class="dactions"><button type="button" class="btn" data-close>Annuler</button><button class="btn primary">${passee?'Créer la séance':'C’est parti'}</button></div>`,
  async(f,close)=>{
    const r=await api('POST','seance',{nom:f.nom,jour:passee?f.jour:undefined,modele_id:f.modele_id||undefined,en_cours:!passee});
    close();location.hash='#/seance/'+r.id;
  });
}

/* ---------- séance ---------- */
function stopClock(){if(S.clock){clearInterval(S.clock);S.clock=null}}
function tickClock(){
  const el=$('#clock');if(!el||!S.sd)return;
  const t=Math.max(0,Math.round(Date.now()/1000-S.sd.seance.debut_ts));el.querySelector('span').textContent=mmss(t);
}
async function pageSeance(id){
  S.sd=await api('GET','seance/'+id);if(!S.exos)S.exos=(await api('GET','exercices')).exercices;
  await majLive();renderSeance();
  if(S.sd.seance.en_cours){tickClock();S.clock=setInterval(tickClock,1000)}
}
function metaSeance(){
  const d=S.sd,r=d.resume,s=d.seance;
  const p=[`${r.nb_exos} exercice${r.nb_exos>1?'s':''}`,`${r.nb_series} série${r.nb_series>1?'s':''}`];
  if(r.volume)p.push(vol(r.volume));if(s.duree_min&&!s.en_cours)p.push(fDur(s.duree_min));
  return p.join(', ')+(r.prs?` · 🏆 ${r.prs} record${r.prs>1?'s':''}`:'');
}
function colsHtml(t){
  const c={charge:['','Reps','','Charge (kg)','','',''],corps:['','Reps','','Lest (kg)','','',''],duree:['','Durée','','',''],cardio:['','Distance (km)','Durée','','','']}[t];
  return c.map(x=>`<span>${x}</span>`).join('')+'<span></span>'.repeat(0);
}
function prevHtml(b){
  const p=b.precedent;if(!p)return `<p class="prev">Première fois sur cet exercice.</p>`;
  return `<p class="prev">Dernière fois (${esc(fCourt(p.jour))}) : ${esc(p.series.map(x=>texteSerie(b.exercice.type,x)).join('  '))}</p>`;
}
function ligneHtml(ex,s,num,k,b){
  const pw=b.precedent?b.precedent.series.filter(x=>!x.echauffement)[k]:null,pv=pw||{};
  const barre=+S.reg.barre||20;let mid='';
  const prTxt=s.pr.map(x=>PRN[x]).join(', ');
  const prHtml=`<span class="pr" data-pr="${s.id}" ${s.pr.length?'':'hidden'} title="Record : ${esc(prTxt)}">🏆</span>`;
  let champs;
  if(ex.type==='charge'||ex.type==='corps'){
    champs=`<input class="in" data-f="reps" inputmode="numeric" value="${s.reps??''}" placeholder="${pv.reps??''}" aria-label="Répétitions"><span class="x">${ex.type==='charge'?'×':'+'}</span><input class="in" data-f="poids" inputmode="decimal" value="${numStr(s.poids)}" placeholder="${numStr(pv.poids)}" aria-label="${ex.type==='charge'?'Charge en kg':'Lest en kg'}">`;
    if(ex.equipement==='barre'&&s.poids>barre)mid=`<button type="button" class="plates" data-act="plates" data-w="${s.poids}" title="Voir les disques" aria-label="Voir les disques">${svgDisques(disques(s.poids,barre),true)}</button>`;
    mid+=prHtml;
  }else if(ex.type==='duree'){
    champs=`<input class="in" data-f="duree_s" inputmode="numeric" value="${s.duree_s!=null?mmss(s.duree_s):''}" placeholder="${pv.duree_s!=null?mmss(pv.duree_s):'mm:ss'}" aria-label="Durée">`;mid=prHtml;
  }else{
    const al=s.distance_m&&s.duree_s?(()=>{const a=s.duree_s/(s.distance_m/1000);return `${Math.floor(a/60)}:${String(Math.round(a%60)).padStart(2,'0')} /km`})():'';
    champs=`<input class="in" data-f="distance_m" inputmode="decimal" value="${s.distance_m!=null?numStr(s.distance_m/1000):''}" placeholder="${pv.distance_m!=null?numStr(pv.distance_m/1000):'km'}" aria-label="Distance en km"><input class="in" data-f="duree_s" inputmode="numeric" value="${s.duree_s!=null?mmss(s.duree_s):''}" placeholder="${pv.duree_s!=null?mmss(pv.duree_s):'mm:ss'}" aria-label="Durée">`;
    mid=`<span class="allure" data-allure="${s.id}">${al}</span>`+prHtml;
  }
  const warm=ex.type==='charge'||ex.type==='corps'?`<button type="button" class="w" data-act="warm" aria-pressed="${s.echauffement?'true':'false'}" title="Série d’échauffement (ne compte ni dans le volume ni dans les records)">É</button>`:'';
  return `<div class="row${s.fait?' fait':''}" data-sid="${s.id}"><span class="n">${num}</span>${champs}<div class="mid">${mid}</div>${warm}<button type="button" class="ok" data-act="ok" aria-pressed="${s.fait?'true':'false'}" aria-label="Série faite">${ic('check',20)}</button><button type="button" class="iconbtn rx" data-act="del-set" aria-label="Supprimer la série">${ic('trash',16)}</button></div>`;
}
function blocHtml(b){
  const ex=b.exercice;let n=0,k=0;
  const cols={charge:['','Reps','','Charge, kg'],corps:['','Reps','','Lest, kg'],duree:['','Durée'],cardio:['','Km','Durée']}[ex.type];
  const lignes=b.series.map(s=>{const num=s.echauffement?'É':++n;const kk=s.echauffement?-1:k++;return ligneHtml(ex,s,num,kk,b)}).join('');
  return `<article class="bloc card t-${ex.type}" data-eid="${ex.id}"><header class="bh"><span class="plate big" style="--gc:var(--g-${ex.groupe})"></span><div><h3>${esc(ex.nom)}</h3><p class="mut">${GR[ex.groupe]}, ${EQUIP[ex.equipement].toLowerCase()}</p></div><button type="button" class="iconbtn" data-act="del-exo" aria-label="Retirer l’exercice de la séance">${ic('trash')}</button></header>
    ${prevHtml(b)}<div class="cols">${cols.map(c=>`<span>${c}</span>`).join('')}</div><div class="rows">${lignes}</div>
    <button type="button" class="btn small" data-act="add-set">${ic('plus',14)}Série</button></article>`;
}
function renderSeance(){
  const d=S.sd,s=d.seance;
  $('#main').innerHTML=`<div class="page"><a class="back" href="#/seances">‹ Séances</a>
    <section class="shead"><input class="titre" id="s-nom" value="${esc(s.nom)}" maxlength="80" aria-label="Nom de la séance">
      <div class="sline"><input type="date" id="s-jour" value="${s.jour}" aria-label="Jour">${s.en_cours?'<span class="clock" id="clock"><i></i><span>0:00</span></span>':''}<span id="s-meta">${esc(metaSeance())}</span></div>
      <div class="sacts">${s.en_cours?`<button class="btn primary" data-act="finish">${ic('check')}Terminer la séance</button>`:''}
        <button class="btn" data-act="save-tpl">${ic('tpl')}Enregistrer comme modèle</button><button class="btn" data-act="redo">${ic('copy')}Refaire cette séance</button>
        <button class="btn" data-act="print">${ic('print')}Imprimer</button><button class="btn danger" data-act="del-seance">${ic('trash')}Supprimer</button></div></section>
    <div id="blocs">${d.exercices.map(blocHtml).join('')||'<p class="none">Aucun exercice pour l’instant.</p>'}</div>
    <button class="btn dark add" data-act="add-exo">${ic('plus')}Ajouter un exercice</button>
    <section class="snote"><label class="field" for="s-note">Notes de séance</label><textarea class="inp" id="s-note" placeholder="Sensations, douleurs, ce qu’il faut améliorer…">${esc(s.note||'')}</textarea></section></div>`;
  if(s.en_cours)tickClock();
}
/* met à jour records, volume et disques sans recréer les champs (on garde le focus) */
async function patchSeance(){
  const d=await api('GET','seance/'+S.sd.seance.id);S.sd=d;
  $('#s-meta').textContent=metaSeance();
  d.exercices.forEach(b=>b.series.forEach(s=>{
    const pr=$(`[data-pr="${s.id}"]`);if(pr){pr.hidden=!s.pr.length;pr.title='Record : '+s.pr.map(x=>PRN[x]).join(', ')}
    const row=$(`.row[data-sid="${s.id}"]`);if(!row)return;
    row.classList.toggle('fait',!!s.fait);
    const al=$(`[data-allure="${s.id}"]`);if(al)al.textContent=s.distance_m&&s.duree_s?(()=>{const a=s.duree_s/(s.distance_m/1000);return `${Math.floor(a/60)}:${String(Math.round(a%60)).padStart(2,'0')} /km`})():'';
    if(b.exercice.equipement==='barre'){
      const mid=row.querySelector('.mid'),btn=mid.querySelector('.plates'),barre=+S.reg.barre||20;
      if(s.poids>barre){const h=svgDisques(disques(s.poids,barre),true);if(btn){btn.dataset.w=s.poids;btn.innerHTML=h}else mid.insertAdjacentHTML('afterbegin',`<button type="button" class="plates" data-act="plates" data-w="${s.poids}" title="Voir les disques" aria-label="Voir les disques">${h}</button>`)}
      else if(btn)btn.remove();
    }
  }));
}
async function saveRow(row){
  const bloc=row.closest('.bloc'),typ=[...bloc.classList].find(c=>c.startsWith('t-')).slice(2),body={};
  for(const i of $$('.in',row)){
    const f=i.dataset.f;let v;
    if(f==='reps')v=dec(i.value);else if(f==='poids')v=dec(i.value);
    else if(f==='distance_m'){v=dec(i.value);if(v!=null&&!Number.isNaN(v))v=Math.round(v*1000)}
    else v=parseDuree(i.value,typ);
    if(Number.isNaN(v)){i.classList.add('bad');toast('Valeur illisible : '+i.value);return}
    body[f]=v;
  }
  await api('PUT','serie/'+row.dataset.sid,body);await patchSeance();
}
async function toggleOk(row){
  const btn=$('.ok',row),on=btn.getAttribute('aria-pressed')!=='true';
  await api('PUT','serie/'+row.dataset.sid,{fait:on});btn.setAttribute('aria-pressed',String(on));row.classList.toggle('fait',on);
  if(on&&S.sd.seance.en_cours&&!$('.w[aria-pressed="true"]',row))startRest(+S.reg.repos||90);
  await patchSeance();
}
async function ajouterSerie(bloc){
  const eid=+bloc.dataset.eid,b=S.sd.exercices.find(x=>x.exercice.id===eid),trav=b.series.filter(s=>!s.echauffement),der=trav[trav.length-1]||b.series[b.series.length-1]||{};
  await api('POST','serie',{seance_id:S.sd.seance.id,exercice_id:eid,reps:der.reps,poids:der.poids,duree_s:der.duree_s,distance_m:der.distance_m,fait:S.sd.seance.en_cours?0:1});
  S.sd=await api('GET','seance/'+S.sd.seance.id);renderSeance();
}
function choisirExercice(onpick){
  const exos=(S.exos||[]);let groupe='',q='';
  const d=dialog(`<h3>Ajouter un exercice</h3><label class="sr" for="p-q">Rechercher</label><input class="inp" id="p-q" placeholder="Rechercher ou créer un exercice" autocomplete="off">
    <div class="chips" id="p-chips">${Object.entries(GR).map(([k,v])=>`<button type="button" data-g="${k}" aria-pressed="false">${v}</button>`).join('')}</div>
    <div class="pick" id="p-list"></div><div class="dactions"><button type="button" class="btn" data-close>Fermer</button></div>`);
  const norm=s=>s.normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLowerCase();
  const maj=()=>{
    const f=exos.filter(e=>(!groupe||e.groupe===groupe)&&(!q||norm(e.nom).includes(norm(q))));
    const exact=exos.some(e=>norm(e.nom)===norm(q.trim()));
    $('#p-list',d.form).innerHTML=f.map(e=>`<button type="button" data-id="${e.id}">${plate(e.groupe)}<span>${esc(e.nom)}</span><small>${e.dernier_jour?esc(fCourt(e.dernier_jour)):EQUIP[e.equipement]}</small></button>`).join('')
      +(q.trim()&&!exact?`<button type="button" data-new="1"><b>${ic('plus',16)} Créer « ${esc(q.trim())} »</b></button>`:'')||'<p class="none">Aucun exercice.</p>';
  };
  maj();
  d.form.addEventListener('input',e=>{if(e.target.id==='p-q'){q=e.target.value;maj()}});
  d.form.addEventListener('click',async e=>{
    const g=e.target.closest('[data-g]');if(g){groupe=groupe===g.dataset.g?'':g.dataset.g;$$('#p-chips button',d.form).forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.g===groupe)));maj();return}
    const it=e.target.closest('[data-id]');if(it){d.close();onpick(+it.dataset.id);return}
    if(e.target.closest('[data-new]')){d.close();dlgExercice({nom:q.trim()},async ex=>{S.exos=(await api('GET','exercices')).exercices;onpick(ex.id)})}
  });
}
function dlgExercice(ex,onsaved){
  const edit=!!(ex&&ex.id);ex=ex||{};
  const opts=(o,v)=>Object.entries(o).map(([k,l])=>`<option value="${k}"${k===v?' selected':''}>${l}</option>`).join('');
  const d=dialog(`<h3>${edit?'Modifier l’exercice':'Nouvel exercice'}</h3>
    <label class="field" for="e-nom">Nom</label><input class="inp" id="e-nom" name="nom" required maxlength="80" value="${esc(ex.nom||'')}">
    <label class="field" for="e-g">Groupe musculaire</label><select class="inp" id="e-g" name="groupe">${opts(GR,ex.groupe||'autre')}</select>
    <label class="field" for="e-t">Type de saisie</label><select class="inp" id="e-t" name="type" ${edit&&ex.nb_series?'disabled title="Un exercice déjà utilisé garde son type"':''}>${opts(TYPES,ex.type||'charge')}</select>
    <label class="field" for="e-q">Équipement</label><select class="inp" id="e-q" name="equipement">${opts(EQUIP,ex.equipement||'autre')}</select>
    <p>Avec « Barre », Reps affiche les disques à charger de chaque côté.</p>
    <div class="dactions">${edit?`<button type="button" class="btn danger" data-act="e-del" style="margin-right:auto">${ex.archive?'Supprimer':'Supprimer ou archiver'}</button>`:''}<button type="button" class="btn" data-close>Annuler</button><button class="btn primary">${edit?'Enregistrer':'Créer'}</button></div>`,
  async(f,close)=>{
    if(f.type===undefined)delete f.type;
    const r=edit?await api('PUT','exercice/'+ex.id,f):await api('POST','exercice',f);close();S.exos=null;if(onsaved)await onsaved(r);else toast(edit?'Exercice enregistré.':'Exercice créé.');
  });
  const del=$('[data-act="e-del"]',d.form);
  if(del)del.addEventListener('click',async()=>{try{const r=await api('DELETE','exercice/'+ex.id);d.close();S.exos=null;toast(r.archive?'Exercice archivé : ton historique est conservé.':'Exercice supprimé.');if(onsaved)await onsaved(null)}catch(e){fail(e)}});
}

/* ---------- repos ---------- */
let audio=null;
function bip(){
  try{audio=audio||new (window.AudioContext||window.webkitAudioContext)();[0,.22,.44].forEach((t,i)=>{const o=audio.createOscillator(),g=audio.createGain();o.frequency.value=i===2?1175:880;g.gain.value=.12;o.connect(g);g.connect(audio.destination);o.start(audio.currentTime+t);o.stop(audio.currentTime+t+.14)})}catch(e){}
}
function startRest(s){S.rest={fin:Date.now()+s*1000,total:s,fini:false};drawRest()}
function drawRest(){
  const el=$('#rest'),r=S.rest;if(!r){el.hidden=true;return}
  const left=Math.max(0,Math.ceil((r.fin-Date.now())/1000));
  if(left===0&&!r.fini){r.fini=true;r.finAt=Date.now();bip()}
  if(r.fini&&Date.now()-r.finAt>6000){S.rest=null;el.hidden=true;return}
  el.hidden=false;el.classList.toggle('fin',r.fini);
  el.innerHTML=`<div class="r1"><span class="t">${mmss(left)}</span><span class="l">${r.fini?'Repos terminé, c’est reparti !':'Repos'}</span>${r.fini?'':'<button data-act="rest-m">−15 s</button><button data-act="rest-p">+15 s</button>'}<button data-act="rest-x">${r.fini?'Fermer':'Passer'}</button></div><div class="pg"><i style="width:${r.fini?100:Math.max(0,100-left/r.total*100)}%"></i></div>`;
}
setInterval(()=>{if(S.rest)drawRest()},250);

/* ---------- actions de la séance ---------- */
async function terminer(){
  const sets=S.sd.exercices.flatMap(b=>b.series),vides=sets.filter(s=>!s.fait||(s.reps==null&&s.duree_s==null&&s.distance_m==null)).length;
  dialog(`<h3>Terminer la séance ?</h3><p>${vides?`${vides} série${vides>1?'s':''} non cochée${vides>1?'s':''} ser${vides>1?'ont':'a'} retirée${vides>1?'s':''} de la séance.`:'Toutes tes séries sont cochées.'}</p>
    <div class="dactions"><button type="button" class="btn" data-close>Continuer la séance</button><button class="btn primary">Terminer</button></div>`,
  async(f,close)=>{
    const r=await api('POST',`seance/${S.sd.seance.id}/terminer`,{});close();stopClock();S.rest=null;drawRest();
    if(r.vide){toast('Séance vide : rien n’a été enregistré.');location.hash='#/seances';return}
    S.sd=r.detail;await majLive();renderSeance();resumeDialog(r.detail);
  });
}
function resumeDialog(d){
  const r=d.resume,prs=[];
  d.exercices.forEach(b=>b.series.forEach(s=>{if(!s.pr.length)return;const ks=[...s.pr].sort((a,c)=>PRIO.indexOf(a)-PRIO.indexOf(c));
    prs.push(`<li>${plate(b.exercice.groupe)}<span><b>${esc(b.exercice.nom)}</b><br><span class="mut" style="font-size:13px">${esc(ks.map(k=>PRN[k]).join(', '))}</span></span><span class="v">${esc(valKind(b.exercice.type,s,ks[0]))}</span></li>`)}));
  dialog(`<h3>Séance terminée</h3><div class="sum"><div><b>${r.nb_series}</b><span>séries</span></div>${r.volume?`<div><b>${vol(r.volume)}</b><span>soulevées</span></div>`:''}<div><b>${fDur(d.seance.duree_min)||'—'}</b><span>durée</span></div></div>
    ${prs.length?`<h3 style="font-size:22px;margin-top:6px">🏆 ${prs.length} record${prs.length>1?'s':''} battu${prs.length>1?'s':''}</h3><ul class="rec-list">${prs.join('')}</ul>`:'<p>Pas de record cette fois : la régularité, c’est ce qui paie.</p>'}
    <div class="dactions"><button type="button" class="btn" data-close>Fermer</button></div>`);
}
function dlgModele(){
  dialog(`<h3>Enregistrer comme modèle</h3><p>La prochaine fois, tu pourras démarrer cette séance en un clic, avec les exercices déjà en place et tes dernières charges.</p>
    <label class="field" for="m-nom">Nom du modèle</label><input class="inp" id="m-nom" name="nom" required maxlength="80" value="${esc(S.sd.seance.nom)}">
    <div class="dactions"><button type="button" class="btn" data-close>Annuler</button><button class="btn primary">Enregistrer</button></div>`,
  async(f,close)=>{await api('POST',`seance/${S.sd.seance.id}/modele`,{nom:f.nom});close();toast('Modèle enregistré.')});
}
function dlgPlates(w){
  const barre=+S.reg.barre||20,l=disques(w,barre),reste=Math.round(((w-barre)/2-l.reduce((a,b)=>a+b,0))*100)/100;
  const cnt={};l.forEach(x=>cnt[x]=(cnt[x]||0)+1);
  dialog(`<h3>${kg(w)} sur la barre</h3><p>Barre de ${kg(barre)}. À charger de chaque côté :</p><div style="margin:14px 0">${svgDisques(l,false)}</div>
    <p style="color:var(--ink)">${l.length?Object.entries(cnt).sort((a,b)=>b[0]-a[0]).map(([d,n])=>`<b>${n} × ${NUM.format(d)} kg</b>`).join(', '):'La barre seule suffit.'}${reste>0.01?` (il resterait ${NUM.format(reste*2)} kg impossibles à charger avec les disques standards)`:''}</p>
    <div class="dactions"><button type="button" class="btn" data-close>Fermer</button></div>`);
}

/* ---------- progrès ---------- */
function chartVolume(sem){
  const W=640,H=300,B=34,T=22,n=sem.length,max=Math.max(1,...sem.map(w=>w.volume)),slot=W/n,bw=Math.min(40,slot-12);
  let s=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Volume soulevé par semaine"><line class="gl" x1="0" x2="${W}" y1="${H-B}" y2="${H-B}"/>`;
  sem.forEach((w,i)=>{
    const h=Math.max(w.volume?3:0,w.volume/max*(H-B-T)),x=i*slot+(slot-bw)/2,y=H-B-h,now=i===n-1;
    s+=`<rect class="bar-r${now?' now':''}" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw}" height="${h.toFixed(1)}" rx="3"><title>Semaine du ${esc(fCourt(w.debut))} : ${vol(w.volume)}, ${w.seances} séance${w.seances>1?'s':''}</title></rect>`;
    if(w.seances)s+=`<text class="ax" x="${(x+bw/2).toFixed(1)}" y="${(y-5).toFixed(1)}" text-anchor="middle">${w.seances}</text>`;
    if(i%2===(n-1)%2)s+=`<text class="ax" x="${(x+bw/2).toFixed(1)}" y="${H-12}" text-anchor="middle">${esc(fCourt(w.debut))}</text>`;
  });
  return s+'</svg>';
}
function chartLigne(pts,fmtY,recVal,o){
  if(pts.length<1)return '<p class="none" style="padding:12px 0">Pas encore de données pour cette mesure.</p>';
  const W=(o&&o.W)||720,H=(o&&o.H)||230,L=48,R=14,T=16,B=30,ys=pts.map(p=>p.y),mn=Math.min(...ys),mx=Math.max(...ys),pad=(mx-mn)*0.15||Math.max(1,mx*0.1),lo=Math.max(0,mn-pad),hi=mx+pad;
  const t0=D(pts[0].x).getTime(),t1=D(pts[pts.length-1].x).getTime(),X=x=>pts.length===1||t1===t0?(L+W-R)/2:L+(D(x).getTime()-t0)/(t1-t0)*(W-L-R),Y=y=>T+(1-(y-lo)/(hi-lo))*(H-T-B);
  let s=`<svg viewBox="0 0 ${W} ${H}" role="img">`;
  for(let i=0;i<=3;i++){const v=lo+(hi-lo)*i/3,y=Y(v);s+=`<line class="gl" x1="${L}" x2="${W-R}" y1="${y}" y2="${y}"/><text class="ax" x="${L-8}" y="${y+4}" text-anchor="end">${esc(fmtY(v))}</text>`}
  s+=`<polyline class="ln" points="${pts.map(p=>`${X(p.x).toFixed(1)},${Y(p.y).toFixed(1)}`).join(' ')}"/>`;
  let dernierX=-999;
  pts.forEach((p,i)=>{const rec=recVal!=null&&p.y===recVal,x=X(p.x);s+=`<circle class="pt${rec?' rec':''}" cx="${x.toFixed(1)}" cy="${Y(p.y).toFixed(1)}" r="${rec?6:4}"><title>${esc(fCourt(p.x))} : ${esc(fmtY(p.y))}</title></circle>`;
    const ecart=x-dernierX>=70&&(i===pts.length-1?true:W-R-x>=70||pts.length<=6);
    if(i===0||ecart){s+=`<text class="ax" x="${x.toFixed(1)}" y="${H-8}" text-anchor="middle">${esc(fCourt(p.x))}</text>`;dernierX=x}});
  return s+'</svg>';
}
function heatmap(jours){
  const d0=new Date();d0.setHours(12,0,0,0);const lun=new Date(d0);lun.setDate(lun.getDate()-((lun.getDay()+6)%7));lun.setDate(lun.getDate()-25*7);
  const vols=Object.values(jours).map(j=>j.volume).filter(Boolean).sort((a,b)=>a-b),q=p=>vols.length?vols[Math.min(vols.length-1,Math.floor(p*vols.length))]:0;
  const q1=q(.25),q2=q(.5),q3=q(.75);let h='';
  for(let w=0;w<26;w++)for(let i=0;i<7;i++){
    const d=new Date(lun);d.setDate(d.getDate()+w*7+i);const j=d.toLocaleDateString('sv-SE'),x=jours[j],fut=d>d0;
    const lvl=!x?0:!x.volume?2:x.volume<=q1?1:x.volume<=q2?2:x.volume<=q3?3:4;
    h+=`<span class="hc${lvl?' l'+lvl:''}${fut?' fut':''}" title="${esc(fLong(j))}${x?` : ${x.n} séance${x.n>1?'s':''}${x.volume?', '+vol(x.volume):''}`:''}"></span>`;
  }
  return `<div class="heat" role="img" aria-label="Séances des 26 dernières semaines">${h}</div><div class="legend">Moins <span class="hc"></span><span class="hc l1"></span><span class="hc l2"></span><span class="hc l3"></span><span class="hc l4"></span> Plus</div>`;
}
const METRIQUES={
  charge:[['e1rm','1RM estimé',v=>`${NUM1.format(v)} kg`],['poids_max','Charge max',v=>`${NUM1.format(v)} kg`],['volume','Volume de la séance',v=>vol(v)]],
  corps:[['reps_max','Répétitions max',v=>NUM1.format(v)],['reps_total','Répétitions totales',v=>NUM1.format(v)],['poids_max','Lest max',v=>`${NUM1.format(v)} kg`]],
  duree:[['duree_max','Durée max',v=>mmss(v)]],
  cardio:[['distance_max','Distance max',v=>`${NUM.format(v/1000)} km`],['allure_min','Meilleure allure',v=>`${Math.floor(v/60)}:${String(Math.round(v%60)).padStart(2,'0')}`]]
};
const REC_DE={e1rm:'e1rm',poids_max:'poids',volume:null,reps_max:'reps',reps_total:null,duree_max:'duree',distance_max:'distance',allure_min:'allure'};
async function dessinerExo(){
  const id=S.progId;if(!id){$('#exo-zone').innerHTML='<p class="none">Note quelques séances pour voir ta progression.</p>';return}
  const r=await api('GET','progres/'+id),typ=r.exercice.type,mets=METRIQUES[typ];
  if(!mets.some(m=>m[0]===S.progMet))S.progMet=mets[0][0];
  const m=mets.find(x=>x[0]===S.progMet),pts=r.points.filter(p=>p[S.progMet]!=null).map(p=>({x:p.jour,y:p[S.progMet]}));
  const recKind=REC_DE[S.progMet],recVal=recKind&&r.records[recKind]?r.records[recKind].valeur:null;
  const recs=Object.entries(r.records).map(([k,v])=>`<li><span>${PRN[k]}</span><span class="mut">${esc(fCourt(v.jour))}</span><span class="v">${esc(valRecord(k,v.valeur))}</span></li>`).join('');
  $('#exo-zone').innerHTML=`<div class="tabs">${mets.map(x=>`<button type="button" data-met="${x[0]}" aria-pressed="${x[0]===S.progMet}">${x[1]}</button>`).join('')}</div>
    <div class="chart">${chartLigne(pts,m[2],recVal)}</div>
    ${recs?`<ul class="rec-list" style="margin-top:12px">${recs}</ul>`:''}`;
}
async function pageProgres(){
  const [st,ex]=await Promise.all([api('GET','stats'),api('GET','exercices')]);S.exos=ex.exercices;
  const utiles=ex.exercices.filter(e=>e.nb_series>0).sort((a,b)=>b.nb_series-a.nb_series);
  if(!utiles.some(e=>e.id===S.progId))S.progId=utiles[0]?utiles[0].id:null;
  const t=st.totaux,maxG=Math.max(1,...st.groupes.map(g=>g.series)),m=st.mesures,dernier=m[m.length-1],premier=m[0];
  $('#main').innerHTML=`<div class="page"><h1>Progrès</h1>
    <p class="sentence">${t.seances?`${t.seances} séance${t.seances>1?'s':''} depuis le ${esc(fCourt(t.depuis))}, ${t.series} séries, ${vol(t.volume)} soulevées en ${NUM1.format(t.minutes/60)} h.`:'Dès ta première séance, tes courbes et tes records apparaîtront ici.'}</p>
    <div class="grid2" style="margin-top:20px"><section class="card panel"><h3>Volume par semaine</h3><p class="mut" style="margin:-6px 0 8px;font-size:14px">Chiffre au-dessus de la barre : nombre de séances. Barre jaune : cette semaine.</p><div class="chart">${chartVolume(st.semaines)}</div></section>
    <section class="card panel"><h3>Records récents</h3>${st.records_recents.length?`<ul class="rec-list">${grouperRecords(st.records_recents).slice(0,6).map(r=>`<li>${plate(r.groupe)}<span><b>${esc(r.nom)}</b><br><span class="mut" style="font-size:13px">${esc(r.kinds.map(k=>PRN[k[0]]).join(', '))}, ${esc(fCourt(r.jour))}</span></span><span class="v">${esc(valRecord(r.kinds[0][0],r.kinds[0][1]))}</span></li>`).join('')}</ul>`:'<p class="none" style="padding:6px 0">Les records apparaissent dès que tu bats ta meilleure performance sur un exercice déjà fait.</p>'}</section></div>
    <h2>Régularité</h2><section class="card panel">${heatmap(st.jours)}</section>
    <h2>Un exercice en détail</h2><section class="card panel"><label class="sr" for="exo-sel">Exercice</label><select class="inp" id="exo-sel" style="max-width:360px">${utiles.map(e=>`<option value="${e.id}"${e.id===S.progId?' selected':''}>${esc(e.nom)}</option>`).join('')||'<option>Aucun exercice utilisé</option>'}</select><div id="exo-zone" style="margin-top:12px"></div></section>
    <div class="grid2"><div><h2>Répartition, 30 derniers jours</h2><section class="card panel">${st.groupes.length?st.groupes.map(g=>`<div class="gbar"><span>${plate(g.groupe)}${GR[g.groupe]}</span><span class="t"><i style="--gc:var(--g-${g.groupe});width:${Math.round(g.series/maxG*100)}%"></i></span><span class="num mut">${g.series} séries</span></div>`).join(''):'<p class="none" style="padding:6px 0">Aucune série ces 30 derniers jours.</p>'}</section></div>
    <div><h2>Poids du corps</h2><section class="card panel"><div class="chart">${m.length>1?chartLigne(m.map(x=>({x:x.jour,y:x.poids})),v=>`${NUM1.format(v)} kg`,null,{W:440,H:220}):`<p class="none" style="padding:6px 0">${m.length?'Une deuxième mesure et la courbe apparaît.':'Note ton poids de temps en temps pour suivre ton évolution.'}</p>`}</div>
      ${dernier?`<p class="mut" style="margin:8px 0 0">${NUM1.format(dernier.poids)} kg le ${esc(fCourt(dernier.jour))}${m.length>1?`, ${dernier.poids-premier.poids>=0?'+':''}${NUM1.format(dernier.poids-premier.poids)} kg depuis le ${esc(fCourt(premier.jour))}`:''}</p>`:''}
      <form id="f-poids" style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap"><input class="inp" type="date" id="mp-jour" value="${today()}" style="width:auto" aria-label="Jour"><input class="inp" id="mp-poids" inputmode="decimal" placeholder="Poids en kg" style="width:130px" aria-label="Poids en kg" required><button class="btn">Enregistrer</button></form></section></div></div></div>`;
  await dessinerExo();
}

/* ---------- exercices ---------- */
async function pageExercices(){
  const r=await api('GET',`exercices${S.archives?'?archives=1':''}`);S.exos=r.exercices;
  const par={};r.exercices.forEach(e=>(par[e.groupe]??=[]).push(e));
  const resume=e=>{const k=e.records;if(k.e1rm)return `1RM estimé ${kg(k.e1rm.valeur)}`;if(k.poids)return `max ${kg(k.poids.valeur)}`;if(k.reps)return `max ${k.reps.valeur} reps`;if(k.duree)return `max ${mmss(k.duree.valeur)}`;if(k.distance)return `max ${NUM.format(k.distance.valeur/1000)} km`;return ''};
  $('#main').innerHTML=`<div class="page"><div class="hero"><div><h1>Exercices</h1><p class="sentence mut" style="margin:6px 0 0">${r.exercices.length} exercices. Un exercice utilisé n’est jamais supprimé, seulement archivé : ton historique reste intact.</p></div>
    <div class="sacts"><button class="btn" data-act="toggle-arch" aria-pressed="${!!S.archives}">Archivés</button><button class="btn primary" data-act="new-exo">${ic('plus')}Nouvel exercice</button></div></div>
    ${Object.keys(GR).filter(g=>par[g]).map(g=>`<section class="egroup"><h2>${plate(g)}${GR[g]}</h2><div class="card">${par[g].map(e=>`<div class="erow"><div><b>${esc(e.nom)}</b>${e.archive?'<span class="badge">Archivé</span>':''}<br><span class="mut">${EQUIP[e.equipement]}, ${TYPES[e.type].toLowerCase()}</span></div><div class="mut num" style="text-align:right">${esc(resume(e))}${e.nb_series?`<br>${e.nb_series} série${e.nb_series>1?'s':''}`:''}</div><button class="iconbtn" data-act="edit-exo" data-id="${e.id}" aria-label="Modifier ${esc(e.nom)}">${ic('edit')}</button></div>`).join('')}</div></section>`).join('')}</div>`;
}

/* ---------- modèles ---------- */
async function pageModeles(){
  const r=await api('GET','modeles');S.mods=r.modeles;
  $('#main').innerHTML=`<div class="page"><div class="hero"><div><h1>Modèles</h1><p class="sentence mut" style="margin:6px 0 0">Tes séances types. Pour en créer un, termine une séance puis choisis « Enregistrer comme modèle ».</p></div></div>
    ${r.modeles.length?r.modeles.map(m=>`<article class="card mcard" data-mid="${m.id}"><h3>${esc(m.nom)}</h3><ul>${m.exercices.map(x=>`<li>${plate(x.groupe)}${esc(x.nom)}, ${x.series} série${x.series>1?'s':''}</li>`).join('')}</ul>
      <div class="sacts"><button class="btn primary" data-act="use-mod" data-id="${m.id}">${ic('bolt')}Démarrer cette séance</button><button class="btn" data-act="ren-mod" data-id="${m.id}">${ic('edit')}Renommer</button><button class="btn danger" data-act="del-mod" data-id="${m.id}">${ic('trash')}Supprimer</button></div></article>`).join(''):'<p class="none">Aucun modèle pour l’instant.</p>'}</div>`;
}

/* ---------- réglages ---------- */
function applyTheme(){document.documentElement.dataset.theme=S.reg.theme==='clair'?'light':S.reg.theme==='sombre'?'dark':'auto'}
function dlgReglages(){
  const o=(v,l)=>`<option value="${v}"${S.reg.theme===v?' selected':''}>${l}</option>`;
  dialog(`<h3>Réglages</h3>
    <label class="field" for="r-th">Thème</label><select class="inp" id="r-th" name="theme">${o('auto','Automatique')}${o('clair','Clair')}${o('sombre','Sombre')}</select>
    <label class="field" for="r-re">Temps de repos par défaut, en secondes</label><input class="inp" id="r-re" name="repos" inputmode="numeric" value="${esc(S.reg.repos)}">
    <label class="field" for="r-ba">Poids de ta barre, en kg</label><input class="inp" id="r-ba" name="barre" inputmode="decimal" value="${esc(S.reg.barre)}">
    <label class="field" for="r-ob">Objectif de séances par semaine</label><input class="inp" id="r-ob" name="objectif" inputmode="numeric" value="${esc(S.reg.objectif)}">
    <div class="dactions"><button type="button" class="btn" data-close>Annuler</button><button class="btn primary">Enregistrer</button></div>`,
  async(f,close)=>{
    for(const k of ['theme','repos','barre','objectif']){if(String(f[k]).replace(',','.')!==String(S.reg[k])){await api('PUT','reglage',{cle:k,valeur:f[k]});S.reg[k]=String(f[k]).replace(',','.')}}
    applyTheme();close();toast('Réglages enregistrés.');if(location.hash.startsWith('#/seances')||!location.hash)route();
  });
}

/* ---------- clics ---------- */
document.addEventListener('click',async e=>{
  const mdrop=$('#drop');if((!e.target.closest('.menu')||e.target.closest('.drop a,.drop button'))&&!mdrop.hidden){setTimeout(()=>{mdrop.hidden=true;$('#b-menu').setAttribute('aria-expanded','false')},0)}
  const t=e.target.closest('[data-act],[data-met]');if(!t)return;
  try{
    if(t.dataset.met){S.progMet=t.dataset.met;await dessinerExo();return}
    const row=t.closest('.row'),bloc=t.closest('.bloc');
    switch(t.dataset.act){
      case 'start':await dlgDemarrer(false);break;
      case 'past':await dlgDemarrer(true);break;
      case 'ok':await toggleOk(row);break;
      case 'warm':{const on=t.getAttribute('aria-pressed')!=='true';await api('PUT','serie/'+row.dataset.sid,{echauffement:on});S.sd=await api('GET','seance/'+S.sd.seance.id);renderSeance();break}
      case 'del-set':await api('DELETE','serie/'+row.dataset.sid);S.sd=await api('GET','seance/'+S.sd.seance.id);renderSeance();break;
      case 'add-set':await ajouterSerie(bloc);break;
      case 'del-exo':{
        const eid=+bloc.dataset.eid,nom=S.sd.exercices.find(x=>x.exercice.id===eid).exercice.nom;
        dialog(`<h3>Retirer « ${esc(nom)} » ?</h3><p>Toutes les séries de cet exercice seront supprimées de cette séance.</p><div class="dactions"><button type="button" class="btn" data-close>Annuler</button><button class="btn danger">Retirer</button></div>`,
          async(f,close)=>{await api('DELETE',`seance/${S.sd.seance.id}/exercice/${eid}`);close();S.sd=await api('GET','seance/'+S.sd.seance.id);renderSeance()});break}
      case 'add-exo':{if(!S.exos)S.exos=(await api('GET','exercices')).exercices;
        choisirExercice(async id=>{try{await api('POST',`seance/${S.sd.seance.id}/exercice`,{exercice_id:id});S.sd=await api('GET','seance/'+S.sd.seance.id);renderSeance();window.scrollTo(0,document.body.scrollHeight)}catch(err){fail(err)}});break}
      case 'finish':await terminer();break;
      case 'save-tpl':dlgModele();break;
      case 'redo':{const r=await api('POST','seance',{copie_id:S.sd.seance.id,nom:S.sd.seance.nom,en_cours:true});location.hash='#/seance/'+r.id;break}
      case 'print':window.print();break;
      case 'del-seance':dialog(`<h3>Supprimer cette séance ?</h3><p>Toutes ses séries seront définitivement supprimées.</p><div class="dactions"><button type="button" class="btn" data-close>Annuler</button><button class="btn danger">Supprimer</button></div>`,
          async(f,close)=>{await api('DELETE','seance/'+S.sd.seance.id);close();S.rest=null;drawRest();toast('Séance supprimée.');location.hash='#/seances'});break;
      case 'plates':dlgPlates(+t.dataset.w);break;
      case 'rest-m':if(S.rest){S.rest.fin-=15000;S.rest.total=Math.max(15,S.rest.total-15);drawRest()}break;
      case 'rest-p':if(S.rest){S.rest.fin+=15000;S.rest.total+=15;drawRest()}break;
      case 'rest-x':S.rest=null;drawRest();break;
      case 'reglages':$('#drop').hidden=true;dlgReglages();break;
      case 'quit':await api('POST','quit');document.body.innerHTML='<div style="max-width:420px;margin:20vh auto;text-align:center;font:400 34px Georgia,serif">Reps est fermé<p style="font:15px sans-serif;color:#777">Tu peux fermer cette fenêtre.</p></div>';window.close();break;
      case 'new-exo':dlgExercice(null,async()=>{await pageExercices()});break;
      case 'edit-exo':dlgExercice(S.exos.find(x=>x.id===+t.dataset.id),async()=>{await pageExercices()});break;
      case 'toggle-arch':S.archives=!S.archives;await pageExercices();break;
      case 'use-mod':{const r=await api('POST','seance',{modele_id:+t.dataset.id,en_cours:true});location.hash='#/seance/'+r.id;break}
      case 'ren-mod':{const m=S.mods.find(x=>x.id===+t.dataset.id);dialog(`<h3>Renommer le modèle</h3><label class="field" for="rm">Nom</label><input class="inp" id="rm" name="nom" required maxlength="80" value="${esc(m.nom)}"><div class="dactions"><button type="button" class="btn" data-close>Annuler</button><button class="btn primary">Enregistrer</button></div>`,
          async(f,close)=>{await api('PUT','modele/'+m.id,{nom:f.nom});close();await pageModeles()});break}
      case 'del-mod':{const m=S.mods.find(x=>x.id===+t.dataset.id);dialog(`<h3>Supprimer « ${esc(m.nom)} » ?</h3><p>Tes séances passées ne sont pas touchées.</p><div class="dactions"><button type="button" class="btn" data-close>Annuler</button><button class="btn danger">Supprimer</button></div>`,
          async(f,close)=>{await api('DELETE','modele/'+m.id);close();await pageModeles();toast('Modèle supprimé.')});break}
    }
  }catch(err){fail(err)}
});
$('#b-menu').addEventListener('click',e=>{e.stopPropagation();const d=$('#drop');d.hidden=!d.hidden;e.currentTarget.setAttribute('aria-expanded',String(!d.hidden))});
document.addEventListener('change',async e=>{
  const i=e.target;
  try{
    if(i.classList.contains('in')){i.classList.remove('bad');await saveRow(i.closest('.row'))}
    else if(i.id==='s-nom'){const v=i.value.trim();if(!v){i.value=S.sd.seance.nom;return}await api('PUT','seance/'+S.sd.seance.id,{nom:v});S.sd.seance.nom=v}
    else if(i.id==='s-jour'){await api('PUT','seance/'+S.sd.seance.id,{jour:i.value});await patchSeance()}
    else if(i.id==='s-note'){await api('PUT','seance/'+S.sd.seance.id,{note:i.value});S.sd.seance.note=i.value}
    else if(i.id==='exo-sel'){S.progId=+i.value;await dessinerExo()}
  }catch(err){fail(err)}
});
document.addEventListener('input',e=>{
  if(e.target.id==='q'){clearTimeout(qTimer);qTimer=setTimeout(()=>{S.q=e.target.value.trim();chercher().catch(fail)},180)}
});
document.addEventListener('submit',async e=>{
  if(e.target.id!=='f-poids')return;e.preventDefault();
  try{await api('POST','mesure',{jour:$('#mp-jour').value,poids:$('#mp-poids').value});toast('Poids enregistré.');await pageProgres()}catch(err){fail(err)}
});
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){const s=$('.scrim');if(s){s.remove();return}const d=$('#drop');if(!d.hidden){d.hidden=true;return}}
  if(e.key==='Enter'&&e.target.classList&&e.target.classList.contains('in')){
    e.preventDefault();const all=$$('#main .in'),i=all.indexOf(e.target);if(all[i+1])all[i+1].focus();else e.target.blur();
  }
});

/* ---------- démarrage ---------- */
async function init(){
  $('#b-menu').innerHTML=ic('dots',20);
  $('#m-csv').innerHTML=ic('down')+'Exporter en CSV (tableur)';$('#m-md').innerHTML=ic('down')+'Exporter en Markdown';$('#m-json').innerHTML=ic('down')+'Exporter en JSON';
  $('#m-save').innerHTML=ic('disk')+'Sauvegarder mes données';$('#m-set').innerHTML=ic('gear')+'Réglages';$('#m-quit').innerHTML=ic('power')+'Quitter';
  try{S.reg=await api('GET','reglages');applyTheme()}catch(e){fail(e)}
  addEventListener('hashchange',route);route();
}
const ping=()=>fetch('/api/ping').catch(()=>{});
ping();setInterval(ping,2000);
addEventListener('pagehide',()=>{navigator.sendBeacon('/api/bye',new Blob(['{}'],{type:'application/json'}))});
init();
})();
</script>
</body>
</html>
'''


# ======================= SERVEUR (API + application) =======================
STATE.update({"db": None, "lock": threading.Lock(), "ping": 0.0, "bye": None, "srv": None, "hotes": set()})


class Fichier:
    def __init__(self, nom, contenu, mime, cache=False):
        self.nom, self.contenu, self.mime, self.cache = nom, contenu, mime, cache


def texte(c, cle, maxi=200, requis=False):
    v = c.get(cle)
    v = "" if v is None else str(v).strip()
    if requis and not v:
        raise ValueError(f"Le champ « {cle} » est obligatoire.")
    if len(v) > maxi:
        raise ValueError(f"Le champ « {cle} » est trop long ({maxi} caractères maximum).")
    return v or None


def jour_iso(v):
    try:
        return date.fromisoformat(str(v)).isoformat()
    except ValueError:
        raise ValueError("Date invalide (format AAAA-MM-JJ).") from None


def nombre(c, cle, mini, maxi, entier=False):
    """Nombre optionnel : accepte « 62,5 », « 62.5 kg », vide -> None."""
    v = c.get(cle)
    if v in (None, ""):
        return None
    try:
        x = float(re.sub(r"\s+", "", str(v)).replace(",", ".").replace("kg", ""))
    except ValueError:
        raise ValueError(f"Valeur invalide pour « {cle} ».") from None
    if not math.isfinite(x) or not mini <= x <= maxi:
        raise ValueError(f"Valeur hors limites pour « {cle} ».")
    return int(round(x)) if entier else round(x, 3)


def booleen(v):
    return 1 if v in (True, 1, "1", "true") else 0


def champs_serie(c, partiel):
    """Champs d'une série présents dans la requête (tous si la création, seulement ceux fournis sinon)."""
    spec = {"reps": lambda: nombre(c, "reps", 0, 1000, True), "poids": lambda: nombre(c, "poids", 0, 2000),
            "duree_s": lambda: nombre(c, "duree_s", 0, 86400, True),
            "distance_m": lambda: nombre(c, "distance_m", 0, 1_000_000),
            "echauffement": lambda: booleen(c.get("echauffement")), "fait": lambda: booleen(c.get("fait"))}
    return {k: f() for k, f in spec.items() if not partiel or k in c}


def champs_exercice(c, partiel=False):
    out = {}
    if not partiel or "nom" in c:
        out["nom"] = texte(c, "nom", 80, True)
    for cle, valides in (("groupe", GROUPES), ("type", TYPES), ("equipement", EQUIPEMENTS)):
        if not partiel or cle in c:
            v = c.get(cle) or ("charge" if cle == "type" else "autre")
            if v not in valides:
                raise ValueError(f"Valeur inconnue pour « {cle} ».")
            out[cle] = v
    if not partiel or "note" in c:
        out["note"] = texte(c, "note", 300)
    if "archive" in c:
        out["archive"] = booleen(c.get("archive"))
    return out


def a_des_series(db, eid):
    return bool(db.execute("SELECT 1 FROM serie WHERE exercice_id=? LIMIT 1", (eid,)).fetchone()
                or db.execute("SELECT 1 FROM modele_exo WHERE exercice_id=? LIMIT 1", (eid,)).fetchone())


def seance_ou_404(db, sid):
    s = db.execute("SELECT * FROM seance WHERE id=?", (sid,)).fetchone()
    if not s:
        raise Erreur(404, "Séance introuvable.")
    return s


def exercice_ou_404(db, eid):
    e = db.execute("SELECT * FROM exercice WHERE id=?", (eid,)).fetchone()
    if not e:
        raise Erreur(404, "Exercice introuvable.")
    return e


def liste_modeles(db):
    out = []
    for m in db.execute("SELECT * FROM modele ORDER BY nom COLLATE NOCASE"):
        exos = [dict(x) for x in db.execute(
            "SELECT me.*, e.nom, e.groupe, e.type FROM modele_exo me JOIN exercice e ON e.id=me.exercice_id "
            "WHERE me.modele_id=? ORDER BY me.ordre", (m["id"],))]
        out.append({"id": m["id"], "nom": m["nom"], "exercices": exos})
    return out


def creer_seance(db, c):
    jour = jour_iso(c.get("jour") or date.today().isoformat())
    live = booleen(c.get("en_cours"))
    if live and db.execute("SELECT 1 FROM seance WHERE en_cours=1").fetchone():
        raise Erreur(409, "Une séance est déjà en cours : termine-la ou reprends-la.")
    modele = copie = None
    if c.get("modele_id"):
        modele = db.execute("SELECT * FROM modele WHERE id=?", (int(c["modele_id"]),)).fetchone()
        if not modele:
            raise Erreur(404, "Modèle introuvable.")
    if c.get("copie_id"):
        copie = seance_ou_404(db, int(c["copie_id"]))
    nom = texte(c, "nom", 80) or (modele["nom"] if modele else copie["nom"] if copie else f"Séance du {jour}")
    fait = 0 if live else 1
    with db:
        sid = db.execute("INSERT INTO seance(nom,jour,en_cours,debut_ts) VALUES(?,?,?,?)",
                         (nom, jour, live, time.time() if live else None)).lastrowid
        if modele:
            for me in db.execute("SELECT * FROM modele_exo WHERE modele_id=? ORDER BY ordre", (modele["id"],)).fetchall():
                series = series_pour_exercice(db, me["exercice_id"], me["series"], dict(me), (jour, sid))
                ajouter_series(db, sid, me["exercice_id"], series, fait)
        elif copie:
            for r in db.execute("SELECT * FROM serie WHERE seance_id=? ORDER BY ordre, id", (copie["id"],)).fetchall():
                ajouter_series(db, sid, r["exercice_id"], [dict(r)], fait)
    return {"id": sid}


def terminer_seance(db, sid, c):
    s = seance_ou_404(db, sid)
    if not s["en_cours"]:
        raise Erreur(409, "Cette séance est déjà terminée.")
    duree = nombre(c, "duree_min", 1, 1440, True)
    if duree is None:
        duree = max(1, round((time.time() - (s["debut_ts"] or time.time())) / 60))
    with db:
        db.execute("DELETE FROM serie WHERE seance_id=? AND (fait=0 OR NOT (reps IS NOT NULL OR duree_s IS NOT NULL "
                   "OR distance_m IS NOT NULL))", (sid,))
        if not db.execute("SELECT 1 FROM serie WHERE seance_id=?", (sid,)).fetchone():
            db.execute("DELETE FROM seance WHERE id=?", (sid,))
            return {"vide": True}
        db.execute("UPDATE seance SET en_cours=0, duree_min=?, debut_ts=NULL WHERE id=?", (duree, sid))
    STATE["v"] += 1
    return {"vide": False, "detail": detail_seance(db, sid)}


def modele_depuis_seance(db, sid, c):
    seance_ou_404(db, sid)
    nom = texte(c, "nom", 80, True)
    rows = db.execute("SELECT r.* FROM serie r WHERE r.seance_id=? AND r.echauffement=0 AND " + VALIDE +
                      " ORDER BY r.ordre, r.id", (sid,)).fetchall()
    if not rows:
        raise Erreur(400, "La séance ne contient encore aucune série.")
    blocs = {}
    for r in rows:
        blocs.setdefault(r["exercice_id"], []).append(r)
    try:
        with db:
            mid = db.execute("INSERT INTO modele(nom) VALUES(?)", (nom,)).lastrowid
            for i, (exo, series) in enumerate(blocs.items()):
                d = series[-1]
                db.execute("INSERT INTO modele_exo(modele_id,exercice_id,ordre,series,reps,poids,duree_s,distance_m) "
                           "VALUES(?,?,?,?,?,?,?,?)", (mid, exo, i, len(series), d["reps"], d["poids"], d["duree_s"], d["distance_m"]))
    except sqlite3.IntegrityError:
        raise Erreur(409, "Un modèle porte déjà ce nom.") from None
    return {"id": mid}


def api(m, chemin, q, c):
    db = STATE["db"]
    p = chemin.strip("/").split("/")
    num = lambda i: len(p) > i and p[i].isdigit()  # noqa: E731
    if m != "GET":
        STATE["v"] += 1  # invalide le cache des records

    # ---- fenêtre
    if p == ["ping"] and m == "GET":
        STATE["ping"], STATE["bye"] = time.time(), None
        return {"ok": True, "app": "reps"}
    if p == ["bye"] and m == "POST":
        STATE["bye"] = time.time()
        return {"ok": True}
    if p == ["quit"] and m == "POST":
        threading.Timer(0.3, arreter).start()
        return {"ok": True}

    # ---- réglages
    if p == ["reglages"] and m == "GET":
        return {k: reglage(db, k) for k in REGLAGES}
    if p == ["reglage"] and m == "PUT":
        cle = texte(c, "cle", 30, True)
        if cle not in REGLAGES:
            raise ValueError("Réglage inconnu.")
        if cle == "theme":
            val = c.get("valeur")
            if val not in ("auto", "clair", "sombre"):
                raise ValueError("Thème inconnu.")
        elif cle == "repos":
            val = nombre(c, "valeur", 10, 900, True)
        elif cle == "barre":
            val = nombre(c, "valeur", 5, 50)
        else:
            val = nombre(c, "valeur", 1, 14, True)
        if val is None:
            raise ValueError("Valeur obligatoire.")
        with db:
            db.execute("INSERT INTO reglage(cle,valeur) VALUES(?,?) ON CONFLICT(cle) DO UPDATE SET valeur=excluded.valeur",
                       (cle, str(val)))
        return {"ok": True}

    # ---- export et sauvegarde
    if p == ["sauvegarde"] and m == "GET":
        return Fichier(f"reps-sauvegarde-{date.today().isoformat()}.db", sauvegarde_octets(db), "application/octet-stream")
    if p == ["export"] and m == "GET":
        fmt = q.get("format") if q.get("format") in ("json", "md") else "csv"
        contenu = {"csv": export_csv, "json": export_json, "md": export_markdown}[fmt](db)
        mime = {"csv": "text/csv", "json": "application/json", "md": "text/markdown"}[fmt]
        return Fichier(f"reps-export.{fmt}", contenu.encode("utf-8"), mime + "; charset=utf-8")

    # ---- exercices
    if p == ["exercices"] and m == "GET":
        return {"exercices": liste_exercices(db, q.get("archives") == "1")}
    if p == ["exercice"] and m == "POST":
        ch = champs_exercice(c)
        try:
            with db:
                cur = db.execute("INSERT INTO exercice(nom,groupe,type,equipement,note) VALUES(?,?,?,?,?)",
                                 (ch["nom"], ch["groupe"], ch["type"], ch["equipement"], ch["note"]))
        except sqlite3.IntegrityError:
            raise Erreur(409, "Un exercice porte déjà ce nom.") from None
        return dict(db.execute("SELECT * FROM exercice WHERE id=?", (cur.lastrowid,)).fetchone())
    if len(p) == 2 and p[0] == "exercice" and num(1):
        eid = int(p[1])
        e = exercice_ou_404(db, eid)
        if m == "PUT":
            ch = champs_exercice(c, partiel=True)
            if "type" in ch and ch["type"] != e["type"] and a_des_series(db, eid):
                raise Erreur(409, "Impossible de changer le type d'un exercice déjà utilisé.")
            if ch:
                try:
                    with db:
                        db.execute("UPDATE exercice SET " + ",".join(f"{k}=?" for k in ch) + " WHERE id=?", (*ch.values(), eid))
                except sqlite3.IntegrityError:
                    raise Erreur(409, "Un exercice porte déjà ce nom.") from None
            return dict(db.execute("SELECT * FROM exercice WHERE id=?", (eid,)).fetchone())
        if m == "DELETE":
            with db:
                if a_des_series(db, eid):  # on garde l'historique : l'exercice est simplement archivé
                    db.execute("UPDATE exercice SET archive=1 WHERE id=?", (eid,))
                    return {"ok": True, "archive": True}
                db.execute("DELETE FROM exercice WHERE id=?", (eid,))
            return {"ok": True, "archive": False}

    # ---- séances
    if p == ["seances"] and m == "GET":
        lim = min(int(q.get("limite", 60)), 200) if q.get("limite", "60").isdigit() else 60
        dec = int(q.get("decalage", 0)) if q.get("decalage", "0").isdigit() else 0
        return liste_seances(db, (q.get("q") or "").strip(), lim, dec)
    if p == ["seance"] and m == "POST":
        return creer_seance(db, c)
    if len(p) >= 2 and p[0] == "seance" and num(1):
        sid = int(p[1])
        if len(p) == 2:
            if m == "GET":
                return detail_seance(db, sid)
            if m == "PUT":
                s = seance_ou_404(db, sid)
                nom = texte(c, "nom", 80, True) if "nom" in c else s["nom"]
                jour = jour_iso(c["jour"]) if "jour" in c else s["jour"]
                note = texte(c, "note", 2000) if "note" in c else s["note"]
                duree = nombre(c, "duree_min", 1, 1440, True) if "duree_min" in c else s["duree_min"]
                with db:
                    db.execute("UPDATE seance SET nom=?,jour=?,note=?,duree_min=? WHERE id=?", (nom, jour, note, duree, sid))
                return {"ok": True}
            if m == "DELETE":
                with db:
                    db.execute("DELETE FROM seance WHERE id=?", (sid,))
                return {"ok": True}
        if p[2:] == ["terminer"] and m == "POST":
            return terminer_seance(db, sid, c)
        if p[2:] == ["modele"] and m == "POST":
            return modele_depuis_seance(db, sid, c)
        if p[2:] == ["exercice"] and m == "POST":
            s = seance_ou_404(db, sid)
            eid = int(c.get("exercice_id") or 0)
            e = exercice_ou_404(db, eid)
            if db.execute("SELECT 1 FROM serie WHERE seance_id=? AND exercice_id=?", (sid, eid)).fetchone():
                raise Erreur(409, "Cet exercice est déjà dans la séance.")
            prec = precedent(db, eid, s["jour"], sid)
            n = nb_series_par_defaut(e["type"], prec["series"] if prec else [])
            with db:
                ajouter_series(db, sid, eid, series_pour_exercice(db, eid, n, None, (s["jour"], sid)), 0 if s["en_cours"] else 1)
            return {"ok": True}
        if len(p) == 4 and p[2] == "exercice" and num(3) and m == "DELETE":
            with db:
                db.execute("DELETE FROM serie WHERE seance_id=? AND exercice_id=?", (sid, int(p[3])))
            return {"ok": True}

    # ---- séries
    if p == ["serie"] and m == "POST":
        sid, eid = int(c.get("seance_id") or 0), int(c.get("exercice_id") or 0)
        seance_ou_404(db, sid)
        exercice_ou_404(db, eid)
        ch = champs_serie(c, partiel=False)
        with db:
            cur = db.execute(
                "INSERT INTO serie(seance_id,exercice_id,ordre,reps,poids,duree_s,distance_m,echauffement,fait) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (sid, eid, prochain_ordre(db, sid), ch["reps"], ch["poids"], ch["duree_s"], ch["distance_m"],
                 ch["echauffement"], ch["fait"]))
        return dict(db.execute("SELECT * FROM serie WHERE id=?", (cur.lastrowid,)).fetchone())
    if len(p) == 2 and p[0] == "serie" and num(1):
        rid = int(p[1])
        if not db.execute("SELECT 1 FROM serie WHERE id=?", (rid,)).fetchone():
            raise Erreur(404, "Série introuvable.")
        if m == "PUT":
            ch = champs_serie(c, partiel=True)
            if ch:
                with db:
                    db.execute("UPDATE serie SET " + ",".join(f"{k}=?" for k in ch) + " WHERE id=?", (*ch.values(), rid))
            return dict(db.execute("SELECT * FROM serie WHERE id=?", (rid,)).fetchone())
        if m == "DELETE":
            with db:
                db.execute("DELETE FROM serie WHERE id=?", (rid,))
            return {"ok": True}

    # ---- modèles
    if p == ["modeles"] and m == "GET":
        return {"modeles": liste_modeles(db)}
    if len(p) == 2 and p[0] == "modele" and num(1):
        mid = int(p[1])
        if m == "PUT":
            nom = texte(c, "nom", 80, True)
            try:
                with db:
                    cur = db.execute("UPDATE modele SET nom=? WHERE id=?", (nom, mid))
            except sqlite3.IntegrityError:
                raise Erreur(409, "Un modèle porte déjà ce nom.") from None
            if not cur.rowcount:
                raise Erreur(404, "Modèle introuvable.")
            return {"ok": True}
        if m == "DELETE":
            with db:
                db.execute("DELETE FROM modele WHERE id=?", (mid,))
            return {"ok": True}

    # ---- progrès et mesures
    if p == ["stats"] and m == "GET":
        st = calcul_stats(db)
        st["jours"] = dict(st["jours"])
        return st
    if len(p) == 2 and p[0] == "progres" and num(1) and m == "GET":
        return progres_exercice(db, int(p[1]))
    if p == ["mesure"] and m == "POST":
        jour = jour_iso(c.get("jour") or date.today().isoformat())
        poids = nombre(c, "poids", 20, 400)
        if poids is None:
            raise ValueError("Le poids est obligatoire.")
        with db:
            db.execute("INSERT INTO mesure(jour,poids) VALUES(?,?) ON CONFLICT(jour) DO UPDATE SET poids=excluded.poids", (jour, poids))
        return {"ok": True}
    if len(p) == 2 and p[0] == "mesure" and num(1) and m == "DELETE":
        with db:
            db.execute("DELETE FROM mesure WHERE id=?", (int(p[1]),))
        return {"ok": True}
    raise Erreur(404, "Introuvable.")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _envoyer(self, code, corps=b"", ctype="application/json; charset=utf-8", headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(corps)))
        extra = dict(headers)
        self.send_header("Cache-Control", extra.pop("Cache-Control", "no-store"))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(corps)

    def _json(self, code, obj):
        self._envoyer(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _verif(self, methode):
        hote = self.headers.get("Host")
        if hote not in STATE["hotes"]:  # protège du DNS rebinding
            self._json(403, {"erreur": "Hôte refusé."})
            return False
        origine = self.headers.get("Origin")
        if origine and urlparse(origine).netloc != hote:  # protège des requêtes d'autres sites
            self._json(403, {"erreur": "Origine refusée."})
            return False
        if methode in ("POST", "PUT") and "application/json" not in (self.headers.get("Content-Type") or ""):
            self._json(415, {"erreur": "Format non pris en charge."})
            return False
        return True

    def _traiter(self, methode):
        if not self._verif(methode):
            return
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if methode == "GET" and u.path == "/":
                return self._envoyer(200, APP_HTML.encode("utf-8"), "text/html; charset=utf-8")
            if not u.path.startswith("/api/"):
                raise Erreur(404, "Page introuvable.")
            corps = {}
            if methode in ("POST", "PUT"):
                n = int(self.headers.get("Content-Length") or 0)
                if n > 2_000_000:
                    raise Erreur(413, "Données trop volumineuses.")
                corps = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(corps, dict):
                    raise ValueError("Données invalides.")
            with STATE["lock"]:
                res = api(methode, u.path[5:], q, corps)
        except Erreur as e:
            return self._json(e.code, {"erreur": str(e)})
        except (ValueError, KeyError) as e:
            return self._json(400, {"erreur": str(e) or "Données invalides."})
        except sqlite3.IntegrityError:
            return self._json(409, {"erreur": "Cette donnée existe déjà."})
        except Exception as e:  # noqa: BLE001
            print("Erreur interne :", repr(e), file=sys.stderr)
            return self._json(500, {"erreur": "Erreur interne."})
        if isinstance(res, Fichier):
            h = []
            if res.nom:
                h.append(("Content-Disposition", f'attachment; filename="{res.nom}"'))
            if res.cache:  # une photo ne change jamais : le navigateur peut la garder
                h.append(("Cache-Control", "private, max-age=31536000, immutable"))
            return self._envoyer(200, res.contenu, res.mime, h)
        self._json(200, res)

    def do_GET(self):
        self._traiter("GET")

    def do_POST(self):
        self._traiter("POST")

    def do_PUT(self):
        self._traiter("PUT")

    def do_DELETE(self):
        self._traiter("DELETE")


def arreter():
    srv = STATE["srv"]
    if srv:
        threading.Thread(target=srv.shutdown, daemon=True).start()


def reps_actif(port):
    """Vrai si Reps tourne déjà sur ce port."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1) as r:
            return json.loads(r.read()).get("app") == "reps"
    except Exception:  # noqa: BLE001
        return False


def ouvrir_serveur(port, souple):
    """Retourne (serveur, port) ; (None, port) si Reps tourne déjà sur ce port."""
    essais = [port, 0] if souple else [port]
    for p in essais:
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
        except OSError:
            if not souple:
                raise
            if p == port and reps_actif(port):
                return None, port
            continue
        port = srv.server_address[1]
        STATE["srv"] = srv
        STATE["hotes"] = {f"127.0.0.1:{port}", f"localhost:{port}"}
        return srv, port
    raise OSError("Aucun port disponible.")


def trouver_navigateur():
    """Cherche Edge / Chrome / Chromium / Brave : ils savent ouvrir une page comme une application."""
    cands = []
    if sys.platform.startswith("win"):
        for cle in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData"):
            base = os.environ.get(cle)
            if base:
                cands += [Path(base) / "Microsoft/Edge/Application/msedge.exe",
                          Path(base) / "Google/Chrome/Application/chrome.exe",
                          Path(base) / "BraveSoftware/Brave-Browser/Application/brave.exe"]
    elif sys.platform == "darwin":
        cands = [Path(f"/Applications/{n}.app/Contents/MacOS/{n}")
                 for n in ("Microsoft Edge", "Google Chrome", "Chromium", "Brave Browser")]
    for nom in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser", "brave-browser"):
        w = shutil.which(nom)
        if w:
            cands.append(Path(w))
    return next((str(c) for c in cands if c.exists()), None)


def lancer_fenetre(url):
    exe = trouver_navigateur()
    if exe:
        profil = Path.home() / ".reps_profil"
        try:
            subprocess.Popen(
                [exe, f"--app={url}", "--window-size=1280,860", f"--user-data-dir={profil}",
                 "--no-first-run", "--no-default-browser-check"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            pass
    webbrowser.open(url)  # repli : navigateur par défaut


def surveiller():
    """Arrête le serveur quand la fenêtre est fermée (signal 'bye') ou n'a jamais été ouverte."""
    debut = time.time()
    while True:
        time.sleep(1)
        now, bye, ping = time.time(), STATE["bye"], STATE["ping"]
        if bye and now - bye > 8:
            break
        if not ping and now - debut > 90:
            break
        if ping and now - ping > 180:
            break
    arreter()


def cmd_gui(db, a):
    STATE["db"] = db
    srv, port = ouvrir_serveur(8766, souple=True)
    url = f"http://127.0.0.1:{port}"
    lancer_fenetre(url)
    if srv is None:  # une fenêtre Reps existe déjà : on s'y rattache
        return
    threading.Thread(target=surveiller, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_web(db, a):
    STATE["db"] = db
    srv, port = ouvrir_serveur(a.port, souple=False)
    url = f"http://127.0.0.1:{port}"
    if not a.no_browser:
        threading.Timer(0.8, webbrowser.open, [url]).start()
    print(f"Reps -> {url}  (Ctrl+C pour arreter)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# ======================= POINT D'ENTRÉE =======================
def main():
    # Application sans console (.exe) : stdout/stderr n'existent pas, on les neutralise
    for nom in ("stdout", "stderr"):
        if getattr(sys, nom) is None:
            setattr(sys, nom, open(os.devnull, "w"))
    args = build_parser().parse_args(sys.argv[1:] or ["gui"])
    db = connect()
    try:
        args.fn(db, args)
    finally:
        db.close()


if __name__ == "__main__":
    main()
