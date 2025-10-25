
from datetime import datetime, timedelta
import random
import sqlite3
import logging
import time
import altair as alt
import streamlit.components.v1 as components
from streamlit_kanban import kanban
from io import BytesIO
import numpy as np
import plotly.express as px 
import pandas as pd
import streamlit as st
from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode
from PIL import Image
EMAILS_HABILITADOS = True
fecha_actual = datetime.now().strftime("%d-%m-%Y %H:%M")

# --- Persistencia de base de datos (SQLite) ---
import os
import shutil
from pathlib import Path
import re
import hashlib

# File names and paths
DB_FILENAME = 'helpdesk.db'
DB_DATA_DIR = 'data'
DB_DATA_PATH = os.path.join(DB_DATA_DIR, DB_FILENAME)
DB_ORIG_PATH = os.path.abspath(DB_FILENAME)
DB_LOCK_PATH = os.path.join(DB_DATA_DIR, DB_FILENAME + '.lock')
DB_REPO_BACKUP_PREFIX = os.path.join(DB_DATA_DIR, 'repo_backup')

def _sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()

def _acquire_lock(lock_path, timeout=30):
    """Simple advisory lock using O_EXCL on a lockfile. Returns file descriptor.

    This is cross-platform and avoids extra dependencies. Raises TimeoutError on failure.
    """
    start = time.time()
    while True:
        try:
            # os.O_CREAT | os.O_EXCL ensures creation fails if exists
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, f"pid:{os.getpid()} time:{time.time()}".encode())
            return fd
        except FileExistsError:
            if time.time() - start > timeout:
                raise TimeoutError(f"Timeout acquiring DB lock {lock_path}")
            time.sleep(0.1)

def _release_lock(fd, lock_path):
    try:
        os.close(fd)
    except Exception:
        pass
    try:
        os.remove(lock_path)
    except Exception:
        pass

def ensure_persistent_db(create_schema_callback=None):
    """Ensure a persistent DB exists in `data/helpdesk.db` and avoid overwriting it.

    Rules implemented:
    - If `data/helpdesk.db` exists: keep it (do not overwrite from repo file).
    - If it doesn't exist and repo `helpdesk.db` exists: move it atomically to `data/`.
      If both exist but differ, keep the data copy and move the repo DB to a timestamped
      backup under `data/` so nothing is lost.
    - If none exist: create an empty DB file and (optionally) invoke
      `create_schema_callback(conn)` to initialize tables.
    """
    os.makedirs(DB_DATA_DIR, exist_ok=True)

    fd = None
    try:
        fd = _acquire_lock(DB_LOCK_PATH, timeout=15)

        data_exists = os.path.exists(DB_DATA_PATH)
        repo_exists = os.path.exists(DB_ORIG_PATH)

        if data_exists:
            # Persistent DB already present. If repo also exists and is different,
            # move repo DB to backups so a subsequent redeploy won't overwrite the
            # persistent one by accident.
            if repo_exists:
                try:
                    repo_hash = _sha256_of_file(DB_ORIG_PATH)
                    data_hash = _sha256_of_file(DB_DATA_PATH)
                except Exception:
                    repo_hash = None
                    data_hash = None

                if repo_hash and data_hash and repo_hash != data_hash:
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    backup_path = f"{DB_REPO_BACKUP_PREFIX}_{timestamp}.db"
                    try:
                        shutil.move(DB_ORIG_PATH, backup_path)
                        logging.getLogger(__name__).info(f"Repo DB moved to backup: {backup_path}")
                    except Exception as e:
                        logging.getLogger(__name__).warning(f"Could not move repo DB to backup: {e}")
                else:
                    # Repo and data equal or hashes not available: remove repo copy to
                    # avoid accidental confusion on next deploy cycle
                    try:
                        os.remove(DB_ORIG_PATH)
                        logging.getLogger(__name__).info("Removed redundant repo DB file")
                    except Exception:
                        pass
            return

        # If we reach here, data DB does not exist
        if repo_exists:
            # Move repo DB into persistent location atomically
            try:
                shutil.move(DB_ORIG_PATH, DB_DATA_PATH)
                logging.getLogger(__name__).info("Moved repo DB to persistent data folder")
                return
            except Exception as e:
                logging.getLogger(__name__).warning(f"Failed to move repo DB, will attempt copy: {e}")
                try:
                    shutil.copy2(DB_ORIG_PATH, DB_DATA_PATH)
                    logging.getLogger(__name__).info("Copied repo DB to persistent data folder")
                    try:
                        os.remove(DB_ORIG_PATH)
                    except Exception:
                        pass
                    return
                except Exception as e2:
                    logging.getLogger(__name__).error(f"Failed to copy repo DB: {e2}")

        # If neither exists, create an empty DB and optionally run schema init
        conn = sqlite3.connect(DB_DATA_PATH)
        if create_schema_callback:
            try:
                create_schema_callback(conn)
            except Exception as e:
                logging.getLogger(__name__).warning(f"Schema callback failed: {e}")
        conn.close()
        logging.getLogger(__name__).info("Created empty persistent DB file")

    finally:
        if fd is not None:
            _release_lock(fd, DB_LOCK_PATH)

def get_db_connection(*args, **kwargs):
    """Return a sqlite3 connection pointing to the persistent DB path.

    This function ensures the persistent DB exists before connecting.
    """
    ensure_persistent_db()
    # Allow callers to override check_same_thread or timeout via kwargs
    conn_kwargs = dict(kwargs)
    # Use a reasonably long timeout for transient lock contention
    if 'timeout' not in conn_kwargs:
        conn_kwargs['timeout'] = 30
    # Allow multi-threaded access from Streamlit app if needed
    if 'check_same_thread' not in conn_kwargs:
        conn_kwargs['check_same_thread'] = False

    return sqlite3.connect(DB_DATA_PATH, **conn_kwargs)


def process_image_bytes(data_bytes, filename=None, max_dim=1600, jpeg_quality=85):
    """Resize/compress image bytes while preserving reasonable quality.

    - Keeps aspect ratio.
    - If both dimensions are <= max_dim, returns original bytes.
    - For JPEGs: converts to RGB if needed and saves with given quality.
    - For PNGs: saves with optimize=True to try to reduce size while keeping transparency.
    Returns: (bytes, new_mime_or_None)
    """
    try:
        import io
        from PIL import Image

        buf_in = io.BytesIO(data_bytes)
        img = Image.open(buf_in)
        img_format = (img.format or '').upper()

        # Determine if resize is needed
        w, h = img.size
        max_wh = max(w, h)
        if max_wh > max_dim:
            scale = max_dim / float(max_wh)
            new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
            img = img.resize(new_size, Image.LANCZOS)

        buf_out = io.BytesIO()
        if img_format in ('JPEG', 'JPG'):
            # Ensure no alpha channel for JPEG
            if img.mode in ('RGBA', 'LA'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[-1])
                img = background
            else:
                img = img.convert('RGB')
            img.save(buf_out, format='JPEG', quality=jpeg_quality, optimize=True)
            return buf_out.getvalue(), 'image/jpeg'
        else:
            # For PNG and others, keep PNG to preserve transparency if present
            if img_format == 'PNG' or img.mode in ('RGBA', 'LA'):
                img.save(buf_out, format='PNG', optimize=True)
                return buf_out.getvalue(), 'image/png'
            else:
                # Fallback: save as PNG for wide compatibility
                img.save(buf_out, format='PNG', optimize=True)
                return buf_out.getvalue(), 'image/png'
    except Exception:
        # If anything goes wrong, fall back to original bytes
        return data_bytes, None
    
# Note: The helper above is intentionally conservative: it rescales images only when the
# largest dimension exceeds `max_dim` (default 1600px) and compresses JPEGs at
# `jpeg_quality` (default 85). This keeps images suitable for clear viewing in the
# ticket history while avoiding storing excessively large files. Tune `max_dim` and
# `jpeg_quality` as needed.

# --- PARCHE: Interceptar llamadas a sqlite3.connect para usar la ruta persistente ---
import sqlite3 as _sqlite3_global
_original_sqlite3_connect = _sqlite3_global.connect

def _patched_sqlite3_connect(*args, **kwargs):
    """Intercept sqlite3.connect and remap requests for the repo DB to the persistent DB."""
    # Detect database argument in positional or keyword form
    db_arg = None
    if len(args) > 0:
        db_arg = args[0]
    elif 'database' in kwargs:
        db_arg = kwargs.get('database')

    # If caller already passed the persistent path, call original to avoid recursion
    try:
        if isinstance(db_arg, str) and os.path.abspath(db_arg) == os.path.abspath(DB_DATA_PATH):
            return _original_sqlite3_connect(*args, **kwargs)
    except Exception:
        pass

    # If caller asked for the repository DB filename (e.g. 'helpdesk.db'), remap it
    if isinstance(db_arg, str) and (os.path.basename(db_arg) == DB_FILENAME or db_arg == DB_FILENAME):
        # Replace with persistent path and preserve other args/kwargs
        new_args = list(args)
        if len(new_args) > 0:
            new_args[0] = DB_DATA_PATH
            return get_db_connection(*new_args, **kwargs)
        else:
            kwargs['database'] = DB_DATA_PATH
            return get_db_connection(**kwargs)

    # For any other DB, call original connect
    return _original_sqlite3_connect(*args, **kwargs)

# Apply the patch
_sqlite3_global.connect = _patched_sqlite3_connect

# funcion para calcular dias transucrridos desde la creacion del ticket en horario laboral
def calcular_dias_transcurridos(fecha_creacion):
    if isinstance(fecha_creacion, str):
        fecha_creacion_dt = datetime.strptime(fecha_creacion, '%d-%m-%Y')
    elif isinstance(fecha_creacion, datetime):
        fecha_creacion_dt = fecha_creacion
    else:
        return 0  # o manejar el error como prefieras

    fecha_actual_dt = datetime.now()
    dias_transcurridos = 0
    dia_actual = fecha_creacion_dt

    while dia_actual.date() <= fecha_actual_dt.date():
        if dia_actual.weekday() < 5:  # Lunes a Viernes
            dias_transcurridos += 1
        dia_actual += timedelta(days=1)

    return dias_transcurridos

def obtener_tiempo_primera_respuesta():
    conn = sqlite3.connect('helpdesk.db')
    c = conn.cursor()
    c.execute('''
SELECT 
    (julianday(
        MIN(substr(h.fecha, 7, 4) || '-' || substr(h.fecha, 4, 2) || '-' || substr(h.fecha, 1, 2))
    ) - 
    julianday(
        MIN(substr(t.date_submitted, 7, 4) || '-' || substr(t.date_submitted, 4, 2) || '-' || substr(t.date_submitted, 1, 2))
    )) * 24.0 AS horas_primera_respuesta
FROM 
    tickets t
JOIN 
    historial h ON t.id = h.ticket_id
WHERE 
    h.comentario IS NOT NULL
GROUP BY 
    t.id;

    ''')
    tiempos = c.fetchall()
    conn.close()
    if tiempos:
        return round(np.mean([t[0] for t in tiempos if t[0] is not None]), 2)
    return None

# Funcion para calcular promedio de tiempo de resolución (tickets cerrados)

def obtener_tiempo_promedio():
    conn = sqlite3.connect('helpdesk.db')
    c = conn.cursor()
    c.execute('''
SELECT 
    t.id,
    (julianday(
        MAX(substr(h.fecha, 7, 4) || '-' || substr(h.fecha, 4, 2) || '-' || substr(h.fecha, 1, 2))
    ) - 
    julianday(
        substr(t.date_submitted, 7, 4) || '-' || substr(t.date_submitted, 4, 2) || '-' || substr(t.date_submitted, 1, 2)
    )) * 24.0 AS horas_ultima_respuesta
FROM 
    tickets t
JOIN 
    historial h ON t.id = h.ticket_id
WHERE 
    h.comentario IS NOT NULL
    AND t.status = 'Cerrado'
GROUP BY 
    t.id;
    ''')
    tiempos = c.fetchall()
    conn.close()
    if tiempos:
        return round(np.mean([float(t[1]) for t in tiempos if t[1] is not None]), 2)
    return None


def agregar_comentario(ticket_id, usuario, comentario):
        conn = sqlite3.connect('helpdesk.db')
        c = conn.cursor()
        fecha = datetime.now().strftime("%d-%m-%Y %H:%M")
        c.execute('INSERT INTO historial (ticket_id, fecha, usuario, comentario) VALUES (?, ?, ?, ?)', (ticket_id, fecha, usuario, comentario))
        conn.commit()
        conn.close()

def on_dismiss():
        if result and result.get("moved_deal"):
            moved_id = result["moved_deal"]["deal_id"]
            nuevo_estado = result["moved_deal"]["from_stage"]
            conn = sqlite3.connect('helpdesk.db')
            c = conn.cursor()
            c.execute('UPDATE tickets SET status=? WHERE id=?', (nuevo_estado, moved_id))
            conn.commit()
            conn.close()
            st.success(f"Ticket {moved_id} movido a estado '{nuevo_estado}'")
            # Recargar los tickets desde la base de datos para reflejar el cambio
            rows = obtener_tickets_db()
            st.session_state.df = pd.DataFrame(rows, columns=["ID", "Issue", "Status", "Priority", "Date Submitted", "usuario", "sede", "tipo", "asignado", "email"])
            st.session_state.dialogo_cerrado = True

@st.dialog("📝 Comentario de cierre", width="small", dismissible=True, on_dismiss=on_dismiss)
def mostrar_dialogo_comentario(ticket_id):
    # col1, col2 = st.columns(2)
    comentario = st.text_area(f"Escribe tu comentario para el ticket {ticket_id}")
    #   with col1: 
    if st.button("Guardar comentario"):
        if comentario.strip() == "":
            st.warning("El comentario no puede estar vacío.")
            return
        agregar_comentario(ticket_id, usuario_actual, comentario)
        conn = sqlite3.connect('helpdesk.db')
        c = conn.cursor()
        c.execute('UPDATE tickets SET status=? WHERE id=?', (nuevo_estado, moved_id))
        conn.commit()
        conn.close()
        st.success(f"Ticket {moved_id} movido a estado '{nuevo_estado}'")
        # Recargar los tickets desde la base de datos para reflejar el cambio
        rows = obtener_tickets_db()
        st.session_state.df = pd.DataFrame(rows, columns=["ID", "Issue", "Status", "Priority", "Date Submitted", "usuario", "sede", "tipo", "asignado", "email"])
        try:
            send_email_gmail(
                subject=f"Cambio de estado: {result['moved_deal']['deal_id']} → {nuevo_estado}",
                body=f"Su ticket:\n\nID: {result['moved_deal']['deal_id']}\nUsuario: {username}\n\nha cambiado de estado a '{nuevo_estado}'",
                to_email=email_moved)
            logger.info(f"Email de notificación enviado para ticket {moved_id}")
            st.success(f"✅ Email enviado correctamente a {email_moved}")
        except Exception as e:
            st.warning(f"No se pudo enviar el email de notificación: {e}")
            st.success("Comentario de cierre agregado al historial.")
            time.sleep(2)
        st.session_state.dialogo_cerrado = True
        time.sleep(2)
        st.rerun()
# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

st.set_page_config(page_title="Gestor de Tickets", layout="wide")
st.title("Mesa de ayuda")

# --- Configuración de notificaciones por email (Gmail) ---
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

def obtener_credenciales():
    conn = sqlite3.connect('helpdesk.db')
    c = conn.cursor()
    c.execute('SELECT username, password, rol from usuarios')
    credenciales = c.fetchall()
    conn.close()
    return credenciales

def obtener_usuarios_sistema():
    conn = sqlite3.connect('helpdesk.db')
    c = conn.cursor()
    c.execute('SELECT id, username, password, rol from usuarios')
    credenciales = c.fetchall()
    conn.close()
    return credenciales

# funcion para tener correo de los usarios en base de datos
def obtener_correos_usuarios(nombre_usuario):
    conn = sqlite3.connect('helpdesk.db')
    c = conn.cursor()
    c.execute('SELECT email FROM usuarios WHERE nombre = ?', (nombre_usuario,))
    correos = c.fetchone()
    conn.close()
    return correos[0] if correos else None

def send_email_gmail(subject, body, to_email):
    # Verificar si los emails están habilitados
    if not EMAILS_HABILITADOS:
        print(f"Emails deshabilitados. No se envió: {subject} a {to_email}")
        return False
    
    # Configura estos datos con tu cuenta de Gmail y contraseña de aplicación
    gmail_user = 'eddy.aluminiologo@gmail.com'
    gmail_password = 'iovu vemy ycra nzbx'
    from_email = gmail_user
    msg = MIMEMultipart()
    msg['From'] = from_email
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))
    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(gmail_user, gmail_password)
        text = msg.as_string()
        server.sendmail(from_email, to_email, text)
        server.quit()
        return True
    except Exception as e:
        print(f"Error enviando email: {e}")
        return False

EMAIL_DESTINO_SOPORTE = 'digitalizacion.alu@gmail.com'

# Configuración de la página y título.
st.set_page_config(page_title="Tickets de soporte", page_icon="🎫")
st.title("🎫 Tickets de soporte")

# Funciones para la base de datos
def obtener_tickets_db():
    conn = sqlite3.connect('helpdesk.db')
    c = conn.cursor()
    c.execute('SELECT id, issue, status, priority, date_submitted, usuario, sede, tipo, asignado, email FROM tickets ORDER BY id DESC')
    rows = c.fetchall()
    conn.close()
    return rows

def obtener_sedes_db():
    conn = sqlite3.connect('helpdesk.db')
    c = conn.cursor()
    c.execute('SELECT nombre FROM sedes')
    rows = c.fetchall()
    conn.close()
    return [row[0] for row in rows]

def obtener_tipos_db():
    conn = sqlite3.connect('helpdesk.db')
    c = conn.cursor()
    c.execute('SELECT descripcion FROM tipos_problema')
    rows = c.fetchall()
    conn.close()
    return [row[0] for row in rows]

def obtener_historial(ticket_id):
    """Obtiene el historial (fecha, usuario, comentario) de un ticket."""
    conn = sqlite3.connect('helpdesk.db')
    c = conn.cursor()
    c.execute('SELECT fecha, usuario, comentario FROM historial WHERE ticket_id = ? ORDER BY id ASC', (ticket_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def obtener_cat_por_tipo(tipo):
    conn = sqlite3.connect('helpdesk.db')
    c = conn.cursor()
    c.execute('SELECT categoria, categoria_2, categoria_3 FROM tipos_problema WHERE descripcion = ?', (tipo,))
    row = c.fetchone()
    conn.close()
    return row if row else (None, None, None)

def agregar_ticket_db(issue, priority, usuario, sede, tipo, email):
    conn = sqlite3.connect('helpdesk.db')
    c = conn.cursor()
    c.execute('SELECT id FROM tickets ORDER BY id DESC LIMIT 1')
    last = c.fetchone()
    if last:
        last_num = int(last[0].split('-')[1])
    else:
        last_num = 1000
    new_id = f"TICKET-{last_num+1}"
    today = datetime.now().strftime("%d-%m-%Y")
    c.execute('INSERT INTO tickets (id, issue, status, priority, date_submitted, usuario, sede, tipo, asignado, email) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (new_id, issue, "Abierto", priority, today, usuario, sede, tipo, "", email))
    conn.commit()
    conn.close()
    return new_id, issue, "Abierto", priority, today, usuario, sede, tipo, "", email

def actualizar_tickets_db(df):
    conn = sqlite3.connect('helpdesk.db')
    c = conn.cursor()
    for _, row in df.iterrows():
        # Solo actualiza el estado si ha cambiado
        c.execute('''UPDATE tickets SET status=? WHERE id=?''', (row['Status'], row['ID']))
    conn.commit()
    conn.close()

def actualizar_estado_ticket(ticket_id, nuevo_estado):
    try:
        conn = sqlite3.connect('helpdesk.db')
        c = conn.cursor()
        c.execute('UPDATE tickets SET status=? WHERE id=?', (nuevo_estado, ticket_id))
        conn.commit()
        return c.rowcount
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

# Crear un dataframe de Pandas con tickets existentes aleatorios.
if "df" not in st.session_state:
    rows = obtener_tickets_db()
    df = pd.DataFrame(rows, columns=["ID", "Issue", "Status", "Priority", "Date Submitted", "usuario", "sede", "tipo", "asignado", "email"])
    st.session_state.df = df

# Selección de rol al inicio
rol = st.sidebar.selectbox(
    "Selecciona tu rol",
    ["Usuario", "Soporte", "Admin", "Config"],
    help="Elige si eres usuario final, personal de soporte o administrador"
)

if rol == "Usuario":
    #tab1, tab2 = st.tabs(["Enviar Ticket", "Tickets pendientes"])
   # with tab1:
        # Inicializar session_state
        if 'tipo_seleccionado' not in st.session_state:
            st.session_state.tipo_seleccionado = "Seleccione"
        
        # Selector de tipo FUERA del form para que haga rerun automático
        st.subheader("Seleccionar tipo de ticket")
        tipos_disponibles = obtener_tipos_db()
        opciones_tipo = ["Seleccione"] + tipos_disponibles
        
        tipo_seleccionado = st.selectbox(
            "Tipo de ticket", 
            opciones_tipo,
            index=0,
            key="selector_tipo_principal",
            help='Categoria a la cual se va a relacionar el ticket'
        )
        
        # Actualizar session_state
        if tipo_seleccionado != st.session_state.tipo_seleccionado:
            st.session_state.tipo_seleccionado = tipo_seleccionado
            st.rerun()
        
        # Mostrar el form solo si se seleccionó un tipo válido
        if st.session_state.tipo_seleccionado != "Seleccione":
            # Ahora el form con las categorías que dependen del tipo seleccionado
            with st.form("add_ticket_form"):
                usuario = st.text_input("Usuario", placeholder="Nombre y Apellido")
                email = st.text_input("Email", placeholder="Correo electronico")
                sede = st.selectbox("Seleccionar sede", obtener_sedes_db())
                
                # Mostrar el tipo seleccionado (solo lectura)
                st.text_input("Tipo de ticket seleccionado", 
                            value=st.session_state.tipo_seleccionado, 
                            disabled=True)
                
                # Categorías basadas en el tipo seleccionado
                categorias_opciones = obtener_cat_por_tipo(st.session_state.tipo_seleccionado)
                categorias = st.selectbox("Categoría", categorias_opciones)
                
                issue = st.text_area("Describe el problema")
                priority = st.selectbox("Prioridad", ["Alta", "Media", "Baja"])
                archivo_usuario = st.file_uploader("Adjuntar archivo (opcional)", type=["jpg", "jpeg", "png"], key="file_usuario")
                submitted = st.form_submit_button("Enviar ticket")

            if submitted and usuario and email and sede and categorias and issue and priority:
                # Concatenar tipo y categoría
                tipo_categoria = f"{st.session_state.tipo_seleccionado} - {categorias}"
                
                new_ticket = agregar_ticket_db(issue, priority, usuario, sede, tipo_categoria, email)
                df_new = pd.DataFrame([
                    {
                        "ID": new_ticket[0],
                        "Issue": new_ticket[1],
                        "Status": new_ticket[2],
                        "Priority": new_ticket[3],
                        "Date Submitted": new_ticket[4],
                        "usuario": new_ticket[5],
                        "sede": new_ticket[6],
                        "tipo": new_ticket[7],
                        "email": new_ticket[9],
                    }
                ])
                st.toast("¡Ticket enviado!", icon="🎉", duration="short")
                
                # Opcional: limpiar el form después de enviar
                st.session_state.tipo_seleccionado = "Seleccione"
                st.markdown("Detalles:")
                st.dataframe(df_new, use_container_width=True, hide_index=True)
                # Guardar archivo adjunto en base de datos si existe
                if archivo_usuario is not None:
                    import mimetypes
                    nombre_archivo = archivo_usuario.name
                    tipo_mime = mimetypes.guess_type(nombre_archivo)[0] or archivo_usuario.type or "application/octet-stream"
                    raw_bytes = archivo_usuario.getbuffer().tobytes()
                    fecha = datetime.now().strftime("%d-%m-%Y %H:%M")
                    usuario_adj = usuario or "Usuario"

                    # If it's an image, attempt to process (resize/compress) while preserving quality
                    contenido = raw_bytes
                    processed_mime = None
                    if tipo_mime and tipo_mime.startswith('image'):
                        contenido, processed_mime = process_image_bytes(raw_bytes, filename=nombre_archivo)
                        if processed_mime:
                            tipo_mime = processed_mime

                    # Guardar en base de datos
                    conn = sqlite3.connect('helpdesk.db')
                    c = conn.cursor()
                    c.execute('INSERT INTO adjuntos (ticket_id, nombre_archivo, tipo_mime, contenido, fecha, usuario) VALUES (?, ?, ?, ?, ?, ?)',
                            (new_ticket[0], nombre_archivo, tipo_mime, contenido, fecha, usuario_adj))
                    conn.commit()
                    conn.close()
                    # Registrar en historial
                    agregar_comentario(new_ticket[0], usuario_adj, f"[Archivo adjunto BD]({nombre_archivo})")
                    st.success(f"Archivo '{archivo_usuario.name}' adjuntado al ticket.")
                # Notificación por email a soporte
                try:
                    # Enviar al admin
                    send_email_gmail(
                        subject=f"Nuevo ticket creado: {new_ticket[0]}",
                        body=f"Se ha creado un nuevo ticket:\n\nID: {new_ticket[0]}\nUsuario: {usuario}\nSede: {sede}\nTipo: {tipo_categoria}\nPrioridad: {priority}\nDescripción: {issue}",
                        to_email=EMAIL_DESTINO_SOPORTE
                    )
                except Exception as e:
                    st.warning(f"No se pudo enviar el email de notificación al admin: {e}")
                try:
                    # Enviar copia al creador del ticket si su email parece válido
                    if isinstance(email, str) and '@' in email:
                        send_email_gmail(
                            subject=f"Confirmación de ticket creado: {new_ticket[0]}",
                            body=f"Su ticket ha sido creado correctamente:\n\nID: {new_ticket[0]}\nAsunto: {issue}\nEstado: Abierto\nPrioridad: {priority}\nSede: {sede}\nTipo: {tipo_categoria}\n\nGracias por contactarnos. El equipo de soporte lo contactará pronto.",
                            to_email=email
                        )
                except Exception as e:
                    st.warning(f"No se pudo enviar el email de confirmación al creador: {e}")
                # Recargar los tickets desde la base de datos
                rows = obtener_tickets_db()
                st.session_state.df = pd.DataFrame(rows, columns=["ID", "Issue", "Status", "Priority", "Date Submitted", "usuario", "sede", "tipo", "asignado", "email"])
            else:
                st.warning("Debe llenar todos los campos obligatorios.")
        # (Se eliminó la sección de consulta de tickets por pedido del cliente)
    # with tab2:
    #     st.header("Tickets enviados")
        
    #     # Filtro por usuario
    #     usuario_filtro = st.text_input("Filtrar por usuario", placeholder="Nombre y Apellido")
    #     if usuario_filtro:
    #         df_usuario = st.session_state.df[st.session_state.df['usuario'].str.contains(usuario_filtro, case=False, na=False)]
    #     else:
    #         df_usuario = st.session_state.df[st.session_state.df['tipo'] != 'archivado'].copy()
        
    #     # Métricas principales
    #     col1, col2, col3, col4 = st.columns(4)
    #     with col1:
    #         st.metric("Total tickets", len(df_usuario))
    #     with col2:
    #         tickets_abiertos = len(df_usuario[df_usuario['Status'].isin(['Abierto', 'Open', 'Pendiente'])])
    #         st.metric("Tickets abiertos", tickets_abiertos)
    #     with col3:
    #         tickets_urgentes = len(df_usuario[df_usuario['Priority'] == 'Alta'])
    #         st.metric("Prioridad alta", tickets_urgentes)
    #     with col4:
    #         tickets_resueltos = len(df_usuario[df_usuario['Status'].isin(['Resuelto', 'Cerrado', 'Closed'])])
    #         st.metric("Resueltos", tickets_resueltos)
        
    #     # Gráficos de distribución
    #     col1, col2 = st.columns(2)
        
    #     with col1:
    #         # Distribución por tipo
    #         tipo_counts = df_usuario['tipo'].value_counts()
    #         if not tipo_counts.empty:
    #             fig_tipo = px.pie(values=tipo_counts.values, names=tipo_counts.index, 
    #                             title="Distribución por Tipo")
    #             st.plotly_chart(fig_tipo, use_container_width=True)
        
    #     with col2:
    #         # Distribución por prioridad
    #         prioridad_counts = df_usuario['Priority'].value_counts()
    #         if not prioridad_counts.empty:
    #             fig_prioridad = px.bar(x=prioridad_counts.index, y=prioridad_counts.values,
    #                                 title="Tickets por Prioridad", 
    #                                 labels={'x': 'Prioridad', 'y': 'Cantidad'})
    #             st.plotly_chart(fig_prioridad, use_container_width=True)
        

    #     # Tabla detallada
    #     st.subheader("Lista detallada de tickets")
    #     gb = GridOptionsBuilder.from_dataframe(df_usuario)

    #     # Ocultar todas las columnas excepto las 2 que quieres mostrar
    #     columnas_a_ocultar = [col for col in df_usuario.columns if col not in ['ID', 'Issue', 'tipo']]  
    #     for col in columnas_a_ocultar:
    #         gb.configure_column(col, hide=True)

    #     gb.configure_pagination(paginationAutoPageSize=True)
    #     gb.configure_side_bar()
    #     gb.configure_selection('single')
    #     gridOptions = gb.build()

    #     grid_response = AgGrid(
    #         df_usuario,
    #         gridOptions=gridOptions,
    #         enable_enterprise_modules=True,
    #         update_mode=GridUpdateMode.SELECTION_CHANGED,
    #         theme='streamlit',
    #         height=400,
    #         fit_columns_on_grid_load=True,
    #         allow_unsafe_jscode=True,
    #     )
        
    #     # Detalles del ticket seleccionado
    #     selected_rows = grid_response['selected_rows']
    #     if selected_rows is not None and not selected_rows.empty:
    #         selected_ticket = selected_rows.iloc[0]
            
    #         # Color según prioridad
    #         color_prioridad = {
    #             'Alta': '#ff4b4b',
    #             'Media': '#ffa500', 
    #             'Baja': '#00cc00'
    #         }.get(selected_ticket['Priority'], '#6c757d')
            
    #         # Solo el encabezado en HTML
    #         st.markdown(f"""
    #         <div style='
    #             background: linear-gradient(135deg, #f8f9fa, #e9ecef);
    #             border-left: 5px solid {color_prioridad};
    #             border-radius: 10px 10px 0 0;
    #             padding: 20px 20px 10px 20px;
    #             margin: 10px 0 0 0;
    #             box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    #         '>
    #             <div style='display: flex; justify-content: space-between; align-items: center;'>
    #                 <h3 style='margin: 0; color: #2c3e50;'>Ticket #{selected_ticket['ID']}</h3>
    #                 <span style='background-color: {color_prioridad}; color: white; padding: 5px 10px; border-radius: 15px; font-size: 12px;'>
    #                     {selected_ticket['Priority']}
    #                 </span>
    #             </div>
    #         </div>
    #         """, unsafe_allow_html=True)
            
    #         # Contenido en Streamlit nativo dentro de un container con estilo
    #         with st.container():
    #             st.markdown(f"### {selected_ticket['Issue']}")
                
    #             # Icono según estado
    #             icono_estado = {
    #                 'Abierto': '⏳',
    #                 'Open': '⏳',
    #                 'Pendiente': '⏳',
    #                 'Resuelto': '✅',
    #                 'Cerrado': '✅',
    #                 'Closed': '✅',
    #                 'archivado': '📁'
    #             }.get(selected_ticket['Status'], '📄')
                
    #             # Información en columnas
    #             col1, col2 = st.columns(2)
    #             with col1:
    #                 st.write(f"**{icono_estado} Estado:** {selected_ticket['Status']}")
    #                 st.write(f"**📊 Tipo:** {selected_ticket['tipo']}")
    #                 st.write(f"**🏢 Sede:** {selected_ticket['sede']}")
    #             with col2:
    #                 st.write(f"**📅 Fecha:** {selected_ticket['Date Submitted']}")
    #                 st.write(f"**👤 Asignado a:** {selected_ticket['asignado'] if selected_ticket['asignado'] else 'No asignado'}")
    #                 st.write(f"**👥 Usuario:** {selected_ticket['usuario']}")
                
    #             # Línea separadora
    #             st.divider()
                
    #     else:
    #         st.info("Seleccione un ticket para ver los detalles.")
elif rol == "Soporte":
    # Autenticación simple para soporte
    if "auth_soporte" not in st.session_state:
        st.session_state.auth_soporte = False
        st.session_state.user_soporte = None
    if not st.session_state.auth_soporte:
        st.header("Acceso restringido para soporte")
        with st.form("login_soporte"):
            user = st.text_input("Usuario")
            pwd = st.text_input("Contraseña", type="password")
            login = st.form_submit_button("Iniciar sesión")
        if login:
            for user_bd, pwd_bd, rol_bd in obtener_credenciales():
                if user == user_bd and pwd == pwd_bd and rol_bd != "admin":
                    st.session_state.auth_soporte = True
                    st.session_state.user_soporte = user_bd
                    st.success(f"Acceso concedido. Bienvenido, {st.session_state.user_soporte}.")
                    time.sleep(1)
                    st.rerun()
                if user == user_bd and pwd == pwd_bd and rol_bd == "admin":
                    st.warning("Los usuarios administradores no tienen acceso al soporte.")
            else:
                st.error("Usuario o contraseña incorrectos.")
        st.stop()
    st.info(f"Sesion de Usuario: {st.session_state.user_soporte}")
    usuario_actual = st.session_state.user_soporte
    st.header("Gestión de tickets de soporte")
    # boton de cerrar sesion
    if st.sidebar.button("Cerrar sesión"):
        st.session_state.auth_soporte = False
        st.session_state.user = None
        st.success("Cerrando sesión... Hasta luego.")
        time.sleep(1)
        st.rerun()
    import pandas as pd
    from streamlit_kanban_board_goviceversa import kanban_board
    st.sidebar.markdown("---")
    st.sidebar.subheader("Configuración de Emails")
    
    # Botón para habilitar/deshabilitar emails
    emails_habilitados = st.sidebar.toggle(
        "Envío de emails habilitado", 
        value=EMAILS_HABILITADOS,
        key="toggle_emails"
    )
    
    # Actualizar la variable global
    EMAILS_HABILITADOS = emails_habilitados
    
    if emails_habilitados:
        st.sidebar.success("✓ Emails habilitados")
    else:
        st.sidebar.warning("✗ Emails deshabilitados")
    # Definir los estados para el tablero Kanban
    def get_priority_color(priority):
        if priority.lower() == "alta":
            return "red"
        elif priority.lower() == "media":
            return "orange"
        elif priority.lower() == "baja":
            return "green"
        else:
            return "gray"  # Por si hay valores inesperados

    stages = [
        {"id": "Abierto", "name": "Abierto", "color": "#FF5555"},
        {"id": "En progreso", "name": "En progreso", "color": "#FFD700"},
        {"id": "Cerrado", "name": "Cerrado", "color": "#55FF55"},
    ]
    df = st.session_state.df.copy()
    df_filtrado = df[df["asignado"] == usuario_actual]
    df_filtrado = df_filtrado[df_filtrado["tipo"] != "archivado"]  # No mostrar archivados en Kanban

    # --- Filtro adicional por encima del Kanban ---
    with st.expander("Filtros avanzados", expanded=False):
        col1, col2, col3 = st.columns(3)
        with col1:
            estado_sel = st.multiselect("Estado", options=sorted(df_filtrado['Status'].dropna().unique()), default=[], key='f_estado')
            prioridad_sel = st.multiselect("Prioridad", options=sorted(df_filtrado['Priority'].dropna().unique()), default=[], key='f_prioridad')
        with col2:
            sede_sel = st.multiselect("Sede", options=sorted(df_filtrado['sede'].dropna().unique()), default=[], key='f_sede')
            tipo_sel = st.multiselect("Tipo", options=sorted(df_filtrado['tipo'].dropna().unique()), default=[], key='f_tipo')
        with col3:
            asignado_sel = st.multiselect("Asignado", options=sorted(df_filtrado['asignado'].dropna().unique()), default=[], key='f_asignado')
            texto_busqueda = st.text_input("Buscar (texto libre)", value="", key='f_texto')

            # Nuevo: rango de fechas y filtro por antigüedad (días)
            today = datetime.now().date()
            # default start = Jan 1 of current year
            default_start = datetime(today.year, 1, 1).date()
        try:
            fecha_range = st.date_input("Rango fecha envío (inicio - fin)", value=(default_start, today), key='f_fecha_range')
        except Exception:
            # En versiones antiguas de Streamlit, date_input puede devolver lista
            fecha_range = st.date_input("Rango fecha envío (inicio - fin)", key='f_fecha_range')

        antig_max = st.slider("Antigüedad máxima (días)", min_value=0, max_value=365, value=365, key='f_antiguedad')

        # Botón para resetear filtros y contador
        c_left, c_mid, c_right = st.columns([1,1,2])
        with c_left:
            if st.button("Reset filtros", key='f_reset'):
                for k in ['f_estado','f_prioridad','f_sede','f_tipo','f_asignado','f_texto','f_fecha_range','f_antiguedad']:
                    if k in st.session_state:
                        del st.session_state[k]
                st.rerun()
        with c_mid:
            st.markdown(f"**Tickets mostrados:** {len(df_filtrado)}")
        with c_right:
            st.write("")

        # Aplicar filtros seleccionados
        if estado_sel:
            df_filtrado = df_filtrado[df_filtrado['Status'].isin(estado_sel)]
        if prioridad_sel:
            df_filtrado = df_filtrado[df_filtrado['Priority'].isin(prioridad_sel)]
        if sede_sel:
            df_filtrado = df_filtrado[df_filtrado['sede'].isin(sede_sel)]
        if tipo_sel:
            df_filtrado = df_filtrado[df_filtrado['tipo'].isin(tipo_sel)]
        if asignado_sel:
            df_filtrado = df_filtrado[df_filtrado['asignado'].isin(asignado_sel)]
        if texto_busqueda and texto_busqueda.strip():
            q = texto_busqueda.strip().lower()
            df_filtrado = df_filtrado[df_filtrado.apply(lambda r: q in (str(r['Issue']) + ' ' + str(r['usuario']) + ' ' + str(r.get('email',''))).lower(), axis=1)]

        # Aplicar filtro por rango de fechas y antigüedad
        # Crear columna temporal con datetimes
        if 'Date Submitted' in df_filtrado.columns:
            df_filtrado = df_filtrado.copy()
            df_filtrado['__date_dt'] = pd.to_datetime(df_filtrado['Date Submitted'], dayfirst=True, errors='coerce')
            # fecha_range puede ser tuple (start, end) o single date
            try:
                if isinstance(fecha_range, (list, tuple)) and len(fecha_range) == 2:
                    start_d = pd.to_datetime(fecha_range[0])
                    end_d = pd.to_datetime(fecha_range[1])
                    df_filtrado = df_filtrado[(df_filtrado['__date_dt'] >= pd.Timestamp(start_d)) & (df_filtrado['__date_dt'] <= pd.Timestamp(end_d) + pd.Timedelta(days=1))]
                elif fecha_range:
                    single = pd.to_datetime(fecha_range)
                    df_filtrado = df_filtrado[df_filtrado['__date_dt'].dt.date == single.date()]
            except Exception:
                pass

            # Antigüedad: calcular días desde la fecha y filtrar
            if antig_max is not None:
                ahora = pd.Timestamp(datetime.now())
                df_filtrado['__dias'] = (ahora - df_filtrado['__date_dt']).dt.days
                df_filtrado = df_filtrado[df_filtrado['__dias'].notna() & (df_filtrado['__dias'] <= int(antig_max))]
    # Adaptar los tickets al formato del kanban_board
    deals = [
        {
            "id": row["ID"],
            "stage": row["Status"],
            "deal_id": row["ID"],
            "company_name": row['sede'] or "null",
            "product_type": row["Issue"] or "",
            "date": row["Date Submitted"],
            "underwriter": row["usuario"] or "",
            "currency": row["Priority"],
            "email": row["email"],
            #"source": "VV",
            "type": row["tipo"],
            "custom_html": f"""
    <div style='display: flex; flex-direction: row; align-items: center; justify-content: space-between;'>
        <p style='color:{get_priority_color(row["Priority"])}; margin:0; padding:0;'>
            Prioridad: {row["Priority"]}
        </p>
        <span style='background:#e0e0e0;border-radius:4px;padding:2px 6px;font-size:12px;margin-left:10px; color: black'>
            💻 {row["asignado"] if row["asignado"] else "No asignado"}
        </span>
    </div>    
    <div>
        <p style='color: black; font-size: 12px; background-color: lightyellow; border-radius: 15px; text-align: center;'>Email: {row['email']}</p>
    </div>
    <div>
        <p style='color: black; font-size: 12px; background-color: lightgreen; border-radius: 15px; text-align: center;'>Proposito: {row['tipo']}</p>
    </div>
        <p style='color: black; font-size: 12px; background-color: lightblue; border-radius: 15px; text-align: center;'>Dias transcurridos: {calcular_dias_transcurridos(row["Date Submitted"])}</p>
    </div>


"""
        }
        for _, row in df_filtrado.iterrows()
    ]
    permission_matrix = {
        "Soporte": {
            "stages": {
                "Abierto": {
                    "view": True,
                    "drag_to": True,
                    "drag_from": True,
                    "approve": True,
                    "reject": True,
                    "edit": True
                },
                "En progreso": {
                    "view": True,
                    "drag_to": True,
                    "drag_from": True,
                    "approve": False,
                    "reject": False,
                    "edit": True
                },
                "Cerrado": {
                    "view": True,
                    "drag_to": True,
                    "drag_from": False,
                    "approve": False,
                    "reject": False,
                    "edit": False
                }
            },
            "actions": {
                "create_deal": True,
                "delete_deal": False,
                "edit_deal": True,
                "approve_deal": True,
                "reject_deal": True,
                "request_info": True
            },
            "approval_limits": {
                "VV": {"EUR": 100000},
                "OF": {"EUR": 150000}
            }
        }
    }


    user_info = {
        "role": "Soporte",
        "email": "risk@company.com",
        "permissions": ["risk_approval", "management_approval"],
        "approval_limits": {"VV": {"EUR": 100000}, "OF": {"EUR": 150000}},
        "department": "Risk Management",
        "is_active": True
    }
    # Badge compacto mostrando rango de fechas y antigüedad activos
    try:
        fr = fecha_range
        if isinstance(fr, (list, tuple)) and len(fr) == 2:
            fstart = fr[0]
            fend = fr[1]
        else:
            fstart = fr
            fend = fr
        fecha_badge = f"{fstart.strftime('%d-%m-%Y')} → {fend.strftime('%d-%m-%Y')}"
    except Exception:
        fecha_badge = "Todos"
    antig_badge = f"Antig ≤ {int(antig_max)} días" if 'antig_max' in locals() and antig_max is not None else "Antig: Todos"
    st.markdown(f"**Filtros activos:** {fecha_badge} · {antig_badge} · Tickets: **{len(df_filtrado)}**")

    # Expander con tabla detallada y AgGrid interactiva
    with st.expander("Vista detallada (tabla)", expanded=False):
        st.write(f"Mostrando {len(df_filtrado)} tickets filtrados")
        # Selector de columnas para exportar
        cols = list(df_filtrado.columns)
        cols_sel = st.multiselect("Columnas a mostrar/ exportar", options=cols, default=cols, key='cols_sel_soporte')

        # AgGrid interactive table with pagination and row selection
        try:
            from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode
            gb = GridOptionsBuilder.from_dataframe(df_filtrado[cols_sel].reset_index(drop=True))
            gb.configure_pagination(paginationAutoPageSize=False, paginationPageSize=25)
            gb.configure_selection(selection_mode='multiple', use_checkbox=True)
            gb.configure_side_bar()
            gridOptions = gb.build()
            grid_response = AgGrid(
                df_filtrado[cols_sel].reset_index(drop=True),
                gridOptions=gridOptions,
                enable_enterprise_modules=False,
                update_mode=GridUpdateMode.MODEL_CHANGED,
                theme='streamlit',
                height=400,
                fit_columns_on_grid_load=True,
            )

            selected = grid_response.get('selected_rows', [])
            # Convert selected to DataFrame
            selected_df = pd.DataFrame(selected) if selected else pd.DataFrame()

            # Export buttons: selected or all
            col_export_1, col_export_2 = st.columns(2)
            with col_export_1:
                if not selected_df.empty:
                    csv_sel = selected_df.to_csv(index=False).encode('utf-8')
                    st.download_button("📥 Descargar CSV (seleccionados)", data=csv_sel, file_name="tickets_seleccionados.csv", mime='text/csv')
                else:
                    csv_all = df_filtrado[cols_sel].to_csv(index=False).encode('utf-8')
                    st.download_button("📥 Descargar CSV (filtrados)", data=csv_all, file_name="tickets_filtrados.csv", mime='text/csv')
            with col_export_2:
                try:
                    towrite = BytesIO()
                    if not selected_df.empty:
                        selected_df.to_excel(towrite, index=False, engine='openpyxl')
                    else:
                        df_filtrado[cols_sel].to_excel(towrite, index=False, engine='openpyxl')
                    towrite.seek(0)
                    st.download_button("📥 Descargar XLSX", data=towrite, file_name="tickets.xlsx", mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                except Exception:
                    # fallback without engine
                    towrite = BytesIO()
                    if not selected_df.empty:
                        selected_df.to_excel(towrite, index=False)
                    else:
                        df_filtrado[cols_sel].to_excel(towrite, index=False)
                    towrite.seek(0)
                    st.download_button("📥 Descargar XLSX (fallback)", data=towrite, file_name="tickets.xlsx", mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        except Exception as e:
            st.warning(f"st_aggrid no disponible o error al renderizar AgGrid: {e}")
            # Fallback to plain dataframe and existing downloads
            st.dataframe(df_filtrado[cols_sel].reset_index(drop=True), use_container_width=True)
            try:
                csv = df_filtrado[cols_sel].to_csv(index=False).encode('utf-8')
                st.download_button("📥 Descargar CSV (filtrados)", data=csv, file_name="tickets_filtrados.csv", mime='text/csv')
                towrite = BytesIO()
                try:
                    df_filtrado[cols_sel].to_excel(towrite, index=False, engine='openpyxl')
                except Exception:
                    df_filtrado[cols_sel].to_excel(towrite, index=False)
                towrite.seek(0)
                st.download_button("📥 Descargar XLSX (filtrados)", data=towrite, file_name="tickets_filtrados.xlsx", mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            except Exception as e2:
                st.warning(f"No se pudo preparar la descarga: {e2}")

    result = kanban_board(
        stages=stages,
        deals=deals,
        user_info=user_info,
        permission_matrix=permission_matrix,
        show_tooltips=True,
        key="kanban_tickets"
    )

    selected_ticket_id = result.get("clicked_deal")
    if 'dialogo_cerrado' not in st.session_state:
        st.session_state.dialogo_cerrado = False 
    # Procesar cambios de estado
    if result and result.get("moved_deal"):
        # Obtener detalles del ticket movido
        moved_id = result["moved_deal"]["deal_id"]
        # Buscar el deal original
        moved_deal_full = next((d for d in deals if d["deal_id"] == moved_id), None)
        if moved_deal_full:
            email_moved = moved_deal_full.get("email", "No disponible")
            username = moved_deal_full.get("underwriter", "Usuario")
        else:
            st.warning("No se encontró el ticket movido en la lista de deals.")
        nuevo_estado = result["moved_deal"]["to_stage"]
        if (nuevo_estado == "Cerrado" and moved_id not in df[df["Status"] == "Cerrado"]["ID"].values and not st.session_state.dialogo_cerrado):
            mostrar_dialogo_comentario(moved_id)
            st.stop()
        else:
            st.session_state.dialogo_cerrado = False
        if nuevo_estado != "Cerrado" or (nuevo_estado == "Cerrado" and st.session_state.dialogo_cerrado):
            conn = sqlite3.connect('helpdesk.db')
            c = conn.cursor()
            c.execute('UPDATE tickets SET status=? WHERE id=?', (nuevo_estado, moved_id))
            conn.commit()
            conn.close()
            st.success(f"Ticket {moved_id} movido a estado '{nuevo_estado}'")
            # Recargar los tickets desde la base de datos para reflejar el cambio
            rows = obtener_tickets_db()
            st.session_state.df = pd.DataFrame(rows, columns=["ID", "Issue", "Status", "Priority", "Date Submitted", "usuario", "sede", "tipo", "asignado", "email"])
            try:
                # Obtener info del ticket
                conn = sqlite3.connect('helpdesk.db')
                c = conn.cursor()
                c.execute('SELECT issue, priority, email FROM tickets WHERE id = ?', (moved_id,))
                trow = c.fetchone()
                conn.close()
                issue = trow[0] if trow else ''
                priority = trow[1] if trow else ''
                ticket_email = trow[2] if trow else email_moved

                subject = f"Cambio de estado: {moved_id} → {nuevo_estado}"
                body = f"Su ticket ha cambiado de estado:\n\nID: {moved_id}\nAsunto: {issue}\nPrioridad: {priority}\nNuevo estado: {nuevo_estado}\n\n"

                # Si se cierra, adjuntar el último comentario de cierre si existe
                if nuevo_estado.lower() == 'cerrado':
                    try:
                        conn = sqlite3.connect('helpdesk.db')
                        c = conn.cursor()
                        c.execute('SELECT comentario, usuario, fecha FROM historial WHERE ticket_id = ? ORDER BY id DESC LIMIT 1', (moved_id,))
                        last = c.fetchone()
                        conn.close()
                        if last and last[0]:
                            comentario_cierre = last[0]
                            usuario_cierre = last[1] or 'Soporte'
                            fecha_cierre = last[2] or ''
                            body += f"Comentario de cierre ({fecha_cierre}) por {usuario_cierre}:\n{comentario_cierre}\n\n"
                    except Exception:
                        pass

                # Enviar al creador si tiene email
                if ticket_email and '@' in ticket_email:
                    send_email_gmail(subject=subject, body=body, to_email=ticket_email)

                # Si el cambio fue hecho por soporte (estamos en la sección Soporte), enviar copia al admin
                try:
                    # Asumimos usuario_actual es el soporte que está logueado
                    if 'user_soporte' in st.session_state and st.session_state.user_soporte:
                        send_email_gmail(subject=subject, body=f"[COPIA_SOPORTE]\n{body}", to_email=EMAIL_DESTINO_SOPORTE)
                except Exception:
                    pass

                logger.info(f"Email de notificación enviado para ticket {moved_id}")
                st.success(f"✅ Email enviado correctamente a {ticket_email}")
            except Exception as e:
                st.warning(f"No se pudo enviar el email de notificación: {e}")
        # Recargar los tickets desde la base de datos para reflejar el cambio
        rows = obtener_tickets_db()
        st.session_state.df = pd.DataFrame(rows, columns=["ID", "Issue", "Status", "Priority", "Date Submitted", "usuario", "sede", "tipo", "asignado", "email"])
    # Mostrar detalles si se selecciona un ticket


    if result and result.get("clicked_deal"):
        st.info(f"Ticket seleccionado: {result['clicked_deal']['deal_id']}")
        with st.expander("Detalles del ticket", icon='📋'):
            st.write("🆔 ID:", result["clicked_deal"]["id"])
            st.write("🏢 Sede:", result["clicked_deal"]["company_name"])
            st.write("📦 Tipo de producto:", result["clicked_deal"]["product_type"])
            st.write("📅 Fecha:", result["clicked_deal"]["date"])
            st.write("👤 Usuario:", result["clicked_deal"]["underwriter"])
            st.write("⚠️ Prioridad:", result["clicked_deal"]["currency"])
            st.write("🎯 Proposito", result["clicked_deal"]["type"])
            st.write()
    def obtener_historial(ticket_id):
            conn = sqlite3.connect('helpdesk.db')
            c = conn.cursor()
            c.execute('SELECT fecha, usuario, comentario FROM historial WHERE ticket_id = ? ORDER BY id ASC', (ticket_id,))
            rows = c.fetchall()
            conn.close()
            return rows

    st.markdown("---")
    if result.get("clicked_deal"):
        st.subheader(f"Historial, comentarios y adjuntos del ticket {result['clicked_deal']['deal_id']}")
        historial = obtener_historial(result['clicked_deal']['deal_id'])
        import os
        from urllib.parse import unquote
        if historial:
            adjuntos_mostrados = set()
            adjuntos_disco_mostrados = set()
            with st.expander("🗨️ Historial de comentarios"):
                for h in historial:
                    fecha, usuario_hist, comentario = h
                    # Detectar si es un adjunto en base de datos
                    if comentario.startswith("[Archivo adjunto BD](") and comentario.endswith(")"):
                        nombre_archivo = comentario[len("[Archivo adjunto BD]("): -1]
                        nombre_archivo = unquote(nombre_archivo)
                        if nombre_archivo in adjuntos_mostrados:
                            continue
                        adjuntos_mostrados.add(nombre_archivo)
                        # Recuperar adjunto de la base de datos
                        conn = sqlite3.connect('helpdesk.db')
                        c = conn.cursor()
                        c.execute('SELECT tipo_mime, contenido FROM adjuntos WHERE ticket_id = ? AND nombre_archivo = ? ORDER BY id DESC LIMIT 1', (result['clicked_deal']['deal_id'], nombre_archivo))
                        adj = c.fetchone()
                        conn.close()
                        st.info(f"[{fecha}] {usuario_hist if usuario_hist else 'Soporte'}: Archivo adjunto: {nombre_archivo}")
                        if adj:
                            tipo_mime, contenido = adj
                            ext = os.path.splitext(nombre_archivo)[1].lower()
                            # Imágenes
                            if tipo_mime and tipo_mime.startswith("image"):
                                import io
                                st.image(io.BytesIO(contenido), caption=nombre_archivo, width=200)
                            # Videos
                            elif tipo_mime and tipo_mime.startswith("video"):
                                import io
                                st.video(io.BytesIO(contenido))
                            # PDFs: mostrar inline si es posible
                            elif ext == '.pdf' or (tipo_mime and tipo_mime == 'application/pdf'):
                                # Intentar mostrar inline; si falla, hacer fallback a iframe y finalmente a descarga
                                try:
                                    from io import BytesIO
                                    if hasattr(st, 'pdf'):
                                        st.pdf(BytesIO(contenido), height=600)
                                    else:
                                        import base64
                                        b64 = base64.b64encode(contenido).decode('utf-8')
                                        pdf_display = f'<iframe src="data:application/pdf;base64,{b64}" width="100%" height="600" type="application/pdf"></iframe>'
                                        components.html(pdf_display, height=600, scrolling=True)
                                    # También ofrecer descarga directa
                                    st.download_button(f"Descargar {nombre_archivo}", data=contenido, file_name=nombre_archivo)
                                except Exception as e:
                                    st.warning(f"No se pudo renderizar PDF inline: {e}")
                                    try:
                                        import base64
                                        b64 = base64.b64encode(contenido).decode('utf-8')
                                        pdf_display = f'<iframe src="data:application/pdf;base64,{b64}" width="100%" height="600" type="application/pdf"></iframe>'
                                        components.html(pdf_display, height=600, scrolling=True)
                                        st.download_button(f"Descargar {nombre_archivo}", data=contenido, file_name=nombre_archivo)
                                    except Exception as e2:
                                        st.error(f"No se pudo mostrar el PDF (fallback falló): {e2}")
                                        st.download_button(f"Descargar {nombre_archivo}", data=contenido, file_name=nombre_archivo)
                            # Otros tipos: ofrecer descarga
                            else:
                                st.download_button(f"Descargar {nombre_archivo}", data=contenido, file_name=nombre_archivo)
                        else:
                            st.warning(f"Archivo adjunto no encontrado en la base de datos: {nombre_archivo}")
                    # Detectar si es un adjunto en disco (legacy)
                    elif comentario.startswith("[Archivo adjunto](") and comentario.endswith(")"):
                        ruta = comentario[len("[Archivo adjunto]("): -1]
                        ruta = unquote(ruta)
                        nombre_archivo = os.path.basename(ruta)
                        if nombre_archivo in adjuntos_disco_mostrados:
                            continue
                        adjuntos_disco_mostrados.add(nombre_archivo)
                        ext = os.path.splitext(nombre_archivo)[1].lower()
                        st.info(f"[{fecha}] {usuario_hist if usuario_hist else 'Soporte'}: Archivo adjunto: {nombre_archivo}")
                        if os.path.exists(ruta):
                            if ext in [".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"]:
                                st.image(ruta, caption=nombre_archivo, width=200)
                            elif ext in [".mp4", ".webm", ".ogg", ".mov", ".avi"]:
                                st.video(ruta)
                            elif ext == '.pdf':
                                try:
                                    if hasattr(st, 'pdf'):
                                        st.pdf(ruta, height=600)
                                    else:
                                        with open(ruta, 'rb') as f:
                                            import base64
                                            b64 = base64.b64encode(f.read()).decode('utf-8')
                                        pdf_display = f'<iframe src="data:application/pdf;base64,{b64}" width="100%" height="600" type="application/pdf"></iframe>'
                                        components.html(pdf_display, height=600, scrolling=True)
                                    # Descarga disponible
                                    with open(ruta, 'rb') as f:
                                        st.download_button(f"Descargar {nombre_archivo}", f.read(), file_name=nombre_archivo)
                                except Exception as e:
                                    st.warning(f"No se pudo mostrar el PDF desde disco: {e}")
                                    try:
                                        with open(ruta, 'rb') as f:
                                            import base64
                                            b64 = base64.b64encode(f.read()).decode('utf-8')
                                        pdf_display = f'<iframe src="data:application/pdf;base64,{b64}" width="100%" height="600" type="application/pdf"></iframe>'
                                        components.html(pdf_display, height=600, scrolling=True)
                                        with open(ruta, 'rb') as f:
                                            st.download_button(f"Descargar {nombre_archivo}", f.read(), file_name=nombre_archivo)
                                    except Exception as e2:
                                        st.error(f"No se pudo mostrar ni descargar el PDF: {e2}")
                            else:
                                with open(ruta, "rb") as f:
                                    st.download_button(f"Descargar {nombre_archivo}", f, file_name=nombre_archivo)
                        else:
                            st.warning(f"Archivo adjunto no encontrado: {nombre_archivo}")
                    else:
                        st.info(f"[{fecha}] {usuario_hist if usuario_hist else 'Soporte'}: {comentario}")
        else:
            st.info("Este ticket no tiene historial aún.")
        with st.form(f"form_comentario_{result['clicked_deal']['deal_id']}"):
            usuario_hist = st.text_input("Usuario", value=usuario_actual, disabled=True)
            comentario = st.text_area("Agregar comentario o acción al historial")
            enviar_com = st.form_submit_button("Agregar comentario")

        if enviar_com and comentario.strip():
            agregar_comentario(result['clicked_deal']['deal_id'], usuario_hist, comentario.strip())
            st.success("Comentario agregado.")
            # Notificar al creador del ticket y al admin si el comentario lo realizó soporte
            try:
                ticket_id = result['clicked_deal']['deal_id']
                conn = sqlite3.connect('helpdesk.db')
                c = conn.cursor()
                c.execute('SELECT email, status, issue FROM tickets WHERE id = ?', (ticket_id,))
                ticket_row = c.fetchone()
                conn.close()
                ticket_email = ticket_row[0] if ticket_row else None
                ticket_status = ticket_row[1] if ticket_row else ''
                ticket_issue = ticket_row[2] if ticket_row else ''

                subject = f"Actualización ticket {ticket_id}: {ticket_status}"
                body = f"Se ha agregado un nuevo comentario al ticket {ticket_id}:\n\nAsunto: {ticket_issue}\nEstado: {ticket_status}\nUsuario que comenta: {usuario_hist}\n\nComentario:\n{comentario}\n"

                # Enviar al creador si tiene email
                if ticket_email and '@' in ticket_email:
                    send_email_gmail(subject=subject, body=body, to_email=ticket_email)

                # Si este bloque es ejecutado desde Soporte (usuario_hist es usuario de soporte), notificar también al admin
                # Aquí asumimos que estamos en la sección Soporte porque "usuario_actual" estaba seteado arriba; en esa sección
                # usuario_hist es el usuario de soporte. Para evitar enviar doble notificación desde Admin, solo enviar admin si
                # el comentarista no es admin (simple heurística: buscar el rol en la tabla usuarios)
                try:
                    # comprobar rol del usuario que comentó
                    conn = sqlite3.connect('helpdesk.db')
                    c = conn.cursor()
                    c.execute('SELECT rol FROM usuarios WHERE username = ? OR nombre = ?', (usuario_hist, usuario_hist))
                    rol_row = c.fetchone()
                    conn.close()
                    rol_comentador = rol_row[0] if rol_row else None
                except Exception:
                    rol_comentador = None

                if rol_comentador and rol_comentador.lower() == 'soporte':
                    # enviar copia al admin
                    send_email_gmail(subject=subject, body=body, to_email=EMAIL_DESTINO_SOPORTE)

            except Exception as e:
                st.warning(f"No se pudo enviar notificaciones por email tras agregar comentario: {e}")
            st.rerun()

        # 🔽 Mostrar el uploader de archivo después del formulario
        st.markdown("**Adjuntar archivo al ticket**")
        archivo = st.file_uploader(
            "Selecciona un archivo para adjuntar",
            type=None,
            key=f"file_{result['clicked_deal']['deal_id']}"
        )

        flag_key = f'adjunto_procesado_{result["clicked_deal"]["deal_id"]}'
        if archivo is not None and not st.session_state.get(flag_key, False):
            import mimetypes
            nombre_archivo = archivo.name
            tipo_mime = mimetypes.guess_type(nombre_archivo)[0] or archivo.type or "application/octet-stream"
            contenido = archivo.getbuffer().tobytes()
            fecha = datetime.now().strftime("%d-%m-%Y %H:%M")
            usuario_adj = usuario_actual
            # Guardar en base de datos SOLO si NO EXISTE
            conn = sqlite3.connect('helpdesk.db')
            c = conn.cursor()
            c.execute('SELECT COUNT(*) FROM adjuntos WHERE ticket_id = ? AND nombre_archivo = ?', (result['clicked_deal']['deal_id'], nombre_archivo))
            exists = c.fetchone()[0]
            if not exists:
                c.execute('INSERT INTO adjuntos (ticket_id, nombre_archivo, tipo_mime, contenido, fecha, usuario) VALUES (?, ?, ?, ?, ?, ?)',
                        (result['clicked_deal']['deal_id'], nombre_archivo, tipo_mime, contenido, fecha, usuario_adj))
                conn.commit()
                # Registrar en historial
                agregar_comentario(result['clicked_deal']['deal_id'], usuario_adj, f"[Archivo adjunto BD]({nombre_archivo})")
                st.success(f"Archivo '{archivo.name}' adjuntado.")
            else:
                st.info(f"El archivo '{archivo.name}' ya fue adjuntado a este ticket.")
            conn.close()
            st.session_state[flag_key] = True
            st.rerun()
        elif archivo is None:
            st.session_state[f'adjunto_procesado_{result["clicked_deal"]["deal_id"]}'] = False
    # Guardar cambios en la base de datos si hay edición
    
    # if not edited_df.equals(df_filtrado):
    #     # Detectar cambios de estado y notificar
    #     for idx, row in edited_df.iterrows():
    #         ticket_id = row['ID']
    #         nuevo_estado = row['Status']
    #         # Buscar el estado anterior
    #         estado_anterior = st.session_state.df.loc[st.session_state.df['ID'] == ticket_id, 'Status'].values[0]
    #         if nuevo_estado != estado_anterior:
    #             # Notificar a soporte
    #             try:
    #                 send_email_gmail(
    #                     subject=f"Ticket {ticket_id} actualizado",
    #                     body=f"El estado del ticket {ticket_id} ha cambiado de '{estado_anterior}' a '{nuevo_estado}'.",
    #                     to_email=EMAIL_DESTINO_SOPORTE
    #                 )
    #             except Exception as e:
    #                 st.warning(f"No se pudo enviar el email de notificación a soporte: {e}")
    #             # Notificar al usuario si su campo parece un email
    #             usuario_email = row['usuario']
    #             if isinstance(usuario_email, str) and '@' in usuario_email:
    #                 try:
    #                     send_email_gmail(
    #                         subject=f"Actualización de su ticket {ticket_id}",
    #                         body=f"Su ticket {ticket_id} ha cambiado de estado: {estado_anterior} → {nuevo_estado}.",
    #                         to_email=usuario_email
    #                     )
    #                 except Exception as e:
    #                     st.warning(f"No se pudo enviar el email al usuario: {e}")
    #     actualizar_tickets_db(edited_df)
    #     # Actualizar solo los tickets filtrados en la sesión
    #     st.session_state.df.update(edited_df)
    st.header("Estadísticas")
    col1, col2, col3 = st.columns(3)
    num_open_tickets = len(df_filtrado[df_filtrado.Status == "Abierto"])
    tiempo_primera_respuesta = obtener_tiempo_primera_respuesta()
    tiempo_promedio = obtener_tiempo_promedio()
    col1.metric(label="Tickets abiertos", value=num_open_tickets, delta=10)
    col2.metric(label="Tiempo primera respuesta (horas)", value=tiempo_primera_respuesta, delta=-1.5)
    col3.metric(label="Tiempo promedio de resolución (horas)", value=tiempo_promedio, delta=2)
    st.write("")
    # st.write("##### Tickets por estado y mes")
    status_plot = (
        alt.Chart(df_filtrado)
        .mark_bar()
        .encode(
            x="month(Date Submitted):O",
            y="count():Q",
            xOffset="Status:N",
            color="Status:N",
        )
        .configure_legend(
            orient="bottom", titleFontSize=14, labelFontSize=14, titlePadding=5
        )
    )
    #Distribución por tipo
    tipo_counts = df_filtrado['tipo'].value_counts()
    if not tipo_counts.empty:
        fig_tipo = px.pie(values=tipo_counts.values, names=tipo_counts.index, 
                        title="Distribución por Tipo")
        st.plotly_chart(fig_tipo, use_container_width=True)



elif rol == "Admin":
    if "auth_admin" not in st.session_state:
        st.session_state.auth_admin = False
        st.session_state.user_admin = None
    if not st.session_state.auth_admin:
        st.header("Acceso restringido para Admin")
        with st.form("login_soporte"):
            user = st.text_input("Usuario")
            pwd = st.text_input("Contraseña", type="password")
            login = st.form_submit_button("Iniciar sesión")
        if login:
            for user_bd, pwd_bd, rol_bd in obtener_credenciales():
                if user == user_bd and pwd == pwd_bd and rol_bd == "admin":
                    st.session_state.auth_admin = True
                    st.session_state.user_admin = user_bd
                    st.success(f"Acceso concedido. Bienvenido, {st.session_state.user_admin}.")
                    time.sleep(2)
                    st.rerun()
                if user == user_bd and pwd == pwd_bd and rol_bd != "admin":
                    st.warning("Solo los usuarios administradores tienen acceso")
            else:
                st.error("Usuario o contraseña incorrectos.")
        st.stop()
    usuario_actual = st.session_state.user_admin
    if st.session_state.auth_admin:
        st.header(f"Bienvenido, {usuario_actual}")
        # boton de cerrar sesion
        if st.sidebar.button("Cerrar sesión"):
            st.session_state.auth_admin = False
            st.success("Cerrando sesión... Hasta luego.")
            time.sleep(1)
            st.rerun()
        import pandas as pd
        from streamlit_kanban_board_goviceversa import kanban_board
        st.sidebar.markdown("---")
        st.sidebar.subheader("Configuración de Emails")
        
        # Botón para habilitar/deshabilitar emails
        emails_habilitados = st.sidebar.toggle(
            "Envío de emails habilitado", 
            value=EMAILS_HABILITADOS,
            key="toggle_emails"
        )
        
        # Actualizar la variable global
        EMAILS_HABILITADOS = emails_habilitados
        
        if emails_habilitados:
            st.sidebar.success("✓ Emails habilitados")
        else:
            st.sidebar.warning("✗ Emails deshabilitados")
        def get_priority_color(priority):
            if priority.lower() == "alta":
                return "red"
            elif priority.lower() == "media":
                return "orange"
            elif priority.lower() == "baja":
                return "green"
            else:
                return "gray"  # Por si hay valores inesperados
        # Definir los estados para el tablero Kanban
        stages = [
            {"id": "Abierto", "name": "Abierto", "color": "#FF5555"},
            {"id": "En progreso", "name": "En progreso", "color": "#FFD700"},
            {"id": "Cerrado", "name": "Cerrado", "color": "#55FF55"}
        ]
        df = st.session_state.df.copy()
        df = df[df["tipo"] != "archivado"]  # No mostrar archivados en Kanban

        # --- Filtros avanzados para Admin (encima del Kanban) ---
        with st.expander("Filtros avanzados", expanded=False):
            c1, c2, c3 = st.columns(3)
            with c1:
                admin_estado = st.multiselect("Estado", options=sorted(df['Status'].dropna().unique()), default=[], key='admin_f_estado')
                admin_prioridad = st.multiselect("Prioridad", options=sorted(df['Priority'].dropna().unique()), default=[], key='admin_f_prioridad')
            with c2:
                admin_sede = st.multiselect("Sede", options=sorted(df['sede'].dropna().unique()), default=[], key='admin_f_sede')
                admin_tipo = st.multiselect("Tipo", options=sorted(df['tipo'].dropna().unique()), default=[], key='admin_f_tipo')
            with c3:
                admin_asignado = st.multiselect("Asignado", options=sorted(df['asignado'].dropna().unique()), default=[], key='admin_f_asignado')
                admin_texto = st.text_input("Buscar (texto libre)", value="", key='admin_f_texto')

            # Fecha y antigüedad para Admin
            today_a = datetime.now().date()
            # default start = Jan 1 of current year
            default_start_a = datetime(today_a.year, 1, 1).date()
            try:
                admin_fecha_range = st.date_input("Rango fecha envío (inicio - fin)", value=(default_start_a, today_a), key='admin_f_fecha_range')
            except Exception:
                admin_fecha_range = st.date_input("Rango fecha envío (inicio - fin)", key='admin_f_fecha_range')

            admin_antig_max = st.slider("Antigüedad máxima (días)", min_value=0, max_value=365, value=365, key='admin_f_antiguedad')

            # Reset y contador
            c_l, c_m, c_r = st.columns([1,1,2])
            with c_l:
                if st.button("Reset filtros", key='admin_f_reset'):
                    for k in ['admin_f_estado','admin_f_prioridad','admin_f_sede','admin_f_tipo','admin_f_asignado','admin_f_texto','admin_f_fecha_range','admin_f_antiguedad']:
                        if k in st.session_state:
                            del st.session_state[k]
                    st.rerun()
            with c_m:
                st.markdown(f"**Tickets mostrados:** {len(df)}")
            with c_r:
                st.write("")

            if admin_estado:
                df = df[df['Status'].isin(admin_estado)]
            if admin_prioridad:
                df = df[df['Priority'].isin(admin_prioridad)]
            if admin_sede:
                df = df[df['sede'].isin(admin_sede)]
            if admin_tipo:
                df = df[df['tipo'].isin(admin_tipo)]
            if admin_asignado:
                df = df[df['asignado'].isin(admin_asignado)]
            if admin_texto and admin_texto.strip():
                q = admin_texto.strip().lower()
                df = df[df.apply(lambda r: q in (str(r['Issue']) + ' ' + str(r['usuario']) + ' ' + str(r.get('email',''))).lower(), axis=1)]

            # Aplicar filtro por rango de fechas y antigüedad (Admin)
            if 'Date Submitted' in df.columns:
                df = df.copy()
                df['__date_dt'] = pd.to_datetime(df['Date Submitted'], dayfirst=True, errors='coerce')
                try:
                    if isinstance(admin_fecha_range, (list, tuple)) and len(admin_fecha_range) == 2:
                        s_d = pd.to_datetime(admin_fecha_range[0])
                        e_d = pd.to_datetime(admin_fecha_range[1])
                        df = df[(df['__date_dt'] >= pd.Timestamp(s_d)) & (df['__date_dt'] <= pd.Timestamp(e_d) + pd.Timedelta(days=1))]
                    elif admin_fecha_range:
                        single = pd.to_datetime(admin_fecha_range)
                        df = df[df['__date_dt'].dt.date == single.date()]
                except Exception:
                    pass

                if admin_antig_max is not None:
                    ahora_a = pd.Timestamp(datetime.now())
                    df['__dias'] = (ahora_a - df['__date_dt']).dt.days
                    df = df[df['__dias'].notna() & (df['__dias'] <= int(admin_antig_max))]

    deals = [
        {
            "id": row["ID"],
            "stage": row["Status"],
            "deal_id": row["ID"],
            "company_name": row['sede'] or "null",
            "product_type": row["Issue"] or "",
            "date": row["Date Submitted"],
            "underwriter": row["usuario"] or "",
            #"source": "OF",
            "currency": row["Priority"],
            "email": row["email"],
            "type": row["tipo"],
            "custom_html": f"""
    <div style='display: flex; flex-direction: row; align-items: center; justify-content: space-between;'>
        <p style='color:{get_priority_color(row["Priority"])}; margin:0; padding:0;'>
            Prioridad: {row["Priority"]}
        </p>
        <span style='background:#e0e0e0;border-radius:4px;padding:2px 6px;font-size:12px;margin-left:10px; color: black'>
            💻 {row["asignado"] if row["asignado"] else "No asignado"}
        </span>
    </div>
    <div>
        <p style='color: black; font-size: 12px; background-color: lightyellow; border-radius: 15px; text-align: center;'>Email: {row['email']}</p>
    </div>
    <div>
        <p style='color: black; font-size: 12px; background-color: lightgreen; border-radius: 15px; text-align: center;'>Proposito: {row['tipo']}</p>
    </div>
    </div>
        <p style='color: black; font-size: 12px; background-color: lightblue; border-radius: 15px; text-align: center;'>Dias transcurridos: {calcular_dias_transcurridos(row["Date Submitted"])}</p>
    </div>
"""
        }
        for _, row in df.iterrows()
    ]

    user_info = {
        "role": "riskManager",
        "email": "risk@company.com",
        "permissions": ["risk_approval", "management_approval"],
        "approval_limits": {"VV": {"EUR": 100000}, "OF": {"EUR": 150000}},
        "department": "Risk Management",
        "is_active": True
    }
    # Badge compacto mostrando rango de fechas y antigüedad activos (Admin)
    try:
        afr = admin_fecha_range
        if isinstance(afr, (list, tuple)) and len(afr) == 2:
            afstart = afr[0]
            afend = afr[1]
        else:
            afstart = afr
            afend = afr
        fecha_badge_admin = f"{afstart.strftime('%d-%m-%Y')} → {afend.strftime('%d-%m-%Y')}"
    except Exception:
        fecha_badge_admin = "Todos"
    antig_badge_admin = f"Antig ≤ {int(admin_antig_max)} días" if 'admin_antig_max' in locals() and admin_antig_max is not None else "Antig: Todos"
    st.markdown(f"**Filtros activos:** {fecha_badge_admin} · {antig_badge_admin} · Tickets: **{len(df)}**")

    # Expander con tabla detallada y AgGrid interactiva (Admin)
    with st.expander("Vista detallada (tabla)", expanded=False):
        st.write(f"Mostrando {len(df)} tickets filtrados")
        cols_a = list(df.columns)
        cols_sel_a = st.multiselect("Columnas a mostrar/ exportar", options=cols_a, default=cols_a, key='cols_sel_admin')

        try:
            from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode
            gb_a = GridOptionsBuilder.from_dataframe(df[cols_sel_a].reset_index(drop=True))
            gb_a.configure_pagination(paginationAutoPageSize=False, paginationPageSize=25)
            gb_a.configure_selection(selection_mode='multiple', use_checkbox=True)
            gb_a.configure_side_bar()
            gridOptions_a = gb_a.build()
            grid_response_a = AgGrid(
                df[cols_sel_a].reset_index(drop=True),
                gridOptions=gridOptions_a,
                enable_enterprise_modules=False,
                update_mode=GridUpdateMode.MODEL_CHANGED,
                theme='streamlit',
                height=400,
                fit_columns_on_grid_load=True,
            )

            selected_a = grid_response_a.get('selected_rows', [])
            selected_df_a = pd.DataFrame(selected_a) if selected_a else pd.DataFrame()

            col_export_1a, col_export_2a = st.columns(2)
            with col_export_1a:
                if not selected_df_a.empty:
                    csv_sel_a = selected_df_a.to_csv(index=False).encode('utf-8')
                    st.download_button("📥 Descargar CSV (seleccionados - Admin)", data=csv_sel_a, file_name="tickets_seleccionados_admin.csv", mime='text/csv')
                else:
                    csv_all_a = df[cols_sel_a].to_csv(index=False).encode('utf-8')
                    st.download_button("📥 Descargar CSV (filtrados - Admin)", data=csv_all_a, file_name="tickets_filtrados_admin.csv", mime='text/csv')
            with col_export_2a:
                try:
                    towrite_a = BytesIO()
                    if not selected_df_a.empty:
                        selected_df_a.to_excel(towrite_a, index=False, engine='openpyxl')
                    else:
                        df[cols_sel_a].to_excel(towrite_a, index=False, engine='openpyxl')
                    towrite_a.seek(0)
                    st.download_button("📥 Descargar XLSX", data=towrite_a, file_name="tickets_admin.xlsx", mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                except Exception:
                    towrite_a = BytesIO()
                    if not selected_df_a.empty:
                        selected_df_a.to_excel(towrite_a, index=False)
                    else:
                        df[cols_sel_a].to_excel(towrite_a, index=False)
                    towrite_a.seek(0)
                    st.download_button("📥 Descargar XLSX (Admin - fallback)", data=towrite_a, file_name="tickets_admin.xlsx", mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        except Exception as e:
            st.warning(f"st_aggrid no disponible o error al renderizar AgGrid: {e}")
            st.dataframe(df[cols_sel_a].reset_index(drop=True), use_container_width=True)
            try:
                csv_admin = df[cols_sel_a].to_csv(index=False).encode('utf-8')
                st.download_button("📥 Descargar CSV (filtrados - Admin)", data=csv_admin, file_name="tickets_filtrados_admin.csv", mime='text/csv')
                towrite = BytesIO()
                try:
                    df[cols_sel_a].to_excel(towrite, index=False, engine='openpyxl')
                except Exception:
                    df[cols_sel_a].to_excel(towrite, index=False)
                towrite.seek(0)
                st.download_button("📥 Descargar XLSX (filtrados - Admin)", data=towrite, file_name="tickets_filtrados_admin.xlsx", mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            except Exception as e2:
                st.warning(f"No se pudo preparar la descarga: {e2}")

    result = kanban_board(
        stages=stages,
        deals=deals,
        user_info=user_info,
        key="kanban_tickets"
    )

    # Procesar cambios de estado
    if 'dialogo_cerrado' not in st.session_state:
        st.session_state.dialogo_cerrado = False
    def obtener_historial(ticket_id):
            conn = sqlite3.connect('helpdesk.db')
            c = conn.cursor()
            c.execute('SELECT fecha, usuario, comentario FROM historial WHERE ticket_id = ? ORDER BY id ASC', (ticket_id,))
            rows = c.fetchall()
            conn.close()
            return rows

    if result and result.get("moved_deal"):
        moved_id = result["moved_deal"]["deal_id"]
        # Buscar el deal original
        moved_deal_full = next((d for d in deals if d["deal_id"] == moved_id), None)
        if moved_deal_full:
            email_moved = moved_deal_full.get("email", "No disponible")
            username = moved_deal_full.get("underwriter", "Usuario")
        else:
            st.warning("No se encontró el ticket movido en la lista de deals.")
        nuevo_estado = result["moved_deal"]["to_stage"]
        if (nuevo_estado == "Cerrado" and moved_id not in df[df["Status"] == "Cerrado"]["ID"].values and not st.session_state.dialogo_cerrado):
            mostrar_dialogo_comentario(moved_id)
            st.stop()
        else:
            st.session_state.dialogo_cerrado = False
        if nuevo_estado != "Cerrado" or (nuevo_estado == "Cerrado" and st.session_state.dialogo_cerrado):
            conn = sqlite3.connect('helpdesk.db')
            c = conn.cursor()
            c.execute('UPDATE tickets SET status=? WHERE id=?', (nuevo_estado, moved_id))
            conn.commit()
            conn.close()
            st.success(f"Ticket {moved_id} movido a estado '{nuevo_estado}'")
            # Recargar los tickets desde la base de datos para reflejar el cambio
            rows = obtener_tickets_db()
            st.session_state.df = pd.DataFrame(rows, columns=["ID", "Issue", "Status", "Priority", "Date Submitted", "usuario", "sede", "tipo", "asignado", "email"])
            try:
                # Obtener info del ticket
                conn = sqlite3.connect('helpdesk.db')
                c = conn.cursor()
                c.execute('SELECT issue, priority, email FROM tickets WHERE id = ?', (moved_id,))
                trow = c.fetchone()
                conn.close()
                issue = trow[0] if trow else ''
                priority = trow[1] if trow else ''
                ticket_email = trow[2] if trow else email_moved

                subject = f"Cambio de estado: {moved_id} → {nuevo_estado}"
                body = f"Su ticket ha cambiado de estado:\n\nID: {moved_id}\nAsunto: {issue}\nPrioridad: {priority}\nNuevo estado: {nuevo_estado}\n\n"

                if nuevo_estado.lower() == 'cerrado':
                    try:
                        conn = sqlite3.connect('helpdesk.db')
                        c = conn.cursor()
                        c.execute('SELECT comentario, usuario, fecha FROM historial WHERE ticket_id = ? ORDER BY id DESC LIMIT 1', (moved_id,))
                        last = c.fetchone()
                        conn.close()
                        if last and last[0]:
                            comentario_cierre = last[0]
                            usuario_cierre = last[1] or 'Soporte'
                            fecha_cierre = last[2] or ''
                            body += f"Comentario de cierre ({fecha_cierre}) por {usuario_cierre}:\n{comentario_cierre}\n\n"
                    except Exception:
                        pass

                # Enviar al creador si tiene email
                if ticket_email and '@' in ticket_email:
                    send_email_gmail(subject=subject, body=body, to_email=ticket_email)

                # Si fue cerrado por soporte (comprobación conservadora), también enviar al admin
                try:
                    if 'user_admin' in st.session_state and st.session_state.user_admin:
                        # Si admin cambió el estado, seguir enviando copia a admin no es necesario; pero si lo cerró soporte, admin debe recibir copia.
                        send_email_gmail(subject=subject, body=f"[COPIA_ADMIN]\n{body}", to_email=EMAIL_DESTINO_SOPORTE)
                except Exception:
                    pass

                logger.info(f"Email de notificación enviado para ticket {moved_id}")
                st.success(f"✅ Email enviado correctamente a {ticket_email}")
            except Exception as e:
                st.warning(f"No se pudo enviar el email de notificación: {e}")

    # Mostrar detalles si se selecciona un ticket
    if result and result.get("clicked_deal"):
    # Columna izquierda: info, centro: selección de usuario, derecha: prioridad
        cols = st.columns([2, 2.2, 2])
        with cols[0]:
            st.info(f"🆔 Ticket seleccionado: {result['clicked_deal']['id']}")
        with cols[1]:
            # --------- Selección y asignación de usuario ---------
            conn = sqlite3.connect('helpdesk.db')
            c = conn.cursor()
            c.execute('SELECT nombre FROM usuarios')
            usuarios = [u[0] for u in c.fetchall()]
            conn.close()
            ticket_id = result["clicked_deal"]["id"]
            conn = sqlite3.connect('helpdesk.db')
            c = conn.cursor()
            c.execute("SELECT asignado FROM tickets WHERE id = ?", (ticket_id,))
            asignado_actual = c.fetchone()
            asignado_actual = asignado_actual[0] 
            conn.close()
            nuevo_usuario = st.selectbox(
                "Asignar usuario", options=[""]+usuarios, index=(usuarios.index(asignado_actual) + 1) if asignado_actual in usuarios else 0, key=f"asignar_{ticket_id}", placeholder=asignado_actual)
            if nuevo_usuario != asignado_actual:
                email_usuario = obtener_correos_usuarios(nuevo_usuario)
                conn = sqlite3.connect('helpdesk.db')
                c = conn.cursor()
                c.execute("UPDATE tickets SET asignado = ? WHERE id = ?", (nuevo_usuario, ticket_id))
                conn.commit()
                conn.close()
                st.success(f"Usuario asignado: {nuevo_usuario if nuevo_usuario else 'Ninguno'}")
                rows = obtener_tickets_db()
                st.session_state.df = pd.DataFrame(
                    rows,
                    columns=["ID", "Issue", "Status", "Priority", "Date Submitted", "usuario", "sede", "tipo", "asignado", "email"]
                )
                try:
                    send_email_gmail(
                        subject=f"Asignacion de ticket: {result['clicked_deal']['id']} → {nuevo_usuario}",
                        body=f"El ticket:\n\nID: {result['clicked_deal']['id']}\n\nha sido asignado a usted",
                        to_email=email_usuario)
                    logger.info(f"Email de notificación enviado para ticket {result['clicked_deal']['id']}")
                    st.success(f"✅ Email enviado correctamente a {email_usuario}")
                except Exception as e:
                    st.warning(f"No se pudo enviar el email de notificación: {e}")
                st.rerun()
        with cols[2]:
            # --------- Selección y cambio de prioridad ---------
            conn = sqlite3.connect('helpdesk.db')
            c = conn.cursor()
            c.execute("SELECT priority FROM tickets WHERE id = ?", (ticket_id,))
            prioridad_actual = c.fetchone()[0]
            conn.close()
            prioridades = ["Alta", "Media", "Baja"]
            nueva_prioridad = st.selectbox(
                "Cambiar prioridad",
                options=prioridades,
                index=prioridades.index(prioridad_actual) if prioridad_actual in prioridades else 1,
                key=f"cambiar_prioridad_{ticket_id}"
            )
            if nueva_prioridad != prioridad_actual:
                conn = sqlite3.connect('helpdesk.db')
                c = conn.cursor()
                c.execute("UPDATE tickets SET priority = ? WHERE id = ?", (nueva_prioridad, ticket_id))
                conn.commit()
                conn.close()
                st.success(f"Prioridad cambiada a: {nueva_prioridad}")
                rows = obtener_tickets_db()
                st.session_state.df = pd.DataFrame(
                    rows,
                    columns=["ID", "Issue", "Status", "Priority", "Date Submitted", "usuario", "sede", "tipo", "asignado", "email"]
                )
                st.rerun()
        with st.expander("Detalles del ticket", icon='📋'):
            st.write("🆔 ID:", result["clicked_deal"]["id"])
            st.write("🏢 Sede:", result["clicked_deal"]["company_name"])
            st.write("📦 Tipo de producto:", result["clicked_deal"]["product_type"])
            st.write("📅 Fecha:", result["clicked_deal"]["date"])
            st.write("👤 Usuario:", result["clicked_deal"]["underwriter"])
            st.write("⚠️ Prioridad:", result["clicked_deal"]["currency"])
            st.write("🎯 Proposito", result["clicked_deal"]["type"])
            # -- Botón archivar para tickets cerrados (solo admin) --
            ticket_id = result["clicked_deal"]["id"]
            conn = sqlite3.connect('helpdesk.db')
            c = conn.cursor()
            c.execute("SELECT status, tipo FROM tickets WHERE id = ?", (ticket_id,))
            row = c.fetchone()
            conn.close()
            status_ticket, tipo_ticket = row if row else (None, None)

            #-- Obtener el propósito actual del ticket (sin la categoría) --
            if tipo_ticket and " - " in tipo_ticket:
                proposito_actual = tipo_ticket.split(" - ")[0]
            else:
                proposito_actual = tipo_ticket

            #-- cambiar tipo del ticket segun base de datos--#
            conn = sqlite3.connect('helpdesk.db')
            c = conn.cursor()
            c.execute("SELECT * FROM tipos_problema")
            Propositos = c.fetchall()
            conn.close()

            propositos_lista = [p[1] for p in Propositos]

            # Encontrar el índice correcto para el selectbox
            if proposito_actual in propositos_lista:
                index_actual = propositos_lista.index(proposito_actual)
            else:
                index_actual = 0  # Valor por defecto si no se encuentra

            nuevo_proposito = st.selectbox(
                "Cambiar proposito",
                options=["Seleccione"] + propositos_lista,
                index=0,
                key=f"cambiar_proposito_{ticket_id}"
            )

            # Solo procesar si el usuario realmente cambió la selección
            if nuevo_proposito != "Seleccione":
                # Buscar las categorías del tipo seleccionado
                conn = sqlite3.connect('helpdesk.db')
                c = conn.cursor()
                c.execute("SELECT categoria, categoria_2, categoria_3 FROM tipos_problema WHERE descripcion = ?", (nuevo_proposito,))
                categorias_raw = c.fetchone()
                conn.close()

                # Filtrar categorías no vacías
                categorias = [cat for cat in categorias_raw if cat] if categorias_raw else []
                opciones_categoria = ["Seleccione"] + categorias

                if categorias:
                    # Mostrar selectbox de categoría
                    categoria_seleccionada = st.selectbox(
                        "Seleccionar categoría",
                        options=opciones_categoria,
                        index=0,
                        key=f"cambiar_categoria_{ticket_id}"
                    )

                    if categoria_seleccionada in categorias and categoria_seleccionada != "Seleccione":
                        tipo_completo = f"{nuevo_proposito} - {categoria_seleccionada}"
                        conn = sqlite3.connect('helpdesk.db')
                        c = conn.cursor()
                        c.execute("UPDATE tickets SET tipo = ? WHERE id = ?", (tipo_completo, ticket_id))
                        conn.commit()
                        conn.close()
                        st.success(f"Propósito y categoría actualizados a: {tipo_completo}")
                        rows = obtener_tickets_db()
                        st.session_state.df = pd.DataFrame(
                            rows,
                            columns=["ID", "Issue", "Status", "Priority", "Date Submitted", "usuario", "sede", "tipo", "asignado", "email"]
                        )
                        
                else:
                    # Si no hay categorías, solo actualizar el propósito
                    conn = sqlite3.connect('helpdesk.db')
                    c = conn.cursor()
                    c.execute("UPDATE tickets SET tipo = ? WHERE id = ?", (nuevo_proposito, ticket_id))
                    conn.commit()
                    conn.close()
                    st.success(f"Propósito actualizado a: {nuevo_proposito}")
                    rows = obtener_tickets_db()
                    st.session_state.df = pd.DataFrame(
                        rows,
                        columns=["ID", "Issue", "Status", "Priority", "Date Submitted", "usuario", "sede", "tipo", "asignado", "email"]
                    )
                    st.rerun()                

            if status_ticket == "Cerrado" and tipo_ticket != "archivado":
                if st.button("Archivar este ticket", key=f"archivar_{ticket_id}"):
                    conn = sqlite3.connect('helpdesk.db')
                    c = conn.cursor()
                    c.execute("UPDATE tickets SET tipo = 'archivado' WHERE id = ?", (ticket_id,))
                    conn.commit()
                    conn.close()
                    st.success(f"Ticket {ticket_id} archivado y removido de la vista kanban.")
                    # Actualizar DF en session
                    rows = obtener_tickets_db()
                    st.session_state.df = pd.DataFrame(rows, columns=["ID", "Issue", "Status", "Priority", "Date Submitted", "usuario", "sede", "tipo", "asignado", "email"])
                    st.rerun()
            elif tipo_ticket == "archivado":
                st.info("Este ticket ya está archivado.")

    st.markdown("---")
    if result.get("clicked_deal"):
        st.subheader(f"Historial, comentarios y adjuntos del ticket {result['clicked_deal']['deal_id']}")
        historial = obtener_historial(result['clicked_deal']['deal_id'])
        import os
        from urllib.parse import unquote
        if historial:
            adjuntos_mostrados = set()
            adjuntos_disco_mostrados = set()
            with st.expander("🗨️ Historial de comentarios"):
                for h in historial:
                    fecha, usuario_hist, comentario = h
                    # Detectar si es un adjunto en base de datos
                    if comentario.startswith("[Archivo adjunto BD](") and comentario.endswith(")"):
                        nombre_archivo = comentario[len("[Archivo adjunto BD]("): -1]
                        nombre_archivo = unquote(nombre_archivo)
                        if nombre_archivo in adjuntos_mostrados:
                            continue
                        adjuntos_mostrados.add(nombre_archivo)
                        # Recuperar adjunto de la base de datos
                        conn = sqlite3.connect('helpdesk.db')
                        c = conn.cursor()
                        c.execute('SELECT tipo_mime, contenido FROM adjuntos WHERE ticket_id = ? AND nombre_archivo = ? ORDER BY id DESC LIMIT 1', (result['clicked_deal']['deal_id'], nombre_archivo))
                        adj = c.fetchone()
                        conn.close()
                        st.info(f"[{fecha}] {usuario_hist if usuario_hist else 'Soporte'}: Archivo adjunto: {nombre_archivo}")
                        if adj:
                            tipo_mime, contenido = adj
                            ext = os.path.splitext(nombre_archivo)[1].lower()
                            if tipo_mime and tipo_mime.startswith("image"):
                                import io
                                st.image(io.BytesIO(contenido), caption=nombre_archivo, width=200)
                            elif tipo_mime and tipo_mime.startswith("video"):
                                import io
                                st.video(io.BytesIO(contenido))
                            elif ext == '.pdf' or (tipo_mime and tipo_mime == 'application/pdf'):
                                try:
                                    if hasattr(st, 'pdf'):
                                        from io import BytesIO
                                        st.pdf(BytesIO(contenido))
                                    else:
                                        import base64
                                        b64 = base64.b64encode(contenido).decode('utf-8')
                                        pdf_display = f'<iframe src="data:application/pdf;base64,{b64}" width="100%" height="600" type="application/pdf"></iframe>'
                                        components.html(pdf_display, height=600, scrolling=True)
                                except Exception:
                                    st.download_button(f"Descargar {nombre_archivo}", data=contenido, file_name=nombre_archivo)
                            else:
                                st.download_button(f"Descargar {nombre_archivo}", data=contenido, file_name=nombre_archivo)
                        else:
                            st.warning(f"Archivo adjunto no encontrado en la base de datos: {nombre_archivo}")
                    # Detectar si es un adjunto en disco (legacy)
                    elif comentario.startswith("[Archivo adjunto](") and comentario.endswith(")"):
                        ruta = comentario[len("[Archivo adjunto]("): -1]
                        ruta = unquote(ruta)
                        nombre_archivo = os.path.basename(ruta)
                        if nombre_archivo in adjuntos_disco_mostrados:
                            continue
                        adjuntos_disco_mostrados.add(nombre_archivo)
                        ext = os.path.splitext(nombre_archivo)[1].lower()
                        st.info(f"[{fecha}] {usuario_hist if usuario_hist else 'Soporte'}: Archivo adjunto: {nombre_archivo}")
                        if os.path.exists(ruta):
                            if ext in [".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"]:
                                st.image(ruta, caption=nombre_archivo, width=200)
                            elif ext in [".mp4", ".webm", ".ogg", ".mov", ".avi"]:
                                st.video(ruta)
                            elif ext == '.pdf':
                                try:
                                    if hasattr(st, 'pdf'):
                                        st.pdf(ruta)
                                    else:
                                        with open(ruta, 'rb') as f:
                                            import base64
                                            b64 = base64.b64encode(f.read()).decode('utf-8')
                                        pdf_display = f'<iframe src="data:application/pdf;base64,{b64}" width="100%" height="600" type="application/pdf"></iframe>'
                                        components.html(pdf_display, height=600, scrolling=True)
                                except Exception:
                                    with open(ruta, "rb") as f:
                                        st.download_button(f"Descargar {nombre_archivo}", f, file_name=nombre_archivo)
                            else:
                                with open(ruta, "rb") as f:
                                    st.download_button(f"Descargar {nombre_archivo}", f, file_name=nombre_archivo)
                        else:
                            st.warning(f"Archivo adjunto no encontrado: {nombre_archivo}")
                    else:
                        st.info(f"[{fecha}] {usuario_hist if usuario_hist else 'Soporte'}: {comentario}")
        else:
            st.info("Este ticket no tiene historial aún.")
        # 🔽 Formulario único para agregar comentario
        with st.form(f"form_comentario_{result['clicked_deal']['deal_id']}"):
            usuario_hist = st.text_input("Usuario", value=usuario_actual, disabled=True)
            comentario = st.text_area("Agregar comentario o acción al historial")
            enviar_com = st.form_submit_button("Agregar comentario")

        if enviar_com and comentario.strip():
            agregar_comentario(result['clicked_deal']['deal_id'], usuario_hist, comentario.strip())
            st.success("Comentario agregado.")
            # Notificar al creador del ticket y al admin si el comentario lo realizó soporte
            try:
                ticket_id = result['clicked_deal']['deal_id']
                conn = sqlite3.connect('helpdesk.db')
                c = conn.cursor()
                c.execute('SELECT email, status, issue FROM tickets WHERE id = ?', (ticket_id,))
                ticket_row = c.fetchone()
                conn.close()
                ticket_email = ticket_row[0] if ticket_row else None
                ticket_status = ticket_row[1] if ticket_row else ''
                ticket_issue = ticket_row[2] if ticket_row else ''

                subject = f"Actualización ticket {ticket_id}: {ticket_status}"
                body = f"Se ha agregado un nuevo comentario al ticket {ticket_id}:\n\nAsunto: {ticket_issue}\nEstado: {ticket_status}\nUsuario que comenta: {usuario_hist}\n\nComentario:\n{comentario}\n"

                # Enviar al creador si tiene email
                if ticket_email and '@' in ticket_email:
                    send_email_gmail(subject=subject, body=body, to_email=ticket_email)

                # Comprobar rol del comentarista y si es soporte, notificar al admin
                try:
                    conn = sqlite3.connect('helpdesk.db')
                    c = conn.cursor()
                    c.execute('SELECT rol FROM usuarios WHERE username = ? OR nombre = ?', (usuario_hist, usuario_hist))
                    rol_row = c.fetchone()
                    conn.close()
                    rol_comentador = rol_row[0] if rol_row else None
                except Exception:
                    rol_comentador = None

                if rol_comentador and rol_comentador.lower() == 'soporte':
                    send_email_gmail(subject=subject, body=body, to_email=EMAIL_DESTINO_SOPORTE)

            except Exception as e:
                st.warning(f"No se pudo enviar notificaciones por email tras agregar comentario: {e}")
            st.rerun()

        # 🔽 Mostrar el uploader de archivo después del formulario
        st.markdown("**Adjuntar archivo al ticket**")
        archivo = st.file_uploader(
            "Selecciona un archivo para adjuntar",
            type=None,
            key=f"file_{result['clicked_deal']['deal_id']}"
        )

        flag_key = f'adjunto_procesado_{result["clicked_deal"]["deal_id"]}'
        if archivo is not None and not st.session_state.get(flag_key, False):
            import mimetypes
            nombre_archivo = archivo.name
            tipo_mime = mimetypes.guess_type(nombre_archivo)[0] or archivo.type or "application/octet-stream"
            contenido = archivo.getbuffer().tobytes()
            fecha = datetime.now().strftime("%d-%m-%Y %H:%M")
            usuario_adj = "Admin"
            # Guardar en base de datos SOLO si NO EXISTE
            conn = sqlite3.connect('helpdesk.db')
            c = conn.cursor()
            c.execute('SELECT COUNT(*) FROM adjuntos WHERE ticket_id = ? AND nombre_archivo = ?', (result['clicked_deal']['deal_id'], nombre_archivo))
            exists = c.fetchone()[0]
            if not exists:
                c.execute('INSERT INTO adjuntos (ticket_id, nombre_archivo, tipo_mime, contenido, fecha, usuario) VALUES (?, ?, ?, ?, ?, ?)',
                        (result['clicked_deal']['deal_id'], nombre_archivo, tipo_mime, contenido, fecha, usuario_adj))
                conn.commit()
                # Registrar en historial
                agregar_comentario(result['clicked_deal']['deal_id'], usuario_adj, f"[Archivo adjunto BD]({nombre_archivo})")
                st.success(f"Archivo '{archivo.name}' adjuntado.")
            else:
                st.info(f"El archivo '{archivo.name}' ya fue adjuntado a este ticket.")
            conn.close()
            st.session_state[flag_key] = True
            st.rerun()
        elif archivo is None:
            st.session_state[f'adjunto_procesado_{result["clicked_deal"]["deal_id"]}'] = False
    st.header("Estadísticas")
    col1, col2, col3 = st.columns(3)
    num_open_tickets = len(st.session_state.df[st.session_state.df.Status == "Abierto"])
    tiempo_primera_respuesta = obtener_tiempo_primera_respuesta()
    tiempo_promedio = obtener_tiempo_promedio()
    col1.metric(label="Tickets abiertos", value=num_open_tickets, delta=10)
    col2.metric(label="Tiempo primera respuesta (horas)", value=tiempo_primera_respuesta, delta=-1.5)
    col3.metric(label="Tiempo promedio de resolución (horas)", value=tiempo_promedio, delta=2)
    st.write("")
    # st.write("##### Tickets por estado y mes")
    status_plot = (
        alt.Chart(df)
        .mark_bar()
        .encode(
            x="month(Date Submitted):O",
            y="count():Q",
            xOffset="Status:N",
            color="Status:N",
        )
        .configure_legend(
            orient="bottom", titleFontSize=14, labelFontSize=14, titlePadding=5
        )
    )
    #Distribución por tipo
    tipo_counts = df['tipo'].value_counts()
    if not tipo_counts.empty:
        fig_tipo = px.pie(values=tipo_counts.values, names=tipo_counts.index, 
                        title="Distribución por Tipo")
        st.plotly_chart(fig_tipo, use_container_width=True)
elif rol == "Config":
    #----icono de engranaje en el sidebar----#
    # st.sidebar.button("⚙️", key="config_avanzada")
    st.markdown("Funciones avanzadas")
    adv = st.text_input("Contraseña", type="password")
    if adv == "alu.calidad":
        st.success("acceso concedido")
        st.title("🔐 Panel de administración")
        st.header("Propositos")

        # Obtener datos de la base de datos
        conn = sqlite3.connect('helpdesk.db')
        c = conn.cursor()
        c.execute('SELECT * FROM tipos_problema')
        tipos = c.fetchall()
        conn.close()

        df_tipos = pd.DataFrame(tipos, columns=["id", "tipo", "categoria", "categoria_2", "categoria_3"])

        st.subheader("Propositos predefinidos")

        # Editor interactivo
        df_editado = st.data_editor(df_tipos, num_rows="dynamic")
        #-----borrar fila en base de datos si se borra en el editor-----#
        filas_borradas = set(df_tipos['id']) - set(df_editado['id'])
        if filas_borradas:
            conn = sqlite3.connect('helpdesk.db')
            c = conn.cursor()
            for fila_id in filas_borradas:
                c.execute('DELETE FROM tipos_problema WHERE id = ?', (fila_id,))
            conn.commit()
            conn.close()
            st.toast(f"Filas borradas: {len(filas_borradas)}", icon="✅")
            time.sleep(2)
            st.rerun()
        # Botón para guardar cambios
        if st.button("💾 Guardar cambios en la base de datos"):
            conn = sqlite3.connect('helpdesk.db')
            c = conn.cursor()
            for _, row in df_editado.iterrows():
                c.execute("""
                    UPDATE tipos_problema
                    SET descripcion = ?, categoria = ?, categoria_2 = ?, categoria_3 = ?
                    WHERE id = ?
                """, (row["tipo"], row["categoria"], row["categoria_2"], row["categoria_3"], row["id"]))
            conn.commit()
            conn.close()
            st.success("✅ Cambios guardados correctamente")
            st.rerun()
        with st.expander("Agregar nuevo proposito", icon= '✨'):
            with st.form("form_tipo_problema"):
                nuevo_tipo = st.text_input("Nuevo proposito")
                descripcion_tipo = st.text_input("Categoria")
                descripcion_tipo2 = st.text_input("Categoria 2")
                descripcion_tipo3 = st.text_input("Categoria 3")
                submit = st.form_submit_button("Agregar proposito")
            if submit and nuevo_tipo and descripcion_tipo and nuevo_tipo.strip():
                conn = sqlite3.connect('helpdesk.db')
                c = conn.cursor()
                c.execute('INSERT INTO tipos_problema (descripcion, categoria, categoria_2, categoria_3) VALUES (?, ?, ?, ?)', (nuevo_tipo.strip(), descripcion_tipo.strip(), descripcion_tipo2.strip(), descripcion_tipo3.strip()))
                conn.commit()
                conn.close()
                st.toast("Nuevo proposito agregado", icon="✅")
                time.sleep(2)
                st.rerun()
            else:
                st.warning("Debe llenar al menos el proposito y la categoria 1")
        st.markdown("---")
        st.header("Gestión de usuarios")
        df_usuarios = pd.DataFrame(obtener_usuarios_sistema(), columns=["id", "username", "password", "rol"])
        df_usuarios_edit = st.data_editor(df_usuarios, num_rows="dynamic")
        #-----borrar fila en base de datos si se borra en el editor-----#
        filas_borradas_usuarios = set(df_usuarios['id']) - set(df_usuarios_edit['id'])
        if filas_borradas_usuarios:
            conn = sqlite3.connect('helpdesk.db')
            c = conn.cursor()
            for fila_usuario in filas_borradas_usuarios:
                c.execute('DELETE FROM usuarios WHERE id = ?', (fila_usuario,))
            conn.commit()
            conn.close()
            st.toast(f"Usuarios borrados: {len(filas_borradas_usuarios)}", icon="✅")
            time.sleep(2)
            st.rerun()
        # guardar cambios en la base de datos
        if st.button("💾 Guardar cambios en usuarios"):
            conn = sqlite3.connect('helpdesk.db')
            c = conn.cursor()
            for _, row in df_usuarios_edit.iterrows():
                c.execute("""
                    UPDATE usuarios
                    SET password = ?, rol = ?, username = ?
                    WHERE id = ?
                """, (row["password"], row["rol"], row["username"], row["id"]))
            conn.commit()
            conn.close()
            st.toast("Cambios en usuarios guardados", icon="✅")
            time.sleep(2)
            st.rerun()
        with st.expander("Agregar nuevo usuario", icon='👤'):
            with st.form("form_nuevo_usuario"):
                nuevo_usuario = st.text_input("Nuevo usuario")
                nueva_contraseña = st.text_input("Contraseña", type="password")
                rol_usuario = st.selectbox("Rol", options=["soporte", "admin"])
                submit_usuario = st.form_submit_button("Agregar usuario")
            if submit_usuario and nuevo_usuario and nueva_contraseña and nuevo_usuario.strip() and nueva_contraseña.strip():
                conn = sqlite3.connect('helpdesk.db')
                c = conn.cursor()
                c.execute('INSERT INTO usuarios (username, password, rol, nombre) VALUES (?, ?, ?, ?)', (nuevo_usuario.strip(), nueva_contraseña.strip(), rol_usuario, ""))
                conn.commit()
                conn.close()
                st.toast("Nuevo usuario agregado", icon="✅")
                time.sleep(2)
                st.rerun()
            else:
                st.warning("Debe llenar todos los campos")
        st.markdown("---")
        st.header("Gestion de Sedes")
        df_sedes = pd.DataFrame(obtener_sedes_db(), columns=["nombre"])
        df_sedes_edit = st.data_editor(df_sedes, num_rows="dynamic")
        with st.expander("Agregar sede", icon='🏪'):
            with st.form("form_nueva_sede"):
                nueva_sede = st.text_input("Nueva sede")
                submit_sede = st.form_submit_button("Agregar sede")
            if submit_sede and nueva_sede and nueva_sede.strip():
                conn = sqlite3.connect('helpdesk.db')
                c = conn.cursor()
                c.execute('INSERT INTO sedes (username) VALUES (?)', (nueva_sede))
                conn.commit()
                conn.close()
                st.toast("Nueva sede agregada", icon="✅")
                time.sleep(2)
                st.rerun
            else:
                st.warning("Debe llenar el campo")
        st.markdown("---")
        with st.expander("Base de datos y consultas SQL", icon='🖥️'):
            st.subheader("Base de datos interna (SQLite)")
            conn = sqlite3.connect('helpdesk.db')
            c = conn.cursor()
            c.execute('SELECT name FROM sqlite_master WHERE type="table"')
            tables = c.fetchall()
            st.write("Tablas en la base de datos:")
            for table in tables:
                st.write(f"- {table[0]}")
            conn.close()
            # ejecutar sql
            sql = st.text_area("Consulta SQL", height=100)
            if st.button("Ejecutar"):
                try:
                    conn = sqlite3.connect('helpdesk.db')
                    cursor = conn.cursor()
            
                    sql_upper = sql.strip().upper()
            
                    if sql_upper.startswith('SELECT'):
                        # Para consultas SELECT que devuelven resultados
                        df = pd.read_sql_query(sql, conn)
                        st.write("Resultados:")
                        st.dataframe(df)
                        st.write(f"Filas encontradas: {len(df)}")
                    else:
                        # Para múltiples sentencias SQL (INSERT, UPDATE, DELETE, CREATE, etc.)
                        cursor.executescript(sql)
                        conn.commit()
                        filas_afectadas = cursor.rowcount
                        st.success(f"Consulta(s) ejecutada(s) exitosamente. Filas afectadas: {filas_afectadas}")
            
                    conn.close()
            
                except Exception as e:
                    st.error(f"Error al ejecutar la consulta: {e}")

        # Descargar archivo db
        if st.button("Descargar base de datos"):
            with open("helpdesk_backup.db", "rb") as f:
                st.download_button("Descargar", f, file_name="helpdesk_backup.db")
        # exportar db a sql con create table if not exist y boton de descarga
        def _convert_sqlite_dump_to_mysql(sql_text: str) -> str:
            """Attempt a conservative conversion from an sqlite3 .dump to MySQL-compatible SQL.

            This performs textual transforms only; complex datatype/constraint differences
            are handled with reasonable defaults (INTEGER->INT, TEXT->TEXT, REAL->DOUBLE,
            BLOB->LONGBLOB, AUTOINCREMENT->AUTO_INCREMENT) and table options are appended.
            The function is intentionally conservative to avoid breaking data; review output
            before applying to a production MySQL instance.
            """
            import re

            out_lines = []
            buf = ''
            in_create = False
            for line in sql_text.splitlines():
                # Skip SQLite-specific transaction pragmas
                if line.strip().upper() == 'BEGIN TRANSACTION;':
                    out_lines.append('SET FOREIGN_KEY_CHECKS = 0;')
                    out_lines.append('START TRANSACTION;')
                    continue
                if line.strip().upper() == 'COMMIT;':
                    out_lines.append('COMMIT;')
                    out_lines.append('SET FOREIGN_KEY_CHECKS = 1;')
                    continue

                # Convert double-quoted identifiers to backticks (conservative)
                # Avoid touching single-quoted string literals.
                # This simple replace is OK because sqlite .dump uses double quotes for identifiers.
                processed = line.replace('"', '`')

                # Collect CREATE TABLE block to post-process types and append ENGINE
                if processed.strip().upper().startswith('CREATE TABLE') and processed.strip().endswith('('):
                    in_create = True
                    buf = processed
                    continue

                if in_create:
                    buf += '\n' + processed
                    if processed.strip().endswith(');') or processed.strip().endswith(')'):
                        # finished CREATE TABLE
                        # Normalize types inside parentheses
                        # Replace common types
                        buf = re.sub(r'\bAUTOINCREMENT\b', 'AUTO_INCREMENT', buf, flags=re.IGNORECASE)
                        buf = re.sub(r'\bINTEGER\b', 'INT', buf, flags=re.IGNORECASE)
                        buf = re.sub(r'\bREAL\b', 'DOUBLE', buf, flags=re.IGNORECASE)
                        buf = re.sub(r'\bBLOB\b', 'LONGBLOB', buf, flags=re.IGNORECASE)
                        buf = re.sub(r'\bBOOLEAN\b', 'TINYINT(1)', buf, flags=re.IGNORECASE)
                        # Ensure PRIMARY KEY AUTO_INCREMENT syntax when applicable
                        buf = re.sub(r'`(\w+)`\s+INT\s+PRIMARY\s+KEY\s+AUTO_INCREMENT', r'`\1` INT PRIMARY KEY AUTO_INCREMENT', buf, flags=re.IGNORECASE)
                        # Remove SQLite-specific WITHOUT ROWID if present
                        buf = re.sub(r'WITHOUT ROWID', '', buf, flags=re.IGNORECASE)
                        # Close with ENGINE and charset
                        if buf.strip().endswith(');'):
                            buf = buf.rstrip().rstrip(';') + ') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;'
                        out_lines.append(buf)
                        buf = ''
                        in_create = False
                    continue

                # Convert INSERT statements: change "table" to `table`
                if processed.strip().upper().startswith('INSERT INTO'):
                    # sqlite dumps sometimes use X'ABCD' for blobs; leave as-is
                    out_lines.append(processed)
                    continue

                # Default: passthrough
                out_lines.append(processed)

            return '\n'.join(out_lines)

        col_e1, col_e2 = st.columns([1,1])
        with col_e1:
            if st.button("Exportar base de datos a SQL (SQLite dump)"):
                conn = sqlite3.connect('helpdesk.db')
                with open("helpdesk_backup.sql", "w", encoding='utf-8') as f:
                    for line in conn.iterdump():
                        f.write(f"{line}\n")
                conn.close()
                st.success("Base de datos exportada como 'helpdesk_backup.sql'")
                with open("helpdesk_backup.sql", "r", encoding='utf-8') as f:
                    st.download_button("Descargar (SQLite)", f, file_name="helpdesk_backup.sql")

        with col_e2:
            if st.button("Exportar base de datos a SQL (MySQL)"):
                conn = sqlite3.connect('helpdesk.db')
                # Build sqlite dump in memory first
                dump_lines = '\n'.join(conn.iterdump())
                conn.close()
                try:
                    mysql_sql = _convert_sqlite_dump_to_mysql(dump_lines)
                    with open("helpdesk_backup_mysql.sql", "w", encoding='utf-8') as f:
                        f.write(mysql_sql)
                    st.success("Base de datos exportada como 'helpdesk_backup_mysql.sql' (convertida para MySQL — revisar antes de aplicar)")
                    with open("helpdesk_backup_mysql.sql", "r", encoding='utf-8') as f:
                        st.download_button("Descargar (MySQL)", f, file_name="helpdesk_backup_mysql.sql")
                except Exception as e:
                    st.error(f"Error al convertir dump a MySQL: {e}")
        # importar archivo sql a bd
        sql_import = st.file_uploader("Subir archivo SQL", type="sql")
        if st.button("Importar base de datos desde SQL"):
            if sql_import is not None:
                conn = sqlite3.connect('helpdesk.db')
                cursor = conn.cursor()
                # Leer el SQL y modificarlo antes de ejecutarlo
                sql_text = sql_import.read().decode("utf-8")
                import re
                # Cambiar CREATE TABLE por CREATE TABLE IF NOT EXISTS
                sql_text = re.sub(r"CREATE TABLE(?! IF NOT EXISTS)", "CREATE TABLE IF NOT EXISTS", sql_text)
                # Cambiar INSERT por INSERT OR IGNORE (para evitar duplicados)
                sql_text = re.sub(r"INSERT INTO", "INSERT OR IGNORE INTO", sql_text)
                cursor.executescript(sql_text)
                conn.commit()
                conn.close()
                st.success("Base de datos importada desde SQL.")
            else:
                st.error("Por favor, sube un archivo SQL.")
#------------------------------------------------------------Reporteria-----------------------------------------------#
if rol == 'Soporte' or rol == 'Admin':
    conn = sqlite3.connect('helpdesk.db')

    # Modificar la consulta principal para identificar qué categoría se usó
    query = """
    SELECT 
        t.id,
        t.tipo as tipo_ticket,
        tp.descripcion,
        tp.Categoria,
        tp.Categoria_2,
        tp.Categoria_3,
        CASE 
            WHEN t.tipo = tp.descripcion || ' - ' || tp.Categoria THEN 'Categoria'
            WHEN t.tipo = tp.descripcion || ' - ' || tp.Categoria_2 THEN 'Categoria_2'
            WHEN t.tipo = tp.descripcion || ' - ' || tp.Categoria_3 THEN 'Categoria_3'
        END as categoria_usada
    FROM tickets t
    INNER JOIN tipos_problema tp ON (
        t.tipo = tp.descripcion || ' - ' || tp.Categoria OR
        t.tipo = tp.descripcion || ' - ' || tp.Categoria_2 OR
        t.tipo = tp.descripcion || ' - ' || tp.Categoria_3
    )
    """
    query2 = "SELECT * FROM tipos_problema"
    df_tickets_tipos = pd.read_sql_query(query, conn)
    df_tipos = pd.read_sql_query(query2, conn)
    conn.close()

    st.markdown("---")
    st.header("Tickets por propósito")

    # Selección del propósito
    proposito_escogido = st.selectbox(
        "Selecciona un propósito para ver detalles", 
        options=["Seleccione"] + df_tipos["descripcion"].unique().tolist(), 
        key="filtro_proposito"
    )

    if proposito_escogido and proposito_escogido != "Seleccione":
        # Obtener todas las categorías disponibles para este propósito
        tipos_filtrados = df_tipos[df_tipos["descripcion"] == proposito_escogido]
        
        # Combinar todas las categorías (categoria, categoria_2, categoria_3)
        todas_categorias = []
        for col in ['Categoria', 'Categoria_2', 'Categoria_3']:
            if col in tipos_filtrados.columns:
                categorias_col = tipos_filtrados[col].dropna().unique()
                todas_categorias.extend(categorias_col)
        
        # Eliminar duplicados y ordenar
        todas_categorias = sorted(list(set(todas_categorias)))
        
        if todas_categorias:
            categoria_escogida = st.selectbox(
                "Selecciona una categoría", 
                options=["Seleccione"] + todas_categorias, 
                key="filtro_categoria"
            )
            
            if categoria_escogida and categoria_escogida != "Seleccione":
                # Filtrar tickets que coincidan exactamente con el propósito y categoría seleccionada
                df_filtrado_proposito = df_tickets_tipos[
                    (df_tickets_tipos["descripcion"] == proposito_escogido) & 
                    (
                        # Verificar que la categoría usada en el ticket coincida con la seleccionada
                        ((df_tickets_tipos["categoria_usada"] == "Categoria") & (df_tickets_tipos["Categoria"] == categoria_escogida)) |
                        ((df_tickets_tipos["categoria_usada"] == "Categoria_2") & (df_tickets_tipos["Categoria_2"] == categoria_escogida)) |
                        ((df_tickets_tipos["categoria_usada"] == "Categoria_3") & (df_tickets_tipos["Categoria_3"] == categoria_escogida))
                    )
                ]
                
                st.write(f"Se encontraron {len(df_filtrado_proposito)} tickets con el propósito '{proposito_escogido}' y categoría '{categoria_escogida}'")                
                if not df_filtrado_proposito.empty:
                    # Mostrar solo las columnas relevantes
                    columnas_mostrar = ['id', 'titulo', 'estado', 'descripcion', 'tipo_ticket']
                    columnas_disponibles = [col for col in columnas_mostrar if col in df_filtrado_proposito.columns]
                    
                    st.dataframe(
                        df_filtrado_proposito[columnas_disponibles],
                        column_config={
                            "id": "ID Ticket",
                            "titulo": "Título",
                            "estado": "Estado",
                            "tipo_ticket": "Tipo en Ticket",
                            "descripcion": "Propósito",
                            "categoria": "Categoría Principal"
                        },
                        hide_index=True
                    )
                else:
                    st.info("No hay tickets con este propósito y categoría.")
        else:
            st.warning("No hay categorías disponibles para este propósito.")
    else:
        st.info("Selecciona un propósito para ver las categorías disponibles.")
#------------------------------------------------------------Admin----------------------------------------------------------------------------------#
st.markdown("---")
st.write(""" <div style="position: static; left: 0; bottom: 0; width: 100%; background-color: rgba(255, 255, 255, 0); color: #495057; text-align: center; padding: 25px; font-size: 0.9em;">   <p>Desarrollado por Eddy Coello. @""" + str(datetime.now().year) + """ V3.0.0.</p>
     </div>
 """, unsafe_allow_html=True)
