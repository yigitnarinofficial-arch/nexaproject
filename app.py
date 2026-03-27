"""
app.py — Render.com Web Sunucusu (Tek Dosya)
─────────────────────────────────────────────
• drive_to_html mantığı bu dosyaya dahil edildi
• Flask ile sunum.html ve sunum_assets/ klasörünü serve eder
• APScheduler ile her gece saat 04:00 (İstanbul) Drive'ı kontrol eder
• Değişiklik varsa HTML'yi baştan üretir
• Uygulama ilk başladığında sunum.html yoksa otomatik üretir
"""

import os, io, json, base64, textwrap, html, re, hashlib, pickle, shutil
import threading
import logging
from pathlib import Path
from datetime import datetime
from collections import OrderedDict

from flask import Flask, send_file, send_from_directory, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

# Google API
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# Dosya işleme
import pandas as pd
from docx import Document as DocxDocument
import PyPDF2
from PIL import Image

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Ayarlar (env var'dan okunur)
# ─────────────────────────────────────────────
FOLDER_ID        = os.environ.get("DRIVE_FOLDER_ID", "")
SCOPES           = ["https://www.googleapis.com/auth/drive.readonly"]
OUTPUT_FILE      = os.environ.get("OUTPUT_FILE", "sunum.html")
ASSETS_DIR       = os.environ.get("ASSETS_DIR", "sunum_assets")
PROJE_ADI        = os.environ.get("PROJE_ADI", "Proje Sunumu")
PROJE_ALT_BASLIK = os.environ.get("PROJE_ALT_BASLIK", "Google Drive Arşivi")
CACHE_DIR        = os.environ.get("CACHE_DIR", ".drive_cache")
MANIFEST_FILE    = os.environ.get("MANIFEST_FILE", ".drive_manifest")

# ─────────────────────────────────────────────
# Flask uygulaması
# ─────────────────────────────────────────────
app = Flask(__name__)
_build_lock   = threading.Lock()
_build_status = {"running": False, "last_run": None, "last_result": "Henüz çalışmadı"}


# ════════════════════════════════════════════════════════════
#  GOOGLE DRIVE
# ════════════════════════════════════════════════════════════
def get_service():
    sa_json_str = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sa_json_str:
        raise EnvironmentError("GOOGLE_SERVICE_ACCOUNT_JSON ortam değişkeni bulunamadı!")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(sa_json_str), scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds)


def list_files(service, folder_id, _depth=0, _path=""):
    results, page_token = [], None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            spaces="drive",
            fields="nextPageToken, files(id,name,mimeType,size,modifiedTime)",
            pageToken=page_token
        ).execute()
        for item in resp.get("files", []):
            if item.get("mimeType") == "application/vnd.google-apps.folder":
                sub_path = (_path + " / " if _path else "") + item["name"]
                results.extend(list_files(service, item["id"], _depth + 1, sub_path))
            else:
                item["folder_path"] = _path
                results.append(item)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


# ════════════════════════════════════════════════════════════
#  İNDİRME & ÖNBELLEK
# ════════════════════════════════════════════════════════════
GAPPS_EXPORT = {
    "application/vnd.google-apps.presentation": ("application/pdf", "pdf"),
    "application/vnd.google-apps.document":     ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
    "application/vnd.google-apps.spreadsheet":  ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
    "application/vnd.google-apps.drawing":      ("image/png", "png"),
}

def download_bytes(service, file_id, mime_type=""):
    def _dl(req):
        buf = io.BytesIO()
        dl  = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = dl.next_chunk()
        return buf.getvalue()

    if mime_type in GAPPS_EXPORT:
        export_mime, _ = GAPPS_EXPORT[mime_type]
        return _dl(service.files().export_media(fileId=file_id, mimeType=export_mime)), export_mime

    try:
        return _dl(service.files().get_media(fileId=file_id)), mime_type
    except Exception as e:
        if "fileNotDownloadable" not in str(e) and "403" not in str(e):
            raise

    for em in ["application/pdf",
               "application/vnd.openxmlformats-officedocument.presentationml.presentation",
               "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
               "image/png"]:
        try:
            return _dl(service.files().export_media(fileId=file_id, mimeType=em)), em
        except Exception:
            continue

    raise RuntimeError(f"Dosya indirilemedi: file_id={file_id}")


def _cache_key(file_id, modified_time):
    return hashlib.sha1(f"{file_id}_{modified_time}".encode()).hexdigest()

def cache_get(file_id, modified_time):
    p = Path(CACHE_DIR) / _cache_key(file_id, modified_time)
    if p.exists():
        try:
            with open(p, "rb") as f:
                return pickle.load(f)
        except Exception:
            p.unlink(missing_ok=True)
    return None

def cache_set(file_id, modified_time, data, mime):
    Path(CACHE_DIR).mkdir(exist_ok=True)
    p = Path(CACHE_DIR) / _cache_key(file_id, modified_time)
    with open(p, "wb") as f:
        pickle.dump((data, mime), f)

def download_cached(service, file_id, mime_type, modified_time):
    hit = cache_get(file_id, modified_time)
    if hit:
        return hit
    data, real_mime = download_bytes(service, file_id, mime_type)
    cache_set(file_id, modified_time, data, real_mime)
    return data, real_mime


# ════════════════════════════════════════════════════════════
#  DOSYA TİPİ & YARDIMCILAR
# ════════════════════════════════════════════════════════════
def ext(name):
    return Path(name).suffix.lower().lstrip(".")

def size_fmt(b):
    if b is None: return "—"
    b = int(b)
    for unit in ("B","KB","MB","GB"):
        if b < 1024: return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"

def slugify(s):
    s = s.upper()
    tr = str.maketrans("ÇĞİÖŞÜçğıöşü", "CGIOSUcgiosu")
    s = s.translate(tr)
    return re.sub(r"[^A-Z0-9]+", "_", s).strip("_")

def file_type_key(name, mime):
    e = ext(name)
    if "google-apps.presentation" in mime: return "pdf"
    if "google-apps.document"     in mime: return "word"
    if "google-apps.spreadsheet"  in mime: return "table"
    if "google-apps.drawing"      in mime: return "image"
    if "pdf"            in mime: return "pdf"
    if "presentationml" in mime: return "pdf"
    if "wordprocessing" in mime: return "word"
    if "spreadsheetml"  in mime: return "table"
    if e in ("jpg","jpeg","png","gif","webp","svg") or mime.startswith("image/"): return "image"
    if e in ("xlsx","xls","csv"): return "table"
    if e == "docx": return "word"
    if e == "json": return "json"
    if e == "pdf":  return "pdf"
    return "other"


# ════════════════════════════════════════════════════════════
#  ASSET DOSYALARI
# ════════════════════════════════════════════════════════════
def _prepare_image(data, size):
    img = Image.open(io.BytesIO(data))
    img.thumbnail(size, Image.LANCZOS)
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, (4, 4, 12))
        paste_img = img.convert("RGBA") if img.mode != "RGBA" else img
        bg.paste(paste_img, mask=paste_img.split()[3])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    return img

def make_thumb(data, mime, uid, size=(600, 400)):
    try:
        Path(ASSETS_DIR).mkdir(exist_ok=True)
        p = Path(ASSETS_DIR) / f"{uid}_t.jpg"
        if p.exists(): return f"{ASSETS_DIR}/{uid}_t.jpg"
        _prepare_image(data, size).save(str(p), format="JPEG", quality=82, optimize=True)
        return f"{ASSETS_DIR}/{uid}_t.jpg"
    except Exception as e:
        log.warning(f"Thumbnail hatası ({uid}): {e}")
        return ""

def make_large(data, mime, uid, size=(1600, 1200)):
    try:
        Path(ASSETS_DIR).mkdir(exist_ok=True)
        p = Path(ASSETS_DIR) / f"{uid}_l.jpg"
        if p.exists(): return f"{ASSETS_DIR}/{uid}_l.jpg"
        _prepare_image(data, size).save(str(p), format="JPEG", quality=90, optimize=True)
        return f"{ASSETS_DIR}/{uid}_l.jpg"
    except Exception as e:
        log.warning(f"Large görsel hatası ({uid}): {e}")
        return ""

def save_video(data, uid):
    try:
        Path(ASSETS_DIR).mkdir(exist_ok=True)
        p = Path(ASSETS_DIR) / f"{uid}.mp4"
        p.write_bytes(data)
        return f"{ASSETS_DIR}/{uid}.mp4"
    except Exception:
        return ""

def save_pdf(data, uid):
    try:
        Path(ASSETS_DIR).mkdir(exist_ok=True)
        p = Path(ASSETS_DIR) / f"{uid}.pdf"
        p.write_bytes(data)
        return f"{ASSETS_DIR}/{uid}.pdf"
    except Exception:
        return ""


# ════════════════════════════════════════════════════════════
#  DOSYA İŞLEYİCİLER
# ════════════════════════════════════════════════════════════
def process_pdf(data, name, label="PDF"):
    try:
        pages = len(PyPDF2.PdfReader(io.BytesIO(data)).pages)
    except Exception:
        pages = 0
    uid      = hashlib.md5(data[:128]).hexdigest()[:12]
    pdf_path = save_pdf(data, uid)
    lbl      = "🎞 Sunum" if label == "Slides" else "📄 PDF"
    pg_label = f"{pages} Sayfa" if pages else "PDF"
    dl_name  = html.escape(name)
    return f"""<div class="pdf-card" onclick="openPdfModal('{pdf_path}','{dl_name}',{pages})">
  <div class="pdf-card-preview">
    <canvas class="pdf-thumb-canvas" data-pdf="{pdf_path}" data-uid="pthumb-{uid}"></canvas>
    <div class="pdf-card-overlay">
      <div class="pdf-open-icon">
        <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><path d="M14 2v6h6"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><line x1="10" y1="9" x2="8" y2="9"/></svg>
      </div>
      <span class="pdf-open-label">Görüntüle</span>
    </div>
  </div>
  <div class="pdf-card-info">
    <div class="pdf-card-tag">{lbl}</div>
    <div class="pdf-card-name">{dl_name}</div>
    <div class="pdf-card-meta">{pg_label}</div>
  </div>
</div>"""


# ════════════════════════════════════════════════════════════
#  MANIFEST
# ════════════════════════════════════════════════════════════
def compute_manifest(files):
    parts = sorted(f"{f['id']}:{f.get('modifiedTime','')}" for f in files)
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()

def load_manifest():
    p = Path(MANIFEST_FILE)
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""

def save_manifest(h):
    Path(MANIFEST_FILE).write_text(h, encoding="utf-8")


# ════════════════════════════════════════════════════════════
#  HTML ŞABLONu
# ════════════════════════════════════════════════════════════
def _html_head(title):
    return f"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{html.escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;1,300;1,400&family=Plus+Jakarta+Sans:wght@300;400;500;600&family=DM+Mono:wght@300;400&display=swap"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
<style>
"""

def _html_css():
    return """
/* ─── TOKENS ──────────────────────────────────────────── */
:root {
  --ink:      #020209;
  --ink2:     #08080F;
  --ink3:     #0D0D1A;
  --ink4:     #131320;
  --ink5:     #1A1A2E;
  --gold:     #C8A55A;
  --gold2:    #E0C07A;
  --gold3:    #F0D9A0;
  --gold-dim: rgba(200,165,90,.08);
  --gold-glow:rgba(200,165,90,.18);
  --plat:     #EDE8E0;
  --plat2:    #A8A4B0;
  --plat3:    #6A677A;
  --border:   rgba(200,165,90,.12);
  --border2:  rgba(200,165,90,.28);
  --border3:  rgba(200,165,90,.45);
  --r:        12px;
  --r2:       20px;
  --serif:    "Cormorant Garamond", Georgia, serif;
  --sans:     "Plus Jakarta Sans", system-ui, sans-serif;
  --mono:     "DM Mono", monospace;
  --ease:     cubic-bezier(.4,0,.2,1);
  --ease-out: cubic-bezier(0,.8,.2,1);
  --t:        .3s;
  --t2:       .55s;
}
*,*::before,*::after { box-sizing:border-box; margin:0; padding:0 }
html { scroll-behavior:smooth; -webkit-font-smoothing:antialiased }
body {
  background:var(--ink);
  color:var(--plat);
  font-family:var(--sans);
  font-size:15px;
  line-height:1.7;
  min-height:100vh;
  overflow-x:hidden;
}
#loader {
  position:fixed; inset:0; z-index:9999;
  background:var(--ink);
  display:flex; align-items:center; justify-content:center;
  flex-direction:column; gap:32px;
  transition:opacity .8s, visibility .8s;
}
#loader.hidden { opacity:0; visibility:hidden; pointer-events:none }
.loader-logo { font-family:var(--serif); font-size:clamp(2rem,5vw,3.5rem); font-weight:300; letter-spacing:.12em; color:var(--gold); opacity:0; animation:loaderFadeIn 1s .3s forwards }
.loader-line  { width:80px; height:1px; background:linear-gradient(90deg,transparent,var(--gold),transparent); animation:loaderExpand 1.2s .5s forwards; transform:scaleX(0); transform-origin:center }
.loader-sub   { font-family:var(--mono); font-size:.65rem; letter-spacing:.3em; text-transform:uppercase; color:var(--plat3); opacity:0; animation:loaderFadeIn .8s .9s forwards }
#cursor       { position:fixed; z-index:9998; pointer-events:none; width:8px; height:8px; background:var(--gold); border-radius:50%; transform:translate(-50%,-50%); mix-blend-mode:screen }
#cursor-ring  { position:fixed; z-index:9997; pointer-events:none; width:36px; height:36px; border:1px solid rgba(200,165,90,.5); border-radius:50%; transform:translate(-50%,-50%); transition:width .3s,height .3s }
@media(hover:none){#cursor,#cursor-ring{display:none}}
#progress-bar { position:fixed; top:0; left:0; z-index:9996; height:2px; background:linear-gradient(90deg,var(--gold),var(--gold2)); width:0%; transition:width .1s linear; box-shadow:0 0 12px var(--gold-glow) }
.ambient-orb  { position:fixed; pointer-events:none; z-index:0; border-radius:50%; filter:blur(80px); opacity:.06; animation:orbFloat 20s ease-in-out infinite }
.orb1 { width:600px; height:600px; background:var(--gold); top:-200px; right:-200px }
.orb2 { width:400px; height:400px; background:#6040C0; bottom:-100px; left:-100px; animation-delay:-7s }
.orb3 { width:300px; height:300px; background:var(--gold2); bottom:40%; right:10%; animation-delay:-13s }
.site-header { position:relative; z-index:10; padding:80px 72px 64px; background:linear-gradient(170deg,#0A0A18 0%,#06060E 60%,var(--ink) 100%); border-bottom:1px solid var(--border); overflow:hidden }
.site-header::before { content:""; position:absolute; inset:0; background:radial-gradient(ellipse 80% 60% at 80% 50%,rgba(200,165,90,.06),transparent); pointer-events:none }
.header-inner { max-width:1400px; margin:0 auto; position:relative; z-index:1 }
.header-eyebrow { font-family:var(--mono); font-size:.65rem; letter-spacing:.35em; text-transform:uppercase; color:var(--gold); margin-bottom:24px; opacity:0; animation:slideUp .8s 1.4s forwards }
.site-header h1 { font-family:var(--serif); font-size:clamp(3rem,7vw,6rem); font-weight:300; letter-spacing:-.02em; line-height:1.05; color:var(--plat); opacity:0; animation:slideUp .9s 1.6s forwards }
.site-header h1 em { font-style:italic; color:var(--gold2) }
.header-sub  { margin-top:20px; color:var(--plat2); font-size:1rem; font-weight:300; letter-spacing:.04em; opacity:0; animation:slideUp .8s 1.8s forwards }
.header-line { width:120px; height:1px; background:linear-gradient(90deg,var(--gold),transparent); margin:32px 0; opacity:0; animation:slideUp .8s 2s forwards }
.header-meta { display:flex; gap:20px; flex-wrap:wrap; align-items:center; opacity:0; animation:slideUp .8s 2.1s forwards }
.hm-pill { font-family:var(--mono); font-size:.65rem; letter-spacing:.12em; color:var(--plat3); padding:7px 20px; border:1px solid var(--border); border-radius:999px; background:rgba(255,255,255,.02) }
.hm-pill span { color:var(--gold2) }
#homepage { position:relative; z-index:1 }
.home-intro { max-width:1400px; margin:0 auto; padding:64px 72px 0; display:flex; align-items:baseline; justify-content:space-between; gap:24px; flex-wrap:wrap }
.home-intro-title { font-family:var(--serif); font-size:1.8rem; font-weight:300; color:var(--plat2) }
.home-intro-title strong { color:var(--plat); font-weight:400 }
.home-count { font-family:var(--mono); font-size:.65rem; letter-spacing:.2em; color:var(--gold); text-transform:uppercase }
.home-grid { max-width:1400px; margin:0 auto; padding:40px 72px 100px; display:grid; grid-template-columns:repeat(auto-fill,minmax(380px,1fr)); gap:24px }
.proj-card { background:var(--ink2); border:1px solid var(--border); border-radius:var(--r2); overflow:hidden; cursor:pointer; position:relative; opacity:0; transform:translateY(32px); transition:border-color var(--t2),box-shadow var(--t2) }
.proj-card.visible { opacity:1; transform:translateY(0) }
.proj-card:hover { border-color:var(--border2); box-shadow:0 32px 80px rgba(0,0,0,.7),0 0 0 1px var(--border2),inset 0 1px 0 rgba(255,255,255,.04) }
.proj-cover-wrap { position:relative; overflow:hidden; aspect-ratio:16/9; background:var(--ink3) }
.proj-cover,.proj-cover-video { width:100%; height:100%; object-fit:cover; display:block; transition:transform .8s }
.proj-card:hover .proj-cover,.proj-card:hover .proj-cover-video { transform:scale(1.08) }
.proj-cover-placeholder { width:100%; height:100%; display:flex; align-items:center; justify-content:center; font-size:2.5rem; background:linear-gradient(135deg,var(--ink3),var(--ink4)) }
.proj-cover-gradient { position:absolute; inset:0; background:linear-gradient(to top,rgba(2,2,9,.92) 0%,rgba(2,2,9,.3) 50%,transparent 100%) }
.proj-cover-number { position:absolute; top:20px; left:20px; font-family:var(--mono); font-size:.6rem; letter-spacing:.25em; color:rgba(200,165,90,.6); background:rgba(2,2,9,.5); backdrop-filter:blur(8px); border:1px solid var(--border); border-radius:999px; padding:4px 12px }
.proj-cover-cta { position:absolute; bottom:20px; right:20px; display:flex; align-items:center; gap:8px; font-family:var(--mono); font-size:.6rem; letter-spacing:.18em; text-transform:uppercase; color:var(--gold2); background:rgba(2,2,9,.6); backdrop-filter:blur(12px); border:1px solid var(--border2); border-radius:999px; padding:6px 16px; opacity:0; transform:translateY(8px); transition:opacity var(--t),transform var(--t) }
.proj-card:hover .proj-cover-cta { opacity:1; transform:translateY(0) }
.proj-info { padding:24px 28px 28px }
.proj-eyebrow { font-family:var(--mono); font-size:.6rem; letter-spacing:.3em; text-transform:uppercase; color:var(--gold); margin-bottom:10px }
.proj-name { font-family:var(--serif); font-size:1.5rem; font-weight:400; line-height:1.2; color:var(--plat); margin-bottom:16px; word-break:break-word }
.proj-badges { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:14px }
.badge { font-family:var(--mono); font-size:.6rem; letter-spacing:.08em; color:var(--plat3); padding:4px 12px; border:1px solid var(--border); border-radius:999px; background:rgba(255,255,255,.02) }
.badge-video { border-color:rgba(200,165,90,.3); color:var(--gold2) }
.proj-divider { height:1px; background:var(--border); margin-bottom:14px }
.proj-total { font-family:var(--mono); font-size:.6rem; letter-spacing:.12em; color:var(--plat3); display:flex; align-items:center; gap:6px }
.proj-total::before { content:""; display:inline-block; width:6px; height:1px; background:var(--gold) }
.project-page { position:relative; z-index:1; min-height:100vh }
.proj-hero { position:relative; height:100vh; min-height:600px; overflow:hidden; background:var(--ink) }
.hero-slides { position:absolute; inset:0 }
.hero-slide { position:absolute; inset:0; opacity:0; transition:opacity 1.2s }
.hero-slide.active { opacity:1 }
.hero-slide img { width:100%; height:100%; object-fit:cover; animation:kenBurns 12s forwards }
.hero-slide video { width:100%; height:100%; object-fit:cover }
.hero-gradient { position:absolute; inset:0; background:linear-gradient(to top,rgba(2,2,9,.95) 0%,rgba(2,2,9,.4) 50%,rgba(2,2,9,.2) 100%),linear-gradient(to right,rgba(2,2,9,.3) 0%,transparent 60%); z-index:1 }
.hero-content { position:absolute; bottom:0; left:0; right:0; z-index:2; padding:0 72px 72px; display:flex; align-items:flex-end; justify-content:space-between; gap:32px; flex-wrap:wrap }
.back-btn { display:inline-flex; align-items:center; gap:8px; font-family:var(--mono); font-size:.65rem; letter-spacing:.15em; text-transform:uppercase; color:var(--plat2); background:rgba(2,2,9,.5); backdrop-filter:blur(12px); border:1px solid var(--border); border-radius:999px; padding:8px 20px; cursor:pointer; transition:all var(--t); margin-bottom:24px }
.back-btn:hover { color:var(--gold); border-color:var(--border2) }
.hero-eyebrow { font-family:var(--mono); font-size:.62rem; letter-spacing:.3em; text-transform:uppercase; color:var(--gold); margin-bottom:16px }
.hero-title { font-family:var(--serif); font-size:clamp(2.5rem,6vw,5rem); font-weight:300; letter-spacing:-.02em; line-height:1.05; color:var(--plat); margin-bottom:24px }
.hero-badges { display:flex; flex-wrap:wrap; gap:8px }
.hero-slide-nav { display:flex; gap:10px; align-items:flex-end; padding-bottom:4px }
.hero-dot { width:6px; height:6px; border-radius:50%; background:rgba(200,165,90,.3); cursor:pointer; transition:background var(--t),width var(--t) }
.hero-dot.active { width:24px; border-radius:999px; background:var(--gold) }
.hero-counter { font-family:var(--mono); font-size:.6rem; letter-spacing:.15em; color:var(--plat3); white-space:nowrap }
.tab-bar { position:sticky; top:0; z-index:50; padding:0 24px 0 16px; background:rgba(2,2,9,.95); backdrop-filter:blur(28px); border-bottom:1px solid var(--border); display:flex; align-items:center; gap:0; overflow-x:auto; box-shadow:0 4px 24px rgba(0,0,0,.4) }
.tab-bar::-webkit-scrollbar { height:0 }
.tab-bar-back { flex-shrink:0; display:inline-flex; align-items:center; gap:7px; font-family:var(--mono); font-size:.62rem; letter-spacing:.14em; text-transform:uppercase; color:var(--gold); background:rgba(200,165,90,.08); border:1px solid var(--border2); border-radius:999px; padding:7px 16px; cursor:pointer; transition:all var(--t); white-space:nowrap; margin-right:16px }
.tab-bar-back:hover { background:rgba(200,165,90,.18); border-color:var(--border3) }
.tab-bar-divider { flex-shrink:0; width:1px; height:24px; background:var(--border); margin-right:4px }
.tab-btn { font-family:var(--mono); font-size:.68rem; letter-spacing:.12em; text-transform:uppercase; color:var(--plat3); background:transparent; border:none; padding:20px 24px; cursor:pointer; border-bottom:2px solid transparent; white-space:nowrap; transition:all var(--t) }
.tab-btn:hover { color:var(--plat2) }
.tab-btn.active { color:var(--gold2); border-bottom-color:var(--gold) }
.tab-count { font-size:.55rem; color:var(--plat3); margin-left:6px; background:var(--ink4); padding:2px 7px; border-radius:999px }
.tab-btn.active .tab-count { background:rgba(200,165,90,.15); color:var(--gold) }
.tab-content { padding:56px 72px 100px; max-width:1600px; margin:0 auto }
.gallery-header { display:flex; align-items:baseline; justify-content:space-between; margin-bottom:32px; flex-wrap:wrap; gap:16px }
.gallery-title { font-family:var(--serif); font-size:2rem; font-weight:300; color:var(--plat2) }
.gallery-title strong { color:var(--plat) }
.gallery-filters { display:flex; gap:8px; flex-wrap:wrap }
.gal-filter { font-family:var(--mono); font-size:.6rem; letter-spacing:.12em; text-transform:uppercase; color:var(--plat3); background:transparent; border:1px solid var(--border); border-radius:999px; padding:5px 14px; cursor:pointer; transition:all var(--t) }
.gal-filter.active { color:var(--gold); border-color:var(--gold); background:var(--gold-dim) }
.gallery { columns:3; column-gap:16px }
@media(max-width:1100px){.gallery{columns:2}}
@media(max-width:600px) {.gallery{columns:1}}
.gal-item { break-inside:avoid; position:relative; overflow:hidden; border-radius:var(--r); background:var(--ink3); border:1px solid var(--border); cursor:zoom-in; margin-bottom:16px; opacity:0; transform:translateY(20px); transition:opacity .6s,transform .6s,border-color var(--t),box-shadow var(--t) }
.gal-item.visible { opacity:1; transform:translateY(0) }
.gal-item:hover { border-color:var(--border2); box-shadow:0 20px 60px rgba(0,0,0,.6) }
.gal-item img { width:100%; display:block; transition:transform .6s }
.gal-item:hover img { transform:scale(1.04) }
.gal-caption { position:absolute; bottom:0; left:0; right:0; background:linear-gradient(transparent,rgba(2,2,9,.88)); padding:32px 14px 12px; opacity:0; transition:opacity var(--t) }
.gal-item:hover .gal-caption { opacity:1 }
.gal-cap-name { font-family:var(--mono); font-size:.58rem; letter-spacing:.08em; color:rgba(237,232,224,.8); word-break:break-word }
.gal-cap-sub  { font-family:var(--mono); font-size:.55rem; color:var(--gold); margin-top:2px }
.gal-cap-zoom { position:absolute; top:12px; right:12px; width:32px; height:32px; background:rgba(2,2,9,.6); backdrop-filter:blur(8px); border:1px solid var(--border2); border-radius:50%; display:flex; align-items:center; justify-content:center; opacity:0; transition:opacity var(--t); color:var(--gold2) }
.gal-item:hover .gal-cap-zoom { opacity:1 }
.pdf-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:24px }
.pdf-card { background:var(--ink2); border:1px solid var(--border); border-radius:var(--r2); overflow:hidden; cursor:pointer; opacity:0; transform:translateY(20px); transition:opacity .6s,transform .6s,border-color var(--t),box-shadow var(--t) }
.pdf-card.visible { opacity:1; transform:translateY(0) }
.pdf-card:hover { border-color:var(--border2); box-shadow:0 24px 60px rgba(0,0,0,.65) }
.pdf-card-preview { position:relative; aspect-ratio:3/4; background:linear-gradient(135deg,var(--ink3),var(--ink4)); overflow:hidden }
.pdf-thumb-canvas { width:100%; height:100%; object-fit:contain; display:block }
.pdf-card-overlay { position:absolute; inset:0; background:rgba(2,2,9,.4); display:flex; flex-direction:column; align-items:center; justify-content:center; gap:12px; opacity:0; transition:opacity var(--t) }
.pdf-card:hover .pdf-card-overlay { opacity:1 }
.pdf-open-icon { width:56px; height:56px; background:rgba(200,165,90,.12); backdrop-filter:blur(8px); border:1px solid var(--border2); border-radius:50%; display:flex; align-items:center; justify-content:center; color:var(--gold2) }
.pdf-open-label { font-family:var(--mono); font-size:.62rem; letter-spacing:.2em; text-transform:uppercase; color:var(--gold2) }
.pdf-card-info { padding:18px 20px 22px }
.pdf-card-tag  { font-family:var(--mono); font-size:.58rem; letter-spacing:.2em; text-transform:uppercase; color:var(--gold); margin-bottom:8px }
.pdf-card-name { font-family:var(--serif); font-size:1.1rem; font-weight:400; color:var(--plat); line-height:1.3; margin-bottom:8px; word-break:break-word }
.pdf-card-meta { font-family:var(--mono); font-size:.6rem; letter-spacing:.1em; color:var(--plat3) }
#pdf-modal { display:none; position:fixed; inset:0; z-index:2000; background:rgba(2,2,9,.96); backdrop-filter:blur(20px); flex-direction:column }
#pdf-modal.open { display:flex }
.pdf-modal-header { display:flex; align-items:center; gap:16px; flex-wrap:wrap; padding:16px 28px; background:rgba(2,2,9,.9); border-bottom:1px solid var(--border) }
.pdf-modal-title { flex:1; min-width:0; font-family:var(--serif); font-size:1.1rem; font-weight:400; color:var(--plat); overflow:hidden; text-overflow:ellipsis; white-space:nowrap }
.pdf-modal-tag  { font-family:var(--mono); font-size:.6rem; letter-spacing:.2em; text-transform:uppercase; color:var(--gold) }
.pdf-modal-close { width:36px; height:36px; border-radius:50%; background:var(--ink3); border:1px solid var(--border); color:var(--plat2); cursor:pointer; display:flex; align-items:center; justify-content:center; transition:all var(--t) }
.pdf-modal-close:hover { border-color:var(--border2); color:var(--gold) }
.pdf-modal-body { flex:1; display:flex; overflow:hidden }
.pdf-main-area  { flex:1; display:flex; align-items:center; justify-content:center; position:relative; overflow:hidden; padding:24px }
#pdf-modal-canvas { max-width:100%; max-height:100%; box-shadow:0 8px 60px rgba(0,0,0,.8); border-radius:4px; display:block; transition:opacity .25s }
.pdf-page-loading { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; font-family:var(--mono); font-size:.75rem; letter-spacing:.2em; color:var(--plat3); background:var(--ink); transition:opacity .3s }
.pdf-page-loading.hidden { opacity:0; pointer-events:none }
.pdf-nav-btn { position:absolute; top:50%; transform:translateY(-50%); width:52px; height:52px; border-radius:50%; background:rgba(2,2,9,.7); backdrop-filter:blur(12px); border:1px solid var(--border2); color:var(--gold); font-size:1.4rem; cursor:pointer; display:flex; align-items:center; justify-content:center; transition:all var(--t); z-index:5 }
.pdf-nav-btn:disabled { opacity:.25; cursor:default }
#pdf-prev { left:20px } #pdf-next { right:20px }
.pdf-thumb-strip { width:140px; flex-shrink:0; background:var(--ink2); border-left:1px solid var(--border); overflow-y:auto; padding:12px; display:flex; flex-direction:column; gap:8px }
.pdf-thumb-item { position:relative; cursor:pointer; border-radius:6px; overflow:hidden; border:1.5px solid transparent; transition:border-color var(--t); background:var(--ink3) }
.pdf-thumb-item.active { border-color:var(--gold) }
.pdf-thumb-item canvas { width:100%; display:block }
.pdf-thumb-num { position:absolute; bottom:4px; right:6px; font-family:var(--mono); font-size:.5rem; color:rgba(237,232,224,.6) }
.pdf-modal-footer { display:flex; align-items:center; justify-content:center; gap:16px; padding:14px 28px; background:rgba(2,2,9,.9); border-top:1px solid var(--border) }
.pdf-page-info { font-family:var(--mono); font-size:.7rem; letter-spacing:.1em; color:var(--gold2); min-width:80px; text-align:center }
.pdf-footer-btn { font-family:var(--mono); font-size:.65rem; letter-spacing:.1em; color:var(--gold); border:1px solid var(--border2); background:var(--gold-dim); border-radius:999px; padding:6px 18px; cursor:pointer; text-decoration:none; transition:all var(--t) }
.pdf-footer-btn:hover { background:rgba(200,165,90,.18) }
#lightbox { display:none; position:fixed; inset:0; z-index:3000; background:rgba(2,2,9,.97); backdrop-filter:blur(16px); align-items:center; justify-content:center; flex-direction:column }
#lightbox.open { display:flex }
#lightbox-wrap { position:relative; max-width:92vw; max-height:86vh; display:flex; align-items:center; justify-content:center }
#lightbox-img { max-width:92vw; max-height:86vh; object-fit:contain; border-radius:6px; box-shadow:0 0 100px rgba(0,0,0,.9); transition:opacity .3s; user-select:none }
#lightbox-caption { position:absolute; bottom:-44px; left:0; right:0; font-family:var(--mono); font-size:.65rem; letter-spacing:.1em; color:var(--plat3); text-align:center }
.lb-close { position:absolute; top:24px; right:28px; width:40px; height:40px; border-radius:50%; background:rgba(2,2,9,.7); border:1px solid var(--border); color:var(--plat2); cursor:pointer; display:flex; align-items:center; justify-content:center; transition:all var(--t); z-index:10 }
.lb-close:hover { color:var(--gold); border-color:var(--border2) }
.lb-nav { position:absolute; top:50%; transform:translateY(-50%); width:52px; height:52px; border-radius:50%; background:rgba(2,2,9,.65); border:1px solid var(--border2); color:var(--plat2); font-size:1.5rem; cursor:pointer; display:flex; align-items:center; justify-content:center; transition:all var(--t); z-index:10 }
.lb-nav:hover { color:var(--gold2); border-color:var(--border3) }
#lb-prev { left:-70px } #lb-next { right:-70px }
#lb-counter { position:absolute; top:24px; left:50%; transform:translateX(-50%); font-family:var(--mono); font-size:.62rem; letter-spacing:.15em; color:var(--plat3); background:rgba(2,2,9,.6); border:1px solid var(--border); border-radius:999px; padding:5px 16px }
footer { position:relative; z-index:1; border-top:1px solid var(--border); padding:40px 72px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:16px }
.footer-logo { font-family:var(--serif); font-size:1.2rem; font-weight:300; color:var(--gold) }
.footer-meta  { font-family:var(--mono); font-size:.62rem; letter-spacing:.12em; color:var(--plat3) }
@keyframes loaderFadeIn { to{opacity:1} }
@keyframes loaderExpand { to{transform:scaleX(1)} }
@keyframes slideUp      { from{opacity:0;transform:translateY(16px)} to{opacity:1;transform:translateY(0)} }
@keyframes kenBurns     { from{transform:scale(1)} to{transform:scale(1.1)} }
@keyframes orbFloat     { 0%,100%{transform:translate(0,0) scale(1)} 33%{transform:translate(30px,-20px) scale(1.05)} 66%{transform:translate(-20px,15px) scale(.95)} }
@keyframes fadeIn       { from{opacity:0} to{opacity:1} }
@keyframes pageFadeUp   { from{opacity:0;transform:translateY(20px)} to{opacity:1;transform:translateY(0)} }
@media(max-width:1024px){.site-header,.home-intro{padding-left:40px;padding-right:40px}.home-grid{padding:40px 40px 80px;grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}.hero-content{padding:0 40px 56px}.tab-bar,.tab-content{padding-left:40px;padding-right:40px}footer{padding:32px 40px}}
@media(max-width:640px){.site-header,.home-intro{padding-left:20px;padding-right:20px}.home-grid{padding:28px 20px 60px;grid-template-columns:1fr}.hero-content{padding:0 20px 40px}.hero-title{font-size:2.2rem}.tab-bar,.tab-content{padding-left:20px;padding-right:20px}.gallery{columns:1}.pdf-grid{grid-template-columns:1fr}.pdf-thumb-strip{display:none}#lb-prev{left:-10px}#lb-next{right:-10px}footer{padding:24px 20px;flex-direction:column;text-align:center}}
"""

def _html_foot(now):
    return """
<div class="ambient-orb orb1"></div>
<div class="ambient-orb orb2"></div>
<div class="ambient-orb orb3"></div>
<div id="cursor"></div>
<div id="cursor-ring"></div>
<div id="progress-bar"></div>

<div id="pdf-modal" role="dialog" aria-modal="true">
  <div class="pdf-modal-header">
    <span class="pdf-modal-tag">📄 PDF</span>
    <div class="pdf-modal-title" id="pdf-modal-title">Yükleniyor…</div>
    <button class="pdf-modal-close" onclick="closePdfModal()">✕</button>
  </div>
  <div class="pdf-modal-body">
    <div class="pdf-main-area">
      <button class="pdf-nav-btn" id="pdf-prev" onclick="pdfModalPrev()" disabled>‹</button>
      <canvas id="pdf-modal-canvas"></canvas>
      <div class="pdf-page-loading" id="pdf-page-loading">Sayfa yükleniyor…</div>
      <button class="pdf-nav-btn" id="pdf-next" onclick="pdfModalNext()">›</button>
    </div>
    <div class="pdf-thumb-strip" id="pdf-thumb-strip"></div>
  </div>
  <div class="pdf-modal-footer">
    <button class="pdf-footer-btn" onclick="pdfModalPrev()">‹ Önceki</button>
    <div class="pdf-page-info" id="pdf-page-info">— / —</div>
    <button class="pdf-footer-btn" onclick="pdfModalNext()">Sonraki ›</button>
    <button class="pdf-footer-btn" onclick="pdfModalFullscreen()">⤢ Tam Ekran</button>
    <a class="pdf-footer-btn" id="pdf-download-btn" href="#" download>⬇ İndir</a>
  </div>
</div>

<div id="lightbox" role="dialog" aria-modal="true">
  <button class="lb-close" onclick="closeLightbox()">✕</button>
  <div id="lb-counter">1 / 1</div>
  <div id="lightbox-wrap">
    <button class="lb-nav" id="lb-prev" onclick="lightboxStep(-1)">‹</button>
    <img id="lightbox-img" src="" alt=""/>
    <button class="lb-nav" id="lb-next" onclick="lightboxStep(1)">›</button>
    <div id="lightbox-caption"></div>
  </div>
</div>

<footer>
  <span class="footer-logo">◈ {0}</span>
  <span class="footer-meta">app.py · {1}</span>
</footer>

<script>
window.addEventListener('load',function(){{setTimeout(function(){{document.getElementById('loader').classList.add('hidden');}},800);}});
(function(){{var c=document.getElementById('cursor'),r=document.getElementById('cursor-ring'),mx=0,my=0,rx=0,ry=0;document.addEventListener('mousemove',function(e){{mx=e.clientX;my=e.clientY;c.style.left=mx+'px';c.style.top=my+'px';}});function a(){{rx+=(mx-rx)*.12;ry+=(my-ry)*.12;r.style.left=rx+'px';r.style.top=ry+'px';requestAnimationFrame(a);}}a();}})();
window.addEventListener('scroll',function(){{var s=document.documentElement;document.getElementById('progress-bar').style.width=(s.scrollTop/(s.scrollHeight-s.clientHeight)*100)+'%';}},{{passive:true}});
var _obs=new IntersectionObserver(function(entries){{entries.forEach(function(e){{if(e.isIntersecting){{e.target.classList.add('visible');_obs.unobserve(e.target);}}}});}},{{threshold:0.05,rootMargin:'0px 0px -40px 0px'}});
function observeItems(){{document.querySelectorAll('.proj-card:not(.visible),.gal-item:not(.visible),.pdf-card:not(.visible)').forEach(function(el,i){{el.style.transitionDelay=(i*.05)+'s';_obs.observe(el);}});}}
document.addEventListener('DOMContentLoaded',observeItems);
(function(){{document.addEventListener('mouseover',function(e){{var c=e.target.closest('.proj-card');if(!c)return;var v=c.querySelector('[data-lazy-video]');if(v&&v.paused)v.play().catch(function(){{}});}});document.addEventListener('mouseout',function(e){{var c=e.target.closest('.proj-card');if(!c)return;if(e.relatedTarget&&c.contains(e.relatedTarget))return;var v=c.querySelector('[data-lazy-video]');if(v&&!v.paused){{v.pause();v.currentTime=0;}}}});}})();
function openProjectAnim(el,slug){{var r=el.getBoundingClientRect(),ov=document.createElement('div'),cx=r.left+r.width/2,cy=r.top+r.height/2;ov.style.cssText='position:fixed;left:'+cx+'px;top:'+cy+'px;width:6px;height:6px;border-radius:50%;background:var(--ink2);transform:translate(-50%,-50%) scale(0);transition:transform .6s cubic-bezier(.4,0,.2,1),opacity .2s .5s;z-index:900;pointer-events:none;';document.body.appendChild(ov);var mx=Math.sqrt(Math.pow(window.innerWidth,2)+Math.pow(window.innerHeight,2));requestAnimationFrame(function(){{ov.style.transform='translate(-50%,-50%) scale('+mx+')';}});setTimeout(function(){{openProject(slug);ov.style.opacity='0';setTimeout(function(){{ov.remove();}},220);}},520);}}
function openProject(slug){{document.querySelectorAll('.proj-cover-video').forEach(function(v){{v.pause();}});document.getElementById('homepage').style.display='none';document.querySelectorAll('.project-page').forEach(function(p){{p.style.display='none';}});var pg=document.getElementById('page-'+slug);if(pg){{pg.style.display='block';pg.style.animation='pageFadeUp .4s both';window.scrollTo(0,0);startHeroSlideshow(slug);setTimeout(observeItems,100);initPdfThumbs(pg);}};}}
function closeProject(){{stopHeroSlideshow();document.querySelectorAll('.project-page').forEach(function(p){{p.style.display='none';}});document.querySelectorAll('.hero-slide video').forEach(function(v){{v.pause();}});var hp=document.getElementById('homepage');hp.style.display='block';hp.style.animation='pageFadeUp .4s both';window.scrollTo(0,0);setTimeout(observeItems,50);}}
function switchTab(slug,key,btn){{var page=document.getElementById('page-'+slug);if(!page)return;page.querySelectorAll('.tab-panel').forEach(function(p){{p.style.display='none';}});page.querySelectorAll('.tab-btn').forEach(function(b){{b.classList.remove('active');}});var panel=document.getElementById('panel-'+slug+'-'+key);if(panel){{panel.style.display='';setTimeout(observeItems,50);}}if(btn)btn.classList.add('active');}}
var _ht=null,_hi=0,_hs=[];
function startHeroSlideshow(slug){{var h=document.querySelector('#page-'+slug+' .hero-slides');if(!h)return;_hs=Array.from(h.querySelectorAll('.hero-slide'));if(!_hs.length)return;_hi=0;_hs.forEach(function(s){{s.classList.remove('active');}});_hs[0].classList.add('active');updateHeroDots(slug,0);if(_hs.length>1)_ht=setInterval(function(){{_hs[_hi].classList.remove('active');_hi=(_hi+1)%_hs.length;_hs[_hi].classList.add('active');var img=_hs[_hi].querySelector('img');if(img){{img.style.animation='none';void img.offsetWidth;img.style.animation='kenBurns 12s forwards';}}updateHeroDots(slug,_hi);}},5000);}}
function stopHeroSlideshow(){{if(_ht){{clearInterval(_ht);_ht=null;}}}}
function goHeroSlide(slug,idx){{if(_ht)clearInterval(_ht);_hs.forEach(function(s){{s.classList.remove('active');}});_hi=idx;_hs[_hi].classList.add('active');var img=_hs[_hi].querySelector('img');if(img){{img.style.animation='none';void img.offsetWidth;img.style.animation='kenBurns 12s forwards';}}updateHeroDots(slug,idx);if(_hs.length>1)_ht=setInterval(function(){{_hs[_hi].classList.remove('active');_hi=(_hi+1)%_hs.length;_hs[_hi].classList.add('active');var img2=_hs[_hi].querySelector('img');if(img2){{img2.style.animation='none';void img2.offsetWidth;img2.style.animation='kenBurns 12s forwards';}}updateHeroDots(slug,_hi);}},5000);}}
function updateHeroDots(slug,idx){{var d=document.querySelector('#page-'+slug+' .hero-slide-nav');if(!d)return;d.querySelectorAll('.hero-dot').forEach(function(dot,i){{dot.classList.toggle('active',i===idx);}});}}
function galFilter(btn,slug,cat){{btn.closest('.gallery-filters').querySelectorAll('.gal-filter').forEach(function(b){{b.classList.remove('active');}});btn.classList.add('active');document.getElementById('gal-'+slug).querySelectorAll('.gal-item').forEach(function(item){{item.style.display=(cat==='all'||item.dataset.cat===cat)?'':'none';}});}}
function initPdfThumbs(container){{if(!window.pdfjsLib){{setTimeout(function(){{initPdfThumbs(container);}},150);return;}}window.pdfjsLib.GlobalWorkerOptions.workerSrc='https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';container.querySelectorAll('.pdf-thumb-canvas[data-pdf]').forEach(function(canvas){{if(canvas.dataset.loaded)return;canvas.dataset.loaded='1';window.pdfjsLib.getDocument(canvas.dataset.pdf).promise.then(function(pdf){{pdf.getPage(1).then(function(page){{var vp=page.getViewport({{scale:1}}),scale=canvas.parentElement.offsetWidth/vp.width,vp2=page.getViewport({{scale:scale*.9}});canvas.width=vp2.width;canvas.height=vp2.height;canvas.style.width='100%';canvas.style.height='auto';page.render({{canvasContext:canvas.getContext('2d'),viewport:vp2}});}});}}).catch(function(){{canvas.style.display='none';}});}});}}
var _pm={{doc:null,cur:1,total:0,url:'',name:''}};
function openPdfModal(url,name,totalHint){{if(!window.pdfjsLib){{alert('PDF görüntüleyici yükleniyor…');return;}}window.pdfjsLib.GlobalWorkerOptions.workerSrc='https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';_pm.url=url;_pm.name=name;_pm.cur=1;document.getElementById('pdf-modal-title').textContent=name;document.getElementById('pdf-download-btn').href=url;document.getElementById('pdf-download-btn').download=name;document.getElementById('pdf-modal').classList.add('open');document.body.style.overflow='hidden';document.getElementById('pdf-page-loading').classList.remove('hidden');document.getElementById('pdf-modal-canvas').style.opacity='0';document.getElementById('pdf-thumb-strip').innerHTML='';window.pdfjsLib.getDocument(url).promise.then(function(pdf){{_pm.doc=pdf;_pm.total=pdf.numPages;renderPdfPage(1);buildPdfThumbs();}}).catch(function(e){{document.getElementById('pdf-page-loading').textContent='PDF açılamadı: '+e.message;}});}}
function renderPdfPage(num){{if(!_pm.doc)return;_pm.cur=num;document.getElementById('pdf-page-loading').classList.remove('hidden');document.getElementById('pdf-modal-canvas').style.opacity='0';_pm.doc.getPage(num).then(function(page){{var area=document.querySelector('.pdf-main-area'),maxW=area.clientWidth-120,maxH=area.clientHeight-40,vp0=page.getViewport({{scale:1}}),scale=Math.min(maxW/vp0.width,maxH/vp0.height,2.5),vp=page.getViewport({{scale:scale}}),canvas=document.getElementById('pdf-modal-canvas');canvas.width=vp.width;canvas.height=vp.height;page.render({{canvasContext:canvas.getContext('2d'),viewport:vp}}).promise.then(function(){{canvas.style.opacity='1';document.getElementById('pdf-page-loading').classList.add('hidden');document.getElementById('pdf-page-info').textContent=num+' / '+_pm.total;document.getElementById('pdf-prev').disabled=(num<=1);document.getElementById('pdf-next').disabled=(num>=_pm.total);document.querySelectorAll('.pdf-thumb-item').forEach(function(t,i){{t.classList.toggle('active',i+1===num);}});}});}}); }}
function buildPdfThumbs(){{var strip=document.getElementById('pdf-thumb-strip');strip.innerHTML='';var n=Math.min(_pm.total,50);for(var i=1;i<=n;i++){{(function(pnum){{var div=document.createElement('div');div.className='pdf-thumb-item'+(pnum===1?' active':'');div.onclick=(function(p){{return function(){{renderPdfPage(p);}};}})(pnum);var c=document.createElement('canvas'),num=document.createElement('span');num.className='pdf-thumb-num';num.textContent=pnum;div.appendChild(c);div.appendChild(num);strip.appendChild(div);_pm.doc.getPage(pnum).then(function(page){{var vp=page.getViewport({{scale:1}}),scale=110/vp.width,vp2=page.getViewport({{scale:scale}});c.width=vp2.width;c.height=vp2.height;c.style.width='100%';c.style.height='auto';page.render({{canvasContext:c.getContext('2d'),viewport:vp2}});}});}}})(i);}}}}
function pdfModalPrev(){{if(_pm.cur>1)renderPdfPage(_pm.cur-1);}}
function pdfModalNext(){{if(_pm.cur<_pm.total)renderPdfPage(_pm.cur+1);}}
function closePdfModal(){{document.getElementById('pdf-modal').classList.remove('open');document.body.style.overflow='';_pm.doc=null;}}
function pdfModalFullscreen(){{var el=document.getElementById('pdf-modal');if(el.requestFullscreen)el.requestFullscreen();else if(el.webkitRequestFullscreen)el.webkitRequestFullscreen();}}
var _lbItems=[],_lbIdx=0,_lbTouchX=null;
function openLightbox(el){{var g=el.closest('.gallery');_lbItems=Array.from(g.querySelectorAll('.gal-item:not([style*="none"])'));_lbIdx=_lbItems.indexOf(el);showLightboxItem();document.getElementById('lightbox').classList.add('open');document.body.style.overflow='hidden';}}
function showLightboxItem(){{var item=_lbItems[_lbIdx],img=item.querySelector('img'),lb=document.getElementById('lightbox-img');lb.style.opacity='0';lb.onload=function(){{lb.style.opacity='1';}};lb.src=img.dataset.large||img.src;lb.alt=img.alt;var cn=item.querySelector('.gal-cap-name'),cs=item.querySelector('.gal-cap-sub');document.getElementById('lightbox-caption').textContent=(cn?cn.textContent:'')+(cs?' · '+cs.textContent:'');document.getElementById('lb-counter').textContent=(_lbIdx+1)+' / '+_lbItems.length;}}
function lightboxStep(dir){{_lbIdx=(_lbIdx+dir+_lbItems.length)%_lbItems.length;showLightboxItem();}}
function closeLightbox(){{document.getElementById('lightbox').classList.remove('open');document.body.style.overflow='';}}
document.getElementById('lightbox').addEventListener('click',function(e){{if(e.target===this)closeLightbox();}});
document.getElementById('lightbox').addEventListener('touchstart',function(e){{_lbTouchX=e.touches[0].clientX;}},{{passive:true}});
document.getElementById('lightbox').addEventListener('touchend',function(e){{if(_lbTouchX===null)return;var dx=e.changedTouches[0].clientX-_lbTouchX;if(Math.abs(dx)>50)lightboxStep(dx<0?1:-1);_lbTouchX=null;}});
document.addEventListener('keydown',function(e){{var lb=document.getElementById('lightbox').classList.contains('open'),pdf=document.getElementById('pdf-modal').classList.contains('open');if(e.key==='Escape'){{if(lb)closeLightbox();else if(pdf)closePdfModal();else closeProject();}}if(lb){{if(e.key==='ArrowRight')lightboxStep(1);if(e.key==='ArrowLeft')lightboxStep(-1);}}if(pdf){{if(e.key==='ArrowRight'||e.key==='ArrowDown')pdfModalNext();if(e.key==='ArrowLeft'||e.key==='ArrowUp')pdfModalPrev();}}}});
</script>
</body>
</html>
""".format(html.escape(PROJE_ADI), now)


# ════════════════════════════════════════════════════════════
#  ANA HTML ÜRETİCİ
# ════════════════════════════════════════════════════════════
def build_html():
    """Drive'dan çekip sunum.html üretir. Döner: True=üretildi, False=değişiklik yok."""
    log.info("🔄 Drive kontrolü başlatılıyor…")
    Path(ASSETS_DIR).mkdir(exist_ok=True)

    service = get_service()
    files   = list_files(service, FOLDER_ID)
    if not files:
        log.warning("⚠  Klasörde dosya bulunamadı.")
        return False

    current_hash = compute_manifest(files)
    if current_hash == load_manifest() and Path(OUTPUT_FILE).exists():
        log.info("✅ Drive'da değişiklik yok.")
        return False

    log.info(f"🔨 {len(files)} dosya işleniyor…")
    projects = OrderedDict()

    def get_project(fp):
        p = fp.split(" / ")
        return p[0] if p else "DİĞER"

    for f in files:
        pname = get_project(f.get("folder_path", ""))
        if pname not in projects:
            projects[pname] = {"images":[], "pdfs":[], "total":0, "cover":"", "video":"", "cover_is_exterior":False}
        projects[pname]["total"] += 1

    total = len(files)
    for i, f in enumerate(files, 1):
        name  = f["name"]
        mime  = f.get("mimeType", "")
        fid   = f["id"]
        tkey  = file_type_key(name, mime)
        fp    = f.get("folder_path", "")
        pname = get_project(fp)
        sub   = " / ".join(fp.split(" / ")[1:]) if " / " in fp else ""
        log.info(f"[{i:03}/{total:03}] {name[:50]} [{tkey}]")

        try:
            data, real_mime = download_cached(service, fid, mime, f.get("modifiedTime",""))
            tkey2 = file_type_key(name, real_mime) if real_mime != mime else tkey
            name_up   = name.upper()
            asset_uid = hashlib.md5((fid + f.get("modifiedTime","")).encode()).hexdigest()[:12]

            if tkey2 == "other":
                is_video    = real_mime.startswith("video/") or name.lower().endswith(".mp4")
                is_tanitim  = "TANITIM" in name_up
                if is_video and is_tanitim and not projects[pname]["video"]:
                    vpath = save_video(data, asset_uid)
                    if vpath:
                        projects[pname]["video"] = vpath
                del data; continue

            if tkey2 in ("word", "table", "json"):
                del data; continue

            if tkey2 == "pdf":
                if not any(k in name_up for k in (
                    "SUNUM","FİYAT","FIYAT","ÖDEME","ODEME",
                    "LİSTE","LISTE","TANITIM","KATALOG","OTURUM","PLAN","BRAVO"
                )):
                    del data; continue

            if tkey2 == "image":
                thumb = make_thumb(data, real_mime, asset_uid)
                large = make_large(data, real_mime, asset_uid)
                del data
                is_exterior = "DIŞ CEPHE" in fp.upper() or "DIS CEPHE" in fp.upper()
                projects[pname]["images"].append((thumb, large, html.escape(name), html.escape(sub), html.escape(fp)))
                if not projects[pname]["cover"] and thumb:
                    projects[pname]["cover"] = thumb
                    projects[pname]["cover_is_exterior"] = is_exterior
                elif is_exterior and not projects[pname].get("cover_is_exterior") and thumb:
                    projects[pname]["cover"] = thumb
                    projects[pname]["cover_is_exterior"] = True

            elif tkey2 == "pdf":
                lbl  = "Slides" if mime == "application/vnd.google-apps.presentation" else "PDF"
                projects[pname]["pdfs"].append(process_pdf(data, name, lbl))
                del data

        except Exception as e:
            log.error(f"İşlenemedi: {e}")
            try: del data
            except: pass

    log.info(f"[✓] HTML yazılıyor → {OUTPUT_FILE}")
    now    = datetime.now().strftime("%d.%m.%Y %H:%M")
    n_proj = len(projects)
    _words = PROJE_ADI.split()
    proje_h1 = " ".join(_words[:-1]) + (f" <em>{_words[-1]}</em>" if len(_words) > 1 else _words[0])

    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
        fh.write(_html_head(PROJE_ADI))
        fh.write(_html_css())
        fh.write("</style>\n</head>\n<body>\n\n")

        # Loader
        fh.write(f'<div id="loader"><div class="loader-logo">{html.escape(PROJE_ADI)}</div>'
                 f'<div class="loader-line"></div>'
                 f'<div class="loader-sub">Gayrimenkul Portföyü</div></div>\n\n')

        # Header
        fh.write(f'<header class="site-header"><div class="header-inner">'
                 f'<p class="header-eyebrow">Gayrimenkul Portföyü · Google Drive Arşivi</p>'
                 f'<h1>{proje_h1}</h1>'
                 f'<p class="header-sub">{html.escape(PROJE_ALT_BASLIK)}</p>'
                 f'<div class="header-line"></div>'
                 f'<div class="header-meta">'
                 f'<div class="hm-pill">Proje <span>{n_proj}</span></div>'
                 f'<div class="hm-pill">Toplam Dosya <span>{total}</span></div>'
                 f'<div class="hm-pill">Oluşturulma <span>{now}</span></div>'
                 f'</div></div></header>\n\n')

        # Homepage
        fh.write('<div id="homepage">\n')
        fh.write(f'<div class="home-intro"><div class="home-intro-title">Tüm <strong>Projeler</strong></div>'
                 f'<div class="home-count">{n_proj} Proje</div></div>\n')
        fh.write('<div class="home-grid">\n')

        for idx, (pname, pdata) in enumerate(projects.items(), 1):
            slug      = slugify(pname)
            cover     = pdata["cover"] or ""
            n_img     = len(pdata["images"])
            n_pdf     = len(pdata["pdfs"])
            total_p   = pdata["total"]
            video_src = pdata.get("video", "")
            has_video = bool(video_src)

            if has_video:
                cover_html = (f'<video class="proj-cover proj-cover-video" src="{video_src}" '
                              f'muted loop playsinline preload="none" poster="{cover}" data-lazy-video="1"></video>')
            elif cover:
                cover_html = f'<img src="{cover}" class="proj-cover" alt="{html.escape(pname)}" loading="lazy"/>'
            else:
                cover_html = '<div class="proj-cover-placeholder">◈</div>'

            badges = ""
            if has_video: badges += '<span class="badge badge-video">▶ Video</span>'
            if n_img:     badges += f'<span class="badge">🖼 {n_img}</span>'
            if n_pdf:     badges += f'<span class="badge">📄 {n_pdf} PDF</span>'

            fh.write(f'<div class="proj-card" onclick="openProjectAnim(this,\'{slug}\')" tabindex="0" role="button" '
                     f'onkeydown="if(event.key===\'Enter\')openProjectAnim(this,\'{slug}\')" aria-label="{html.escape(pname)}">\n'
                     f'  <div class="proj-cover-wrap">{cover_html}'
                     f'<div class="proj-cover-gradient"></div>'
                     f'<div class="proj-cover-number">{idx:02d}</div>'
                     f'<div class="proj-cover-cta">{"▶ İzle →" if has_video else "Keşfet →"}</div></div>\n'
                     f'  <div class="proj-info"><div class="proj-eyebrow">Proje</div>'
                     f'<h2 class="proj-name">{html.escape(pname)}</h2>'
                     f'<div class="proj-badges">{badges}</div>'
                     f'<div class="proj-divider"></div>'
                     f'<div class="proj-total">{total_p} dosya</div></div>\n</div>\n')

        fh.write('</div>\n</div>\n\n<!-- PROJECT PAGES -->\n')

        for pname, pdata in projects.items():
            slug      = slugify(pname)
            n_img     = len(pdata["images"])
            n_pdf     = len(pdata["pdfs"])
            has_video = bool(pdata.get("video"))
            hero_imgs = [item for item in pdata["images"] if item[1]][:8]
            hero_video = pdata.get("video", "")

            # Hero slides
            hero_html = ""
            if hero_video:
                hero_html += f'<div class="hero-slide active"><video src="{hero_video}" autoplay muted loop playsinline style="width:100%;height:100%;object-fit:cover"></video></div>\n'
                for item in hero_imgs:
                    hero_html += f'<div class="hero-slide"><img src="{item[1]}" alt="" loading="lazy"/></div>\n'
            elif hero_imgs:
                for i, item in enumerate(hero_imgs):
                    hero_html += f'<div class="hero-slide{" active" if i==0 else ""}"><img src="{item[1]}" alt="" loading="lazy"/></div>\n'
            else:
                hero_html = '<div class="hero-slide active" style="background:var(--ink3)"></div>\n'

            n_slides = (1 if hero_video else 0) + len(hero_imgs)
            dots_html = "".join(
                f'<div class="hero-dot{" active" if i==0 else ""}" onclick="goHeroSlide(\'{slug}\',{i})"></div>\n'
                for i in range(min(n_slides, 8))
            )

            # Tab bar
            tabs_html = (f'<button class="tab-bar-back" onclick="closeProject()">'
                         f'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"/></svg>'
                         f'Tüm Projeler</button><div class="tab-bar-divider"></div>')
            if pdata["images"]:
                tabs_html += (f'<button class="tab-btn active" onclick="switchTab(\'{slug}\',\'images\',this)">'
                              f'🖼 Görseller <span class="tab-count">{n_img}</span></button>')
            if pdata["pdfs"]:
                active2 = "" if pdata["images"] else " active"
                tabs_html += (f'<button class="tab-btn{active2}" onclick="switchTab(\'{slug}\',\'pdfs\',this)">'
                              f'📄 Sunum &amp; Fiyat <span class="tab-count">{n_pdf}</span></button>')

            badges_h = ""
            if has_video: badges_h += '<span class="badge badge-video">▶ Video</span>'
            if n_img:     badges_h += f'<span class="badge">🖼 {n_img} görsel</span>'
            if n_pdf:     badges_h += f'<span class="badge">📄 {n_pdf} PDF</span>'

            fh.write(f'<section class="project-page" id="page-{slug}" style="display:none">\n'
                     f'  <div class="proj-hero"><div class="hero-slides">{hero_html}</div>'
                     f'<div class="hero-gradient"></div>'
                     f'<div class="hero-content"><div class="hero-text">'
                     f'<button class="back-btn" onclick="closeProject()">← Tüm Projeler</button>'
                     f'<div class="hero-eyebrow">Proje Detayı</div>'
                     f'<h1 class="hero-title">{html.escape(pname)}</h1>'
                     f'<div class="hero-badges">{badges_h}</div></div>'
                     f'<div style="display:flex;flex-direction:column;align-items:flex-end;gap:12px;padding-bottom:4px">'
                     f'<div class="hero-slide-nav">{dots_html}</div>'
                     f'<div class="hero-counter">{n_slides} görsel</div></div>'
                     f'</div></div>\n'
                     f'  <div class="tab-bar">{tabs_html}</div>\n'
                     f'  <div class="tab-content">\n')

            # Images panel
            if pdata["images"]:
                cats, seen = [], set()
                for item in pdata["images"]:
                    sub = item[3]
                    if sub and sub not in seen:
                        seen.add(sub); cats.append(sub)

                filters_html = f'<button class="gal-filter active" onclick="galFilter(this,\'{slug}\',\'all\')">Tümü</button>'
                for cat in cats[:6]:
                    short = cat.split(" / ")[-1] if " / " in cat else cat
                    filters_html += f'<button class="gal-filter" onclick="galFilter(this,\'{slug}\',\'{cat}\')">{short}</button>'

                fh.write(f'<div class="tab-panel" id="panel-{slug}-images">\n')
                fh.write(f'<div class="gallery-header"><div class="gallery-title"><strong>{html.escape(pname)}</strong> · Görseller</div>'
                         f'<div class="gallery-filters">{filters_html}</div></div>\n')
                fh.write(f'<div class="gallery" id="gal-{slug}">\n')
                zoom_icon = ('<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
                             '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>'
                             '<line x1="11" y1="8" x2="11" y2="14"/><line x1="8" y1="11" x2="14" y2="11"/></svg>')
                for item in pdata["images"]:
                    thumb, large, iname, isub, ifp = item
                    if not thumb: continue
                    cat_attr = f' data-cat="{isub}"' if isub else ''
                    sub_div  = f'<div class="gal-cap-sub">{isub}</div>' if isub else ''
                    fh.write(f'<div class="gal-item" onclick="openLightbox(this)"{cat_attr}>'
                             f'<img src="{thumb}" data-large="{large}" alt="{iname}" loading="lazy"/>'
                             f'<div class="gal-caption"><div class="gal-cap-name">{iname}</div>{sub_div}</div>'
                             f'<div class="gal-cap-zoom">{zoom_icon}</div></div>\n')
                fh.write('</div>\n</div>\n')

            # PDF panel
            if pdata["pdfs"]:
                hidden = ' style="display:none"' if pdata["images"] else ""
                fh.write(f'<div class="tab-panel" id="panel-{slug}-pdfs"{hidden}>\n')
                fh.write(f'<div class="gallery-header"><div class="gallery-title">'
                         f'<strong>{html.escape(pname)}</strong> · Belgeler</div></div>\n')
                fh.write('<div class="pdf-grid">\n')
                for card in pdata["pdfs"]:
                    fh.write(card + "\n")
                fh.write('</div>\n</div>\n')

            fh.write('  </div>\n</section>\n\n')

        fh.write(_html_foot(now))

    save_manifest(current_hash)
    size_kb = Path(OUTPUT_FILE).stat().st_size / 1024
    log.info(f"✅ {OUTPUT_FILE} oluşturuldu ({size_kb:.0f} KB)")
    return True


# ════════════════════════════════════════════════════════════
#  BUILD WRAPPER (kilit + durum takibi)
# ════════════════════════════════════════════════════════════
def run_build(force=False):
    global _build_status
    if _build_lock.locked():
        log.warning("⚠  Zaten bir üretim süreci çalışıyor.")
        return False
    with _build_lock:
        _build_status["running"] = True
        _build_status["last_run"] = datetime.now().isoformat()
        try:
            if force:
                p = Path(MANIFEST_FILE)
                if p.exists(): p.unlink()
            result = build_html()
            _build_status["last_result"] = "Başarılı" if result else "Değişiklik yok"
            return result
        except Exception as e:
            log.error(f"❌ Üretim hatası: {e}", exc_info=True)
            _build_status["last_result"] = f"Hata: {e}"
            return False
        finally:
            _build_status["running"] = False


# ════════════════════════════════════════════════════════════
#  FLASK ROUTES
# ════════════════════════════════════════════════════════════
@app.route("/")
def index():
    if not Path(OUTPUT_FILE).exists():
        return ("""<!DOCTYPE html><html lang="tr"><head><meta charset="UTF-8">
            <meta http-equiv="refresh" content="15"><title>Yükleniyor…</title>
            <style>body{background:#020209;color:#C8A55A;font-family:sans-serif;
            display:flex;align-items:center;justify-content:center;min-height:100vh;
            flex-direction:column;gap:24px}
            .spinner{width:40px;height:40px;border:3px solid #1A1A2E;
            border-top-color:#C8A55A;border-radius:50%;animation:spin 1s linear infinite}
            @keyframes spin{to{transform:rotate(360deg)}}</style></head><body>
            <div class="spinner"></div><p>Sunum hazırlanıyor, lütfen bekleyin…</p>
            <p style="font-size:12px;color:#6A677A">Sayfa 15 saniyede yenilenir</p>
            </body></html>""", 202)
    return send_file(OUTPUT_FILE)


@app.route(f"/{ASSETS_DIR}/<path:filename>")
def assets(filename):
    return send_from_directory(ASSETS_DIR, filename)


@app.route("/status")
def status():
    html_exists = Path(OUTPUT_FILE).exists()
    html_size   = Path(OUTPUT_FILE).stat().st_size // 1024 if html_exists else 0
    asset_count = len(list(Path(ASSETS_DIR).iterdir())) if Path(ASSETS_DIR).exists() else 0
    return jsonify({
        "ok": html_exists,
        "html_size_kb": html_size,
        "asset_files": asset_count,
        "build_running": _build_status["running"],
        "last_run": _build_status["last_run"],
        "last_result": _build_status["last_result"],
        "server_time": datetime.now().isoformat(),
    })


@app.route("/rebuild")
def manual_rebuild():
    secret = os.environ.get("SECRET_REBUILD_TOKEN")
    if secret and request.args.get("token") != secret:
        return jsonify({"error": "Yetkisiz"}), 403
    if _build_status["running"]:
        return jsonify({"message": "Zaten bir üretim çalışıyor"}), 409
    threading.Thread(target=lambda: run_build(force=True), daemon=True).start()
    return jsonify({"message": "Yeniden üretim başlatıldı. /status takip edin."}), 202


# ════════════════════════════════════════════════════════════
#  ZAMANLAYICI
# ════════════════════════════════════════════════════════════
def start_scheduler():
    tz = pytz.timezone("Europe/Istanbul")
    scheduler = BackgroundScheduler(timezone=tz)
    scheduler.add_job(lambda: run_build(force=False), "cron", hour=4, minute=0,
                      id="nightly", name="Gece 04:00 Drive Kontrolü")
    scheduler.start()
    log.info("⏰ Zamanlayıcı başlatıldı — her gece 04:00 (İstanbul).")
    return scheduler


# ════════════════════════════════════════════════════════════
#  BAŞLANGIÇ
# ════════════════════════════════════════════════════════════
scheduler = start_scheduler()

if not Path(OUTPUT_FILE).exists():
    log.info("📂 sunum.html bulunamadı, arka planda üretiliyor…")
    threading.Thread(target=lambda: run_build(force=True), daemon=True).start()
else:
    log.info(f"📄 {OUTPUT_FILE} mevcut, sunucu hazır.") 

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    log.info(f"🚀 http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
