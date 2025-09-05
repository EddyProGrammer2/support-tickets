import datetime
import random
import sqlite3
import logging

import altair as alt
import streamlit.components.v1 as components
from streamlit_kanban import kanban
import numpy as np
import pandas as pd
import streamlit as st
from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

st.set_page_config(page_title="Gestor de Tickets", layout="wide")
st.title("Mesa de ayuda")

# --- Configuración de notificaciones por email (Gmail) ---
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

def obtener_credenciales(credenciales):
    conn = sqlite3.connect('helpdesk.db')
    c = conn.cursor()
    c.execute('SELECT username, password from usuarios')
    credenciales = c.fetchall()
    conn.close()
    return credenciales

def send_email_gmail(subject, body, to_email):
    # Configura estos datos con tu cuenta de Gmail y contraseña de aplicación
    gmail_user = 'eddy.aluminiologo@gmail.com'
    gmail_password = 'TU_CONTRASEÑA_DE_APLICACION'
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
    except Exception as e:
        print(f"Error enviando email: {e}")

# Configura aquí el correo de destino del soporte
EMAIL_DESTINO_SOPORTE = 'DESTINO@ejemplo.com'

# Configuración de la página y título.
st.set_page_config(page_title="Tickets de soporte", page_icon="🎫")
st.title("🎫 Tickets de soporte")

# Funciones para la base de datos
def obtener_tickets_db():
    conn = sqlite3.connect('helpdesk.db')
    c = conn.cursor()
    c.execute('SELECT id, issue, status, priority, date_submitted, usuario, sede, tipo, asignado FROM tickets ORDER BY id DESC')
    rows = c.fetchall()
    conn.close()
    return rows

def agregar_ticket_db(issue, priority, usuario, sede, tipo):
    conn = sqlite3.connect('helpdesk.db')
    c = conn.cursor()
    c.execute('SELECT id FROM tickets ORDER BY id DESC LIMIT 1')
    last = c.fetchone()
    if last:
        last_num = int(last[0].split('-')[1])
    else:
        last_num = 1000
    new_id = f"TICKET-{last_num+1}"
    today = datetime.datetime.now().strftime("%d-%m-%Y")
    c.execute('INSERT INTO tickets (id, issue, status, priority, date_submitted, usuario, sede, tipo, asignado) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', (new_id, issue, "Abierto", priority, today, usuario, sede, tipo, ""))
    conn.commit()
    conn.close()
    return new_id, issue, "Abierto", priority, today, usuario, sede, tipo, ""

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
    df = pd.DataFrame(rows, columns=["ID", "Issue", "Status", "Priority", "Date Submitted", "usuario", "sede", "tipo", "asignado"])
    st.session_state.df = df

# Selección de rol al inicio
rol = st.sidebar.selectbox(
    "Selecciona tu rol",
    ["Usuario", "Soporte", "Admin"],
    help="Elige si eres usuario final, personal de soporte o administrador"
)

if rol == "Usuario":
    st.header("Enviar un ticket de soporte")
    with st.form("add_ticket_form"):
        usuario = st.text_input("Usuario", placeholder="Nombre y Apellido")
        email = st.text_input("Email", placeholder="Correo electronico")
        sede = st.selectbox("Seleccionar sede", ["Catia", "La Guaira", "Mariche", "CENDIS", "Fabrica y Laminadora"])
        tipo = st.selectbox("Tipo de ticket", ["Problema técnico", "Solicitud de información", "Otro"])
        issue = st.text_area("Describe el problema")
        priority = st.selectbox("Prioridad", ["Alta", "Media", "Baja"])
        archivo_usuario = st.file_uploader("Adjuntar archivo (opcional)", type=None, key="file_usuario")
        submitted = st.form_submit_button("Enviar ticket")

    if submitted and usuario and email and sede and tipo and issue and priority:
        new_ticket = agregar_ticket_db(issue, priority, usuario, sede, tipo)
        df_new = pd.DataFrame([
            {
                "ID": new_ticket[0],
                "Issue": new_ticket[1],
                "Status": new_ticket[2],
                "Priority": new_ticket[3],
                "Date Submitted": new_ticket[4],
                "usuario": new_ticket[5],
                "sede": new_ticket[6],
                "tipo": new_ticket[7]
            }
        ])
        st.write("¡Ticket enviado! Detalles:")
        st.dataframe(df_new, use_container_width=True, hide_index=True)
        # Guardar archivo adjunto en base de datos si existe
        if archivo_usuario is not None:
            import mimetypes
            nombre_archivo = archivo_usuario.name
            tipo_mime = mimetypes.guess_type(nombre_archivo)[0] or archivo_usuario.type or "application/octet-stream"
            contenido = archivo_usuario.getbuffer().tobytes()
            fecha = datetime.datetime.now().strftime("%d-%m-%Y %H:%M")
            usuario_adj = usuario or "Usuario"
            # Guardar en base de datos
            conn = sqlite3.connect('helpdesk.db')
            c = conn.cursor()
            c.execute('INSERT INTO adjuntos (ticket_id, nombre_archivo, tipo_mime, contenido, fecha, usuario) VALUES (?, ?, ?, ?, ?, ?)',
                      (new_ticket[0], nombre_archivo, tipo_mime, contenido, fecha, usuario_adj))
            conn.commit()
            conn.close()
            # Registrar en historial
            def agregar_comentario(ticket_id, usuario, comentario):
                conn = sqlite3.connect('helpdesk.db')
                c = conn.cursor()
                fecha = datetime.datetime.now().strftime("%d-%m-%Y %H:%M")
                c.execute('INSERT INTO historial (ticket_id, fecha, usuario, comentario) VALUES (?, ?, ?, ?)', (ticket_id, fecha, usuario, comentario))
                conn.commit()
                conn.close()
            agregar_comentario(new_ticket[0], usuario_adj, f"[Archivo adjunto BD]({nombre_archivo})")
            st.success(f"Archivo '{archivo_usuario.name}' adjuntado al ticket.")
        # Notificación por email a soporte
        try:
            send_email_gmail(
                subject=f"Nuevo ticket creado: {new_ticket[0]}",
                body=f"Se ha creado un nuevo ticket:\n\nID: {new_ticket[0]}\nUsuario: {usuario}\nSede: {sede}\nTipo: {tipo}\nPrioridad: {priority}\nDescripción: {issue}",
                to_email=EMAIL_DESTINO_SOPORTE
            )
        except Exception as e:
            st.warning(f"No se pudo enviar el email de notificación: {e}")
        # Recargar los tickets desde la base de datos
        rows = obtener_tickets_db()
        st.session_state.df = pd.DataFrame(rows, columns=["ID", "Issue", "Status", "Priority", "Date Submitted", "usuario", "sede", "tipo", "asignado"])
    else:
        st.warning("Debe llenar todos los campos obligatorios.")

elif rol == "Soporte":
    # Autenticación simple para soporte
    if "auth_soporte" not in st.session_state:
        st.session_state.auth_soporte = False
    if not st.session_state.auth_soporte:
        st.header("Acceso restringido para soporte")
        with st.form("login_soporte"):
            user = st.text_input("Usuario")
            pwd = st.text_input("Contraseña", type="password")
            login = st.form_submit_button("Iniciar sesión")
        if login:
            if user == "soporte" and pwd == "1234":
                st.session_state.auth_soporte = True
                st.success("Acceso concedido. Bienvenido, soporte.")
                st.rerun()
            else:
                st.error("Usuario o contraseña incorrectos.")
        st.stop()

    st.header("Gestión de tickets de soporte")
    import pandas as pd
    from streamlit_kanban_board_goviceversa import kanban_board
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
        {"id": "Cerrado", "name": "Cerrado", "color": "#55FF55"}
    ]
    df = st.session_state.df.copy()
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
            "source": "VV",
            "custom_html": f"""
    <div>
        <p>{row['Issue']}</p>
    </div>
    <div>
        <p style='color:{get_priority_color(row["Priority"])}'>
            Prioridad: {row["Priority"]}
        </p>
    </div>
"""
        }
        for _, row in df.iterrows()
    ]
    st.markdown("### Tickets Kanban")

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
    selected_ticket_id = result.get("clicked_deal")

    # Procesar cambios de estado
    if result and result.get("moved_deal"):
        moved_id = result["moved_deal"]["deal_id"]
        nuevo_estado = result["moved_deal"]["to_stage"]
        conn = sqlite3.connect('helpdesk.db')
        c = conn.cursor()
        c.execute('UPDATE tickets SET status=? WHERE id=?', (nuevo_estado, moved_id))
        conn.commit()
        conn.close()
        st.success(f"Ticket {moved_id} movido a estado '{nuevo_estado}'")
        # Recargar los tickets desde la base de datos para reflejar el cambio
        rows = obtener_tickets_db()
        st.session_state.df = pd.DataFrame(rows, columns=["ID", "Issue", "Status", "Priority", "Date Submitted", "usuario", "sede", "tipo", "asignado"])
    # Mostrar detalles si se selecciona un ticket
    if result and result.get("clicked_deal"):
        st.info(f"Ticket seleccionado: {result['clicked_deal']['deal_id']}")
        with st.expander("Detalles del ticket"):
            st.write("🆔 ID:", result["clicked_deal"]["id"])
            st.write("🏢 Sede:", result["clicked_deal"]["company_name"])
            st.write("📦 Tipo de producto:", result["clicked_deal"]["product_type"])
            st.write("📅 Fecha:", result["clicked_deal"]["date"])
            st.write("👤 Usuario:", result["clicked_deal"]["underwriter"])
            st.write("⚠️ Prioridad:", result["clicked_deal"]["currency"])
    def obtener_historial(ticket_id):
            conn = sqlite3.connect('helpdesk.db')
            c = conn.cursor()
            c.execute('SELECT fecha, usuario, comentario FROM historial WHERE ticket_id = ? ORDER BY id ASC', (ticket_id,))
            rows = c.fetchall()
            conn.close()
            return rows

    def agregar_comentario(ticket_id, usuario, comentario):
            conn = sqlite3.connect('helpdesk.db')
            c = conn.cursor()
            fecha = datetime.datetime.now().strftime("%d-%m-%Y %H:%M")
            c.execute('INSERT INTO historial (ticket_id, fecha, usuario, comentario) VALUES (?, ?, ?, ?)', (ticket_id, fecha, usuario, comentario))
            conn.commit()
            conn.close()

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
                                st.image(io.BytesIO(contenido), caption=nombre_archivo, use_container_width=True)
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
                                st.image(ruta, caption=nombre_archivo, use_container_width=True)
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
            usuario_hist = st.text_input("Usuario (opcional)", value="Soporte")
            comentario = st.text_area("Agregar comentario o acción al historial")
            enviar_com = st.form_submit_button("Agregar comentario")

        if enviar_com and comentario.strip():
            agregar_comentario(result['clicked_deal']['deal_id'], usuario_hist, comentario.strip())
            st.success("Comentario agregado.")
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
            fecha = datetime.datetime.now().strftime("%d-%m-%Y %H:%M")
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
    num_open_tickets = len(st.session_state.df[st.session_state.df.Status == "Abierto"])
    col1.metric(label="Tickets abiertos", value=num_open_tickets, delta=10)
    col2.metric(label="Tiempo primera respuesta (horas)", value=5.2, delta=-1.5)
    col3.metric(label="Tiempo promedio de resolución (horas)", value=16, delta=2)
    st.write("")
    st.write("##### Tickets por estado y mes")
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
    st.altair_chart(status_plot, use_container_width=True, theme="streamlit")
    st.write("##### Prioridades actuales de tickets")
    priority_plot = (
        alt.Chart(df)
        .mark_arc()
        .encode(theta="count():Q", color="Priority:N")
        .properties(height=300)
        .configure_legend(
            orient="bottom", titleFontSize=14, labelFontSize=14, titlePadding=5
        )
    )
    st.altair_chart(priority_plot, use_container_width=True, theme="streamlit")

elif rol == "Admin":
    if "auth_admin" not in st.session_state:
        st.session_state.auth_admin = False
    if not st.session_state.auth_admin:
        st.header("Acceso restringido para Admin")
        with st.form("login_soporte"):
            user = st.text_input("Usuario")
            pwd = st.text_input("Contraseña", type="password")
            login = st.form_submit_button("Iniciar sesión")
        if login:
            if user == 'admin' and pwd == '1234':
                st.session_state.auth_admin = True
                st.success("Acceso concedido. Bienvenido, Admin.")
                st.rerun()
            else:
                st.error("Usuario o contraseña incorrectos.")
        st.stop()

    if st.session_state.auth_admin:
        st.header("Bienvenido, Admin")
        import pandas as pd
        from streamlit_kanban_board_goviceversa import kanban_board
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
            "custom_html": f"""
    <div>
        <p>{row['Issue']}</p>
    </div>
    <div style="display: flex; flex-direction: row; align-items: center; justify-content: space-between;">
        <p style='color:{get_priority_color(row["Priority"])}; margin:0; padding:0;'>
            Prioridad: {row["Priority"]}
        </p>
        <span style="background:#e0e0e0;border-radius:4px;padding:2px 6px;font-size:12px;margin-left:10px;">
            👤 {row["asignado"] if row["asignado"] else "No asignado"}
        </span>
    </div>
"""
        }
        for _, row in df.iterrows()
    ]
    st.markdown("### Tickets Kanban")

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
    if result and result.get("moved_deal"):
        moved_id = result["moved_deal"]["deal_id"]
        nuevo_estado = result["moved_deal"]["to_stage"]
        conn = sqlite3.connect('helpdesk.db')
        c = conn.cursor()
        c.execute('UPDATE tickets SET status=? WHERE id=?', (nuevo_estado, moved_id))
        conn.commit()
        conn.close()
        st.success(f"Ticket {moved_id} movido a estado '{nuevo_estado}'")
        # Recargar los tickets desde la base de datos para reflejar el cambio
        rows = obtener_tickets_db()
        st.session_state.df = pd.DataFrame(rows, columns=["ID", "Issue", "Status", "Priority", "Date Submitted", "usuario", "sede", "tipo", "asignado"])

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
                conn = sqlite3.connect('helpdesk.db')
                c = conn.cursor()
                c.execute("UPDATE tickets SET asignado = ? WHERE id = ?", (nuevo_usuario, ticket_id))
                conn.commit()
                conn.close()
                st.success(f"Usuario asignado: {nuevo_usuario if nuevo_usuario else 'Ninguno'}")
                rows = obtener_tickets_db()
                st.session_state.df = pd.DataFrame(
                    rows,
                    columns=["ID", "Issue", "Status", "Priority", "Date Submitted", "usuario", "sede", "tipo", "asignado"]
                )
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
                    columns=["ID", "Issue", "Status", "Priority", "Date Submitted", "usuario", "sede", "tipo", "asignado"]
                )
                st.rerun()
        with st.expander("Detalles del ticket"):
            st.write("🆔 ID:", result["clicked_deal"]["id"])
            st.write("🏢 Sede:", result["clicked_deal"]["company_name"])
            st.write("📦 Tipo de producto:", result["clicked_deal"]["product_type"])
            st.write("📅 Fecha:", result["clicked_deal"]["date"])
            st.write("👤 Usuario:", result["clicked_deal"]["underwriter"])
            st.write("⚠️ Prioridad:", result["clicked_deal"]["currency"])
    def obtener_historial(ticket_id):
            conn = sqlite3.connect('helpdesk.db')
            c = conn.cursor()
            c.execute('SELECT fecha, usuario, comentario FROM historial WHERE ticket_id = ? ORDER BY id ASC', (ticket_id,))
            rows = c.fetchall()
            conn.close()
            return rows

    def agregar_comentario(ticket_id, usuario, comentario):
            conn = sqlite3.connect('helpdesk.db')
            c = conn.cursor()
            fecha = datetime.datetime.now().strftime("%d-%m-%Y %H:%M")
            c.execute('INSERT INTO historial (ticket_id, fecha, usuario, comentario) VALUES (?, ?, ?, ?)', (ticket_id, fecha, usuario, comentario))
            conn.commit()
            conn.close()

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
                                st.image(io.BytesIO(contenido), caption=nombre_archivo, use_container_width=True)
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
                                st.image(ruta, caption=nombre_archivo, use_container_width=True)
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
            usuario_hist = st.text_input("Usuario (opcional)", value="Soporte")
            comentario = st.text_area("Agregar comentario o acción al historial")
            enviar_com = st.form_submit_button("Agregar comentario")

        if enviar_com and comentario.strip():
            agregar_comentario(result['clicked_deal']['deal_id'], usuario_hist, comentario.strip())
            st.success("Comentario agregado.")
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
            fecha = datetime.datetime.now().strftime("%d-%m-%Y %H:%M")
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
    col1.metric(label="Tickets abiertos", value=num_open_tickets, delta=10)
    col2.metric(label="Tiempo primera respuesta (horas)", value=5.2, delta=-1.5)
    col3.metric(label="Tiempo promedio de resolución (horas)", value=16, delta=2)
    st.write("")
    st.write("##### Tickets por estado y mes")
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
    st.altair_chart(status_plot, use_container_width=True, theme="streamlit")
    st.write("##### Prioridades actuales de tickets")
    priority_plot = (
        alt.Chart(df)
        .mark_arc()
        .encode(theta="count():Q", color="Priority:N")
        .properties(height=300)
        .configure_legend(
            orient="bottom", titleFontSize=14, labelFontSize=14, titlePadding=5
        )
    )
    st.altair_chart(priority_plot, use_container_width=True, theme="streamlit")
