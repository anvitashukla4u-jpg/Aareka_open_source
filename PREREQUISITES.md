# What you need to run the Aareka Collector

The **Collector** is a small program that measures how much electricity your
servers use — and traces it to the workloads using it. It only **reads**. It never
changes anything on your machines, and it never sends commands.

You don't need everything below. Start with the basics; add the rest to get more
accurate numbers. The Collector always uses the best source you give it, labels
every number as **measured** (from a real sensor) or **estimated**, and is honest
when something can't be measured — it never makes a number up.

---

## 1. The basics (to run it at all)

- **Python 3.9 or newer.** That's the only hard requirement — the main program uses
  nothing else, so there's nothing to install to get started.
- **Linux is best.** It also runs on Windows, but Linux lets you measure CPU power
  for real (explained below).

---

## 2. To measure GPU power

- Install the **NVIDIA driver** (the one that gives you the `nvidia-smi` command).
- If you can run `nvidia-smi` and see your GPUs, you're done — you get real,
  measured GPU power automatically.

---

## 3. To measure CPU power

- **On Linux:** the Collector reads the CPU's built-in energy meter automatically —
  a real measurement. You just need permission to read it (usually root).
- **On Windows:** there is no such meter, so CPU power can only be **estimated**.
  For the estimate, install one small package:
  ```bash
  pip install psutil
  ```
  Without it, CPU power is simply left out — never guessed.

---

## 4. For the most accurate whole-machine number (optional, but best)

Servers usually have a small management chip that knows the machine's *total* power
from the wall — including fans, disks, and power-supply loss. It goes by names like
**BMC, iDRAC, iLO, Redfish, or IPMI**.

- Give the Collector **read-only access** to it (its network address and a read-only
  login) and you get the exact whole-machine number.
- Without it, the Collector still works — it just says *"the parts I could measure
  add up to at least X watts; the rest is estimated,"* instead of inventing the
  missing piece.

---

## 5. If you run Kubernetes or VMware (optional)

To automatically match power to your pods or VMs, install the matching helper:

```bash
pip install kubernetes     # for Kubernetes
pip install pyVmomi        # for VMware / vCenter
```

You'll also need normal read-only access — your Kubernetes cluster config, or a
read-only vCenter login.

---

## 6. Try it right now

Measure the machine you're on:
```bash
python imp_server_power_source.py
```

Watch its power live, second by second:
```bash
python imp_live.py
```

No servers or GPUs handy? Run the built-in examples — they show the whole thing
working with realistic made-up data:
```bash
python sim/k8s_sim.py
python sim/vmware_sim.py
```

---

## In short

- **Just want to try it?** Python + the NVIDIA driver. Nothing else.
- **Want exact whole-machine numbers?** Add read-only access to the server's
  management chip (BMC/IPMI).
- **Want it mapped to pods/VMs?** Add the Kubernetes or VMware helper.

Everything it can't measure is shown as an estimate and clearly labeled — never
disguised as a real reading.
