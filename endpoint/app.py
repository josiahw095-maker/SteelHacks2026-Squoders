"""Mesh Mail endpoint - the inbox you read when there is no internet.

    streamlit run endpoint/app.py

Everything that touches the radio lives in station.py. This file is only the
screen, so replacing it with a TypeScript front end later means reimplementing
the Station methods it calls: Threads, Waiting, Status, Reply, Compose.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import streamlit as st
from station import Station

st.set_page_config(page_title = "Mesh Mail", page_icon = "\U0001F4E1", layout = "wide")


@st.cache_resource
def GetStation(port, mock):
    """One radio connection shared by every rerun and every browser tab."""
    station = Station(port = port or None, mock = mock)
    if mock:
        station.SeedDemo()
    return station


def WhenText(stamp):
    return time.strftime("%H:%M", time.localtime(stamp))


def SendWithBar(slot, label, action):
    """Run a send, drawing how many packets have gone out so far.

    A full packet is seconds of airtime, so this is real progress, not a
    decorative spinner.
    """
    bar = slot.progress(0.0, text = f"{label}...")

    def Tick(sent, total):
        bar.progress(sent / total, text = f"{label}  -  packet {sent} of {total}")

    count = action(Tick)
    bar.progress(1.0, text = f"{label}  -  {count} packet(s) away")
    return count


# --- sidebar: the connection ------------------------------------------------

with st.sidebar:
    st.header("Connection")
    demo = st.toggle("Demo mode", value = True,
                     help = "Run with no radio attached, using sample mail.")
    port = st.text_input("Node port", value = "loopback", disabled = demo,
                         help = "A COM port for USB, a Bluetooth name, "
                                "or 'loopback' to run with no radio.")

    station = GetStation("" if demo else port, demo)
    st.caption(station.Status())

    if station.error and not demo:
        st.error(station.error)
        st.caption("Close anything else holding the port, then rerun.")

    inbox = station.Inbox()
    if inbox:
        air = sum(m["airbytes"] for m in inbox)
        got = sum(m["delivered"] for m in inbox)
        st.metric("Carried over LoRa", f"{air} B",
                  delta = f"{got} B delivered", delta_color = "off")

    waiting = station.Waiting()
    if waiting:
        st.warning(f"{len(waiting)} message(s) still arriving")
        for chunk_id, (count, asks) in waiting.items():
            chased = f", asked {asks}x" if asks else ""
            st.caption(f"  {chunk_id}: {count} packet(s) so far{chased}")

    st.divider()
    st.header("New email")
    with st.form("compose", clear_on_submit = True):
        to = st.text_input("To")
        subject = st.text_input("Subject")
        body = st.text_area("Message", height = 100)
        if st.form_submit_button("Send", use_container_width = True):
            if "@" not in to:
                st.error("That does not look like an address.")
            elif not body.strip():
                st.error("Nothing to send.")
            else:
                SendWithBar(st, "Sending",
                            lambda tick: station.Compose(to, subject, body,
                                                         on_progress = tick))


# --- main: the inbox --------------------------------------------------------

st.title("Mesh Mail")
st.caption("Email carried over LoRa. No cellular, no Wi-Fi at this end.")


@st.fragment(run_every = 2)
def Inbox():
    station.Poll()          # no-op with a real radio; pulls the spool otherwise
    station.Chase()         # ask for anything that has stalled
    threads = station.Threads()
    if not threads:
        st.info("Nothing yet. Mail appears here as the radio receives it.")
        return

    for thread, messages in threads:
        newest = messages[-1]
        label = f"{newest['subject'] or '(no subject)'}  -  {newest['sender']}"
        with st.expander(label, expanded = (thread == threads[0][0])):
            for message in messages:
                st.markdown(f"**{message['sender']}** &nbsp; `{WhenText(message['at'])}`")
                st.write(message["body"])
                st.caption(
                    f"{message['packets']} packet(s) &nbsp;|&nbsp; "
                    f"{message['airbytes']} bytes on air &nbsp;|&nbsp; "
                    f"{message['delivered']} bytes delivered &nbsp;|&nbsp; "
                    f"{message['ratio']:.1f}x", unsafe_allow_html = True)
                st.divider()

            with st.form(f"reply-{thread}", clear_on_submit = True):
                answer = st.text_area("Reply", key = f"text-{thread}", height = 80,
                                      label_visibility = "collapsed",
                                      placeholder = "Write a reply...")
                if st.form_submit_button("Send reply"):
                    if answer.strip():
                        SendWithBar(st, "Sending reply",
                                    lambda tick: station.Reply(thread, answer,
                                                               on_progress = tick))
                    else:
                        st.error("Nothing to send.")


Inbox()
