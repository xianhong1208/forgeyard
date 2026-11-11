# Security

## Reporting a vulnerability

Please report it privately through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability**. Do not open a public issue for it.

Say what an attacker can do, what they need first (an account? which role? network access to the
server, an agent or a replica?), and how to reproduce it. A first answer comes within a week. A
fix is released as a patch version, and the advisory names you unless you ask otherwise.

Only the latest minor release gets security fixes.

## What Forgeyard assumes

Forgeyard is built for **one organisation's own machines on a LAN or VPN**. It is not hardened to
face the internet on its own; put it behind a VPN or a reverse proxy you trust (see
[docs/https-and-trust.md](docs/https-and-trust.md)).

- **Admins and operators are trusted with the machines.** Anyone who may create a workload can run
  code on them, which is why only those two roles can. Users can only call deployed models with
  their own API keys and see their own usage.
- **An agent runs with the rights it is given.** As root, with the host's Docker socket, it
  controls that host. Process workloads never run as root (`--workload-user`), get an empty
  environment and only their GPUs; containers never run `--privileged`. See *Isolation* in
  [docs/design.md](docs/design.md).
- **The server's data directory is the crown jewels.** It holds the database, the certificate
  authority's key (`data/tls/`) and the key that encrypts stored secrets (`data/secret.key`). Back
  it up, keep it private, and never commit it.

## What a report can expect us to treat as a vulnerability

- A user (or anyone without an account) reaching what their role does not allow: another
  person's keys, usage or sessions, workloads, admin settings.
- An API key used beyond its models, limits, cloud setting or budget.
- A request reaching a model replica without going through the gateway.
- Secrets leaving where they are stored: API keys (only their SHA-256 is kept), provider keys,
  Hugging Face tokens, workload environment values, the server's keys.
- A workload getting GPUs, environment or host access it was not given.
- Data sent to a cloud provider for a key, route or request that does not allow it.

## Known limits (not vulnerabilities)

- A process workload on NVIDIA is kept to its cards cooperatively (`CUDA_VISIBLE_DEVICES`);
  `/dev/nvidia*` is usually world-readable. Use the container runtime when that matters.
- Without systemd, process workloads get rlimits only: CPU is counted for placement, not capped.
- Costs are estimates from the provider's reported tokens and the prices you set, not invoices; a
  single request can go past a budget by the difference between its estimate and its real cost.
