# streamlit_file_viewer.py
# Visualizador de archivos para un proyecto Streamlit
# Uso: streamlit run streamlit_file_viewer.py

import streamlit as st
from pathlib import Path
import mimetypes
from PIL import Image
import io
import os
import datetime

st.set_page_config(page_title="Visor de archivos", layout="wide")

def list_entries(root: Path, show_hidden=False):
    try:
        entries = sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        if not show_hidden:
            entries = [e for e in entries if not e.name.startswith(".")]
        return entries
    except Exception as e:
        st.error(f"No se puede listar {root}: {e}")
        return []

def read_file_bytes(path: Path):
    try:
        return path.read_bytes()
    except Exception as e:
        st.error(f"No se puede leer {path}: {e}")
        return None

def pretty_size(n):
    for unit in ["B","KB","MB","GB","TB"]:
        if n < 1024.0:
            return f"{n:3.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"

# UI - barra lateral
st.sidebar.title("Navegación")
root_input = st.sidebar.text_input("Directorio raíz", value=".")
root_path = Path(root_input).expanduser().resolve()
show_hidden = st.sidebar.checkbox("Mostrar archivos ocultos", value=False)
filter_ext = st.sidebar.text_input("Filtrar por extensión (ej: .py,.md) - dejar vacío para todos")
search = st.sidebar.text_input("Buscar por nombre")
st.sidebar.markdown("---")
st.sidebar.write(f"Directorio raíz: {root_path}")

# Build directory selection
if not root_path.exists():
    st.error(f"El directorio {root_path} no existe.")
    st.stop()

# Collect directories recursively (max depth to avoid huge trees)
max_depth = st.sidebar.slider("Profundidad máxima (recursión)", 1, 6, 3)

def collect_dirs(base: Path, depth=0, maxd=3):
    dirs = []
    if depth > maxd:
        return dirs
    for p in sorted(base.iterdir()):
        if p.is_dir() and (not p.name.startswith(".") or show_hidden):
            dirs.append(p)
            dirs += collect_dirs(p, depth+1, maxd)
    return dirs

all_dirs = [root_path] + collect_dirs(root_path, 0, max_depth)
dir_display = [str(p.relative_to(root_path)) if p != root_path else "." for p in all_dirs]
selected_dir_idx = st.sidebar.selectbox("Selecciona carpeta", options=list(range(len(all_dirs))), format_func=lambda i: dir_display[i])
selected_dir = all_dirs[selected_dir_idx]

# List files in selected directory
entries = list_entries(selected_dir, show_hidden=show_hidden)
# filter files
if filter_ext.strip():
    exts = [e.strip().lower() for e in filter_ext.split(",") if e.strip()]
    entries = [e for e in entries if (e.is_dir() or e.suffix.lower() in exts)]
if search.strip():
    q = search.lower()
    entries = [e for e in entries if q in e.name.lower()]

# Split into dirs and files for display
dirs = [e for e in entries if e.is_dir()]
files = [e for e in entries if e.is_file()]

col1, col2 = st.columns([1,3])
with col1:
    st.header("Contenido")
    if dirs:
        st.subheader("Carpetas")
        for d in dirs:
            st.write(f"📁 {d.name}")
    else:
        st.info("No hay subcarpetas")
    st.subheader("Archivos")
    if files:
        sel_file = st.selectbox("Selecciona archivo", options=files, format_func=lambda p: p.name)
    else:
        sel_file = None
        st.info("No hay archivos en esta carpeta")

with col2:
    st.header("Vista previa")
    if sel_file:
        st.subheader(sel_file.name)
        stat = sel_file.stat()
        st.write({
            "Ruta": str(sel_file),
            "Tamaño": pretty_size(stat.st_size),
            "Última modificación": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
        })
        mime, _ = mimetypes.guess_type(sel_file.name)
        # Images
        if mime and mime.startswith("image"):
            try:
                img = Image.open(sel_file)
                st.image(img, caption=sel_file.name, use_column_width=True)
                b = io.BytesIO()
                img.save(b, format=img.format or "PNG")
                b.seek(0)
                st.download_button("Descargar imagen", data=b.read(), file_name=sel_file.name, mime=mime)
            except Exception as e:
                st.error(f"No se puede mostrar la imagen: {e}")
        # Text / code
        else:
            raw = read_file_bytes(sel_file)
            if raw is not None:
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = None
                if text is not None:
                    # intentar detección simple del lenguaje por extensión
                    lang = {
                        ".py":"python", ".md":"markdown", ".sql":"sql", ".json":"json",
                        ".yaml":"yaml", ".yml":"yaml", ".js":"javascript", ".css":"css",
                        ".html":"html", ".txt":"text"
                    }.get(sel_file.suffix.lower(), None)
                    st.code(text, language=lang)
                    st.download_button("Descargar archivo", data=raw, file_name=sel_file.name, mime=mime or "application/octet-stream")
                else:
                    st.warning("Archivo binario — no se puede previsualizar. Puede descargarlo.")
                    st.download_button("Descargar archivo binario", data=raw, file_name=sel_file.name, mime=mime or "application/octet-stream")
    else:
        st.write("Selecciona un archivo para ver su contenido.")

st.sidebar.markdown("---")
st.sidebar.write("Atajos:")
st.sidebar.write("- Navegar carpetas desde el desplegable")
st.sidebar.write("- Filtrar por extensiones o buscar por nombre")
