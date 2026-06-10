# Phone Access

Maestro should be testable from a phone as early as possible. The local MVP supports two access paths: same-network LAN access first, then Tailscale for secure remote access.

## Same-Network LAN Access

1. Start the backend bound to all interfaces:

   ```bash
   uvicorn app.api.main:app --host 0.0.0.0 --port 8000
   ```

2. Start the frontend bound to all interfaces:

   ```bash
   cd frontend
   npm run dev -- --host 0.0.0.0
   ```

3. Find the Mac LAN IP:

   ```bash
   ipconfig getifaddr en0
   ```

   If that returns nothing, try:

   ```bash
   ipconfig getifaddr en1
   ```

4. Open the app from the phone while it is on the same network:

   ```text
   http://<mac-lan-ip>:5173
   ```

## macOS Firewall Notes

macOS may ask whether to allow incoming connections for Python, Node, or the terminal app. Allow the connection for local testing. If the phone cannot connect:

- Confirm the phone and Mac are on the same Wi-Fi network.
- Confirm the frontend is running with `--host 0.0.0.0`.
- Confirm the backend is running with `--host 0.0.0.0`.
- Check System Settings -> Network -> Firewall.

## Tailscale Path

For secure access away from the local network:

1. Install Tailscale on the Mac.
2. Install Tailscale on the phone.
3. Sign into the same tailnet.
4. Enable MagicDNS if desired.
5. Start the backend and frontend with the same commands above.
6. Open the frontend using the Mac Tailscale IP or MagicDNS name:

   ```text
   http://<mac-tailscale-ip>:5173
   ```

Do not expose early Maestro builds by opening router ports directly to the internet.
