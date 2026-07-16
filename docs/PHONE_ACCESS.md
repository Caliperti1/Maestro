# Phone Access

Maestro should be testable from a phone as early as possible. The local MVP supports two access paths: same-network LAN access first, then Tailscale for secure remote access.

## Same-Network LAN Access

1. Start the backend bound to all interfaces:

   ```bash
   make backend-reload
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

For secure access away from the local network, use your private Tailnet IP rather than exposing
Maestro publicly. The frontend target below is configured for this Mac's current Tailscale address
(`100.66.109.2`).

1. Install Tailscale on the Mac and phone and sign into the same tailnet.
2. Add the following to `.env`, then restart the backend so its CORS policy accepts the private
   frontend origin:

   ```bash
   TAILSCALE_FRONTEND_ORIGIN=http://100.66.109.2:5173
   ```

3. Start the backend on all interfaces:

   ```bash
   make backend-reload
   ```

4. From the repository root, start the remote-capable frontend:

   ```bash
   make frontend-tailscale
   ```

5. Open the same Maestro instance from the phone:

   ```text
   http://100.66.109.2:5173
   ```

The Mac can still use `http://localhost:5173` at the same time; both clients talk to the same local
database and backend. `TAILSCALE_IP` can be overridden if this device's Tailscale address changes:

```bash
make frontend-tailscale TAILSCALE_IP=<new-tailscale-ip>
```

Do not expose early Maestro builds by opening router ports directly to the internet. For a later
polished setup, Tailscale Serve can supply a stable private HTTPS hostname.
