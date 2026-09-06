# Internal image registry

The Worker image is stored on a 20 GiB, three-replica Longhorn volume and is
reachable only from the homelab LAN and cluster nodes at
`172.16.40.200:5000`. Talos is configured with `insecureSkipVerify` for this
private endpoint; transport is still TLS, while trust does not depend on a
public CA.

The `registry-tls` Secret is created out-of-band and must never be committed.
Use a long-lived self-signed certificate with `IP:172.16.40.200` in its SAN,
then restart the Deployment. The image data itself survives Pod rescheduling
and is replicated across all three storage nodes by Longhorn.
