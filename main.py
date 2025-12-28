# Hyperion
# Copyright (C) 2025 Arian Ott <arian.ott@ieee.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import asyncio
import json
import os
import logging
import aiohttp
import websockets
import uuid
import re
import socket
import ipaddress  # Neu für Broadcast-Berechnung
from aiohttp import web
from pyartnet import ArtNetNode

# Konfiguration
CONFIG_FILE = "node_config.json"
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("Hyperion-Node")


def get_mac_address():
    """Ermittelt die MAC-Adresse des Geräts."""
    mac = ':'.join(re.findall('..', '%012x' % uuid.getnode()))
    return mac


def get_local_ip():
    """Ermittelt die eigene IP-Adresse."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP


def get_subnet_broadcast(ip):
    """
    Berechnet die Broadcast-Adresse für das lokale Subnetz.
    Nimmt vereinfacht eine /24 Maske (255.255.255.0) an, was für Heimnetze Standard ist.
    Macht aus '192.168.178.50' -> '192.168.178.255'.
    """
    try:
        # Erstellt ein Netzwerk-Objekt mit /24 Maske
        net = ipaddress.IPv4Network(f"{ip}/24", strict=False)
        return str(net.broadcast_address)
    except Exception:
        return "255.255.255.255"


# --- TEIL 1: SETUP SERVER ---

async def handle_setup_page(request):
    my_mac = get_mac_address()
    my_ip = get_local_ip()

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Hyperion Node Setup</title>
        <style>
            body {{ font-family: -apple-system, system-ui, sans-serif; padding: 2rem; max-width: 500px; margin: 0 auto; background: #1a1a1a; color: white; }}
            input {{ width: 100%; padding: 10px; margin: 5px 0 20px 0; border-radius: 4px; border: none; box-sizing: border-box; }}
            button {{ width: 100%; padding: 15px; background: #007bff; color: white; border: none; border-radius: 4px; font-weight: bold; cursor: pointer; }}
            label {{ font-weight: bold; color: #ccc; }}
            .info {{ font-size: 0.8rem; color: #666; margin-bottom: 20px; }}
        </style>
    </head>
    <body>
        <h1>🔌 Node Enrollment</h1>
        <div class="info">Device MAC: {my_mac} | IP: {my_ip}</div>
        
        <form action="/register" method="post">
            <label>Hyperion Server URL</label>
            <input type="url" name="host" placeholder="http://192.168.178.50:8000" required>
            
            <label>Node Name</label>
            <input type="text" name="name" placeholder="Stage-Left-01" required>
            
            <label>ArtNet Target IP (Lass 255.255.255.255 für Auto-Broadcast)</label>
            <input type="text" name="artnet_ip" value="255.255.255.255" required>

            <label>OTP Code</label>
            <input type="text" name="otp" placeholder="XY1234" required style="text-transform: uppercase; letter-spacing: 5px; font-family: monospace;">
            
            <button type="submit">Verbinden & Registrieren</button>
        </form>
    </body>
    </html>
    """
    return web.Response(text=html, content_type='text/html')


async def handle_register(request):
    data = await request.post()
    server_base = data.get('host').rstrip("/")
    node_name = data.get('name')
    otp_code = data.get('otp').upper()
    artnet_ip = data.get('artnet_ip')

    mac_address = get_mac_address()
    register_url = f"{server_base}/api/dmx/otp-authenticate"

    logger.info(f"Registrierung bei {register_url} mit MAC {mac_address}...")

    async with aiohttp.ClientSession() as session:
        try:
            payload = {
                "otp": otp_code,
                "mac_adress": mac_address,
                "name": node_name
            }

            async with session.post(register_url, json=payload) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    device_secret = result.get('device_secret')

                    if not device_secret:
                        return web.Response(text="<h1>❌ Fehler</h1><p>Kein device_secret erhalten.</p>")

                    new_config = {
                        "server_url": server_base,
                        "node_name": node_name,
                        "device_secret": device_secret,
                        "artnet_ip": artnet_ip,
                        "mac_address": mac_address
                    }

                    with open(CONFIG_FILE, "w") as f:
                        json.dump(new_config, f, indent=2)

                    return web.Response(text="<h1>✅ Erfolg!</h1><p>Node registriert. Neustart...</p>")
                else:
                    text = await resp.text()
                    return web.Response(text=f"<h1>❌ Fehler {resp.status}</h1><p>{text}</p>")
        except Exception as e:
            return web.Response(text=f"<h1>❌ Error: {e}</h1>")


async def run_setup_server():
    app = web.Application()
    app.add_routes([web.get('/', handle_setup_page),
                    web.post('/register', handle_register)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 80)
    logger.info("⚠️ SETUP MODUS: http://<PI-IP>/")
    await site.start()

    while not os.path.exists(CONFIG_FILE):
        await asyncio.sleep(1)

    await runner.cleanup()


# --- TEIL 2: DMX / ARTNET ENGINE ---

async def run_dmx_client(config):
    target_ip = config.get("artnet_ip", "255.255.255.255")

    # 1. FIX: Eigene IP ermitteln
    my_ip = get_local_ip()

    # 2. FIX: Wenn Target globaler Broadcast ist, berechne Subnetz-Broadcast
    # Das löst das Windows Permission Problem ohne bind_ip
    if target_ip == "255.255.255.255":
        target_ip = get_subnet_broadcast(my_ip)
        logger.info(f"Broadcast optimiert für Windows: {target_ip}")

    logger.info(f"Starte ArtNet Node -> Target: {target_ip} (von {my_ip})")

    # 3. FIX: bind_ip entfernt, da pyartnet das hier nicht unterstützt
    async with ArtNetNode.create(target_ip, port=6454, max_fps=30, refresh_every=1) as node:

        universe_channels = {}

        ws_base = config['server_url'].replace("http://", "ws://")
        uri = f"{ws_base}/dmx?token={config['device_secret']}"
        logger.info(
            f"Verbinde zu Hyperion Core als '{config['node_name']}'...")

        while True:
            try:
                async with websockets.connect(uri) as websocket:
                    logger.info("🟢 Verbunden! Warte auf DMX Daten...")

                    async for message in websocket:
                        # 4. Binary Check (Header > 2 Bytes)
                        if isinstance(message, bytes) and len(message) > 2:

                            # Universum lesen (Big Endian)
                            u_id = int.from_bytes(
                                message[0:2], byteorder='big')
                            dmx_len = len(message) - 2
                            logger.info(f"DEBUG: Empfange {dmx_len} Kanäle für Universum {u_id}")
                            # --- DEBUGGING END ---
                            # DMX Werte lesen
                            dmx_raw = message[2:]
                            dmx_values = list(dmx_raw)

                            if u_id not in universe_channels:
                                logger.info(
                                    f"Registriere ArtNet Universum {u_id}")
                                universe = node.add_universe(u_id)
                                channel = universe.add_channel(
                                    start=1, width=512, channel_name=f"Univ-{u_id}")
                                universe_channels[u_id] = channel

                            if len(dmx_values) < 512:
                                dmx_values.extend(
                                    [0] * (512 - len(dmx_values)))

                            universe_channels[u_id].set_values(dmx_values)

                        elif isinstance(message, str):
                            logger.info(f"Server Info: {message}")

            except websockets.exceptions.ConnectionClosed:
                logger.warning("🔴 Verbindung verloren. Retry in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Kritischer Fehler: {e}")
                await asyncio.sleep(5)


# --- MAIN ---

async def main():
    if not os.path.exists(CONFIG_FILE):
        await run_setup_server()

    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)
        await run_dmx_client(config)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
