from datetime import datetime, timedelta
import random
import sqlite3
import logging
import time
import altair as alt
import streamlit.components.v1 as components
from streamlit_kanban import kanban
import numpy as np
import plotly.express as px 
import pandas as pd
import streamlit as st
from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode
EMAILS_HABILITADOS = True
fecha_actual = datetime.now().strftime("%d-%m-%Y %H:%M")

# --- Persistencia de base de datos (SQLite) ----
import os
import shutil
from pathlib import Path
import re


def asegurar_db_persistente():
    """
    Copia la base de datos del repositorio a una carpeta persistente en el host (si no existe).
    Retorna la ruta final del archivo .db a utilizar.
    """
    # Directorio persistente configurable por variable de entorno
    base_dir = os.environ.get("STREAMLIT_PERSIST_DIR", None)
    if base_dir:
        data_dir = Path(base_dir)
    else:
        # Fallback al HOME del usuario
        data_dir = Path.home() / ".streamlit" / "data" / "helpdesk"
    data_dir.mkdir(parents=True, exist_ok=True)

    target_db = data_dir / "helpdesk.db"
    repo_dir = Path(__file__).resolve().parent
    repo_db = repo_dir / "helpdesk.db"
    repo_sql = repo_dir / "helpdesk.db.sql"

    if not target_db.exists():
        # 1) Copiar archivo .db si existe en el repo
        if repo_db.exists():
            shutil.copy2(repo_db, target_db)
        # 2) O, inicializar desde el dump SQL si existe
        elif repo_sql.exists():
            import sqlite3 as _sqlite3
            conn = _sqlite3.connect(str(target_db))
            with open(repo_sql, "r", encoding="utf-8") as f:
                sql_text = f.read()
            # Asegurar que las tablas se creen solo si no existen
            sql_text = re.sub(r"CREATE TABLE(?! IF NOT EXISTS)", "CREATE TABLE IF NOT EXISTS", sql_text)
            conn.executescript(sql_text)
            conn.commit()
            conn.close()
    return str(target_db)


PERSISTENT_DB_PATH = asegurar_db_persistente()


def obtener_conexion_db():
    """
    Retorna una nueva conexión SQLite apuntando a la base de datos persistente.
    """
    import sqlite3 as _sqlite3
    return _sqlite3.connect(PERSISTENT_DB_PATH, check_same_thread=False)


# Parchear sqlite3.connect para que toda la app use la ruta persistente,
# sin necesidad de modificar cada llamada existente.
import sqlite3 as _sqlite3_global
_original_sqlite3_connect = _sqlite3_global.connect

def _connect_persistente(*args, **kwargs):
    kwargs.setdefault("check_same_thread", False)
    return _original_sqlite3_connect(PERSISTENT_DB_PATH, **kwargs)

_sqlite3_global.connect = _connect_persistente



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
                    contenido = archivo_usuario.getbuffer().tobytes()
                    fecha = datetime.now().strftime("%d-%m-%Y %H:%M")
                    usuario_adj = usuario or "Usuario"
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
                    send_email_gmail(
                        subject=f"Nuevo ticket creado: {new_ticket[0]}",
                        body=f"Se ha creado un nuevo ticket:\n\nID: {new_ticket[0]}\nUsuario: {usuario}\nSede: {sede}\nTipo: {tipo_categoria}\nPrioridad: {priority}\nDescripción: {issue}",
                        to_email=EMAIL_DESTINO_SOPORTE
                    )
                except Exception as e:
                    st.warning(f"No se pudo enviar el email de notificación: {e}")
                # Recargar los tickets desde la base de datos
                rows = obtener_tickets_db()
                st.session_state.df = pd.DataFrame(rows, columns=["ID", "Issue", "Status", "Priority", "Date Submitted", "usuario", "sede", "tipo", "asignado", "email"])
            else:
                st.warning("Debe llenar todos los campos obligatorios.")
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
                send_email_gmail(
                    subject=f"Cambio de estado: {result['moved_deal']['deal_id']} → {nuevo_estado}",
                    body=f"Su ticket:\n\nID: {result['moved_deal']['deal_id']}\nUsuario: {username}\n\nha cambiado de estado a '{nuevo_estado}'",
                    to_email=email_moved)
                logger.info(f"Email de notificación enviado para ticket {moved_id}")
                st.success(f"✅ Email enviado correctamente a {email_moved}")
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
                            if tipo_mime and tipo_mime.startswith("image"):
                                import io
                                st.image(io.BytesIO(contenido), caption=nombre_archivo, width=200)
                            elif tipo_mime and tipo_mime.startswith("video"):
                                import io
                                st.video(io.BytesIO(contenido))
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
            try:
                send_email_gmail(
                    subject=f"Nuevo comentario en el ticket {result['clicked_deal']['deal_id']}",
                    body=f"Se ha agregado un nuevo comentario al ticket {result['clicked_deal']['deal_id']}:\n\n{comentario}",
                    to_email=EMAIL_DESTINO_SOPORTE             #result['clicked_deal'].get("email", "No disponible")
                )
            except Exception as e:
                st.warning(f"No se pudo enviar el email de notificación: {e}")
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
                send_email_gmail(
                    subject=f"Cambio de estado: {result['moved_deal']['deal_id']} → {nuevo_estado}",
                    body=f"Su ticket:\n\nID: {result['moved_deal']['deal_id']}\nUsuario: {username}\n\nha cambiado de estado a '{nuevo_estado}'",
                    to_email=email_moved)
                logger.info(f"Email de notificación enviado para ticket {moved_id}")
                st.success(f"✅ Email enviado correctamente a {email_moved}")
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
            try:
                send_email_gmail(
                    subject=f"Nuevo comentario en el ticket {result['clicked_deal']['deal_id']}",
                    body=f"Se ha agregado un nuevo comentario al ticket {result['clicked_deal']['deal_id']}:\n\n{comentario}",
                    to_email=EMAIL_DESTINO_SOPORTE             #result['clicked_deal'].get("email", "No disponible")
                )
            except Exception as e:
                st.warning(f"No se pudo enviar el email de notificación: {e}")
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
        if st.button("Exportar base de datos a SQL"):
            conn = sqlite3.connect('helpdesk.db')
            with open("helpdesk_backup.sql", "w") as f:
                for line in conn.iterdump():
                    f.write(f"{line}\n")
            conn.close()
            st.success("Base de datos exportada como 'helpdesk_backup.sql'")
            with open("helpdesk_backup.sql", "r") as f:
                st.download_button("Descargar", f, file_name="helpdesk_backup.sql")
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
st.write(""" <div style="position: static; left: 0; bottom: 0; width: 100%; background-color: rgba(255, 255, 255, 0); color: #495057; text-align: center; padding: 25px; font-size: 0.9em;">   <p>Desarrollado por Eddy Coello. ©2025 V1.0.0..</p>
     </div>
 """, unsafe_allow_html=True)
