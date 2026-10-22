"""Extract verified SBOM/provenance JSON from a Docker save OCI archive.

Reads named blobs without extracting archive paths to the filesystem. The archive
and both attestations must refer to the same image manifest for the requested
Linux architecture (amd64 by default).
"""

import argparse
import hashlib
import json
import re
import tarfile
from pathlib import Path


def statements(index, native, blob):
    found = {}
    for descriptor in index["manifests"]:
        if (
            descriptor.get("annotations", {}).get("vnd.docker.reference.type")
            != "attestation-manifest"
        ):
            continue
        if descriptor["annotations"]["vnd.docker.reference.digest"] != native["digest"]:
            raise ValueError("Attestation references a different image")
        for layer in blob(descriptor)["layers"]:
            statement = blob(layer)
            if not any(
                s["digest"].get("sha256") == native["digest"].split(":")[1]
                for s in statement["subject"]
            ):
                raise ValueError("Attestation subject differs from the image")
            predicate_type = statement["predicateType"]
            if predicate_type == "https://spdx.dev/Document":
                found["image.spdx.json"] = statement
            elif predicate_type.startswith("https://slsa.dev/provenance/"):
                found["provenance.json"] = statement
    return found


def extract(archive, output, arch="amd64"):
    with tarfile.open(archive) as stream:

        def read(name):
            member = stream.getmember(name)
            if not member.isfile() or member.size > 32 * 1024 * 1024:
                raise ValueError("Expected a bounded JSON blob")
            return stream.extractfile(member).read()

        def blob(descriptor):
            algorithm, digest = descriptor["digest"].split(":", 1)
            if algorithm != "sha256" or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("Expected SHA256 OCI descriptor")
            data = read("blobs/sha256/" + digest)
            if len(data) != descriptor["size"] or hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("OCI blob differs from its descriptor")
            return json.loads(data)

        root = json.loads(read("index.json"))
        (index_descriptor,) = root["manifests"]
        index = blob(index_descriptor)
        (native,) = [
            m
            for m in index["manifests"]
            if m.get("platform") == {"architecture": arch, "os": "linux"}
        ]
        manifest = blob(native)
        config = blob(manifest["config"])
        found = statements(index, native, blob)
        if set(found) != {"image.spdx.json", "provenance.json"}:
            raise ValueError("Both SPDX SBOM and provenance are required")
        output.mkdir(parents=True, exist_ok=True)
        for name, value in found.items():
            (output / name).write_text(json.dumps(value, indent=2) + "\n")
        receipt = {
            "index_digest": index_descriptor["digest"],
            "architecture": arch,
            f"linux_{arch}_manifest": native["digest"],
            "config_digest": manifest["config"]["digest"],
            "revision": config["config"]["Labels"]["org.opencontainers.image.revision"],
            "sbom_packages": len(found["image.spdx.json"]["predicate"]["packages"]),
            "signed": False,
        }
        (output / "image.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--arch", choices=("amd64", "arm64"), default="amd64")
    args = parser.parse_args()
    extract(args.archive, args.output, args.arch)


if __name__ == "__main__":
    main()
