import datetime
import random
import sqlite3

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

# Configuración de la página y título.
st.set_page_config(page_title="Tickets de soporte", page_icon="🎫")
st.title("🎫 Tickets de soporte")
st.write(
    """
    Esta aplicación muestra cómo puedes construir una herramienta interna en Streamlit. Aquí implementamos un flujo de trabajo para tickets de soporte. El usuario puede crear un ticket, editar tickets existentes y ver algunas estadísticas.
    """
)

# Funciones para la base de datos
def obtener_tickets_db():
    conn = sqlite3.connect('helpdesk.db')
    c = conn.cursor()
    c.execute('SELECT id, issue, status, priority, date_submitted FROM tickets ORDER BY id DESC')
    rows = c.fetchall()
    conn.close()
    return rows

def agregar_ticket_db(issue, priority):
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
    c.execute('INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?, ?)', (new_id, issue, "Abierto", priority, today, usuario, sede))
    conn.commit()
    conn.close()
    return new_id, issue, "Abierto", priority, today

def actualizar_tickets_db(df):
    conn = sqlite3.connect('helpdesk.db')
    c = conn.cursor()
    for _, row in df.iterrows():
        c.execute('''UPDATE tickets SET issue=?, status=?, priority=?, date_submitted=? WHERE id=?''',
                  (row['Issue'], row['Status'], row['Priority'], row['Date Submitted'], row['ID']))
    conn.commit()
    conn.close()

# Crear un dataframe de Pandas con tickets existentes aleatorios.
if "df" not in st.session_state:
    rows = obtener_tickets_db()
    df = pd.DataFrame(rows, columns=["ID", "Issue", "Status", "Priority", "Date Submitted"])
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
        issue = st.text_area("Describe el problema")
        priority = st.selectbox("Prioridad", ["Alta", "Media", "Baja"])
        submitted = st.form_submit_button("Enviar ticket")

    if submitted:
        new_ticket = agregar_ticket_db(issue, priority)
        df_new = pd.DataFrame([
            {
                "ID": new_ticket[0],
                "Issue": new_ticket[1],
                "Status": new_ticket[2],
                "Priority": new_ticket[3],
                "Date Submitted": new_ticket[4]
            }
        ])
        st.write("¡Ticket enviado! Detalles:")
        st.dataframe(df_new, use_container_width=True, hide_index=True)
        # Recargar los tickets desde la base de datos
        rows = obtener_tickets_db()
        st.session_state.df = pd.DataFrame(rows, columns=["ID", "Issue", "Status", "Priority", "Date Submitted", "usuario", "sede"])

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
    edited_df = st.data_editor(
        st.session_state.df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Status": st.column_config.SelectboxColumn(
                "Estado",
                help="Estado del ticket",
                options=["Abierto", "En progreso", "Cerrado"],
                required=True,
            ),
            "Priority": st.column_config.SelectboxColumn(
                "Prioridad",
                help="Prioridad",
                options=["Alta", "Media", "Baja"],
                required=True,
            ),
        },
        disabled=["ID", "Date Submitted"],
    )
    # Guardar cambios en la base de datos si hay edición
    if not edited_df.equals(st.session_state.df):
        actualizar_tickets_db(edited_df)
        st.session_state.df = edited_df
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
