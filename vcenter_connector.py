#!/usr/bin/env python3
"""
IMP  .  vCenter connector   (reads the VMware side of the chain)

One read-only vCenter connection yields everything the VMware power chain needs:

  * host TOTAL power  - the vSphere "Power / Usage" performance counter
                        (power.power.average, Watts). Measured whole-box (PSU via
                        the host's IPMI sensors) -> a node_total, no tail estimate.
  * per-VM topology   - vCPU + memory (memory feeds the memory-aware split).
  * per-VM GPU assignment - passthrough device or vGPU profile -> the mode map
                        the GPU->VM collector routes on.

Real impl uses pyVmomi; the API calls are written out and run against MOCK
responses here (same discipline as the topology/power stubs). Inject via
_fetch_hosts (or point _connect at a live vCenter) to go live.

Real reads (for reference):
    from pyVim.connect import SmartConnect
    si = SmartConnect(host=vc, user=u, pwd=p)          # read-only account
    content = si.RetrieveContent()
    # host power (Watts):
    #   counter 'power.power.average' (group power / name power / rollup average)
    #   content.perfManager.QueryPerf(QuerySpec(entity=host,
    #       metricId=[MetricId(counterId, instance='')], intervalId=20))
    # per VM: vm.config.hardware.numCPU, vm.config.hardware.memoryMB,
    #         vm.runtime.host, and PCI passthrough / vGPU from
    #         vm.config.hardware.device (VirtualPCIPassthrough backing).
    # department (the org fact, never inferred): a vCenter Custom Attribute on
    #         the VM (CustomFieldsManager + vm.customValue), OR a Tag via the
    #         vSphere Automation API (com.vmware.cis.tagging). Which attribute /
    #         tag category = department is configured per customer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class VmInfo:
    name: str
    vcpu: int
    mem_mb: float
    gpu_mode: Optional[str] = None        # "passthrough" | "vgpu" | None
    gpu_ids: list = field(default_factory=list)   # physical GPU id(s) it uses
    vgpu_profile: Optional[str] = None    # e.g. "GRID V100D-8Q"
    department: Optional[str] = None      # from a vCenter custom attribute / tag;
                                          # None -> unmapped (customer must tag)


@dataclass
class HostInfo:
    name: str
    power_w: Optional[float]              # vSphere Power counter; None if unavailable
    vms: list                            # list[VmInfo]

    @property
    def has_power(self) -> bool:
        return self.power_w is not None


class VCenterConnector:
    """Read-only vCenter reader. Override _fetch_hosts (or _connect) for live."""

    def __init__(self, vcenter: Optional[str] = None,
                 user: Optional[str] = None, pwd: Optional[str] = None,
                 department_attribute: Optional[str] = None):
        self.vcenter, self.user, self.pwd = vcenter, user, pwd
        # Which vCenter Custom Attribute (or tag category) name holds the owning
        # department/business-unit. Set per customer; None -> nothing mapped.
        self.department_attribute = department_attribute

    def _connect(self):
        """Open a READ-ONLY pyVmomi connection. Needs 'pyvmomi'. Honors
        AAREKA_VCENTER_INSECURE=1 for self-signed lab certs (default: verify)."""
        try:
            from pyVim.connect import SmartConnect
        except ImportError as e:
            raise RuntimeError("pyVmomi not installed (pip install pyvmomi)") from e
        import os
        import ssl
        if not (self.vcenter and self.user and self.pwd):
            raise RuntimeError("vCenter host/user/password not set "
                               "(AAREKA_VCENTER / _USER / _PASSWORD)")
        ctx = None
        if os.environ.get("AAREKA_VCENTER_INSECURE", "").lower() in ("1", "true", "yes"):
            ctx = ssl._create_unverified_context()
        return SmartConnect(host=self.vcenter, user=self.user, pwd=self.pwd,
                            sslContext=ctx)

    # --- LIVE READ (the only un-fixture-testable layer; needs a real vCenter) ---
    # All pyVmomi calls are isolated in _fetch_hosts + its helpers; they return
    # plain dicts of the SAME shape the mock uses, so hosts() and everything
    # downstream (attribution) is fully testable without vSphere.
    def _fetch_hosts(self) -> list[dict]:
        """Walk the live vCenter inventory READ-ONLY -> host dicts. Requires a
        real vCenter (validated on first connect)."""
        from pyVim.connect import Disconnect
        from pyVmomi import vim
        si = self._connect()
        try:
            content = si.RetrieveContent()
            counter_id = self._power_counter_id(content)
            field_name = self._custom_field_names(content)
            view = content.viewManager.CreateContainerView(
                content.rootFolder, [vim.HostSystem], True)
            try:
                return [self._host_dict(content, h, counter_id, field_name, vim)
                        for h in view.view]
            finally:
                view.Destroy()
        finally:
            Disconnect(si)

    @staticmethod
    def _power_counter_id(content):
        """Resolve the 'power.power.average' (Watts) performance counter id."""
        for c in content.perfManager.perfCounter:
            if (c.groupInfo.key == "power" and c.nameInfo.key == "power"
                    and str(c.rollupType) == "average"):
                return c.key
        return None

    @staticmethod
    def _custom_field_names(content) -> dict:
        cfm = getattr(content, "customFieldsManager", None)
        return {f.key: f.name for f in (cfm.field or [])} if cfm else {}

    def _host_dict(self, content, host, counter_id, field_name, vim) -> dict:
        vms = []
        for vm in (host.vm or []):
            cfg = getattr(vm, "config", None)
            if cfg is None or getattr(cfg, "template", False):
                continue                                  # skip templates / no config
            hw = cfg.hardware
            attrs = {}
            for cv in (getattr(vm, "customValue", None) or []):
                nm = field_name.get(cv.key)
                if nm:
                    attrs[nm] = cv.value
            vms.append({
                "name": vm.name,
                "vcpu": int(getattr(hw, "numCPU", 0) or 0),
                "mem_mb": float(getattr(hw, "memoryMB", 0) or 0),
                "gpu": self._vm_gpu(hw, vim),
                "custom_attributes": attrs,
            })
        return {"name": host.name,
                "power_w": self._host_power_w(content, host, counter_id, vim),
                "vms": vms}

    @staticmethod
    def _host_power_w(content, host, counter_id, vim):
        """Latest host-total power in Watts from the vSphere power counter."""
        if counter_id is None:
            return None
        try:
            metric = vim.PerformanceManager.MetricId(counterId=counter_id, instance="")
            spec = vim.PerformanceManager.QuerySpec(
                entity=host, metricId=[metric], intervalId=20, maxSample=1)
            res = content.perfManager.QueryPerf(querySpec=[spec])
            if res and res[0].value and res[0].value[0].value:
                return float(res[0].value[0].value[-1])
        except Exception:
            return None
        return None

    @staticmethod
    def _vm_gpu(hw, vim) -> dict:
        """passthrough vs vGPU from the VM's virtual PCI devices. The VmiopBacking
        marks a vGPU (and carries the profile); a plain passthrough backing is a
        whole-card DirectPath device."""
        for dev in (getattr(hw, "device", None) or []):
            if isinstance(dev, vim.vm.device.VirtualPCIPassthrough):
                backing = getattr(dev, "backing", None)
                vmiop = getattr(vim.vm.device.VirtualPCIPassthrough, "VmiopBackingInfo", None)
                if vmiop is not None and isinstance(backing, vmiop):
                    return {"mode": "vgpu", "profile": getattr(backing, "vgpu", None),
                            "gpu_ids": []}
                return {"mode": "passthrough", "gpu_ids": []}
        return {}

    def hosts(self) -> list[HostInfo]:
        out = []
        for h in self._fetch_hosts():
            vms = []
            for v in h.get("vms", []):
                g = v.get("gpu") or {}
                # department read from the configured custom attribute / tag.
                attrs = v.get("custom_attributes") or {}
                dept = (attrs.get(self.department_attribute)
                        if self.department_attribute else None) or None
                vms.append(VmInfo(
                    name=v["name"],
                    vcpu=int(v.get("vcpu", 0)),
                    mem_mb=float(v.get("mem_mb", 0.0)),
                    gpu_mode=g.get("mode"),
                    gpu_ids=list(g.get("gpu_ids", [])),
                    vgpu_profile=g.get("profile"),
                    department=dept,
                ))
            out.append(HostInfo(name=h["name"],
                                power_w=h.get("power_w"), vms=vms))
        return out

    # ------------------------------------------------------- derived inputs
    @staticmethod
    def department_map(host: HostInfo) -> dict:
        """VM -> department, from the read custom attribute/tag. Absent -> None
        (the power will land in the honest 'unmapped' bucket)."""
        return {vm.name: vm.department for vm in host.vms}

    @staticmethod
    def untagged_vms(host: HostInfo) -> list:
        """VMs with NO department mapped - the customer must tag these before
        their power can roll up to a department."""
        return [vm.name for vm in host.vms if not vm.department]

    @staticmethod
    def gpu_mode_map(host: HostInfo) -> dict:
        """Per-GPU mode map for the GPU->VM collector, from the VMs' assignments.
        passthrough GPUs get their owning VM; vGPU GPUs are marked shared (the
        slice util/VMs come from nvidia-smi vgpu host-side)."""
        mode_map: dict[str, dict] = {}
        for vm in host.vms:
            if vm.gpu_mode == "passthrough":
                for gid in vm.gpu_ids:
                    mode_map[gid] = {"mode": "passthrough", "owner": vm.name}
            elif vm.gpu_mode in ("vgpu", "mig"):
                for gid in vm.gpu_ids:
                    mode_map.setdefault(gid, {"mode": vm.gpu_mode})
        return mode_map

    @staticmethod
    def topology(host: HostInfo) -> list[dict]:
        """Per-VM vCPU + memory, for the CPU/RAM (memory-aware) split.
        Returned raw so it can feed the split once mem_share lands in the engine."""
        return [{"name": vm.name, "vcpu": vm.vcpu, "mem_mb": vm.mem_mb}
                for vm in host.vms]
