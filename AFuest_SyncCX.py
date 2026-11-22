#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AFuest_SyncCX.py

Überträgt Einsätze (Missionen) aus der CommandX-Datenbank (CommandXWeb) an
Alamos FE2 und kennzeichnet sie anschließend in CommandX als "in Arbeit"
(MissionState = 11).

Ersetzt afuest_sync.py (alte Datenbank DBNAST06).

Übertragen werden nur Missionen, die
  - mit der Befehlsstelle "AFüSt Illertissen" geteilt sind
    (Eintrag in MissionShareConfiguration),
  - im MissionState "Geteilt" sind,
  - IsActive = 1 und IsLocked = 0 haben.

Das Script läuft ohne Benutzereingriff (z.B. per Aufgabenplanung / cron).
Alle Aktionen werden an der Konsole und in einer Log-Datei protokolliert.

Aufruf:
    python AFuest_SyncCX.py              # normaler Lauf
    python AFuest_SyncCX.py --dry-run    # nur lesen + loggen, kein POST, kein UPDATE
    python AFuest_SyncCX.py --diagnose   # zeigt Befehlsstellen-Treffer und
                                         # MissionState-Verteilung der geteilten Missionen

Libs to be installed via pip:
    pip install pyodbc requests
    pip install tzdata  # nur für Python < 3.9, sonst ZoneInfo in stdlib

"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from zoneinfo import ZoneInfo

import pyodbc
import requests

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

# CommandX Datenbank (Werte können per Umgebungsvariable überschrieben werden)
DB_SERVER = os.environ.get("CX_DB_SERVER", r"192.168.112.8,1433")  # fixed port
#DB_SERVER = os.environ.get("CX_DB_SERVER", r"192.168.112.8")
DB_NAME = os.environ.get("CX_DB_NAME", "CommandXWeb")
DB_USER = os.environ.get("CX_DB_USER", "CommandXUser")
DB_PASSWORD = os.environ.get("CX_DB_PASSWORD", "FFI!cx112")
DB_DRIVER = os.environ.get("CX_DB_DRIVER", "ODBC Driver 17 for SQL Server")
DB_TIMEOUT = 15  # Sekunden

# Befehlsstelle AFüSt Illertissen
# Ist die Id bekannt, hier eintragen (GUID) -> dann entfällt die Namenssuche.
AFUEST_DEPARTMENT_ID = os.environ.get("CX_AFUEST_DEPARTMENT_ID", "8F940D4D-B6A8-EF11-B3D4-0050569B60FE")
# Sonst wird in DepartmentStation.Name / Abbreviation per LIKE gesucht.
# Es muss genau EINE Befehlsstelle gefunden werden, sonst bricht das Script ab.
AFUEST_DEPARTMENT_NAME_LIKE = "%AF%St%Illertissen%"

# Tabelle MissionShareConfiguration: Spaltennamen
SHARE_COL_MISSION = "MissionId"
SHARE_COL_DEPARTMENT = "DepartmentStationId"

# MissionState-Werte
# ACHTUNG: Zahlenwert für "Geteilt" bitte mit --diagnose an der DB verifizieren!
MISSION_STATE_GETEILT = 10
MISSION_STATE_IN_ARBEIT = 11

# Zeitzone, in der CommandX die Datumswerte speichert (für den Alamos-Timestamp)
CX_TIMEZONE = ZoneInfo("Europe/Berlin")

# Alamos FE2
ALAMOS_URL = "http://192.168.112.50:83/rest/external/http/alarm/v2"
ALAMOS_TIMEOUT = 15  # Sekunden
ALAMOS_SENDER = "KEZ"  # wie bisher in afuest_sync.py (Alamos-Eingang ggf. darauf konfiguriert)
ALAMOS_AUTHORIZATION = "112p_Adapter"
ALAMOS_UNITS = [{"address": "4837d5a705444237bc933d46f22c272c"}]

# Logging
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_FILE = os.path.join(LOG_DIR, "AFuest_SyncCX.log")
LOG_BACKUP_DAYS = 30

log = logging.getLogger("AFuest_SyncCX")


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)

    logfile = TimedRotatingFileHandler(LOG_FILE, when="midnight", backupCount=LOG_BACKUP_DAYS,
                                       encoding="utf-8")
    logfile.setFormatter(fmt)

    log.setLevel(logging.INFO)
    log.addHandler(console)
    log.addHandler(logfile)


def connect_db():
    cstr = (f"DRIVER={{{DB_DRIVER}}};SERVER={DB_SERVER};DATABASE={DB_NAME};"
            f"UID={DB_USER};PWD={DB_PASSWORD}")
    return pyodbc.connect(cstr, timeout=DB_TIMEOUT, autocommit=False)


def clean(value):
    """None / 'NULL' / Leerstring -> '' ; sonst getrimmter String."""
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s.upper() == "NULL" else s


def to_iso_timestamp(dt):
    """DB-Datum -> ISO-8601 mit korrektem Offset (Sommer-/Winterzeit)."""
    if dt is None:
        dt = datetime.now(CX_TIMEZONE)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=CX_TIMEZONE)
    return dt.replace(microsecond=0).isoformat()


def to_float(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f != 0.0 else None


# ---------------------------------------------------------------------------
# Datenbankzugriffe
# ---------------------------------------------------------------------------

def find_department_ids(cursor):
    """Liefert die Ids der passenden Befehlsstellen (Name/Abkürzung)."""
    cursor.execute(
        "SELECT [Id], [Name], [Abbreviation] FROM [dbo].[DepartmentStation] "
        "WHERE [DeleteDate] IS NULL AND ([Name] LIKE ? OR [Abbreviation] LIKE ?)",
        AFUEST_DEPARTMENT_NAME_LIKE, AFUEST_DEPARTMENT_NAME_LIKE)
    return cursor.fetchall()


def resolve_department_id(cursor):
    if AFUEST_DEPARTMENT_ID:
        log.info("Befehlsstelle AFüSt Illertissen (konfiguriert): %s", AFUEST_DEPARTMENT_ID)
        return AFUEST_DEPARTMENT_ID

    rows = find_department_ids(cursor)
    if len(rows) != 1:
        log.error("Befehlsstelle AFüSt Illertissen nicht eindeutig gefunden (%d Treffer für '%s'):",
                  len(rows), AFUEST_DEPARTMENT_NAME_LIKE)
        for r in rows:
            log.error("    %s  %s  (%s)", r.Id, r.Name, r.Abbreviation)
        log.error("Bitte AFUEST_DEPARTMENT_ID oder AFUEST_DEPARTMENT_NAME_LIKE im Script anpassen.")
        return None
    log.info("Befehlsstelle gefunden: %s (%s) Id=%s", rows[0].Name, rows[0].Abbreviation, rows[0].Id)
    return str(rows[0].Id)


MISSION_QUERY = f"""
SELECT m.[Id], m.[DateCreated], m.[StartDate], m.[ExternalId], m.[ExternalYear],
       m.[YearId], m.[DispatchNumber], m.[AlarmDefinition], m.[AlarmDetail],
       m.[AlarmKeyword], m.[Description], m.[ReporterContactPerson], m.[Contact],
       m.[SituationReported], m.[AdditionalInformation], m.[ObjectName],
       m.[Street], m.[StreetNumber], m.[City], m.[PostCode], m.[Municipality],
       m.[MapLongitude], m.[MapLatitude], m.[MapAddress], m.[RIC], m.[MissionState]
FROM [dbo].[Mission] m
WHERE m.[MissionState] = ?
  AND m.[IsActive] = 1
  AND m.[IsLocked] = 0
  AND m.[DeleteDate] IS NULL
  AND EXISTS (SELECT 1 FROM [dbo].[MissionShareConfiguration] s
              WHERE s.[{SHARE_COL_MISSION}] = m.[Id]
                AND s.[{SHARE_COL_DEPARTMENT}] = ?)
ORDER BY m.[StartDate]
"""


def read_missions(cursor, department_id):
    cursor.execute(MISSION_QUERY, MISSION_STATE_GETEILT, department_id)
    return cursor.fetchall()


def mark_in_arbeit(conn, mission_id):
    """Setzt MissionState = 11. Nur wenn die Mission noch 'Geteilt' und nicht gesperrt ist."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE [dbo].[Mission] SET [MissionState] = ?, [DateModified] = SYSDATETIME() "
            "WHERE [Id] = ? AND [MissionState] = ? AND [IsLocked] = 0",
            MISSION_STATE_IN_ARBEIT, mission_id, MISSION_STATE_GETEILT)
        count = cursor.rowcount
        conn.commit()
        return count
    except pyodbc.Error:
        conn.rollback()
        raise
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# Alamos
# ---------------------------------------------------------------------------

def build_alamos_object(m):
    external_id = clean(m.DispatchNumber)
    keyword = clean(m.AlarmKeyword) 
    keyword_desc = clean(m.AlarmDetail) or clean(m.AlarmDefinition)
    keyword_misc = clean(m.AlarmDefinition) 

    start = m.StartDate or m.DateCreated
    message = clean(m.Description)

    location = {
        "street": clean(m.Street),
        "house": clean(m.StreetNumber),
        "postalCode": clean(m.PostCode),
        "city": clean(m.City),
        "city_abbr": clean(m.Municipality) or clean(m.City),
    }
    lon, lat = to_float(m.MapLongitude), to_float(m.MapLatitude)
    if lon is not None and lat is not None:
        location["coordinate"] = [lon, lat]  # Alamos erwartet [Länge, Breite]

    return {
        "type": "ALARM",
        "timestamp": to_iso_timestamp(start),
        "sender": ALAMOS_SENDER,
        "authorization": ALAMOS_AUTHORIZATION,
        "data": {
            "externalId": external_id,
            "keyword": keyword,
            "keyword_description": keyword_desc,
            "keyword_misc": keyword_misc,
            "message": [message],
            "location": location,
            "caller": {"name": clean(m.ReporterContactPerson), "contact": clean(m.Contact)},
            "units": ALAMOS_UNITS,
        },
    }


def send_to_alamos(obj):
    """True bei Erfolg, sonst False (Fehler wird geloggt)."""
    try:
        resp = requests.post(ALAMOS_URL, json=obj, timeout=ALAMOS_TIMEOUT,
                             headers={"Content-Type": "application/json", "accept-charset": "UTF-8"})
    except requests.RequestException as exc:
        log.error("    Alamos nicht erreichbar: %s", exc)
        return False

    try:
        ok = resp.ok and resp.json().get("status") == "OK"
    except ValueError:
        ok = False
    if not ok:
        log.error("    Alamos-Antwort fehlerhaft: HTTP %s - %s", resp.status_code, resp.text[:500])
    return ok


# ---------------------------------------------------------------------------
# Ablauf
# ---------------------------------------------------------------------------

def diagnose(cursor):
    log.info("=== Diagnose ===")
    rows = find_department_ids(cursor)
    log.info("Befehlsstellen-Treffer für '%s': %d", AFUEST_DEPARTMENT_NAME_LIKE, len(rows))
    for r in rows:
        log.info("    %s  %s  (%s)", r.Id, r.Name, r.Abbreviation)

    dep_id = AFUEST_DEPARTMENT_ID or (str(rows[0].Id) if len(rows) == 1 else None)
    if not dep_id:
        log.info("Keine eindeutige Befehlsstelle -> MissionState-Auswertung übersprungen.")
        return
    cursor.execute(
        f"SELECT m.[MissionState], m.[IsActive], m.[IsLocked], COUNT(*) AS Anzahl "
        f"FROM [dbo].[Mission] m JOIN [dbo].[MissionShareConfiguration] s "
        f"  ON s.[{SHARE_COL_MISSION}] = m.[Id] "
        f"WHERE s.[{SHARE_COL_DEPARTMENT}] = ? "
        f"GROUP BY m.[MissionState], m.[IsActive], m.[IsLocked] "
        f"ORDER BY m.[MissionState]", dep_id)
    log.info("Mit AFüSt geteilte Missionen nach MissionState / IsActive / IsLocked:")
    for r in cursor.fetchall():
        log.info("    MissionState=%s  IsActive=%s  IsLocked=%s  Anzahl=%s",
                 r.MissionState, r.IsActive, r.IsLocked, r.Anzahl)
    log.info("Konfiguriert: Geteilt=%s, In Arbeit=%s", MISSION_STATE_GETEILT, MISSION_STATE_IN_ARBEIT)


def run(dry_run=False, diag=False):
    log.info("=" * 70)
    log.info("Sync CommandX -> Alamos gestartet%s", " (DRY-RUN)" if dry_run else "")

    try:
        conn = connect_db()
    except pyodbc.Error as exc:
        log.error("Verbindung zur CommandX-Datenbank fehlgeschlagen: %s", exc)
        return 2

    try:
        cursor = conn.cursor()
        if diag:
            diagnose(cursor)
            return 0

        department_id = resolve_department_id(cursor)
        if not department_id:
            return 3

        missions = read_missions(cursor, department_id)
        cursor.close()
        log.info("%d Einsatz/Einsätze zur Übertragung gefunden.", len(missions))

        ok_count = err_count = 0
        for m in missions:
            obj = build_alamos_object(m)
            d = obj["data"]
            loc = d["location"]
            log.info("Einsatz %s | %s | %s %s, %s %s | Koord: %s",
                     d["externalId"], d["keyword"], loc["street"], loc["house"],
                     loc["postalCode"], loc["city"], loc.get("coordinate", "keine"))

            if dry_run:
                log.info("    DRY-RUN: keine Übertragung, kein Statuswechsel. Payload: %s", obj)
                continue

            if not send_to_alamos(obj):
                log.error("    Übertragung an Alamos fehlgeschlagen - Einsatz bleibt 'Geteilt' "
                          "und wird beim nächsten Lauf erneut versucht.")
                err_count += 1
                continue
            log.info("    An Alamos übertragen.")

            try:
                updated = mark_in_arbeit(conn, m.Id)
            except pyodbc.Error as exc:
                log.error("    Statuswechsel in CommandX fehlgeschlagen (Mission %s): %s", m.Id, exc)
                err_count += 1
                continue
            if updated == 1:
                log.info("    In CommandX als 'in Arbeit' (MissionState=%s) markiert.",
                         MISSION_STATE_IN_ARBEIT)
                ok_count += 1
            else:
                log.warning("    Mission %s wurde zwischenzeitlich geändert/gesperrt - "
                            "Status in CommandX NICHT geändert.", m.Id)
                err_count += 1

        log.info("Sync beendet: %d erfolgreich, %d Fehler.", ok_count, err_count)
        return 1 if err_count else 0
    except pyodbc.Error as exc:
        log.error("Datenbankfehler: %s", exc)
        return 2
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Sync CommandX -> Alamos FE2 (AFüSt Illertissen)")
    parser.add_argument("--dry-run", action="store_true",
                        help="nur lesen und protokollieren, nichts senden oder ändern")
    parser.add_argument("--diagnose", action="store_true",
                        help="Befehlsstelle und MissionState-Werte der geteilten Missionen anzeigen")
    args = parser.parse_args()

    setup_logging()
    try:
        rc = run(dry_run=args.dry_run, diag=args.diagnose)
    except Exception:  # unerwartete Fehler trotzdem ins Log
        log.exception("Unerwarteter Fehler")
        rc = 99
    sys.exit(rc)


if __name__ == "__main__":
    main()
