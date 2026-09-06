# Internal image registry

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
