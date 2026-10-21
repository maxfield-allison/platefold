"""Specialize Docker's conditional profile for a zero-capability OCI runtime.

Kubernetes Localhost profiles use the OCI schema, which has no Docker includes,
excludes or archMap fields. Never pass the conditional Docker file to a runtime
that could ignore those conditions and grant privileged syscall rules. One OCI
profile is generated per supported image architecture.
"""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "packaging/container"

# Docker's archMap: the native seccomp architecture plus the compatibility
# architectures the same kernel can execute.
ARCHITECTURES = {
    "amd64": ["SCMP_ARCH_X86_64", "SCMP_ARCH_X86", "SCMP_ARCH_X32"],
    "arm64": ["SCMP_ARCH_AARCH64", "SCMP_ARCH_ARM"],
}
TARGETS = {
    "amd64": "seccomp-kubernetes.json",
    "arm64": "seccomp-kubernetes-arm64.json",
}


def specialize(source, arch="amd64"):
    if arch not in ARCHITECTURES:
        raise ValueError(f"Unsupported architecture: {arch}")
    syscalls = []
    for entry in source["syscalls"]:
        include, exclude = entry.get("includes", {}), entry.get("excludes", {})
        if include.get("minKernel"):
            # Optional ptrace allowance is unnecessary for the renderer.
            continue
        if include.get("caps") or (include.get("arches") and arch not in include["arches"]):
            continue
        if arch in exclude.get("arches", []):
            continue
        syscalls.append(
            {
                key: value
                for key, value in entry.items()
                if key in {"names", "action", "args", "errnoRet"}
            }
        )
    allowed = {
        name for entry in syscalls if entry["action"] == "SCMP_ACT_ALLOW" for name in entry["names"]
    }
    forbidden = {"mount", "bpf", "ptrace", "reboot", "init_module", "open_by_handle_at"}
    if allowed & forbidden or not {"chroot", "clone", "setns", "unshare"} <= allowed:
        raise ValueError("Unexpected privileged or missing browser syscall allowance")
    return {
        "defaultAction": source["defaultAction"],
        "architectures": ARCHITECTURES[arch],
        "syscalls": syscalls,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    source = json.loads((ROOT / "seccomp.json").read_text())
    for arch, name in TARGETS.items():
        value = specialize(source, arch)
        target = ROOT / name
        if args.check:
            if not target.is_file() or json.loads(target.read_text()) != value:
                raise SystemExit(f"Kubernetes seccomp profile {name} is stale")
        else:
            target.write_text(json.dumps(value, indent=2) + "\n")
        print(f"Verified {arch} OCI profile {name} with zero container capabilities")


if __name__ == "__main__":
    main()
