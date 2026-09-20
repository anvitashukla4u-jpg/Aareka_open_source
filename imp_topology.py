#!/usr/bin/env python3
"""
IMP  .  topology readers                  (virtualization layer)

Turn a live orchestrator into the Child tree the collector consumes. Each reader
is READ-ONLY and emits the same shape (a Child node), so scenarios compose:

    VMware only      -> vcenter_tree()              host -> VM
    Kubernetes only  -> k8s_tree()                  node -> pod
    K8s on VMware    -> stack k8s pods under the VM that hosts their K8s node

Neither talks to a live cluster here; the real API calls are written out and
exercised against MOCK responses (same discipline as the power ladders). Point
them at a real kubeconfig / vCenter to go live.

What each reader pulls:
  K8s     : nodes, pods, per-pod CPU request, GPU device count, namespace/labels
            -> gives BOTH the CPU share and the app/priority tags
  VMware  : hosts, VMs, per-VM vCPU allocation, host placement
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# The Child shape the readers emit: one node in the topology tree.
@dataclass
class Child:
    cid: str
    cpu_share: float
    cpu_share_method: str = "alloc"
    mem_mb: float = 0.0
    gpu_ids: list = field(default_factory=list)
    children: list = field(default_factory=list)


# ============================================================ Kubernetes
class K8sReader:
    """
    Reads node -> pod topology from the Kubernetes API (read-only kubeconfig).
    Real impl uses the official client:
        from kubernetes import client, config
        config.load_kube_config()            # or load_incluster_config()
        v1 = client.CoreV1Api()
        pods = v1.list_pod_for_all_namespaces().items
    We pull per pod: node, namespace, labels, CPU request, GPU count.
    """

    def __init__(self, kubeconfig: Optional[str] = None):
        self.kubeconfig = kubeconfig

    def _fetch_pods(self) -> list[dict]:
        """Live read from the Kubernetes API (read-only). Requires the
        'kubernetes' package + a readable kubeconfig or in-cluster config.
        Sums CPU/memory requests and GPU count across a pod's containers."""
        try:
            from kubernetes import client, config
        except ImportError as e:
            raise RuntimeError("kubernetes client not installed "
                               "(pip install kubernetes)") from e
        try:
            if self.kubeconfig:
                config.load_kube_config(config_file=self.kubeconfig)
            else:
                try:
                    config.load_incluster_config()
                except Exception:
                    config.load_kube_config()
        except Exception as e:
            raise RuntimeError(f"could not load kube config: {e}") from e

        v1 = client.CoreV1Api()
        out = []
        for p in v1.list_pod_for_all_namespaces(watch=False).items:
            node = p.spec.node_name
            if not node:
                continue                         # unscheduled pod - no host yet
            mc = mib = 0.0
            gpu = 0
            for cont in (p.spec.containers or []):
                req = {}
                if cont.resources and cont.resources.requests:
                    req = cont.resources.requests
                mc += self._cpu_millicores(req.get("cpu", "0"))
                mib += self._mem_mib(req.get("memory", "0"))
                gpu += int(float(req.get("nvidia.com/gpu", 0) or 0))
            out.append({
                "node": node,
                "namespace": p.metadata.namespace,
                "name": p.metadata.name,
                "cpu_request": f"{mc}m",          # millicores -> parsed back
                "memory_request": f"{mib}Mi",     # MiB -> parsed back
                "gpu_count": gpu,
                "labels": dict(p.metadata.labels or {}),
            })
        return out

    @staticmethod
    def _cpu_millicores(req: str) -> float:
        """K8s CPU request -> millicores. '500m' -> 500, '2' -> 2000."""
        if not req:
            return 0.0
        req = req.strip()
        return float(req[:-1]) if req.endswith("m") else float(req) * 1000

    @staticmethod
    def _mem_mib(req: str) -> float:
        """K8s memory request -> MiB. '8Gi'->8192, '512Mi'->512, '2G'->~1907."""
        if not req:
            return 0.0
        req = str(req).strip()
        mult = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
                "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}
        for suf in ("Ki", "Mi", "Gi", "Ti", "K", "M", "G", "T"):
            if req.endswith(suf):
                try:
                    return float(req[:-len(suf)]) * mult[suf] / (1024**2)
                except ValueError:
                    return 0.0
        try:
            return float(req) / (1024**2)          # plain bytes -> MiB
        except ValueError:
            return 0.0

    def tree(self) -> dict[str, list[Child]]:
        """Return {node_name: [Child(pod), ...]} with cpu_share normalised per node."""
        pods = self._fetch_pods()
        by_node: dict[str, list[dict]] = {}
        for p in pods:
            by_node.setdefault(p["node"], []).append(p)

        out: dict[str, list[Child]] = {}
        for node, plist in by_node.items():
            total_mc = sum(self._cpu_millicores(p.get("cpu_request", "0"))
                           for p in plist) or 1e-9
            children = []
            for p in plist:
                mc = self._cpu_millicores(p.get("cpu_request", "0"))
                app = p.get("labels", {}).get("app", p["name"])
                children.append(Child(
                    cid=f"{p['namespace']}/{p['name']}",
                    cpu_share=mc / total_mc,
                    cpu_share_method="alloc",          # request = allocation
                    mem_mb=self._mem_mib(p.get("memory_request", "0")),
                    gpu_ids=[f"{node}-gpu{i}" for i in range(p.get("gpu_count", 0))],
                ))
                children[-1].app = app                 # attach app tag for later
            out[node] = children
        return out


# ============================================================ VMware
class VmwareReader:
    """
    Reads host -> VM topology from vCenter (read-only).
    Real impl uses pyvmomi:
        from pyVim.connect import SmartConnect
        si = SmartConnect(host=vc, user=u, pwd=p)
        for vm in container_view(vim.VirtualMachine):
            vm.runtime.host, vm.config.hardware.numCPU, ...
    We pull per VM: host, vCPU count, name.
    """

    def __init__(self, vcenter: Optional[str] = None):
        self.vcenter = vcenter

    def _fetch_vms(self) -> list[dict]:
        raise NotImplementedError("wire pyvmomi, or inject via _fetch_vms")

    def tree(self) -> dict[str, list[Child]]:
        """Return {host_name: [Child(vm), ...]} with cpu_share by vCPU allocation."""
        vms = self._fetch_vms()
        by_host: dict[str, list[dict]] = {}
        for v in vms:
            by_host.setdefault(v["host"], []).append(v)

        out: dict[str, list[Child]] = {}
        for host, vlist in by_host.items():
            total_vcpu = sum(v.get("vcpu", 0) for v in vlist) or 1e-9
            children = []
            for v in vlist:
                children.append(Child(
                    cid=v["name"],
                    cpu_share=v.get("vcpu", 0) / total_vcpu,
                    cpu_share_method="alloc",
                    mem_mb=float(v.get("mem_mb", v.get("memoryMB", 0.0))),
                    gpu_ids=v.get("gpu_ids", []),      # passthrough GPUs, if any
                ))
            out[host] = children
        return out


# ============================================================ nesting
def stack_k8s_on_vmware(vm_children: list[Child],
                        k8s_by_node: dict[str, list[Child]],
                        vm_to_k8s_node: dict[str, str]) -> list[Child]:
    """
    K8s-on-VMware: attach each VM's pods under that VM.
    vm_to_k8s_node maps a VM name -> the k8s node name it runs (often identical).
    """
    for vm in vm_children:
        knode = vm_to_k8s_node.get(vm.cid)
        if knode and knode in k8s_by_node:
            vm.children = k8s_by_node[knode]
    return vm_children


# ============================================================ demo (mock)
if __name__ == "__main__":
    # --- Kubernetes mock ---
    k = K8sReader()
    k._fetch_pods = lambda: [
        {"node": "node-5", "namespace": "ml", "name": "train-0",
         "cpu_request": "8", "gpu_count": 2, "labels": {"app": "llm-train"}},
        {"node": "node-5", "namespace": "ml", "name": "infer-0",
         "cpu_request": "2", "gpu_count": 1, "labels": {"app": "inference"}},
        {"node": "node-5", "namespace": "sys", "name": "logging",
         "cpu_request": "500m", "gpu_count": 0, "labels": {"app": "fluentd"}},
    ]
    ktree = k.tree()
    print("K8S node -> pods (cpu_share, gpus):")
    for node, pods in ktree.items():
        for p in pods:
            print(f"  {node}  {p.cid:<16} share={p.cpu_share:.2f}  gpus={p.gpu_ids}")

    # --- VMware mock ---
    vm = VmwareReader()
    vm._fetch_vms = lambda: [
        {"host": "esxi-3", "name": "VM-A", "vcpu": 16, "gpu_ids": ["gpu0"]},
        {"host": "esxi-3", "name": "VM-B", "vcpu": 8, "gpu_ids": ["gpu1"]},
        {"host": "esxi-3", "name": "VM-C", "vcpu": 4, "gpu_ids": []},
    ]
    vtree = vm.tree()
    print("\nVMWARE host -> VMs (cpu_share, gpus):")
    for host, vms in vtree.items():
        for v in vms:
            print(f"  {host}  {v.cid:<6} share={v.cpu_share:.2f}  gpus={v.gpu_ids}")

    # --- K8s on VMware: pods from node-5 live inside VM-A ---
    print("\nK8S-ON-VMWARE nested tree:")
    stacked = stack_k8s_on_vmware(vtree["esxi-3"], ktree,
                                  vm_to_k8s_node={"VM-A": "node-5"})
    for v in stacked:
        print(f"  VM {v.cid} share={v.cpu_share:.2f}")
        for pod in v.children:
            print(f"      pod {pod.cid} share={pod.cpu_share:.2f} gpus={pod.gpu_ids}")
