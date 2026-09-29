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

## Quickstart — send your data to the Aareka pilot

*For design partners we've given a pilot **org key** and a **dashboard login**. The
collector is read-only and makes outbound HTTPS calls only — it sends power readings
and workload ownership, never commands, and nothing else leaves your machine.*

**1. Prerequisites** (details in [PREREQUISITES.md](PREREQUISITES.md))
- **Python 3.9+** — the only hard requirement.
- **GPU power:** the NVIDIA driver, so `nvidia-smi` works (gives *measured* GPU power).
- **Best whole-machine accuracy (optional):** read-only BMC/IPMI/Redfish access.

**2. Get the collector**
```bash
git clone https://github.com/anvitashukla4u-jpg/Aareka_open_source.git
cd Aareka_open_source
```

**3. Point it at the pilot.** The URL is fixed; the key was sent to you privately —
keep it secret, don't commit it.
```bash
export AAREKA_INGEST_URL="https://aareka.aicontransformation.com"
export AAREKA_ORG_KEY="<the org key we emailed you>"
# Optional: label who owns what. Maps a process name or VM name -> a department.
export AAREKA_DEPT_MAP='{"training-job":"Research","inference-svc":"Platform"}'
```

**4. Send one batch and read the receipt**
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

**5. See it on your dashboard.** Go to **aareka.aicontransformation.com**, click
**Create account**, and register with your email, a password you choose, and the
**same org key** from step 3. You land straight on your dashboard — workloads,
departments, and measured-vs-estimated power. Your login only ever shows *your*
organisation's data. (Next time, just **Sign in**.)

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
