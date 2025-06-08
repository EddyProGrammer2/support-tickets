import datetime
import random
import sqlite3

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st
from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode

# --- Configuración de notificaciones por email (Gmail) ---
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

def send_email_gmail(subject, body, to_email):
    # Configura estos datos con tu cuenta de Gmail y contraseña de aplicación
    gmail_user = 'TU_CORREO@gmail.com'
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
        c.execute('''UPDATE tickets SET issue=?, status=?, priority=?, date_submitted=?, asignado=? WHERE id=?''',
                  (row['Issue'], row['Status'], row['Priority'], row['Date Submitted'], row['asignado'], row['ID']))
    conn.commit()
    conn.close()

# Crear un dataframe de Pandas con tickets existentes aleatorios.
if "df" not in st.session_state:
    rows = obtener_tickets_db()
    df = pd.DataFrame(rows, columns=["ID", "Issue", "Status", "Priority", "Date Submitted", "usuario", "sede", "tipo", "asignado"])
    st.session_state.df = df

# Selección de rol al inicio
rol = st.sidebar.selectbox(
    "Selecciona tu rol",
    ["Usuario", "Soporte"],
    help="Elige si eres usuario final o personal de soporte"
)

if rol == "Usuario":
    st.header("Enviar un ticket de soporte")
    with st.form("add_ticket_form"):
        usuario = st.text_input("Usuario", placeholder="Nombre y Apellido")
        sede = st.selectbox("Seleccionar sede", ["Catia", "La Guaira", "Mariche", "CENDIS", "Fabrica y Laminadora"])
        tipo = st.selectbox("Tipo de ticket", ["Problema técnico", "Solicitud de información", "Otro"])
        issue = st.text_area("Describe el problema")
        priority = st.selectbox("Prioridad", ["Alta", "Media", "Baja"])
        archivo_usuario = st.file_uploader("Adjuntar archivo (opcional)", type=None, key="file_usuario")
        submitted = st.form_submit_button("Enviar ticket")

    if submitted:
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
    st.write(f"Número de tickets: `{len(st.session_state.df)}`")
    st.info(
        "Puedes editar los tickets haciendo doble clic en una celda. Los reportes se actualizan automáticamente.",
        icon="✍️",
    )

    # Filtros avanzados
    with st.expander("🔎 Filtros avanzados", expanded=False):
        colf1, colf2, colf3 = st.columns(3)
        with colf1:
            estado_filtro = st.multiselect("Estado", options=st.session_state.df["Status"].unique().tolist(), default=st.session_state.df["Status"].unique().tolist())
            prioridad_filtro = st.multiselect("Prioridad", options=st.session_state.df["Priority"].unique().tolist(), default=st.session_state.df["Priority"].unique().tolist())
        with colf2:
            usuario_filtro = st.multiselect("Usuario", options=st.session_state.df["usuario"].unique().tolist(), default=st.session_state.df["usuario"].unique().tolist())
            sede_filtro = st.multiselect("Sede", options=st.session_state.df["sede"].unique().tolist(), default=st.session_state.df["sede"].unique().tolist())
        with colf3:
            tipo_filtro = st.multiselect("Tipo", options=st.session_state.df["tipo"].unique().tolist(), default=st.session_state.df["tipo"].unique().tolist())
            fechas = st.date_input("Rango de fechas", [])

    df_filtrado = st.session_state.df[
        st.session_state.df["Status"].isin(estado_filtro)
        & st.session_state.df["Priority"].isin(prioridad_filtro)
        & st.session_state.df["usuario"].isin(usuario_filtro)
        & st.session_state.df["sede"].isin(sede_filtro)
        & st.session_state.df["tipo"].isin(tipo_filtro)
    ]
    # Filtrado por rango de fechas si se selecciona
    if fechas and len(fechas) == 2:
        try:
            fecha_inicio = fechas[0].strftime("%d-%m-%Y")
            fecha_fin = fechas[1].strftime("%d-%m-%Y")
            df_filtrado = df_filtrado[
                pd.to_datetime(df_filtrado["Date Submitted"], format="%d-%m-%Y") >= pd.to_datetime(fecha_inicio, format="%d-%m-%Y")
            ]
            df_filtrado = df_filtrado[
                pd.to_datetime(df_filtrado["Date Submitted"], format="%d-%m-%Y") <= pd.to_datetime(fecha_fin, format="%d-%m-%Y")
            ]
        except Exception:
            st.warning("Formato de fecha inválido en los datos.")

    # Mostrar y editar tickets con AgGrid (solo resumen)
    resumen_cols = ["ID", "Issue", "Status", "Priority", "Date Submitted"]
    df_resumen = df_filtrado[resumen_cols].copy()
    gb = GridOptionsBuilder.from_dataframe(df_resumen)
    gb.configure_selection('single', use_checkbox=False)
    gb.configure_column("Status", editable=True, cellEditor='agSelectCellEditor', cellEditorParams={"values": ["Abierto", "En progreso", "Cerrado"]})
    gb.configure_column("Priority", editable=True, cellEditor='agSelectCellEditor', cellEditorParams={"values": ["Alta", "Media", "Baja"]})
    gb.configure_column("ID", editable=False)
    gb.configure_column("Date Submitted", editable=False)
    grid_options = gb.build()
    grid_response = AgGrid(
        df_resumen,
        gridOptions=grid_options,
        update_mode=GridUpdateMode.MODEL_CHANGED | GridUpdateMode.SELECTION_CHANGED,
        allow_unsafe_jscode=True,
        enable_enterprise_modules=False,
        fit_columns_on_grid_load=True,
        height=350,
        key="aggrid_tickets"
    )
    # Reflejar ediciones en el DataFrame original
    edited_df = df_filtrado.copy()
    for idx, row in grid_response["data"].iterrows() if hasattr(grid_response["data"], 'iterrows') else enumerate(grid_response["data"]):
        if isinstance(row, dict):
            ticket_id = row["ID"]
            for col in ["Status", "Priority"]:
                edited_df.loc[edited_df["ID"] == ticket_id, col] = row[col]
        else:
            ticket_id = row["ID"]
            for col in ["Status", "Priority"]:
                edited_df.loc[edited_df["ID"] == ticket_id, col] = row[col]
    selected_ticket_id = None
    if grid_response["selected_rows"] is not None and len(grid_response["selected_rows"]) > 0:
        sel = grid_response["selected_rows"]
        if isinstance(sel, list):
            selected_ticket_id = sel[0]["ID"]
        else:
            selected_ticket_id = sel.iloc[0]["ID"]
    # Exportar tickets filtrados
    import io
    st.markdown("### Exportar tickets filtrados")
    col_exp1, col_exp2 = st.columns(2)
    with col_exp1:
        csv = df_filtrado.to_csv(index=False).encode('utf-8')
        st.download_button(
            label="Descargar CSV",
            data=csv,
            file_name="tickets_filtrados.csv",
            mime="text/csv"
        )
    with col_exp2:
        output = io.BytesIO()
        df_filtrado.to_excel(output, index=False, engine='xlsxwriter')
        st.download_button(
            label="Descargar Excel",
            data=output.getvalue(),
            file_name="tickets_filtrados.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    # Mostrar detalle, historial, comentarios y adjuntos justo debajo de la tabla si hay ticket seleccionado en AgGrid
    if selected_ticket_id:
        # Mostrar detalle del ticket
        ticket_detalle = df_filtrado[df_filtrado["ID"] == selected_ticket_id]
        if not ticket_detalle.empty:
            st.markdown("#### Detalle del ticket seleccionado")
            st.json(ticket_detalle.iloc[0].to_dict())

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
        st.subheader(f"Historial, comentarios y adjuntos del ticket {selected_ticket_id}")
        historial = obtener_historial(selected_ticket_id)
        import os
        from urllib.parse import unquote
        if historial:
            for h in historial:
                fecha, usuario_hist, comentario = h
                # Detectar si es un adjunto en base de datos
                if comentario.startswith("[Archivo adjunto BD](") and comentario.endswith(")"):
                    nombre_archivo = comentario[len("[Archivo adjunto BD]("): -1]
                    nombre_archivo = unquote(nombre_archivo)
                    # Recuperar adjunto de la base de datos
                    conn = sqlite3.connect('helpdesk.db')
                    c = conn.cursor()
                    c.execute('SELECT tipo_mime, contenido FROM adjuntos WHERE ticket_id = ? AND nombre_archivo = ? ORDER BY id DESC LIMIT 1', (selected_ticket_id, nombre_archivo))
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
            st.write("Sin comentarios ni historial aún.")
        # Adjuntar archivos en base de datos
        st.markdown("**Adjuntar archivo al ticket**")
        archivo = st.file_uploader("Selecciona un archivo para adjuntar", type=None, key=f"file_{selected_ticket_id}")
        if archivo is not None:
            import mimetypes
            nombre_archivo = archivo.name
            tipo_mime = mimetypes.guess_type(nombre_archivo)[0] or archivo.type or "application/octet-stream"
            contenido = archivo.getbuffer().tobytes()
            fecha = datetime.datetime.now().strftime("%d-%m-%Y %H:%M")
            usuario_adj = "Soporte"
            # Guardar en base de datos
            conn = sqlite3.connect('helpdesk.db')
            c = conn.cursor()
            c.execute('INSERT INTO adjuntos (ticket_id, nombre_archivo, tipo_mime, contenido, fecha, usuario) VALUES (?, ?, ?, ?, ?, ?)',
                      (selected_ticket_id, nombre_archivo, tipo_mime, contenido, fecha, usuario_adj))
            conn.commit()
            conn.close()
            # Registrar en historial
            agregar_comentario(selected_ticket_id, usuario_adj, f"[Archivo adjunto BD]({nombre_archivo})")
            st.success(f"Archivo '{archivo.name}' adjuntado.")
            st.experimental_rerun()

        with st.form("form_comentario"):
            usuario_hist = st.text_input("Usuario (opcional)", value="Soporte")
            comentario = st.text_area("Agregar comentario o acción al historial")
            enviar_com = st.form_submit_button("Agregar comentario")
        if enviar_com and comentario.strip():
            agregar_comentario(selected_ticket_id, usuario_hist, comentario.strip())
            st.success("Comentario agregado.")
            st.experimental_rerun()

    # Guardar cambios en la base de datos si hay edición
    if not edited_df.equals(df_filtrado):
        # Detectar cambios de estado y notificar
        for idx, row in edited_df.iterrows():
            ticket_id = row['ID']
            nuevo_estado = row['Status']
            # Buscar el estado anterior
            estado_anterior = st.session_state.df.loc[st.session_state.df['ID'] == ticket_id, 'Status'].values[0]
            if nuevo_estado != estado_anterior:
                # Notificar a soporte
                try:
                    send_email_gmail(
                        subject=f"Ticket {ticket_id} actualizado",
                        body=f"El estado del ticket {ticket_id} ha cambiado de '{estado_anterior}' a '{nuevo_estado}'.",
                        to_email=EMAIL_DESTINO_SOPORTE
                    )
                except Exception as e:
                    st.warning(f"No se pudo enviar el email de notificación a soporte: {e}")
                # Notificar al usuario si su campo parece un email
                usuario_email = row['usuario']
                if isinstance(usuario_email, str) and '@' in usuario_email:
                    try:
                        send_email_gmail(
                            subject=f"Actualización de su ticket {ticket_id}",
                            body=f"Su ticket {ticket_id} ha cambiado de estado: {estado_anterior} → {nuevo_estado}.",
                            to_email=usuario_email
                        )
                    except Exception as e:
                        st.warning(f"No se pudo enviar el email al usuario: {e}")
        actualizar_tickets_db(edited_df)
        # Actualizar solo los tickets filtrados en la sesión
        st.session_state.df.update(edited_df)
    st.header("Estadísticas")
    col1, col2, col3 = st.columns(3)
    num_open_tickets = len(st.session_state.df[st.session_state.df.Status == "Abierto"])
    col1.metric(label="Tickets abiertos", value=num_open_tickets, delta=10)
    col2.metric(label="Tiempo primera respuesta (horas)", value=5.2, delta=-1.5)
    col3.metric(label="Tiempo promedio de resolución (horas)", value=16, delta=2)
    st.write("")
    st.write("##### Tickets por estado y mes")
    status_plot = (
        alt.Chart(edited_df)
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
        alt.Chart(edited_df)
        .mark_arc()
        .encode(theta="count():Q", color="Priority:N")
        .properties(height=300)
        .configure_legend(
            orient="bottom", titleFontSize=14, labelFontSize=14, titlePadding=5
        )
    )
    st.altair_chart(priority_plot, use_container_width=True, theme="streamlit")

