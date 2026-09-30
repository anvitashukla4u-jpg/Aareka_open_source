# Aareka - A Product of AICON Transformation Solutions

**The trusted energy ledger for private and hybrid AI datacenters.**

Aareka reconciles the physical power your servers draw — GPU, CPU, and whole-box —
into a confidence-scored figure for energy consumed over a reporting interval, and
attributes it to the workloads, applications, and business units responsible. It
accounts for IT load first; facility overhead and cooling (PUE) are separate
allocation layers, not implied by these figures. Every number is tagged **measured or
estimated** and carries an explicit confidence. It runs on the heterogeneous estates
that Kubernetes-native cost tools can't read: bare metal, VMware, mixed vendors, and
incomplete telemetry.

It is **read-only**. It never touches production. It tells you what is actually
happening, and — where a constraint appears — what you could do about it. It never
does it for you.

---

## Quickstart — try Aareka on your own hardware

Self-serve: you create your own private space, get a collector key, run the read-only
collector on a GPU box, and watch your power get attributed. The collector makes
outbound HTTPS calls only — it sends power readings and workload ownership, never
commands, and nothing else leaves your machine.

**1. Create your space and get your key.** Go to **aareka.aicontransformation.com**,
click **Create account**, and sign up with your email and a password. Leave *team key*
blank — that's only for joining an existing team. You'll be shown your **collector
key**; copy it (you can find it again anytime on your dashboard).

**2. Prerequisites** (details in [PREREQUISITES.md](PREREQUISITES.md))
- **Python 3.9+** — the only hard requirement.
- **GPU power:** the NVIDIA driver, so `nvidia-smi` works (gives *measured* GPU power).
- **Best whole-machine accuracy (optional):** read-only BMC/IPMI/Redfish access.

**3. Get the collector**
```bash
git clone https://github.com/anvitashukla4u-jpg/Aareka_open_source.git
cd Aareka_open_source
```

**4. Point it at your space** (paste your key from step 1 — keep it private, don't commit it)
```bash
export AAREKA_INGEST_URL="https://aareka.aicontransformation.com"
export AAREKA_ORG_KEY="<your collector key from the dashboard>"
# Optional: label who owns what. Maps a process name or VM name -> a department.
export AAREKA_DEPT_MAP='{"training-job":"Research","inference-svc":"Platform"}'
```

**5. Send one batch and read the receipt**
```bash
python aareka_sender.py --once
```
Expected output (numbers illustrative):
```
  Aareka sender  ->  https://aareka.aicontransformation.com   host=gpu-node-01
  sent full (watts + ownership) -> HTTP 200: readings=3 attributed=2
      Research          440.5 W
      Platform           59.5 W
```

**6. See it on your dashboard.** Refresh **aareka.aicontransformation.com** — your
workloads, departments, and measured-vs-estimated power appear. Your login only ever
shows your own space's data.

**Keep it running** (send every 60 s instead of once):
```bash
python aareka_sender.py            # Ctrl+C to stop
```

**No GPU handy?** You can exercise the vGPU/MIG path from a saved dump:
```bash
export AAREKA_VGPU_QFILE=/path/to/nvidia-smi-vgpu-q.txt
python aareka_sender.py --once
```

Trouble sending? `python aareka_sender.py --once` prints the exact HTTP error. The
most common causes are a wrong/missing `AAREKA_ORG_KEY` (HTTP 401) or no outbound
HTTPS to `aareka.aicontransformation.com`.

---

## Deployment modes — where to install, and what access it needs

Aareka collects in one of four modes, selected with the `AAREKA_SOURCE` environment
variable. In every mode the collector is **read-only**: it makes **outbound HTTPS calls
only** to the ingest API, never opens an inbound port on your machines, and never writes to
or issues commands against your systems. Bare-metal (`local`) is stable; the `vcenter`,
`kubernetes`, and `fleet` modes are **beta** — the code is complete and tested against
fixtures, and is validated against your real environment on first connect.

| `AAREKA_SOURCE` | Install it on | Access it needs | Status |
|---|---|---|---|
| `local` | each GPU server | local `nvidia-smi`; outbound HTTPS | stable |
| `vcenter` | one machine that can reach vCenter | read-only vCenter account; outbound HTTPS | beta |
| `kubernetes` | one machine with a kubeconfig, or in-cluster | read-only Kubernetes API + Prometheus; outbound HTTPS | beta |
| `fleet` | one control node | read-only SSH (or Prometheus/BMC) to the servers; outbound HTTPS | beta |

### Bare metal (`local`)

Install the collector on **each GPU server** you want to measure; a single server is enough
for a first test. It reads that machine directly, so it needs:

- the **NVIDIA driver**, so that `nvidia-smi` works — this provides measured GPU power and
  identifies which process is using each GPU;
- optionally, permission to read the CPU energy counter (root on Linux) for measured CPU power;
- optionally, **read-only** access to the server's management controller (BMC / iDRAC / iLO,
  via Redfish or IPMI) for an exact whole-machine total;
- **outbound HTTPS (port 443)** to the ingest API. Nothing needs to be opened inbound.

### VMware / vCenter (`vcenter`)

Install the collector on **one machine that can reach your vCenter over the network**. You do
**not** install anything on the ESXi hosts or inside the virtual machines. A single read-only
connection to vCenter returns everything Aareka needs: each host's total power, every virtual
machine's vCPU and memory allocation, its GPU assignment (passthrough or vGPU), and its
department tag. It needs:

- a **read-only vCenter account**;
- network access to the **vCenter API (TCP 443)**;
- **outbound HTTPS (port 443)** to the ingest API.

### Kubernetes (`kubernetes`)

Install the collector on **one machine that holds a read-only kubeconfig**, or run it inside
the cluster as a pod with a read-only service account. Kubernetes supplies the topology — which
pod runs on which node, with its CPU request, memory, GPU count and labels — while the power
figures come from your monitoring stack. It needs:

- **read-only access to the Kubernetes API (TCP 443)**;
- a **Prometheus** endpoint that scrapes `dcgm-exporter` (GPU power) and `node-exporter`
  (CPU power);
- **outbound HTTPS (port 443)** to the ingest API.

### Fleet — many bare-metal servers from one place (`fleet`)

When you have many bare-metal servers and would rather not install an agent on each one, run
the collector on **one control node** that can already reach them. It sweeps every server for
its total power and attributes each whole server to its owning business unit using the mapping
your IT team provides (see below). It needs:

- **read-only SSH** to each server (or their Prometheus / BMC endpoints);
- **outbound HTTPS (port 443)** to the ingest API.

---

## What we need from your IT team — the ownership mapping

Aareka answers two questions, and it can only answer the second one with your help:

1. **How much power did each workload draw?** — measured automatically, in every mode.
2. **Which team, application, or business unit owns that power?** — this is an *ownership
   fact*, not something that can be inferred from telemetry, so it has to come from you.

Ownership is expressed as a simple mapping, and where it comes from depends on the estate:

- **VMware:** ideally the department is already recorded on each VM as a vCenter **custom
  attribute or tag**, which Aareka reads directly. If it isn't, you can supply a
  VM-name → business-unit mapping instead.
- **Kubernetes:** the owning team is usually already expressed as a **label** (for example
  `app` or `team`) or as the **namespace**, both of which Aareka reads. An explicit mapping
  can override this.
- **Bare metal / fleet:** there is no orchestrator to read, so your IT team provides a simple
  **server → application/business-unit** list (for example, `gpu-node-01 → Research`). This is
  the single most important input for a bare-metal deployment.

In one sentence: the one thing we need from your IT team is **a list of which server, VM, or
pod belongs to which application or business unit.**

## If the ownership mapping isn't available

Aareka never guesses ownership and never blocks on it. If the mapping is missing or only
partial:

- **Power is still measured and attributed** down to each workload, VM, pod, or server, and
  the totals still reconcile. You get an accurate picture of *how much* power is being drawn
  and *where* — which machine, which workload.
- **What you lose is the business roll-up.** Anything without an owner appears in an honest
  **"unmapped"** bucket rather than rolling up to a named department. You will see that, for
  example, 40 kW is unattributed — but not which team it should be charged to.
- **Nothing is fabricated.** Unmapped power is clearly labeled as such; it is never quietly
  assigned to the wrong team.

In practice this means you can deploy and start measuring straight away, and the
department-level showback becomes complete as you fill in the ownership mapping. Providing it
up front simply means your very first report already rolls up cleanly to business units.

See [PREREQUISITES.md](PREREQUISITES.md) for the exact packages, accounts, and environment
variables required by each mode.

---

## The problem

You started running AI in datacenters you own. Your power draw jumped, unpredictably,
and no one can defend which team, application, or pipeline is driving it. The facility
team sees a power spike; the AI teams see their jobs; nobody can connect the two.

- **Facility tools** (DCIM, energy platforms) measure real power — but stop at the
  rack or the building. They don't know what software ran.
- **Cost tools** (OpenCost, Kubecost, Cast AI) attribute to workloads — but by
  multiplying resource-hours by a configured price, or by reading device-level
  telemetry inside Kubernetes. They don't reconcile whole-server energy across a
  mixed, non-Kubernetes estate.

Aareka is the layer between them: it reconciles heterogeneous physical telemetry into
a confidence-scored, whole-server energy ledger, attributed to business ownership,
across private and hybrid estates — including bare metal and virtualized
infrastructure.

---

## What makes the number trustworthy

Not "we show GPU watts" — every GPU already reports that, and so do the utilization
tools. Aareka is built around the accounting gap those tools leave:

- **Measured vs estimated, visibly labeled.** Every reading is tagged `measured`
  (from a physical sensor) or `modeled` (estimated), with an error band. No modeled
  number is ever presented as measured. Aareka produces metered energy figures where
  physical meters exist, and clearly bounded estimates where they do not.
- **Reconciled to server totals.** Component readings are reconciled against a
  whole-box meter where one exists, so nothing is double-counted; a rack meter is
  never mistaken for a server.
- **Honest about what it can't see.** With no whole-box meter, Aareka reports a
  bounded range for the unmeasured tail (fans, storage, PSU loss) — a floor plus a
  stated estimate, not false precision. A device-level GPU sensor is a device reading,
  not the whole server's electrical draw; Aareka is explicit about the difference.
- **Provenance by source and method.** A fallback ladder reads whichever telemetry
  each server exposes and records, for every value, its scope, source, and measurement
  method — hardware meter (BMC/Redfish/IPMI whole-box), device telemetry (DCGM/NVML
  GPU), processor counter (RAPL, an energy counter, not a meter), or model. It
  degrades gracefully, reporting coverage and confidence. Polling is lightweight and
  low-frequency, so it does not saturate BMC interfaces or the management network.
- **Rack meters stay at rack scope.** Rack-PDU readings are retained for rack-level
  reconciliation and coverage; they are never treated as an individual server
  measurement unless a valid allocation mechanism is present.
- **Audit-ready provenance.** Attribution rolls up to app, service, and business unit
  with energy-weighted confidence and per-reading source lineage, suitable for
  internal showback, capacity planning, and review.

---

## How this differs from GPU utilization tools

If you already run NVIDIA Run:ai, Cast AI, Kubecost, ScaleOps, or a DCGM-Exporter →
Prometheus → Grafana stack, you have **GPU power and utilization on Kubernetes**.
Aareka differs on scope and accounting integrity, on three lines:

- **Whole-server energy, not device telemetry.** Those tools read GPU device power and
  utilization. Aareka reconciles the *whole server* — GPU **and** CPU, memory, and the
  unmeasured tail (fans, storage, PSU loss) — against a physical meter where one
  exists. In a real datacenter the non-GPU load is not a rounding error.
- **Accounting integrity, not raw metrics.** A metric is a number; a ledger is a
  number you can defend. Aareka labels every figure measured-or-modeled, reconciles it
  to a total, and surfaces what it could not attribute — rather than presenting a
  device reading as if it were the answer.
- **Your estate, not just pods.** Kubernetes-focused cost and allocation tools depend
  on orchestration metadata. Aareka is designed to preserve attribution where that
  metadata is incomplete or absent — across bare metal, virtualized, and mixed
  estates, where most enterprises actually run their newly-arrived AI.

Utilization tools answer "is this GPU busy, and what does it draw?" Aareka answers
"how much electricity did this workload consume across the whole machine, and can I
defend that number to finance?"

---

## Truth first, action second

A ledger that only reports is inert. Initial recommendations identify attribution
gaps, abnormally high energy intensity, and power-capacity risk — all shaped by
measured energy, not GPU-utilization heuristics. Deeper scheduling recommendations
(which workload to shed, when) require ownership, priority, and SLA context and follow
validated customer data; they are not claimed for the measurement product today.

But it stops at a **recommendation**. On approval, the action is handed to your
existing orchestrator (Kubernetes, Slurm, vSphere). **Aareka never executes.** That
boundary is deliberate: no write path, nothing to compromise.

Deployment is offered in two modes: a cloud-hosted control plane for standard
deployments, and a customer-managed (self-hosted) deployment for restricted or
air-gapped environments where telemetry must not leave the site.

```
[ Physical power & telemetry ]
            |
[ Reconciliation & confidence tagging ]   (measured vs modeled)
            |
[ Workload attribution ]                  (app / service / business unit)
            |
[ Constraint detection & ranked action ]
            |
[ Human approval ]  ->  [ Orchestrator executes ]
```

---

## Where it fits

**Initial focus: enterprise private and hybrid GPU estates with VMware/bare-metal
legacy infrastructure and multiple internal AI teams.** The pain is concrete: you
bought expensive GPU capacity, the facility team sees a power spike, and nobody can
defend which team owns it.

HPC and research clusters fit the same profile — often bare metal, Slurm-scheduled,
with non-standard telemetry where Kubernetes-native cost tools do not operate at
all.

Aareka is optimized for owned and hybrid estates, where cloud-style billing data is
absent and physical power constraints matter most. It is less relevant to pure
public-cloud consumers whose *spend* is already itemized — though energy attribution,
capacity, and emissions accountability remain unmet even there.

---

## Where this is heading

**Component-level breakout — storage and NIC/DPU.** Today storage and network draw are
reconciled *within* the whole-server total but not itemized. A next version breaks them
out as first-class components — measured where the hardware exposes it (Redfish
per-component, NVIDIA DPU telemetry; a BlueField DPU alone can draw ~75 W), modeled
per-device otherwise (per-drive, per-NIC TDP) — and attributes them to the owning
workload, so a team's *full* footprint (compute + memory + storage + network) rolls up
to its department.

A trustworthy IT-load energy ledger is the foundation other layers can eventually sit
on — facility-overhead and PUE allocation, emissions-factor reporting, and workload
placement decisions across cost, energy, and power constraints. These are explicitly
not near-term claims and Aareka does not do them today; they are noted only to show
where a defensible measurement layer leads. Measurement is the product now.

---

## Status

Prototype, validated against simulated and representative telemetry. The live-cluster
and live-BMC integrations are written against the standard interfaces (Redfish, DCGM,
vSphere, Kubernetes) and exercised against mocks; they have not yet run end-to-end in
a production environment. **We are seeking a design partner for the first live
deployment.** Treat this as a working foundation, not a finished product.

---

## Open source and commercial

The collector and connectors are open source under Apache 2.0. The intended
commercial layer is the hosted ledger, enterprise integrations, long-term data
retention and reporting, governance, support, and advanced recommendation logic. The
measurement rigor is open; the operated service around it is the product.

See [LICENSE](LICENSE).

---

## About the name

*Aareka* — from आरेख (*aarekh*, "the chart" / "the diagram") + *eureka*: the moment you
finally see where your power goes. A project of AICON Transformation Solutions.

## Not affiliated with

NVIDIA, or any datacenter or energy vendor. "DCGM", "Redfish", "Kubernetes",
"vSphere" are trademarks of their respective owners, referenced only for
interoperability.
