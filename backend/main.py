"""Flora Colonia — backend for the ficha web app.

Endpoints (all JSON, CORS-restricted to the GitHub Pages origin):
  POST /upload  {nombre, imagen, slot}  -> stores a plant photo in Drive and
                writes its public URL into "Foto (URL pública)" (slot 1) or
                "Foto 2 (URL pública)" (slot 2) of the "Fichas" sheet.
  POST /estado  {nombre, estado}        -> writes the approval state
                (Pendiente | Aprobada) into the "Estado" column.
  POST /ficha   {nombre, imagen}        -> stores the downloaded A4 ficha PNG in
                Drive and writes its URL into "Ficha PNG (URL)".

Runs on Cloud Run (project: digitana). Credentials come from the runtime
service account (ADC) or SA_KEY_B64 / GOOGLE_SERVICE_ACCOUNT_KEY locally.
Photo uploads no longer require a PIN.
"""

import base64
import binascii
import io
import json
import os
import re
import unicodedata

import google.auth
from flask import Flask, jsonify, request
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

SPREADSHEET_ID = "1M6G-gH41FSEtoIOvp1TYQdNJd2m8QzymY5ZRp_eqb74"
SHEET_TAB = "Fichas"
NAME_COL = "E"           # Nombre común
PHOTO_COL = "V"          # Foto (URL pública)
PHOTO2_COL = "AB"        # Foto 2 (URL pública)
ESTADO_COL = "AC"        # Estado (Pendiente | Aprobada)
FICHA_PNG_COL = "AD"     # Ficha PNG (URL)
PARENT_FOLDER_ID = "10xKXZUnroVtfQJJ89MoGCeP7aJeSkT7V"  # "Datos fichas"
PHOTOS_FOLDER_NAME = "Fotos fichas"
FICHAS_FOLDER_NAME = "Fichas PNG"
MAX_BYTES = 8 * 1024 * 1024
ALLOWED_MIMES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
ESTADOS_VALIDOS = {"Pendiente", "Aprobada"}
ALLOWED_ORIGINS = {
    "https://luciametodica.github.io",
    "http://localhost:8899",
}
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

app = Flask(__name__)

_services = {}


def _load_credentials():
    if os.environ.get("SA_KEY_B64"):
        info = json.loads(base64.b64decode(os.environ["SA_KEY_B64"]))
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    if os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY"):
        return service_account.Credentials.from_service_account_file(
            os.environ["GOOGLE_SERVICE_ACCOUNT_KEY"], scopes=SCOPES
        )
    creds, _ = google.auth.default(scopes=SCOPES)
    return creds


def get_services():
    if not _services:
        creds = _load_credentials()
        _services["drive"] = build("drive", "v3", credentials=creds, cache_discovery=False)
        _services["sheets"] = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return _services["drive"], _services["sheets"]


def norm(s):
    s = unicodedata.normalize("NFD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


@app.after_request
def add_cors(resp):
    origin = request.headers.get("Origin", "")
    if origin in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "flora-upload"})


def _find_row(sheets, nombre):
    """Return the 1-based row number of the plant, or None."""
    col = sheets.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_TAB}'!{NAME_COL}:{NAME_COL}",
    ).execute().get("values", [])
    for i, row in enumerate(col):
        if i == 0:
            continue  # header
        if row and norm(row[0]) == norm(nombre):
            return i + 1
    return None


def _ensure_folder(drive, name):
    q = (
        f"name = '{name}' and '{PARENT_FOLDER_ID}' in parents "
        "and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    found = drive.files().list(
        q=q, supportsAllDrives=True, includeItemsFromAllDrives=True, fields="files(id)",
    ).execute().get("files", [])
    if found:
        return found[0]["id"]
    return drive.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder",
              "parents": [PARENT_FOLDER_ID]},
        supportsAllDrives=True, fields="id",
    ).execute()["id"]


def _decode_image(data_url):
    """Return (raw_bytes, ext) or (None, error_message)."""
    if not data_url.startswith("data:"):
        return None, "Falta la imagen"
    m = re.match(r"data:([\w/+.-]+);base64,(.+)$", data_url, re.S)
    if not m or m.group(1) not in ALLOWED_MIMES:
        return None, "Formato no soportado (usar JPG, PNG o WebP)"
    mime = m.group(1)
    try:
        raw = base64.b64decode(m.group(2))
    except (binascii.Error, ValueError):
        return None, "Imagen corrupta"
    if len(raw) > MAX_BYTES:
        return None, "Imagen demasiado grande (máx 8 MB)"
    return (raw, mime), None


def _store_image(drive, folder_id, base_name, raw, mime):
    """Upload (replacing any existing file with the same base name); return public URL."""
    safe = re.sub(r"[^\w\sáéíóúüñÁÉÍÓÚÜÑ-]", "", base_name).strip().replace(" ", "_")
    filename = safe + ALLOWED_MIMES[mime]
    media = MediaIoBaseUpload(io.BytesIO(raw), mimetype=mime, resumable=False)
    existing = drive.files().list(
        q=f"name contains '{safe}' and '{folder_id}' in parents and trashed = false",
        supportsAllDrives=True, includeItemsFromAllDrives=True, fields="files(id, name)",
    ).execute().get("files", [])
    existing = [f for f in existing if os.path.splitext(f["name"])[0] == safe]
    if existing:
        file_id = existing[0]["id"]
        drive.files().update(fileId=file_id, media_body=media, body={"name": filename},
                             supportsAllDrives=True).execute()
    else:
        file_id = drive.files().create(
            body={"name": filename, "parents": [folder_id]},
            media_body=media, supportsAllDrives=True, fields="id",
        ).execute()["id"]
    drive.permissions().create(
        fileId=file_id, body={"type": "anyone", "role": "reader"}, supportsAllDrives=True,
    ).execute()
    return f"https://lh3.googleusercontent.com/d/{file_id}"


def _write_cell(sheets, col, row_num, value):
    sheets.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID, range=f"'{SHEET_TAB}'!{col}{row_num}",
        valueInputOption="RAW", body={"values": [[value]]},
    ).execute()


@app.route("/upload", methods=["POST", "OPTIONS"])
def upload():
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(silent=True) or {}
    nombre = str(data.get("nombre", "")).strip()
    slot = str(data.get("slot", "1")).strip()
    if not nombre:
        return jsonify({"error": "Falta el nombre de la planta"}), 400
    if slot not in ("1", "2"):
        return jsonify({"error": "slot inválido"}), 400

    decoded, err = _decode_image(str(data.get("imagen", "")))
    if err:
        return jsonify({"error": err}), 400
    raw, mime = decoded

    drive, sheets = get_services()
    row_num = _find_row(sheets, nombre)
    if row_num is None:
        return jsonify({"error": f"Planta '{nombre}' no encontrada en el Sheet"}), 404

    folder_id = _ensure_folder(drive, PHOTOS_FOLDER_NAME)
    base_name = nombre if slot == "1" else f"{nombre}_2"
    url = _store_image(drive, folder_id, base_name, raw, mime)
    _write_cell(sheets, PHOTO_COL if slot == "1" else PHOTO2_COL, row_num, url)
    return jsonify({"ok": True, "url": url, "slot": slot, "fila": row_num})


@app.route("/estado", methods=["POST", "OPTIONS"])
def estado():
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(silent=True) or {}
    nombre = str(data.get("nombre", "")).strip()
    est = str(data.get("estado", "")).strip().capitalize()
    if not nombre:
        return jsonify({"error": "Falta el nombre de la planta"}), 400
    if est not in ESTADOS_VALIDOS:
        return jsonify({"error": "Estado inválido (Pendiente | Aprobada)"}), 400

    _, sheets = get_services()
    row_num = _find_row(sheets, nombre)
    if row_num is None:
        return jsonify({"error": f"Planta '{nombre}' no encontrada en el Sheet"}), 404
    _write_cell(sheets, ESTADO_COL, row_num, est)
    return jsonify({"ok": True, "estado": est, "fila": row_num})


@app.route("/ficha", methods=["POST", "OPTIONS"])
def ficha():
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(silent=True) or {}
    nombre = str(data.get("nombre", "")).strip()
    if not nombre:
        return jsonify({"error": "Falta el nombre de la planta"}), 400

    decoded, err = _decode_image(str(data.get("imagen", "")))
    if err:
        return jsonify({"error": err}), 400
    raw, mime = decoded

    drive, sheets = get_services()
    row_num = _find_row(sheets, nombre)
    if row_num is None:
        return jsonify({"error": f"Planta '{nombre}' no encontrada en el Sheet"}), 404

    folder_id = _ensure_folder(drive, FICHAS_FOLDER_NAME)
    url = _store_image(drive, folder_id, f"Ficha_{nombre}", raw, mime)
    _write_cell(sheets, FICHA_PNG_COL, row_num, url)
    return jsonify({"ok": True, "url": url, "fila": row_num})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
