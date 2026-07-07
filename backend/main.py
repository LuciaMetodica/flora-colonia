"""Flora Colonia — photo upload backend.

Receives a photo from the ficha web app, stores it in the kit's shared Drive
folder and writes the public URL into the "Foto (URL pública)" column of the
"Datos de plantas" sheet.

Runs on Cloud Run (project: digitana). Credentials come from SA_KEY_B64 env
(claude-drive-reader service account, editor on the shared drive).
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
NAME_COL = "E"          # Nombre común
PHOTO_COL = "W"         # Foto (URL pública)
PARENT_FOLDER_ID = "10xKXZUnroVtfQJJ89MoGCeP7aJeSkT7V"  # "Datos fichas"
PHOTOS_FOLDER_NAME = "Fotos fichas"
MAX_BYTES = 8 * 1024 * 1024
ALLOWED_MIMES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
ALLOWED_ORIGINS = {
    "https://luciametodica.github.io",
    "http://localhost:8899",
}
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]
UPLOAD_PIN = os.environ.get("UPLOAD_PIN", "")  # if set, requests must include it

app = Flask(__name__)

_services = {}


def _load_credentials():
    # 1) explicit key passed as base64 env (local/dev)
    if os.environ.get("SA_KEY_B64"):
        info = json.loads(base64.b64decode(os.environ["SA_KEY_B64"]))
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    # 2) key file path (local)
    if os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY"):
        return service_account.Credentials.from_service_account_file(
            os.environ["GOOGLE_SERVICE_ACCOUNT_KEY"], scopes=SCOPES
        )
    # 3) Application Default Credentials = Cloud Run runtime service account
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


@app.route("/upload", methods=["POST", "OPTIONS"])
def upload():
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(silent=True) or {}
    if UPLOAD_PIN and str(data.get("pin", "")) != UPLOAD_PIN:
        return jsonify({"error": "PIN incorrecto"}), 403
    nombre = str(data.get("nombre", "")).strip()
    data_url = str(data.get("imagen", ""))

    if not nombre or not data_url.startswith("data:"):
        return jsonify({"error": "Falta nombre o imagen"}), 400

    m = re.match(r"data:([\w/+.-]+);base64,(.+)$", data_url, re.S)
    if not m or m.group(1) not in ALLOWED_MIMES:
        return jsonify({"error": "Formato de imagen no soportado (usar JPG, PNG o WebP)"}), 400
    mime = m.group(1)
    try:
        raw = base64.b64decode(m.group(2))
    except (binascii.Error, ValueError):
        return jsonify({"error": "Imagen corrupta"}), 400
    if len(raw) > MAX_BYTES:
        return jsonify({"error": "Imagen demasiado grande (máx 8 MB)"}), 413

    drive, sheets = get_services()

    # 1. Find the plant row by common name
    col = sheets.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_TAB}'!{NAME_COL}:{NAME_COL}",
    ).execute().get("values", [])
    row_num = None
    for i, row in enumerate(col):
        if i == 0:
            continue  # header
        if row and norm(row[0]) == norm(nombre):
            row_num = i + 1
            break
    if row_num is None:
        return jsonify({"error": f"Planta '{nombre}' no encontrada en el Sheet"}), 404

    # 2. Ensure "Fotos fichas" subfolder exists
    q = (
        f"name = '{PHOTOS_FOLDER_NAME}' and '{PARENT_FOLDER_ID}' in parents "
        "and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    found = drive.files().list(
        q=q, supportsAllDrives=True, includeItemsFromAllDrives=True,
        fields="files(id)",
    ).execute().get("files", [])
    if found:
        folder_id = found[0]["id"]
    else:
        folder_id = drive.files().create(
            body={
                "name": PHOTOS_FOLDER_NAME,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [PARENT_FOLDER_ID],
            },
            supportsAllDrives=True, fields="id",
        ).execute()["id"]

    # 3. Upload (replace if a photo for this plant already exists)
    safe_name = re.sub(r"[^\w\sáéíóúüñÁÉÍÓÚÜÑ-]", "", nombre).strip().replace(" ", "_")
    filename = safe_name + ALLOWED_MIMES[mime]
    media = MediaIoBaseUpload(io.BytesIO(raw), mimetype=mime, resumable=False)

    existing = drive.files().list(
        q=f"name contains '{safe_name}' and '{folder_id}' in parents and trashed = false",
        supportsAllDrives=True, includeItemsFromAllDrives=True,
        fields="files(id, name)",
    ).execute().get("files", [])
    existing = [f for f in existing if os.path.splitext(f["name"])[0] == safe_name]

    if existing:
        file_id = existing[0]["id"]
        drive.files().update(
            fileId=file_id, media_body=media, body={"name": filename},
            supportsAllDrives=True,
        ).execute()
    else:
        file_id = drive.files().create(
            body={"name": filename, "parents": [folder_id]},
            media_body=media, supportsAllDrives=True, fields="id",
        ).execute()["id"]

    # 4. Public read permission + embeddable URL
    drive.permissions().create(
        fileId=file_id, body={"type": "anyone", "role": "reader"},
        supportsAllDrives=True,
    ).execute()
    public_url = f"https://lh3.googleusercontent.com/d/{file_id}"

    # 5. Write URL into the sheet
    sheets.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_TAB}'!{PHOTO_COL}{row_num}",
        valueInputOption="RAW",
        body={"values": [[public_url]]},
    ).execute()

    return jsonify({"ok": True, "url": public_url, "fila": row_num})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
