# Host configuration

Machine-level settings the stack depends on but cannot apply itself — they
need root, so they are not part of any script here.

## `resolved-mdns.conf` — keep `.local` working under ProtonVPN

Only relevant if you reach your server by an mDNS `.local` name.

**Symptom:** `scripts/transfer.py` and the media-server scan stop working,
because the `.local` hostname stops resolving. The server is fine, reachable
by IP, and nothing logs a reason.

**Cause:** ProtonVPN takes DNS over completely. The LAN link is left with *no*
resolver of its own, so every query goes to systemd-resolved, which — with
mDNS disabled — answers `.local` with an **authoritative NXDOMAIN** rather
than routing it to multicast. avahi knows the answer the entire time and is
never asked. `nsswitch.conf` is `files mdns4_minimal [NOTFOUND=return] dns`,
so once `mdns4_minimal` defers the chain stops there anyway.

PIA never caused this: it left the LAN link's DNS and default route intact
underneath its `0.0.0.0/1` + `128.0.0.0/1` split. Proton's kill switch does
not, so this appears the moment you migrate.

**Fix** (root, once per machine):

```bash
sudo cp deploy/host/resolved-mdns.conf /etc/systemd/resolved.conf.d/mdns.conf
sudo resolvectl mdns wlp1s0 yes          # substitute your LAN interface
sudo systemctl restart systemd-resolved
```

The `resolvectl` line applies it now; the drop-in makes it survive a reboot.
Both are needed.

**Verify:**

```bash
getent hosts your-server.local   # expect an address
curl -s -o /dev/null -w '%{http_code}\n' http://your-server.local:8096/System/Info/Public
```

`200` from the second means the media pipeline is whole again. Note the name
resolves to an IPv6 link-local address (`fe80::…`) rather than the IPv4 one —
that is normal, and ssh, rsync and curl all handle it.
