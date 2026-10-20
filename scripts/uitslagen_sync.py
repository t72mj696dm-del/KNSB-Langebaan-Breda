"""Leest uitslag-PDF's (Sportity/KNSB-wedstrijdprogramma) en schrijft data/uitslagen.json.

Bronnen:
  1. PDF's in de map pdfs/ van deze repo (handmatig geüpload)
  2. PDF's in een openbare Google Drive-map (als DRIVE_API_KEY en DRIVE_FOLDER_ID gezet zijn)

Elke PDF wordt maar één keer verwerkt; welke al gedaan zijn staat in data/uitslagen.json onder "bronnen".
"""
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "uitslagen.json"
PDF_DIR = ROOT / "pdfs"

MAANDEN = {m: i + 1 for i, m in enumerate(
    "januari februari maart april mei juni juli augustus september oktober november december".split())}

KOP_AFSTAND = re.compile(r"^\s*\d+\.\s+Uitslag\s+(\d+)\s*meter", re.I)
RIJ = re.compile(
    r"^\s*(?:(\d+)\s+)?\d+\s+(.+?)\s+([DH][A-Z0-9]{1,3})\s+\d+\s+[IO]\s+((?:\d+:)?\d{1,2}[.,]\d{2})(?:\s+(.*))?$")
DATUM = re.compile(r"^\s*(\d{1,2})\s+([a-z]+)\s+(\d{4})\s*$", re.I)


def lees_pdf(data: bytes, bron: str):
    """Geeft (wedstrijd, lijst met tijden) terug."""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        paginas = [p.extract_text() or "" for p in pdf.pages]

    kop = [r.strip() for r in paginas[0].splitlines() if r.strip()][:3]
    if len(kop) < 3:
        raise ValueError("kop van de PDF niet herkend")
    naam, baan_regel, datum_regel = kop
    baan = baan_regel.split(" - ")[-1].strip()  # "Kunstijsbaan Breda - Breda" -> "Breda"
    m = DATUM.match(datum_regel)
    if not m or m.group(2).lower() not in MAANDEN:
        raise ValueError(f"datum niet herkend: {datum_regel!r}")
    datum = f"{m.group(3)}-{MAANDEN[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"

    tijden = []
    for tekst in paginas:
        regels = tekst.splitlines()
        afstand = None
        for r in regels:
            k = KOP_AFSTAND.match(r)
            if k:
                afstand = int(k.group(1))
                continue
            if "Rituitslag" in r:  # ritpagina's bevatten dezelfde tijden nog een keer
                afstand = None
                continue
            if afstand is None:
                continue
            rij = RIJ.match(r)
            if rij:
                info = (rij.group(5) or "").split()
                tijden.append({
                    "n": rij.group(2).strip(),
                    "c": rij.group(3),
                    "a": afstand,
                    "t": rij.group(4).replace(",", "."),
                    "pr": "PR" in info,
                })
    if not tijden:
        raise ValueError("geen tijden gevonden")
    wedstrijd = {"w": naam, "b": baan, "d": datum, "bron": bron}
    return wedstrijd, tijden


def drive_bestanden(api_key: str, map_id: str):
    q = f"'{map_id}' in parents and mimeType='application/pdf' and trashed=false"
    url = "https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode(
        {"q": q, "key": api_key, "fields": "files(id,name,modifiedTime)", "pageSize": 1000})
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r).get("files", [])


def drive_download(api_key: str, file_id: str) -> bytes:
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&key={urllib.parse.quote(api_key)}"
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def main():
    db = json.loads(DATA.read_text("utf-8")) if DATA.exists() else {"wedstrijden": [], "bronnen": {}}
    bronnen = db.setdefault("bronnen", {})
    nieuw, fouten = 0, []

    def verwerk(sleutel, versie, data_fn, label):
        nonlocal nieuw
        if bronnen.get(sleutel) == versie:
            return
        try:
            wedstrijd, tijden = lees_pdf(data_fn(), label)
        except Exception as e:  # één kapotte PDF mag de rest niet tegenhouden
            fouten.append(f"{label}: {e}")
            return
        # zelfde wedstrijd (naam + baan + datum) opnieuw geüpload? dan vervangen
        db["wedstrijden"] = [x for x in db["wedstrijden"]
                             if (x["w"], x["b"], x["d"]) != (wedstrijd["w"], wedstrijd["b"], wedstrijd["d"])]
        wedstrijd["tijden"] = tijden
        db["wedstrijden"].append(wedstrijd)
        bronnen[sleutel] = versie
        nieuw += 1
        print(f"✓ {label}: {wedstrijd['w']} ({wedstrijd['b']}, {wedstrijd['d']}) – {len(tijden)} tijden")

    if PDF_DIR.exists():
        for f in sorted(PDF_DIR.glob("*.pdf")):
            verwerk("repo:" + f.name, str(f.stat().st_size), f.read_bytes, f.name)

    key, map_id = os.environ.get("DRIVE_API_KEY"), os.environ.get("DRIVE_FOLDER_ID")
    if key and map_id:
        for f in drive_bestanden(key, map_id):
            verwerk("drive:" + f["id"], f["modifiedTime"], lambda f=f: drive_download(key, f["id"]), f["name"])
    else:
        print("Geen DRIVE_API_KEY/DRIVE_FOLDER_ID: Drive overgeslagen")

    db["wedstrijden"].sort(key=lambda x: (x["d"], x["b"], x["w"]))
    if nieuw:
        DATA.parent.mkdir(exist_ok=True)
        DATA.write_text(json.dumps(db, ensure_ascii=False, indent=1) + "\n", "utf-8")
    print(f"{nieuw} nieuwe uitslag(en) verwerkt")
    for f in fouten:
        print("✗", f, file=sys.stderr)
    sys.exit(1 if fouten and not nieuw else 0)


if __name__ == "__main__":
    if len(sys.argv) > 1:  # testen: python uitslagen_sync.py bestand.pdf
        w, t = lees_pdf(Path(sys.argv[1]).read_bytes(), Path(sys.argv[1]).name)
        print(json.dumps({**w, "aantal": len(t), "voorbeeld": t[:3]}, ensure_ascii=False, indent=1))
    else:
        main()
