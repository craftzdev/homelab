# Internal image registry

> **Read-only.** Every workload pulls from Harbor (`172.16.40.201:5000`). This
> registry has no authentication, so it runs in read-only maintenance mode:
> pushes and deletes return `405 Method Not Allowed`. It is kept only as a
> fallback until Harbor recovery has been verified, and should then be removed
> together with its `RegistryTLSConfig` in `tofu/10-proxmox-talos`.

The Worker image is stored on a 20 GiB, three-replica Longhorn volume and is
reachable only from the homelab LAN and cluster nodes at
`172.16.40.200:5000`. Talos trusts the registry's self-signed CA through the
declarative `RegistryTLSConfig` in `talos/patches/registry-tls.yaml.tftpl`.
Certificate verification remains enabled.

The `registry-tls` Secret (including its private key) is created out-of-band
and must never be committed. The public certificate is committed separately
at `talos/certs/registry-ca.crt` so every rebuilt node receives the same trust
configuration through the Talos API.
Use a long-lived self-signed certificate with `IP:172.16.40.200` in its SAN,
then restart the Deployment. The image data itself survives Pod rescheduling
and is replicated across all three storage nodes by Longhorn.
